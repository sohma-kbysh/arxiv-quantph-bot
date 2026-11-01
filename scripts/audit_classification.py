#!/usr/bin/env python3
"""Preview route-aware classification against past posts without delivering."""

from __future__ import annotations

import argparse
from collections import Counter
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
    return ", ".join(labels) if labels else "skip (投稿対象外)"


def paper_from_log(row: dict) -> dict:
    """Restore the acquisition context used by the production classifiers."""
    paper = {
        "id": row.get("id", ""),
        "title": row.get("title", ""),
        "abstract": row.get("abstract_en", ""),
        "primary": row.get("primary", ""),
        "categories": row.get("categories", []),
        "announce_type": row.get("announce_type", ""),
        "source_feed": row.get("source_feed", ""),
        "classification_context": row.get("classification_context", ""),
    }
    context = row.get("classification_context")
    if context == "external":
        paper["announce_type"] = "external"
    elif context == "quant-ph":
        paper["source_feed"] = "quant-ph"
    if paper["announce_type"] == "external" and not paper["source_feed"]:
        paper["source_feed"] = paper["primary"]
    return paper


def audit_rows(rows: list[dict], cfg: dict, genres: list[dict],
               chain: list[dict], batch_size: int) -> list[dict | None]:
    """Run the matching production prompt/policy; never load or save state."""
    papers = [paper_from_log(row) for row in rows]
    results: list[dict | None] = [None] * len(rows)
    normal = [i for i, paper in enumerate(papers)
              if arxiv_bot.is_quantph_feed_paper(paper)]
    normal_indices = set(normal)
    genre_map = {g["id"]: g for g in genres}
    limit = cfg.get("max_translate_chars", 2000)
    for offset in range(0, len(normal), batch_size):
        indices = normal[offset:offset + batch_size]
        for spec in chain:
            if (not arxiv_bot.classifier_key_present(spec)
                    or arxiv_bot.classifier_dead(spec)):
                continue
            todo = [i for i in indices if results[i] is None]
            if not todo:
                break
            texts = [
                f"Title: {papers[i]['title']}\n\n"
                f"Abstract: {papers[i]['abstract'][:limit]}" for i in todo
            ]
            outputs = arxiv_bot.classify_llm_batch(
                texts, cfg, genres, spec=spec)
            for i, gids in zip(todo, outputs):
                selected = [genre_map[g] for g in gids if g in genre_map]
                if selected:
                    selected = arxiv_bot.postprocess_genres(
                        papers[i], selected, genres, cfg)
                    results[i] = {
                        "genre_ids": [g["id"] for g in selected],
                        "classifier": arxiv_bot.classifier_spec_name(spec),
                    }

    external: dict[str, list[dict]] = {}
    external_indices: dict[str, list[int]] = {}
    for i, paper in enumerate(papers):
        if i in normal_indices:
            continue
        category = paper["source_feed"] or paper["primary"]
        rule = cfg.get("external_arxiv_queries", {}).get(category)
        if not isinstance(rule, dict):
            print(f"[warn] {paper['id']}: no external review rule for "
                  f"{category}; left unclassified", file=sys.stderr)
            continue
        external.setdefault(category, []).append(paper)
        external_indices.setdefault(category, []).append(i)
    if external:
        review_cfg = dict(cfg, translate_batch_size=batch_size)
        _, reviews, _ = arxiv_bot.review_external_candidates(
            external, review_cfg, genres, {}, model_specs=chain)
        for category, indices in external_indices.items():
            for i in indices:
                review = reviews.get(f"{category}:{papers[i]['id']}")
                if review is not None:
                    results[i] = review
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview classification against past posts using their "
                    "original acquisition route. No Discord posts or "
                    "production state changes; uses classifier API quota.")
    parser.add_argument("--date", required=True,
                        help="Local date to audit, e.g. 2026-07-03.")
    parser.add_argument("--timezone", default="Asia/Tokyo")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum papers to audit; 0 audits the whole day.")
    parser.add_argument("--model", action="append", default=None,
                        help="Gemini model chain override (repeatable). "
                             "Default: configured classifier chain.")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be 0 or greater")

    cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
    log = arxiv_bot.load_json(arxiv_bot.LOG_PATH, [])
    genres = cfg.get("genres", [])
    genre_map = {g["id"]: g for g in genres}
    batch_size = max(1, args.batch_size or cfg.get("translate_batch_size", 5))

    rows = [
        row for row in log
        if row.get("posted_at")
        and local_date(row["posted_at"], args.timezone) == args.date
    ]
    if args.limit:
        rows = rows[:args.limit]
    print(f"[audit] date={args.date} timezone={args.timezone} papers={len(rows)}")
    if not rows:
        print("[audit] No matching papers; audit was not performed.", file=sys.stderr)
        return 2

    if args.model:
        configured_specs = arxiv_bot.classifier_model_specs(cfg)
        chain = []
        for model in args.model:
            matched = next((
                spec for spec in configured_specs
                if model in {
                    arxiv_bot.classifier_spec_name(spec),
                    str(spec.get("model", "")),
                }
            ), None)
            chain.append(matched or {
                "provider": "gemini",
                "model": model,
                "name": model,
            })
    else:
        chain = arxiv_bot.classifier_model_specs(cfg)
    seen_models: set[tuple[str, str]] = set()
    chain = [
        m for m in chain
        if not (
            (str(m.get("provider", "gemini")), arxiv_bot.classifier_spec_name(m))
            in seen_models
            or seen_models.add(
                (str(m.get("provider", "gemini")),
                 arxiv_bot.classifier_spec_name(m)))
        )
    ]
    print("[audit] model_chain="
          f"{[arxiv_bot.classifier_spec_name(s) for s in chain]}")
    if not any(arxiv_bot.classifier_key_present(s) for s in chain):
        print("No classifier API key is set for the selected models.",
              file=sys.stderr)
        return 2

    diffs: list[tuple[dict, list[str], list[str]]] = []
    unclassified: list[dict] = []
    results = audit_rows(rows, cfg, genres, chain, batch_size)
    if len(results) != len(rows):
        print("[audit] INCOMPLETE: result count does not match selected papers.",
              file=sys.stderr)
        return 2
    model_counts: Counter = Counter()
    for row, result in zip(rows, results):
        if result is None:
            unclassified.append(row)
            continue
        model_counts[result["classifier"]] += 1
        new_ids = result["genre_ids"]
        old_ids = old_genre_ids(row)
        if set(old_ids) != set(new_ids):
            diffs.append((row, old_ids, new_ids))

    print(f"[audit] llm_classified={len(rows) - len(unclassified)}/{len(rows)} "
          f"by_model={dict(model_counts)}")
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

    if unclassified:
        print("[audit] INCOMPLETE: unclassified papers remain; "
              "differences are not a complete audit result.", file=sys.stderr)
        return 2
    print("[audit] COMPLETE: every selected paper has a decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
