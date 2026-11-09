import io
import json
import os
import urllib.error
import urllib.parse
from datetime import date

import pytest

from health_pipeline import cli, sync
from health_pipeline.sources import google_health as g

FX = os.path.join(os.path.dirname(__file__), "fixtures", "google_health_sample.json")


def _raw():
    with open(FX, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- parsing ----
def test_parse_daily_metrics_and_temperature():
    days, _ = g.parse(_raw())
    d = days["2026-03-11"]
    assert d["hrv_ms"] == 41.2 and d["sleep_hr_bpm"] == 52 and d["resting_hr_bpm"] == 60
    assert d["spo2_pct"] == 96.4 and d["resp_rate_bpm"] == 14.25 and d["vo2max"] == pytest.approx(47.31234)
    assert d["wrist_temp_c"] == pytest.approx(34.1234)
    assert d["skin_temp_delta_c"] == pytest.approx(0.2234)
    assert days["2026-03-10"]["hrv_ms"] == 38


def test_parse_sleep_main_night_by_wake_date_and_nap_ignored():
    days, _ = g.parse(_raw())
    d = days["2026-03-11"]
    assert d["sleep_start"] == "2026-03-10 23:15" and d["sleep_end"] == "2026-03-11 07:30"
    assert (d["sleep_min"], d["in_bed_min"], d["sleep_awake_min"]) == (450, 495, 45)
    assert (d["sleep_deep_min"], d["sleep_rem_min"], d["sleep_core_min"]) == (90, 110, 250)
    classic = days["2026-03-10"]
    assert classic["sleep_min"] == 330 and classic["sleep_awake_min"] == 25
    assert classic["sleep_start"] == "2026-03-10 02:05"
    assert "sleep_deep_min" not in classic  # classic logs have no stages


def test_parse_rollups_units():
    days, _ = g.parse(_raw())
    d = days["2026-03-11"]
    assert d["steps"] == 11234 and days["2026-03-10"]["steps"] == 8021
    assert d["distance_km"] == pytest.approx(8.1253)
    assert d["flights"] == 7 and d["active_kcal"] == 702.4 and d["total_kcal"] == 2987.6
    assert d["active_zone_min"] == 20 + 2 * (8 + 2)
    assert d["exercise_min"] == 25 + 18
    assert (d["hr_min_bpm"], d["hr_avg_bpm"], d["hr_max_bpm"]) == (48, 71.6, 151)
    assert "2026-03-09" not in days  # empty rollup -> no row


def test_parse_weight_takes_morning_reading_and_body_fat():
    days, _ = g.parse(_raw())
    assert days["2026-03-11"]["weight_kg"] == pytest.approx(80.6)
    assert days["2026-03-11"]["body_fat_pct"] == 19.4


def test_parse_workout_local_time_across_midnight():
    _, wk = g.parse(_raw())
    assert wk == [{
        "start": "2026-03-11 00:30", "date": "2026-03-11", "name": "Walk", "end": "2026-03-11 01:35",
        "duration_min": 65.0, "distance_km": pytest.approx(5.25), "total_kcal": 410, "steps": 6500,
        "hr_avg_bpm": 104,
    }]


def test_parse_empty_and_missing_types():
    assert g.parse({}) == ({}, [])
    assert g.parse({"sleep": [], "steps.rollup": [{"civilStartTime": {"date": {}}}]}) == ({}, [])


def test_load_dir_reads_probe_layout(tmp_path):
    (tmp_path / "steps.rollup.json").write_text(json.dumps(_raw()["steps.rollup"]))
    (tmp_path / "weight.json").write_text("[]")
    (tmp_path / "daily-vo2-max.json").write_text(json.dumps({"http_status": 403, "error": "x"}))
    (tmp_path / "_summary.json").write_text("{}")
    assert sorted(g.load_dir(str(tmp_path))) == ["steps.rollup", "weight"]


# ---------------------------------------------------------------- client -----
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGoogle:
    """Answers token, list and dailyRollUp requests; records what was asked."""

    def __init__(self, token_status=200, fail=None, flaky=0):
        self.token_status, self.fail, self.flaky = token_status, fail or {}, flaky
        self.token_calls, self.requests = 0, []

    def _err(self, req, code, body):
        return urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))

    def __call__(self, req):
        url = req.full_url
        if url == g.TOKEN_URL:
            self.token_calls += 1
            if self.token_status != 200:
                raise self._err(req, self.token_status, {"error": "invalid_grant", "error_description": "expired"})
            return FakeResp(json.dumps({"access_token": "AT", "expires_in": 3600}).encode())
        assert req.headers["Authorization"] == "Bearer AT"
        path = urllib.parse.urlparse(url).path
        dtype = path.split("/dataTypes/")[1].split("/")[0]
        self.requests.append((req.get_method(), dtype, url, req.data))
        if self.flaky:
            self.flaky -= 1
            raise self._err(req, 503, {"error": {"status": "UNAVAILABLE", "message": "try later"}})
        if dtype in self.fail:
            raise self._err(req, self.fail[dtype], {"error": {"status": "PERMISSION_DENIED", "message": "MISSING_OAUTH_SCOPE"}})
        if path.endswith(":dailyRollUp"):
            body = json.loads(req.data)
            s = body["range"]["start"]["date"]
            return FakeResp(json.dumps({"rollupDataPoints": [
                {"civilStartTime": {"date": s}, "steps": {"countSum": "1"}}]}).encode())
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if dtype == "sleep" and "pageToken" not in q:
            return FakeResp(json.dumps({"dataPoints": [{"n": 1}], "nextPageToken": "P2"}).encode())
        return FakeResp(json.dumps({"dataPoints": [{"n": 2}]}).encode())


