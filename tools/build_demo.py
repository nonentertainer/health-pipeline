"""Build the public demo of the dashboard (docs/, served by GitHub Pages).

    python3 tools/build_demo.py            # write docs/index.html and docs/demo.js
    python3 tools/build_demo.py --check    # exit 1 if the committed files are stale

No real data is involved. A seeded generator invents one person's 97 days of tables in the
pipeline's own schema; those tables then go through the same code the real deployment runs
(``insights.build`` and ``tracker_docs.build``), and the result is embedded next to an
in-memory database shim (dashboard/demo-shim.js). The page itself, dashboard/index.html,
is copied byte for byte apart from one added <script> tag.

The weekly report card is filled the same way: ``report.build`` over the synthetic tables
and log, for the last complete week.

Two things are planted on purpose so the analysis has something to find:
a later bedtime lowers that night's HRV, and one week has a feverish illness.
Steps and training are generated independently of recovery and should stay "unclear".
"""

from __future__ import annotations

import argparse
import csv
import functools
import json
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tracker_docs  # noqa: E402
from health_pipeline import insights, report  # noqa: E402
from health_pipeline.model import DAILY_COLUMNS, ROUNDING, WORKOUT_COLUMNS  # noqa: E402

END = date(2026, 3, 1)          # a Sunday; the shim moves everything forward in whole weeks
DAYS = 97                       # 90 days of history plus the week "today" can fall in
DIET_DAYS = 30                  # the diet starts this many days before END
ILL = range(46, 50)             # day indexes of the planted illness
SEED = 11
CONFIG = {"goal": 82, "startW": 93, "milestone": 87,
          "gymEx": ["Bench", "Pull", "Face pull", "Triceps", "Biceps", "Leg press"],
          "steps": {"pull": 10, "leg press": 10, "face pull": 5}}
