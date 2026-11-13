#!/usr/bin/env python3
"""
arXiv quant-ph -> Discord notifier with translated abstracts.

- Fetches the official arXiv RSS feed (rss.arxiv.org/rss/quant-ph) and
  reconciles it against arXiv OAI-PMH announcement datestamps, so an empty or
  stale RSS snapshot cannot drop an announcement day
- Harvests selected adjacent categories through the same OAI-PMH sets to find
  non-cross-listed candidates, then subjects them to strict LLM review with
  an explicit skip decision before they can enter configured genre channels
- Filters out cross-listed papers whose primary category is irrelevant
  (e.g. cond-mat.*) while keeping quantum-information-adjacent categories
- Classifies papers using free-tier Gemini 3.5 Flash-Lite then 3.8 Flash.
  A normalized TF-IDF/phrase classifier provides the no-model fallback.
  Records raw labels, deterministic corrections and the policy version.
- Translates abstracts via the configurable translator chain
  (default: DeepL -> Azure -> optional Apps Script); backends stop on quota exhaustion
  (circuit breaker), and any paper left untranslated is deferred, never
  posted in English.
- Persists a durable per-paper/per-channel queue before translation and
  Discord delivery. Multi-genre papers are retried only for channels that have
  not acknowledged delivery, and the embed footer lists all assigned genres.
- Posts a per-run summary report (in Japanese) to the bot-emergency
  channel: which papers went to which channels, deferrals, failures

Standard library only. Designed to run on GitHub Actions.
"""

import email.utils
import json
import hashlib
import math
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "seen_ids.json"
LOG_PATH = BASE_DIR / "posted_log.json"

RSS_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}
ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

USER_AGENT = "arxiv-quantph-discord-bot/1.0 (personal research notifier)"
DIAGNOSTICS_PATH = Path(os.environ.get(
    "ERROR_DIAGNOSTICS_PATH", BASE_DIR / "error_diagnostics.jsonl"))
DIAGNOSTIC_BODY_LIMIT = 4096
DIAGNOSTIC_RESPONSE_HEADERS = {
    "age",
    "cf-ray",
    "content-length",
    "content-type",
    "date",
    "retry-after",
    "server",
    "via",
    "x-cloud-trace-context",
    "x-correlation-id",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-request-id",
}


# ---------------------------------------------------------------- utilities

def redact_url(url: str) -> str:
    """Return a log-safe URL with credentials and query values removed."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<redacted-url>"
    path = parsed.path
    if "/api/webhooks/" in path:
        prefix = path.split("/api/webhooks/", 1)[0]
        path = f"{prefix}/api/webhooks/<redacted>/<redacted>"
    query = "<redacted>" if parsed.query else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, query, ""))


def redact_request_error(value: Any, url: str) -> str:
    """Remove the request URL and configured credentials from error text."""
    text = str(value)
    if url:
        text = text.replace(url, redact_url(url))
    for name, secret in os.environ.items():
        upper_name = name.upper()
        sensitive = (
            "SECRET" in upper_name
            or "PASSWORD" in upper_name
            or "WEBHOOK" in upper_name
            or upper_name.endswith("_KEY")
            or upper_name.endswith("_TOKEN")
        )
        if sensitive and len(secret) >= 4:
            text = text.replace(secret, "<redacted>")
    return text


def _diagnostic_body(body: bytes | str | None, url: str) -> str:
    """Return a bounded, log-safe representation of a response body."""
    if body is None:
        return ""
    if isinstance(body, bytes):
        text = body[:DIAGNOSTIC_BODY_LIMIT].decode("utf-8", errors="replace")
        truncated = len(body) > DIAGNOSTIC_BODY_LIMIT
    else:
        text = body[:DIAGNOSTIC_BODY_LIMIT]
        truncated = len(body) > DIAGNOSTIC_BODY_LIMIT
    text = redact_request_error(text, url)
    return text + ("\n<truncated>" if truncated else "")


def _diagnostic_headers(headers: Any, url: str) -> dict[str, str]:
    """Keep only response headers useful for debugging and safe to retain."""
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {}
    return {
        str(name).lower(): redact_request_error(str(value)[:1000], url)
        for name, value in items
        if str(name).lower() in DIAGNOSTIC_RESPONSE_HEADERS
    }


def save_error_diagnostic(
    kind: str,
    *,
    url: str = "",
    method: str = "",
    status: int | None = None,
    reason: Any = "",
    headers: Any = None,
    body: bytes | str | None = None,
    exception: BaseException | None = None,
) -> str:
    """Append one sanitized diagnostic record without masking the real error.

    The JSONL file is uploaded by GitHub Actions as a retained artifact.  A
    failure to write diagnostics is reported but never replaces the original
    network/application failure.
    """
    diagnostic_id = f"diag-{os.getpid()}-{time.time_ns()}"
    safe_url = redact_url(url) if url else ""
    record = {
        "id": diagnostic_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "method": method,
        "url": safe_url,
        "status": status,
        "reason": redact_request_error(reason, url) if reason else "",
        "response_headers": _diagnostic_headers(headers, url),
        "response_body": _diagnostic_body(body, url),
        "exception_type": type(exception).__name__ if exception else "",
        "exception": (
            redact_request_error(exception, url) if exception else ""
        ),
    }
    try:
        DIAGNOSTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DIAGNOSTICS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(
            f"[diagnostic] {diagnostic_id} saved: kind={kind} "
            f"status={status if status is not None else '-'} "
            f"url={safe_url or '-'} body={record['response_body'][:500]!r}",
            file=sys.stderr,
        )
    except Exception as diagnostic_error:  # noqa: BLE001
        print(
            "[warn] could not save sanitized error diagnostic: "
            f"{type(diagnostic_error).__name__}: {diagnostic_error}",
            file=sys.stderr,
        )
    return diagnostic_id


def read_http_error_body(exc: urllib.error.HTTPError) -> bytes:
    """Consume and close an HTTPError response body."""
    try:
        return exc.read()
    finally:
        exc.close()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTPError so credentials cannot cross origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url: str, timeout: int = 30,
             headers: dict[str, str] | None = None,
             *, allow_redirects: bool = True) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    try:
        if allow_redirects:
            response = urllib.request.urlopen(req, timeout=timeout)
        else:
            response = urllib.request.build_opener(
                _NoRedirectHandler()).open(req, timeout=timeout)
        with response as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = read_http_error_body(exc)
        save_error_diagnostic(
            "http_error", url=url, method="GET", status=exc.code,
            reason=exc.reason, headers=exc.headers, body=body, exception=exc)
        raise
    except urllib.error.URLError as exc:
        save_error_diagnostic(
            "connection_error", url=url, method="GET", reason=exc.reason,
            exception=exc)
        raise
    except Exception as exc:
        save_error_diagnostic(
            "request_error", url=url, method="GET", exception=exc)
        raise


def http_post_json(url: str, payload: dict, headers: dict | None = None,
                   timeout: int = 120) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = read_http_error_body(e)
        save_error_diagnostic(
            "http_error", url=url, method="POST", status=e.code,
            reason=e.reason, headers=e.headers, body=body, exception=e)
        return e.code, body
    except TimeoutError as e:
        save_error_diagnostic(
            "request_timeout", url=url, method="POST", exception=e)
        print(f"[warn] Request timed out after {timeout:g}s for "
              f"{redact_url(url)}", file=sys.stderr)
        return 0, b""
    except urllib.error.URLError as e:
        save_error_diagnostic(
            "connection_error", url=url, method="POST", reason=e.reason,
            exception=e)
        print(f"[warn] Connection error for {redact_url(url)}: "
              f"{redact_request_error(e.reason, url)}",
              file=sys.stderr)
        return 0, b""
    except Exception as e:
        save_error_diagnostic(
            "request_error", url=url, method="POST", exception=e)
        print(f"[warn] Unexpected request error for {redact_url(url)}: "
              f"{redact_request_error(e, url)}",
              file=sys.stderr)
        return 0, b""


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def atomic_write_json(path: Path, data: Any, *,
                      ensure_ascii: bool = True) -> None:
    """Atomically replace a JSON file after flushing its new contents."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=ensure_ascii)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------- arXiv RSS

def fetch_feed(category: str) -> list[dict]:
    """Parse rss.arxiv.org/rss/<category> into a list of paper dicts.

    For local testing, set ARXIV_TEST_FEED to a local RSS file path to read
    from disk instead of the network (useful on weekends/holidays when the
    live feed is empty).
    """
    test_path = os.environ.get("ARXIV_TEST_FEED", "")
    if test_path:
        raw = Path(test_path).read_bytes()
    else:
        raw = http_get(f"https://rss.arxiv.org/rss/{category}")
    root = ET.fromstring(raw)
    papers = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = item.findtext("description") or ""
        creator = (item.findtext("dc:creator", namespaces=RSS_NS) or "").strip()
        announce = (item.findtext("arxiv:announce_type",
                                  namespaces=RSS_NS) or "new").strip()
        categories = [c.text.strip() for c in item.findall("category")
                      if c.text]
        m = re.search(r"Abstract:\s*(.*)", desc, flags=re.S)
        abstract = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        arxiv_id = link.rsplit("/", 1)[-1] if link else title
        papers.append({
            "id": arxiv_id,
            "title": title,
            "link": link,
            "authors": creator,
            "announce_type": announce,          # new | cross | replace | ...
            "categories": categories,           # first entry = primary (heuristic)
            "primary": categories[0] if categories else category,
            "abstract": abstract,
        })
    return papers


