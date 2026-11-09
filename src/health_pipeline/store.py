"""CSV store with keyed upserts.

Rows are keyed (by ``date`` for daily metrics, ``start`` for workouts). Merging
a new record only touches the columns the record carries, so a late export
that only has sleep for a day does not wipe that day's steps. Values are
rounded before they are compared, so float noise never produces a diff and a
re-run over the same input leaves the file byte-for-byte unchanged.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import ROUNDING


def _fmt(col: str, v: Any) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, float) or (isinstance(v, int) and not isinstance(v, bool)):
        nd = ROUNDING.get(col)
        if nd is None:
            return repr(float(v)) if isinstance(v, float) else str(v)
        r = round(float(v), nd)
        return str(int(r)) if nd == 0 else f"{r:.{nd}f}".rstrip("0").rstrip(".")
    return str(v)


@dataclass
class MergeReport:
    added: list[str] = field(default_factory=list)
    updated: dict[str, list[str]] = field(default_factory=dict)  # key -> changed columns
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)

    def summary(self, label: str) -> str:
        return (f"{label}: {len(self.added)} added, {len(self.updated)} updated, "
                f"{self.unchanged} unchanged")


class CsvTable:
    def __init__(self, path: str, columns: list[str], key: str):
        self.path, self.columns, self.key = path, columns, key
        self.rows: dict[str, dict[str, str]] = {}
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    k = r.get(key)
                    if k:
                        self.rows[k] = {c: r.get(c, "") or "" for c in columns}

    def merge(self, records: Iterable[dict[str, Any]], report: MergeReport | None = None) -> MergeReport:
        report = report or MergeReport()
        seen: set[str] = set()
        for rec in records:
            k = rec.get(self.key)
            if not k:
                continue
            k = str(k)
            new_vals = {c: _fmt(c, v) for c, v in rec.items() if c in self.columns and c != self.key}
            new_vals = {c: v for c, v in new_vals.items() if v != ""}
            if k not in self.rows:
                row = {c: "" for c in self.columns}
                row[self.key] = k
                row.update(new_vals)
                self.rows[k] = row
                if k not in report.added:
                    report.added.append(k)
                seen.add(k)
                continue
            row = self.rows[k]
            changed = [c for c, v in new_vals.items() if row.get(c, "") != v]
            if changed:
                row.update({c: new_vals[c] for c in changed})
                if k not in report.added:
                    cols = report.updated.setdefault(k, [])
                    cols.extend(c for c in changed if c not in cols)
            elif k not in report.added and k not in report.updated and k not in seen:
                report.unchanged += 1
            seen.add(k)
        return report

    def render(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.columns, lineterminator="\n")
        w.writeheader()
        for k in sorted(self.rows):
            w.writerow(self.rows[k])
        return buf.getvalue()

    def save(self) -> bool:
        """Write only if the content changed. Returns True when the file was written."""
        text = self.render()
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                if f.read() == text:
                    return False
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return True
