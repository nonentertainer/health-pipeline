"""Command line entry point.

    health-pipeline sync --data-dir data --state state/sync_state.json \
        [--google [--google-days 7]]                 # creds from $GOOGLE_HEALTH_CREDS
        [--inbox inbox] [--drive-folder FOLDER_ID]   # key from $GDRIVE_SA_KEY

    health-pipeline insights --data-dir data --out data/insights.json [--md data/insights.md]
    health-pipeline report --data-dir data [--log LOG] [--week 2026-W45] [--out reports/2026-W45.md] [--json F]

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
    i = sub.add_parser("insights", help="analyse data/*.csv: habit/recovery associations and the illness-rule backtest")
    i.add_argument("--data-dir", default="data")
    i.add_argument("--out", default="data/insights.json")
    i.add_argument("--md", default=None, help="also write a readable Markdown summary here")
    r = sub.add_parser("report", help="one ISO week in numbers and sentences (Markdown, optional JSON)")
    r.add_argument("--data-dir", default="data")
    r.add_argument("--log", default=None, help="the person's day documents: a JSON list/object or a folder of JSON files")
    r.add_argument("--week", default=None, help="ISO week like 2026-W45 (default: the last complete week)")
    r.add_argument("--out", default=None, help="Markdown file (default: reports/<week>.md)")
    r.add_argument("--json", default=None, help="also write the numbers and sentences as JSON here")
    r.add_argument("--lang", default="tr", choices=["tr", "en"])
    r.add_argument("--target", type=int, default=5, help="planned training days per week")
    args = p.parse_args(argv)

    if args.cmd == "report":
        from datetime import date
        from . import report
        week = args.week or report.last_full_week(date.today())
        rep = report.build(args.data_dir, week, report.read_log(args.log), args.target)
        out = args.out or os.path.join("reports", f"{week}.md")
        report.write(rep, out, args.json, args.lang)
        print(f"report {week}: {rep['training']['days']} training days, "
              f"{rep['sleep']['nights'] if rep['sleep'] else 0} nights, "
              f"{rep['weight']['n'] if rep['weight'] else 0} weigh-ins -> {out}")
        return 0

    if args.cmd == "insights":
        from . import insights
        ins = insights.build(args.data_dir)
        insights.write(ins, args.out, args.md)
        clear = sum(1 for d in ins["drivers"] if d["verdict"] == "clear")
        print(f"insights: {len(ins['drivers'])} tests on {ins['nights']} nights, {clear} clear, "
              f"{len(ins['alerts']['flagged'])} alert nights of {ins['alerts']['nights']} -> {args.out}")
        return 0

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
