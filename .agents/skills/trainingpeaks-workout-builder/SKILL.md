---
name: trainingpeaks-workout-builder
description: >-
  Build, create, import, or update TrainingPeaks workouts with correct interval
  structure, calibrated pace/power targets, and post-write validation. Use for
  individual workouts or tabular/CSV training plans, including requests to set
  intervals, quantize watch pace targets, preserve coach notes, or reconcile
  planned duration and distance with the structured workout.
---

# TrainingPeaks Workout Builder

## Step 1 — Always fetch athlete settings first

Before writing any intensity value, call `tp_get_athlete_settings` and extract
the relevant threshold:

| Sport | Field to read | Unit |
|-------|--------------|------|
| Run   | `speedZones[workoutTypeId=3].threshold` | m/s |
| Bike  | `powerZones[workoutTypeId=2].threshold` | watts |
| All   | `heartRateZones[0].threshold` | bpm |

> **Note**: `speedZones` may have multiple entries. Prefer `workoutTypeId=3`
> for running. If only `workoutTypeId=0` is present (generic entry), use it as
> the run threshold. Always read the **live** value — it changes as fitness
> updates; do not trust the cached number below without re-checking.

**Run pace conversion** (threshold m/s → min/km):

```
pace_sec_per_km = 1000 / threshold_m_per_s
```

## Step 2 — Convert target pace/power to % of threshold

### Running (`percentOfThresholdPace`)

```
% = (target_speed_m_per_s / threshold_m_per_s) × 100
target_speed_m_per_s = 1000 / target_pace_sec_per_km
```

### Quantize pace before converting

If the athlete's watch displays pace in fixed increments, quantize every range
boundary, point target, and title midpoint **before** converting to percent.
When instructed to round toward lower speed, choose the slower pace (larger
seconds/km): `4:48 -> 4:50`, and a midpoint of `4:37.5 -> 4:40` for a 5-second
increment. Never leave a non-displayable pace such as `4:22` in a title while
the structure uses 5-second targets.

Use `scripts/workout_math.py` for deterministic quantization, pace-to-percent
conversion, midpoint selection, and structure totals. Read
[references/pace-and-validation.md](references/pace-and-validation.md) when a
request includes watch increments, mixed distance/time structures, or a full
CSV plan.

### Cycling (`percentOfFtp`)

```
% = (target_watts / ftp_watts) × 100
```

### Heart rate (`percentOfThresholdHr`)

```
% = (target_bpm / threshold_bpm) × 100
```

## Step 3 — Fetch the target workout

Use `tp_get_workouts` with the exact date, then `tp_get_workout` with the ID
to see the existing structure before overwriting.

For a plan import, fetch the whole date range once to map dates to existing
workout IDs, then fetch each matched workout's details before updating. Update
in place when exactly one planned workout matches the intended cell. Do not
create a duplicate merely because the existing title or structure differs.

## Step 4 — Build the structure

### Length type

**Always use `distance_meters` for running workouts** — this ensures TrainingPeaks
displays intensity as pace (min/km) instead of speed (kph). Only use `duration_seconds`
for timed efforts where distance is truly unknown (e.g. hill sprints, open recovery walks).

| Use `distance_meters` | Use `duration_seconds` |
|-----------------------|----------------------|
| Warm-up / cool-down blocks | Hill sprints (fixed time, variable distance) |
| Tempo / endurance blocks | Recovery walk / jog between sets |
| Interval repeats (500m, 800m, 1km, 2km…) | Open efforts by feel |
| Distance targets (e.g. 5 km tempo) | Fartlek time-based alternations |

**Mixed structures are supported**: distance blocks and time blocks can coexist
in the same workout. `primaryLengthMetric` is detected from the first block.

> ⚠️ **Pace display rule**: Using `duration_seconds` with `percentOfThresholdPace`
> causes TP to display speed (kph). Using `distance_meters` causes TP to display
> pace (min/km). **Default to `distance_meters` for all running steps.**
>
> ⚠️ **Race workouts**: Always use `sport=Run` (not `sport=Race`) for running races.
> `sport=Race` maps to `workoutTypeId=6` (generic) — TP will show speed (kph) instead of pace.
> Only use `sport=Race` for multi-sport or non-running race events.

