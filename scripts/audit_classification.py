#!/usr/bin/env python3
"""Re-run Gemini classification for already-posted papers and show diffs only."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import arxiv_bot  # noqa: E402


def local_date(posted_at: str, timezone_name: str) -> str:
    dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def old_genre_ids(row: dict) -> list[str]:
    ids = row.get("genre_ids")
    if isinstance(ids, list) and ids:
        return [str(g) for g in ids if g]
    gid = row.get("genre_id")
    return [str(gid)] if gid else []


def genre_names(ids: list[str], genre_map: dict[str, dict]) -> str:
    labels = []
    for gid in ids:
        genre = genre_map.get(gid)
        labels.append(genre.get("name", gid) if genre else gid)
    return ", ".join(labels) if labels else "other"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-run Gemini classification for posted_log entries.")
    parser.add_argument("--date", required=True,
                        help="Local date to audit, e.g. 2026-07-03.")
    parser.add_argument("--timezone", default="Asia/Tokyo")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--model", action="append", default=None,
                        help="Gemini model chain override (repeatable). "
                             "Default: gemini_model_primary, then "
                             "gemini_model_secondary as fallback.")
    args = parser.parse_args()

    cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
    log = arxiv_bot.load_json(arxiv_bot.LOG_PATH, [])
    genres = cfg.get("genres", [])
    genre_map = {g["id"]: g for g in genres}
    batch_size = max(1, args.batch_size or cfg.get("translate_batch_size", 5))
    limit = cfg.get("max_translate_chars", 2000)

    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set.", file=sys.stderr)
        return 2

    rows = [
        row for row in log
        if row.get("posted_at")
        and local_date(row["posted_at"], args.timezone) == args.date
    ]
    print(f"[audit] date={args.date} timezone={args.timezone} papers={len(rows)}")
    if not rows:
        return 0

    chain = args.model or [m for m in (
        cfg.get("gemini_model_primary"),
        cfg.get("gemini_model_secondary"),
    ) if m] or [cfg.get("gemini_model", "gemini-2.5-flash")]
    seen_models: set[str] = set()
    chain = [m for m in chain if not (m in seen_models or seen_models.add(m))]
    print(f"[audit] model_chain={chain}")

    diffs: list[tuple[dict, list[str], list[str]]] = []
    unclassified: list[dict] = []
    attempted = 0
    classified = 0
    model_counts: dict[str, int] = {}

    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        texts = [
            f"Title: {row.get('title', '')}\n\n"
            f"Abstract: {row.get('abstract_en', '')[:limit]}"
            for row in chunk
        ]
        attempted += len(chunk)
        results: list[list[str]] = [[] for _ in chunk]
        for model in chain:
            if model in arxiv_bot._gemini_dead_models:
                continue
            todo = [j for j, r in enumerate(results) if not r]
            if not todo:
                break
            gid_lists = arxiv_bot.classify_gemini_batch(
                [texts[j] for j in todo], cfg, genres, model=model)
            for j, gids in zip(todo, gid_lists):
                if gids:
                    results[j] = gids
                    model_counts[model] = model_counts.get(model, 0) + 1
        for row, gids in zip(chunk, results):
            if not gids:
                unclassified.append(row)
                continue
            paper = {
                "id": row.get("id", ""),
                "title": row.get("title", ""),
                "abstract": row.get("abstract_en", ""),
                "primary": row.get("primary", ""),
                "categories": row.get("categories", []),
            }
            selected = [genre_map[g] for g in gids if g in genre_map]
            selected = selected or [arxiv_bot.genre_by_id(None, genres)]
            selected = arxiv_bot.postprocess_genres(paper, selected, genres, cfg)
            new_ids = [g["id"] for g in selected if g]
            classified += 1
            old_ids = old_genre_ids(row)
            if old_ids != new_ids:
                diffs.append((row, old_ids, new_ids))

    print(f"[audit] gemini_classified={classified}/{attempted} "
          f"by_model={model_counts}")
    print(f"[audit] differences={len(diffs)}")
    if unclassified:
        print("[audit] unclassified=" + ",".join(
            row.get("id", "") for row in unclassified))
    for row, before, after in diffs:
        print()
        print(row.get("id", ""))
        print(row.get("title", ""))
        print(f"before: {before} ({genre_names(before, genre_map)})")
        print(f"after : {after} ({genre_names(after, genre_map)})")
        print(row.get("link", ""))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
