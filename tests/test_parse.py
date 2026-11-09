import os

from health_pipeline import parse_hae

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    return parse_hae.load(os.path.join(FX, name))


def test_metrics_v2_units_and_columns():
    days = parse_hae.parse_metrics(load("hae_v2_sample.json"))
    d = days["2026-01-10"]
    assert d["hrv_ms"] == 41.25
    assert d["resting_hr_bpm"] == 58
    assert (d["hr_min_bpm"], d["hr_avg_bpm"], d["hr_max_bpm"]) == (49, 71.4, 151)
    assert d["steps"] == 9876
    assert round(d["active_kcal"]) == 500          # kJ -> kcal
    assert round(d["distance_km"], 2) == 4.83      # mi -> km
    assert d["spo2_pct"] == 96.5                   # fraction -> percent
    assert d["sleep_min"] == 435 and d["sleep_deep_min"] == 75 and d["sleep_awake_min"] == 30
    assert d["sleep_start"].startswith("2026-01-09 23:40")
    assert "some_metric_we_ignore" not in str(d)
    assert days["2026-01-11"]["weight_kg"] == 80.4


def test_sleep_v1_falls_back_to_asleep_and_in_bed():
    days = parse_hae.parse_metrics(load("hae_v1_sleep.json"))
    d = days["2026-01-11"]
    assert d["sleep_min"] == 390 and d["in_bed_min"] == 420
    assert d["sleep_end"].startswith("2026-01-11 07:10")


def test_workouts_both_schemas():
    w2 = parse_hae.parse_workouts(load("hae_v2_sample.json"))[0]
    assert w2["name"] == "Outdoor Walk" and w2["duration_min"] == 60 and w2["hr_avg_bpm"] == 112.3
    w1 = parse_hae.parse_workouts(load("hae_v1_sleep.json"))[0]
    assert w1["active_kcal"] == 250 and w1["hr_max_bpm"] == 140 and w1["duration_min"] == 40


def test_garbage_is_ignored():
    assert parse_hae.parse_metrics({"data": {"metrics": [{"name": "step_count", "data": [{"date": "bad", "qty": "x"}]}]}}) == {}
    assert parse_hae.parse_workouts({}) == []
