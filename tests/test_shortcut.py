import json
import os

import pytest

from health_pipeline import parse_shortcut as ps
from health_pipeline import sync
from health_pipeline.sources.local import LocalSource

FX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.mark.parametrize("text,expected", [
    ("30,1", 30.1), ("10.234", 10234), ("1.234,5", 1234.5), ("87.75", 87.75), ("87,75", 87.75),
    ("12 345", 12345), ("57 bpm", 57), ("0.965", 0.965), ("1,234,567", 1234567), ("2.5", 2.5),
    ("", None), ("abc", None), (42, 42.0),
])
def test_parse_number_locales(text, expected):
    assert ps.parse_number(text) == expected


def test_night_assignment():
    from datetime import datetime
    assert ps.night_of(datetime(2026, 1, 11, 7, 25)) == "2026-01-11"
    assert ps.night_of(datetime(2026, 1, 10, 23, 50)) == "2026-01-11"
    assert ps.night_of(datetime(2026, 1, 11, 15, 0)) == "2026-01-11"   # afternoon nap -> same day


def test_payload_turkish():
    with open(os.path.join(FX, "shortcut_tr.json"), encoding="utf-8") as f:
        doc = json.load(f)
    assert ps.is_shortcut_payload(doc)
    days = ps.parse(doc)
    assert days["2026-01-09"]["steps"] == 10234 and days["2026-01-09"]["active_kcal"] == 512.4
    assert days["2026-01-11"]["hrv_ms"] == 29.8 and days["2026-01-11"]["weight_kg"] == 87.75
    n = days["2026-01-11"]
    # asleep = 23:40-02:30 (overlapping deep segments merged) + 03:00-07:15 = 170 + 255
    assert n["sleep_min"] == 425
    assert n["sleep_deep_min"] == 80 and n["sleep_rem_min"] == 60 and n["sleep_awake_min"] == 30
    assert n["in_bed_min"] == 480 and n["sleep_start"] == "2026-01-10 23:40" and n["sleep_end"] == "2026-01-11 07:15"
    assert days["2026-01-10"]["sleep_min"] == 420  # "Uykuda" (unspecified) counts as asleep


def test_flat_payload_and_detection():
    days = ps.parse({"date": "11.01.2026", "hrv_ms": "31,5", "steps": 9000})
    assert days == {"2026-01-11": {"hrv_ms": 31.5, "steps": 9000.0}}
    assert not ps.is_shortcut_payload({"data": {"metrics": []}})


def test_sync_routes_shortcut_payload(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "2026-01-11.json").write_text(open(os.path.join(FX, "shortcut_tr.json"), encoding="utf-8").read(), encoding="utf-8")
    r = sync.run([LocalSource(str(inbox))], str(tmp_path / "data"), str(tmp_path / "s.json"))
    assert r.files_processed == 1 and sorted(r.daily.added) == ["2026-01-09", "2026-01-10", "2026-01-11"]
    csv = (tmp_path / "data" / "daily.csv").read_text()
    assert "2026-01-11,425,80,60," in csv


def test_nested_metrics_aliases_and_workouts(tmp_path):
    doc = {
        "source": "shortcut",
        "metrics": {
            "steps": "2026-01-10=9.500",
            "spo2": "2026-01-10=0,96",
            "walking_hr": "2026-01-10=101,5",
            "vo2max": "2026-01-10=42,3",
            "resp": "2026-01-10=15,6",
            "kilo": "2026-01-10=87,4",
            "empty_metric_is_fine": "",
        },
        "workouts": "2026-01-10T18:06|2026-01-10T18:27|Bisiklet|172|;2026-01-10T19:13|2026-01-10T20:10|Yürüyüş|280|4,82;junk",
    }
    assert ps.is_shortcut_payload(doc)
    d = ps.parse(doc)["2026-01-10"]
    assert d == {"steps": 9500, "spo2_pct": 96.0, "walking_hr_bpm": 101.5, "vo2max": 42.3,
                 "resp_rate_bpm": 15.6, "weight_kg": 87.4}
    wk = ps.parse_workouts(doc["workouts"])
    assert [w["name"] for w in wk] == ["Bisiklet", "Yürüyüş"]
    assert wk[0]["duration_min"] == 21 and wk[0]["distance_km"] is None and wk[1]["distance_km"] == 4.82

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "p.json").write_text(json.dumps(doc), encoding="utf-8")
    r = sync.run([LocalSource(str(inbox))], str(tmp_path / "data"), str(tmp_path / "s.json"))
    assert r.files_processed == 1 and len(r.workouts.added) == 2
    assert "Bisiklet" in (tmp_path / "data" / "workouts.csv").read_text(encoding="utf-8")


def test_hae_list_metrics_not_mistaken_for_shortcut():
    assert not ps.is_shortcut_payload({"metrics": [{"name": "step_count", "data": []}]})


@pytest.mark.parametrize("text,stage", [
    ("Çekirdek", "core"), ("Çekirdek Uyku", "core"), ("Asleep Core", "core"), ("Uykuda (Derin)", "deep"),
    ("REM", "rem"), ("Uyanık", "awake"), ("Yatakta", "in_bed"), ("In Bed", "in_bed"), ("Uykuda", "asleep"),
    ("3", "core"), ("0", "in_bed"), ("bilinmeyen", None),
])
def test_stage_names(text, stage):
    assert ps.stage_of(text) == stage


def test_realistic_shortcut_output():
    # what the generated Shortcut produces: values carry units, a space after '=', Turkish separators
    doc = {"source": "shortcut", "metrics": {
        "steps": "2026-11-07= 10.234 adım;2026-11-08= 8.120 adım;2026-11-09= 1.205 adım",
        "hrv": "2026-11-08= 30,1 ms;2026-11-09= 29,8 ms",
        "rhr": "2026-11-09= 57,3 atım/dk",
        "spo2": "2026-11-09= %96",
        "temp": "2026-11-09= 36,2 °C",
        "kilo": "",
        "vo2max": "",
    }, "sleep": "2026-11-08T23:40|2026-11-09T01:10|Çekirdek Uyku;2026-11-09T01:10|2026-11-09T02:00|Derin;2026-11-09T02:00|2026-11-09T02:20|Uyanık"}
    d = ps.parse(doc)
    assert d["2026-11-07"]["steps"] == 10234 and d["2026-11-09"]["steps"] == 1205
    n = d["2026-11-09"]
    assert n["hrv_ms"] == 29.8 and n["resting_hr_bpm"] == 57.3 and n["spo2_pct"] == 96 and n["wrist_temp_c"] == 36.2
    assert n["sleep_min"] == 140 and n["sleep_deep_min"] == 50 and n["sleep_awake_min"] == 20
    assert "weight_kg" not in n
