#!/usr/bin/env python3
"""Map a Sunday-first weekly TrainingPeaks CSV into a normalized JSON manifest."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DAY_NAMES = ("ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת")
WEEK_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})-(\d{1,2})\.(\d{1,2})$")


def load_existing(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        workouts = payload
    elif isinstance(payload, dict) and isinstance(payload.get("workouts"), list):
        workouts = payload["workouts"]
    else:
        raise ValueError("existing workouts JSON must be a list or an object with a workouts list")

    by_date: dict[str, list[dict[str, Any]]] = {}
    for index, workout in enumerate(workouts):
        if not isinstance(workout, dict):
            raise ValueError(f"existing workout at index {index} must be an object")
        workout_date = str(workout.get("date", ""))[:10]
        by_date.setdefault(workout_date, []).append(workout)
    return by_date


def parse_week(value: str, year: int) -> date:
    match = WEEK_RE.match(value.strip())
    if not match:
        raise ValueError(f"invalid week range: {value!r}")
    start_day, start_month, end_day, end_month = map(int, match.groups())
    start = date(year, start_month, start_day)
    if start.weekday() != 6:
        raise ValueError(f"week must start on Sunday: {start.isoformat()} is {start:%A}")
    end_year = year + int((end_month, end_day) < (start_month, start_day))
    end = date(end_year, end_month, end_day)
    expected_end = start + timedelta(days=6)
    if end != expected_end:
        raise ValueError(
            f"week must end six days after it starts: expected {expected_end:%d.%m}, got {end:%d.%m}"
        )
    return start


def is_rest(value: str) -> bool:
    normalized = " ".join(value.split()).strip(" .")
    return normalized == "מנוחה"


def allows_rest(value: str) -> bool:
    return "/" in value and "מנוחה" in value


def build_manifest(csv_path: Path, year: int, existing_path: Path | None) -> dict[str, Any]:
    existing = load_existing(existing_path)
    entries: list[dict[str, Any]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError("CSV is empty")
    header = rows[0]
    notes_index = next((i for i, value in enumerate(header) if "הערות" in value), len(header) - 1)
    for row in rows[1:]:
        row += [""] * (len(header) - len(row))
        if not row or not row[0].strip():
            continue
        start = parse_week(row[0], year)
        notes = row[notes_index].strip() if notes_index < len(row) else ""
        for offset, day_name in enumerate(DAY_NAMES):
            cell = row[offset + 1].strip() if offset + 1 < len(row) else ""
            if not cell or is_rest(cell):
                continue
            workout_date = (start + timedelta(days=offset)).isoformat()
            matches = existing.get(workout_date, [])
            entry: dict[str, Any] = {
                "date": workout_date,
                "day": day_name,
                "source_text": cell,
                "notes": notes,
                "description": cell + (f"\n\n{notes}" if notes else ""),
                "operation": "lookup" if existing_path is None else ("create" if not matches else "update"),
            }
            if allows_rest(cell):
                entry["optional_rest"] = True
                if not matches:
                    entry["operation"] = "review"
            if len(matches) == 1:
                entry["existing_id"] = str(matches[0].get("id"))
            elif len(matches) > 1:
                entry["operation"] = "review"
                entry["candidate_ids"] = [str(item.get("id")) for item in matches]
            entries.append(entry)
    dates = [item["date"] for item in entries]
    return {
        "source": str(csv_path),
        "year": year,
        "count": len(entries),
        "start_date": min(dates) if dates else None,
        "end_date": max(dates) if dates else None,
        "entries": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--existing-workouts", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = build_manifest(args.csv_file, args.year, args.existing_workouts)
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
