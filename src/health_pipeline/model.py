"""Daily record schema: one row per calendar day, one column per metric.

The column order here is the column order of the output CSV. Every column
except ``date`` is optional, because not every source reports every metric
every day.
"""

from __future__ import annotations

DAILY_COLUMNS: list[str] = [
    "date",
    # sleep (minutes, for the night that ended on this date)
    "sleep_min",
    "sleep_deep_min",
    "sleep_rem_min",
    "sleep_core_min",
    "sleep_awake_min",
    "in_bed_min",
    "sleep_start",
    "sleep_end",
    # heart
    "hrv_ms",
    "resting_hr_bpm",
    "hr_min_bpm",
    "hr_avg_bpm",
    "hr_max_bpm",
    "walking_hr_bpm",
    "vo2max",
    # recovery signals
    "resp_rate_bpm",
    "spo2_pct",
    "wrist_temp_c",
    # activity
    "steps",
    "active_kcal",
    "basal_kcal",
    "distance_km",
    "exercise_min",
    "flights",
    # body
    "weight_kg",
    "body_fat_pct",
    "lean_mass_kg",
    "bmi",
]

WORKOUT_COLUMNS: list[str] = [
    "start",
    "date",
    "name",
    "end",
    "duration_min",
    "distance_km",
    "active_kcal",
    "hr_avg_bpm",
    "hr_max_bpm",
]

# Decimal places kept per column when writing. Anything not listed is written as-is.
ROUNDING: dict[str, int] = {
    "sleep_min": 0, "sleep_deep_min": 0, "sleep_rem_min": 0, "sleep_core_min": 0,
    "sleep_awake_min": 0, "in_bed_min": 0,
    "hrv_ms": 1, "resting_hr_bpm": 1, "hr_min_bpm": 0, "hr_avg_bpm": 1, "hr_max_bpm": 0,
    "resp_rate_bpm": 1, "spo2_pct": 1, "wrist_temp_c": 2,
    "steps": 0, "active_kcal": 0, "basal_kcal": 0, "distance_km": 2, "exercise_min": 0,
    "weight_kg": 2, "body_fat_pct": 1, "lean_mass_kg": 2, "bmi": 1,
    "walking_hr_bpm": 1, "vo2max": 1, "flights": 0,
    "duration_min": 1,
}
