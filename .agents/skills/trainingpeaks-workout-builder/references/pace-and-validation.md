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
   available.
2. After each update, fetch the workout detail and confirm repetition counts,
   open-duration flags, pace percentages, description, and planned metrics.
3. After a multi-day plan, fetch the entire range and compare it with the parsed
   source: one intended workout per non-rest cell, no unexpected duplicates,
   correct dates, English titles, and matching distances.
4. Resolve discrepancies instead of merely reporting them when the correct
   value follows deterministically from the source structure.

For mixed time/distance workouts, fixed-distance totals exclude timed or open
steps. Planned duration should represent the whole workout when it can be
computed or sensibly estimated; otherwise make clear that the title duration
describes only the main set.
