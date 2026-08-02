"""Workout structure builder, validator, and IF/TSS computation.

Converts a simplified step-based structure format into the wire format
expected by the TrainingPeaks API, including cumulative begin/end times
and polyline generation.
"""

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from tp_mcp.tools._validation import format_validation_error

logger = logging.getLogger("tp-mcp")

# Valid intensity classes for workout steps
INTENSITY_CLASSES = {"warmUp", "active", "rest", "coolDown", "other"}

# Valid primary intensity metrics
INTENSITY_METRICS = {"percentOfFtp", "percentOfThresholdHr", "percentOfThresholdPace"}


class SimpleStep(BaseModel):
    """A single workout step in the simplified input format."""

    name: str = Field(min_length=1, max_length=100)
    type: str = Field(default="step")
    duration_seconds: int | None = Field(default=None, gt=0, le=86400)
    distance_meters: int | None = Field(default=None, gt=0, le=200000)
    intensity_min: float = Field(ge=0, le=300)
    intensity_max: float = Field(ge=0, le=300)
    intensityClass: str = Field(default="active")  # noqa: N815
    cadence_min: float | None = Field(default=None, ge=0, le=300)
    cadence_max: float | None = Field(default=None, ge=0, le=300)
    openDuration: bool = Field(default=False)  # noqa: N815

    @field_validator("intensityClass")
    @classmethod
    def check_intensity_class(cls, v: str) -> str:
        if v not in INTENSITY_CLASSES:
            valid = ", ".join(sorted(INTENSITY_CLASSES))
            raise ValueError(f"Invalid intensityClass '{v}'. Valid: {valid}")
        return v

    @model_validator(mode="after")
    def check_step(self) -> "SimpleStep":
        if self.duration_seconds is None and self.distance_meters is None:
            raise ValueError("Either duration_seconds or distance_meters must be provided")
        if self.duration_seconds is not None and self.distance_meters is not None:
            raise ValueError("Only one of duration_seconds or distance_meters can be provided")
        if self.intensity_min > self.intensity_max:
            raise ValueError("intensity_min must be <= intensity_max")
        if (
            self.cadence_min is not None
            and self.cadence_max is not None
            and self.cadence_min > self.cadence_max
        ):
            raise ValueError("cadence_min must be <= cadence_max")
        return self

    @property
    def length_value(self) -> int:
        """Return the configured step length in its native unit."""
        if self.duration_seconds is not None:
            return self.duration_seconds
        assert self.distance_meters is not None
        return self.distance_meters

    @property
    def length_unit(self) -> str:
        """Return the TrainingPeaks wire unit for this step."""
        return "second" if self.duration_seconds is not None else "meter"


class SimpleRepetitionBlock(BaseModel):
    """A repetition block containing multiple steps repeated N times."""

    type: str = Field(default="repetition")
    name: str = Field(default="Repeat")
    reps: int = Field(gt=0, le=100)
    steps: list[SimpleStep] = Field(min_length=1)


class SimpleWorkoutStructure(BaseModel):
    """Top-level simplified structure input from the LLM."""

    primaryIntensityMetric: str = Field(default="percentOfFtp")  # noqa: N815
    steps: list[SimpleStep | SimpleRepetitionBlock] = Field(min_length=1)

    @field_validator("primaryIntensityMetric")
    @classmethod
    def check_metric(cls, v: str) -> str:
        if v not in INTENSITY_METRICS:
            valid = ", ".join(sorted(INTENSITY_METRICS))
            raise ValueError(f"Invalid primaryIntensityMetric '{v}'. Valid: {valid}")
        return v


def _build_step_wire(step: SimpleStep) -> dict[str, Any]:
    """Convert a SimpleStep to wire format."""
    targets: list[dict[str, Any]] = [
        {"minValue": step.intensity_min, "maxValue": step.intensity_max},
    ]
    if step.cadence_min is not None and step.cadence_max is not None:
        targets.append(
            {
                "minValue": step.cadence_min,
                "maxValue": step.cadence_max,
                "unit": "roundOrStridePerMinute",
            }
        )

    return {
        "name": step.name,
        "type": "step",
        "length": {"value": step.length_value, "unit": step.length_unit},
        "targets": targets,
        "intensityClass": step.intensityClass,
        "openDuration": step.openDuration,
    }


