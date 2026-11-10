"""The public demo: synthetic data through the real pipeline, and the committed build is current."""

import importlib.util
import json
import os
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("build_demo", os.path.join(ROOT, "tools", "build_demo.py"))
build_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_demo)


def test_committed_demo_matches_the_builder():
    """docs/ is generated; editing dashboard/ or the generator without rebuilding fails here."""
    for name, text in build_demo.render().items():
        with open(os.path.join(ROOT, "docs", name), encoding="utf-8") as f:
            assert f.read() == text, f"docs/{name} is stale: run python3 tools/build_demo.py"


def test_demo_page_is_the_dashboard_plus_one_script_tag():
    with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as f:
        page = f.read()
    assert build_demo.render()["index.html"].replace(build_demo.TAG, "") == page


def test_pipeline_recovers_what_the_generator_planted():
    data = build_demo.demo_data()
    fit = data["docs"]["fitbit/daily"]
    ins = fit["insights"]
    by = {(f["x"], f["y"]): f for f in ins["drivers"]}
    assert by[("bedtime", "hrv")]["verdict"] == "clear" and by[("bedtime", "hrv")]["effect"] < 0
    for habit in ("steps", "training", "sleep", "shift"):            # generated independently of recovery
        assert by[(habit, "hrv")]["verdict"] == "unclear"
    first = build_demo.END - timedelta(days=build_demo.DAYS - 1)
    ill = {(first + timedelta(days=i)).isoformat() for i in build_demo.ILL}
    flagged = {x["date"] for x in ins["alerts"]["flagged"]}
    assert ill <= flagged and len(flagged - ill) <= 3                # the illness is caught; false alarms stay rare
    assert len(fit["days"]) == build_demo.DAYS and max(fit["days"]) == data["end"]


def test_demo_data_is_deterministic_and_self_consistent():
    assert json.dumps(build_demo.synth(), default=str) == json.dumps(build_demo.synth(), default=str)
    a = build_demo.demo_data()
    cfg = a["docs"]["meta/config"]
    assert cfg["goal"] < cfg["milestone"] < cfg["startW"]
    weights = [d["weight"] for d in a["collections"]["days"] if d["weight"] is not None]
    assert weights[0] > weights[-1] > cfg["goal"]                    # losing weight, not there yet
    assert all(d["date"] >= cfg["dietStart"] for d in a["collections"]["days"] if d["weight"] is not None)
