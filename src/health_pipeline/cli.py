"""Command line entry point.

    health-pipeline sync --data-dir data --state state/sync_state.json \
        [--google [--google-days 7]]                 # creds from $GOOGLE_HEALTH_CREDS
        [--inbox inbox] [--drive-folder FOLDER_ID]   # key from $GDRIVE_SA_KEY

Logs only counts and dates, never metric values, so CI logs stay clean even if
someone runs this from a public repository.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import sync
from .sources.local import LocalSource


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="health-pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="pull new exports and upsert data/*.csv")
    s.add_argument("--data-dir", default="data")
    s.add_argument("--state", default="state/sync_state.json")
    s.add_argument("--inbox", default=None, help="local folder with Health Auto Export *.json files")
    s.add_argument("--drive-folder", default=os.environ.get("GDRIVE_FOLDER_ID"),
                   help="Google Drive folder id (default: $GDRIVE_FOLDER_ID)")
    s.add_argument("--full", action="store_true", help="re-process every file, ignoring state")
    s.add_argument("--google", action="store_true",
                   help="fetch from the Google Health API (credentials JSON in $GOOGLE_HEALTH_CREDS)")
    s.add_argument("--google-days", type=int, default=7, help="trailing days to fetch (default 7)")
    args = p.parse_args(argv)

    api_sources = []
    if args.google:
        from .sources.google_health import GoogleHealthClient, GoogleHealthSource
        client = GoogleHealthClient.from_env()
        if client is None:
            print("GOOGLE_HEALTH_CREDS is not set; skipping Google Health", file=sys.stderr)
        else:
            api_sources.append(GoogleHealthSource(client, days=args.google_days))

    sources = []
    if args.inbox:
        sources.append(LocalSource(args.inbox))
    if args.drive_folder:
        key = os.environ.get("GDRIVE_SA_KEY")
        if not key:
            print("GDRIVE_SA_KEY is not set; skipping Drive", file=sys.stderr)
        else:
            from .sources.drive import DriveSource
            sources.append(DriveSource(key, args.drive_folder))
    if not sources and not api_sources:
        print("nothing to read: pass --google, --inbox and/or --drive-folder", file=sys.stderr)
        return 2

    res = sync.run(sources, args.data_dir, args.state, full=args.full, api_sources=api_sources)
    lines = res.lines()
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### health-pipeline sync\n\n" + "\n".join(f"- {l}" for l in lines) + "\n")
    if any("AuthError" in e for e in res.api_failed.values()):
        print("::error::Google Health token rejected (expired or revoked). "
              "Log in again on your computer (tools/google_login.py) and update GOOGLE_HEALTH_CREDS.")
    if res.api_failed:
        return 1  # fail the job so the owner gets notified
    return 1 if res.files_failed and not res.files_processed else 0


if __name__ == "__main__":
    raise SystemExit(main())
