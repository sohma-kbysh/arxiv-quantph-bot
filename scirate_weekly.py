#!/usr/bin/env python3
"""Daily and weekly SciRate digests for one dedicated Discord channel.

The daily digest posts an exact Top N for one arXiv/SciRate publication date.
The weekly digest reposts every paper in one completed seven-day window whose
Scite count reaches the configured threshold.  Both use one dedicated webhook;
SciRate content is never routed back into the normal genre channels.

Production always probes SciRate's anticipated JSON feed API first.  An
operator-provided JSON relay with the same papers schema can be used only when
that official request fails; the bot never falls back to HTML scraping.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import arxiv_bot


BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "scirate_weekly_state.json"
SCIRATE_API_URL_TEMPLATE = (
    "https://scirate.com/arxiv/quant-ph.json"
    "?date={date}&range={days}&page={page}"
)
ARXIV_API_URL = "https://export.arxiv.org/api/query"
JST = ZoneInfo("Asia/Tokyo")

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


class SciRateAPIError(RuntimeError):
    """The prospective SciRate JSON API is unavailable or incompatible."""


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path: Path, data: Any) -> None:
    arxiv_bot.atomic_write_json(path, data, ensure_ascii=False)


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


def scirate_api_page_url(
    template: str,
    days: int,
    page: int,
    target_date: str | None = None,
) -> str:
    """Expand the configurable future API URL without assuming its host."""
    try:
        url = template.format(
            days=days, page=page, date=target_date or "")
    except (KeyError, ValueError) as exc:
        raise SciRateAPIError(
            "scirate_api_url_template supports only {date}, {days}, and "
            "{page}: "
            f"{exc}") from exc
    if target_date and "{date}" not in template:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != "date"]
        query.append(("date", target_date))
        url = urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        ))
    return url


def parse_scirate_json_page(payload: Any) -> tuple[list[dict], int]:
    """Parse the schema proposed by scirate/scirate#535 defensively.

    The accepted aliases make an endpoint rename deployable through a repo
    variable rather than a code release, while still rejecting an HTML page or
    an unrelated JSON document instead of silently treating it as no papers.
    """
    rows = payload.get("papers") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise SciRateAPIError("SciRate JSON response has no papers array")

    candidates: list[dict] = []
    valid_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("uid") or row.get("arxiv_id") or row.get("id")
        raw_score = (
            row.get("scites_count")
            if row.get("scites_count") is not None
            else row.get("scites", row.get("score"))
        )
        if raw_id is None or raw_score is None:
            continue
        arxiv_id = str(raw_id).strip()
        arxiv_id = re.sub(r"^https?://arxiv\.org/abs/", "", arxiv_id)
        arxiv_id = re.sub(r"^arXiv:", "", arxiv_id, flags=re.I)
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        try:
            scites = int(raw_score)
        except (TypeError, ValueError):
            continue
        if not arxiv_id or scites < 0:
            continue
        valid_rows += 1
        candidates.append({
            "id": arxiv_id,
            "scites": scites,
            "pubdate": str(row.get("pubdate") or "").strip(),
            "submit_date": str(row.get("submit_date") or "").strip(),
        })

    if rows and valid_rows == 0:
        raise SciRateAPIError(
            "SciRate papers array has no rows with an id and scites_count")
    if valid_rows != len(rows):
        raise SciRateAPIError(
            "SciRate papers array contains malformed rows; refusing a "
            "potentially incomplete digest")
    return candidates, len(rows)


def fetch_scirate_candidates_api(
    template: str,
    days: int,
    min_scites: int,
    *,
    max_pages: int = 20,
    page_size: int = 50,
    timeout: int = 60,
    target_date: str | None = None,
    limit: int | None = None,
    require_pubdate: bool = False,
    require_response_date: bool = False,
    bearer_token: str = "",
    require_complete_snapshot: bool = False,
    require_period_metadata: bool = False,
    require_https: bool = False,
) -> tuple[list[dict], int]:
    """Fetch every relevant JSON page, stopping below the score threshold."""
    if not template:
        raise SciRateAPIError("SciRate JSON API URL is not configured")
    max_pages = max(1, int(max_pages))
    page_size = max(1, int(page_size))
    ordered: list[dict] = []
    by_id: dict[str, dict] = {}
    pages_fetched = 0
    previous_score: int | None = None

    for page in range(1, max_pages + 1):
        url = scirate_api_page_url(
            template, days, page, target_date=target_date)
        parsed_url = urllib.parse.urlsplit(url)
        if require_https and (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise SciRateAPIError(
                "trusted SciRate relay URL must be credential-free HTTPS")
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "arxiv-quantph-discord-bot/1.0 "
                "(+https://github.com/sohma-kbysh/arxiv-quantph-bot; "
                "SciRate JSON digest client)"
            ),
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        try:
            if require_https or bearer_token:
                raw = arxiv_bot.http_get(
                    url, timeout=timeout, headers=headers,
                    allow_redirects=False)
            else:
                raw = arxiv_bot.http_get(
                    url, timeout=timeout, headers=headers)
        except SciRateAPIError:
            raise
        except urllib.error.HTTPError as exc:
            raise SciRateAPIError(f"SciRate JSON API HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SciRateAPIError(
                f"SciRate JSON API request failed: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            arxiv_bot.save_error_diagnostic(
                "invalid_response", url=url, method="GET", status=200,
                body=raw, exception=exc)
            raise SciRateAPIError(
                "SciRate JSON endpoint returned non-JSON content") from exc

        page_rows, row_count = parse_scirate_json_page(payload)
        pages_fetched += 1
        snapshot_complete = bool(
            isinstance(payload, dict) and payload.get("complete") is True)
        if require_complete_snapshot and not snapshot_complete:
            raise SciRateAPIError(
                "trusted SciRate relay snapshot must declare complete=true")
        if target_date:
            response_date = (
                str(payload.get("date") or "")
                if isinstance(payload, dict) else ""
            )
            if require_response_date and not response_date:
                raise SciRateAPIError(
                    "SciRate JSON response has no date; requested period "
                    "cannot be verified")
            if response_date and response_date[:10] != target_date:
                raise SciRateAPIError(
                    "SciRate JSON response date does not match requested "
                    f"date {target_date}: {response_date}")
            if require_period_metadata:
                target_day = date.fromisoformat(target_date)
                expected_start = (
                    target_day - timedelta(days=max(1, days) - 1)
                ).isoformat()
                raw_range = payload.get("range_days")
                raw_start = str(payload.get("period_start") or "")
                raw_end = str(payload.get("period_end") or "")
                try:
                    snapshot_days = int(raw_range)
                except (TypeError, ValueError):
                    snapshot_days = -1
                if (
                    snapshot_days != days
                    or raw_start[:10] != expected_start
                    or raw_end[:10] != target_date
                ):
                    raise SciRateAPIError(
                        "trusted SciRate relay period metadata does not "
                        "match requested period "
                        f"{expected_start}..{target_date} (days={days})")
        if require_pubdate:
            missing_dates = [row["id"] for row in page_rows if not row["pubdate"]]
            if missing_dates:
                raise SciRateAPIError(
                    "SciRate JSON rows have no pubdate; exact daily batch "
                    "selection is unsafe")
        if target_date and require_pubdate:
            target_day = date.fromisoformat(target_date)
            first_day = target_day - timedelta(days=max(1, days) - 1)
            wrong_dates: list[dict] = []
            for row in page_rows:
                try:
                    row_day = date.fromisoformat(row["pubdate"][:10])
                except (TypeError, ValueError):
                    wrong_dates.append(row)
                    continue
                if not first_day <= row_day <= target_day:
                    wrong_dates.append(row)
            if wrong_dates:
                label = "daily batch" if days == 1 else "requested period"
                raise SciRateAPIError(
                    "SciRate JSON returned papers outside the requested "
                    f"{label} {first_day.isoformat()}..{target_date}")
        scores = [row["scites"] for row in page_rows]
        if any(left < right for left, right in zip(scores, scores[1:])):
            raise SciRateAPIError(
                "SciRate JSON papers are not sorted by scites_count "
                "descending; threshold pagination is unsafe")
        if scores and previous_score is not None and scores[0] > previous_score:
            raise SciRateAPIError(
                "SciRate JSON page order is not globally descending; "
                "threshold pagination is unsafe")
        if scores:
            previous_score = scores[-1]
        for candidate in page_rows:
            if candidate["id"] in by_id:
                raise SciRateAPIError(
                    "SciRate JSON contains duplicate paper id "
                    f"{candidate['id']}; snapshot completeness is ambiguous")
            if candidate["scites"] >= min_scites:
                by_id[candidate["id"]] = candidate
                ordered.append(candidate)
            else:
                # Keep below-threshold IDs too so a duplicate on a later page
                # cannot be hidden merely by crossing the score boundary.
                by_id[candidate["id"]] = candidate

        # SciRate's response is already deterministically ordered by score,
        # comments, dates, and arXiv id.  Once Top N is present, later pages
        # cannot change that ranking.
        if limit is not None and len(ordered) >= max(0, int(limit)):
            break

        # A trusted relay may publish a normalized, complete snapshot rather
        # than emulating SciRate's pagination.  This marker is deliberately
        # ignored for the official endpoint and is trusted only by the relay
        # call site below.
        if require_complete_snapshot and snapshot_complete:
            break

        # The feed is sorted by scites_count descending in SciRate. Once a
        # complete page crosses the threshold, later pages cannot qualify.
        if row_count == 0 or row_count < page_size:
            break
        if scores and min(scores) < min_scites:
            break
    else:
        raise SciRateAPIError(
            f"SciRate JSON API exceeded max_pages={max_pages} before a "
            "complete threshold boundary; refusing a partial digest")

    candidates = ordered
    if limit is not None:
        candidates = candidates[:max(0, int(limit))]
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates, pages_fetched


def fetch_scirate_candidates_with_fallback(
    official_template: str,
    relay_template: str,
    days: int,
    min_scites: int,
    *,
    max_pages: int = 20,
    page_size: int = 50,
    timeout: int = 60,
    target_date: str | None = None,
    limit: int | None = None,
    require_pubdate: bool = False,
    relay_bearer_token: str = "",
) -> tuple[list[dict], int, str, str]:
    """Fetch official SciRate JSON first, then an optional JSON relay.

    The final string is the official failure message when the relay was used.
    Keeping it separate from the successful relay result makes the fallback
    visible in state without ever persisting the relay credential.
    """
    common = {
        "max_pages": max_pages,
        "page_size": page_size,
        "timeout": timeout,
        "target_date": target_date,
        "limit": limit,
        "require_pubdate": require_pubdate,
        "require_response_date": bool(target_date),
    }
    try:
        candidates, pages = fetch_scirate_candidates_api(
            official_template, days, min_scites, **common)
    except SciRateAPIError as official_error:
        if not relay_template:
            raise
        try:
            candidates, pages = fetch_scirate_candidates_api(
                relay_template,
                days,
                min_scites,
                **common,
                bearer_token=relay_bearer_token,
                require_complete_snapshot=True,
                require_period_metadata=True,
                require_https=True,
            )
        except SciRateAPIError as relay_error:
            raise SciRateAPIError(
                "official SciRate JSON failed: "
                f"{official_error}; configured relay failed: {relay_error}"
            ) from relay_error
        return candidates, pages, "relay", str(official_error)
    return candidates, pages, "official", ""


def fetch_arxiv_metadata(ids: list[str], batch_size: int = 50) -> dict[str, dict]:
    if not ids:
        return {}
    papers: dict[str, dict] = {}
    batch_size = max(1, int(batch_size))
    for offset in range(0, len(ids), batch_size):
        chunk = ids[offset:offset + batch_size]
        query = urllib.parse.urlencode({
            "id_list": ",".join(chunk),
            "max_results": str(len(chunk)),
        })
        url = f"{ARXIV_API_URL}?{query}"
        try:
            raw = arxiv_bot.http_get(url, timeout=60)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"arXiv API HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"arXiv API fetch failed: {exc}") from exc
        root = ET.fromstring(raw)
        for entry in root.findall("atom:entry", ATOM_NS):
            raw_id = entry.findtext(
                "atom:id", default="", namespaces=ATOM_NS)
            arxiv_id = re.sub(
                r"v\d+$", "", raw_id.rstrip("/").rsplit("/", 1)[-1])
            title = re.sub(
                r"\s+", " ",
                entry.findtext(
                    "atom:title", default="", namespaces=ATOM_NS),
            ).strip()
            abstract = re.sub(
                r"\s+", " ",
                entry.findtext(
                    "atom:summary", default="", namespaces=ATOM_NS),
            ).strip()
            authors = [
                a.findtext(
                    "atom:name", default="", namespaces=ATOM_NS).strip()
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
                if primary_node is not None
                else (categories[0] if categories else "quant-ph")
            )
            papers[arxiv_id] = {
                "id": arxiv_id,
                "title": title,
                "link": f"https://arxiv.org/abs/{arxiv_id}",
                "authors": ", ".join(a for a in authors if a),
                "announce_type": "scirate",
                "categories": categories,
                "primary": primary,
                "abstract": abstract,
            }
        if offset + batch_size < len(ids):
            time.sleep(3)
    return papers


def log_index(log: list[dict]) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for item in log:
        if item.get("id"):
            indexed[item["id"]] = item
    return indexed


def translate_entries(
    entries: list[dict], cfg: dict, *, include_abstract: bool
) -> None:
    batch_size = max(1, cfg.get("translate_batch_size", 5))
    if include_abstract:
        to_abstract = [
            e for e in entries
            if e["paper"].get("abstract") and e.get("jp") is None
        ]
        for i in range(0, len(to_abstract), batch_size):
            chunk = to_abstract[i: i + batch_size]
            abstracts = [e["paper"]["abstract"] for e in chunk]
            for e, jp in zip(chunk, arxiv_bot.translate_batch(abstracts, cfg)):
                e["jp"] = jp

    if arxiv_bot.show_translated_title(cfg):
        to_title = [
            e for e in entries
            if e["paper"].get("title") and e.get("jp_title") is None
            and not (
                include_abstract
                and cfg.get("require_translation", True)
                and e["paper"].get("abstract") and e.get("jp") is None
            )
        ]
        for i in range(0, len(to_title), batch_size):
            chunk = to_title[i: i + batch_size]
            titles = [e["paper"]["title"] for e in chunk]
            for e, jp_title in zip(chunk, arxiv_bot.translate_batch(titles, cfg)):
                e["jp_title"] = jp_title


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def resolve_mode(raw_mode: str, target: date) -> str:
    """Resolve scheduled `auto` runs from the JST calendar."""
    if raw_mode in {"daily", "weekly"}:
        return raw_mode
    if raw_mode != "auto":
        raise ValueError(f"unknown SciRate digest mode: {raw_mode}")
    if target.weekday() == 6:
        return "weekly"
    if target.weekday() < 5:
        return "daily"
    return "skip"


def period_details(mode: str, target: date) -> tuple[str, str, int]:
    if mode == "daily":
        value = target.isoformat()
        return value, value, 1
    start = target - timedelta(days=6)
    key = f"{start.isoformat()}_{target.isoformat()}"
    return key, f"{start.isoformat()}〜{target.isoformat()}", 7


def ordered_discovery_targets(
    pending: dict,
    period_key: str,
    target: date,
    backlog_max_periods: int,
) -> list[tuple[str, date]]:
    """Put the due period first, then fairly rotate pending backlog."""
    backlog: list[tuple[str, date, str]] = []
    for pending_key, record in pending.items():
        if pending_key == period_key or not isinstance(record, dict):
            continue
        try:
            pending_target = date.fromisoformat(str(record.get("target_date")))
        except ValueError:
            continue
        last_attempt = str(record.get("last_attempt_at") or "")
        backlog.append((pending_key, pending_target, last_attempt))
    # Never-attempted periods sort first. Afterwards, retry the least recently
    # attempted periods so a permanently broken snapshot cannot monopolize the
    # bounded backlog budget forever.
    backlog.sort(key=lambda item: (
        bool(item[2]), item[2], item[1], item[0]))
    return [
        (period_key, target),
        *[
            (pending_key, pending_target)
            for pending_key, pending_target, _last_attempt in
            backlog[:max(0, backlog_max_periods)]
        ],
    ]


def normalize_state(raw: Any) -> dict:
    """Add the v2 digest stores without destroying legacy weekly history."""
    state = raw if isinstance(raw, dict) else {}
    state["schema_version"] = 2
    for key in (
        "source_status", "daily_posted", "weekly_posted",
        "daily_deliveries", "weekly_deliveries", "pending_discovery",
    ):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    for mode in ("daily", "weekly"):
        if not isinstance(state["pending_discovery"].get(mode), dict):
            state["pending_discovery"][mode] = {}
    legacy_all = state.get("deliveries", {})
    legacy = legacy_all.get("7", {}) if isinstance(legacy_all, dict) else {}
    if isinstance(legacy, dict) and legacy:
        migrated = state["weekly_deliveries"].setdefault("legacy", {})
        for pid, record in legacy.items():
            if not isinstance(record, dict) or not isinstance(
                    record.get("paper"), dict):
                continue
            migrated.setdefault(pid, {
                "paper": record["paper"],
                "scites": int(record.get("scites", 0)),
                "rank": 0,
                "abstract_translated": record.get("abstract_translated"),
                "title_translated": record.get("title_translated"),
                "genre_names": [],
                "status": "pending",
                "queued_at": record.get("queued_at") or _now_utc(),
            })
        legacy.clear()
    return state


def genre_names_from_log(item: dict, genre_map: dict[str, dict]) -> list[str]:
    names = [str(name) for name in item.get("genre_names", []) if name]
    if names:
        return names
    return [
        genre_map[gid]["name"]
        for gid in item.get("genre_ids", [])
        if gid in genre_map
    ]


def entry_from_candidate(
    candidate: dict,
    paper: dict,
    previous: dict,
    cfg: dict,
    genre_map: dict[str, dict],
    mode: str,
) -> dict:
    reusable = previous if arxiv_bot.translation_log_matches(
        previous, cfg) else {}
    genre_names = genre_names_from_log(previous, genre_map)
    paper["announce_type"] = f"scirate {mode}"
    return {
        "paper": paper,
        "scites": int(candidate["scites"]),
        "rank": int(candidate.get("rank", 0)),
        "jp": arxiv_bot.log_abstract_translation(reusable),
        "jp_title": arxiv_bot.log_title_translation(reusable),
        "genre_names": genre_names,
    }


def delivery_from_entry(entry: dict, old: dict | None = None) -> dict:
    prior = old if isinstance(old, dict) else {}
    return {
        "paper": entry["paper"],
        "scites": int(entry["scites"]),
        "rank": int(entry.get("rank", 0)),
        "abstract_translated": entry.get("jp"),
        "title_translated": entry.get("jp_title"),
        "genre_names": list(entry.get("genre_names", [])),
        "status": prior.get("status", "pending"),
        "queued_at": prior.get("queued_at") or _now_utc(),
    }


def entry_from_delivery(delivery: dict) -> dict:
    return {
        "paper": delivery["paper"],
        "scites": int(delivery.get("scites", 0)),
        "rank": int(delivery.get("rank", 0)),
        "jp": delivery.get("abstract_translated"),
        "jp_title": delivery.get("title_translated"),
        "genre_names": list(delivery.get("genre_names", [])),
    }


def pending_entries(state: dict, mode: str) -> list[tuple[str, dict]]:
    store = state[f"{mode}_deliveries"]
    result: list[tuple[str, dict]] = []
    for period_key in sorted(store):
        deliveries = store[period_key]
        if not isinstance(deliveries, dict):
            continue
        for delivery in deliveries.values():
            if (
                isinstance(delivery, dict)
                and isinstance(delivery.get("paper"), dict)
                and delivery.get("status", "pending") != "delivered"
            ):
                result.append((period_key, entry_from_delivery(delivery)))
    return result


def persist_translations(
    state: dict, mode: str, entries: list[tuple[str, dict]]
) -> None:
    store = state[f"{mode}_deliveries"]
    for period_key, entry in entries:
        pid = entry["paper"]["id"]
        delivery = store.get(period_key, {}).get(pid)
        if not isinstance(delivery, dict):
            continue
        delivery["abstract_translated"] = entry.get("jp")
        delivery["title_translated"] = entry.get("jp_title")


def post_daily_digest(
    webhook: str, period_key: str, entries: list[dict]
) -> bool:
    fields = []
    for rank, entry in enumerate(entries, start=1):
        paper = entry["paper"]
        display_title = entry.get("jp_title") or paper["title"]
        value = f"[{display_title}]({paper['link']})"
        if display_title != paper["title"]:
            value += f"\n{paper['title']}"
        genres = ", ".join(entry.get("genre_names", []))
        if genres:
            value += f"\n分類: {genres}"
        fields.append({
            "name": f"#{rank} · {entry['scites']} Scites",
            "value": arxiv_bot.truncate(value, 1024),
        })
    embed = {
        "title": f"🔥 SciRate 日次Top {len(entries)} | {period_key}",
        "description": (
            "その日のquant-ph発表から、23:30 JST時点の"
            "Scite数上位を掲載します。"
        ),
        "color": 0xF39C12,
        "fields": fields,
        "footer": {"text": "SciRate snapshot · exactly ranked by SciRate"},
        "timestamp": _now_utc(),
    }
    status, body = arxiv_bot.http_post_json(webhook, {"embeds": [embed]})
    if status == 429:
        try:
            wait = float(json.loads(body).get("retry_after", 2))
        except (json.JSONDecodeError, TypeError, ValueError):
            wait = 2
        time.sleep(wait + 0.5)
        status, _ = arxiv_bot.http_post_json(webhook, {"embeds": [embed]})
    return status in (200, 204)


def source_report_fields(source_status: dict) -> tuple[list[dict], list[dict]]:
    notices: list[dict] = []
    failures: list[dict] = []
    if source_status.get("status") == "waiting_for_api":
        detail = str(source_status.get(
            "last_error", "公式JSON endpointから有効な応答なし"))
        notices.append({
            "source": "SciRate JSON API",
            "message": (
                f"取得待ち: {detail}; 対象期間は再取得待ちに保存済み。"
                "HTML直取得もCloudflare Managed Challengeで拒否されるため"
                "使用していません"
            ),
        })
    elif source_status.get("status") == "waiting_for_source":
        detail = str(source_status.get(
            "last_error", "公式JSON・設定済みrelayから有効な応答なし"))
        notices.append({
            "source": "SciRate JSON sources",
            "message": (
                f"取得待ち: {detail}; 対象期間は再取得待ちに保存済み。"
                "HTML直取得もCloudflare Managed Challengeで拒否されるため"
                "使用していません"
            ),
        })
    elif source_status.get("status") == "degraded":
        failures.append({
            "source": "SciRate discovery",
            "error": str(source_status.get(
                "last_error", "previously available API is unavailable")),
        })
    elif (
        source_status.get("status") == "available"
        and source_status.get("provider") == "relay"
    ):
        primary_error = str(source_status.get(
            "last_primary_error", "公式JSON endpointは利用不可"))
        notices.append({
            "source": "SciRate trusted relay",
            "message": (
                f"公式JSON失敗 ({primary_error}); "
                "検証済みrelay snapshotで取得"
            ),
        })
    return notices, failures


def update_source_failure(
    source_status: dict,
    exc: Exception,
    template: str,
    days: int,
    target_date: str,
    relay_configured: bool = False,
) -> None:
    ever_available = bool(source_status.get("api_ever_available", False))
    source_ever_available = bool(
        source_status.get("source_ever_available", ever_available))
    try:
        endpoint = arxiv_bot.redact_url(scirate_api_page_url(
            template, days, 1, target_date=target_date))
    except SciRateAPIError:
        endpoint = arxiv_bot.redact_url(template)
    source_status.update({
        "api_ever_available": ever_available,
        "source_ever_available": source_ever_available,
        "status": (
            "degraded" if source_ever_available
            else "waiting_for_source" if relay_configured
            else "waiting_for_api"),
        "last_probe_at": _now_utc(),
        "last_error": str(exc),
        "last_endpoint": endpoint,
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("auto", "daily", "weekly"),
                        default=os.environ.get("SCIRATE_DIGEST_MODE", "auto"))
    parser.add_argument("--date", dest="target_date",
                        default=os.environ.get("SCIRATE_TARGET_DATE", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--translate-only", action="store_true")
    parser.add_argument("--deliver-only", action="store_true")
    parser.add_argument("--html-file", help="Local weekly parser fixture only")
    args = parser.parse_args()
    phase_flags = [args.discover_only, args.translate_only, args.deliver_only]
    if sum(phase_flags) > 1:
        raise SystemExit("SciRate phase flags are mutually exclusive")
    if args.dry_run and any(phase_flags):
        raise SystemExit("--dry-run cannot be combined with phase flags")

    try:
        target = (
            date.fromisoformat(args.target_date)
            if args.target_date else datetime.now(JST).date()
        )
    except ValueError as exc:
        raise SystemExit("--date must be YYYY-MM-DD") from exc
    mode = resolve_mode(args.mode, target)
    if mode == "skip":
        print("[info] Saturday JST: no SciRate digest is scheduled")
        return
    period_key, period_label, range_days = period_details(mode, target)
    resume_only = args.translate_only or args.deliver_only

    cfg = load_json(arxiv_bot.CONFIG_PATH, {})
    top_n = max(1, int(cfg.get("scirate_daily_top_n", 3)))
    daily_min = max(0, int(cfg.get("scirate_daily_min_scites", 1)))
    weekly_min = max(0, int(cfg.get("scirate_min_scites", 30)))
    min_scites = daily_min if mode == "daily" else weekly_min
    template = (
        os.environ.get("SCIRATE_API_URL_TEMPLATE", "").strip()
        or str(cfg.get(
            "scirate_api_url_template", SCIRATE_API_URL_TEMPLATE)).strip()
    )
    relay_template = os.environ.get(
        "SCIRATE_RELAY_URL_TEMPLATE", "").strip()
    relay_bearer_token = os.environ.get(
        "SCIRATE_RELAY_BEARER_TOKEN", "").strip()
    max_pages = int(
        os.environ.get("SCIRATE_API_MAX_PAGES", "").strip()
        or cfg.get("scirate_api_max_pages", 20)
    )
    page_size = int(
        os.environ.get("SCIRATE_API_PAGE_SIZE", "").strip()
        or cfg.get("scirate_api_page_size", 50)
    )
    backlog_max_periods = max(0, int(
        os.environ.get("SCIRATE_BACKLOG_MAX_PERIODS", "").strip()
        or cfg.get("scirate_backlog_max_periods", 8)
    ))

    state = normalize_state(load_json(STATE_PATH, {}))
    source_status = state["source_status"]
    log: list[dict] = load_json(arxiv_bot.LOG_PATH, [])
    previous = log_index(log)
    genre_map = {g["id"]: g for g in cfg.get("genres", [])}
    candidates: list[dict] = []
    pages_fetched = 0
    discovery_failures: dict[str, str] = {}

    if not resume_only:
        if args.html_file and mode != "weekly":
            raise SystemExit("--html-file supports weekly fixtures only")
        posted_periods = state[f"{mode}_posted"]
        deliveries_by_period = state[f"{mode}_deliveries"]
        pending = state["pending_discovery"][mode]

        # Persist every target before I/O.  In particular, an initial 403 must
        # not erase a period merely because no source has succeeded before.
        pending.setdefault(period_key, {
            "target_date": target.isoformat(), "queued_at": _now_utc()})
        ordered_targets = ordered_discovery_targets(
            pending, period_key, target, backlog_max_periods)
        if not args.dry_run:
            write_json(STATE_PATH, state)

        for discover_key, discover_target in ordered_targets:
            current_key, _label, discover_days = period_details(
                mode, discover_target)
            if current_key != discover_key:
                pending.pop(discover_key, None)
                discover_key = current_key
            if discover_key in posted_periods:
                pending.pop(discover_key, None)
                continue
            attempt_record = pending.setdefault(discover_key, {
                "target_date": discover_target.isoformat(),
                "queued_at": _now_utc(),
            })
            attempt_record["last_attempt_at"] = _now_utc()
            attempt_record["attempts"] = int(
                attempt_record.get("attempts", 0)) + 1
            if not args.dry_run:
                write_json(STATE_PATH, state)
            if args.html_file:
                period_candidates = parse_scirate_candidates(
                    Path(args.html_file).read_text(encoding="utf-8"),
                    min_scites)
                period_pages = 0
            else:
                try:
                    (
                        period_candidates,
                        period_pages,
                        provider,
                        primary_error,
                    ) = fetch_scirate_candidates_with_fallback(
                        template,
                        relay_template,
                        discover_days,
                        min_scites,
                        max_pages=max_pages,
                        page_size=page_size,
                        target_date=discover_target.isoformat(),
                        limit=top_n if mode == "daily" else None,
                        require_pubdate=True,
                        relay_bearer_token=relay_bearer_token,
                    )
                except SciRateAPIError as exc:
                    discovery_failures[discover_key] = str(exc)
                    update_source_failure(
                        source_status, exc, template, discover_days,
                        discover_target.isoformat(),
                        relay_configured=bool(relay_template))
                    arxiv_bot.save_error_diagnostic(
                        "optional_source_unavailable", method="GET",
                        body=traceback.format_exc(), exception=exc)
                    print(
                        "[notice] SciRate JSON API unavailable; persisted "
                        f"queue only ({exc})", file=sys.stderr)
                    if not args.dry_run:
                        write_json(STATE_PATH, state)
                    # A current snapshot can be briefly unavailable while
                    # older relay snapshots are valid, and one old snapshot
                    # can expire while newer backlog is valid. Continue within
                    # the bounded backlog budget so neither direction starves.
                    continue
                else:
                    pages_fetched += period_pages
                    selected_template = (
                        template if provider == "official"
                        else relay_template
                    )
                    source_status.update({
                        "api_ever_available": bool(
                            source_status.get("api_ever_available"))
                            or provider == "official",
                        "source_ever_available": True,
                        "status": "available",
                        "provider": provider,
                        "last_probe_at": _now_utc(),
                        "last_success_at": _now_utc(),
                        "last_error": "",
                        "last_primary_error": primary_error,
                        "last_endpoint": arxiv_bot.redact_url(
                            scirate_api_page_url(
                                selected_template, discover_days, 1,
                                target_date=discover_target.isoformat())),
                        "pages_fetched": period_pages,
                    })
                    pending.setdefault(discover_key, {
                        "target_date": discover_target.isoformat(),
                        "queued_at": _now_utc(),
                    })
                    if not args.dry_run:
                        write_json(STATE_PATH, state)

            queued = deliveries_by_period.setdefault(discover_key, {})
            already_posted = set(
                posted_periods.get(discover_key, {}).get("ids", []))
            period_candidates = [
                item for item in period_candidates
                if item["id"] not in already_posted
                and item["id"] not in queued
            ]
            try:
                metadata = fetch_arxiv_metadata(
                    [item["id"] for item in period_candidates])
                missing = [item["id"] for item in period_candidates
                           if item["id"] not in metadata]
                if missing:
                    raise RuntimeError(
                        "arXiv metadata API omitted candidate(s): "
                        + ", ".join(missing[:10]))
            except Exception as exc:  # noqa: BLE001
                discovery_failures[discover_key] = (
                    f"arXiv metadata API: {exc}")
                arxiv_bot.save_error_diagnostic(
                    "source_error", method="GET", body=traceback.format_exc(),
                    exception=exc)
                if not queued:
                    deliveries_by_period.pop(discover_key, None)
                if not args.dry_run:
                    write_json(STATE_PATH, state)
                continue
            for candidate in period_candidates:
                entry = entry_from_candidate(
                    candidate, metadata[candidate["id"]],
                    previous.get(candidate["id"], {}), cfg, genre_map, mode)
                queued[candidate["id"]] = delivery_from_entry(
                    entry, queued.get(candidate["id"]))
            candidates.extend(period_candidates)
            pending.pop(discover_key, None)
            if not queued:
                deliveries_by_period.pop(discover_key, None)
                posted_periods[discover_key] = {
                    "ids": [], "checked_at": _now_utc(), "empty": True}
            if not args.dry_run:
                write_json(STATE_PATH, state)

        # A later successful backlog fetch must not erase an earlier missed
        # period from the operational report. Keep every per-period error
        # visible and leave those periods pending for a future retry.
        if discovery_failures:
            source_status["pending_errors"] = dict(discovery_failures)
            source_status["last_error"] = "; ".join(
                f"{key}: {error}"
                for key, error in sorted(discovery_failures.items())
            )
            source_status["status"] = (
                "degraded"
                if source_status.get("source_ever_available")
                else "waiting_for_source" if relay_template
                else "waiting_for_api"
            )
        else:
            source_status.pop("pending_errors", None)
        if not args.dry_run:
            write_json(STATE_PATH, state)

    queued_entries = pending_entries(state, mode)
    print(
        f"[info] SciRate {mode}: period={period_label}, "
        f"api_status={source_status.get('status', 'unknown')}, "
        f"pages={pages_fetched}, min_scites={min_scites}, "
        f"discovered={len(candidates)}, queued={len(queued_entries)}"
    )
    if args.dry_run:
        for _key, entry in queued_entries:
            print(
                f"[{entry['scites']:>3} Scites] "
                f"{entry['paper']['title']}\n      {entry['paper']['link']}")
        return
    if args.discover_only:
        print(f"discovered {len(candidates)} SciRate {mode} paper(s)")
        return

    if not args.deliver_only:
        entries_only = [entry for _, entry in queued_entries]
        translate_entries(
            entries_only, cfg, include_abstract=(mode == "weekly"))
        persist_translations(state, mode, queued_entries)
        write_json(STATE_PATH, state)
    if args.translate_only:
        print(f"translated {len(queued_entries)} queued SciRate paper(s)")
        return

    webhook = os.environ.get("DISCORD_WEBHOOK_SCIRATE", "").strip()
    posted_records: list[dict] = []
    deferred_records: list[dict] = []
    failed_records: list[dict] = []
    messages = 0
    require_translation = bool(cfg.get("require_translation", True))
    show_jp_title = arxiv_bot.show_translated_title(cfg)
    store = state[f"{mode}_deliveries"]
    posted_store = state[f"{mode}_posted"]

    if mode == "daily":
        for daily_key in sorted(store):
            period_entries = [
                entry_from_delivery(delivery)
                for delivery in store[daily_key].values()
                if isinstance(delivery, dict)
                and delivery.get("status", "pending") != "delivered"
            ]
            if not period_entries:
                continue
            period_entries.sort(key=lambda entry: (
                entry.get("rank") or 10_000,
                -entry["scites"],
                entry["paper"]["id"],
            ))
            missing_title = [
                entry for entry in period_entries
                if require_translation and show_jp_title
                and entry["paper"].get("title") and not entry.get("jp_title")
            ]
            if missing_title:
                for entry in missing_title:
                    deferred_records.append({
                        "id": entry["paper"]["id"],
                        "title": entry["paper"]["title"],
                        "link": entry["paper"]["link"],
                        "genre_names": ["SciRate(日次・翻訳待ち)"],
                    })
                continue
            if not webhook:
                failed_records.extend({
                    "id": entry["paper"]["id"],
                    "title": entry.get("jp_title") or entry["paper"]["title"],
                    "link": entry["paper"]["link"],
                    "genre_names": ["SciRate(webhook未設定)"],
                } for entry in period_entries)
                continue
            if post_daily_digest(webhook, daily_key, period_entries):
                messages += 1
                ids = [entry["paper"]["id"] for entry in period_entries]
                posted_store[daily_key] = {
                    "ids": ids, "posted_at": _now_utc(), "message_count": 1}
                store.pop(daily_key, None)
                for entry in period_entries:
                    posted_records.append({
                        "id": entry["paper"]["id"],
                        "title": entry.get("jp_title") or entry["paper"]["title"],
                        "link": entry["paper"]["link"],
                        "genre_names": ["SciRate日次Top"],
                    })
                write_json(STATE_PATH, state)
            else:
                failed_records.extend({
                    "id": entry["paper"]["id"],
                    "title": entry.get("jp_title") or entry["paper"]["title"],
                    "link": entry["paper"]["link"],
                    "genre_names": ["SciRate(日次投稿失敗)"],
                } for entry in period_entries)
    else:
        daily_ids = {
            pid for record in state["daily_posted"].values()
            if isinstance(record, dict) for pid in record.get("ids", [])
        }
        for weekly_key in sorted(store):
            deliveries = store[weekly_key]
            succeeded: list[str] = []
            for pid, delivery in list(deliveries.items()):
                if not isinstance(delivery, dict):
                    continue
                entry = entry_from_delivery(delivery)
                paper = entry["paper"]
                record = {
                    "id": pid,
                    "title": entry.get("jp_title") or paper["title"],
                    "link": paper["link"],
                }
                if (require_translation and paper.get("abstract")
                        and not entry.get("jp")):
                    deferred_records.append({
                        **record, "genre_names": ["SciRate(週間・翻訳待ち)"]})
                    continue
                if not webhook:
                    failed_records.append({
                        **record, "genre_names": ["SciRate(webhook未設定)"]})
                    continue
                genre_label = ", ".join(entry.get("genre_names", []))
                extra_fields = [{
                    "name": "SciRate",
                    "value": (
                        f"{entry['scites']} Scites · 週間30+ "
                        f"({weekly_key.replace('_', '〜')})"),
                }]
                if pid in daily_ids:
                    extra_fields.append({
                        "name": "日次ランキング",
                        "value": "日次Top 3掲載済み",
                    })
                if arxiv_bot.post_to_discord(
                    webhook, paper, genre_label or "SciRate",
                    entry.get("jp"), entry.get("jp_title"), cfg,
                    extra_fields=extra_fields,
                ):
                    messages += 1
                    succeeded.append(pid)
                    posted_records.append({
                        **record, "genre_names": ["SciRate週間30+"]})
                    delivery["status"] = "delivered"
                    delivery["delivered_at"] = _now_utc()
                    write_json(STATE_PATH, state)
                else:
                    failed_records.append({
                        **record, "genre_names": ["SciRate(週間投稿失敗)"]})
                time.sleep(1.2)
            if succeeded:
                old_ids = set(posted_store.get(weekly_key, {}).get("ids", []))
                posted_store[weekly_key] = {
                    "ids": sorted(old_ids | set(succeeded)),
                    "posted_at": _now_utc(),
                    "message_count": len(old_ids | set(succeeded)),
                }
                for pid in succeeded:
                    deliveries.pop(pid, None)
                if not deliveries:
                    store.pop(weekly_key, None)
                write_json(STATE_PATH, state)

    if not webhook and (queued_entries or candidates):
        arxiv_bot.save_error_diagnostic(
            "configuration_error",
            body="DISCORD_WEBHOOK_SCIRATE is not configured")
    notices, source_failures = source_report_fields(source_status)
    arxiv_bot.notify_run_report({
        "source": "SciRate日次Top" if mode == "daily" else "SciRate週間30+",
        "fetched": len(candidates),
        "candidates": len(queued_entries),
        "messages": messages,
        "posted": posted_records,
        "deferred": deferred_records,
        "failed": failed_records,
        "source_notices": notices,
        "source_failures": source_failures,
        "translated": dict(arxiv_bot._translation_success),
        "dead_translators": arxiv_bot.dead_translators(cfg),
    }, cfg)
    print(
        f"posted {len(posted_records)} SciRate {mode} paper(s) in "
        f"{messages} message(s); deferred={len(deferred_records)}, "
        f"failed={len(failed_records)}"
    )
    if failed_records or source_failures:
        raise SystemExit(3)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        arxiv_bot.save_error_diagnostic(
            "unhandled_exception", body=traceback.format_exc(), exception=exc)
        raise
