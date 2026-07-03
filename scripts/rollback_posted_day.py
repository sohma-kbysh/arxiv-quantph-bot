#!/usr/bin/env python3
"""Remove one local day of posted paper state so the notifier can repost it.

Dry-run by default. This edits posted_log.json and seen_ids.json only when
--write is passed.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "posted_log.json"
STATE_PATH = ROOT / "seen_ids.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rollback posted_log.json and seen_ids.json for one local day.")
    parser.add_argument("--date",
                        help="Local date to rollback, YYYY-MM-DD.")
    parser.add_argument("--today", action="store_true",
                        help="Rollback today's date in --timezone.")
    parser.add_argument("--timezone", default="Asia/Tokyo",
                        help="Timezone for --date/--today. Default: Asia/Tokyo.")
    parser.add_argument("--write", action="store_true",
                        help="Actually write files. Omit for dry-run.")
    return parser.parse_args()


def utc_window(day: str, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.fromisoformat(day).replace(tzinfo=tz)
    return start.astimezone(timezone.utc), (
        start + timedelta(days=1)).astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> None:
    args = parse_args()
    if args.today:
        args.date = datetime.now(ZoneInfo(args.timezone)).date().isoformat()
    if not args.date:
        raise SystemExit("Pass --date YYYY-MM-DD or --today.")

    start, end = utc_window(args.date, args.timezone)
    log = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    remove_ids: set[str] = set()
    kept_log = []
    removed_log = []
    for entry in log:
        posted_at = parse_utc(entry.get("posted_at", "1970-01-01T00:00:00Z"))
        if start <= posted_at < end:
            removed_log.append(entry)
            if entry.get("id"):
                remove_ids.add(entry["id"])
        else:
            kept_log.append(entry)

    remaining_ids = {entry.get("id") for entry in kept_log if entry.get("id")}
    remove_from_seen = remove_ids - remaining_ids
    old_seen = list(state.get("seen", []))
    new_seen = [paper_id for paper_id in old_seen if paper_id not in remove_from_seen]

    action = "write" if args.write else "dry-run"
    print(f"[summary] action={action} date={args.date} timezone={args.timezone}")
    print(f"[summary] utc_window={start.isoformat()}..{end.isoformat()}")
    print(f"[summary] posted_log remove={len(removed_log)} keep={len(kept_log)}")
    print(f"[summary] unique removed paper ids={len(remove_ids)}")
    print(f"[summary] seen_ids remove={len(old_seen) - len(new_seen)} keep={len(new_seen)}")
    for paper_id in sorted(remove_ids):
        print(f"[id] {paper_id}")

    if not args.write:
        print("[summary] no files written; rerun with --write to update state")
        return

    LOG_PATH.write_text(json.dumps(kept_log, indent=1) + "\n", encoding="utf-8")
    state["seen"] = new_seen
    STATE_PATH.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    print("[summary] files updated")


if __name__ == "__main__":
    main()
