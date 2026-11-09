"""Extract (sources) -> transform (parse_hae) -> load (CsvTable) in one incremental run.

A small state file remembers which source files were already processed and at
which version, so each run only downloads and parses new or changed exports.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol

from . import parse_hae, parse_shortcut
from .model import DAILY_COLUMNS, WORKOUT_COLUMNS
from .store import CsvTable, MergeReport


class Source(Protocol):
    def list_json(self) -> list: ...
    def download(self, f) -> bytes: ...


@dataclass
class SyncResult:
    files_seen: int = 0
    files_processed: int = 0
    files_failed: list[str] = field(default_factory=list)
    daily: MergeReport = field(default_factory=MergeReport)
    workouts: MergeReport = field(default_factory=MergeReport)
    written: bool = False

    def lines(self) -> list[str]:
        out = [
            f"files: {self.files_seen} seen, {self.files_processed} processed, {len(self.files_failed)} failed",
            self.daily.summary("daily"),
            self.workouts.summary("workouts"),
        ]
        if self.daily.added:
            out.append("daily added: " + ", ".join(sorted(self.daily.added)))
        if self.daily.updated:
            out.append("daily updated: " + ", ".join(f"{d} ({len(c)} cols)" for d, c in sorted(self.daily.updated.items())))
        if self.files_failed:
            out.append("failed: " + ", ".join(self.files_failed))
        return out


def _load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"files": {}}


def run(sources: Iterable[Source], data_dir: str, state_path: str, full: bool = False) -> SyncResult:
    state = _load_state(state_path)
    done: dict[str, str] = {} if full else dict(state.get("files", {}))
    daily = CsvTable(os.path.join(data_dir, "daily.csv"), DAILY_COLUMNS, "date")
    workouts = CsvTable(os.path.join(data_dir, "workouts.csv"), WORKOUT_COLUMNS, "start")
    res = SyncResult()

    for src in sources:
        for f in src.list_json():  # oldest first, so newer exports win
            res.files_seen += 1
            if done.get(f.state_key) == f.modified:
                continue
            try:
                doc = parse_hae.load(src.download(f))
                if parse_shortcut.is_shortcut_payload(doc):
                    days, wk = parse_shortcut.parse(doc), []
                else:
                    days, wk = parse_hae.parse_metrics(doc), parse_hae.parse_workouts(doc)
                daily.merge(({"date": d, **vals} for d, vals in sorted(days.items())), res.daily)
                workouts.merge(wk, res.workouts)
            except Exception as e:  # one bad file must not block the rest
                res.files_failed.append(f"{f.name}: {type(e).__name__}")
                continue
            done[f.state_key] = f.modified
            res.files_processed += 1

    wrote_daily, wrote_wk = daily.save(), workouts.save()
    res.written = wrote_daily or wrote_wk
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.update({
        "files": done,
        "last_run": now,
        "latest_date": max(daily.rows) if daily.rows else None,
        "rows": len(daily.rows),
    })
    if res.written:
        state["last_change"] = now
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    return res