def build_external_arxiv_query(category: str, terms: list[str],
                               max_results: int = 100,
                               start: int = 0) -> str:
    """Build an arXiv API query for a category plus recall-oriented terms."""
    clauses = []
    for raw_term in terms:
        term = str(raw_term).strip().replace('"', "")
        if not term:
            continue
        clauses.append(f'all:"{term}"' if re.search(r"\s", term)
                       else f"all:{term}")
    if not clauses:
        raise ValueError(f"external arXiv query for {category} has no terms")
    search_query = f"cat:{category} AND ({' OR '.join(clauses)})"
    params = urllib.parse.urlencode({
        "search_query": search_query,
        "start": max(0, int(start)),
        "max_results": max(1, int(max_results)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    return f"https://export.arxiv.org/api/query?{params}"


def parse_external_atom(raw: bytes, source_category: str) -> list[dict]:
    """Parse custom-query Atom results into the normal internal paper shape."""
    root = ET.fromstring(raw)
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise ValueError("arXiv API did not return an Atom feed")
    papers = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = re.sub(
            r"\s+", " ", entry.findtext("atom:title", "", ATOM_NS)).strip()
        abstract = re.sub(
            r"\s+", " ", entry.findtext("atom:summary", "", ATOM_NS)).strip()
        id_url = entry.findtext("atom:id", "", ATOM_NS).strip()
        if not id_url or "/api/errors" in id_url:
            raise ValueError("arXiv API returned an error or invalid entry")
        arxiv_id = re.sub(r"v\d+$", "", id_url.rsplit("/", 1)[-1])
        categories = [
            node.get("term", "").strip()
            for node in entry.findall("atom:category", ATOM_NS)
            if node.get("term")
        ]
        primary_node = entry.find("arxiv:primary_category", ATOM_NS)
        primary = (
            primary_node.get("term", "").strip()
            if primary_node is not None else
            (categories[0] if categories else source_category)
        )
        authors = ", ".join(
            name.text.strip()
            for name in entry.findall("atom:author/atom:name", ATOM_NS)
            if name.text
        )
        alternate = next((
            node.get("href", "")
            for node in entry.findall("atom:link", ATOM_NS)
            if node.get("rel") == "alternate"
        ), id_url)
        papers.append({
            "id": arxiv_id,
            "title": title,
            "link": alternate,
            "authors": authors,
            "announce_type": "external",
            "categories": categories,
            "primary": primary,
            "abstract": abstract,
            "published": entry.findtext("atom:published", "", ATOM_NS).strip(),
            "updated": entry.findtext("atom:updated", "", ATOM_NS).strip(),
            "source_feed": source_category,
        })
    return papers


_RETRYABLE_ARXIV_API_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(headers) -> float | None:
    """Return a numeric Retry-After value, ignoring HTTP-date forms."""
    try:
        value = float((headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def http_get_arxiv_api(url: str, rule: dict) -> bytes:
    """GET an arXiv API page, retrying timeouts and rate limits.

    export.arxiv.org intermittently times out or answers 429 "Rate exceeded."
    during busy hours.  A short backoff almost always succeeds, whereas
    failing immediately turns a transient blip into a red workflow run.
    """
    timeout = int(rule.get("timeout_sec", 60))
    retries = max(0, int(rule.get("retry_attempts", 3)))
    backoff_base = float(rule.get("retry_backoff_sec", 10))
    backoff_max = float(rule.get("retry_backoff_max_sec", 120))
    for attempt in range(retries + 1):
        try:
            return http_get(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if (exc.code not in _RETRYABLE_ARXIV_API_STATUSES
                    or attempt >= retries):
                raise
            retry_after = _retry_after_seconds(exc.headers)
            reason = f"HTTP {exc.code}"
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt >= retries:
                raise
            retry_after = None
            reason = type(exc).__name__
        backoff = backoff_base * (2 ** attempt)
        if retry_after is not None:
            backoff = max(backoff, retry_after)
        backoff = min(backoff_max, backoff)
        print(f"[warn] arXiv API {reason}; retry in {backoff:g}s "
              f"({attempt + 1}/{retries})", file=sys.stderr)
        time.sleep(backoff)
    raise AssertionError("unreachable")


def fetch_external_arxiv(category: str, rule: dict) -> list[dict]:
    """Fetch short API queries, paging until the lookback boundary."""
    terms = [str(t) for t in rule.get("terms", []) if str(t).strip()]
    terms_per_query = max(1, int(rule.get("terms_per_query", 8)))
    page_size = max(1, int(rule.get("max_results", 100)))
    lookback_days = int(rule.get("lookback_days", 4))
    min_interval = float(rule.get("query_min_interval_sec", 3))
    by_id: dict[str, dict] = {}
    chunks = [
        terms[i:i + terms_per_query]
        for i in range(0, len(terms), terms_per_query)
    ]
    request_count = 0
    for chunk in chunks:
        start = 0
        previous_page: tuple[str, ...] | None = None
        while True:
            if request_count:
                time.sleep(min_interval)
            url = build_external_arxiv_query(
                category, chunk, page_size, start=start)
            rows = parse_external_atom(
                http_get_arxiv_api(url, rule), category)
            request_count += 1
            signature = tuple(paper["id"] for paper in rows)
            # Defensive guard for an upstream endpoint that ignores ``start``.
            if signature and signature == previous_page:
                break
            previous_page = signature
            for paper in rows:
                by_id.setdefault(paper["id"], paper)
            if (
                len(rows) < page_size
                or not rows
                or not external_paper_is_recent(rows[-1], lookback_days)
            ):
                break
            start += page_size
    return list(by_id.values())


def _normalized_match_text(value: str) -> str:
    return " " + re.sub(r"[^a-z0-9+]+", " ", value.lower()).strip() + " "


def matches_external_terms(paper: dict, terms: list[str]) -> bool:
    """Locally verify that an API result contains at least one query term."""
    haystack = _normalized_match_text(
        f"{paper.get('title', '')} {paper.get('abstract', '')}")
    return any(
        _normalized_match_text(str(term)).strip()
        and f" {_normalized_match_text(str(term)).strip()} " in haystack
        for term in terms
    )


def is_qec_adjacent_coding_paper(paper: dict, cfg: dict) -> bool:
    """Return whether a paper has a substantive coding-theory signal."""
    terms = [
        str(term) for term in cfg.get("qec_adjacent_coding_terms", [])
    ]
    return bool(terms) and matches_external_terms(paper, terms)


def external_paper_is_recent(paper: dict, lookback_days: int,
                             now: datetime | None = None) -> bool:
    """Bound first-run backfill while retaining papers across long weekends."""
    published = str(paper.get("published", "")).strip()
    if not published:
        return True
    try:
        timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return True
    reference = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp >= reference - timedelta(days=max(1, lookback_days))


def effective_external_lookback_days(
        rule: dict, last_success: str | None,
        now: datetime | None = None) -> int:
    """Expand the API lookback to cover time since the last complete fetch."""
    base = max(1, int(rule.get("lookback_days", 4)))
    if not last_success:
        return base
    try:
        cursor = datetime.fromisoformat(
            str(last_success).replace("Z", "+00:00"))
    except ValueError:
        return base
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    elapsed = max(0.0, (reference - cursor).total_seconds())
    overlap = max(1, int(rule.get("cursor_overlap_days", 2)))
    return max(base, math.ceil(elapsed / 86400) + overlap)


def select_external_candidates(
        fetched: list[dict], category: str, rule: dict,
        core_ids: set[str], seen: set[str],
        now: datetime | None = None) -> list[dict]:
    """Apply local safeguards after the recall-oriented arXiv API query."""
    lookback = int(rule.get("lookback_days", 4))
    terms = [str(t) for t in rule.get("terms", [])]
    return [
        paper for paper in fetched
        if paper["id"] not in core_ids
        and paper["id"] not in seen
        and paper["primary"] == category
        and "quant-ph" not in paper.get("categories", [])
        and external_paper_is_recent(paper, lookback, now)
        and matches_external_terms(paper, terms)
    ]


def feed_backfill_due(rule: dict, last_success: str | None,
                      now: datetime | None = None) -> bool:
    """Rate-limit complete feed reconciliation, retaining retry after failure."""
    if not rule.get("enabled", False):
        return False
    if not last_success:
        return True
    try:
        cursor = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return True
    elapsed = ((now or datetime.now(timezone.utc)) - cursor).total_seconds()
    return elapsed >= max(0, float(rule.get("min_interval_hours", 20))) * 3600


def fetch_feed_backfill(category: str, rule: dict,
                        now: datetime | None = None) -> list[dict]:
    """Reconcile recent submissions, paging through the entire outage window.

    This recovers new submissions and recent cross-lists after RSS outages;
    old submissions newly cross-listed outside this window still need RSS.
    Repeated or truncated pages are failures, never a successful checkpoint.
    """
    page_size = max(1, int(rule.get("max_results", 100)))
    max_pages = max(1, int(rule.get("max_pages", 100)))
    lookback = max(1, int(rule.get("lookback_days", 7)))
    reference = now or datetime.now(timezone.utc)
    result: dict[str, dict] = {}
    signatures: set[tuple[str, ...]] = set()
    for page in range(max_pages):
        if page:
            time.sleep(max(3, float(rule.get("query_min_interval_sec", 3))))
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "search_query": f"cat:{category}",
            "start": page * page_size,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        })
        raw = http_get_arxiv_api(url, rule)
        rows = parse_external_atom(raw, category)
        metadata = ET.fromstring(raw)
        namespace = "{http://a9.com/-/spec/opensearch/1.1/}"
        try:
            total = int(metadata.findtext(namespace + "totalResults", ""))
            offset = int(metadata.findtext(namespace + "startIndex", ""))
        except ValueError as err:
            raise ValueError("arXiv backfill has invalid pagination metadata") from err
        if (offset != page * page_size or total < offset + len(rows)
                or (len(rows) < page_size and total > offset + len(rows))):
            raise ValueError("arXiv backfill returned an incomplete page")
        signature = tuple(p["id"] for p in rows)
        if signature and signature in signatures:
            raise ValueError("arXiv feed backfill repeated a page")
        signatures.add(signature)
        for paper in rows:
            if not paper.get("published"):
                raise ValueError("arXiv backfill entry has no publication date")
            datetime.fromisoformat(paper["published"].replace("Z", "+00:00"))
            if not external_paper_is_recent(paper, lookback, reference):
                continue
            if category not in paper.get("categories", []):
                raise ValueError("arXiv backfill returned a different category")
            paper["source_feed"] = category
            paper["announce_type"] = (
                "new" if paper["primary"] == category else "cross")
            result.setdefault(paper["id"], paper)
        if (len(rows) < page_size or not rows or total == offset + len(rows)
                or not external_paper_is_recent(rows[-1], lookback, reference)):
            return list(result.values())
    raise ValueError("arXiv feed backfill exceeded max_pages; cursor retained")


# ------------------------------------------------------------ arXiv OAI-PMH
#
# RSS shows only the latest announcement and has served empty feeds on
# announcement days, while API date queries index the *submission* date, so a
# paper held in moderation can be announced outside any submission window.
# OAI-PMH datestamps follow announcements and later metadata changes, so a
# harvest from the last successful harvest date sees every announcement.

OAI_ENDPOINT = "https://oaipmh.arxiv.org/oai"
OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "raw": "http://arxiv.org/OAI/arXivRaw/",
}
_OAI_PHYSICS_ARCHIVES = frozenset({
    "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th",
    "math-ph", "nlin", "nucl-ex", "nucl-th", "physics", "quant-ph",
})


def oai_set_for_category(category: str) -> str:
    """Map an arXiv category such as quant-ph or cs.CR to its OAI set."""
    archive, _, subject = category.strip().partition(".")
    prefix = (f"physics:{archive}" if archive in _OAI_PHYSICS_ARCHIVES
              else f"{archive}:{archive}")
    return f"{prefix}:{subject}" if subject else prefix


def _oai_version_datetime(value: str) -> datetime | None:
    """Parse an arXivRaw version date such as 'Tue, 10 Nov 2026 18:45:36 GMT'."""
    try:
        parsed = email.utils.parsedate_to_datetime((value or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_oai_list_records(raw: bytes) -> tuple[list[dict], str | None, str, int]:
    """Parse one arXivRaw ListRecords page.

    Returns ``(records, resumption_token, response_date, skipped)``.  The OAI
    ``noRecordsMatch`` error is a successful empty harvest; any other OAI
    error, an HTML page or a missing ``responseDate`` raises, so a broken
    response can never advance the harvest cursor.
    """
    root = ET.fromstring(raw)
    if root.tag != f"{{{OAI_NS['oai']}}}OAI-PMH":
        raise ValueError("arXiv OAI-PMH did not return an OAI-PMH document")
    response_date = (root.findtext("oai:responseDate", "", OAI_NS) or "").strip()
    if not response_date:
        raise ValueError("arXiv OAI-PMH response has no responseDate")
    error = root.find("oai:error", OAI_NS)
    if error is not None:
        if error.get("code") == "noRecordsMatch":
            return [], None, response_date, 0
        raise ValueError(
            f"arXiv OAI-PMH error {error.get('code', '?')}: "
            f"{(error.text or '').strip()[:200]}")
    listing = root.find("oai:ListRecords", OAI_NS)
    if listing is None:
        raise ValueError("arXiv OAI-PMH response has no ListRecords element")

    def clean(meta: ET.Element, tag: str) -> str:
        return re.sub(r"\s+", " ", meta.findtext(tag, "", OAI_NS) or "").strip()

    records: list[dict] = []
    skipped = 0
    for record in listing.findall("oai:record", OAI_NS):
        header = record.find("oai:header", OAI_NS)
        if header is not None and header.get("status") == "deleted":
            continue
        meta = record.find("oai:metadata/raw:arXivRaw", OAI_NS)
        datestamp = (
            header.findtext("oai:datestamp", "", OAI_NS).strip()
            if header is not None else "")
        if meta is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", datestamp):
            skipped += 1
            continue
        pid = clean(meta, "raw:id")
        versions: list[tuple[int, datetime]] = []
        for node in meta.findall("raw:version", OAI_NS):
            label = re.fullmatch(r"v(\d+)", (node.get("version") or "").strip())
            stamp = _oai_version_datetime(node.findtext("raw:date", "", OAI_NS))
            if label and stamp:
                versions.append((int(label.group(1)), stamp))
        categories = clean(meta, "raw:categories").split()
        if not pid or not versions or not categories:
            skipped += 1
            continue
        versions.sort()
        records.append({
            "id": pid,
            "title": clean(meta, "raw:title"),
            "link": f"https://arxiv.org/abs/{pid}",
            "authors": clean(meta, "raw:authors"),
            "categories": categories,
            "primary": categories[0],
            "abstract": clean(meta, "raw:abstract"),
            "published": versions[0][1].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated": versions[-1][1].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version_count": len(versions),
            "oai_datestamp": datestamp,
        })
    token_node = listing.find("oai:resumptionToken", OAI_NS)
    token = (token_node.text or "").strip() if token_node is not None else ""
    return records, token or None, response_date, skipped


def oai_harvest_from_date(cursor: str | None, rule: dict,
                          now: datetime | None = None,
                          extra_days: int = 0) -> str:
    """First datestamp to harvest: the last successful harvest minus overlap."""
    reference = now or datetime.now(timezone.utc)
    base = reference
    if cursor:
        try:
            parsed = datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            base = min(base, parsed)
        except ValueError:
            pass
    start = base - timedelta(days=max(1, int(rule.get("overlap_days", 3))))
    if extra_days > 0:
        start = min(start, reference - timedelta(days=extra_days))
    return start.date().isoformat()


def fetch_oai_records(set_spec: str, from_date: str,
                      rule: dict) -> tuple[list[dict], str, int]:
    """Harvest every arXivRaw record in ``set_spec`` with datestamp >= from_date.

    Returns ``(records, cursor, skipped)``. ``cursor`` is the server's
    responseDate for the first page (never later than the local clock), so
    a stale cached response cannot move the next harvest past unseen records.
    Repeated resumption tokens or too many pages raise instead of returning
    a silently truncated harvest.
    """
    endpoint = str(rule.get("endpoint") or OAI_ENDPOINT)
    max_pages = max(1, int(rule.get("max_pages", 100)))
    interval = max(0.0, float(rule.get("page_interval_sec", 3)))
    params = {"verb": "ListRecords", "metadataPrefix": "arXivRaw",
              "set": set_spec, "from": from_date}
    by_id: dict[str, dict] = {}
    cursor: str | None = None
    skipped = 0
    tokens: set[str] = set()
    for page in range(max_pages):
        if page:
            time.sleep(interval)
        raw = http_get_arxiv_api(
            f"{endpoint}?{urllib.parse.urlencode(params)}", rule)
        rows, token, response_date, bad = parse_oai_list_records(raw)
        skipped += bad
        if cursor is None:
            try:
                served = datetime.fromisoformat(
                    response_date.replace("Z", "+00:00"))
            except ValueError as err:
                raise ValueError(
                    "arXiv OAI-PMH responseDate is not ISO 8601") from err
            if served.tzinfo is None:
                served = served.replace(tzinfo=timezone.utc)
            served = min(served, datetime.now(timezone.utc))
            cursor = served.strftime("%Y-%m-%dT%H:%M:%SZ")
        for row in rows:
            by_id[row["id"]] = row
        if not token:
            return list(by_id.values()), cursor, skipped
        if token in tokens:
            raise ValueError("arXiv OAI-PMH repeated a resumptionToken")
        tokens.add(token)
        params = {"verb": "ListRecords", "resumptionToken": token}
    raise ValueError("arXiv OAI-PMH harvest exceeded max_pages; cursor retained")


def oai_record_is_recent(record: dict, max_delay_days: int) -> bool:
    """Whether v1 was submitted at most ``max_delay_days`` before the datestamp.

    New announcements carry a fresh datestamp; papers announced long ago
    reappear only through metadata changes (journal-ref, DOI), so a bound on
    submission-to-datestamp delay keeps them out without an ID registry.
    """
    try:
        stamp = datetime.fromisoformat(str(record["oai_datestamp"])).date()
        first = datetime.fromisoformat(
            str(record["published"]).replace("Z", "+00:00")).date()
    except (KeyError, ValueError):
        return False
    return (stamp - first).days <= max(1, int(max_delay_days))


def _paper_from_oai(record: dict, **fields: str) -> dict:
    paper = {
        key: value for key, value in record.items()
        if key not in ("version_count", "oai_datestamp")
    }
    paper.update(fields)
    return paper


def select_oai_feed_papers(records: list[dict], category: str,
                           rule: dict) -> list[dict]:
    """Turn first-version OAI records of a feed category into new/cross rows.

    Replacements (more than one version) are not new announcements and are
    skipped exactly like RSS ``replace`` rows when include_replacements=false.
    """
    max_delay = int(rule.get("max_announce_delay_days", 90))
    return [
        _paper_from_oai(
            record, source_feed=category,
            announce_type="new" if record["primary"] == category else "cross")
        for record in records
        if record.get("version_count") == 1
        and category in record.get("categories", [])
        and oai_record_is_recent(record, max_delay)
    ]


def select_oai_external_candidates(
        records: list[dict], category: str, rule: dict, max_delay_days: int,
        core_ids: set[str], seen: set[str]) -> list[dict]:
    """Apply the external mechanical filters to first-version OAI records."""
    terms = [str(t) for t in rule.get("terms", [])]
    return [
        _paper_from_oai(record, source_feed=category, announce_type="external")
        for record in records
        if record.get("version_count") == 1
        and oai_record_is_recent(record, max_delay_days)
        and record["id"] not in core_ids
        and record["id"] not in seen
        and record["primary"] == category
        and "quant-ph" not in record.get("categories", [])
        and matches_external_terms(record, terms)
    ]


def parse_recover_ids(raw: str) -> list[str]:
    """Extract unique arXiv IDs (versions dropped) from free-form operator text."""
    ids: list[str] = []
    for pid in re.findall(r"(?<![\d.])\d{4}\.\d{4,5}(?!\d)", raw or ""):
        if pid not in ids:
            ids.append(pid)
    return ids


def fetch_recovery_papers(ids: list[str], category: str,
                          rule: dict) -> tuple[list[dict], list[str]]:
    """Fetch operator-listed missed papers as normal new/cross feed rows.

    Used once to deliver announcements that every automatic source missed.
    Returns ``(papers, rejected)``; an ID is rejected when the API does not
    return it, it is no longer listed in ``category``, or it was withdrawn.
    """
    papers: list[dict] = []
    rejected: list[str] = []
    for start in range(0, len(ids), 100):
        if start:
            time.sleep(3)
        chunk = ids[start:start + 100]
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "id_list": ",".join(chunk), "max_results": len(chunk)})
        raw = http_get_arxiv_api(url, rule)
        rows = {row["id"]: row for row in parse_external_atom(raw, category)}
        withdrawn = set()
        for entry in ET.fromstring(raw).findall("atom:entry", ATOM_NS):
            comment = entry.findtext("arxiv:comment", "", ATOM_NS) or ""
            if re.search(r"\bwithdrawn\b", comment, flags=re.I):
                withdrawn.add(re.sub(r"v\d+$", "", entry.findtext(
                    "atom:id", "", ATOM_NS).strip().rsplit("/", 1)[-1]))
        for pid in chunk:
            row = rows.get(pid)
            if (row is None or pid in withdrawn
                    or category not in row.get("categories", [])):
                rejected.append(pid)
                continue
            row.update(source_feed=category, announce_type=(
                "new" if row["primary"] == category else "cross"))
            papers.append(row)
    return papers, rejected


# ---------------------------------------------------------------- filtering

def category_matches(cat: str, patterns: list[str]) -> bool:
    """'cond-mat.*' style prefix patterns or exact match."""
    for p in patterns:
        if p.endswith(".*"):
            if cat == p[:-2] or cat.startswith(p[:-2] + "."):
                return True
        elif cat == p:
            return True
    return False


def is_quantph_feed_paper(paper: dict) -> bool:
    """Whether normal quant-ph classification semantics apply to a paper.

    RSS rows carry ``source_feed=quant-ph``.  SciRate and legacy audit rows may
    only carry categories (or no source metadata at all), so those are treated
    as quant-ph context unless they are explicitly marked as external.
    """
    if paper.get("announce_type") == "external":
        return False
    source = str(paper.get("source_feed", "")).strip()
    if source:
        return source == "quant-ph"
    categories = [str(cat) for cat in paper.get("categories", [])]
    return not categories or "quant-ph" in categories


def should_post(paper: dict, cfg: dict) -> bool:
    at = paper["announce_type"]
    if at.startswith("replace"):
        return cfg.get("include_replacements", False)
    if at == "new":
        # primary is quant-ph: always a genuine quant-ph paper.
        return True
    if at == "cross":
        # Recall-first policy: a cross-listed paper is DROPPED only when its
        # primary category is on the explicit denylist of fields judged
        # unrelated to quantum information. Everything else passes, so a
        # field we simply forgot to enumerate is kept (favoring recall over
        # precision, as requested). An optional allowlist can override the
        # denylist to force-keep specific primaries.
        primary = paper["primary"]
        if category_matches(primary, cfg.get("cross_allow_primary", [])):
            return True  # explicit keep
        return not category_matches(primary, cfg.get("cross_deny_primary", []))
    return True


_STOPWORDS = frozenset(
    "a an the of in for to and or with on at by as is are was be been "
    "we our this that these which its it also can show based using used "
    "such via from have has had not do does did will would could may "
    "must both only even more most some any all one two new no "
    "quantum qubit qubits state states system systems "
    "blind blindness verifiable verifiability secure securely security "
    "protocol experiment result model method framework".split()
)
_classifier_cache = None  # (genre signature, genre_tf, idf)

# Deliberately conservative domain normalization: unrestricted prefix matching
# confuses "blind" with "blinded", and "code" with unrelated words.  These
# aliases handle common scientific inflections without adding a dependency.
_CLASSIFIER_TOKEN_ALIASES = {
    "correct": "correction", "correcting": "correction",
    "corrected": "correction", "corrections": "correction",
    "decode": "decoding", "decoder": "decoding", "decoders": "decoding",
    "decoded": "decoding", "tolerance": "tolerant",
    "entropies": "entropy", "capacities": "capacity",
    "complexities": "complexity", "inequalities": "inequality",
    "matrices": "matrix", "indices": "index",
    "topological": "topology", "superconducting": "superconductivity",
    "classes": "class", "processes": "process",
}


def _classifier_tokens(text: str) -> list[str]:
    """Keep phrase boundaries and normalize common dash/plural variants."""
    text = text.casefold().translate(str.maketrans("‐‑‒–—−", "------"))
    words = re.findall(r"[a-z][a-z0-9]*", text)
    normalized = []
    for word in words:
        if word in _CLASSIFIER_TOKEN_ALIASES:
            word = _CLASSIFIER_TOKEN_ALIASES[word]
        elif len(word) > 3 and word.endswith("s") and not word.endswith(
                ("ss", "us", "is", "ics")):
            word = word[:-1]
        normalized.append(word)
    return normalized


def _tokenize(text: str) -> list[str]:
    return [w for w in _classifier_tokens(text)
            if w not in _STOPWORDS and len(w) > 2]


def _keyword_tokens(keywords: list[str]) -> list[str]:
    """Use single-token keywords in TF-IDF; phrases are scored separately."""
    tokens: list[str] = []
    for keyword in keywords:
        key = str(keyword).strip().lower()
        if re.search(r"[\s-]", key):
            continue
        tokens.append(key)
    return tokens


def _build_tfidf(genres: list[dict]) -> tuple[dict, dict]:
    """Build TF vectors from English classifier descriptions and keywords.

    Terms appearing in all genres get IDF=0 (e.g. "quantum"), so only
    discriminative vocabulary contributes to similarity scores.
    """
    tf: dict[str, Counter] = {}
    for g in genres:
        description = g.get("classifier_description", g.get("description", ""))
        words = _tokenize(
            f"{description} {' '.join(_keyword_tokens(g.get('keywords', [])))}")
        # Repeating an acronym in a description and keyword list is not
        # independent evidence about that genre.
        tf[g["id"]] = Counter(set(words))
    N = len(tf)
    df: dict[str, int] = {}
    for vec in tf.values():
        for term in vec:
            df[term] = df.get(term, 0) + 1
    idf = {t: math.log(N / d) for t, d in df.items() if d < N}
    return tf, idf


def _phrase_spans(tokens: list[str], phrase: tuple[str, ...]) -> list[tuple[int, int]]:
    size = len(phrase)
    if not size:
        return []
    return [(i, i + size) for i in range(len(tokens) - size + 1)
            if tuple(tokens[i:i + size]) == phrase]


def _keyword_evidence_scores(paper: dict, genres: list[dict],
                             cfg: dict | None = None) -> dict[str, float]:
    title = _classifier_tokens(paper.get("title", ""))
    abstract = _classifier_tokens(paper.get("abstract", ""))
    scores = {g["id"]: 0.0 for g in genres}

    title_phrase = cfg.get("fallback_title_phrase_bonus", 0.35) if cfg else 0.35
    abstract_phrase = cfg.get("fallback_abstract_phrase_bonus", 0.18) if cfg else 0.18
    title_token = cfg.get("fallback_title_token_bonus", 0.10) if cfg else 0.10
    abstract_token = cfg.get("fallback_abstract_token_bonus", 0.03) if cfg else 0.03

    for g in genres:
        gid = g["id"]
        keywords = [*g.get("keywords", []), *(cfg or {}).get(
            "fallback_keyword_boosts", {}).get(gid, [])]
        phrases = {tuple(_classifier_tokens(str(key))) for key in keywords}
        evidence = []
        for phrase in phrases:
            if not phrase or (len(phrase) == 1 and phrase[0] in _STOPWORDS):
                continue
            spans = _phrase_spans(title, phrase)
            section = "title"
            if not spans:
                spans = _phrase_spans(abstract, phrase)
                section = "abstract"
            if spans:
                phrase_bonus = title_phrase if section == "title" else abstract_phrase
                token_bonus = title_token if section == "title" else abstract_token
                evidence.append((len(phrase), phrase_bonus if len(phrase) > 1
                                 else token_bonus, section, spans))
        # Count a phrase, its plural/spelling variants and contained keywords
        # once. Distinct, non-overlapping contributions still add evidence.
        occupied: dict[str, set[int]] = {"title": set(), "abstract": set()}
        for _length, bonus, section, spans in sorted(
                evidence, key=lambda row: (-row[0], -row[1], row[2], row[3])):
            for start, end in spans:
                positions = set(range(start, end))
                if not positions & occupied[section]:
                    scores[gid] += bonus
                    occupied[section].update(positions)
                    break

    return scores


def _score_genres(paper: dict, genres: list[dict],
                  cfg: dict | None = None) -> dict[str, float]:
    """Compute TF-IDF cosine similarity + category hint scores for each genre."""
    global _classifier_cache
    signature = tuple((g["id"], g.get("classifier_description", g.get(
        "description", "")), tuple(g.get("keywords", []))) for g in genres)
    if _classifier_cache is None or _classifier_cache[0] != signature:
        _classifier_cache = (signature, *_build_tfidf(genres))
    _, genre_tf, idf = _classifier_cache

    title_weight = (cfg or {}).get("fallback_title_weight", 2.0)
    terms = Counter(_tokenize(paper.get("abstract", "")))
    for token, count in Counter(_tokenize(paper.get("title", ""))).items():
        terms[token] += title_weight * count
    paper_vec = {k: (1 + math.log(v)) * idf.get(k, 0.0)
                 for k, v in terms.items()}
    norm_p = math.sqrt(sum(v ** 2 for v in paper_vec.values())) or 1.0

    scores: dict[str, float] = {}
    for gid, gtf in genre_tf.items():
        gvec = {k: v * idf.get(k, 0.0) for k, v in gtf.items()}
        dot = sum(paper_vec.get(k, 0.0) * v for k, v in gvec.items())
        norm_g = math.sqrt(sum(v ** 2 for v in gvec.values())) or 1.0
        scores[gid] = dot / (norm_p * norm_g)

    if cfg:
        hints = cfg.get("category_genre_hints", {})
        strong_other = set(cfg.get("category_other_overrides", []))
        hinted: set[str] = set()
        for cat in paper.get("categories", []):
            if cat in strong_other and "other" in scores:
                hinted.add("other")
            else:
                gid = hints.get(cat)
                if gid and gid in scores and scores[gid] > 0:
                    hinted.add(gid)
        for gid in hinted:
            scores[gid] += 1.0 if gid == "other" else 0.15
        keyword_scores = _keyword_evidence_scores(paper, genres, cfg)
        for gid, score in keyword_scores.items():
            scores[gid] = scores.get(gid, 0.0) + score

    return scores


def classify(paper: dict, genres: list[dict], cfg: dict | None = None) -> dict | None:
    """Return single best-matching genre, or None if below threshold."""
    scores = _score_genres(paper, genres, cfg)
    genre_map = {g["id"]: g for g in genres}
    min_score = cfg.get("classify_min_score", 0.05) if cfg else 0.05
    best_id = max(scores, key=lambda k: scores[k]) if scores else None
    if best_id and scores.get(best_id, 0) >= min_score:
        return genre_map.get(best_id)
    return None


def classify_multi(paper: dict, genres: list[dict],
                   cfg: dict | None = None, *,
                   apply_policy: bool = True) -> list[dict]:
    """Return up to classify_max_genres genres, score-ordered.

    The primary genre must exceed classify_min_score.
    Each additional genre must also exceed min_score AND be at least
    classify_secondary_ratio (default 0.7) times the primary score,
    ensuring only genuinely multi-topic papers get multiple genres.
    Falls back to ['other'] when nothing scores high enough.
    """
    scores = _score_genres(paper, genres, cfg)
    genre_map = {g["id"]: g for g in genres}
    min_score = cfg.get("classify_min_score", 0.05) if cfg else 0.05
    max_genres = cfg.get("classify_max_genres", 2) if cfg else 2
    sec_ratio = cfg.get("classify_secondary_ratio", 0.7) if cfg else 0.7

    ranked = sorted(
        [gid for gid, s in scores.items() if s >= min_score],
        key=lambda gid: -scores[gid],
    )
    fallback = genre_by_id(None, genres)
    if not ranked:
        return [fallback] if fallback else []

    best_score = scores[ranked[0]]
    keyword_evidence = _keyword_evidence_scores(paper, genres, cfg)
    secondary_evidence = (cfg or {}).get("fallback_secondary_evidence_min", 0.35)
    result: list[dict] = []
    for gid in ranked[:max_genres]:
        if gid not in genre_map:
            continue
        if (result and scores[gid] < best_score * sec_ratio
                and keyword_evidence.get(gid, 0.0) < secondary_evidence):
            continue
        result.append(genre_map[gid])
    result = result if result else ([fallback] if fallback else [])
    return postprocess_genres(paper, result, genres, cfg) if apply_policy else result


def postprocess_genres(paper: dict, selected: list[dict | None],
                       genres: list[dict], cfg: dict | None = None) -> list[dict]:
    """Apply deterministic category and keyword overrides after classification."""
    result = [g for g in selected if g]
    if not cfg:
        return result

    primary = paper.get("primary", "")
    if not is_quantph_feed_paper(paper):
        quantph_equivalent = cfg.get(
            "cross_classify_primary_as_quantph", ["quant-ph", "cs.CR"])
        if primary and not category_matches(primary, quantph_equivalent):
            fallback = genre_by_id("other", genres)
            return [fallback] if fallback else result

        if category_matches(primary, cfg.get("category_other_overrides", [])):
            fallback = genre_by_id("other", genres)
            return [fallback] if fallback else result

    result = apply_forced_genres(paper, result, genres, cfg)
    if (
        is_qec_adjacent_coding_paper(paper, cfg)
        and not any(g.get("id") == "qec" for g in result)
    ):
        qec = genre_by_id("qec", genres)
        if qec:
            result.append(qec)
    return result


def apply_forced_genres(paper: dict, selected: list[dict | None],
                        genres: list[dict], cfg: dict | None = None) -> list[dict]:
    """Add configured genres when high-signal keywords appear in title/abstract."""
    result = [g for g in selected if g]
    if not cfg:
        return result

    genre_map = {g["id"]: g for g in genres}
    selected_ids = {g["id"] for g in result}
    for gid, keywords in cfg.get("force_genre_keywords", {}).items():
        if gid in selected_ids or gid not in genre_map:
            continue
        if _matched_forced_terms(paper, keywords):
            result = [g for g in result if g.get("id") != "other"]
            result.append(genre_map[gid])
            selected_ids.add(gid)
    return result


def _matched_forced_terms(paper: dict, keywords: list[str]) -> list[str]:
    texts = [_classifier_tokens(paper.get(field, ""))
             for field in ("title", "abstract")]
    return [str(keyword) for keyword in keywords
            if any(_phrase_spans(tokens, tuple(_classifier_tokens(str(keyword))))
                   for tokens in texts)]


def deterministic_genre_trace(
        paper: dict, base_genres: list, final_genres: list, cfg: dict) -> list[dict]:
    """Explain actual deterministic changes; accept genre dicts or genre IDs.

    This is observational only: it never changes labels or calls a provider.
    The supplied final labels constrain the trace, so an external route that
    deliberately bypasses the primary-category override is not misreported.
    """
    def ids(values: list) -> list[str]:
        return list(dict.fromkeys(str(value.get("id")) if isinstance(value, dict)
                                  else str(value) for value in values if value))

    current, final = ids(base_genres), ids(final_genres)
    trace: list[dict] = []

    def record(rule: str, terms: list[str], updated: list[str]) -> None:
        nonlocal current
        added = [gid for gid in updated if gid not in current]
        removed = [gid for gid in current if gid not in updated]
        if added or removed:
            trace.append({"rule": rule, "matched_terms": terms,
                          "added_genre_ids": added, "removed_genre_ids": removed})
        current = updated

    primary = paper.get("primary", "")
    if not is_quantph_feed_paper(paper) and final == ["other"]:
        equivalent = cfg.get("cross_classify_primary_as_quantph", ["quant-ph", "cs.CR"])
        if primary and not category_matches(primary, equivalent):
            record("primary_category_other", [primary], ["other"])
            return trace
        if category_matches(primary, cfg.get("category_other_overrides", [])):
            record("category_other_override", [primary], ["other"])
            return trace

    for gid, keywords in cfg.get("force_genre_keywords", {}).items():
        if gid not in final or gid in current:
            continue
        matched = _matched_forced_terms(paper, keywords)
        if matched:
            record(f"force_genre_keywords.{gid}", matched,
                   [g for g in current if g != "other"] + [gid])
    if "qec" in final and "qec" not in current:
        matched = [str(term) for term in cfg.get("qec_adjacent_coding_terms", [])
                   if matches_external_terms(paper, [str(term)])]
        if matched:
            record("qec_adjacent_coding", matched, [*current, "qec"])
    return trace


def genre_by_id(genre_id: str | None, genres: list[dict]) -> dict | None:
    """Map an LLM-returned genre id to its genre dict.

    Unknown or missing ids fall back to the 'other' genre if defined,
    so DISCORD_WEBHOOK_GENERAL is only a last-resort safety net.
    """
    if genre_id:
        for g in genres:
            if g.get("id") == genre_id:
                return g
    for g in genres:
        if g.get("id") == "other":
            return g
    return None


# ------------------------------------------------------------- translation

_last_gemini_calls: dict[str, float] = {}   # per-model request pacing
_gemini_dead_models: set[str] = set()       # models given up for this run
_gemini_fail_streaks: dict[str, int] = {}   # per-model overload streaks
_last_openai_compat_calls: dict[str, float] = {}
_openai_compat_dead_models: set[str] = set()

BATCH_TAG = re.compile(r"<<<(\d+)>>>")
# Tag form for the combined translate+classify call: <<<k|genre_id>>> or <<<k|id1,id2>>>
BATCH_TAG_CLS = re.compile(r"<<<(\d+)\s*\|\s*([A-Za-z0-9_,\s]+?)>>>")


def gemini_min_interval(cfg: dict, model: str) -> float:
    """Per-model pacing; actual free quotas remain project-specific."""
    intervals = cfg.get("gemini_min_intervals", {})
    if model in intervals:
        return float(intervals[model])
    base = float(cfg.get("gemini_min_interval_sec", 7))
    return base


def _gemini_request(prompt: str, cfg: dict, model: str | None = None) -> str | None:
    """One paced, retried Gemini call. Marks the model dead for this run on
    persistent quota exhaustion (429) or sustained server overload (500/503);
    other models keep working."""
    model = model or cfg.get("gemini_model", "gemini-3.5-flash-lite")
    if model in _gemini_dead_models:
        return None
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    min_interval = gemini_min_interval(cfg, model)
    max_retries = cfg.get("gemini_max_retries", 4)
    timeout = max(1.0, float(cfg.get("gemini_timeout_sec", 45)))
    budget = max(1.0, float(cfg.get("gemini_request_budget_sec", 100)))
    transport_retries = max(0, int(cfg.get("gemini_transport_retries", 1)))
    transport_retries_used = 0
    started = time.monotonic()
    deadline = started + budget

    for attempt in range(max_retries + 1):
        remaining = deadline - time.monotonic()
        wait = _last_gemini_calls.get(model, 0.0) + min_interval - time.time()
        if remaining <= max(0, wait):
            print(f"[warn] Gemini {model} request budget exhausted; "
                  "using the next classifier", file=sys.stderr)
            _gemini_dead_models.add(model)
            return None
        if wait > 0:
            time.sleep(wait)
        _last_gemini_calls[model] = time.time()

        generation = {
            "maxOutputTokens": int(cfg.get("gemini_max_output_tokens", 4096)),
        }
        thinking = cfg.get("gemini_thinking_levels", {}).get(model)
        if thinking:
            generation["thinkingConfig"] = {"thinkingLevel": thinking}
        status, body = http_post_json(url, {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }, timeout=min(timeout, max(0.1, deadline - time.monotonic())))
        if status == 200:
            _gemini_fail_streaks[model] = 0
            try:
                data = json.loads(body)
                candidate = data["candidates"][0]
                if candidate.get("finishReason", "STOP") != "STOP":
                    raise ValueError("incomplete or blocked Gemini response")
                parts = candidate["content"]["parts"]
                output = "".join(
                    p["text"] for p in parts if isinstance(p, dict)
                    and isinstance(p.get("text"), str) and not p.get("thought"))
                if not output.strip():
                    raise ValueError("empty Gemini response")
                usage = data.get("usageMetadata", {})
                counts = {
                    k: v for k, v in usage.items()
                    if k in {"promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount"}
                    and isinstance(v, int)
                } if isinstance(usage, dict) else {}
                print(f"[info] Gemini {model} response: "
                      f"elapsed={time.monotonic() - started:.1f}s, usage={counts}")
                return output.strip()
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                save_error_diagnostic(
                    "invalid_response", url=url, method="POST", status=200,
                    body=body)
                return None
        daily_quota = False
        retry_after = None
        if status == 429:
            try:
                details = json.loads(body).get("error", {}).get("details", [])
                for detail in details:
                    for violation in detail.get("violations", []):
                        quota_id = str(violation.get("quotaId", "")).lower()
                        daily_quota |= "perday" in quota_id or "per_day" in quota_id
                    delay = str(detail.get("retryDelay", "")).removesuffix("s")
                    if delay:
                        retry_after = float(delay)
                # Some quota errors only identify the exhausted daily limit in text.
                message = json.loads(body).get("error", {}).get("message", "").lower()
                daily_quota |= "per day" in message or "requestsperday" in message
            except (TypeError, ValueError, AttributeError):
                pass
        retryable = status in (0, 408, 429, 500, 502, 503, 504)
        can_retry_transport = status != 0 or transport_retries_used < transport_retries
        if (retryable and not daily_quota and can_retry_transport
                and attempt < max_retries):
            backoff = min(60, 10 * (2 ** attempt))
            if retry_after is not None:
                if retry_after > 60:
                    _gemini_dead_models.add(model)
                    return None
                backoff = max(backoff, retry_after)
            if backoff >= deadline - time.monotonic():
                print(f"[warn] Gemini {model} retry would exceed request budget; "
                      "using the next classifier", file=sys.stderr)
                _gemini_dead_models.add(model)
                return None
            if status == 0:
                transport_retries_used += 1
            print(f"[warn] Gemini {model} HTTP {status}; retry in {backoff}s "
                  f"({attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(backoff)
            continue
        # Full backoff exhausted (or a non-retryable status).
        if status == 429:
            # Daily quota (requests per day) exhausted -- or a model with no
            # free-tier quota at all (429 with "limit: 0" on the first call).
            # Either way it will not recover today.
            print(f"[warn] Gemini {model} quota exhausted or unavailable "
                  f"({body[:300]!r}); skipping this model for the rest of "
                  "this run.", file=sys.stderr)
            _gemini_dead_models.add(model)
        elif status == 0:
            print(f"[warn] Gemini {model} transport retries exhausted after "
                  f"{time.monotonic() - started:.1f}s; skipping this model "
                  "for the rest of this run.", file=sys.stderr)
            _gemini_dead_models.add(model)
        elif status in (408, 500, 502, 503, 504):
            # Server overload. One request surviving full backoff is bad
            # enough; if it keeps happening, stop hammering this model for
            # the run and let callers fall through to the next model.
            streak = _gemini_fail_streaks.get(model, 0) + 1
            _gemini_fail_streaks[model] = streak
            print(f"[warn] Gemini {model} HTTP {status} after full backoff "
                  f"(streak {streak}/"
                  f"{cfg.get('gemini_overload_giveup', 2)})", file=sys.stderr)
            if streak >= cfg.get("gemini_overload_giveup", 2):
                print(f"[warn] Gemini {model} appears overloaded; skipping "
                      "this model for the rest of this run.", file=sys.stderr)
                _gemini_dead_models.add(model)
        else:
            if status in (400, 401, 403, 404):
                _gemini_dead_models.add(model)
            print(f"[warn] Gemini {model} HTTP {status}: {body[:200]!r}",
                  file=sys.stderr)
        return None
    return None


def _openai_compat_request(prompt: str, cfg: dict, spec: dict) -> str | None:
    """One OpenAI-compatible chat.completions call for classification fallback."""
    model = str(spec.get("model", "")).strip()
    name = str(spec.get("name") or model or "openai-compatible").strip()
    if not model:
        return None
    if name in _openai_compat_dead_models:
        return None
    key_env = str(spec.get("api_key_env", "OPENAI_COMPAT_API_KEY")).strip()
    key = os.environ.get(key_env, "")
    if not key:
        return None
    base_url = str(spec.get("base_url", "")).rstrip("/")
    if not base_url:
        print(f"[warn] {name} fallback missing base_url", file=sys.stderr)
        _openai_compat_dead_models.add(name)
        return None
    min_interval = float(spec.get("min_interval_sec", 5))
    max_retries = int(spec.get("max_retries", cfg.get("gemini_max_retries", 4)))
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "arxiv-quantph-bot-classifier/1.0",
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You classify quantum-information arXiv papers. "
                    "Return only the requested tags and genre IDs."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": float(spec.get("temperature", 0)),
        "max_tokens": int(spec.get("max_tokens", 256)),
    }

    for attempt in range(max_retries + 1):
        wait = _last_openai_compat_calls.get(name, 0.0) + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_openai_compat_calls[name] = time.time()

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw_body = read_http_error_body(exc)
            body = raw_body.decode("utf-8", errors="replace")
            save_error_diagnostic(
                "http_error", url=url, method="POST", status=exc.code,
                reason=exc.reason, headers=exc.headers, body=raw_body,
                exception=exc)
            if exc.code in (429, 500, 503) and attempt < max_retries:
                backoff = min(60, 10 * (2 ** attempt))
                print(f"[warn] {name} HTTP {exc.code}; retry in {backoff}s "
                      f"({attempt + 1}/{max_retries})", file=sys.stderr)
                time.sleep(backoff)
                continue
            if exc.code == 429:
                print(f"[warn] {name} quota/rate limit exhausted "
                      f"({body[:500]!r}); skipping this model for the rest "
                      "of this run.", file=sys.stderr)
                _openai_compat_dead_models.add(name)
            else:
                print(f"[warn] {name} HTTP {exc.code}: {body[:500]!r}",
                      file=sys.stderr)
            return None
        except urllib.error.URLError as exc:
            save_error_diagnostic(
                "connection_error", url=url, method="POST",
                reason=exc.reason, exception=exc)
            print(f"[warn] {name} request failed: {exc}", file=sys.stderr)
            return None

        try:
            data = json.loads(body)
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if (not isinstance(content, str) or not content.strip()
                    or choice.get("finish_reason", "stop") != "stop"):
                raise ValueError("empty or incomplete classifier response")
            return content.strip()
        except (KeyError, IndexError, TypeError, ValueError):
            save_error_diagnostic(
                "invalid_response", url=url, method="POST", status=200,
                body=body)
            print(f"[warn] {name} returned an unexpected response: "
                  f"{body[:500]!r}", file=sys.stderr)
            return None
    return None


def classifier_model_specs(cfg: dict) -> list[dict]:
    primary = cfg.get("gemini_model_primary") or cfg.get(
        "gemini_model", "gemini-3.5-flash-lite")
    secondary = cfg.get("gemini_model_secondary") or primary
    specs: list[dict] = [
        {"provider": "gemini", "model": primary, "name": primary},
    ]
    if secondary != primary:
        specs.append({"provider": "gemini", "model": secondary, "name": secondary})
    for spec in cfg.get("llm_classification_fallbacks", []):
        if isinstance(spec, dict):
            specs.append(spec)
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for spec in specs:
        provider = str(spec.get("provider", "gemini"))
        model = str(spec.get("model", ""))
        key = (provider, model)
        if model and key not in seen:
            result.append(spec)
            seen.add(key)
    return result


def classifier_spec_name(spec: dict) -> str:
    return str(spec.get("name") or spec.get("model") or "unknown")


def classifier_key_present(spec: dict) -> bool:
    provider = str(spec.get("provider", "gemini"))
    if provider == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY", ""))
    key_env = str(spec.get("api_key_env", "OPENAI_COMPAT_API_KEY"))
    return bool(os.environ.get(key_env, ""))


def classifier_dead(spec: dict) -> bool:
    provider = str(spec.get("provider", "gemini"))
    name = classifier_spec_name(spec)
    if provider == "gemini":
        return str(spec.get("model", "")) in _gemini_dead_models
    return name in _openai_compat_dead_models


def classification_policy_fingerprint(cfg: dict) -> str:
    """Version cached decisions when models, descriptions or routing change."""
    keys = (
        "genres", "force_genre_keywords", "qec_adjacent_coding_terms",
        "external_arxiv_queries", "classify_max_genres",
        "external_classify_max_genres", "external_skip_consensus",
        "category_other_overrides", "cross_classify_primary_as_quantph",
        "category_genre_hints", "fallback_keyword_boosts",
    )
    policy = {key: cfg.get(key) for key in keys}
    policy.update(version=2, models=classifier_model_specs(cfg))
    return hashlib.sha256(json.dumps(
        policy, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]


def classification_record(paper: dict, base_genres: list,
                          final_genres: list, cfg: dict) -> dict:
    def ids(values: list) -> list[str]:
        return [g["id"] if isinstance(g, dict) else g for g in values if g]
    return {
        "policy": classification_policy_fingerprint(cfg),
        "base_genre_ids": ids(base_genres),
        "final_genre_ids": ids(final_genres),
        "postprocess": deterministic_genre_trace(
            paper, base_genres, final_genres, cfg),
    }


def external_cache_is_current(review: Any, cfg: dict) -> bool:
    if not isinstance(review, dict) or "genre_ids" not in review:
        return False
    # Already accepted/delivered papers are never automatically reposted.
    return (bool(review.get("genre_ids"))
            or not cfg.get("revalidate_external_skips", False)
            or review.get("policy") == classification_policy_fingerprint(cfg))


def target_language(cfg: dict) -> str:
    return str(cfg.get("target_language", "ja")).strip() or "ja"


def target_language_name(cfg: dict) -> str:
    code = target_language(cfg)
    default_names = {
        "ja": "Japanese",
        "fr": "French",
        "de": "German",
        "es": "Spanish",
        "it": "Italian",
        "ko": "Korean",
        "zh-cn": "Simplified Chinese",
        "zh-tw": "Traditional Chinese",
    }
    configured = str(cfg.get("target_language_name", "")).strip()
    if configured and not (
        configured == "Japanese" and code.lower() != "ja"
    ):
        return configured
    return default_names.get(code.lower(), code)


def deepl_target_language(cfg: dict) -> str:
    code = str(cfg.get("deepl_target_language", target_language(cfg))).strip()
    return code.upper() or "JA"


def google_target_language(cfg: dict) -> str:
    return str(cfg.get("google_target_language", target_language(cfg))).strip() or "ja"


def azure_target_language(cfg: dict) -> str:
    return str(cfg.get("azure_target_language", target_language(cfg))).strip() or "ja"


def show_translated_title(cfg: dict) -> bool:
    return cfg.get("show_translated_title",
                   cfg.get("show_japanese_title", True))


def translated_title_label(cfg: dict) -> str:
    default = "邦題" if target_language(cfg).lower() == "ja" else "Translated title"
    configured = str(cfg.get("translated_title_label", "")).strip()
    if configured and not (
        configured == "邦題" and target_language(cfg).lower() != "ja"
    ):
        return configured
    return default


def translation_log_matches(entry: dict, cfg: dict) -> bool:
    return entry.get("translation_language", "ja") == target_language(cfg)


def log_title_translation(entry: dict) -> str | None:
    return entry.get("title_translated") or entry.get("title_ja")


def log_abstract_translation(entry: dict) -> str | None:
    return entry.get("abstract_translated") or entry.get("abstract_ja")


def translate_gemini_batch(texts: list[str], cfg: dict) -> list[str | None]:
    """Translate several abstracts in one request using <<<k>>> delimiters."""
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    lang_name = target_language_name(cfg)
    prompt = (
        f"Below are {len(texts)} English abstracts from arXiv papers in "
        "quantum information science. Translate each abstract into "
        f"scholarly {lang_name}, using standard technical terminology "
        "and keeping the English term in parentheses when helpful.\n"
        "In the output, place the matching number tag <<<k>>> immediately "
        "before each translated abstract. The final AI output must be in "
        f"{lang_name}; include nothing except the tags and translated text "
        "(no preface or afterword).\n\n"
        + numbered
    )
    out = _gemini_request(prompt, cfg)
    results: list[str | None] = [None] * len(texts)
    if not out:
        return results
    parts = BATCH_TAG.split(out)
    # parts = [preamble, '1', text1, '2', text2, ...]
    for k_str, body in zip(parts[1::2], parts[2::2]):
        try:
            k = int(k_str) - 1
        except ValueError:
            continue
        if 0 <= k < len(texts):
            t = body.strip()
            if t:
                results[k] = t
    return results


def _genre_menu(genres: list[dict]) -> str:
    lines = []
    for g in genres:
        lines.append(f"- {g['id']}: {g.get('description', g['name'])}")
    return "\n".join(lines)


def normalize_genre_ids(raw_ids: list[str], valid_ids: set[str]) -> list[str]:
    """Keep valid genre IDs in model order while removing duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    for gid in raw_ids:
        gid = gid.strip()
        if gid in valid_ids and gid not in seen:
            result.append(gid)
            seen.add(gid)
    return result


def _classification_prompt(texts: list[str], cfg: dict,
                           genres: list[dict]) -> str:
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    max_genres = cfg.get("classify_max_genres", 2)
    return (
        f"Below are {len(texts)} English titles and abstracts from arXiv "
        "papers in quantum information science. Each genre below is a "
        "Discord channel followed by researchers of that area. For each "
        "paper, carefully read every genre description and choose the "
        "genre IDs of the channels where the paper should be posted.\n"
        + _classification_rules(max_genres)
        + "\n[Genre list]\n"
        + _genre_menu(genres)
        + "\n\n[Output format]\n"
        "For each entry, output only the genre ID immediately after <<<k>>>. "
        "For multiple genres, separate IDs with commas "
        "(example: <<<1>>> qec,ft). Include nothing except genre IDs, tags, "
        "and newlines.\n\n"
        + numbered
    )


def _parse_classification_output(out: str | None, count: int,
                                 valid_ids: set[str]) -> list[list[str]]:
    results: list[list[str]] = [[] for _ in range(count)]
    if not out:
        return results
    pattern = r"<<<(\d+)>>>[\s:-]*([A-Za-z0-9_][A-Za-z0-9_,\s]*)"
    for match in re.finditer(pattern, out):
        try:
            k = int(match.group(1)) - 1
        except ValueError:
            continue
        if 0 <= k < count:
            gids = [g.strip() for g in match.group(2).split(",")]
            results[k] = normalize_genre_ids(gids, valid_ids)
    return results


def translate_classify_gemini_batch(
        texts: list[str], cfg: dict,
        genres: list[dict]) -> list[tuple[str | None, list[str]]]:
    """Translate AND classify several abstracts in a single Gemini request.

    Returns a list of (translated_text, genre_ids) tuples.
    genre_ids is a list of 1-N valid genre id strings (empty on failure).
    """
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    valid_ids = {g["id"] for g in genres}
    max_genres = cfg.get("classify_max_genres", 2)
    lang_name = target_language_name(cfg)
    prompt = (
        f"Below are {len(texts)} English titles and abstracts from arXiv "
        "papers in quantum information science. For each paper, perform "
        "the following two tasks.\n"
        "(1) Carefully read every genre description and choose the genre "
        "IDs from the list below. Each genre is a Discord channel followed "
        "by researchers of that area.\n"
        + _classification_rules(max_genres)
        + "(2) Translate the abstract into scholarly "
        f"{lang_name}, using standard technical terminology and keeping "
        "the English term in parentheses when helpful.\n\n"
        "[Genre list]\n"
        + _genre_menu(genres)
        + "\n\n[Output format]\n"
        "Place <<<k|genre_id>>> immediately before the translated abstract "
        "for each entry. For multiple genres, use <<<k|id1,id2>>> "
        "(example: <<<1|qec,ft>>>). The final AI output must be in "
        f"{lang_name}; include nothing except the tags and translated text "
        "(no preface, afterword, or headings).\n\n"
        + numbered
    )
    out = _gemini_request(prompt, cfg)
    results: list[tuple[str | None, list[str]]] = [(None, [])] * len(texts)
    if not out:
        return results
    parts = BATCH_TAG_CLS.split(out)
    # parts = [preamble, '1', 'qec,ft', text1, '2', 'algo', text2, ...]
    for k_str, gids_str, body in zip(parts[1::3], parts[2::3], parts[3::3]):
        try:
            k = int(k_str) - 1
        except ValueError:
            continue
        if 0 <= k < len(texts):
            t = body.strip()
            gids = [g.strip() for g in gids_str.split(",")]
            gids = normalize_genre_ids(gids, valid_ids)
            results[k] = (t or None, gids)
    return results


def _classification_rules(max_genres: int) -> str:
    """Shared multi-label criterion: route a paper to every channel whose
    researchers would genuinely want to read it, but never for genres that
    are merely used as a tool or demonstration platform."""
    return (
        "    - First choose the genre ID of the paper's primary "
        "contribution.\n"
        f"    - Additionally choose more genres (up to {max_genres} total, "
        "in priority order, separated by commas, example: qec,ft) whenever "
        "the paper also has genuine value for researchers who follow that "
        "genre -- i.e. they would want to read it even though it is not the "
        "primary topic. Example: a paper that constructs or analyzes "
        "error-correcting codes in order to realize transversal or "
        "fault-tolerant logic belongs to BOTH qec and ft.\n"
        "    - Treat qec as a broad coding-theory channel. Include qec for "
        "a substantive classical or quantum coding-theory contribution "
        "whenever it has any non-incidental quantum, quantum-communication, "
        "or post-quantum-cryptography connection; the code itself does not "
        "have to be a quantum error-correcting code.\n"
        "    - Do NOT add a genre whose subject is merely used as a tool, "
        "platform, or demonstration. Example: a paper that simply runs a "
        "known algorithm on quantum hardware is hardware, not algo; "
        "routine use of entanglement measures does not make a paper qit.\n"
        "    - If unsure, choose one genre. If the paper does not fit any "
        "description, choose other.\n"
    )


def classify_gemini_batch(
        texts: list[str], cfg: dict,
        genres: list[dict], model: str | None = None) -> list[list[str]]:
    """Classify papers using Gemini without translating (classification only).

    Visible output is limited to genre IDs. Input and thinking tokens and
    request counts still consume quota, so short labels do not guarantee
    capacity. Translation uses its separate configured backend chain.

    `model` overrides the Gemini model (defaults to gemini_model_primary,
    then gemini_model). Returns a list of genre ID lists (empty list when
    Gemini fails for that entry).
    """
    model = model or cfg.get("gemini_model_primary") or cfg.get(
        "gemini_model", "gemini-3.5-flash-lite")
    valid_ids = {g["id"] for g in genres}
    prompt = _classification_prompt(texts, cfg, genres)
    out = _gemini_request(prompt, cfg, model=model)
    return _parse_classification_output(out, len(texts), valid_ids)


def classify_llm_batch(
        texts: list[str], cfg: dict,
        genres: list[dict], spec: dict) -> list[list[str]]:
    provider = str(spec.get("provider", "gemini"))
    if provider == "gemini":
        return classify_gemini_batch(texts, cfg, genres, model=spec.get("model"))
    if provider == "openai_compatible":
        valid_ids = {g["id"] for g in genres}
        prompt = _classification_prompt(texts, cfg, genres)
        out = _openai_compat_request(prompt, cfg, spec)
        return _parse_classification_output(out, len(texts), valid_ids)
    print(f"[warn] unknown classifier provider '{provider}'", file=sys.stderr)
    return [[] for _ in texts]


def external_allowed_genre_ids(rule: dict,
                               genres: list[dict]) -> list[str]:
    """Resolve hard output choices; source mappings may remain soft hints."""
    genre_ids = [str(g["id"]) for g in genres]
    excluded = {str(gid) for gid in rule.get("excluded_genres", ["other"])}
    if rule.get("allow_all_genres", False):
        return [gid for gid in genre_ids if gid not in excluded]
    requested = {str(gid) for gid in rule.get("candidate_genres", [])}
    return [
        gid for gid in genre_ids
        if gid in requested and gid not in excluded
    ]


def _external_review_prompt(texts: list[str], cfg: dict,
                            genres: list[dict], category: str,
                            rule: dict) -> str:
    """Strict adjacent-category prompt with an explicit non-posting class."""
    genre_map = {g["id"]: g for g in genres}
    allowed_ids = external_allowed_genre_ids(rule, genres)
    allowed_genres = [genre_map[gid] for gid in allowed_ids]
    likely_ids = [
        str(gid) for gid in rule.get("candidate_genres", [])
        if str(gid) in allowed_ids
    ]
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{text}" for i, text in enumerate(texts))
    max_genres = min(
        max(1, int(cfg.get(
            "external_classify_max_genres",
            cfg.get("classify_max_genres", 2)))),
        max(1, len(allowed_ids)),
    )
    instructions = str(rule.get("review_instructions", "")).strip()
    return (
        f"Below are {len(texts)} papers retrieved by a deliberately "
        f"recall-oriented keyword query from arXiv category {category}. "
        "They have NOT yet been accepted for a quantum-information Discord "
        "server. This review is optimized for COMPLETENESS: missing a relevant "
        "paper is substantially worse than posting a borderline one.\n\n"
        "For each paper, choose every allowed channel where the paper makes "
        "at least one SUBSTANTIVE new contribution or concrete application "
        "that researchers following that channel would value. The channel "
        "topic does not need to be the paper's single primary field. New "
        "theorems, constructions, algorithms, protocols, implementations, "
        "experiments, attacks, benchmarks, or nontrivial applications count. "
        "Choose skip ONLY when the apparent connection occurs solely in "
        "background, motivation, future work, citations, or a comparison, "
        "with no substantive result for any allowed channel. When uncertain, "
        "prefer the most plausible allowed genre instead of skip.\n"
        f"Choose at most {max_genres} allowed genre IDs. Never output a "
        "genre outside this allowed list. The token skip must appear alone.\n"
        + (f"The source category most often maps to {', '.join(likely_ids)}, "
           "but these are soft hints only; use any allowed channel supported "
           "by the paper's actual contributions.\n" if likely_ids else "")
        + (f"\n[Source-specific criteria]\n{instructions}\n" if instructions
           else "")
        + "\n[Allowed channels]\n"
        + _genre_menu(allowed_genres)
        + "\n- skip: Do not post this paper to any channel.\n"
        "\n[Output format]\n"
        "For every entry output only <<<k>>> followed by allowed genre IDs "
        "separated by commas, or skip. Examples: <<<1>>> pqc and "
        "<<<2>>> skip. Include every input number exactly once and include "
        "no explanation, headings, or other text.\n\n"
        + numbered
    )


def classify_external_llm_batch(
        texts: list[str], cfg: dict, genres: list[dict],
        category: str, rule: dict, spec: dict) -> list[list[str]]:
    """Classify adjacent-category candidates as allowed genres or ``skip``."""
    allowed_ids = set(external_allowed_genre_ids(rule, genres))
    valid_ids = allowed_ids | {"skip"}
    prompt = _external_review_prompt(texts, cfg, genres, category, rule)
    provider = str(spec.get("provider", "gemini"))
    if provider == "gemini":
        out = _gemini_request(prompt, cfg, model=spec.get("model"))
    elif provider == "openai_compatible":
        out = _openai_compat_request(prompt, cfg, spec)
    else:
        print(f"[warn] unknown classifier provider '{provider}'",
              file=sys.stderr)
        return [[] for _ in texts]
    results = _parse_classification_output(out, len(texts), valid_ids)
    normalized = []
    for gids in results:
        if "skip" in gids:
            normalized.append(["skip"])
        else:
            normalized.append([gid for gid in gids if gid in allowed_ids])
    return normalized


def apply_external_qec_policy(
        paper: dict, genre_ids: list[str], allowed_ids: set[str],
        cfg: dict) -> list[str]:
    """Include broad quantum-adjacent coding theory in the QEC channel."""
    result = list(genre_ids)
    if "qec" not in allowed_ids or "qec" in result:
        return result
    if is_qec_adjacent_coding_paper(paper, cfg):
        result.append("qec")
    return result


def review_external_candidates(
        candidates_by_category: dict[str, list[dict]], cfg: dict,
        genres: list[dict], cached_reviews: dict[str, dict],
        dry_run: bool = False,
        *, model_specs: list[dict] | None = None,
) -> tuple[list[dict], dict[str, dict], dict[str, Any]]:
    """Strictly review external candidates using the configured LLM chain.

    A completed decision is cached by ``category:arxiv_id``. Rejections are
    cached separately from the global posted-ID set so a later quant-ph
    cross-list of the same paper can still be processed normally.
    """
    accepted: list[dict] = []
    new_reviews: dict[str, dict] = {}
    stats: dict[str, Any] = {
        "mechanical_candidates": sum(
            len(rows) for rows in candidates_by_category.values()),
        "cached": 0,
        "reviewed": 0,
        "accepted": 0,
        "skipped": 0,
        "unreviewed": 0,
        "skip_disagreements": 0,
        "single_skip_pending": 0,
        "requests": 0,
        "classifier_counts": {},
        "_pending_papers": {},
    }
    model_counts: Counter = Counter()
    specs = classifier_model_specs(cfg) if model_specs is None else model_specs
    batch_size = max(1, int(cfg.get("translate_batch_size", 5)))
    text_limit = int(cfg.get("max_translate_chars", 2000))
    skip_consensus = max(2, int(cfg.get("external_skip_consensus", 2)))

    for category, papers in candidates_by_category.items():
        rule = cfg.get("external_arxiv_queries", {}).get(category, {})
        allowed = set(external_allowed_genre_ids(rule, genres))
        pending: list[dict] = []
        for paper in papers:
            key = f"{category}:{paper['id']}"
            cached = cached_reviews.get(key)
            if external_cache_is_current(cached, cfg):
                stats["cached"] += 1
                gids = [
                    str(gid) for gid in cached.get("genre_ids", [])
                    if str(gid) in allowed
                ]
                if gids:
                    gids = apply_external_qec_policy(
                        paper, gids, allowed, cfg)
                    paper["external_genre_ids"] = gids
                    paper["external_classifier"] = cached.get(
                        "classifier", "cached-external-review")
                    paper["external_classification"] = cached.get("classification")
                    accepted.append(paper)
                    stats["accepted"] += 1
                else:
                    stats["skipped"] += 1
                continue
            pending.append(paper)

        if dry_run:
            stats["unreviewed"] += len(pending)
            continue

        for i in range(0, len(pending), batch_size):
            chunk = pending[i:i + batch_size]
            texts = [
                f"Title: {paper['title']}\n\n"
                f"Abstract: {paper['abstract'][:text_limit]}"
                for paper in chunk
            ]
            accepted_decisions: list[list[str]] = [[] for _ in chunk]
            accepted_models: list[str | None] = [None] * len(chunk)
            skip_models: list[list[str]] = [[] for _ in chunk]
            for spec in specs:
                if not classifier_key_present(spec) or classifier_dead(spec):
                    continue
                todo = [
                    j for j in range(len(chunk))
                    if not accepted_decisions[j]
                    and len(skip_models[j]) < skip_consensus
                ]
                if not todo:
                    break
                stats["requests"] += 1
                model_name = classifier_spec_name(spec)
                outputs = classify_external_llm_batch(
                    [texts[j] for j in todo], cfg, genres,
                    category, rule, spec)
                for j, gids in zip(todo, outputs):
                    if gids == ["skip"]:
                        skip_models[j].append(model_name)
                    elif gids:
                        accepted_decisions[j] = [
                            gid for gid in gids if gid in allowed
                        ]
                        accepted_models[j] = model_name

            for paper, gids, model_name, skip_votes in zip(
                    chunk, accepted_decisions, accepted_models, skip_models):
                has_skip_consensus = len(skip_votes) >= skip_consensus
                if not gids and not has_skip_consensus:
                    stats["unreviewed"] += 1
                    key = f"{category}:{paper['id']}"
                    stats["_pending_papers"][key] = paper
                    if skip_votes:
                        stats["single_skip_pending"] += 1
                    continue
                stats["reviewed"] += 1
                key = f"{category}:{paper['id']}"
                accepted_gids = gids
                if accepted_gids:
                    accepted_gids = apply_external_qec_policy(
                        paper, accepted_gids, allowed, cfg)
                    final_classifier = str(model_name)
                    model_counts[final_classifier] += 1
                    if skip_votes:
                        stats["skip_disagreements"] += 1
                else:
                    final_classifier = (
                        "skip-consensus:" + "+".join(skip_votes))
                    model_counts["skip-consensus"] += 1
                review = {
                    "genre_ids": accepted_gids,
                    "classifier": final_classifier,
                    "skip_votes": skip_votes,
                    "reviewed_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "policy": classification_policy_fingerprint(cfg),
                    "classification": classification_record(
                        paper, gids, accepted_gids, cfg),
                    "paper": paper,
                }
                if accepted_gids:
                    # Keep the paper until it has actually been posted. This
                    # lets a later run retry translation or Discord delivery
                    # even after the API lookback window has elapsed.
                    review["paper"] = paper
                new_reviews[key] = review
                if accepted_gids:
                    paper["external_genre_ids"] = accepted_gids
                    paper["external_classifier"] = final_classifier
                    paper["external_classification"] = review["classification"]
                    accepted.append(paper)
                    stats["accepted"] += 1
                else:
                    stats["skipped"] += 1

    stats["classifier_counts"] = dict(model_counts)
    return accepted, new_reviews, stats


_deepl_dead = False
_azure_dead = False
_google_dead = False
_last_azure_call = 0.0
_last_google_call = 0.0
_translation_success: Counter = Counter()  # successful texts per backend
_apps_script_dead = False
_last_apps_script_call = 0.0

# Free-tier budgets for translators whose overage is billed (Google Cloud
# Translation) or whose quota is shared with classification (Gemini). Only a
# process that loaded them from the bot state may use those backends, so any
# other caller (SciRate, ad-hoc scripts) cannot spend outside the budget.
_translation_budget: dict[str, dict] | None = None
_budget_exhausted: set[str] = set()


def _pacific_time(now: datetime | None = None) -> datetime:
    """Google resets monthly credits and daily API quotas on Pacific time."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:  # noqa: BLE001 - tzdata unavailable
        return now.astimezone(timezone(timedelta(hours=-8)))


def load_translation_budget(state: dict, cfg: dict,
                            now: datetime | None = None) -> dict[str, dict]:
    """Load this Pacific month's Google characters and day's Gemini requests."""
    global _translation_budget
    usage = state.get("translation_usage")
    usage = usage if isinstance(usage, dict) else {}
    local = _pacific_time(now)
    periods = {"google": local.strftime("%Y-%m"),
               "gemini": local.strftime("%Y-%m-%d")}
    limits = {
        "google": int(cfg.get("google_monthly_char_limit", 0) or 0),
        "gemini": int(cfg.get("gemini_translation_daily_request_limit", 0)
                      or 0),
    }
    budget: dict[str, dict] = {}
    for backend, period in periods.items():
        saved = usage.get(backend)
        saved = saved if isinstance(saved, dict) else {}
        try:
            used = int(saved.get("used", 0)) if saved.get("period") == period else 0
        except (TypeError, ValueError):
            used = 0
        budget[backend] = {"period": period, "used": max(0, used),
                           "limit": max(0, limits[backend])}
    _translation_budget = budget
    _budget_exhausted.clear()
    return budget


def save_translation_budget(state: dict) -> None:
    """Persist budget usage with the state committed after translation."""
    if _translation_budget is not None:
        state["translation_usage"] = {
            backend: dict(values)
            for backend, values in _translation_budget.items()
        }


def reserve_translation_budget(backend: str, amount: int) -> bool:
    """Reserve quota before a request; False once it would exceed the cap.

    Reserving before sending counts failed or timed-out requests too, which
    errs on the side of never exceeding the free allowance.
    """
    budget = (_translation_budget or {}).get(backend)
    if (budget is None or budget["limit"] <= 0
            or budget["used"] + amount > budget["limit"]):
        if backend not in _budget_exhausted:
            detail = (f"{budget['used']}/{budget['limit']}"
                      if budget else "not loaded in this process")
            print(f"[info] {backend} free-tier translation budget reached "
                  f"({detail}); skipping {backend} for the rest of this run.")
        _budget_exhausted.add(backend)
        return False
    budget["used"] += amount
    return True


def wait_for_backend_slot(last_call: float, min_interval: float) -> float:
    wait = last_call + min_interval - time.time()
    if wait > 0:
        time.sleep(wait)
    return time.time()


def translate_deepl(text: str, cfg: dict) -> str | None:
    global _deepl_dead
    if _deepl_dead:
        return None
    key = os.environ.get("DEEPL_API_KEY", "")
    if not key:
        _deepl_dead = True
        return None
    data = urllib.parse.urlencode(
        {"text": text, "target_lang": deepl_target_language(cfg),
         "source_lang": "EN"}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api-free.deepl.com/v2/translate",
        data=data,
        headers={"Authorization": f"DeepL-Auth-Key {key}",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw_body = resp.read()
            body = json.loads(raw_body)
            return body["translations"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        error_body = read_http_error_body(e)
        save_error_diagnostic(
            "http_error", url=req.full_url, method="POST", status=e.code,
            reason=e.reason, headers=e.headers, body=error_body, exception=e)
        print(f"[warn] DeepL HTTP {e.code}", file=sys.stderr)
        if e.code == 456:  # monthly quota exhausted on the free plan
            print("[warn] DeepL monthly quota exhausted; "
                  "skipping DeepL for the rest of this run.", file=sys.stderr)
            _deepl_dead = True
        return None
    except Exception as e:  # noqa: BLE001
        save_error_diagnostic(
            "translation_error", url=req.full_url, method="POST",
            body=locals().get("raw_body"), exception=e)
        print(f"[warn] DeepL error: {e}", file=sys.stderr)
        return None


def translate_google(text: str, cfg: dict) -> str | None:
    """Official Cloud Translation API (v2), capped below its free tier.

    The monthly $10 credit covers 500k characters; anything above is billed,
    so every request first reserves its characters from
    ``google_monthly_char_limit`` (see reserve_translation_budget).
    """
    global _google_dead, _last_google_call
    if _google_dead or "google" in _budget_exhausted:
        return None
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    if not key:
        _google_dead = True
        return None
    if not reserve_translation_budget("google", len(text)):
        return None
    url = ("https://translation.googleapis.com/language/translate/v2"
           f"?key={urllib.parse.quote(key)}")
    payload = {"q": text, "source": "en",
               "target": google_target_language(cfg), "format": "text"}
    max_retries = cfg.get("google_max_retries", 3)
    min_interval = cfg.get("google_min_interval_sec", 1.2)
    for attempt in range(max_retries + 1):
        _last_google_call = wait_for_backend_slot(
            _last_google_call, min_interval)
        status, body = http_post_json(url, payload)
        if status == 200:
            try:
                data = json.loads(body)
                return data["data"]["translations"][0]["translatedText"].strip()
            except (KeyError, IndexError, json.JSONDecodeError):
                save_error_diagnostic(
                    "invalid_response", url=url, method="POST", status=200,
                    body=body)
                return None
        retryable = status == 429 or (
            status == 403 and b"User Rate Limit Exceeded" in body
        )
        if retryable and attempt < max_retries:
            backoff = min(60, 10 * (2 ** attempt))
            print(f"[warn] Google Translate HTTP {status}; retry in "
                  f"{backoff}s ({attempt + 1}/{max_retries})",
                  file=sys.stderr)
            time.sleep(backoff)
            continue
        print(f"[warn] Google Translate HTTP {status}: {body[:200]!r}",
              file=sys.stderr)
        if status in (400, 401, 403, 429):
            print("[warn] Google Translate quota/credential problem; "
                  "skipping Google for the rest of this run.", file=sys.stderr)
            _google_dead = True
        return None
    return None


def azure_translate_url(cfg: dict) -> str:
    endpoint = str(
        cfg.get("azure_translator_endpoint")
        or os.environ.get("AZURE_TRANSLATOR_ENDPOINT", "")
        or "https://api.cognitive.microsofttranslator.com"
    ).rstrip("/")
    if endpoint.endswith("/translate"):
        base = endpoint
    elif "cognitiveservices.azure.com" in endpoint:
        base = endpoint + "/translator/text/v3.0/translate"
    else:
        base = endpoint + "/translate"
    query = urllib.parse.urlencode({
        "api-version": "3.0",
        "from": "en",
        "to": azure_target_language(cfg),
    })
    return f"{base}?{query}"


def translate_azure(text: str, cfg: dict) -> str | None:
    """Azure AI Translator Text API. Free F0 tier: 2M chars/month."""
    global _azure_dead, _last_azure_call
    if _azure_dead:
        return None
    key = os.environ.get("AZURE_TRANSLATOR_KEY", "")
    if not key:
        _azure_dead = True
        return None
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/json; charset=UTF-8",
    }
    region = os.environ.get("AZURE_TRANSLATOR_REGION", "")
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    max_retries = cfg.get("azure_max_retries", 4)
    min_interval = cfg.get("azure_min_interval_sec", 1.2)
    for attempt in range(max_retries + 1):
        _last_azure_call = wait_for_backend_slot(
            _last_azure_call, min_interval)
        status, body = http_post_json(
            azure_translate_url(cfg), [{"Text": text}], headers=headers)
        if status == 200:
            try:
                data = json.loads(body)
                return data[0]["translations"][0]["text"].strip()
            except (KeyError, IndexError, json.JSONDecodeError):
                save_error_diagnostic(
                    "invalid_response", url=azure_translate_url(cfg),
                    method="POST", status=200, body=body)
                return None
        if status == 429 and attempt < max_retries:
            backoff = min(60, 10 * (2 ** attempt))
            print(f"[warn] Azure Translator HTTP 429; retry in {backoff}s "
                  f"({attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(backoff)
            continue
        print(f"[warn] Azure Translator HTTP {status}: {body[:200]!r}",
              file=sys.stderr)
        if status in (401, 403, 429):
            print("[warn] Azure Translator credential/quota problem; "
                  "skipping Azure for the rest of this run.", file=sys.stderr)
            _azure_dead = True
        return None
    return None


def translate_apps_script_batch(texts: list[str], cfg: dict) -> list[str | None]:
    """Optional official LanguageApp bridge; no Cloud billing or Gemini quota."""
    global _apps_script_dead, _last_apps_script_call
    results: list[str | None] = [None] * len(texts)
    url = os.environ.get("GOOGLE_APPS_SCRIPT_URL", "").strip()
    token = os.environ.get("GOOGLE_APPS_SCRIPT_TOKEN", "")
    if _apps_script_dead or not url or not token:
        return results
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "script.google.com"
            or not re.fullmatch(r"/macros/s/[A-Za-z0-9_-]+/exec", parsed.path)
            or parsed.query or parsed.fragment or not 32 <= len(token) <= 256):
        print("[warn] Apps Script URL/token configuration is invalid", file=sys.stderr)
        _apps_script_dead = True
        return results
    for start in range(0, len(texts), 5):
        chunk = texts[start:start + 5]
        _last_apps_script_call = wait_for_backend_slot(
            _last_apps_script_call, float(cfg.get("apps_script_min_interval_sec", 1.2)))
        status, body = http_post_json(url, {
            "token": token, "texts": chunk, "source_language": "en",
            "target_language": target_language(cfg),
        })
        try:
            data = json.loads(body)
            translations = data.get("translations")
            if (status != 200 or data.get("error")
                    or not isinstance(translations, list)
                    or len(translations) != len(chunk)
                    or any(not isinstance(t, str) or not t.strip()
                           for t in translations)):
                raise ValueError("invalid or failed Apps Script response")
        except (TypeError, ValueError, AttributeError):
            # Do not print service responses: a misconfigured deployment could
            # return arbitrary text. A failed bridge is retried next run.
            print(f"[warn] Apps Script translation unavailable (HTTP {status})",
                  file=sys.stderr)
            _apps_script_dead = True
            break
        results[start:start + len(chunk)] = [t.strip() for t in translations]
    return results


def translate_batch(texts: list[str], cfg: dict,
                    google_allowed: list[bool] | None = None
                    ) -> list[str | None]:
    """Translate a chunk of abstracts through the configured backend chain.

    Each backend only receives the items that all previous backends
    failed to translate.
    """
    limit = cfg.get("max_translate_chars", 2000)
    texts = [t[:limit] for t in texts]
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    if google_allowed is None:
        google_allowed = [True] * len(texts)

    results: list[str | None] = [None] * len(texts)
    for backend in chain:
        missing = [i for i, r in enumerate(results) if r is None]
        if not missing:
            break
        target = missing
        if backend == "google":
            target = [i for i in missing if google_allowed[i]]
            if not target:
                continue
        subset = [texts[i] for i in target]
        if backend == "gemini":
            # One request per chunk, drawn from a daily budget so translation
            # cannot exhaust the free quota that classification depends on.
            sub = (translate_gemini_batch(subset, cfg)
                   if "gemini" not in _budget_exhausted
                   and reserve_translation_budget("gemini", 1)
                   else [None] * len(subset))
        elif backend == "deepl":
            sub = [translate_deepl(t, cfg) for t in subset]
        elif backend == "azure":
            sub = [translate_azure(t, cfg) for t in subset]
        elif backend == "google":
            sub = [translate_google(t, cfg) for t in subset]
        elif backend == "apps_script":
            sub = translate_apps_script_batch(subset, cfg)
        else:
            save_error_diagnostic(
                "configuration_error",
                body=f"unknown translator backend: {backend}")
            print(f"[warn] unknown translator '{backend}'", file=sys.stderr)
            continue
        sub = [normalized_translation(r) for r in sub]
        ok = sum(1 for r in sub if r)
        if ok:
            _translation_success[backend] += ok
        for i, r in zip(target, sub):
            results[i] = r
    return results


def google_translation_allowed(entry: dict, cfg: dict) -> bool:
    """Whether this entry may use Google after DeepL/Azure fail."""
    skip = set(cfg.get("google_skip_translation_genres",
                       ["other", "foundations", "sensing", "nisq"]))
    genre_ids = {g["id"] for g in entry.get("genres", []) if g}
    return not genre_ids or not genre_ids.issubset(skip)


def translation_priority(entry: dict, cfg: dict) -> tuple[int, int, str]:
    """Prioritize quant-ph papers, then higher-priority genre channels."""
    priority = cfg.get("translation_priority_genres") or []
    rank = {genre_id: i for i, genre_id in enumerate(priority)}
    genre_ids = [g["id"] for g in entry.get("genres", []) if g]
    best = min((rank.get(gid, len(rank)) for gid in genre_ids),
               default=len(rank))
    external = int(entry["paper"].get("announce_type") == "external")
    return external, best, entry["paper"]["id"]


_TRANSLATOR_DEAD_FLAGS = {
    "deepl": lambda: _deepl_dead,
    "azure": lambda: _azure_dead,
    "google": lambda: _google_dead,
    "apps_script": lambda: _apps_script_dead,
}


def dead_translators(cfg: dict) -> list[str]:
    """Backends in the chain that gave up, or used up their budget, this run."""
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    dead = []
    for b in chain:
        if b in _budget_exhausted:
            dead.append(b)
        elif b == "gemini":
            # Gemini-as-translator uses gemini_model; dead flags are per model.
            if cfg.get("gemini_model", "gemini-3.5-flash-lite") in _gemini_dead_models:
                dead.append(b)
        elif _TRANSLATOR_DEAD_FLAGS.get(b, lambda: False)():
            dead.append(b)
    return dead


# Credentials each backend needs. A backend without them cannot translate,
# even though it never trips its circuit breaker (Apps Script returns early).
_TRANSLATOR_REQUIRED_ENV = {
    "deepl": ("DEEPL_API_KEY",),
    "azure": ("AZURE_TRANSLATOR_KEY",),
    "google": ("GOOGLE_TRANSLATE_API_KEY",),
    "apps_script": ("GOOGLE_APPS_SCRIPT_URL", "GOOGLE_APPS_SCRIPT_TOKEN"),
    "gemini": ("GEMINI_API_KEY",),
}


def unconfigured_translators(cfg: dict) -> list[str]:
    """Backends in the chain that are unknown or lack their credentials."""
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    return [
        backend for backend in chain
        if backend not in _TRANSLATOR_REQUIRED_ENV
        or not all(os.environ.get(name, "").strip()
                   for name in _TRANSLATOR_REQUIRED_ENV[backend])
    ]


def translation_missing(entry: dict, cfg: dict) -> bool:
    """Whether delivery must wait for a required abstract or title translation."""
    if entry.get("allow_untranslated", False):
        return False
    missing_abstract = (cfg.get("require_translation", True)
                        and entry.get("need_tr") and entry.get("jp") is None)
    missing_title = (cfg.get("require_translated_title", False)
                     and show_translated_title(cfg)
                     and entry["paper"].get("title")
                     and not entry.get("jp_title"))
    return bool(missing_abstract or missing_title)


def record_translation_status(state: dict, entries: list[dict], cfg: dict,
                              now: datetime | None = None) -> dict:
    """Persist this translation pass and alert (once a day) on a full outage.

    A full outage means papers still wait for translation while every backend
    in the chain has either tripped its circuit breaker in this pass or is not
    configured at all.
    """
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    unconfigured = unconfigured_translators(cfg)
    dead = [b for b in dead_translators(cfg) if b not in unconfigured]
    waiting = sum(1 for entry in entries if translation_missing(entry, cfg))
    stamp = now or datetime.now(timezone.utc)
    status = {
        "at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "translated": dict(_translation_success),
        "dead": dead,
        "unconfigured": unconfigured,
        "waiting": waiting,
        "budget_exhausted": sorted(_budget_exhausted & set(chain)),
        "free_tier": {
            backend: dict(values)
            for backend, values in (_translation_budget or {}).items()
            if backend in chain
        },
    }
    save_translation_budget(state)
    state["translation_status"] = status
    if waiting and chain and all(b in dead or b in unconfigured for b in chain):
        alerts = state.get("translation_alerts")
        if not isinstance(alerts, dict):
            alerts = state["translation_alerts"] = {}
        today = stamp.date().isoformat()
        if (alerts.get("outage_notified_on") != today
                and notify_translation_outage(waiting, dead, unconfigured)):
            alerts["outage_notified_on"] = today
    return status


def notify_translation_outage(deferred: int, dead: list[str],
                              unconfigured: list[str] | None = None) -> bool:
    """Warn the bot-emergency Discord channel when every translator backend
    in the chain is unavailable, so papers are being silently deferred.

    Returns whether Discord accepted the notice.
    """
    webhook = os.environ.get("DISCORD_WEBHOOK_BOT_EMERGENCY", "")
    states = [f"{name}: 停止" for name in dead]
    states += [f"{name}: 未設定" for name in unconfigured or []]
    content = (
        "⚠️ 翻訳バックエンドがすべて利用できません "
        f"({', '.join(states)})。翻訳待ちの{deferred}件は、翻訳が復旧するまで"
        "配信を保留します。"
    )
    if not webhook:
        print(f"[warn] {content} (no DISCORD_WEBHOOK_BOT_EMERGENCY configured "
              "to send this notice)", file=sys.stderr)
        return False
    status, body = http_post_json(webhook, {"content": content})
    if status not in (200, 204):
        print(f"[warn] failed to send translation-outage notice: "
              f"HTTP {status} {body[:200]!r}", file=sys.stderr)
        return False
    return True


def _report_paper_line(item: dict) -> str:
    title = truncate(str(item.get("title") or item.get("id") or "?"), 80)
    link = item.get("link", "")
    head = f"[{title}]({link})" if link else title
    channels = ", ".join(item.get("genre_names", []))
    return f"・{head} → **{channels}**" if channels else f"・{head}"


def notify_run_report(report: dict, cfg: dict) -> None:
    """Post a per-run summary (in Japanese) to the bot-emergency channel.

    Sent on every run, including fully successful ones, so the channel
    doubles as an execution log: which papers were posted to which genre
    channels, what was deferred for translation, and what failed.
    """
    webhook = os.environ.get("DISCORD_WEBHOOK_BOT_EMERGENCY", "")
    if not webhook:
        print("[info] run report skipped "
              "(DISCORD_WEBHOOK_BOT_EMERGENCY not configured)")
        return

    posted = report.get("posted", [])
    deferred = report.get("deferred", [])
    failed = report.get("failed", [])
    source_failures = report.get("source_failures", [])
    source_notices = report.get("source_notices", [])

    lines = [
        f"📥 フィード取得: {report.get('fetched', 0)}件 / "
        f"新規投稿対象: {report.get('candidates', 0)}件",
        f"📤 投稿成功: {len(posted)}論文({report.get('messages', 0)}メッセージ)"
        f" / ⏸ 翻訳持ち越し: {len(deferred)}件"
        f" / ❌ 投稿失敗: {len(failed)}件",
    ]
    if source_failures:
        lines.append(
            "🚨 取得失敗: " + " / ".join(
                f"{item.get('source', '?')}: "
                f"{truncate(str(item.get('error', 'unknown error')), 120)}"
                for item in source_failures
            )
        )
    if source_notices:
        lines.append(
            "ℹ️ 補助ソース: " + " / ".join(
                f"{item.get('source', '?')}: "
                f"{truncate(str(item.get('message', '')), 160)}"
                for item in source_notices
            )
        )
    gemini = report.get("gemini")
    classifier_counts = report.get("classifier_counts") or {}
    if classifier_counts:
        breakdown = " / ".join(
            f"{'TF-IDF' if m == 'tfidf' else m}: {n}件"
            for m, n in sorted(classifier_counts.items(),
                               key=lambda kv: -kv[1]))
        lines.append(f"🏷 分類: {breakdown}")
    elif gemini and gemini.get("entries_attempted"):
        lines.append(
            f"🏷 分類: Gemini({gemini.get('mode', '?')})"
            f" {gemini.get('entries_classified', 0)}/"
            f"{gemini.get('entries_attempted', 0)}件成功、"
            f"TF-IDFフォールバック {report.get('tfidf_fallback', 0)}件")
    else:
        lines.append(
            f"🏷 分類: TF-IDFフォールバック {report.get('tfidf_fallback', 0)}件"
            "(Gemini未使用)")
    external = report.get("external_review") or {}
    if external.get("mechanical_candidates"):
        lines.append(
            "🔎 外部分野の厳密審査: "
            f"機械候補 {external.get('mechanical_candidates', 0)}件 / "
            f"採用 {external.get('accepted', 0)}件 / "
            f"skip {external.get('skipped', 0)}件 / "
            f"未審査 {external.get('unreviewed', 0)}件"
            f"(skip反対判定 {external.get('skip_disagreements', 0)}件 / "
            f"単独skip保留 {external.get('single_skip_pending', 0)}件 / "
            f"キャッシュ {external.get('cached', 0)}件)")
    translated = report.get("translated") or {}
    status_at = str(report.get("translation_status_at") or "")
    when = (f"、翻訳処理 {status_at[5:16].replace('T', ' ')} UTC"
            if len(status_at) >= 16 else "")
    if translated:
        usage = " / ".join(f"{b}: {n}件" for b, n in translated.items())
        lines.append(f"🌐 翻訳成功(タイトル含む{when}): {usage}")
    dead = report.get("dead_translators") or []
    if dead:
        lines.append(f"⚠️ 停止した翻訳バックエンド{f'({when[1:]})' if when else ''}: "
                     f"{', '.join(dead)}")
    unconfigured = report.get("unconfigured_translators") or []
    if unconfigured and deferred:
        lines.append(f"ℹ️ 未設定の翻訳バックエンド: {', '.join(unconfigured)}")
    free_tier = report.get("free_tier") or {}
    if free_tier:
        units = {"google": "字/月", "gemini": "回/日"}
        lines.append("📊 無料枠の使用量: " + " / ".join(
            f"{backend} {values.get('used', 0):,}/{values.get('limit', 0):,}"
            f"{units.get(backend, '')}"
            for backend, values in free_tier.items()))
    if not report.get("candidates"):
        lines.append("🈳 新規の投稿対象論文はありませんでした。")

    sections = [
        ("📤 投稿した論文と送信先チャンネル", posted),
        ("⏸ 翻訳できず次回へ持ち越した論文", deferred),
        ("❌ Discord投稿に失敗した論文", failed),
    ]
    body_lines = list(lines)
    total = sum(len(line) + 1 for line in body_lines)
    clipped = False
    for heading, items in sections:
        if not items:
            continue
        heading_line = f"\n**{heading}**"
        if total + len(heading_line) > 3800:
            clipped = True
            break
        body_lines.append(heading_line)
        total += len(heading_line) + 1
        for item in items:
            line = _report_paper_line(item)
            if total + len(line) > 3800:
                clipped = True
                break
            body_lines.append(line)
            total += len(line) + 1
    if clipped:
        body_lines.append("…(長いため以降は省略)")

    if failed or source_failures:
        icon, color = "🚨", 0xE74C3C
    elif deferred or source_notices:
        icon, color = "🟡", 0xE67E22
    else:
        icon, color = "✅", 0x2ECC71
    embed = {
        "title": truncate(
            f"{icon} 実行レポート | {report.get('source', 'arXiv新着通知')}", 256),
        "description": truncate("\n".join(body_lines), 4000),
        "color": color,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    status, body = http_post_json(webhook, {"embeds": [embed]})
    if status not in (200, 204):
        print(f"[warn] failed to send run report: "
              f"HTTP {status} {body[:200]!r}", file=sys.stderr)


# ----------------------------------------------------------------- discord

def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def embed_description(paper: dict, jp_title: str | None,
                      jp_abstract: str | None, cfg: dict) -> str:
    abstract = jp_abstract if jp_abstract else paper["abstract"]
    if jp_title and show_translated_title(cfg):
        return f"**{translated_title_label(cfg)}:** {jp_title}\n\n{abstract}"
    return abstract


def post_to_discord(webhook: str, paper: dict, genre_name: str,
                    jp_abstract: str | None, jp_title: str | None,
                    cfg: dict, extra_fields: list[dict] | None = None) -> bool:
    desc = embed_description(paper, jp_title, jp_abstract, cfg)
    embed = {
        "title": truncate(paper["title"], 256),
        "url": paper["link"],
        "description": truncate(desc, 4000),
        "color": 0xB31B1B,  # arXiv red
        "fields": [],
        "footer": {"text": f"{paper['primary']} | {genre_name} | "
                           f"{paper['announce_type']}"},
    }
    if paper["authors"]:
        embed["fields"].append(
            {"name": "Authors", "value": truncate(paper["authors"], 1024)})
    if extra_fields:
        embed["fields"].extend(extra_fields)
    if jp_abstract and cfg.get("show_original_abstract", False):
        embed["fields"].append(
            {"name": "Original abstract",
             "value": truncate(paper["abstract"], 1024)})
    status, body = http_post_json(webhook, {"embeds": [embed]})
    if status == 429:  # rate limited; wait and retry once
        try:
            wait = json.loads(body).get("retry_after", 2)
        except json.JSONDecodeError:
            wait = 2
        time.sleep(float(wait) + 0.5)
        status, _ = http_post_json(webhook, {"embeds": [embed]})
    return status in (200, 204)


def resolve_webhook(genre: dict | None) -> tuple[str, str]:
    """Return (webhook_url, genre_name) without cross-channel fallback."""
    general = os.environ.get("DISCORD_WEBHOOK_GENERAL", "")
    if genre is None:
        return general, "general"
    url = os.environ.get(genre.get("webhook_env", ""), "")
    return url, genre["name"]


def normalize_bot_state(raw: Any) -> dict:
    """Migrate legacy paper-level state to the durable delivery schema."""
    state = dict(raw) if isinstance(raw, dict) else {}
    completed = state.get("completed_ids", state.get("seen", []))
    if not isinstance(completed, list):
        completed = []
    completed_ids = sorted({str(pid) for pid in completed if pid})
    deliveries = state.get("deliveries", {})
    if not isinstance(deliveries, dict):
        deliveries = {}
    state["schema_version"] = 2
    state["completed_ids"] = completed_ids
    # Keep the legacy key while helper scripts transition to completed_ids.
    state["seen"] = completed_ids
    state["deliveries"] = {
        str(pid): row for pid, row in deliveries.items()
        if isinstance(row, dict) and isinstance(row.get("paper"), dict)
    }
    for key in ("external_reviews", "external_pending", "external_cursors",
                "feed_cursors", "oai_cursors", "translation_alerts"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def normalized_translation(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def entry_from_delivery(record: dict, genre_map: dict[str, dict], *,
                        reclassify_fallback: bool = False) -> dict:
    """Restore a normal in-memory entry from a durable delivery record."""
    return {
        "paper": record["paper"],
        "genres": [
            genre_map[gid] for gid in record.get("genre_ids", [])
            if gid in genre_map
        ],
        "jp": normalized_translation(record.get("abstract_translated")),
        "jp_title": normalized_translation(record.get("title_translated")),
        "need_tr": bool(record.get(
            "need_translation", record["paper"].get("abstract"))),
        "allow_untranslated": bool(record.get("allow_untranslated", False)),
        "llm_done": not (
            reclassify_fallback and record.get("classifier") == "tfidf"
            and is_quantph_feed_paper(record["paper"])
            and not any(channel.get("status") == "delivered"
                        for channel in record.get("channels", {}).values()
                        if isinstance(channel, dict))),
        "classifier": record.get("classifier", "persisted-delivery"),
        "classification": record.get("classification"),
    }


def merge_entry_into_delivery(entry: dict, existing: dict | None = None) -> dict:
    """Create/update a delivery record while preserving channel receipts."""
    old = existing if isinstance(existing, dict) else {}
    genre_ids = [g["id"] for g in entry.get("genres", []) if g]
    old_channels = old.get("channels", {})
    if not isinstance(old_channels, dict):
        old_channels = {}
    channels = {}
    for gid in genre_ids:
        previous = old_channels.get(gid, {})
        status = (
            "delivered"
            if isinstance(previous, dict)
            and previous.get("status") == "delivered"
            else "pending"
        )
        channels[gid] = {
            "status": status,
            **({
                "delivered_at": previous.get("delivered_at"),
            } if status == "delivered" and previous.get("delivered_at") else {}),
        }
    return {
        "paper": entry["paper"],
        "genre_ids": genre_ids,
        "classifier": entry.get("classifier", "tfidf"),
        "classification": entry.get("classification"),
        "abstract_translated": entry.get("jp"),
        "title_translated": entry.get("jp_title"),
        "need_translation": bool(entry.get("need_tr", False)),
        "allow_untranslated": bool(entry.get("allow_untranslated", False)),
        "channels": channels,
        "queued_at": old.get("queued_at") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def persist_bot_state(
        state: dict, completed: set[str], deliveries: dict[str, dict],
        external_reviews: dict[str, dict],
        external_pending: dict[str, dict],
        external_cursors: dict[str, str]) -> None:
    """Persist all correctness-critical state without lossy size caps."""
    completed_ids = sorted(completed)
    state["schema_version"] = 2
    state["completed_ids"] = completed_ids
    state["seen"] = completed_ids
    state["deliveries"] = deliveries
    state["external_reviews"] = external_reviews
    state["external_pending"] = external_pending
    state["external_cursors"] = external_cursors
    atomic_write_json(STATE_PATH, state)


def strip_completed_external_papers(
        external_reviews: dict[str, dict], completed: set[str],
        queued_ids: set[str] | None = None) -> None:
    """Keep review decisions, but move retry payloads into the delivery queue."""
    queued = queued_ids or set()
    for review in external_reviews.values():
        if not isinstance(review, dict):
            continue
        paper = review.get("paper")
        if (
            isinstance(paper, dict)
            and (paper.get("id") in completed or paper.get("id") in queued)
        ):
            review.pop("paper", None)


def record_delivery_success(
        log: list[dict], entry: dict, delivered_gid: str,
        genre_map: dict[str, dict], record: dict, cfg: dict) -> None:
    """Upsert a log row containing only channels actually delivered."""
    paper = entry["paper"]
    delivery_id = f"main:{paper['id']}"
    row = next((
        item for item in reversed(log)
        if item.get("delivery_id") == delivery_id
    ), None)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if row is None:
        row = {
            "delivery_id": delivery_id,
            "id": paper["id"],
            "posted_at": now,
            "title": paper["title"],
            "title_ja": entry.get("jp_title"),
            "title_translated": entry.get("jp_title"),
            "translation_language": target_language(cfg),
            "authors": paper["authors"],
            "link": paper["link"],
            "primary": paper["primary"],
            "categories": paper.get("categories", []),
            "source_feed": paper.get("source_feed", paper["primary"]),
            "classification_context": (
                "quant-ph" if is_quantph_feed_paper(paper) else "external"),
            "announce_type": paper["announce_type"],
            "genre_ids": [],
            "genre_names": [],
            "target_genre_ids": list(record.get("genre_ids", [])),
            "target_genre_names": [
                genre_map[gid]["name"]
                for gid in record.get("genre_ids", [])
                if gid in genre_map
            ],
            "classifier": entry.get("classifier", "tfidf"),
            "classification": entry.get("classification"),
            "abstract_en": paper["abstract"],
            "abstract_ja": entry.get("jp"),
            "abstract_translated": entry.get("jp"),
        }
        log.append(row)
    delivered = list(row.get("genre_ids", []))
    delivered_now = [
        gid for gid in record.get("genre_ids", [])
        if isinstance(record.get("channels", {}).get(gid), dict)
        and record["channels"][gid].get("status") == "delivered"
    ]
    if delivered_gid not in delivered_now:
        delivered_now.append(delivered_gid)
    for gid in delivered_now:
        if gid not in delivered:
            delivered.append(gid)
    row["genre_ids"] = delivered
    row["genre_names"] = [
        genre_map[gid]["name"] for gid in delivered if gid in genre_map
    ]
    row["last_delivered_at"] = now


# -------------------------------------------------------------------- main

def main() -> None:
    dry_run = "--dry-run" in sys.argv
    discover_only = "--discover-only" in sys.argv
    translate_only = "--translate-only" in sys.argv
    prepare_only = "--prepare-only" in sys.argv
    deliver_only = "--deliver-only" in sys.argv
    phase_flags = [discover_only, translate_only, prepare_only, deliver_only]
    if sum(phase_flags) > 1:
        raise SystemExit("delivery phase flags are mutually exclusive")
    if dry_run and any(phase_flags):
        raise SystemExit("--dry-run cannot be combined with delivery phase flags")
    resume_only = translate_only or deliver_only

    cfg = load_json(CONFIG_PATH, {})
    if isinstance(cfg.get("main_translators"), list):
        cfg["translators"] = cfg["main_translators"]
    backfill_raw = os.environ.get("EXTERNAL_ARXIV_LOOKBACK_DAYS", "").strip()
    backfill_days = 0
    if backfill_raw:
        try:
            backfill_days = max(0, int(backfill_raw))
        except ValueError:
            backfill_days = 0
            print("[warn] EXTERNAL_ARXIV_LOOKBACK_DAYS must be an integer; "
                  f"ignoring {backfill_raw!r}", file=sys.stderr)
        if backfill_days > 0:
            rules = cfg.get("external_arxiv_queries", {})
            if isinstance(rules, dict):
                cfg["external_arxiv_queries"] = {
                    category: {
                        **rule,
                        "lookback_days": backfill_days,
                    } if isinstance(rule, dict) else rule
                    for category, rule in rules.items()
                }
                print("[info] one-time external arXiv backfill window: "
                      f"{backfill_days} days")
    state = normalize_bot_state(load_json(STATE_PATH, {"seen": []}))
    completed = (
        set() if dry_run else set(state.get("completed_ids", [])))
    deliveries: dict[str, dict] = (
        {} if dry_run else dict(state.get("deliveries", {})))
    log: list[dict] = load_json(LOG_PATH, [])
    genres = cfg.get("genres", [])
    cached_external_reviews = (
        {} if dry_run else state.get("external_reviews", {}))
    if not isinstance(cached_external_reviews, dict):
        cached_external_reviews = {}
    cached_external_pending = (
        {} if dry_run else state.get("external_pending", {}))
    if not isinstance(cached_external_pending, dict):
        cached_external_pending = {}
    external_cursors = (
        {} if dry_run else state.get("external_cursors", {}))
    if not isinstance(external_cursors, dict):
        external_cursors = {}
    strip_completed_external_papers(
        cached_external_reviews, completed, set(deliveries))
    excluded_ids = completed | set(deliveries)
    source_failures: list[dict[str, str]] = []
    source_notices: list[dict[str, str]] = []
    using_test_feed = bool(os.environ.get("ARXIV_TEST_FEED", ""))
    oai_rule = cfg.get("oai_harvest", {})
    if not isinstance(oai_rule, dict):
        oai_rule = {}
    use_oai = bool(oai_rule.get("enabled", False))

    def harvest_oai(category: str, legacy_cursor: str | None,
                    extra_days: int = 0) -> list[dict] | None:
        """Harvest one category's OAI set; None after a recorded failure."""
        set_spec = oai_set_for_category(category)
        cursor = state["oai_cursors"].get(set_spec) or legacy_cursor
        from_date = oai_harvest_from_date(
            cursor, oai_rule, extra_days=extra_days)
        try:
            records, served_at, skipped = fetch_oai_records(
                set_spec, from_date, oai_rule)
        except Exception as err:  # noqa: BLE001
            message = str(err)
            save_error_diagnostic(
                "source_error", method="GET", body=traceback.format_exc(),
                exception=err)
            source_failures.append({
                "source": f"arXiv OAI-PMH:{set_spec}", "error": message})
            print(f"[error] OAI-PMH {set_spec} from={from_date} failed: "
                  f"{message}", file=sys.stderr)
            return None
        finally:
            if not dry_run:
                time.sleep(3)  # arXiv asks harvesters to pace requests
        if not dry_run:
            state["oai_cursors"][set_spec] = served_at
        if skipped:
            source_notices.append({
                "source": f"OAI-PMH:{set_spec}",
                "message": f"解析できないレコード{skipped}件を除外"})
        print(f"[info] OAI-PMH {set_spec}: from={from_date}, "
              f"records={len(records)}, unparsable={skipped}")
        return records

    papers: dict[str, dict] = {}
    rss_listed: dict[str, int] = {}
    if not resume_only:
        for cat in cfg.get("feeds", ["quant-ph"]):
            try:
                rss_rows = fetch_feed(cat)
                for p in rss_rows:
                    p["source_feed"] = cat
                    papers.setdefault(p["id"], p)
                rss_listed[cat] = sum(
                    p["announce_type"] in ("new", "cross") for p in rss_rows)
                print(f"[info] RSS {cat}: items={len(rss_rows)}, "
                      f"new_or_cross={rss_listed[cat]}")
            except Exception as err:  # noqa: BLE001
                message = str(err)
                save_error_diagnostic(
                    "source_error", method="GET", body=traceback.format_exc(),
                    exception=err)
                source_failures.append({
                    "source": f"RSS:{cat}", "error": message})
                print(f"[error] feed {cat} failed: {message}",
                      file=sys.stderr)
            if not dry_run:
                time.sleep(3)  # be polite to arXiv

        backfill_rule = cfg.get("feed_backfill", {})
        if use_oai and not using_test_feed:
            # Announcement-date reconciliation: every first-version record
            # whose OAI datestamp is after the last harvest, regardless of
            # submission date or of whether RSS showed it.
            for cat in cfg.get("feeds", ["quant-ph"]):
                records = harvest_oai(cat, state["feed_cursors"].get(cat))
                if records is None:
                    continue
                rows = select_oai_feed_papers(records, cat, oai_rule)
                recovered = [
                    p for p in rows
                    if p["id"] not in papers and p["id"] not in excluded_ids
                ]
                for p in rows:
                    papers.setdefault(p["id"], p)
                print(f"[info] OAI-PMH {cat}: new_or_cross={len(rows)}, "
                      f"missing_from_rss_and_state={len(recovered)}")
                # RSS is rebuilt about four hours after each announcement, so
                # today's listing legitimately reaches OAI first. Report only
                # an empty RSS snapshot or announcements from earlier days.
                datestamps = {r["id"]: r["oai_datestamp"] for r in records}
                today = datetime.now(timezone.utc).date().isoformat()
                earlier = sorted(
                    datestamps.get(p["id"], today) for p in recovered
                    if datestamps.get(p["id"], today) < today)
                if recovered and (not rss_listed.get(cat) or earlier):
                    shown = ", ".join(p["id"] for p in recovered[:8])
                    more = "…" if len(recovered) > 8 else ""
                    reason = ("RSSに新着が無かった" if not rss_listed.get(cat)
                              else f"{earlier[0]}以降のRSS取りこぼし")
                    source_notices.append({
                        "source": f"OAI-PMH:{cat}",
                        "message": (f"{reason}ため新着{len(recovered)}件を"
                                    f"OAI-PMHで回収 ({shown}{more})"),
                    })
        elif (isinstance(backfill_rule, dict)
                and not using_test_feed):
            for cat in cfg.get("feeds", ["quant-ph"]):
                cursor = state["feed_cursors"].get(cat)
                if not feed_backfill_due(backfill_rule, cursor):
                    continue
                rule = dict(backfill_rule)
                rule["lookback_days"] = effective_external_lookback_days(
                    backfill_rule, cursor)
                started_at = datetime.now(timezone.utc).isoformat()
                try:
                    rows = fetch_feed_backfill(cat, rule)
                    for p in rows:
                        papers.setdefault(p["id"], p)
                    if not dry_run:
                        state["feed_cursors"][cat] = started_at
                    print(f"[info] feed backfill {cat}: fetched={len(rows)}, "
                          f"lookback_days={rule['lookback_days']}")
                except Exception as err:  # noqa: BLE001
                    source_failures.append({
                        "source": f"arXiv backfill:{cat}", "error": str(err)})
                    save_error_diagnostic(
                        "source_error", method="GET", exception=err)
                    print(f"[error] feed backfill {cat} failed: {err}",
                          file=sys.stderr)
                if not dry_run:
                    time.sleep(3)

        # One-time operator recovery of announcements that every automatic
        # source missed (workflow input recover_ids). Recovered papers go
        # through the normal classification, translation and delivery.
        recover_ids = parse_recover_ids(
            os.environ.get("ARXIV_RECOVER_IDS", ""))
        if recover_ids and not using_test_feed:
            feed = cfg.get("feeds", ["quant-ph"])[0]
            todo = [pid for pid in recover_ids
                    if pid not in excluded_ids and pid not in papers]
            try:
                rows, rejected = (
                    fetch_recovery_papers(todo, feed, oai_rule)
                    if todo else ([], []))
            except Exception as err:  # noqa: BLE001
                save_error_diagnostic(
                    "source_error", method="GET",
                    body=traceback.format_exc(), exception=err)
                source_failures.append({
                    "source": "arXiv API:指定ID回収", "error": str(err)})
                print(f"[error] ID recovery failed: {err}", file=sys.stderr)
            else:
                for p in rows:
                    papers.setdefault(p["id"], p)
                skipped = len(recover_ids) - len(todo)
                message = (f"指定{len(recover_ids)}件のうち{len(rows)}件を"
                           "通常の分類・翻訳・配信へ追加")
                if skipped:
                    message += f"、処理済み・取得済み{skipped}件"
                if rejected:
                    message += (f"、対象外{len(rejected)}件 "
                                f"({', '.join(rejected[:8])})")
                source_notices.append({"source": "指定ID回収",
                                       "message": message})
                print(f"[info] ID recovery: requested={len(recover_ids)}, "
                      f"queued={len(rows)}, already_done={skipped}, "
                      f"rejected={len(rejected)}")

    # Adjacent arXiv categories use a high-recall mechanical first stage (OAI
    # announcements plus local term matching, or legacy API queries). Their
    # results are never sent through the normal "other" fallback: a separate
    # strict LLM review must explicitly accept them into only the configured
    # destination channels.
    external_candidates: dict[str, list[dict]] = {}
    external_rules = cfg.get("external_arxiv_queries", {})
    if (
        not resume_only
        and isinstance(external_rules, dict)
        and not using_test_feed
    ):
        max_delay = int(oai_rule.get("max_announce_delay_days", 90))
        for cat, rule in external_rules.items():
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            if use_oai:
                records = harvest_oai(
                    cat, external_cursors.get(cat), extra_days=backfill_days)
                if records is None:
                    continue
                rows = select_oai_external_candidates(
                    records, cat, rule, max_delay, set(papers), excluded_ids)
                external_candidates[cat] = rows
                external_cursors[cat] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                print(f"[info] external OAI-PMH {cat}: "
                      f"records={len(records)}, "
                      f"mechanical_candidates={len(rows)}")
                continue
            try:
                effective_rule = dict(rule)
                effective_rule["lookback_days"] = (
                    effective_external_lookback_days(
                        rule, external_cursors.get(cat)))
                fetched = fetch_external_arxiv(cat, effective_rule)
                rows = select_external_candidates(
                    fetched, cat, effective_rule, set(papers), excluded_ids)
                external_candidates[cat] = rows
                external_cursors[cat] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                print(f"[info] external query {cat}: fetched={len(fetched)}, "
                      f"mechanical_candidates={len(rows)}, "
                      f"lookback_days={effective_rule['lookback_days']}")
            except Exception as err:  # noqa: BLE001
                message = str(err)
                save_error_diagnostic(
                    "source_error", method="GET", body=traceback.format_exc(),
                    exception=err)
                source_failures.append({
                    "source": f"arXiv API:{cat}", "error": message})
                print(f"[error] external query {cat} failed: {message}",
                      file=sys.stderr)
            if not dry_run:
                time.sleep(3)  # arXiv API asks clients to avoid rapid calls
    elif not resume_only and using_test_feed and external_rules:
        print("[info] external queries skipped while ARXIV_TEST_FEED is set")

    # A candidate without a completed review must not disappear merely because
    # it ages out of lookback_days. Accepted-but-not-yet-posted papers are also
    # restored from the review cache, so translation or Discord failures remain
    # retryable. Fresh API rows win when both copies are present.
    if not dry_run and not resume_only and isinstance(external_rules, dict):
        durable_candidates: list[tuple[str, dict]] = []
        for key, paper in cached_external_pending.items():
            if isinstance(paper, dict):
                durable_candidates.append((key, paper))
        for key, review in cached_external_reviews.items():
            if (
                isinstance(review, dict)
                and (review.get("genre_ids")
                     or not external_cache_is_current(review, cfg))
                and isinstance(review.get("paper"), dict)
            ):
                durable_candidates.append((key, review["paper"]))

        for key, paper in durable_candidates:
            category = key.split(":", 1)[0]
            rule = external_rules.get(category)
            if (
                not isinstance(rule, dict)
                or not rule.get("enabled", True)
                or paper.get("id") in completed
                or paper.get("id") in deliveries
                or paper.get("id") in papers
            ):
                continue
            rows = external_candidates.setdefault(category, [])
            if not any(row.get("id") == paper.get("id") for row in rows):
                rows.append(paper)

    # ---- determine which papers to post (filtering only) ------------------
    pending = []  # papers passing should_post, not yet seen
    for pid, paper in papers.items():
        if pid in excluded_ids or not should_post(paper, cfg):
            continue
        pending.append(paper)

    # Each entry carries the paper plus its resolved genres + translation.
    # genres is always a non-empty list; fallback genre is "other".
    genre_map = {g["id"]: g for g in genres}
    entries: list[dict[str, Any]] = [
        entry_from_delivery(
            record, genre_map, reclassify_fallback=(
                not resume_only and cfg.get("reclassify_pending_fallback", True)))
        for record in deliveries.values()
    ]
    entries.extend([
        {
            "paper": p,
            "genres": [
                genre_map[gid]
                for gid in p.get("external_genre_ids", [])
                if gid in genre_map
            ],
            "jp": None,
            "jp_title": None,
            "need_tr": bool(p["abstract"]),
            "allow_untranslated": False,
            "llm_done": False,
            "classifier": None,
        }
        for p in pending
    ])
    if not cfg.get("allow_untranslated_low_priority", False):
        for entry in entries:
            entry["allow_untranslated"] = False

    batch_size = max(1, cfg.get("translate_batch_size", 5))
    use_llm_cls = cfg.get("classify_with_llm", True)
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    # Discovery must never combine classification with translation: its queue
    # is committed before a separate translation process starts. This remains
    # true even for the legacy configuration that puts Gemini first in the
    # translator chain.
    llm_first = (
        use_llm_cls
        and chain
        and chain[0] == "gemini"
        and not (discover_only or prepare_only)
    )
    llm_classify_only = use_llm_cls and not llm_first
    classifier_specs = classifier_model_specs(cfg)
    model_primary = cfg.get("gemini_model_primary") or cfg.get(
        "gemini_model", "gemini-3.5-flash-lite")
    model_secondary = cfg.get("gemini_model_secondary") or model_primary
    classifier_names = [classifier_spec_name(s) for s in classifier_specs]
    gemini_stats = {
        "mode": "disabled",
        "model": "+".join(classifier_names) or model_primary,
        "key_present": bool(os.environ.get("GEMINI_API_KEY", "")),
        "classifier_key_present": any(classifier_key_present(s)
                                      for s in classifier_specs),
        "requests": 0,
        "entries_attempted": 0,
        "entries_classified": 0,
        "entries_translated": 0,
    }
    if dry_run:
        gemini_stats["mode"] = "dry-run"
    elif not use_llm_cls:
        gemini_stats["mode"] = "disabled-by-config"
    elif llm_first:
        gemini_stats["mode"] = "translate-and-classify"
    elif llm_classify_only:
        gemini_stats["mode"] = "classify-only"

    # ---- path A: Gemini translate + classify in one request ---------------
    # Used when "gemini" is first in the translators chain.
    if llm_first and not dry_run and gemini_stats["key_present"]:
        normal_entries = [e for e in entries if not e.get("llm_done")]
        for i in range(0, len(normal_entries), batch_size):
            chunk = normal_entries[i: i + batch_size]
            limit = cfg.get("max_translate_chars", 2000)
            abstracts = [
                f"Title: {e['paper']['title']}\n\nAbstract: {e['paper']['abstract'][:limit]}"
                for e in chunk
            ]
            gemini_stats["requests"] += 1
            gemini_stats["entries_attempted"] += len(chunk)
            pairs = translate_classify_gemini_batch(abstracts, cfg, genres)
            for e, (jp, gids) in zip(chunk, pairs):
                if jp:
                    e["jp"] = jp
                    gemini_stats["entries_translated"] += 1
                    gs = [genre_map[g] for g in gids if g in genre_map]
                    e["genres"] = gs if gs else [genre_by_id(None, genres)]
                    e["genres"] = postprocess_genres(
                        e["paper"], e["genres"], genres, cfg)
                    e["classification"] = classification_record(
                        e["paper"], gs, e["genres"], cfg)
                    e["llm_done"] = True
                    e["classifier"] = cfg.get("gemini_model",
                                              "gemini-3.5-flash-lite")
                    gemini_stats["entries_classified"] += 1

    # ---- path B: Gemini classify only, translate via DeepL/Google ---------
    # Used when classify_with_llm=true but "gemini" is NOT in translators.
    # Visible output is genre IDs only; request/input/thinking quotas still apply.
    #
    # Use the same free-tier model chain for all genres. Per-run routing
    # thresholds cannot represent project-wide daily quotas. The deterministic
    # classifier is used only for unresolved entries after both models fail.
    elif (llm_classify_only and not dry_run
          and gemini_stats["classifier_key_present"]):
        normal_entries = [e for e in entries if not e.get("llm_done")]
        print(f"[info] classification chain: papers={len(normal_entries)}, "
              f"models={'+'.join(classifier_names)}")

        def classify_group(group: list[dict], model_chain: list[dict]) -> None:
            limit = cfg.get("max_translate_chars", 2000)
            seen_models: set[tuple[str, str]] = set()
            model_chain = [
                m for m in model_chain
                if not (
                    (str(m.get("provider", "gemini")), classifier_spec_name(m))
                    in seen_models
                    or seen_models.add(
                        (str(m.get("provider", "gemini")),
                         classifier_spec_name(m)))
                )
            ]
            for i in range(0, len(group), batch_size):
                chunk = group[i: i + batch_size]
                texts = [
                    f"Title: {e['paper']['title']}\n\n"
                    f"Abstract: {e['paper']['abstract'][:limit]}"
                    for e in chunk
                ]
                gemini_stats["entries_attempted"] += len(chunk)
                for spec in model_chain:
                    if not classifier_key_present(spec) or classifier_dead(spec):
                        continue
                    model_name = classifier_spec_name(spec)
                    todo = [j for j, e in enumerate(chunk)
                            if not e.get("llm_done")]
                    if not todo:
                        break
                    gemini_stats["requests"] += 1
                    gid_lists = classify_llm_batch(
                        [texts[j] for j in todo], cfg, genres, spec=spec)
                    for j, gids in zip(todo, gid_lists):
                        if not gids:
                            continue
                        e = chunk[j]
                        gs = [genre_map[g] for g in gids if g in genre_map]
                        e["genres"] = gs if gs else [genre_by_id(None, genres)]
                        e["genres"] = postprocess_genres(
                            e["paper"], e["genres"], genres, cfg)
                        e["classification"] = classification_record(
                            e["paper"], gs, e["genres"], cfg)
                        e["llm_done"] = True
                        e["classifier"] = model_name
                        gemini_stats["entries_classified"] += 1

        classify_group(normal_entries, classifier_specs)

    # ---- fallback: TF-IDF classify (papers not yet classified) ------------
    # Reuses the pre-screen result when available (emergency fallback only).
    leftover = [e for e in entries if not e.get("llm_done")]
    for e in leftover:
        fallback_genres = classify_multi(
            e["paper"], genres, cfg, apply_policy=False)
        e["genres"] = postprocess_genres(
            e["paper"], fallback_genres, genres, cfg)
        e["classifier"] = "tfidf"
        e["classification"] = classification_record(
            e["paper"], fallback_genres, e["genres"], cfg)
    gemini_fallback = len(leftover)

    if dry_run:
        print("[info] Gemini usage: skipped (dry-run; TF-IDF only)")
    elif not use_llm_cls:
        print("[info] Gemini usage: skipped (classify_with_llm=false)")
    elif not gemini_stats["classifier_key_present"]:
        print("[info] LLM classification skipped (no classifier API key); "
              f"TF-IDF fallback={gemini_fallback}")
    else:
        translated = ""
        if gemini_stats["mode"] == "translate-and-classify":
            translated = f", translated={gemini_stats['entries_translated']}"
        print(
            "[info] Gemini usage: "
            f"mode={gemini_stats['mode']}, "
            f"model={gemini_stats['model']}, "
            f"requests={gemini_stats['requests']}, "
            f"classified={gemini_stats['entries_classified']}/"
            f"{gemini_stats['entries_attempted']}"
            f"{translated}, "
            f"tfidf_fallback={gemini_fallback}, "
            f"disabled_models="
            f"{sorted(_gemini_dead_models | _openai_compat_dead_models) or None}"
        )

    # ---- strict external review (only after quant-ph classification) ------
    # This ordering is intentional: adjacent-category candidates may use only
    # classifier capacity left after every normal quant-ph paper has received
    # its classification attempt.
    if resume_only:
        accepted_external: list[dict] = []
        new_external_reviews: dict[str, dict] = {}
        pending_external_reviews = cached_external_pending
        external_stats = {
            "mechanical_candidates": 0,
            "cached": 0,
            "reviewed": 0,
            "accepted": 0,
            "skipped": 0,
            "unreviewed": len(cached_external_pending),
            "skip_disagreements": 0,
            "single_skip_pending": 0,
            "requests": 0,
            "classifier_counts": {},
        }
    else:
        accepted_external, new_external_reviews, external_stats = (
            review_external_candidates(
                external_candidates, cfg, genres, cached_external_reviews,
                dry_run=dry_run))
        pending_external_reviews = dict(cached_external_pending)
        pending_external_reviews.update(
            external_stats.pop("_pending_papers", {}))
        completed_review_keys = set(new_external_reviews)
        for category, rows in external_candidates.items():
            for paper in rows:
                key = f"{category}:{paper['id']}"
                cached = cached_external_reviews.get(key)
                if external_cache_is_current(cached, cfg):
                    completed_review_keys.add(key)
        for key in completed_review_keys:
            pending_external_reviews.pop(key, None)
    if external_stats["mechanical_candidates"]:
        print(
            "[info] external review (after quant-ph): "
            f"candidates={external_stats['mechanical_candidates']}, "
            f"cached={external_stats['cached']}, "
            f"reviewed={external_stats['reviewed']}, "
            f"accepted={external_stats['accepted']}, "
            f"skipped={external_stats['skipped']}, "
            f"unreviewed={external_stats['unreviewed']}, "
            f"skip_disagreements={external_stats['skip_disagreements']}, "
            f"single_skip_pending={external_stats['single_skip_pending']}, "
            f"requests={external_stats['requests']}"
        )
    cached_external_reviews.update(new_external_reviews)
    entries.extend({
        "paper": paper,
        "genres": [
            genre_map[gid]
            for gid in paper.get("external_genre_ids", [])
            if gid in genre_map
        ],
        "jp": None,
        "jp_title": None,
        "need_tr": bool(paper["abstract"]),
        "allow_untranslated": False,
        "llm_done": True,
        "classifier": paper.get(
            "external_classifier", "cached-external-review"),
        "classification": paper.get("external_classification"),
    } for paper in accepted_external)

    def finish_queue_phase(label: str) -> None:
        # Recovered-from-OAI notices are reported here because the delivery
        # phase runs in a separate process and cannot see this run's sources.
        if source_failures or source_notices:
            notify_run_report({
                "source": "arXiv取得・配信準備",
                "fetched": len(papers),
                "candidates": len(pending) + len(accepted_external),
                "messages": 0,
                "posted": [],
                "deferred": [],
                "failed": [],
                "source_failures": source_failures,
                "source_notices": source_notices,
                "gemini": gemini_stats,
                "classifier_counts": dict(Counter(
                    e.get("classifier", "tfidf") for e in entries)),
                "tfidf_fallback": gemini_fallback,
                "translated": dict(_translation_success),
                "dead_translators": dead_translators(cfg),
                "external_review": external_stats,
            }, cfg)
        print(f"{label} {len(entries)} queued paper(s); "
              f"source_failures={len(source_failures)}")
        if source_failures:
            raise SystemExit(2)

    # Save newly discovered/classified papers before translation starts.  A
    # translator outage therefore cannot make a normal quant-ph paper depend
    # on remaining present in the next RSS snapshot.
    if not dry_run and not resume_only:
        for entry in entries:
            pid = entry["paper"]["id"]
            if pid not in completed:
                deliveries[pid] = merge_entry_into_delivery(
                    entry, deliveries.get(pid))
        strip_completed_external_papers(
            cached_external_reviews, completed, set(deliveries))
        persist_bot_state(
            state, completed, deliveries, cached_external_reviews,
            pending_external_reviews, external_cursors)
    if discover_only:
        finish_queue_phase("discovered")
        return

    # ---- translation via chain (all papers without jp) --------------------
    # Covers path B (Gemini classify-only) and TF-IDF fallback papers.
    # Also covers path A papers where Gemini failed to return a translation.
    # Every quant-ph paper sorts before every external paper, irrespective of
    # genre, so external sources cannot consume translation quota first.
    entries.sort(key=lambda e: translation_priority(e, cfg))
    if not dry_run and not deliver_only and not discover_only:
        load_translation_budget(state, cfg)
        to_tr = [e for e in entries if e["need_tr"] and e["jp"] is None and (
            e["genres"] or not cfg.get("translate_only_matched", False))]
        for i in range(0, len(to_tr), batch_size):
            chunk = to_tr[i: i + batch_size]
            abstracts = [e["paper"]["abstract"] for e in chunk]
            google_allowed = [google_translation_allowed(e, cfg) for e in chunk]
            for e, jp in zip(
                    chunk, translate_batch(abstracts, cfg, google_allowed)):
                e["jp"] = jp
            for e, allowed in zip(chunk, google_allowed):
                if (e["jp"] is None and not allowed
                        and cfg.get("allow_untranslated_low_priority", False)):
                    e["allow_untranslated"] = True

        if show_translated_title(cfg):
            to_title_tr = [
                e for e in entries
                if e["paper"].get("title") and e["jp_title"] is None
                and not e.get("allow_untranslated", False)
                and not (
                    cfg.get("require_translation", True)
                    and e["need_tr"] and e["jp"] is None
                )
            ]
            for i in range(0, len(to_title_tr), batch_size):
                chunk = to_title_tr[i: i + batch_size]
                titles = [e["paper"]["title"] for e in chunk]
                google_allowed = [google_translation_allowed(e, cfg)
                                  for e in chunk]
                for e, jp_title in zip(
                        chunk, translate_batch(titles, cfg, google_allowed)):
                    e["jp_title"] = jp_title

        # The translation phase is the only process that observes backend
        # failures, so it records them for the delivery report and raises
        # the full-outage alert itself.
        record_translation_status(state, entries, cfg)

    # ---- dry-run: print classification results and exit --------------------
    if dry_run:
        print(f"[dry-run] {len(entries)} papers from feed (seen_ids ignored)\n")
        label_width = max(
            (sum(len(g["name"]) for g in e["genres"] if g) + len(e["genres"]) - 1
             for e in entries if e.get("genres")),
            default=7,
        )
        for e in entries:
            label = ", ".join(g["name"] for g in e["genres"] if g) or "other"
            cats = ", ".join(e["paper"]["categories"][:3])
            title = e["paper"]["title"][:72]
            print(f"  [{label:<{label_width}}]  {title}")
            print(f"  {'':>{label_width+2}}  cats={cats}  id={e['paper']['id']}")
        return

    # Persist a complete retry payload before any Discord side effect.  The
    # workflow commits this queue in a separate prepare phase before delivery.
    for entry in entries:
        pid = entry["paper"]["id"]
        if pid in completed:
            continue
        deliveries[pid] = merge_entry_into_delivery(
            entry, deliveries.get(pid))
    strip_completed_external_papers(
        cached_external_reviews, completed, set(deliveries))
    persist_bot_state(
        state, completed, deliveries, cached_external_reviews,
        pending_external_reviews, external_cursors)

    if prepare_only or translate_only:
        finish_queue_phase(
            "translated" if translate_only else "prepared")
        return

    # ---- post ---------------------------------------------------------------
    posted = deferred = 0
    posted_records: list[dict] = []
    deferred_records: list[dict] = []
    failed_records: list[dict] = []
    for e in entries:
        pid = e["paper"]["id"]
        delivery = deliveries.get(pid)
        if not isinstance(delivery, dict):
            continue
        if translation_missing(e, cfg):
            deferred += 1
            deferred_records.append({
                "id": e["paper"]["id"],
                "title": e.get("jp_title") or e["paper"]["title"],
                "link": e["paper"]["link"],
                "genre_names": [g["name"] for g in e["genres"] if g],
            })
            continue
        posted_channels: list[str] = []
        failed_channels: list[str] = []
        # Footer shows every assigned genre, not just the channel posted to.
        genre_label = ", ".join(g["name"] for g in e["genres"] if g)
        webhook_counts = Counter(
            webhook
            for genre in e["genres"]
            for webhook, _ in [resolve_webhook(genre)]
            if webhook
        )
        for genre in e["genres"]:
            gid = genre["id"]
            channel_state = delivery.setdefault("channels", {}).setdefault(
                gid, {"status": "pending"})
            if channel_state.get("status") == "delivered":
                continue
            webhook, genre_name = resolve_webhook(genre)
            if not webhook:
                save_error_diagnostic(
                    "configuration_error",
                    body=json.dumps({
                        "paper_id": pid,
                        "genre_id": gid,
                        "error": "destination webhook is not configured",
                    }))
                failed_channels.append(f"{genre_name}(webhook未設定)")
                continue
            if webhook_counts[webhook] > 1:
                save_error_diagnostic(
                    "configuration_error",
                    body=json.dumps({
                        "paper_id": pid,
                        "genre_id": gid,
                        "error": "destination webhook is duplicated",
                    }))
                failed_channels.append(f"{genre_name}(webhook重複)")
                continue
            if post_to_discord(
                    webhook, e["paper"], genre_label or genre_name, e["jp"],
                    e.get("jp_title"), cfg):
                posted_channels.append(genre_name)
                channel_state["status"] = "delivered"
                channel_state["delivered_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                record_delivery_success(
                    log, e, gid, genre_map, delivery, cfg)
                persist_bot_state(
                    state, completed, deliveries, cached_external_reviews,
                    pending_external_reviews, external_cursors)
                atomic_write_json(
                    LOG_PATH, log[-5000:], ensure_ascii=False)
                posted += 1
            else:
                failed_channels.append(genre_name)
            time.sleep(1.2)  # Discord webhook rate limit headroom
        record = {
            "id": e["paper"]["id"],
            "title": e.get("jp_title") or e["paper"]["title"],
            "link": e["paper"]["link"],
        }
        if posted_channels:
            posted_records.append({**record, "genre_names": posted_channels})
        if failed_channels:
            failed_records.append({**record, "genre_names": failed_channels})
        target_ids = list(delivery.get("genre_ids", []))
        channels = delivery.get("channels", {})
        if target_ids and all(
            isinstance(channels.get(gid), dict)
            and channels[gid].get("status") == "delivered"
            for gid in target_ids
        ):
            completed.add(pid)
            deliveries.pop(pid, None)
            strip_completed_external_papers(
                cached_external_reviews, completed, set(deliveries))
            persist_bot_state(
                state, completed, deliveries, cached_external_reviews,
                pending_external_reviews, external_cursors)

    # The full-outage alert was already decided by the translation pass
    # (record_translation_status). In the three-phase workflow this delivery
    # process never translates, so its live circuit-breaker flags are empty;
    # report the persisted result of the latest translation pass instead.
    translation_status = state.get("translation_status")
    if not isinstance(translation_status, dict):
        translation_status = {}
    notify_run_report({
        "source": "arXiv新着通知",
        "fetched": len(papers),
        "candidates": len(pending) + len(accepted_external),
        "messages": posted,
        "posted": posted_records,
        "deferred": deferred_records,
        "failed": failed_records,
        "source_failures": source_failures,
        "source_notices": source_notices,
        "gemini": gemini_stats,
        "classifier_counts": dict(Counter(
            e.get("classifier", "tfidf") for e in entries)),
        "tfidf_fallback": gemini_fallback,
        "translated": (translation_status.get("translated")
                       or dict(_translation_success)),
        "dead_translators": (translation_status.get("dead")
                             or dead_translators(cfg)),
        "unconfigured_translators": translation_status.get("unconfigured", []),
        "free_tier": translation_status.get("free_tier", {}),
        "translation_status_at": translation_status.get("at"),
        "external_review": external_stats,
    }, cfg)

    persist_bot_state(
        state, completed, deliveries, cached_external_reviews,
        pending_external_reviews, external_cursors)
    atomic_write_json(LOG_PATH, log[-5000:], ensure_ascii=False)
    print(f"posted {posted} papers ({len(papers)} fetched, "
          f"{deferred} deferred for retry)")
    if source_failures:
        raise SystemExit(2)
    if failed_records:
        raise SystemExit(3)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        save_error_diagnostic(
            "unhandled_exception", body=traceback.format_exc(), exception=exc)
        raise
