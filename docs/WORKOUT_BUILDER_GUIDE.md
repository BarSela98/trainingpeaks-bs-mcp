# TrainingPeaks Workout Builder Guide

Generic guide for creating structured workouts in TrainingPeaks for any athlete.

## Step 1: Get Athlete Threshold

Call `tp_get_athlete_settings` to fetch the athlete's current threshold:

| Sport | Field to extract | Unit |
|-------|------------------|------|
| Run   | `speedZones[workoutTypeId=3].threshold` | m/s |
| Bike  | `powerZones[workoutTypeId=2].threshold` | watts |

## Step 2: Convert Target Pace to % of Threshold (Running)

### Formula

```
target_speed_m_s = 1000 / target_pace_sec_km
intensity_percent = (target_speed_m_s / athlete_threshold_m_s) × 100
```

### Example Calculation

For athlete with threshold **3.5842 m/s** (4:39/km):

**To find intensity % for 4:30/km:**
- pace_sec_km = 4×60 + 30 = 270 sec
- target_speed = 1000 / 270 = 3.704 m/s
- % = (3.704 / 3.5842) × 100 = **103.3%**

**To find intensity % for 5:40/km:**
- pace_sec_km = 5×60 + 40 = 340 sec
- target_speed = 1000 / 340 = 2.941 m/s
- % = (2.941 / 3.5842) × 100 = **82.1%**

## Step 3: Use Decimal Precision

⚠️ **Always calculate with 1 decimal place** (e.g., 82.1%, 87.2%, 99.6%, 103.3%)

Do NOT round to whole numbers. Decimal precision ensures accurate pace display on watch and TrainingPeaks app.

## Step 4: Build Workout Structure

### Key Rules

- **Always use `distance_meters`** for running workouts (ensures pace display, not speed)
- **Use `duration_seconds`** only for timed efforts (hill sprints, recovery walks)
- **Intensity zones** (typical):
  - Z1 easy: up to 80%
  - Z2 aerobic: 80–90%
  - Z3 tempo: 90–100%
  - Z4 threshold: 100–106%
  - Z5a VO2: 106–112%
  - Z5b+ anaerobic: 112%+

### Typical Structure Example

```json
{
  "primaryIntensityMetric": "percentOfThresholdPace",
  "steps": [
    {
      "name": "Warm-up",
      "distance_meters": 2000,
      "intensity_min": 70.0,
      "intensity_max": 80.0,
      "intensityClass": "warmUp"
    },
    {
      "name": "Build",
      "distance_meters": 2000,
      "intensity_min": 82.1,
      "intensity_max": 87.2,
      "intensityClass": "active"
    },
    {
      "name": "Rest",
      "duration_seconds": 120,
      "intensity_min": 0.0,
      "intensity_max": 0.0,
      "intensityClass": "rest"
    },
    {
      "type": "repetition",
      "reps": 4,
      "steps": [
        {
          "name": "1600m Hard",
          "distance_meters": 1600,
          "intensity_min": 99.6,
          "intensity_max": 103.3,
          "intensityClass": "active"
        },
        {
          "name": "400m Jog Recovery",
          "distance_meters": 400,
          "intensity_min": 75.0,
          "intensity_max": 81.0,
          "intensityClass": "rest"
        }
      ]
    },
    {
      "name": "Cool-down",
      "distance_meters": 2000,
      "intensity_min": 70.0,
      "intensity_max": 80.0,
      "intensityClass": "coolDown"
    }
  ]
}
```

## Step 5: Create Workout

Call `tp_create_workout` with:
- `date`: YYYY-MM-DD format
- `sport`: "Run", "Bike", "Swim", etc.
- `title`: English description of main set (no warm-up/cool-down)
- `description`: Verbatim workout text (can be Hebrew)
- `distance_km`: Total distance for distance-based workouts
- `duration_minutes`: Estimated duration
- `structure`: The JSON structure above

### Example Parameters

```json
{
  "date": "2026-08-06",
  "sport": "Run",
  "title": "4x1600m @4:35",
  "description": "2 ק״מ חימום בקצב נוח + 2 ק״מ קצב בינוני (5:20-5:40)\n...",
  "distance_km": 14.0,
  "duration_minutes": 77,
  "structure": { ... }
}
```

## Quick Reference: Common Paces (for 4:39/km threshold)

| Pace    | % of threshold | Zone        |
|---------|----------------|-------------|
| 6:00/km | 77.5%          | Z1 easy    |
| 5:40/km | 82.1%          | Z1 easy    |
| 5:20/km | 87.2%          | Z2 aerobic |
| 5:00/km | 93.0%          | Z3 tempo   |
| 4:40/km | 99.6%          | Threshold  |
| 4:30/km | 103.3%         | Z4 threshold |
| 4:00/km | 116.3%         | Z5b+ anaerobic |

> **Note**: Update these values for other athletes by calculating with their individual threshold using the formula in Step 2.

## Common Workout Patterns

### Fartlek (alternating pace)

```json
{
  "type": "repetition",
  "reps": 10,
  "steps": [
    {
      "name": "Moderate",
      "duration_seconds": 180,
      "intensity_min": 85.0,
      "intensity_max": 90.0,
      "intensityClass": "active"
    },
    {
      "name": "Fast",
      "duration_seconds": 60,
      "intensity_min": 101.0,
      "intensity_max": 103.0,
      "intensityClass": "active"
    }
  ]
}
```

### Hill Sprints

```json
{
  "type": "repetition",
  "reps": 10,
  "steps": [
    {
      "name": "Uphill Effort",
      "duration_seconds": 30,
      "intensity_min": 112.0,
      "intensity_max": 120.0,
      "intensityClass": "active",
      "openDuration": false
    },
    {
      "name": "Downhill Recovery",
      "duration_seconds": 90,
      "intensity_min": 55.0,
      "intensity_max": 65.0,
      "intensityClass": "rest",
      "openDuration": true
    }
  ]
}
```

### Progressive Tempo

```json
[
  {
    "name": "Tempo 1",
    "distance_meters": 2000,
    "intensity_min": 93.0,
    "intensity_max": 96.0,
    "intensityClass": "active"
  },
  {
    "name": "Tempo 2",
    "distance_meters": 2000,
    "intensity_min": 96.0,
    "intensity_max": 100.0,
    "intensityClass": "active"
  },
  {
    "name": "Tempo 3",
    "distance_meters": 2000,
    "intensity_min": 100.0,
    "intensity_max": 103.3,
    "intensityClass": "active"
  }
]
```

## Checklist Before Creating Workout

- [ ] Fetched `tp_get_athlete_settings` and have live threshold
- [ ] Calculated all % values with 1 decimal place
- [ ] Used `distance_meters` for distance-based sections
- [ ] Used `duration_seconds` only for timed efforts
- [ ] Included verbatim workout description in `description` field
- [ ] Title describes only the main set (no warm-up/cool-down)
- [ ] All intensity values are decimal format (e.g., 82.1%, not 82%)

## Notes

- Always verify athlete threshold is current before calculating %
- When updating existing workouts, use `tp_update_workout` with the same structure
- Decimal precision is critical for accurate pace display on the watch
- Keep descriptions in the athlete's preferred language
- Title should be in English for consistency
