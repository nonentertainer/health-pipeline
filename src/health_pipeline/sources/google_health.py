"""Google Health API source: Fitbit data straight from Google, no phone in the loop.

Every run asks for a trailing window of days (default 7) and the store upserts it,
so late-arriving data (a night that synced at noon, an HRV value finalised after
waking) is picked up by the next run without any bookkeeping.

The parser works on a plain ``{data type: [data points]}`` dict, the same shape
``tools/google_login.py`` writes to disk, so it can be tested against saved
responses without network access.

Credentials come from one JSON secret (``GOOGLE_HEALTH_CREDS``):
``{"client_id": ..., "client_secret": ..., "refresh_token": ...}``.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

API = "https://health.googleapis.com/v4/users/me/dataTypes"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# daily data types: (json key, {column: field})
DAILY: dict[str, tuple[str, dict[str, str]]] = {
    "daily-heart-rate-variability": ("dailyHeartRateVariability", {
        "hrv_ms": "averageHeartRateVariabilityMilliseconds",
        "sleep_hr_bpm": "nonRemHeartRateBeatsPerMinute"}),
    "daily-resting-heart-rate": ("dailyRestingHeartRate", {"resting_hr_bpm": "beatsPerMinute"}),
    "daily-oxygen-saturation": ("dailyOxygenSaturation", {"spo2_pct": "averagePercentage"}),
    "daily-respiratory-rate": ("dailyRespiratoryRate", {"resp_rate_bpm": "breathsPerMinute"}),
    "daily-vo2-max": ("dailyVo2Max", {"vo2max": "vo2Max"}),
    "daily-sleep-temperature-derivations": ("dailySleepTemperatureDerivations", {}),  # custom below
}
ROLLUP = ["steps", "distance", "floors", "active-energy-burned", "total-calories",
          "active-zone-minutes", "active-minutes", "heart-rate"]
ROLLUP_MAX_DAYS = 14  # the API's limit for heart-rate / total-calories / active-minutes
SESSIONS = {"sleep": "sleep.interval.civil_end_time", "exercise": "exercise.interval.civil_start_time"}
SAMPLES = ["weight", "body-fat"]


class GoogleHealthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message = status, message


class AuthError(GoogleHealthError):
    """The refresh token was rejected (expired or revoked): someone has to log in again."""


def _err_text(body: str) -> str:
    try:
        e = json.loads(body).get("error", {})
    except ValueError:
        return body[:200]
    if isinstance(e, dict):
        return f"{e.get('status', '')} {e.get('message', '')}".strip()[:200]
    return str(e)[:200]


Opener = Callable[[urllib.request.Request], Any]


class GoogleHealthClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=60))
        self._sleep = sleep
        self._token: str | None = None
        self._token_exp = 0.0
        self.calls = 0

    @classmethod
    def from_env(cls, var: str = "GOOGLE_HEALTH_CREDS") -> "GoogleHealthClient | None":
        raw = os.environ.get(var)
        if not raw:
            return None
        c = json.loads(raw)
        return cls(c["client_id"], c.get("client_secret", ""), c["refresh_token"])

    # -- http -----------------------------------------------------------------
    def _send(self, req: urllib.request.Request) -> dict:
        for attempt in range(3):
            try:
                with self._open(req) as r:
                    self.calls += 1
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    self._sleep(2 ** attempt * 2)
                    continue
                raise GoogleHealthError(e.code, _err_text(body)) from None
        raise AssertionError("unreachable")

    def access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        data = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode()
        req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            tok = self._send(req)
        except GoogleHealthError as e:
            if e.status in (400, 401):
                raise AuthError(e.status, e.message) from None
            raise
        self._token = tok["access_token"]
        self._token_exp = time.time() + float(tok.get("expires_in", 3600))
        return self._token

    def _api(self, method: str, url: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        return self._send(urllib.request.Request(url, data=data, method=method, headers=headers))

    # -- endpoints ------------------------------------------------------------
    def list(self, dtype: str, flt: str, page_size: int = 1000) -> list[dict]:
        out: list[dict] = []
        page = None
        for _ in range(100):
            q = {"filter": flt, "pageSize": page_size}
            if page:
                q["pageToken"] = page
            r = self._api("GET", f"{API}/{dtype}/dataPoints?" + urllib.parse.urlencode(q))
            out.extend(r.get("dataPoints", []))
            page = r.get("nextPageToken")
            if not page:
                return out
        raise GoogleHealthError(0, f"{dtype}: too many pages")

    def daily_rollup(self, dtype: str, start: date, end: date) -> list[dict]:
        """Daily totals for [start, end), split into chunks the API accepts."""
        out: list[dict] = []
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=ROLLUP_MAX_DAYS), end)
            body = {"range": {"start": _civil(cur), "end": _civil(nxt)}, "windowSizeDays": 1}
            r = self._api("POST", f"{API}/{dtype}/dataPoints:dailyRollUp", body)
            out.extend(r.get("rollupDataPoints", []))
            cur = nxt
        return out


def _civil(d: date) -> dict:
    return {"date": {"year": d.year, "month": d.month, "day": d.day},
            "time": {"hours": 0, "minutes": 0, "seconds": 0, "nanos": 0}}


def fetch(client: GoogleHealthClient, start: date, end: date) -> tuple[dict[str, list], dict[str, str]]:
    """Raw data points for [start, end) per data type, plus per-type errors."""
    s, e = start.isoformat(), end.isoformat()
    jobs: dict[str, Callable[[], list]] = {}
    for t in DAILY:
        f = t.replace("-", "_")
        jobs[t] = lambda t=t, f=f: client.list(t, f'{f}.date >= "{s}" AND {f}.date < "{e}"')
    for t, field in SESSIONS.items():
        jobs[t] = lambda t=t, field=field: client.list(
            t, f'{field} >= "{s}T00:00:00" AND {field} < "{e}T00:00:00"', page_size=25)
    for t in SAMPLES:
        f = t.replace("-", "_")
        jobs[t] = lambda t=t, f=f: client.list(
            t, f'{f}.sample_time.civil_time >= "{s}T00:00:00" AND {f}.sample_time.civil_time < "{e}T00:00:00"')
    for t in ROLLUP:
        jobs[t + ".rollup"] = lambda t=t: client.daily_rollup(t, start, end)

    raw: dict[str, list] = {}
    errors: dict[str, str] = {}
    for name, job in jobs.items():
        try:
            raw[name] = job()
        except AuthError:
            raise
        except GoogleHealthError as ex:  # one data type failing must not lose the others
            errors[name] = f"HTTP {ex.status}"
    return raw, errors


# ---------------------------------------------------------------- parsing ----
def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None  # e.g. skin temperature before Fitbit has a baseline


def _date(d: dict | None) -> str | None:
    if not d:
        return None
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def _offset(v: Any) -> timezone:
    try:
        return timezone(timedelta(seconds=int(str(v).rstrip("s"))))
    except (TypeError, ValueError):
        return timezone.utc


def _local(ts: str | None, offset: Any) -> datetime | None:
    """'2026-11-09T05:58:00.123Z' + '10800s' -> naive local datetime."""
    if not ts:
        return None
    t = ts.replace("Z", "+00:00")
    if "." in t:  # drop fractional seconds of any length (fromisoformat is picky before 3.11)
        head, _, rest = t.partition(".")
        cut = next((i for i, ch in enumerate(rest) if ch in "+-"), len(rest))
        t = head + rest[cut:]
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_offset(offset)).replace(tzinfo=None)


def _payload(p: dict) -> dict:
    """The type-specific object of a data point (the key that is not name/dataSource)."""
    for k, v in p.items():
        if k not in ("name", "dataSource") and isinstance(v, dict):
            return v
    return {}


def _put(days: dict[str, dict], d: str | None, col: str, v: Any) -> None:
    if d and v is not None and col not in days.setdefault(d, {}):
        days[d][col] = v


def parse_daily(raw: dict[str, list], days: dict[str, dict]) -> None:
    for dtype, (key, cols) in DAILY.items():
        for p in raw.get(dtype) or []:
            obj = p.get(key) or {}
            d = _date(obj.get("date"))
            for col, field in cols.items():
                _put(days, d, col, _num(obj.get(field)))
            if dtype == "daily-sleep-temperature-derivations":
                night, base = _num(obj.get("nightlyTemperatureCelsius")), _num(obj.get("baselineTemperatureCelsius"))
                _put(days, d, "wrist_temp_c", night)
                if night is not None and base is not None:
                    _put(days, d, "skin_temp_delta_c", night - base)


def parse_rollups(raw: dict[str, list], days: dict[str, dict]) -> None:
    def each(dtype: str, key: str):
        for p in raw.get(dtype + ".rollup") or []:
            d = _date((p.get("civilStartTime") or {}).get("date"))
            obj = p.get(key)
            if d and isinstance(obj, dict) and obj:
                yield d, obj

    for d, o in each("steps", "steps"):
        _put(days, d, "steps", _num(o.get("countSum")))
    for d, o in each("distance", "distance"):
        mm = _num(o.get("millimetersSum"))
        _put(days, d, "distance_km", None if mm is None else mm / 1e6)
    for d, o in each("floors", "floors"):
        _put(days, d, "flights", _num(o.get("countSum")))
    for d, o in each("active-energy-burned", "activeEnergyBurned"):
        _put(days, d, "active_kcal", _num(o.get("kcalSum")))
    for d, o in each("total-calories", "totalCalories"):
        _put(days, d, "total_kcal", _num(o.get("kcalSum")))
    for d, o in each("active-zone-minutes", "activeZoneMinutes"):
        fat = _num(o.get("sumInFatBurnHeartZone")) or 0
        hard = (_num(o.get("sumInCardioHeartZone")) or 0) + (_num(o.get("sumInPeakHeartZone")) or 0)
        _put(days, d, "active_zone_min", fat + 2 * hard)  # Fitbit counts cardio/peak minutes double
    for d, o in each("active-minutes", "activeMinutes"):
        lv = {x.get("activityLevel"): _num(x.get("activeMinutesSum")) or 0
              for x in o.get("activeMinutesRollupByActivityLevel") or []}
        if lv:
            _put(days, d, "exercise_min", lv.get("MODERATE", 0) + lv.get("VIGOROUS", 0))
    for d, o in each("heart-rate", "heartRate"):
        _put(days, d, "hr_min_bpm", _num(o.get("beatsPerMinuteMin")))
        _put(days, d, "hr_avg_bpm", _num(o.get("beatsPerMinuteAvg")))
        _put(days, d, "hr_max_bpm", _num(o.get("beatsPerMinuteMax")))


def parse_sleep(raw: dict[str, list], days: dict[str, dict]) -> None:
    """One night per wake-up date: the session Fitbit marks as main sleep (else the longest)."""
    best: dict[str, tuple[int, int, dict, datetime, datetime]] = {}
    for p in raw.get("sleep") or []:
        s = p.get("sleep") or {}
        iv = s.get("interval") or {}
        start = _local(iv.get("startTime"), iv.get("startUtcOffset"))
        end = _local(iv.get("endTime"), iv.get("endUtcOffset"))
        if not start or not end:
            continue
        summ = s.get("summary") or {}
        rank = (1 if (s.get("metadata") or {}).get("mainSleep") else 0, int(_num(summ.get("minutesAsleep")) or 0))
        d = end.date().isoformat()
        if d not in best or rank > best[d][:2]:
            best[d] = (*rank, s, start, end)

    for d, (_, _, s, start, end) in best.items():
        summ = s.get("summary") or {}
        stages = {x.get("type"): _num(x.get("minutes")) for x in summ.get("stagesSummary") or []}
        row = {
            "sleep_min": _num(summ.get("minutesAsleep")),
            "in_bed_min": _num(summ.get("minutesInSleepPeriod")),
            "sleep_awake_min": stages.get("AWAKE", _num(summ.get("minutesAwake"))),
            "sleep_start": start.strftime("%Y-%m-%d %H:%M"),
            "sleep_end": end.strftime("%Y-%m-%d %H:%M"),
        }
        if s.get("type") == "STAGES":
            row.update({"sleep_deep_min": stages.get("DEEP"), "sleep_rem_min": stages.get("REM"),
                        "sleep_core_min": stages.get("LIGHT")})
        for col, v in row.items():
            _put(days, d, col, v)


def parse_samples(raw: dict[str, list], days: dict[str, dict]) -> None:
    """Weight / body fat: the first reading of each day (the morning weigh-in)."""
    for dtype, col in (("weight", "weight_kg"), ("body-fat", "body_fat_pct")):
        readings = []
        for p in raw.get(dtype) or []:
            obj = _payload(p)
            st = obj.get("sampleTime") or {}
            when = _local(st.get("physicalTime"), st.get("utcOffset")) if st.get("physicalTime") else None
            if when is None and st.get("civilTime"):
                c = st["civilTime"]
                d0 = _date(c.get("date"))
                tm = c.get("time") or {}
                if d0:
                    when = datetime.fromisoformat(d0).replace(hour=int(tm.get("hours", 0)), minute=int(tm.get("minutes", 0)))
            val = None
            for k, v in obj.items():
                n = _num(v)
                if n is None:
                    continue
                kl = k.lower()
                if dtype == "weight" and "gram" in kl:  # weightGrams or kilograms
                    val = n if "kilogram" in kl else n / 1000
                elif dtype == "body-fat" and "percent" in kl:
                    val = n
            if when and val is not None:
                readings.append((when, val))
        for when, val in sorted(readings):
            _put(days, when.date().isoformat(), col, val)


def parse_workouts(raw: dict[str, list]) -> list[dict[str, Any]]:
    rows = []
    for p in raw.get("exercise") or []:
        e = p.get("exercise") or {}
        iv = e.get("interval") or {}
        start = _local(iv.get("startTime"), iv.get("startUtcOffset"))
        end = _local(iv.get("endTime"), iv.get("endUtcOffset"))
        if not start:
            continue
        m = e.get("metricsSummary") or {}
        active_s = _num(str(e.get("activeDuration", "")).rstrip("s"))
        dur = active_s / 60 if active_s is not None else ((end - start).total_seconds() / 60 if end else None)
        mm = _num(m.get("distanceMillimeters"))
        rows.append({
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "date": start.date().isoformat(),
            "name": e.get("displayName") or e.get("exerciseType") or "",
            "end": end.strftime("%Y-%m-%d %H:%M") if end else None,
            "duration_min": dur,
            "distance_km": None if mm is None else mm / 1e6,
            "total_kcal": _num(m.get("caloriesKcal")),
            "steps": _num(m.get("steps")),
            "hr_avg_bpm": _num(m.get("averageHeartRateBeatsPerMinute")),
        })
    return rows


def parse(raw: dict[str, list]) -> tuple[dict[str, dict], list[dict]]:
    days: dict[str, dict] = {}
    parse_daily(raw, days)
    parse_sleep(raw, days)
    parse_rollups(raw, days)
    parse_samples(raw, days)
    days = {d: {k: v for k, v in row.items() if v is not None} for d, row in days.items()}
    return {d: r for d, r in days.items() if r}, parse_workouts(raw)


def load_dir(path: str) -> dict[str, list]:
    """Read a folder of saved responses (``<type>.json``, as written by tools/google_login.py)."""
    raw = {}
    for name in os.listdir(path):
        if name.endswith(".json") and not name.startswith("_"):
            with open(os.path.join(path, name), encoding="utf-8") as f:
                v = json.load(f)
            if isinstance(v, list):
                raw[name[:-5]] = v
    return raw


class GoogleHealthSource:
    """API source for sync.run: fetches a trailing window and parses it."""

    name = "google"

    def __init__(self, client: GoogleHealthClient, days: int = 7, today: date | None = None):
        self.client, self.days = client, days
        self.today = today or datetime.now(timezone(timedelta(hours=3))).date()  # Europe/Istanbul

    def fetch(self) -> tuple[dict[str, dict], list[dict], dict[str, Any]]:
        start, end = self.today - timedelta(days=self.days - 1), self.today + timedelta(days=1)
        raw, errors = fetch(self.client, start, end)
        days, workouts = parse(raw)
        info = {"window": f"{start}..{self.today}", "types_ok": len(raw), "types_failed": errors,
                "points": sum(len(v) for v in raw.values()), "api_calls": self.client.calls}
        return days, workouts, info