### `structure` input format (simplified — for `tp_create_workout` / `tp_update_workout`)

```json
{
  "primaryIntensityMetric": "percentOfThresholdPace",
  "steps": [
    {
      "name": "Warm-up",
      "distance_meters": 2000,
      "intensity_min": 60,
      "intensity_max": 80,
      "intensityClass": "warmUp"
    },
    {
      "name": "Tempo",
      "distance_meters": 5000,
      "intensity_min": 101,
      "intensity_max": 108,
      "intensityClass": "active"
    },
    {
      "type": "repetition",
      "reps": 8,
      "steps": [
        {
          "name": "Hard",
          "duration_seconds": 30,
          "intensity_min": 110,
          "intensity_max": 120,
          "intensityClass": "active"
        },
        {
          "name": "Recovery",
          "duration_seconds": 60,
          "intensity_min": 50,
          "intensity_max": 65,
          "intensityClass": "rest"
        }
      ]
    },
    {
      "name": "Cool-down",
      "distance_meters": 2000,
      "intensity_min": 60,
      "intensity_max": 80,
      "intensityClass": "coolDown"
    }
  ]
}
```

`intensityClass` values: `warmUp`, `active`, `rest`, `coolDown`, `other`

### Typical intensity zones (running)

| Zone | %TP Pace | Feel |
|------|----------|------|
| Z1 easy | up to 80% | Conversational |
| Z2 aerobic | 80–90% | Comfortable effort |
| Z3 tempo | 90–100% | Comfortably hard |
| Z4 threshold | 100–106% | Hard, sustainable ~1 hr |
| Z5a VO2 | 106–112% | Very hard |
| Z5b+ anaerobic | 112%+ | Max/near-max |
| Recovery / walk | 50–72% | Easy / walking |

> **Warm-up & cool-down = Zone 1.** Always set warm-up and cool-down steps to
> Zone 1: pace from walk/easy (0) up to **5:50/km**. In % terms cap
> `intensity_max` at **80%** (5:50/km = 80% of the current 4:39/km threshold) and
> use `intensity_min` ≈ **60%** as the easy floor. Do **not** push warm-up/cool-down
> into Z2+ (above 80% / faster than 5:50/km).

### Common workout patterns from this athlete's plan

#### "שינויי קצב" / Fartlek (alternating pace)
Use **`duration_seconds`** for both the hard and moderate blocks. Alternate in repetitions:
```json
{
  "type": "repetition",
  "reps": 10,
  "steps": [
    {"name": "בינוני", "duration_seconds": 180, "intensity_min": 90, "intensity_max": 96, "intensityClass": "active"},
    {"name": "מהיר", "duration_seconds": 60, "intensity_min": 101, "intensity_max": 103, "intensityClass": "active"}
  ]
}
```
For "3 דק׳ בינוני - 1 דק׳ מהיר" style: 3 min moderate (90–96%), 1 min fast (101–103%).
For "1 דק׳ בינוני - 1 דק׳ מהיר" style: 1 min moderate, 1 min fast.

#### "עליות / Hill sprints" (e.g. `10*30 שניות עליה`)
Use **`duration_seconds`** for the effort. The descent recovery uses **`openDuration: true`** so the athlete advances manually with the lap button when they reach the bottom:
```json
{
  "type": "repetition",
  "reps": 10,
  "steps": [
    {"name": "עליה", "duration_seconds": 40, "intensity_min": 112, "intensity_max": 120, "intensityClass": "active", "openDuration": false},
    {"name": "ירידה בהליכה", "duration_seconds": 90, "intensity_min": 55, "intensity_max": 65, "intensityClass": "rest", "openDuration": true}
  ]
}
```
> ⚠️ Always set `openDuration: true` on the descent/recovery step for hill sprints — the actual descent time varies with terrain. The athlete presses lap when ready for the next rep.
> For `10*20 שניות`: use `duration_seconds: 20` on the effort step.

#### "טמפו מתפתח" (Progressive tempo)
Use **`distance_meters`** blocks, progressively faster:
```json
[
  {"name": "טמפו 1", "distance_meters": 2000, "intensity_min": 93, "intensity_max": 96, "intensityClass": "active"},
  {"name": "טמפו 2", "distance_meters": 2000, "intensity_min": 96, "intensity_max": 100, "intensityClass": "active"},
  {"name": "טמפו 3", "distance_meters": 2000, "intensity_min": 100, "intensity_max": 103, "intensityClass": "active"}
]
```

