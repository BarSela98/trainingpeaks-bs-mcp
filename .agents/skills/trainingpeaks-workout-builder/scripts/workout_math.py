#!/usr/bin/env python3
"""Deterministic pace and simplified TrainingPeaks structure arithmetic."""

from __future__ import annotations

import argparse
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

    args = parser.parse_args()
    if args.command == "pace":
        seconds = quantize_slower(parse_pace(args.value), args.increment)
        percent = (1000 / seconds) / args.threshold * 100
        print(json.dumps({"pace": format_pace(seconds), "seconds_per_km": seconds, "percent": percent}))
    elif args.command == "midpoint":
        seconds = quantize_slower((parse_pace(args.pace_a) + parse_pace(args.pace_b)) / 2, args.increment)
        print(format_pace(seconds))
    else:
        payload = json.loads(args.json_file.read_text())
        steps = payload.get("steps", payload)
        meters = distance_meters(steps)
        seconds = duration_seconds(steps)
        print(json.dumps({"distance_meters": meters, "distance_km": meters / 1000, "timed_seconds": seconds}))


if __name__ == "__main__":
    main()
