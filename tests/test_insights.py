"""Insights: the statistics do what they claim, on synthetic data with a planted effect."""

import csv
import json
import random
from datetime import date, timedelta

import pytest

from health_pipeline import cli, insights as I


def test_ranks_average_ties():
    assert I.ranks([10, 20, 20, 30]) == [1, 2.5, 2.5, 4]


def test_spearman_is_rank_based():
    x = [1, 2, 3, 4, 5]
    assert I.spearman(x, [1, 4, 9, 16, 1000]) == pytest.approx(1)   # monotone, far from linear
    assert I.spearman(x, [5, 4, 3, 2, 1]) == pytest.approx(-1)
    assert I.spearman(x, [7, 7, 7, 7, 7]) == 0                       # no variation: no claim


def test_permutation_p_separates_signal_from_noise_and_is_repeatable():
    rng = random.Random(1)
    x = [rng.random() for _ in range(40)]
    noise = [rng.random() for _ in range(40)]
    strong = I.permutation_p(x, [v * 2 + rng.random() * 0.2 for v in x], 2000)
    assert 0 < strong < 0.005                       # never exactly zero: (hits + 1) / (n + 1)
    assert I.permutation_p(x, noise, 2000) > 0.05
    assert I.permutation_p(x, noise, 2000) == I.permutation_p(x, noise, 2000)


def test_benjamini_hochberg_known_values():
    q = I.benjamini_hochberg([0.01, 0.04, 0.03, 0.005])
    assert q == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert I.benjamini_hochberg([0.5]) == [0.5]


def test_onset_is_minutes_after_six_pm_of_the_evening_before():
    night = date(2026, 11, 10)
    assert I.onset_minutes("2026-11-10 02:51", night) == 531
    assert I.onset_minutes("2026-11-09 23:30", night) == 330      # asleep before midnight
    assert I.onset_minutes("", night) is None
    assert I.clock(531) == "02:51" and I.clock(330) == "23:30"


def test_detrend_uses_only_earlier_nights():
    d0 = date(2026, 1, 1)
    s = {d0 + timedelta(days=k): 30.0 for k in range(6)}
    s[d0 + timedelta(days=6)] = 50.0
    dev = I.detrend(s)
    assert dev[d0 + timedelta(days=6)] == 20.0           # the jump is not part of its own baseline
    assert d0 + timedelta(days=4) not in dev             # fewer than 5 earlier nights: no estimate
    assert dev[d0 + timedelta(days=5)] == 0.0


def test_nights_needed_grows_as_the_effect_shrinks():
    assert I.nights_needed(0.5) < I.nights_needed(0.3) < I.nights_needed(0.15)
    assert I.nights_needed(-0.3) == I.nights_needed(0.3)
    assert I.nights_needed(0.05) is None


def _daily(n=80, seed=3, effect=-0.04):
    """n nights; HRV drifts upward over time and drops with a later bedtime. Steps are pure noise."""
    rng = random.Random(seed)
    rows, d0 = [], date(2026, 6, 1)
    for k in range(n):
        d = d0 + timedelta(days=k)
        onset = 420 + rng.uniform(-90, 90)                          # around 01:00
        start = (d - timedelta(days=1)).isoformat() + " 18:00"
        hh, mm = divmod(int(18 * 60 + onset), 60)
        start = f"{d.isoformat() if hh >= 24 else (d - timedelta(days=1)).isoformat()} {hh % 24:02d}:{mm:02d}"
        hrv = 30 + 0.08 * k + effect * (onset - 420) + rng.gauss(0, 1.5)
        rows.append({"date": d.isoformat(), "sleep_start": start, "sleep_min": round(400 + rng.gauss(0, 30)),
                     "hrv_ms": round(hrv, 1), "sleep_hr_bpm": round(56 + rng.gauss(0, 1.5)),
                     "steps": round(rng.uniform(3000, 14000)), "skin_temp_delta_c": round(rng.gauss(0, 0.15), 2)})
    return rows


