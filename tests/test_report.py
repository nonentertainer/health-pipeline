"""Weekly report: week arithmetic, counting rules and the rendered text, on small synthetic tables."""

import csv
import json
from datetime import date

from health_pipeline import cli, report as R


def test_iso_weeks():
    assert R.week_of(date(2026, 11, 10)) == "2026-W46"
    assert R.week_bounds("2026-W45") == (date(2026, 11, 2), date(2026, 11, 8))
    assert R.week_bounds("2026-W01") == (date(2025, 12, 29), date(2026, 1, 4))   # ISO week 1 starts in December
    assert R.last_full_week(date(2026, 11, 10)) == "2026-W45"                    # Tuesday: last week is done
    assert R.last_full_week(date(2026, 11, 8)) == "2026-W44"                     # Sunday: this week is not


def _tables(tmp_path):
    daily = [{"date": f"2026-11-{d:02d}", "sleep_min": m, "sleep_start": s, "hrv_ms": h, "sleep_hr_bpm": 56,
              "steps": 8000, "active_kcal": 600}
             for d, m, s, h in [(2, 420, "2026-11-02 01:00", 30), (3, 330, "2026-11-03 03:00", 28),
                                (4, 400, "2026-11-04 00:30", 31), (5, 340, "2026-11-05 03:30", 29),
                                (6, 450, "2026-11-06 01:10", 32)]]
    daily += [{"date": f"2026-10-{d:02d}", "hrv_ms": 30, "sleep_hr_bpm": 57} for d in range(10, 30)]
    with open(tmp_path / "daily.csv", "w", newline="") as f:
        w = csv.DictWriter(f, ["date", "sleep_min", "sleep_start", "hrv_ms", "sleep_hr_bpm", "steps", "active_kcal"])
        w.writeheader(); w.writerows(daily)
    workouts = [("2026-11-03", "Weights", 60),        # same day as a logged session: counted once
                ("2026-11-04", "Elliptical", 20),     # cardio from the watch alone
                ("2026-11-05", "Run", 10),            # too short to count
                ("2026-11-06", "Walk", 45), ("2026-11-07", "Walk", 30),
                ("2026-11-09", "Run", 40)]            # next week
    with open(tmp_path / "workouts.csv", "w", newline="") as f:
        w = csv.DictWriter(f, ["start", "date", "name", "duration_min"])
        w.writeheader()
        w.writerows({"start": d + " 18:00", "date": d, "name": n, "duration_min": m} for d, n, m in workouts)
    (tmp_path / "insights.json").write_text(json.dumps({"alerts": {"flagged": [{"date": "2026-11-05"}, {"date": "2026-10-01"}]},
        "drivers": [{"x": "bedtime", "y": "hrv", "effect": -3.0, "verdict": "unclear", "n": 42, "needN": 97}]}))
    log = {"2026-10-27": {"date": "2026-10-27", "weight": 89.0, "sessions": [{"kind": "gym", "ex": [{"name": "Bench", "kg": 17.5}]}]},
           "2026-10-29": {"date": "2026-10-29", "weight": 88.6},
           "2026-11-03": {"date": "2026-11-03", "weight": 88.1, "sessions": [{"kind": "gym", "ex": [
               {"name": "Bench", "kg": 20}, {"name": "Lateral raise", "kg": 7.5}, {"name": "Biceps", "kg": None}]}]},
           "2026-11-06": {"date": "2026-11-06", "weight": 87.9, "sessions": [{"kind": "walk", "min": 25}]},
           "2026-11-08": {"date": "2026-11-08", "sessions": [{"kind": "run", "min": 30}]}}
    return log


def test_week_numbers(tmp_path):
    log = _tables(tmp_path)
    rep = R.build(str(tmp_path), "2026-W45", log)
    assert rep["from"] == "2026-11-02" and rep["to"] == "2026-11-08"
    assert rep["training"] == {"gym": 1, "cardio": 2, "days": 3, "walkMin": 100}   # 45 + 30 from the watch + 25 logged
    assert rep["weight"] == {"avg": 88.0, "n": 2, "prevAvg": 88.8, "change": -0.8}
    assert rep["strength"] == [{"name": "Bench", "from": 17.5, "to": 20.0}, {"name": "Lateral raise", "from": None, "to": 7.5}]
    assert rep["sleep"]["nights"] == 5 and rep["sleep"]["short"] == 2 and rep["sleep"]["avgMin"] == 388
    assert rep["sleep"]["onset"] == "01:50"
    assert rep["recovery"]["alerts"] == ["2026-11-05"] and rep["recovery"]["hrvPrev"] == 30.0
    assert [f["k"] for f in rep["focus"]] == ["alert", "train", "bedtime"]          # priority order, at most three


def test_focus_rules():
    base = {"recovery": {"alerts": []}, "sleep": {"spreadMin": 20, "short": 0}, "training": {"days": 5},
            "weight": {"change": -0.5}, "target": 5}
    assert R.focus(base) == [{"k": "steady"}]
    assert R.focus({**base, "weight": {"change": 0.4}}) == [{"k": "weight_up", "change": 0.4}]
    assert R.focus({**base, "sleep": None, "weight": None}) == [{"k": "steady"}]


def test_without_a_log_weight_and_strength_are_left_out(tmp_path):
    _tables(tmp_path)
    rep = R.build(str(tmp_path), "2026-W45")
    assert rep["weight"] is None and rep["strength"] == [] and rep["training"]["gym"] == 1   # the watch still saw it
    md = R.to_markdown(rep, "en")
    assert "## Weight" not in md and "## Training" in md


def test_read_log_accepts_a_folder_of_export_files(tmp_path):
    folder = tmp_path / "export" / "days"
    folder.mkdir(parents=True)
    (folder / "2026-11-03.json").write_text(json.dumps({"id": "2026-11-03", "version": 6, "data": {"date": "2026-11-03", "weight": 88.1}}))
    (folder / "junk.json").write_text(json.dumps({"no": "date"}))
    (tmp_path / "list.json").write_text(json.dumps([{"date": "2026-11-04", "weight": 87.7}]))
    assert R.read_log(str(tmp_path / "export")) == {"2026-11-03": {"date": "2026-11-03", "weight": 88.1}}
    assert R.read_log(str(tmp_path / "list.json"))["2026-11-04"]["weight"] == 87.7
    assert R.read_log(None) == {} and R.read_log(str(tmp_path / "missing")) == {}


def test_cli_writes_markdown_and_json_in_both_languages(tmp_path, capsys):
    log = _tables(tmp_path)
    (tmp_path / "log.json").write_text(json.dumps(list(log.values())))
    out, js = tmp_path / "r" / "w.md", tmp_path / "r" / "w.json"
    args = ["report", "--data-dir", str(tmp_path), "--log", str(tmp_path / "log.json"), "--week", "2026-W45"]
    assert cli.main(args + ["--out", str(out), "--json", str(js)]) == 0
    md = out.read_text()
    assert md.startswith("# Hafta 2026-W45") and "3/5 antrenman günü" in md and "Bench: 17.5 → 20 kg" in md
    assert "Hastalık uyarısı çaldı: 2026-11-05" in md
    data = json.loads(js.read_text())
    assert data["lang"] == "tr" and "Gelecek hafta" in data["sections"] and data["training"]["days"] == 3
    assert "88" not in capsys.readouterr().out                                   # counts only in logs
    assert cli.main(args + ["--out", str(out), "--lang", "en"]) == 0
    assert "Next week" in out.read_text()
