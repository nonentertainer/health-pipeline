"""Insights: which habits move overnight recovery, and how often the illness rule fires.

Reads the tables the sync maintains (``daily.csv``, ``workouts.csv``) and answers
two questions with nothing but the standard library:

1. **Drivers.** Does bedtime, sleep length, yesterday's steps, yesterday's training
   or an off-schedule bedtime go together with a better or worse night (HRV, sleeping
   heart rate)?

   * Each outcome is first *detrended*: the night's value minus the median of the
     previous 14 nights. Fitness and weight change move HRV over weeks; without this
     step any habit that also drifted over the same weeks would look like a cause.
   * Association is Spearman's rank correlation, so one freak night cannot carry it.
   * The p-value is a permutation test (shuffle the outcome, recompute, count how
     often chance does as well), which assumes no distribution.
   * All tests form one family and are corrected together (Benjamini-Hochberg), so
     asking ten questions does not manufacture a finding.
   * The effect is reported in the outcome's own unit: mean of the nights above the
     habit's median minus the nights at or below it.

   * Each row also carries ``needN``: roughly how many nights it would take to
     confirm a correlation of the observed size, so "unclear" comes with an estimate
     of how far away an answer is.

   This is observational data from one person. A "clear" verdict means the pattern
   is unlikely to be chance, not that the habit causes it. Neighbouring nights are
   also not fully independent, which makes the p-values somewhat optimistic.

2. **Alerts.** A walk-forward backtest of the illness rule (two of: skin temperature
   >= +0.5 C, sleeping heart rate above the personal range, HRV below it; or
   temperature >= +1.0 C alone). Each night is judged against the 30 days *before*
   it, never against data it could not have known.

The result is a small JSON document; the same run is deterministic (fixed seed), so
an unchanged table produces an identical file.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics
from datetime import date, datetime, timedelta

MIN_N = 20              # nights needed before a driver is tested at all
MIN_GROUP = 5           # nights needed on each side of a split
TREND_WINDOW, TREND_MIN = 14, 5
BASE_WINDOW, BASE_MIN = 30, 7
PERMUTATIONS = 10_000
SEED = 7
CLEAR_Q, LIKELY_Q = 0.05, 0.20

OUTCOMES = {"hrv": "hrv_ms", "sleep_hr": "sleep_hr_bpm"}
# a workout counts as training when it is long enough and not a walk
TRAINING_MIN = 15
WALK_WORDS = ("walk", "hike")


# ---------- small statistics ----------

def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def ranks(xs: list[float]) -> list[float]:
    """Average ranks (1-based); ties share the mean of their positions."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = r
        i = j + 1
    return out


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = sum((x - ma) ** 2 for x in a)
    sb = sum((y - mb) ** 2 for y in b)
    if sa == 0 or sb == 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(sa * sb)


def spearman(x: list[float], y: list[float]) -> float:
    return pearson(ranks(x), ranks(y))