def test_drivers_find_the_planted_effect_and_nothing_else():
    found = I.drivers(_daily(), [], n_perm=2000)
    by = {(f["x"], f["y"]): f for f in found}
    bed = by[("bedtime", "hrv")]
    assert bed["verdict"] == "clear" and bed["effect"] < 0 and bed["rho"] < -0.5
    assert bed["nHi"] + bed["nLo"] == bed["n"] and bed["needN"] < bed["n"]
    assert by[("steps", "hrv")]["verdict"] == "unclear"
    assert by[("steps", "sleep_hr")]["verdict"] == "unclear"
    assert not any(f["x"] == "training" for f in found)             # no workout table: not tested
    assert found[0] is bed                                           # clearest HRV finding first
    assert all(0 < f["p"] <= 1 and f["p"] <= f["q"] <= 1 for f in found)


def test_drivers_need_enough_nights():
    assert I.drivers(_daily(n=18), []) == []


def test_training_uses_the_day_before_and_ignores_walks():
    daily = _daily(n=10)
    wk = [{"date": daily[2]["date"], "name": "Run", "duration_min": "30"},
          {"date": daily[4]["date"], "name": "Walk", "duration_min": "60"},
          {"date": daily[6]["date"], "name": "Weights", "duration_min": "5"}]
    tr = I.predictors(daily, wk)["training"]
    d = lambda k: date.fromisoformat(daily[k]["date"])
    assert tr[d(3)] == 1.0 and tr[d(5)] == 0.0 and tr[d(7)] == 0.0
    assert d(1) not in tr                                            # before the workout table starts


def test_alerts_are_judged_against_earlier_nights_only():
    d0 = date(2026, 3, 1)
    rows = [{"date": (d0 + timedelta(days=k)).isoformat(), "hrv_ms": 30 + (k % 2), "sleep_hr_bpm": 56 + (k % 2),
             "skin_temp_delta_c": 0.0} for k in range(12)]
    rows[9].update(skin_temp_delta_c=0.6, sleep_hr_bpm=62)           # two signals
    rows[10].update(skin_temp_delta_c=1.1)                           # temperature alone
    rows[11].update(hrv_ms=20)                                       # one signal: no alert
    a = I.alerts(rows)
    assert a["nights"] == 5                                          # the first 7 nights have no range to judge by
    assert a["flagged"] == [{"date": "2026-03-10", "signals": ["temp", "hr"]},
                            {"date": "2026-03-11", "signals": ["temp"]}]


def _write(tmp_path, rows):
    cols = ["date", "sleep_start", "sleep_min", "hrv_ms", "sleep_hr_bpm", "steps", "skin_temp_delta_c"]
    with open(tmp_path / "daily.csv", "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(rows)


def test_cli_writes_identical_files_for_identical_data(tmp_path, capsys):
    _write(tmp_path, _daily(n=40))
    out, md = tmp_path / "out" / "insights.json", tmp_path / "out" / "insights.md"
    assert cli.main(["insights", "--data-dir", str(tmp_path), "--out", str(out), "--md", str(md)]) == 0
    first = out.read_bytes()
    assert cli.main(["insights", "--data-dir", str(tmp_path), "--out", str(out)]) == 0
    assert out.read_bytes() == first
    ins = json.loads(first)
    assert ins["asOf"] == "2026-07-10" and ins["nights"] == 40 and ins["regularity"]["n"] == 14
    assert "later bedtime" in md.read_text() and "Illness rule backtest" in md.read_text()
    printed = capsys.readouterr().out
    assert "tests on 40 nights" in printed and "ms" not in printed   # counts only, no metric values in logs


def test_empty_tables_do_not_crash(tmp_path):
    ins = I.build(str(tmp_path))
    assert ins["drivers"] == [] and ins["alerts"] == {"nights": 0, "flagged": []} and ins["regularity"] is None
    assert "Not enough nights" in I.to_markdown(ins)