def _client(fake):
    return g.GoogleHealthClient("cid", "sec", "RT", opener=fake, sleep=lambda s: None)


def test_client_token_cached_and_pagination():
    fake = FakeGoogle()
    c = _client(fake)
    assert c.list("sleep", "f", 25) == [{"n": 1}, {"n": 2}]
    c.list("weight", "f")
    assert fake.token_calls == 1
    q = urllib.parse.parse_qs(urllib.parse.urlparse(fake.requests[1][2]).query)
    assert q["pageToken"] == ["P2"] and q["pageSize"] == ["25"]


def test_rollup_is_chunked_to_api_limit():
    fake = FakeGoogle()
    out = _client(fake).daily_rollup("heart-rate", date(2026, 1, 1), date(2026, 1, 31))
    bodies = [json.loads(r[3]) for r in fake.requests]
    spans = [(b["range"]["start"]["date"]["day"], b["range"]["end"]["date"]["day"]) for b in bodies]
    assert spans == [(1, 15), (15, 29), (29, 31)] and len(out) == 3
    assert all(b["windowSizeDays"] == 1 for b in bodies)


def test_retries_transient_errors():
    fake = FakeGoogle(flaky=2)
    assert _client(fake).list("weight", "f") == [{"n": 2}]


def test_expired_refresh_token_raises_auth_error():
    with pytest.raises(g.AuthError):
        _client(FakeGoogle(token_status=400)).list("weight", "f")


def test_fetch_builds_filters_and_isolates_failures():
    fake = FakeGoogle(fail={"daily-vo2-max": 403})
    raw, errors = g.fetch(_client(fake), date(2026, 3, 5), date(2026, 3, 12))
    assert errors == {"daily-vo2-max": "HTTP 403"}
    assert "daily-heart-rate-variability" in raw and "steps.rollup" in raw and "daily-vo2-max" not in raw
    filters = {r[1]: urllib.parse.parse_qs(urllib.parse.urlparse(r[2]).query).get("filter", [""])[0]
               for r in fake.requests if r[0] == "GET"}
    assert filters["daily-heart-rate-variability"] == \
        'daily_heart_rate_variability.date >= "2026-03-05" AND daily_heart_rate_variability.date < "2026-03-12"'
    assert filters["sleep"] == \
        'sleep.interval.civil_end_time >= "2026-03-05T00:00:00" AND sleep.interval.civil_end_time < "2026-03-12T00:00:00"'
    assert filters["body-fat"].startswith('body_fat.sample_time.civil_time >= "2026-03-05T00:00:00"')


# ---------------------------------------------------------------- sync -------
class StaticApi:
    name = "google"

    def __init__(self, raw=None, exc=None):
        self.raw, self.exc = raw, exc

    def fetch(self):
        if self.exc:
            raise self.exc
        days, wk = g.parse(self.raw)
        return days, wk, {"window": "2026-03-05..2026-03-11", "types_ok": len(self.raw), "types_failed": {}}


def test_sync_api_source_upserts_and_is_idempotent(tmp_path):
    data, state = tmp_path / "data", tmp_path / "state.json"
    r1 = sync.run([], str(data), str(state), api_sources=[StaticApi(_raw())])
    assert sorted(r1.daily.added) == ["2026-03-10", "2026-03-11"] and r1.workouts.added == ["2026-03-11 00:30"]
    first = (data / "daily.csv").read_text()
    assert "skin_temp_delta_c" in first.splitlines()[0]
    r2 = sync.run([], str(data), str(state), api_sources=[StaticApi(_raw())])
    assert not r2.written and (data / "daily.csv").read_text() == first
    st = json.loads(state.read_text())
    assert st["api"]["google"]["types_ok"] == len(_raw()) and st["latest_date"] == "2026-03-11"
    assert any(l.startswith("google: 2026-03-05..2026-03-11") for l in r2.lines())


def test_sync_api_failure_is_reported_not_fatal(tmp_path):
    res = sync.run([], str(tmp_path / "d"), str(tmp_path / "s.json"),
                   api_sources=[StaticApi(exc=g.AuthError(400, "invalid_grant"))])
    assert "AuthError" in res.api_failed["google"]
    assert not res.daily.added and not res.workouts.added
    assert any("FAILED" in l for l in res.lines())


def test_cli_google_without_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("GOOGLE_HEALTH_CREDS", raising=False)
    rc = cli.main(["sync", "--google", "--data-dir", str(tmp_path / "d"), "--state", str(tmp_path / "s.json")])
    assert rc == 2 and "GOOGLE_HEALTH_CREDS is not set" in capsys.readouterr().err


def test_cli_google_end_to_end_with_fake_api(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GOOGLE_HEALTH_CREDS", json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    fake = FakeGoogle(token_status=400)
    real = g.GoogleHealthClient.__init__

    def patched(self, *a, **k):
        real(self, *a, opener=fake, sleep=lambda s: None)

    monkeypatch.setattr(g.GoogleHealthClient, "__init__", patched)
    rc = cli.main(["sync", "--google", "--data-dir", str(tmp_path / "d"), "--state", str(tmp_path / "s.json")])
    out = capsys.readouterr().out
    assert rc == 1 and "token rejected" in out and "RT" not in out and '"r"' not in out
