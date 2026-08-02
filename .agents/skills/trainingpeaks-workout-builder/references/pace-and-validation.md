# Pace Quantization and Plan Validation

## Pace policy

Apply the athlete's display increment before converting pace to threshold
percentage. For a 5-second watch increment and a preference for lower speed:

- Quantize a pace upward in seconds/km: `4:48 -> 4:50`.
- Quantize an exact boundary only once: `4:45 -> 4:45`.
- Quantize a range midpoint toward slower pace: midpoint `4:37.5 -> 4:40`.
- Use the quantized values in step names, titles, descriptions added by the
  builder, and intensity calculations. Preserve the coach's source text
  verbatim even when it contains unquantized values.

Convert after quantization:

```text
percentOfThresholdPace = (1000 / pace_seconds_per_km) / threshold_m_per_s * 100
```

Do not round the percentage to an integer if exact display behavior matters.
Retain enough precision for TrainingPeaks to reconstruct the requested pace.

Never encode a named target as equal minimum and maximum values. If only one
pace is prescribed, use one display increment on its slower side. For example,
encode `5:30/km` as `5:35–5:30/km`, not as `5:30–5:30/km`. This keeps the watch
target achievable without asking the athlete to run faster than prescribed.
Use 0–1% for static rest instead of 0–0%.

For an existing point target, prefer deterministic repair:

```bash
workout_math.py repair-equal-ranges workout.json \
  --metric pace --threshold LIVE_MPS --increment 5 --output fixed.json
```

The repair must preserve the faster target boundary and add one display step
on its slower side (`4:50` becomes `4:55–4:50`). If the original percentage
maps to a non-displayable pace such as `4:48`, quantize it first, producing
`4:55–4:50`. Static `0–0` becomes `0–1`. Reversed ranges are reported, not
automatically swapped, because their intended target is ambiguous.

## Arithmetic rules

Expand repetition blocks when totaling fixed distance:

```text
total = standalone fixed distance
      + reps * sum(fixed distance within repetition)
```

Example: `3km easy + 2km moderate + 12x(400m + 200m jog) + 2km easy`
is `14.2km`, not `13.2km`. Recovery distance is part of the workout.

For a prescribed timed main set, compute:

```text
full_reps = total_seconds // cycle_seconds
remainder = total_seconds % cycle_seconds
```

Add a final partial step following the cycle order when the remainder is not
zero. Example: 40 minutes of `2 min moderate + 1 min fast` becomes 13 complete
cycles plus 1 minute moderate.

## Validation layers

1. Before writing, run local arithmetic and `tp_validate_structure` when
   available. Run `workout_math.py validate-ranges` and require zero errors.
2. After each update, fetch the workout detail and confirm repetition counts,
   open-duration flags, pace percentages, description, and planned metrics.
   Recursively validate the native `targets` arrays; do not validate only the
   simplified payload that was sent.
3. After a multi-day plan, fetch the entire range and compare it with the parsed
   source: one intended workout per non-rest cell, no unexpected duplicates,
   correct dates, English titles, and matching distances.
4. Resolve discrepancies instead of merely reporting them when the correct
   value follows deterministically from the source structure.

For mixed time/distance workouts, fixed-distance totals exclude timed or open
steps. Planned duration should represent the whole workout when it can be
computed or sensibly estimated; otherwise make clear that the title duration
describes only the main set.

## Fast manifest workflow

Create one manifest before writing. Keep the source text, notes, date, intended
operation, existing ID, final payload, arithmetic summary, and fingerprint in
each item. This makes planning reviewable and prevents repeated parsing.

Use `scripts/plan_csv.py` for deterministic CSV/date mapping. Add final workout
payloads after semantic interpretation, then run the `fingerprint` command from
`scripts/workout_math.py` on comparable normalized payloads. Skip a write only when both
payloads contain the complete same field set. A calendar summary is not enough
to prove that structures match.

After writes, use one range fetch for broad verification. Reserve detailed
fetches for quality workouts, errors, duplicates, and changed arithmetic.
