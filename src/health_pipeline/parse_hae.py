"""Parse Health Auto Export (iOS) JSON exports into daily records.

Health Auto Export writes files shaped like::

    {"data": {"metrics": [{"name": "...", "units": "...", "data": [{"date": "...", "qty": 1.0}, ...]}, ...],
              "workouts": [...]}}

with daily aggregation turned on, each metric has at most one point per day.
The app's schema has shifted between versions (field names for sleep and
workouts in particular), so the parser accepts the known variants and ignores
anything it does not recognise instead of failing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable

# Health Auto Export metric name -> (daily column, kind)
# kind "qty" takes the point's qty; "hr" splits Min/Avg/Max.
METRIC_MAP: dict[str, str] = {
    "heart_rate_variability": "hrv_ms",
    "resting_heart_rate": "resting_hr_bpm",
    "respiratory_rate": "resp_rate_bpm",
    "blood_oxygen_saturation": "spo2_pct",
    "oxygen_saturation": "spo2_pct",
    "apple_sleeping_wrist_temperature": "wrist_temp_c",
    "sleeping_wrist_temperature": "wrist_temp_c",
    "step_count": "steps",
    "steps": "steps",
    "active_energy": "active_kcal",
    "active_energy_burned": "active_kcal",
    "basal_energy_burned": "basal_kcal",
    "resting_energy": "basal_kcal",
    "walking_running_distance": "distance_km",
    "distance_walking_running": "distance_km",
    "apple_exercise_time": "exercise_min",
    "exercise_time": "exercise_min",
    "weight_body_mass": "weight_kg",
    "body_mass": "weight_kg",
    "weight": "weight_kg",
    "body_fat_percentage": "body_fat_pct",
}

SLEEP_NAMES = {"sleep_analysis", "sleep"}
HEART_RATE_NAMES = {"heart_rate"}


def _date(value: Any) -> str | None:
    """'2026-11-08 00:00:00 +0300' -> '2026-11-08'. Local date as the app wrote it."""
    if not value or not isinstance(value, str) or len(value) < 10:
        return None
    d = value[:10]
    return d if d[4] == "-" and d[7] == "-" else None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):  # {"qty": 1.2, "units": "km"}
        value = value.get("qty")
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def convert(value: float | None, units: str | None, column: str) -> float | None:
    """Normalise to the units the daily schema uses (kcal, km, kg, degC, %, min)."""
    if value is None:
        return None
    u = (units or "").strip().lower()
    if column.endswith("_kcal") and u in {"kj", "kilojoule", "kilojoules"}:
        return value / 4.184
    if column == "distance_km":
        if u in {"mi", "mile", "miles"}:
            return value * 1.609344
        if u in {"m", "meter", "meters"}:
            return value / 1000
    if column == "weight_kg" and u in {"lb", "lbs", "pound", "pounds"}:
        return value * 0.45359237
    if column == "wrist_temp_c" and u in {"degf", "°f", "f"}:
        return (value - 32) * 5 / 9
    if column == "spo2_pct" and value <= 1.0:  # some exports use a 0-1 fraction
        return value * 100
    if column == "exercise_min" and u in {"hr", "h", "hour", "hours"}:
        return value * 60
    return value


def _hours_to_min(v: Any) -> float | None:
    n = _num(v)
    return None if n is None else n * 60


def _parse_sleep(points: Iterable[dict], out: dict[str, dict]) -> None:
    for p in points:
        d = _date(p.get("date")) or _date(p.get("sleepEnd")) or _date(p.get("inBedEnd"))
        if not d:
            continue
        rec = out[d]
        core, deep, rem = _hours_to_min(p.get("core")), _hours_to_min(p.get("deep")), _hours_to_min(p.get("rem"))
        total = _hours_to_min(p.get("totalSleep"))
        if not total:
            total = _hours_to_min(p.get("asleep"))
        if not total and any(x is not None for x in (core, deep, rem)):
            total = sum(x or 0 for x in (core, deep, rem))
        rec.update({k: v for k, v in {
            "sleep_min": total or None,
            "sleep_core_min": core,
            "sleep_deep_min": deep,
            "sleep_rem_min": rem,
            "sleep_awake_min": _hours_to_min(p.get("awake")),
            "in_bed_min": _hours_to_min(p.get("inBed")) or None,
            "sleep_start": (p.get("sleepStart") or p.get("inBedStart") or None),
            "sleep_end": (p.get("sleepEnd") or p.get("inBedEnd") or None),
        }.items() if v is not None})


def _parse_hr(points: Iterable[dict], out: dict[str, dict]) -> None:
    for p in points:
        d = _date(p.get("date"))
        if not d:
            continue
        for key, col in (("Min", "hr_min_bpm"), ("Avg", "hr_avg_bpm"), ("Max", "hr_max_bpm")):
            v = _num(p.get(key, p.get(key.lower())))
            if v is not None:
                out[d][col] = v


def parse_metrics(doc: dict) -> dict[str, dict[str, Any]]:
    """Return {date: {column: value}} for every day the document covers."""
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    metrics = (doc.get("data") or {}).get("metrics") or doc.get("metrics") or []
    for m in metrics:
        name = str(m.get("name", "")).strip().lower()
        points = m.get("data") or []
        if name in SLEEP_NAMES:
            _parse_sleep(points, out)
            continue
        if name in HEART_RATE_NAMES:
            _parse_hr(points, out)
            continue
        col = METRIC_MAP.get(name)
        if not col:
            continue
        units = m.get("units")
        for p in points:
            d = _date(p.get("date"))
            v = convert(_num(p.get("qty")), units, col)
            if d and v is not None:
                out[d][col] = v
    return dict(out)


def _wk_value(w: dict, *keys: str) -> tuple[float | None, str | None]:
    for k in keys:
        v = w.get(k)
        if isinstance(v, dict) and "qty" in v:
            return _num(v), v.get("units")
        if v is not None and not isinstance(v, dict):
            return _num(v), None
    return None, None


def parse_workouts(doc: dict) -> list[dict[str, Any]]:
    rows = []
    for w in (doc.get("data") or {}).get("workouts") or doc.get("workouts") or []:
        start = w.get("start")
        d = _date(start)
        if not d:
            continue
        dur = _num(w.get("duration"))  # seconds in every known version
        dist, du = _wk_value(w, "distance")
        kcal, ku = _wk_value(w, "activeEnergyBurned", "activeEnergy")
        hr = w.get("heartRate") if isinstance(w.get("heartRate"), dict) else {}
        avg_hr, _ = _wk_value(w, "avgHeartRate") if "avgHeartRate" in w else (_num(hr.get("avg")), None)
        max_hr, _ = _wk_value(w, "maxHeartRate") if "maxHeartRate" in w else (_num(hr.get("max")), None)
        rows.append({
            "start": start,
            "date": d,
            "name": w.get("name") or "",
            "duration_min": None if dur is None else dur / 60,
            "distance_km": convert(dist, du, "distance_km"),
            "active_kcal": convert(kcal, ku, "active_kcal"),
            "hr_avg_bpm": avg_hr,
            "hr_max_bpm": max_hr,
        })
    return rows


def load(path_or_bytes) -> dict:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return json.loads(path_or_bytes.decode("utf-8"))
    with open(path_or_bytes, encoding="utf-8") as f:
        return json.load(f)
