#!/usr/bin/env python3
"""Turn a clipboard SciRate snapshot into a published relay file.

The bookmarklet in ``tools/scirate_snapshot_bookmarklet.js`` copies a JSON
snapshot from a SciRate page you opened yourself.  This script validates that
JSON through the *bot's own* acceptance path -- not a re-implementation of it --
so anything this script accepts is guaranteed to be accepted at digest time.
It then writes the file into the snapshot repository and pushes it.

    python3 tools/scirate_snapshot.py                 # read the clipboard
    python3 tools/scirate_snapshot.py --file snap.json
    python3 tools/scirate_snapshot.py --dry-run       # validate only
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import arxiv_bot                        # noqa: E402
import scirate_weekly                   # noqa: E402

DEFAULT_REPO = Path.home() / "scirate-snapshots"
RELAY_DIR = "quant-ph"


def read_payload(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    try:
        out = subprocess.run(
            ["pbpaste"], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"could not read the clipboard: {exc}") from exc
    return out.stdout


def validate(snapshot: dict, cfg: dict) -> tuple[int, int, int]:
    """Run the real relay acceptance path against this snapshot in memory.

    Returns (range_days, accepted_rows, min_scites_used).  Raises SystemExit
    with the bot's own message when the snapshot would be rejected.
    """
    days = int(snapshot.get("range_days") or 0)
    target = str(snapshot.get("date") or "")
    if days < 1 or not target:
        raise SystemExit("snapshot is missing date or range_days")

    if days == 1:
        min_scites = int(cfg.get("scirate_daily_min_scites", 1))
        limit = int(cfg.get("scirate_daily_top_n", 3))
    else:
        min_scites = int(cfg.get("scirate_min_scites", 30))
        limit = None

    raw = json.dumps(snapshot).encode("utf-8")
    original = arxiv_bot.http_get

    def fake_http_get(url, timeout=30, headers=None, allow_redirects=True):
        # The relay call site must not follow redirects; assert that here so a
        # regression in the bot cannot silently loosen it.
        assert allow_redirects is False, "relay fetch must reject redirects"
        return raw

    arxiv_bot.http_get = fake_http_get
    try:
        rows, _pages = scirate_weekly.fetch_scirate_candidates_api(
            "https://relay.invalid/{date}-{days}d.json",
            days,
            min_scites,
            target_date=target,
            limit=limit,
            require_pubdate=True,
            require_response_date=True,
            require_complete_snapshot=True,
            require_period_metadata=True,
            require_https=True,
        )
    except scirate_weekly.SciRateAPIError as exc:
        raise SystemExit(f"snapshot rejected by the bot's validator: {exc}")
    finally:
        arxiv_bot.http_get = original
    return days, len(rows), min_scites


def publish(repo: Path, snapshot: dict, days: int, dry_run: bool) -> Path:
    target = f"{snapshot['date']}-{days}d.json"
    out_dir = repo / RELAY_DIR
    out_path = out_dir / target
    if dry_run:
        return out_path
    if not (repo / ".git").is_dir():
        raise SystemExit(
            f"{repo} is not a git clone; pass --repo or clone the snapshot "
            "repository there first")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True)
        return done.stdout.strip()

    git("add", str(out_path.relative_to(repo)))
    if not git("diff", "--cached", "--name-only"):
        print("[info] snapshot unchanged; nothing to commit")
        return out_path
    git("commit", "-m", f"snapshot {snapshot['date']} range={days}d")
    git("push")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="read the snapshot from a file instead of "
                                   "the clipboard")
    ap.add_argument("--repo", default=str(DEFAULT_REPO),
                    help=f"snapshot repository clone (default: {DEFAULT_REPO})")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate only; do not write, commit, or push")
    args = ap.parse_args()

    payload = read_payload(args)
    try:
        snapshot = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "clipboard does not contain JSON. Run the bookmarklet on a "
            f"SciRate page first ({exc}).")
    if not isinstance(snapshot, dict):
        raise SystemExit("snapshot must be a JSON object")

    cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
    days, accepted, min_scites = validate(snapshot, cfg)

    label = "daily" if days == 1 else f"{days}-day"
    print(f"[ok] {label} snapshot {snapshot['period_start']}..{snapshot['date']}: "
          f"{len(snapshot.get('papers', []))} rows, "
          f"{accepted} at/above {min_scites} scites")

    out_path = publish(Path(args.repo).expanduser(), snapshot, days,
                       args.dry_run)
    if args.dry_run:
        print(f"[dry-run] would publish {out_path}")
    else:
        print(f"[ok] published {out_path}")


if __name__ == "__main__":
    main()
