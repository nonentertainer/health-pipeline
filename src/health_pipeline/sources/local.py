"""Local folder source: any *.json dropped into an inbox folder.

Useful for a one-off backfill (export 90 days by hand, drop the file in) and
for running the pipeline without Drive at all.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


@dataclass
class LocalFile:
    path: str
    digest: str

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def state_key(self) -> str:
        return f"local:{self.name}"

    @property
    def modified(self) -> str:
        return self.digest  # content hash: re-processed only when the content changes


class LocalSource:
    def __init__(self, folder: str):
        self.folder = folder

    def list_json(self) -> list[LocalFile]:
        if not os.path.isdir(self.folder):
            return []
        out = []
        for root, _, files in os.walk(self.folder):
            for n in sorted(files):
                if n.lower().endswith(".json"):
                    p = os.path.join(root, n)
                    with open(p, "rb") as fh:
                        out.append(LocalFile(p, hashlib.sha256(fh.read()).hexdigest()[:16]))
        return sorted(out, key=lambda f: (os.path.getmtime(f.path), f.name))

    def download(self, f: LocalFile) -> bytes:
        with open(f.path, "rb") as fh:
            return fh.read()