def permutation_p(x: list[float], y: list[float], n_perm: int = PERMUTATIONS, seed: int = SEED) -> float:
    """Two-sided p-value of Spearman's rho under shuffling. Never returns 0."""
    rx, ry = ranks(x), ranks(y)
    observed = abs(pearson(rx, ry))
    rng = random.Random(seed)
    shuffled = ry[:]
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(shuffled)
        if abs(pearson(rx, shuffled)) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def benjamini_hochberg(pvals: list[float]) -> list[float]:
    """q-values: the smallest false-discovery rate at which each test would pass."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvals[i] * m / rank)
        q[i] = running
    return q


def nights_needed(rho: float, z_alpha: float = 1.96, z_power: float = 0.8416) -> int | None:
    """Rough number of nights for an 80 % chance of detecting a correlation this size
    (two-sided 5 %, Fisher z with the 1.06 variance factor for Spearman). A planning
    figure for one test on its own, not a promise: the family correction asks for more."""
    r = min(abs(rho), 0.99)
    if r < 0.1:
        return None  # too weak to plan for; would take years of nights
    return math.ceil(3 + 1.06 * ((z_alpha + z_power) / math.atanh(r)) ** 2)


# ---------- series built from the tables ----------

def _read(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _series(daily: list[dict], col: str) -> dict[date, float]:
    out = {}
    for r in daily:
        v = _num(r.get(col))
        if v is not None:
            out[date.fromisoformat(r["date"])] = v
    return out


def onset_minutes(sleep_start: str, night: date) -> float | None:
    """Minutes after 18:00 of the evening before ``night`` (the date the sleep ended on)."""
    try:
        t = datetime.strptime(sleep_start.strip()[:16].replace("T", " "), "%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return None
    base = datetime.combine(night - timedelta(days=1), datetime.min.time()) + timedelta(hours=18)
    m = (t - base).total_seconds() / 60
    return m if 0 <= m <= 24 * 60 else None


def clock(onset: float) -> str:
    m = int(round(18 * 60 + onset)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def detrend(series: dict[date, float], window: int = TREND_WINDOW, min_n: int = TREND_MIN) -> dict[date, float]:
    """Value minus the median of the previous ``window`` days (never the day itself)."""
    out = {}
    for d, v in series.items():
        prev = [series[p] for k in range(1, window + 1) if (p := d - timedelta(days=k)) in series]
        if len(prev) >= min_n:
            out[d] = v - statistics.median(prev)
    return out


def predictors(daily: list[dict], workouts: list[dict]) -> dict[str, dict[date, float]]:
    """Habit values keyed by the night they could have influenced."""
    onset = {}
    for r in daily:
        d = date.fromisoformat(r["date"])
        m = onset_minutes(r.get("sleep_start", ""), d)
        if m is not None:
            onset[d] = m
    steps = _series(daily, "steps")
    trained = set()
    for w in workouts:
        dur = _num(w.get("duration_min"))
        name = (w.get("name") or "").lower()
        if dur is not None and dur >= TRAINING_MIN and not any(x in name for x in WALK_WORDS):
            try:
                trained.add(date.fromisoformat(w["date"]))
            except (KeyError, ValueError):
                pass
    one = timedelta(days=1)
    # training is only known for days the workout table covers
    covered = sorted(date.fromisoformat(w["date"]) for w in workouts if w.get("date"))
    shift = {}
    for d, m in onset.items():
        prev = [onset[p] for k in range(1, 8) if (p := d - timedelta(days=k)) in onset]
        if len(prev) >= 3:
            shift[d] = abs(m - sum(prev) / len(prev))
    return {
        "bedtime": onset,
        "sleep": _series(daily, "sleep_min"),
        "steps": {d + one: v for d, v in steps.items()},
        "training": ({d + one: 1.0 if d in trained else 0.0 for d in steps if covered[0] <= d <= covered[-1]}
                     if covered else {}),
        "shift": shift,
    }


# ---------- the two analyses ----------

def _verdict(q: float) -> str:
    return "clear" if q < CLEAR_Q else "likely" if q < LIKELY_Q else "unclear"


def drivers(daily: list[dict], workouts: list[dict], n_perm: int = PERMUTATIONS, seed: int = SEED) -> list[dict]:
    preds = predictors(daily, workouts)
    found = []
    for y_name, col in OUTCOMES.items():
        dev = detrend(_series(daily, col))
        for x_name, xs in preds.items():
            days = sorted(set(dev) & set(xs))
            if len(days) < MIN_N:
                continue
            x = [xs[d] for d in days]
            y = [dev[d] for d in days]
            binary = x_name == "training"
            cut = 0.5 if binary else statistics.median(x)
            hi = [b for a, b in zip(x, y) if a > cut]
            lo = [b for a, b in zip(x, y) if a <= cut]
            if len(hi) < MIN_GROUP or len(lo) < MIN_GROUP:
                continue
            found.append({
                "x": x_name, "y": y_name, "n": len(days),
                "rho": round(spearman(x, y), 2),
                "p": permutation_p(x, y, n_perm, seed),
                "effect": round(sum(hi) / len(hi) - sum(lo) / len(lo), 1),
                "nHi": len(hi), "nLo": len(lo),
                "cut": None if binary else (clock(cut) if x_name == "bedtime" else round(cut)),
            })
            found[-1]["needN"] = nights_needed(found[-1]["rho"])
    for item, q in zip(found, benjamini_hochberg([f["p"] for f in found])):
        item["p"] = round(item["p"], 4)
        item["q"] = round(q, 4)
        item["verdict"] = _verdict(q)
    found.sort(key=lambda f: (f["y"] != "hrv", f["q"], -abs(f["rho"])))
    return found


def _range(series: dict[date, float], d: date) -> tuple[float, float] | None:
    prev = [series[p] for k in range(1, BASE_WINDOW + 1) if (p := d - timedelta(days=k)) in series]
    if len(prev) < BASE_MIN:
        return None
    m, sd = statistics.fmean(prev), statistics.pstdev(prev)
    return m - sd, m + sd


def alerts(daily: list[dict]) -> dict:
    """Walk-forward backtest of the illness rule over every night that can be judged."""
    temp = _series(daily, "skin_temp_delta_c")
    hr = _series(daily, "sleep_hr_bpm")
    hrv = _series(daily, "hrv_ms")
    judged, flagged = 0, []
    for d in sorted(set(temp) | set(hr) | set(hrv)):
        hr_rng, hrv_rng = _range(hr, d), _range(hrv, d)
        if hr_rng is None and hrv_rng is None:
            continue  # no personal range yet: the rule cannot be evaluated fairly
        judged += 1
        sig = []
        t = temp.get(d)
        if t is not None and t >= 0.5:
            sig.append("temp")
        if d in hr and hr_rng and hr[d] > hr_rng[1]:
            sig.append("hr")
        if d in hrv and hrv_rng and hrv[d] < hrv_rng[0]:
            sig.append("hrv")
        if len(sig) >= 2 or (t is not None and t >= 1.0):
            flagged.append({"date": d.isoformat(), "signals": sig})
    return {"nights": judged, "flagged": flagged}


def regularity(daily: list[dict], nights: int = 14) -> dict | None:
    rows = [(date.fromisoformat(r["date"]), onset_minutes(r.get("sleep_start", ""), date.fromisoformat(r["date"])))
            for r in daily]
    vals = [m for _, m in sorted(rows)[-nights * 2:] if m is not None][-nights:]
    if len(vals) < 5:
        return None
    return {"n": len(vals), "onset": clock(statistics.fmean(vals)), "sdMin": round(statistics.pstdev(vals))}


def build(data_dir: str, n_perm: int = PERMUTATIONS, seed: int = SEED) -> dict:
    daily = _read(os.path.join(data_dir, "daily.csv"))
    workouts = _read(os.path.join(data_dir, "workouts.csv"))
    hrv_nights = len(_series(daily, "hrv_ms"))
    return {
        "asOf": max((r["date"] for r in daily), default=None),
        "nights": hrv_nights,
        "method": {"detrend": f"minus median of previous {TREND_WINDOW} nights", "test": "Spearman, permutation",
                   "permutations": n_perm, "correction": "Benjamini-Hochberg",
                   "clearBelowQ": CLEAR_Q, "likelyBelowQ": LIKELY_Q, "minNights": MIN_N},
        "drivers": drivers(daily, workouts, n_perm, seed),
        "alerts": alerts(daily),
        "regularity": regularity(daily),
    }


# ---------- output ----------

_X = {"bedtime": "later bedtime", "sleep": "longer sleep", "steps": "more steps the day before",
      "training": "training the day before", "shift": "bedtime further from the usual"}
_Y = {"hrv": ("HRV", "ms"), "sleep_hr": ("sleeping HR", "bpm")}


def _p(v: float) -> str:
    return "<0.001" if v < 0.001 else f"{v:.3f}"


def to_markdown(ins: dict) -> str:
    lines = [f"# Insights as of {ins.get('asOf')}", "",
             f"{ins.get('nights', 0)} nights with HRV. Outcomes are detrended against the previous "
             f"{TREND_WINDOW} nights; p from {ins['method']['permutations']} permutations; q is "
             "Benjamini-Hochberg across all rows. Observational: association, not cause.", ""]
    if ins["drivers"]:
        lines += ["| habit | outcome | effect | nights (above / at-or-below) | rho | p | q | verdict |",
                  "|---|---|---|---|---|---|---|---|"]
        for f in ins["drivers"]:
            name, unit = _Y[f["y"]]
            cut = f" (split at {f['cut']})" if f["cut"] is not None else ""
            lines.append(f"| {_X[f['x']]}{cut} | {name} | {f['effect']:+.1f} {unit} | {f['nHi']} / {f['nLo']} | "
                         f"{f['rho']:+.2f} | {_p(f['p'])} | {_p(f['q'])} | {f['verdict']} |")
    else:
        lines.append(f"Not enough nights yet (needs {MIN_N} per habit).")
    a = ins["alerts"]
    lines += ["", f"## Illness rule backtest", "",
              f"{len(a['flagged'])} of {a['nights']} judged nights raised the alert."]
    lines += [f"- {x['date']}: {', '.join(x['signals'])}" for x in a["flagged"]]
    r = ins.get("regularity")
    if r:
        lines += ["", "## Sleep timing", "",
                  f"Last {r['n']} nights: asleep at {r['onset']} on average, night-to-night spread {r['sdMin']} min."]
    return "\n".join(lines) + "\n"


def write(ins: dict, out: str, md: str | None = None) -> None:
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(ins, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if md:
        os.makedirs(os.path.dirname(md) or ".", exist_ok=True)
        with open(md, "w", encoding="utf-8") as f:
            f.write(to_markdown(ins))
