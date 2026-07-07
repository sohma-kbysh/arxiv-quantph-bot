#!/usr/bin/env python3
"""Post already-published papers to genre channels they should have reached.

Reads a plan JSON: a list of {"id": arXiv id, "channels": [genre ids to post
to now], "genres_after": [full corrected genre id list]}. Reuses the stored
translation from posted_log.json (no translation API calls), posts one embed
per missing channel, and rewrites the log entry's genre ids to the corrected
list so future runs (SciRate weekly, audits) see the fixed classification.

Never touches seen_ids.json. Intentionally does NOT fall back to
DISCORD_WEBHOOK_GENERAL: a channel whose webhook secret is missing is
skipped and reported instead of spamming the general channel.

Usage:
    python3 scripts/repost_missing_channels.py --plan repost_plan.json --dry-run
    python3 scripts/repost_missing_channels.py --plan repost_plan.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arxiv_bot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post papers to channels missed by the original run.")
    parser.add_argument("--plan", default="repost_plan.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
    genres = cfg.get("genres", [])
    genre_map = {g["id"]: g for g in genres}
    log = arxiv_bot.load_json(arxiv_bot.LOG_PATH, [])
    by_id: dict[str, dict] = {}
    for row in log:
        if row.get("id"):
            by_id[row["id"]] = row  # last occurrence wins

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    print(f"[repost] plan={args.plan} papers={len(plan)} "
          f"posts={sum(len(p.get('channels', [])) for p in plan)} "
          f"dry_run={args.dry_run}")

    posted = 0
    posted_records: list[dict] = []
    failed_records: list[dict] = []
    for item in plan:
        pid = item.get("id", "")
        row = by_id.get(pid)
        if not row:
            print(f"[warn] {pid}: not found in posted_log.json; skipped",
                  file=sys.stderr)
            failed_records.append(
                {"id": pid, "title": pid, "genre_names": ["ログ未発見"]})
            continue
        jp = arxiv_bot.log_abstract_translation(row)
        jp_title = arxiv_bot.log_title_translation(row)
        after_ids = [g for g in (item.get("genres_after")
                                 or row.get("genre_ids") or [])
                     if g in genre_map]
        label = ", ".join(genre_map[g]["name"] for g in after_ids)
        paper = {
            "id": pid,
            "title": row.get("title", ""),
            "link": row.get("link", ""),
            "authors": row.get("authors", ""),
            "primary": row.get("primary", ""),
            "announce_type": row.get("announce_type", "new"),
            "abstract": row.get("abstract_en", ""),
        }
        ok_channels: list[str] = []
        ng_channels: list[str] = []
        for gid in item.get("channels", []):
            genre = genre_map.get(gid)
            if not genre:
                print(f"[warn] {pid}: unknown genre '{gid}'; skipped",
                      file=sys.stderr)
                continue
            webhook = os.environ.get(genre.get("webhook_env", ""), "")
            if not webhook:
                print(f"[warn] {pid}: no webhook for {gid} "
                      f"({genre.get('webhook_env')}); skipped",
                      file=sys.stderr)
                ng_channels.append(f"{genre['name']}(webhook未設定)")
                continue
            if args.dry_run:
                print(f"[dry-run] {pid} -> {genre['name']} "
                      f"(footer: {label or genre['name']})")
                ok_channels.append(genre["name"])
                continue
            if arxiv_bot.post_to_discord(
                    webhook, paper, label or genre["name"], jp, jp_title, cfg):
                posted += 1
                ok_channels.append(genre["name"])
            else:
                ng_channels.append(genre["name"])
            time.sleep(1.2)

        record = {"id": pid, "title": jp_title or row.get("title", pid),
                  "link": row.get("link", "")}
        if ok_channels:
            posted_records.append({**record, "genre_names": ok_channels})
            if not args.dry_run:
                row["genre_ids"] = after_ids
                row["genre_names"] = [genre_map[g]["name"]
                                      for g in after_ids]
                row.setdefault("repost_channels", []).extend(ok_channels)
                row["reposted_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if ng_channels:
            failed_records.append({**record, "genre_names": ng_channels})

    if not args.dry_run:
        arxiv_bot.LOG_PATH.write_text(
            json.dumps(log[-5000:], indent=1, ensure_ascii=False),
            encoding="utf-8")
        arxiv_bot.notify_run_report({
            "source": "分類修正の追い投稿",
            "fetched": len(plan),
            "candidates": len(plan),
            "messages": posted,
            "posted": posted_records,
            "deferred": [],
            "failed": failed_records,
            "classifier_counts": {"監査での再分類": len(plan)},
            "translated": {"posted_log再利用": len(posted_records)},
        }, cfg)

    print(f"[repost] done: {posted} posts to "
          f"{len(posted_records)} papers' missing channels, "
          f"{len(failed_records)} with problems")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