#### "ריצת נפח מתפתח" (Progressive long run: "X ק׳׳מ בינוני + Y ק׳׳מ מהיר" repeating)
```json
{
  "type": "repetition",
  "reps": N,
  "steps": [
    {"name": "בינוני", "distance_meters": 2000, "intensity_min": 90, "intensity_max": 96, "intensityClass": "active"},
    {"name": "מהיר", "distance_meters": 1000, "intensity_min": 96, "intensity_max": 101, "intensityClass": "active"}
  ]
}
```

#### "חימום מורכב" (Two-phase warmup: easy + moderate)
"2 ק׳׳מ חימום נוח + 1 ק׳׳מ בינוני" → two separate warmup steps:
```json
[
  {"name": "חימום קל", "distance_meters": 2000, "intensity_min": 60, "intensity_max": 80, "intensityClass": "warmUp"},
  {"name": "חימום בינוני", "distance_meters": 1000, "intensity_min": 90, "intensity_max": 96, "intensityClass": "warmUp"}
]
```

#### "אינטרוולים מדורגים" (Descending distance intervals: 1600 + 800 + 400)
Multiple independent repetition blocks, each with its own distance and recovery:
- 1600m blocks: `distance_meters: 1600`, intensity at Z4 threshold (103–107%), jog recovery `duration_seconds: 120`
- 800m blocks: `distance_meters: 800`, intensity at Z4/Z5a (107–116%), jog recovery `duration_seconds: 90`
- 400m blocks: `distance_meters: 400`, intensity at Z5b anaerobic (116–127%), static rest `duration_seconds: 60` @ 0%

Use `intensityClass: "rest"` for both jog and static recovery.
- Jog recovery = 72–82% (`intensity_min: 72, intensity_max: 82`)
- **Static rest = 0%** (`intensity_min: 0, intensity_max: 0`) — complete stop, no movement target

**Rest between sets** (e.g. "2 דק׳ הפסקה" before the first set): standalone rest step with `duration_seconds` and **0% intensity**:
```json
{"name": "הפסקה", "duration_seconds": 120, "intensity_min": 0, "intensity_max": 0, "intensityClass": "rest"}
```

#### "ריצה קלה / רכיבה נוחה" (Easy/recovery session)
When a specific pace range is given (e.g. "6:02–6:33/km"), always build an explicit structure:
```python
structure = {
    'primaryIntensityMetric': 'percentOfThresholdPace',
    'steps': [
        {
            'name': 'Easy Run',
            'distance_meters': 6600,   # ~6.6 km at 6:02–6:33/km for 40 min
            'intensity_min': 71,        # 6:33/km = 1000/393/3.5842×100 = 71%
            'intensity_max': 77,        # 6:02/km = 1000/362/3.5842×100 = 77%
            'intensityClass': 'active'
        }
    ]
}
tp_create_workout(date_str=..., sport='Run', title='40 min Easy Run',
                  description='...', duration_minutes=40, distance_km=6.6,
                  structure=structure)
```

When **no specific pace** is given, omit `structure` and pass only `duration_minutes` — TP
auto-generates a single Z1 `active` block (~71–77% for this athlete, ~7 km for 40 min):
```python
tp_create_workout(date_str=..., sport='Run', title='40 min Easy Run',
                  description='...', duration_minutes=40)
```
For easy bike rides, use an explicit structure with `openDuration: true` so the athlete
rides as long as they want (45–90 min) and ends the workout manually with the lap button:
```python
structure = {
    'primaryIntensityMetric': 'percentOfFtp',
    'steps': [
        {
            'name': 'Easy Ride',
            'duration_seconds': 5400,   # 90 min nominal; openDuration overrides this
            'intensity_min': 40,
            'intensity_max': 60,
            'intensityClass': 'active',
            'openDuration': True        # athlete ends when ready (45–90 min range)
        }
    ]
}
tp_create_workout(date_str=..., sport='Bike', title='45-90 min Easy Ride',
                  description='...', duration_minutes=90, structure=structure)
```

> ⚠️ Always set `openDuration: true` on the easy bike step — the ride duration is open
> (45–90 min). The athlete presses lap/stop when they are done.