# weekday (Mon=0) -> planned session, the same plan the dashboard shows
PLAN = {0: "cardio", 1: "gym", 2: "cardio", 3: "gym", 4: "rest", 5: "gym", 6: "rest"}
START_KG = {"Bench": 40, "Pull": 55, "Face pull": 20, "Triceps": 25, "Biceps": 12.5, "Leg press": 90}
NOTES = ["Enerji iyi. Akşam hafif yedim.", "Öğleden sonra açlık bastırdı, yoğurtla geçti.",
         "Geç yattım, sabah ağır kalktım.", "Koşu rahat geçti, nabız düşük kaldı.",
         "Dışarıda yemek: porsiyonu yarıda bıraktım.", "Su az içtim, yarın telafi."]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def synth(seed: int = SEED) -> dict:
    """Tables and documents for one invented person. Deterministic for a given seed."""
    rng = random.Random(seed)
    first = END - timedelta(days=DAYS - 1)
    diet_start = END - timedelta(days=DIET_DAYS)
    daily, workouts, day_docs, posture = [], [], [], []
    kg = dict(START_KG)
    gym_count, cardio_count = 0, 0
    for i in range(DAYS):
        d = first + timedelta(days=i)
        ill = i in ILL
        recent = i >= DAYS - 17
        row = {"date": d.isoformat()}

        # ---- the day's movement (independent of recovery by construction) ----
        steps = 3200 + rng.random() * 1500 if ill else _clamp(rng.gauss(9500, 2800), 2500, 17000)
        plan = PLAN[d.weekday()]
        done = None
        if not ill and plan != "rest" and rng.random() < 0.86:
            done = plan
        w_kcal = w_min = azm = 0
        hh, mm = 18 + rng.randrange(0, 3), rng.randrange(0, 60)
        start = datetime(d.year, d.month, d.day, hh, mm)
        if done == "cardio":
            cardio_count += 1
            run = cardio_count % 2 == 1
            w_min = rng.uniform(34, 50) if run else rng.uniform(45, 62)
            km = w_min / rng.uniform(6.3, 7.2) if run else w_min * rng.uniform(0.38, 0.45)
            hr = rng.uniform(141, 152) if run else rng.uniform(124, 135)
            w_kcal = w_min * (11.5 if run else 8.5)
            azm = w_min * (1.6 if run else 1.0)
            workouts.append({"start": start, "name": "Run" if run else "Bike", "duration_min": w_min, "distance_km": km,
                             "total_kcal": w_kcal, "steps": w_min * 165 if run else 0, "hr_avg_bpm": hr, "hr_max_bpm": hr + 18})
        elif done == "gym":
            w_min, hr = rng.uniform(50, 66), rng.uniform(104, 118)
            w_kcal, azm = w_min * 6.2, w_min * 0.35
            workouts.append({"start": start, "name": "Weights", "duration_min": w_min, "distance_km": 0,
                             "total_kcal": w_kcal, "steps": 600, "hr_avg_bpm": hr, "hr_max_bpm": hr + 25})
        elif not ill and rng.random() < 0.6:
            w_min = rng.uniform(28, 70)
            w_kcal = w_min * 5.2
            workouts.append({"start": start, "name": "Walk", "duration_min": w_min, "distance_km": w_min * 0.085,
                             "total_kcal": w_kcal, "steps": w_min * 105, "hr_avg_bpm": rng.uniform(96, 110),
                             "hr_max_bpm": 124})
        active = steps * 0.045 + w_kcal
        row.update(steps=steps, distance_km=steps * 0.00074, active_kcal=active, total_kcal=1950 + active,
                   exercise_min=w_min + steps / 900, active_zone_min=azm, flights=rng.randrange(2, 14),
                   vo2max=44 + 0.012 * i)

        # ---- the night that ended this morning ----
        if recent or ill or rng.random() > 0.06:                       # the watch is off a few nights
            onset = _clamp(rng.gauss(410, 55) + (35 if d.weekday() >= 5 else 0), 270, 570)   # minutes after 18:00
            asleep = _clamp(rng.gauss(432, 38) - 0.25 * (onset - 410), 300, 540)
            awake = _clamp(rng.gauss(40, 10), 15, 80)
            s = datetime(d.year, d.month, d.day) - timedelta(hours=6) + timedelta(minutes=onset)
            deep, rem = asleep * rng.uniform(0.16, 0.22), asleep * rng.uniform(0.19, 0.25)
            hrv = 40 + 0.05 * i - 0.04 * (onset - 410) + rng.gauss(0, 2.2) - (9 if ill else 0)
            shr = 56 - 0.02 * i + 0.008 * (onset - 410) + rng.gauss(0, 1.3) + (6 if ill else 0)
            row.update(sleep_min=asleep, sleep_deep_min=deep, sleep_rem_min=rem, sleep_core_min=asleep - deep - rem,
                       sleep_awake_min=awake, in_bed_min=asleep + awake,
                       sleep_start=s.strftime("%Y-%m-%d %H:%M"),
                       sleep_end=(s + timedelta(minutes=asleep + awake)).strftime("%Y-%m-%d %H:%M"),
                       hrv_ms=hrv, sleep_hr_bpm=shr, resting_hr_bpm=shr + 7 + rng.gauss(0, 1),
                       resp_rate_bpm=14.8 + rng.gauss(0, 0.4) + (1.6 if ill else 0), spo2_pct=96.6 + rng.gauss(0, 0.5),
                       skin_temp_delta_c=rng.uniform(0.7, 1.2) if ill else rng.gauss(0, 0.18))
        daily.append(row)

        # ---- what the person typed into the dashboard ----
        doc = {"date": d.isoformat(), "weight": None, "waist": None, "notes": "", "sessions": [],
               "updatedAt": d.isoformat() + "T19:30:00.000Z"}
        since = (d - diet_start).days
        if since >= 0:
            if rng.random() < 0.86 or since == 0 or recent:
                doc["weight"] = round(CONFIG["startW"] - 0.105 * since + rng.gauss(0, 0.33) + (0.4 if since < 2 else 0), 2)
            if since % 7 == 0:
                doc["waist"] = round(99 - 0.4 * since / 7 + rng.gauss(0, 0.2), 1)
            if rng.random() < 0.3:
                doc["notes"] = rng.choice(NOTES)
        if done == "gym" and i >= 30:
            gym_count += 1
            for name in CONFIG["gymEx"]:
                if gym_count > 1 and rng.random() < 0.12:
                    kg[name] += CONFIG["steps"].get(name.lower(), 2.5)
            doc["sessions"].append({"kind": "gym", "ex": [{"name": n, "kg": kg[n], "sets": None, "reps": None}
                                                           for n in CONFIG["gymEx"]]})
        if doc["weight"] is not None or doc["waist"] is not None or doc["notes"] or doc["sessions"]:
            day_docs.append(doc)
        if i >= DAYS - 20:
            n = rng.choice([2, 3, 3, 3]) if not ill else 1
            posture.append({"date": d.isoformat(), "lies": [], "routine": {}, "updatedAt": d.isoformat() + "T16:00:00.000Z",
                            "rounds": [{"how": "guided", "t": f"{d.isoformat()}T{h}:00.000Z"}
                                       for h in ("06:40", "10:15", "15:30")[:n]]})

    workout_rows = [{**w, "date": w["start"].date().isoformat(), "start": w["start"].strftime("%Y-%m-%d %H:%M"),
                     "end": (w["start"] + timedelta(minutes=w["duration_min"])).strftime("%Y-%m-%d %H:%M")}
                    for w in workouts]
    return {"daily": daily, "workouts": workout_rows, "days": day_docs, "posture": posture,
            "config": {**CONFIG, "dietStart": diet_start.isoformat()}}


