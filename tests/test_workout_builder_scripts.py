"""Tests for the bundled workout-builder helper scripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load_plan_csv() -> ModuleType:
    script = (
        Path(__file__).parents[1]
        / ".agents"
        / "skills"
        / "trainingpeaks-workout-builder"
        / "scripts"
        / "plan_csv.py"
    )
    spec = importlib.util.spec_from_file_location("workout_builder_plan_csv", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_csv = _load_plan_csv()


def test_parse_week_validates_full_sunday_to_saturday_range() -> None:
    assert plan_csv.parse_week("04.08-10.08", 2024).isoformat() == "2024-08-04"

    with pytest.raises(ValueError, match="must end six days"):
        plan_csv.parse_week("04.08-07.08", 2024)


def test_parse_week_supports_year_rollover() -> None:
    assert plan_csv.parse_week("29.12-04.01", 2024).isoformat() == "2024-12-29"


@pytest.mark.parametrize("wrapped", [False, True])
def test_load_existing_accepts_raw_or_wrapped_workout_lists(tmp_path: Path, wrapped: bool) -> None:
    workouts = [
        {"id": 1, "date": "2026-08-02T00:00:00"},
        {"id": 2, "date": "2026-08-02"},
    ]
    payload: object = {"workouts": workouts} if wrapped else workouts
    source = tmp_path / "workouts.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    assert plan_csv.load_existing(source) == {"2026-08-02": workouts}


def test_load_existing_rejects_invalid_json_shape(tmp_path: Path) -> None:
    source = tmp_path / "workouts.json"
    source.write_text('{"workouts": "invalid"}', encoding="utf-8")

    with pytest.raises(ValueError, match="must be a list"):
        plan_csv.load_existing(source)