def _repetition_length_by_unit(block: SimpleRepetitionBlock, unit: str) -> int:
    """Return a repeated block's total length for one wire unit."""
    return sum(step.length_value for step in block.steps if step.length_unit == unit) * block.reps


def _block_length_by_unit(block: SimpleStep | SimpleRepetitionBlock, unit: str) -> int:
    """Return a block's contribution for one wire unit."""
    if isinstance(block, SimpleRepetitionBlock):
        return _repetition_length_by_unit(block, unit)
    return block.length_value if block.length_unit == unit else 0


def _detect_length_metric(steps: list[SimpleStep | SimpleRepetitionBlock]) -> str:
    """Use distance when any block contains a distance-based step."""
    for block in steps:
        candidates = block.steps if isinstance(block, SimpleRepetitionBlock) else [block]
        if any(step.distance_meters is not None for step in candidates):
            return "distance"
    return "duration"


def _polyline_bar(
    t_start: float, t_end: float, intensity: float, polyline: list[list[float]],
) -> None:
    """Append a rectangular bar to the polyline (TP native format).

    Each segment is drawn as: drop to 0 → rise to intensity → hold → drop to 0.
    """
    polyline.append([round(t_start, 4), 0])
    polyline.append([round(t_start, 4), round(intensity, 4)])
    polyline.append([round(t_end, 4), round(intensity, 4)])
    polyline.append([round(t_end, 4), 0])


def build_wire_structure(structure: SimpleWorkoutStructure) -> dict[str, Any]:
    """Convert simplified structure to TP API wire format.

    Args:
        structure: The simplified workout structure.

    Returns:
        Dict matching the TP API structure format.
    """
    wire_blocks: list[dict[str, Any]] = []
    primary_length_metric = _detect_length_metric(structure.steps)
    primary_unit = "meter" if primary_length_metric == "distance" else "second"
    cumulative = {"meter": 0, "second": 0}

    for block in structure.steps:
        if isinstance(block, SimpleRepetitionBlock):
            block_units = {step.length_unit for step in block.steps}
            wire_unit = primary_unit if primary_unit in block_units else next(iter(block_units))
            block_length = _repetition_length_by_unit(block, wire_unit)
            begin = cumulative[wire_unit]
            cumulative[wire_unit] += block_length
            end = cumulative[wire_unit]

            # Advance the non-wire unit too so later blocks use the right offset.
            for unit in block_units - {wire_unit}:
                cumulative[unit] += _repetition_length_by_unit(block, unit)

            wire_blocks.append(
                {
                    "type": "repetition",
                    "length": {"value": block.reps, "unit": "repetition"},
                    "steps": [_build_step_wire(step) for step in block.steps],
                    "begin": begin,
                    "end": end,
                }
            )
        else:
            begin = cumulative[block.length_unit]
            cumulative[block.length_unit] += block.length_value
            wire_blocks.append(
                {
                    "type": "step",
                    "length": {"value": 1, "unit": "repetition"},
                    "steps": [_build_step_wire(block)],
                    "begin": begin,
                    "end": cumulative[block.length_unit],
                }
            )

    totals = {
        unit: sum(_block_length_by_unit(block, unit) for block in structure.steps)
        for unit in ("meter", "second")
    }
    polyline: list[list[float]] = []
    poly_cumulative = {"meter": 0, "second": 0}

    for block in structure.steps:
        steps = block.steps if isinstance(block, SimpleRepetitionBlock) else [block]
        repetitions = block.reps if isinstance(block, SimpleRepetitionBlock) else 1
        for _rep in range(repetitions):
            for step in steps:
                unit = step.length_unit
                total = totals[unit]
                start = poly_cumulative[unit] / total if total else 0
                poly_cumulative[unit] += step.length_value
                poly_end = poly_cumulative[unit] / total if total else 0
                _polyline_bar(start, poly_end, step.intensity_max / 100.0, polyline)

    result: dict[str, Any] = {
        "structure": wire_blocks,
        "polyline": polyline,
        "primaryLengthMetric": primary_length_metric,
        "primaryIntensityMetric": structure.primaryIntensityMetric,
        "primaryIntensityTargetOrRange": "range",
    }
    if primary_length_metric == "distance":
        result["visualizationDistanceUnit"] = "kilometer"
    return result