> **Note**: TP's auto-generated block uses `intensityClass: "active"` with a Z1 pace target
> (71–77%). This is intentional — it gives the watch a pace band to display even on easy days.
>
> **Easy run pace for this athlete**: 6:02–6:33/km = 71–77% of threshold (4:39/km).

#### "טמפו נפח מתפתח" (Long progressive run — 5 blocks of 4km)
"20 ק׳׳מ קצב מתפתח (5:20–4:40) הגברת קצב כל 4 ק׳׳מ" → 5 flat distance blocks:
```json
[
  {"name": "טמפו 1 (5:20/km)", "distance_meters": 4000, "intensity_min": 85, "intensity_max": 88, "intensityClass": "active"},
  {"name": "טמפו 2 (5:10/km)", "distance_meters": 4000, "intensity_min": 88, "intensity_max": 91, "intensityClass": "active"},
  {"name": "טמפו 3 (5:00/km)", "distance_meters": 4000, "intensity_min": 91, "intensity_max": 95, "intensityClass": "active"},
  {"name": "טמפו 4 (4:50/km)", "distance_meters": 4000, "intensity_min": 95, "intensity_max": 98, "intensityClass": "active"},
  {"name": "טמפו 5 (4:40/km)", "distance_meters": 4000, "intensity_min": 98, "intensity_max": 101, "intensityClass": "active"}
]
```
Include pace label in step name so the athlete sees the target on their watch.

## Workout title

Always write the workout `title` in **English**, regardless of the language used in the description or by the user.

### Title format

Title describes only the **main set** — do not mention warm-up or cool-down:

```
{duration/distance} {workout type} ({interval structure @pace})
```

**Examples:**
- `40 min Fartlek (2min @5:10 + 1min @4:40)`
- `5x800m @4:40 + 5x200m (free)`
- `6x800m @4:40 + 6x400m @4:10`
- `12x500m @4:20`
- `8 km Progressive Tempo (5:15→4:45)`
- `80 min Fartlek (8min @5:20 + 2min @4:40)`
- `20 min Easy Run`
- `Herzliya 10K Race | Target 4:40/km → 46:40`

**Rules:**
- Use `@pace` for a range midpoint; apply any watch-increment rule first
- Use `(pace1→pace2)` for progressive efforts
- Never include `W/U`, `C/D`, warm-up distances, or cool-down distances in the title
- For races: `{Name} {distance} Race | Target {pace} → {finish time}`

The `description` field can be in any language (Hebrew, English, etc.).

## Description — preserve source plan / table text

When the session comes from a coach plan, Excel/Sheets week grid, PDF, or any **tabular source**:

1. Put the **original** workout cell or line for that day in **`description`** on `tp_create_workout` / `tp_update_workout`, as close as possible **verbatim** (keep `|` separators, units exactly as written e.g. `ק׳׳מ` vs `ק״מ`, pace ranges, rep notation such as `10*30`).
2. If that week/row has a **notes column** (e.g. `הערות:`) or a week-wide note, append it **below** the main workout line so the athlete sees full coach intent in TrainingPeaks next to the structured chart.
3. Do not replace the source wording in `description` with a paraphrase; structured steps (`structure` / `structured_workout`) can still follow TP naming and % thresholds.

## Step 5 — Call the tool

For **new** workouts: `tp_create_workout` with `structure` param.

For **updates**: `tp_update_workout` with `workout_id` + `structure` param.
Always pass `distance_km` when the workout is distance-based so the metric
field in TP is correct.

Derive `distance_km` from all fixed-distance steps, expanding repetitions.
Recovery distance counts. Open/timed recovery does not. Never copy an old or
hand-estimated distance when it disagrees with the structure (for example,
`3km + 2km + 12x(400m + 200m) + 2km = 14.2km`).

For timed main sets, distinguish the main-set duration named in the title from
the total planned duration. Include fixed warm-up/cool-down time in the total
when it can be estimated; otherwise avoid claiming that the main-set duration
is the whole workout. If a pattern does not divide evenly (for example, 40 min
of `2 min + 1 min`), use complete repetitions plus a final remainder step so
the main set totals exactly the prescribed duration.

