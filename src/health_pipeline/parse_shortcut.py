"""Parse the payload an iOS Shortcut sends (no third-party export app).

The Shortcut stays deliberately dumb: it reads the last few days from Apple
Health and sends the values as plain strings. Everything that is easy to get
wrong on a phone (day bucketing, sleep-stage overlaps, locale number formats)
happens here, where it is tested.

Payload (GitHub ``repository_dispatch`` ``client_payload``)::

    {
      "source": "shortcut",
      "steps":          "2026-11-07=10.234;2026-11-08=8.120",   # Health "group by day" output
      "active_kcal":    "2026-11-07=512,4;2026-11-08=430",
      "hrv_ms":         "2026-11-08=30,1;2026-11-09=29,8",
      "resting_hr_bpm": "2026-11-08=57;2026-11-09=57,3",
      "weight_kg":      "2026-11-09=87,75",
      "sleep":          "2026-11-08T23:40|2026-11-09T02:10|Çekirdek;2026-11-09T02:10|2026-11-09T02:55|Derin;..."
    }

Any metric key of the daily schema may appear as ``"YYYY-MM-DD=value;..."``.
A flat ``{"date": "...", "hrv_ms": 30.1, ...}`` object is accepted too.
Numbers may use Turkish formatting (``10.234`` = ten thousand, ``30,1`` = 30.1).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .model import DAILY_COLUMNS

QUANTITY_KEYS = set(DAILY_COLUMNS) - {"date", "sleep_start", "sleep_end"} - {
    c for c in DAILY_COLUMNS if c.startswith("sleep_") or c == "in_bed_min"}

# Apple Health sleep values as Shortcuts prints them, English and Turkish.
STAGES = {
    "in bed": "in_bed", "inbed": "in_bed", "yatakta": "in_bed",
    "awake": "awake", "uyanık": "awake", "uyanik": "awake",
    "core": "core", "çekirdek": "core", "cekirdek": "core", "asleep core": "core",
    "deep": "deep", "derin": "deep", "asleep deep": "deep",
    "rem": "rem", "asleep rem": "rem",
    "asleep": "asleep", "uykuda": "asleep", "asleep unspecified": "asleep", "uyku": "asleep",
}
ASLEEP = {"core", "deep", "rem", "asleep"}

_NUM = re.compile(r"[-+]?[\d.,   ]*\d")


def parse_number(text: Any) -> float | None:
    """Locale-tolerant number parsing.

    '30,1' -> 30.1, '10.234' -> 10234, '1.234,5' -> 1234.5, '87.75' -> 87.75,
    '12 345' -> 12345, '57 bpm' -> 57.
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _NUM.search(str(text))
    if not m:
        return None
    s = re.sub(r"[   ]", "", m.group(0))
    if "," in s and "." in s:
        # whichever comes last is the decimal separator
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", "") if len(parts) > 2 else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif "." in s:
        head, tail = s.split(".")
        # '10.234' with exactly 3 trailing digits and a short head is Turkish thousands grouping
        if len(tail) == 3 and 1 <= len(head.lstrip("-+")) <= 3 and head.lstrip("-+") != "0":
            s = head + tail
    try:
        return float(s)
    except ValueError:
        return None


_DATE_PATTERNS = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"]
_DT_PATTERNS = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"]


def parse_date(text: str) -> str | None:
    t = str(text).strip()[:10]
    for p in _DATE_PATTERNS:
        try:
            return datetime.strptime(t, p).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_dt(text: str) -> datetime | None:
    t = re.sub(r"\s*([+-]\d{2}:?\d{2}|Z)$", "", str(text).strip())  # local wall time; drop offset
    for p in _DT_PATTERNS:
        try:
            return datetime.strptime(t, p)
        except ValueError:
            pass
    return None


def _pairs(value: Any):
    """'2026-11-08=30,1;2026-11-09=29,8' -> [('2026-11-08', 30.1), ...]. Also accepts dicts and lists."""
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = [tuple(str(x).split("=", 1)) for x in value if "=" in str(x)]
    else:
        items = [tuple(part.split("=", 1)) for part in re.split(r"[;\n]", str(value or "")) if "=" in part]
    for d, v in items:
        date, num = parse_date(d), parse_number(v)
        if date and num is not None:
            yield date, num


def _union(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _minutes(intervals) -> float:
    return sum((e - s).total_seconds() for s, e in intervals) / 60


def night_of(end: datetime) -> str:
    """The date a sleep segment counts toward: the morning it ends on.

    Anything ending before 18:00 belongs to that day; ending in the evening
    belongs to the next day (an early bedtime segment of tonight's sleep).
    """
    return ((end - timedelta(hours=18)).date() + timedelta(days=1)).isoformat()


def parse_sleep(value: Any) -> dict[str, dict[str, Any]]:
    segs: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    raw = value if isinstance(value, list) else re.split(r"[;\n]", str(value or ""))
    for item in raw:
        parts = [p.strip() for p in (item if isinstance(item, (list, tuple)) else str(item).split("|"))]
        if len(parts) < 3:
            continue
        s, e = parse_dt(parts[0]), parse_dt(parts[1])
        stage = STAGES.get(parts[2].strip().lower())
        if not s or not e or e <= s or not stage:
            continue
        segs[night_of(e)][stage].append((s, e))
    out: dict[str, dict[str, Any]] = {}
    for night, by_stage in segs.items():
        asleep = _union([iv for st in ASLEEP for iv in by_stage.get(st, [])])
        if not asleep:
            continue
        everything = _union([iv for ivs in by_stage.values() for iv in ivs])
        rec: dict[str, Any] = {
            "sleep_min": _minutes(asleep),
            "in_bed_min": _minutes(everything),
            "sleep_start": asleep[0][0].strftime("%Y-%m-%d %H:%M"),
            "sleep_end": asleep[-1][1].strftime("%Y-%m-%d %H:%M"),
        }
        for st, col in (("core", "sleep_core_min"), ("deep", "sleep_deep_min"),
                        ("rem", "sleep_rem_min"), ("awake", "sleep_awake_min")):
            if by_stage.get(st):
                rec[col] = _minutes(_union(by_stage[st]))
        out[night] = rec
    return out


def is_shortcut_payload(doc: Any) -> bool:
    if not isinstance(doc, dict) or "data" in doc or "metrics" in doc:
        return False
    return doc.get("source") == "shortcut" or "sleep" in doc or any(k in QUANTITY_KEYS for k in doc)


def parse(doc: dict) -> dict[str, dict[str, Any]]:
    days: dict[str, dict[str, Any]] = defaultdict(dict)
    flat_date = parse_date(doc["date"]) if doc.get("date") else None
    for key, value in doc.items():
        if key == "sleep":
            for night, rec in parse_sleep(value).items():
                days[night].update(rec)
        elif key in QUANTITY_KEYS:
            single = not isinstance(value, (list, dict)) and "=" not in str(value)
            if flat_date and single:  # flat form: {"date": ..., "hrv_ms": 30.1}
                n = parse_number(value)
                if n is not None:
                    days[flat_date][key] = n
                continue
            for d, n in _pairs(value):
                if key == "spo2_pct" and n <= 1:
                    n *= 100
                days[d][key] = n
    return dict(days)