def compute_if_tss(structure: SimpleWorkoutStructure) -> tuple[float, float, int]:
    """Compute IF and TSS from a time-only workout structure.

    Uses NP-style time-weighted 4th-power average of midpoint intensities.
    IF = (weighted_sum / total_seconds) ^ 0.25 / 100
    TSS = (total_seconds * IF^2 * 100) / 3600

    Args:
        structure: The simplified workout structure.

    Returns:
        Tuple of (IF, TSS, total_duration_seconds).
    """
    weighted_sum = 0.0
    total_seconds = 0

    for block in structure.steps:
        if isinstance(block, SimpleRepetitionBlock):
            for _rep in range(block.reps):
                for step in block.steps:
                    if step.duration_seconds is None:
                        return 0.0, 0.0, 0
                    midpoint = (step.intensity_min + step.intensity_max) / 2.0
                    weighted_sum += step.duration_seconds * (midpoint ** 4)
                    total_seconds += step.duration_seconds
        else:
            if block.duration_seconds is None:
                return 0.0, 0.0, 0
            midpoint = (block.intensity_min + block.intensity_max) / 2.0
            weighted_sum += block.duration_seconds * (midpoint ** 4)
            total_seconds += block.duration_seconds

    if total_seconds == 0:
        return 0.0, 0.0, 0

    intensity_factor = (weighted_sum / total_seconds) ** 0.25 / 100.0
    tss = (total_seconds * intensity_factor ** 2 * 100.0) / 3600.0

    return round(intensity_factor, 3), round(tss, 1), total_seconds


def parse_structure_input(structure_input: dict[str, Any] | str) -> SimpleWorkoutStructure:
    """Parse structure input from either a dict or JSON string.

    Args:
        structure_input: Structure as dict or JSON string.

    Returns:
        Parsed SimpleWorkoutStructure.

    Raises:
        ValidationError: If structure is invalid.
        ValueError: If JSON is malformed.
    """
    if isinstance(structure_input, str):
        try:
            data = json.loads(structure_input)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in structure: {e}") from e
    else:
        data = structure_input

    # Parse steps - distinguish between simple steps and repetition blocks
    raw_steps = data.get("steps", [])
    parsed_steps: list[SimpleStep | SimpleRepetitionBlock] = []

    for raw_step in raw_steps:
        if raw_step.get("type") == "repetition":
            parsed_steps.append(SimpleRepetitionBlock.model_validate(raw_step))
        else:
            parsed_steps.append(SimpleStep.model_validate(raw_step))

    return SimpleWorkoutStructure(
        primaryIntensityMetric=data.get("primaryIntensityMetric", "percentOfFtp"),
        steps=parsed_steps,
    )


async def tp_validate_structure(structure: str) -> dict[str, Any]:
    """Validate a workout interval structure without creating a workout.

    Args:
        structure: JSON string of the simplified structure format.

    Returns:
        Dict with validation result (block count, total duration, metric) or error.
    """
    try:
        parsed = parse_structure_input(structure)
    except (ValidationError, ValueError) as e:
        msg = format_validation_error(e) if isinstance(e, ValidationError) else str(e)
        return {
            "isError": True,
            "error_code": "VALIDATION_ERROR",
            "message": msg,
        }

    intensity_factor, tss, total_seconds = compute_if_tss(parsed)

    # Count blocks
    block_count = len(parsed.steps)
    step_count = 0
    for block in parsed.steps:
        if isinstance(block, SimpleRepetitionBlock):
            step_count += len(block.steps) * block.reps
        else:
            step_count += 1

    total_distance = sum(_block_length_by_unit(block, "meter") for block in parsed.steps)
    total_time = sum(_block_length_by_unit(block, "second") for block in parsed.steps)
    is_mixed = total_distance > 0 and total_time > 0
    length_metric = _detect_length_metric(parsed.steps)

    result: dict[str, Any] = {
        "valid": True,
        "block_count": block_count,
        "total_steps": step_count,
        "length_metric": "mixed" if is_mixed else length_metric,
        "intensity_metric": parsed.primaryIntensityMetric,
    }
    if total_distance:
        result["total_distance_meters"] = total_distance
        result["total_distance_km"] = round(total_distance / 1000, 3)
    if total_time:
        result["total_duration_seconds"] = total_time
        result["total_duration_minutes"] = round(total_time / 60, 1)
    if length_metric == "duration" and not is_mixed:
        result["estimated_if"] = intensity_factor
        result["estimated_tss"] = tss
    return result
