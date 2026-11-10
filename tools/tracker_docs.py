"""data/*.csv -> the one document the 'Antrenman Defteri' tracker reads: fitbit/daily.

usage: python3 tools/tracker_docs.py OUT.json [--data data] [--state state/sync_state.json] [--days 120]

The document is {"updated": ..., "days": {date: {...}}, "workouts": [...], "insights": {...}}.
"insights" is data/insights.json as written by `health-pipeline insights` (absent if that file is). Keeping
everything in one document means a sync is a single pinned "set" (read its version,
write with if_version), which an unattended run can do without bookkeeping.
Notes and Bevel values stay in the tracker's own metrics/<date> documents; the page
merges both, preferring these numbers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from datetime import date, datetime, timedelta, timezone

# tracker field <- daily.csv column (rhr = overnight heart rate, comparable to what Bevel showed)
FIELDS = {
    "sleepMin": "sleep_min", "deepMin": "sleep_deep_min", "remMin": "sleep_rem_min",
    "lightMin": "sleep_core_min", "awakeMin": "sleep_awake_min",
    "sleepStart": "sleep_start", "sleepEnd": "sleep_end",
    "hrv": "hrv_ms", "rhr": "sleep_hr_bpm", "fitbitRhr": "resting_hr_bpm",
    "rr": "resp_rate_bpm", "spo2": "spo2_pct", "tempDelta": "skin_temp_delta_c",
    "steps": "steps", "kcal": "total_kcal", "activeKcal": "active_kcal",
    "azm": "active_zone_min", "vo2max": "vo2max", "weight": "weight_kg",
}
WORKOUT_FIELDS = {"start": "start", "date": "date", "name": "name", "min": "duration_min",
                  "km": "distance_km", "kcal": "total_kcal", "hr": "hr_avg_bpm"}
TEXT = {"sleepStart", "sleepEnd", "start", "date", "name"}


def _val(field: str, v: str):
    v = (v or "").strip()
    if not v:
        return None
    if field in TEXT:
        return v
    try:
        x = float(v)
    except ValueError:
        return None
    if not math.isfinite(x):  # e.g. skin temperature before Fitbit has a baseline
        return None
    return int(x) if x.is_integer() else x


def _read(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build(data_dir: str, days: int, state_path: str | None = None) -> dict:
    rows = _read(os.path.join(data_dir, "daily.csv"))
    updated = None
    if state_path and os.path.exists(state_path):  # last real data change, so an unchanged run writes an identical file
        with open(state_path, encoding="utf-8") as f:
            updated = json.load(f).get("last_change")
    updated = updated or datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {"updated": updated, "src": "Fitbit", "days": {}, "workouts": []}
    if not rows:
        return out
    last = max(r["date"] for r in rows)
    since = (date.fromisoformat(last) - timedelta(days=days - 1)).isoformat()
    for r in rows:
        if r["date"] < since:
            continue
        d = {f: v for f, c in FIELDS.items() if (v := _val(f, r.get(c))) is not None}
        if d:
            out["days"][r["date"]] = d
    wk_since = (date.fromisoformat(last) - timedelta(days=29)).isoformat()
    for w in _read(os.path.join(data_dir, "workouts.csv")):
        if w.get("date", "") >= wk_since:
            out["workouts"].append({f: v for f, c in WORKOUT_FIELDS.items() if (v := _val(f, w.get(c))) is not None})
    out["workouts"].sort(key=lambda w: w.get("start", ""))
    ins_path = os.path.join(data_dir, "insights.json")
    if os.path.exists(ins_path):
        with open(ins_path, encoding="utf-8") as f:
            ins = json.load(f)
        ins.pop("method", None)  # the page explains the method itself; keep the document small
        out["insights"] = ins
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--data", default="data")
    ap.add_argument("--state", default="state/sync_state.json")
    ap.add_argument("--days", type=int, default=120)
    a = ap.parse_args()
    doc = build(a.data, a.days, a.state)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    ds = sorted(doc["days"])
    print(f"{len(ds)} days ({ds[0] if ds else '-'} .. {ds[-1] if ds else '-'}), {len(doc['workouts'])} workouts, "
          f"{os.path.getsize(a.out)} bytes -> {a.out}")


if __name__ == "__main__":
    main()