**If the `structure` param returns a 400 API error**, fall back to
`structured_workout` (native wire format). The server may be running cached
code; the native format bypasses structure building entirely. See
[native-wire-format.md](native-wire-format.md) for the wire format spec.

## Multiple workouts in one request

When the user provides a full week (or multiple days) at once:
1. Fetch `tp_get_athlete_settings` **once** at the start, reuse the threshold for all workouts.
2. Fetch the date range once and map existing workout IDs.
3. Calculate pace percentages and structure arithmetic up front.
4. Run independent create/update calls in bounded parallel batches.
5. Collect results and report a summary table (day | title | ID).

## Creating workouts from a CSV training plan

When the user provides a CSV file with a multi-week training plan:

### CSV structure (Hebrew weekly plan)
- **Column 1**: Week range (e.g. `19.07-25.07`)
- **Columns 2–8**: Days of the week — `ראשון` (Sun) through `שבת` (Sat)
- **Last column**: `הערות:` — week-wide coach notes; append to every workout description in that row

> ⚠️ **Israeli calendar**: The week starts on **Sunday (ראשון)**. Column 2 = Sunday.
> Always verify: `19.07.2026` = ראשון (Sunday). Map each column to the correct calendar date
> before creating workouts. A 1-day shift error means all workouts land on wrong days.

### Date mapping verification
Before creating workouts, confirm the day-of-week for the first date of each week using Python:
```python
from datetime import date
d = date(2026, 7, 19)
print(d.strftime('%A'))  # Should print 'Sunday'
```

### Batch workflow

1. Parse the CSV in full and map each non-rest cell to an exact date.
2. Fetch settings once and existing workouts once for the complete date range.
3. Precompute quantized paces, percentages, repetition remainders, and fixed
   distance totals before any write.
4. Update existing workouts; create only dates with no intended match.
5. Parallelize only independent cells and respect available concurrency. Reuse
   workers in bounded waves; do not assume one worker per cell is available.
6. After all writes, fetch the full range again and verify count, dates, titles,
   distances, descriptions, and IDs. Fetch detailed workouts to verify structure
   for quality sessions and any item whose arithmetic changed.

Treat worker success messages as provisional: the final range read is the
source of truth. Avoid dispatching a follow-up to a worker until its previous
turn is fully complete, since completion-message races can drop queued work.

### Rest / bike days
- **מנוחה** (rest): skip — do not create a workout.
- **רכיבה נוחה** (easy bike): `sport='Bike'`, `duration_minutes=90`, use structure with `openDuration: true` (see easy bike pattern above).
- **ריצה קלה** (easy run with pace): use explicit structure at 71–77% (see easy run pattern above).

## `tp_create_workout` function signature

```python
tp_create_workout(
    date_str: str,          # "YYYY-MM-DD"
    sport: str,             # "Run", "Bike", "Swim", etc.
    title: str,
    duration_minutes: int | None = None,
    description: str | None = None,
    distance_km: float | None = None,
    tss_planned: float | None = None,
    structure: dict | str | None = None,
    structured_workout: dict | None = None,
    subtype_id: int | None = None,
    tags: str | None = None,
    feeling: int | None = None,
    rpe: int | None = None,
)
```

> ⚠️ The parameter is `date_str`, **not** `workout_date`. Using the wrong name
> raises `TypeError: unexpected keyword argument`.

## Checklist

- [ ] Fetched `tp_get_athlete_settings` and extracted threshold (once per session)
- [ ] Calculated % values from actual pace/power targets
- [ ] Verified workout ID via `tp_get_workouts` before updating
- [ ] Used `distance_meters` for distance targets, `duration_seconds` for timed efforts
- [ ] Passed `distance_km` alongside distance-based structures
- [ ] Quantized every displayed pace and title midpoint before % conversion
- [ ] Expanded repetitions when calculating distance and timed-set totals
- [ ] Made the prescribed main-set duration exact, including any remainder step
- [ ] For easy/recovery sessions with no targets: omit `structure`, pass only `duration_minutes`
- [ ] For multiple workouts: used bounded parallel writes without exceeding concurrency
- [ ] If a source table/plan exists: **verbatim** workout line plus row/week notes in `description`
- [ ] Re-fetched the final date range and verified count, dates, IDs, titles, and planned metrics
- [ ] Re-fetched detailed structures for quality sessions and corrected arithmetic
