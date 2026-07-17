# Native Wire Format (structured_workout)

Use this when `structure` param returns a 400 error (server running cached code).

Pass directly to `structured_workout` param of `tp_create_workout` / `tp_update_workout`.

## Structure

```json
{
  "structure": [ /* blocks, see below */ ],
  "polyline": [ /* [x, y] pairs, see below */ ],
  "primaryLengthMetric": "distance" | "duration",
  "primaryIntensityMetric": "percentOfThresholdPace" | "percentOfFtp" | "percentOfThresholdHr",
  "primaryIntensityTargetOrRange": "range"
}
```

## Single step block

```json
{
  "type": "step",
  "length": {"value": 1, "unit": "repetition"},
  "steps": [{
    "name": "Block name",
    "type": "step",
    "length": {"value": 3000, "unit": "meter"},
    "targets": [{"minValue": 82, "maxValue": 88}],
    "intensityClass": "warmUp",
    "openDuration": false
  }],
  "begin": 0,
  "end": 3000
}
```

`unit` in inner step `length`: `"meter"` | `"second"` | `"mile"`

## Repetition block

```json
{
  "type": "repetition",
  "length": {"value": 10, "unit": "repetition"},
  "steps": [
    {
      "name": "Hard",
      "type": "step",
      "length": {"value": 30, "unit": "second"},
      "targets": [{"minValue": 110, "maxValue": 120}],
      "intensityClass": "active",
      "openDuration": false
    },
    {
      "name": "Recovery",
      "type": "step",
      "length": {"value": 60, "unit": "second"},
      "targets": [{"minValue": 50, "maxValue": 65}],
      "intensityClass": "rest",
      "openDuration": false
    }
  ],
  "begin": 0,
  "end": 900
}
```

## begin / end rules

- Distance blocks: cumulative meters across all distance blocks
- Time blocks: cumulative seconds across all time blocks (independent counter)
- Mixed workouts: each block type uses its own cumulative counter

## Polyline

Flat list of `[x, y]` where x ∈ [0,1] (normalized position) and y = intensity/100.

Each step is drawn as 4 points (rectangular bar):
```
[x_start, 0], [x_start, intensity/100], [x_end, intensity/100], [x_end, 0]
```

Normalization:
- Distance blocks: `x = cumulative_meters / total_meters`
- Time blocks: `x = cumulative_seconds / total_seconds` (separate 0→1 scale)

### Example polyline for mixed workout

```
Distance blocks (total 11000m):
  warmup 3000m:   x 0.000 → 0.273
  tempo  6000m:   x 0.273 → 0.818
  cooldown 2000m: x 0.818 → 1.000

Time blocks (10 reps × 90s = 900s):
  rep 0 hard  30s: x 0.000 → 0.033
  rep 0 rest  60s: x 0.033 → 0.100
  rep 1 hard  30s: x 0.100 → 0.133
  ...
  rep 9 rest  60s: x 0.933 → 1.000
```
