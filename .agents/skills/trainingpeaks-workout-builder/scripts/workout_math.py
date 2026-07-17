#!/usr/bin/env python3
"""Deterministic pace and simplified TrainingPeaks structure arithmetic."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def parse_pace(value: str) -> float:
    minutes, seconds = value.split(":", 1)
    return int(minutes) * 60 + float(seconds)


def format_pace(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def quantize_slower(seconds: float, increment: int) -> int:
    return math.ceil(seconds / increment) * increment


def distance_meters(steps: list[dict[str, Any]]) -> float:
    total = 0.0
    for step in steps:
        multiplier = int(step.get("reps", 1)) if step.get("type") == "repetition" else 1
        children = step.get("steps") if step.get("type") == "repetition" else [step]
        total += multiplier * sum(float(child.get("distance_meters", 0)) for child in children)
    return total


def duration_seconds(steps: list[dict[str, Any]]) -> float:
    total = 0.0
    for step in steps:
        multiplier = int(step.get("reps", 1)) if step.get("type") == "repetition" else 1
        children = step.get("steps") if step.get("type") == "repetition" else [step]
        total += multiplier * sum(float(child.get("duration_seconds", 0)) for child in children)
    return total


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in sorted(value.items()) if item is not None}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, float):
        return round(value, 9)
    return value


def fingerprint(value: Any) -> str:
    encoded = json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def equal_intensity_ranges(steps: list[dict[str, Any]], prefix: str = "steps") -> list[str]:
    errors: list[str] = []
    for index, step in enumerate(steps):
        path = f"{prefix}[{index}]"
        if step.get("type") == "repetition":
            errors.extend(equal_intensity_ranges(step.get("steps", []), f"{path}.steps"))
            continue
        if "intensity_min" in step and "intensity_max" in step:
            if float(step["intensity_min"]) >= float(step["intensity_max"]):
                errors.append(path)
        for target_index, target in enumerate(step.get("targets", [])):
            if float(target.get("minValue", 0)) >= float(target.get("maxValue", 0)):
                errors.append(f"{path}.targets[{target_index}]")
        native_children = step.get("steps", [])
        if native_children:
            errors.extend(equal_intensity_ranges(native_children, f"{path}.steps"))
    return errors


def repair_equal_intensity_ranges(
    steps: list[dict[str, Any]],
    *,
    metric: str,
    threshold: float,
    increment: int,
    prefix: str = "steps",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Repair equal ranges in simplified or native TP steps, preserving all other fields."""
    changes: list[dict[str, Any]] = []
    reversed_ranges: list[str] = []

    def repair_pair(container: dict[str, Any], min_key: str, max_key: str, path: str) -> None:
        minimum = float(container[min_key])
        maximum = float(container[max_key])
        if minimum < maximum:
            return
        if minimum > maximum:
            reversed_ranges.append(path)
            return

        before = [minimum, maximum]
        if minimum == 0:
            container[min_key], container[max_key] = 0, 1
        elif metric == "pace":
            target_seconds = 1000 / (threshold * maximum / 100)
            display_target = quantize_slower(target_seconds, increment)
            container[min_key] = (1000 / (display_target + increment)) / threshold * 100
            container[max_key] = (1000 / display_target) / threshold * 100
        else:
            target_value = threshold * maximum / 100
            container[min_key] = max(0, target_value - increment) / threshold * 100

        changes.append({"path": path, "before": before, "after": [container[min_key], container[max_key]]})

    for index, step in enumerate(steps):
        path = f"{prefix}[{index}]"
        if "intensity_min" in step and "intensity_max" in step:
            repair_pair(step, "intensity_min", "intensity_max", path)
        for target_index, target in enumerate(step.get("targets", [])):
            if "minValue" in target and "maxValue" in target:
                repair_pair(target, "minValue", "maxValue", f"{path}.targets[{target_index}]")
        children = step.get("steps", [])
        if children:
            child_changes, child_reversed = repair_equal_intensity_ranges(
                children,
                metric=metric,
                threshold=threshold,
                increment=increment,
                prefix=f"{path}.steps",
            )
            changes.extend(child_changes)
            reversed_ranges.extend(child_reversed)
    return changes, reversed_ranges


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    pace = sub.add_parser("pace")
    pace.add_argument("value")
    pace.add_argument("--threshold", type=float, required=True)
    pace.add_argument("--increment", type=int, default=5)

    midpoint = sub.add_parser("midpoint")
    midpoint.add_argument("pace_a")
    midpoint.add_argument("pace_b")
    midpoint.add_argument("--increment", type=int, default=5)

    summary = sub.add_parser("structure-summary")
    summary.add_argument("json_file", type=Path)

    fingerprint_parser = sub.add_parser("fingerprint")
    fingerprint_parser.add_argument("json_file", type=Path)

    compare = sub.add_parser("compare")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)

    validate_ranges = sub.add_parser("validate-ranges")
    validate_ranges.add_argument("json_file", type=Path)

    repair_ranges = sub.add_parser("repair-equal-ranges")
    repair_ranges.add_argument("json_file", type=Path)
    repair_ranges.add_argument("--metric", choices=("pace", "power", "hr"), required=True)
    repair_ranges.add_argument("--threshold", type=float, required=True)
    repair_ranges.add_argument("--increment", type=int)
    repair_ranges.add_argument("--output", type=Path)

    args = parser.parse_args()
    if args.command == "pace":
        seconds = quantize_slower(parse_pace(args.value), args.increment)
        percent = (1000 / seconds) / args.threshold * 100
        print(json.dumps({"pace": format_pace(seconds), "seconds_per_km": seconds, "percent": percent}))
    elif args.command == "midpoint":
        seconds = quantize_slower((parse_pace(args.pace_a) + parse_pace(args.pace_b)) / 2, args.increment)
        print(format_pace(seconds))
    elif args.command == "structure-summary":
        payload = json.loads(args.json_file.read_text())
        steps = payload.get("steps", payload)
        meters = distance_meters(steps)
        seconds = duration_seconds(steps)
        print(json.dumps({"distance_meters": meters, "distance_km": meters / 1000, "timed_seconds": seconds}))
    elif args.command == "fingerprint":
        print(fingerprint(json.loads(args.json_file.read_text())))
    elif args.command == "compare":
        left = fingerprint(json.loads(args.left.read_text()))
        right = fingerprint(json.loads(args.right.read_text()))
        print(json.dumps({"equal": left == right, "left": left, "right": right}))
    elif args.command == "validate-ranges":
        payload = json.loads(args.json_file.read_text())
        steps = payload.get("steps", payload.get("structure", payload))
        errors = equal_intensity_ranges(steps)
        print(json.dumps({"valid": not errors, "equal_or_reversed_ranges": errors}))
        raise SystemExit(0 if not errors else 1)
    else:
        payload = json.loads(args.json_file.read_text())
        steps = payload.get("steps", payload.get("structure", payload))
        increment = args.increment or (5 if args.metric == "pace" else 1)
        changes, reversed_ranges = repair_equal_intensity_ranges(
            steps,
            metric=args.metric,
            threshold=args.threshold,
            increment=increment,
        )
        if reversed_ranges:
            print(json.dumps({"repaired": 0, "reversed_ranges": reversed_ranges}))
            raise SystemExit(1)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered)
            print(json.dumps({"repaired": len(changes), "changes": changes, "output": str(args.output)}))
        else:
            print(rendered, end="")


if __name__ == "__main__":
    main()
