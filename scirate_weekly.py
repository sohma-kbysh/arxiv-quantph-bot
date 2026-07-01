#!/usr/bin/env python3
"""
Weekly SciRate -> Discord notifier.

Fetches SciRate's quant-ph weekly page, selects papers with at least
N "Scite!" votes, reuses prior classifications from posted_log.json when
available, and otherwise classifies/translates them through arxiv_bot.py's
existing pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import arxiv_bot


BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "scirate_weekly_state.json"
SCIRATE_URL = "https://scirate.com/arxiv/quant-ph?range={days}"
ARXIV_API_URL = "https://export.arxiv.org/api/query"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def fetch_text(url: str, timeout: int = 60) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; arxiv-quantph-discord-bot/1.0; "
            "+https://github.com/sohma-kbysh/arxiv-quantph-bot)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_scirate_candidates(html_text: str, min_scites: int) -> list[dict]:
    parser = TextExtractor()
    parser.feed(html_text)
    text = parser.text()
    chunks = re.split(r"\barXiv:", text)
    candidates: list[dict] = []
    seen: set[str] = set()
    for chunk in chunks[1:]:
        id_match = re.match(r"\s*(\d{4}\.\d{4,5})(?:v\d+)?", chunk)
        if not id_match:
            continue
        arxiv_id = id_match.group(1)
        if arxiv_id in seen:
            continue
        score_match = re.search(r"\bScited\s+Scite!\s+(\d+)\b", chunk)
        if not score_match:
            continue
        scites = int(score_match.group(1))
        if scites < min_scites:
            continue
        candidates.append({"id": arxiv_id, "scites": scites})
        seen.add(arxiv_id)
    candidates.sort(key=lambda p: (-p["scites"], p["id"]))
    return candidates


def fetch_arxiv_metadata(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    query = urllib.parse.urlencode({
        "id_list": ",".join(ids),
        "max_results": str(len(ids)),
    })
    url = f"{ARXIV_API_URL}?{query}"
    try:
        raw = arxiv_bot.http_get(url, timeout=60)
    except urllib.error.HTTPError as exc:
        print(f"[warn] arXiv API HTTP {exc.code}; skipping metadata fetch")
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] arXiv API fetch failed: {exc}; skipping metadata fetch")
        return {}
    root = ET.fromstring(raw)
    papers: dict[str, dict] = {}
    for entry in root.findall("atom:entry", ATOM_NS):
        raw_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        arxiv_id = raw_id.rstrip("/").rsplit("/", 1)[-1].split("v", 1)[0]
        title = re.sub(
            r"\s+", " ",
            entry.findtext("atom:title", default="", namespaces=ATOM_NS),
        ).strip()
        abstract = re.sub(
            r"\s+", " ",
            entry.findtext("atom:summary", default="", namespaces=ATOM_NS),
        ).strip()
        authors = [
            a.findtext("atom:name", default="", namespaces=ATOM_NS).strip()
            for a in entry.findall("atom:author", ATOM_NS)
        ]
        categories = [
            c.attrib.get("term", "")
            for c in entry.findall("atom:category", ATOM_NS)
            if c.attrib.get("term")
        ]
        primary_node = entry.find("arxiv:primary_category", {
            "arxiv": "http://arxiv.org/schemas/atom",
        })
        primary = (
            primary_node.attrib.get("term")
            if primary_node is not None else (categories[0] if categories else "quant-ph")
        )
        papers[arxiv_id] = {
            "id": arxiv_id,
            "title": title,
            "link": f"https://arxiv.org/abs/{arxiv_id}",
            "authors": ", ".join(a for a in authors if a),
            "announce_type": "scirate weekly",
            "categories": categories,
            "primary": primary,
            "abstract": abstract,
        }
    return papers


def log_index(log: list[dict]) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for item in log:
        if item.get("id"):
            indexed[item["id"]] = item
    return indexed


def genres_from_log(item: dict, genres: list[dict]) -> list[dict]:
    genre_map = {g["id"]: g for g in genres}
    result = [genre_map[gid] for gid in item.get("genre_ids", []) if gid in genre_map]
    return result


def classify_entries(entries: list[dict], cfg: dict, genres: list[dict],
                     dry_run: bool) -> tuple[int, int, int]:
    if not entries:
        return 0, 0, 0
    batch_size = max(1, cfg.get("translate_batch_size", 5))
    genre_map = {g["id"]: g for g in genres}
    attempted = classified = 0
    if (
        cfg.get("classify_with_llm", True)
        and os.environ.get("GEMINI_API_KEY")
        and not dry_run
    ):
        for i in range(0, len(entries), batch_size):
            chunk = entries[i: i + batch_size]
            limit = cfg.get("max_translate_chars", 2000)
            payloads = [
                f"Title: {e['paper']['title']}\n\nAbstract: "
                f"{e['paper']['abstract'][:limit]}"
                for e in chunk
            ]
            attempted += len(chunk)
            gid_lists = arxiv_bot.classify_gemini_batch(payloads, cfg, genres)
            for e, gids in zip(chunk, gid_lists):
                if gids:
                    gs = [genre_map[g] for g in gids if g in genre_map]
                    e["genres"] = arxiv_bot.postprocess_genres(
                        e["paper"], gs, genres, cfg)
                    e["classified_by"] = "gemini"
                    classified += 1
    fallback = 0
    for e in entries:
        if e.get("genres"):
            continue
        e["genres"] = arxiv_bot.classify_multi(e["paper"], genres, cfg)
        e["classified_by"] = "tfidf"
        fallback += 1
    return attempted, classified, fallback


def translate_entries(entries: list[dict], cfg: dict) -> None:
    batch_size = max(1, cfg.get("translate_batch_size", 5))
    to_abstract = [
        e for e in entries
        if e["paper"].get("abstract") and e.get("jp") is None
    ]
    for i in range(0, len(to_abstract), batch_size):
        chunk = to_abstract[i: i + batch_size]
        abstracts = [e["paper"]["abstract"] for e in chunk]
        for e, jp in zip(chunk, arxiv_bot.translate_batch(abstracts, cfg)):
            e["jp"] = jp

    if cfg.get("show_japanese_title", True):
        to_title = [
            e for e in entries
            if e["paper"].get("title") and e.get("jp_title") is None
            and not (
                cfg.get("require_translation", True)
                and e["paper"].get("abstract") and e.get("jp") is None
            )
        ]
        for i in range(0, len(to_title), batch_size):
            chunk = to_title[i: i + batch_size]
            titles = [e["paper"]["title"] for e in chunk]
            for e, jp_title in zip(chunk, arxiv_bot.translate_batch(titles, cfg)):
                e["jp_title"] = jp_title


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--html-file", help="Use a local SciRate HTML file")
    args = parser.parse_args()

    cfg = load_json(arxiv_bot.CONFIG_PATH, {})
    genres = cfg.get("genres", [])
    min_scites = int(cfg.get("scirate_min_scites", 30))
    range_days = int(cfg.get("scirate_range_days", 7))
    url = cfg.get("scirate_url", SCIRATE_URL.format(days=range_days))

    state = load_json(STATE_PATH, {"posted": {}})
    weekly_seen = set(state.get("posted", {}).get(str(range_days), []))
    log: list[dict] = load_json(arxiv_bot.LOG_PATH, [])
    previous = log_index(log)

    try:
        html_text = (
            Path(args.html_file).read_text(encoding="utf-8")
            if args.html_file else fetch_text(url)
        )
    except urllib.error.HTTPError as exc:
        print(f"[warn] SciRate HTTP {exc.code}; skipping weekly digest")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] SciRate fetch failed: {exc}; skipping weekly digest")
        return

    candidates = parse_scirate_candidates(html_text, min_scites)
    candidates = [c for c in candidates if c["id"] not in weekly_seen]
    metadata = fetch_arxiv_metadata([c["id"] for c in candidates])

    entries: list[dict] = []
    reused = 0
    for cand in candidates:
        paper = metadata.get(cand["id"])
        if not paper:
            continue
        paper["announce_type"] = f"scirate weekly · {cand['scites']} Scites"
        prev = previous.get(cand["id"], {})
        genre_list = genres_from_log(prev, genres)
        if genre_list:
            genre_list = arxiv_bot.postprocess_genres(paper, genre_list, genres, cfg)
            reused += 1
        entries.append({
            "paper": paper,
            "scites": cand["scites"],
            "genres": genre_list,
            "jp": prev.get("abstract_ja"),
            "jp_title": prev.get("title_ja"),
            "classified_by": "posted_log" if genre_list else None,
        })

    to_classify = [e for e in entries if not e.get("genres")]
    attempted, classified, fallback = classify_entries(
        to_classify, cfg, genres, args.dry_run)

    print(
        "[info] SciRate weekly: "
        f"url={url}, min_scites={min_scites}, "
        f"candidates={len(candidates)}, postable={len(entries)}, "
        f"reused_classification={reused}, "
        f"gemini_classified={classified}/{attempted}, "
        f"tfidf_fallback={fallback}"
    )

    if args.dry_run:
        for e in entries:
            labels = ", ".join(g["name"] for g in e["genres"])
            print(f"[{e['scites']:>3} Scites] {labels} | {e['paper']['title']}")
            print(f"      {e['paper']['link']}")
        return

    translate_entries(entries, cfg)

    require_translation = cfg.get("require_translation", True)
    posted = deferred = 0
    posted_ids: set[str] = set()
    for e in entries:
        if require_translation and e["paper"].get("abstract") and e.get("jp") is None:
            deferred += 1
            continue
        posted_webhooks: set[str] = set()
        for genre in e["genres"]:
            webhook, genre_name = arxiv_bot.resolve_webhook(genre)
            if not webhook or webhook in posted_webhooks:
                continue
            fields = [{
                "name": "SciRate",
                "value": f"{e['scites']} Scites in the past {range_days} days",
            }]
            if arxiv_bot.post_to_discord(
                webhook, e["paper"], genre_name, e.get("jp"),
                e.get("jp_title"), cfg, extra_fields=fields,
            ):
                posted_webhooks.add(webhook)
                posted += 1
                posted_ids.add(e["paper"]["id"])
            time.sleep(1.2)

        if e["paper"]["id"] in posted_ids:
            log.append({
                "id": e["paper"]["id"],
                "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": "scirate_weekly",
                "scirate_scites": e["scites"],
                "title": e["paper"]["title"],
                "title_ja": e.get("jp_title"),
                "authors": e["paper"]["authors"],
                "link": e["paper"]["link"],
                "primary": e["paper"]["primary"],
                "announce_type": e["paper"]["announce_type"],
                "genre_ids": [g["id"] for g in e["genres"] if g],
                "genre_names": [g["name"] for g in e["genres"] if g],
                "abstract_en": e["paper"]["abstract"],
                "abstract_ja": e.get("jp"),
            })

    state.setdefault("posted", {})
    prior = set(state["posted"].get(str(range_days), []))
    state["posted"][str(range_days)] = sorted((prior | posted_ids))[-1000:]
    write_json(STATE_PATH, state)
    write_json(arxiv_bot.LOG_PATH, log[-5000:])
    print(
        f"posted {posted} SciRate weekly posts "
        f"({len(candidates)} candidates, {deferred} deferred for retry)"
    )


if __name__ == "__main__":
    main()