def _write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    def cell(col, v):                                  # same precision the real store writes
        if v is None or isinstance(v, str):
            return v or ""
        nd = ROUNDING.get(col, 2)
        return str(round(v)) if nd == 0 else f"{v:.{nd}f}"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: cell(c, r.get(c)) for c in columns})


def demo_data(seed: int = SEED) -> dict:
    """Synthetic tables -> the documents the dashboard reads, through the real pipeline code."""
    return json.loads(_demo_json(seed))


@functools.lru_cache(maxsize=2)
def _demo_json(seed: int) -> str:
    s = synth(seed)
    with tempfile.TemporaryDirectory() as tmp:
        _write_csv(os.path.join(tmp, "daily.csv"), DAILY_COLUMNS, s["daily"])
        _write_csv(os.path.join(tmp, "workouts.csv"), WORKOUT_COLUMNS, s["workouts"])
        insights.write(insights.build(tmp), os.path.join(tmp, "insights.json"))
        fit = tracker_docs.build(tmp, days=120)
        # last week's report, as the weekly task would write it on Monday morning
        week = report.week_of(END - timedelta(days=7))
        rep = report.build(tmp, week, {d["date"]: d for d in s["days"]})
        weekly = {**rep, "lang": "tr", "sections": report.sections(rep, "tr")}
    fit["updated"] = END.isoformat() + "T12:00:00+00:00"       # the shim replaces this with "a few minutes ago"
    return json.dumps({"end": END.isoformat(), "docs": {"fitbit/daily": fit, "meta/config": s["config"], "meta/report": weekly},
                       "collections": {"days": s["days"], "posture": s["posture"]}}, ensure_ascii=False)


MARK = "<script>\n(function(){"
TAG = '<script src="demo.js"></script>\n'


def render() -> dict[str, str]:
    with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as f:
        page = f.read()
    if page.count(MARK) != 1:
        raise SystemExit("dashboard/index.html: main script not found exactly once")
    with open(os.path.join(ROOT, "dashboard", "demo-shim.js"), encoding="utf-8") as f:
        shim = f.read()
    data = json.dumps(demo_data(), ensure_ascii=False, separators=(",", ":"))
    return {"index.html": page.replace(MARK, TAG + MARK),
            "demo.js": "// Generated by tools/build_demo.py. Synthetic data; do not edit.\n"
                       f"window.DEMO_DATA={data};\n" + shim,
            ".nojekyll": ""}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "docs"))
    ap.add_argument("--check", action="store_true", help="verify the files in --out instead of writing them")
    a = ap.parse_args()
    stale = []
    os.makedirs(a.out, exist_ok=True)
    for name, text in render().items():
        path = os.path.join(a.out, name)
        if a.check:
            if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
                stale.append(name)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"{name}: {len(text.encode()):,} bytes")
    if stale:
        print("stale: " + ", ".join(stale) + " (run python3 tools/build_demo.py)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
