import json
import os
import shutil

from health_pipeline import sync
from health_pipeline.sources.local import LocalSource

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def _inbox(tmp_path, *names):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    for n in names:
        shutil.copy(os.path.join(FX, n), inbox / n)
    return inbox


def test_first_run_then_idempotent(tmp_path):
    inbox = _inbox(tmp_path, "hae_v2_sample.json")
    data, state = tmp_path / "data", tmp_path / "state.json"
    r1 = sync.run([LocalSource(str(inbox))], str(data), str(state))
    assert r1.files_processed == 1 and sorted(r1.daily.added) == ["2026-01-10", "2026-01-11"]
    first = (data / "daily.csv").read_text()
    r2 = sync.run([LocalSource(str(inbox))], str(data), str(state))
    assert r2.files_processed == 0 and not r2.written           # state skips the known file
    r3 = sync.run([LocalSource(str(inbox))], str(data), str(state), full=True)
    assert r3.files_processed == 1 and not r3.written           # re-parse gives identical output
    assert (data / "daily.csv").read_text() == first
    st = json.loads(state.read_text())
    assert st["latest_date"] == "2026-01-11" and st["rows"] == 2


def test_later_export_updates_only_changed_columns(tmp_path):
    inbox = _inbox(tmp_path, "hae_v2_sample.json")
    data, state = tmp_path / "data", tmp_path / "state.json"
    sync.run([LocalSource(str(inbox))], str(data), str(state))
    # a later export revises steps for the 10th and adds sleep for the 11th
    shutil.copy(os.path.join(FX, "hae_v1_sleep.json"), inbox / "later.json")
    os.utime(inbox / "later.json", (2e9, 2e9))
    r = sync.run([LocalSource(str(inbox))], str(data), str(state))
    assert r.files_processed == 1
    assert r.daily.updated == {"2026-01-10": ["steps"], "2026-01-11": ["sleep_min", "in_bed_min", "sleep_start", "sleep_end"]}
    rows = (data / "daily.csv").read_text().splitlines()
    header = rows[0].split(",")
    d10 = dict(zip(header, rows[1].split(",")))
    assert d10["steps"] == "10500" and d10["hrv_ms"] == "41.2"  # untouched column kept (41.25 -> 41.2 rounded)
    assert len((data / "workouts.csv").read_text().splitlines()) == 3


def test_bad_file_does_not_block_others(tmp_path):
    inbox = _inbox(tmp_path, "hae_v2_sample.json")
    (inbox / "broken.json").write_text("{not json")
    r = sync.run([LocalSource(str(inbox))], str(tmp_path / "data"), str(tmp_path / "s.json"))
    assert r.files_processed == 1 and len(r.files_failed) == 1
