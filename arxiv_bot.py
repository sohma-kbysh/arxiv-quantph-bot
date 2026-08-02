#!/usr/bin/env python3
"""
arXiv quant-ph -> Discord notifier with translated abstracts.

- Fetches the official arXiv RSS feed (rss.arxiv.org/rss/quant-ph)
- Uses custom arXiv API queries to find non-cross-listed candidates from
  selected adjacent categories, then subjects them to strict LLM review with
  an explicit skip decision before they can enter configured genre channels
- Filters out cross-listed papers whose primary category is irrelevant
  (e.g. cond-mat.*) while keeping quantum-information-adjacent categories
- Classifies papers into user-defined genres. A TF-IDF pre-screen routes
  papers through the configured classifier chain (currently Gemini 2.5 Flash,
  Gemini 2.5 Flash Lite, then Cerebras gpt-oss-120b). The TF-IDF result itself
  is only posted as an emergency fallback when every LLM is unavailable.
- Translates abstracts via the configurable translator chain
  (default: DeepL -> Azure -> Google); each backend stops for the run on quota exhaustion
  (circuit breaker), and any paper left untranslated is deferred, never
  posted in English.
- Persists a durable per-paper/per-channel queue before translation and
  Discord delivery. Multi-genre papers are retried only for channels that have
  not acknowledged delivery, and the embed footer lists all assigned genres.
- Posts a per-run summary report (in Japanese) to the bot-emergency
  channel: which papers went to which channels, deferrals, failures

Standard library only. Designed to run on GitHub Actions.
"""

import json
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


def http_get(url: str, timeout: int = 30,
             headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    papers = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = re.sub(
            r"\s+", " ", entry.findtext("atom:title", "", ATOM_NS)).strip()
        abstract = re.sub(
            r"\s+", " ", entry.findtext("atom:summary", "", ATOM_NS)).strip()
        id_url = entry.findtext("atom:id", "", ATOM_NS).strip()
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
            rows = parse_external_atom(http_get(url), category)
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
    "quantum qubit qubits state states system systems".split()
)
_classifier_cache = None  # (genre_tf, idf) precomputed once per run


def _tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9]*", text.lower())
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
    """Build TF vectors and IDF weights from genre descriptions + keywords.

    Terms appearing in all genres get IDF=0 (e.g. "quantum"), so only
    discriminative vocabulary contributes to similarity scores.
    """
    tf: dict[str, Counter] = {}
    for g in genres:
        words = _tokenize(
            f"{g.get('description', '')} {' '.join(_keyword_tokens(g.get('keywords', [])))}"
        )
        tf[g["id"]] = Counter(words)
    N = len(tf)
    df: dict[str, int] = {}
    for vec in tf.values():
        for term in vec:
            df[term] = df.get(term, 0) + 1
    idf = {t: math.log(N / d) for t, d in df.items() if d < N}
    return tf, idf


def _phrase_pattern(phrase: str) -> str:
    parts = [re.escape(p) for p in re.findall(r"[a-z][a-z0-9]*", phrase.lower())]
    return r"\b" + r"[\s-]+".join(parts) + r"\b" if parts else r"$^"


def _keyword_evidence_scores(paper: dict, genres: list[dict],
                             cfg: dict | None = None) -> dict[str, float]:
    title = paper.get("title", "").lower()
    abstract = paper.get("abstract", "").lower()
    scores = {g["id"]: 0.0 for g in genres}

    title_phrase = cfg.get("fallback_title_phrase_bonus", 0.35) if cfg else 0.35
    abstract_phrase = cfg.get("fallback_abstract_phrase_bonus", 0.18) if cfg else 0.18
    title_token = cfg.get("fallback_title_token_bonus", 0.10) if cfg else 0.10
    abstract_token = cfg.get("fallback_abstract_token_bonus", 0.03) if cfg else 0.03

    for g in genres:
        gid = g["id"]
        for keyword in g.get("keywords", []):
            key = str(keyword).strip().lower()
            if not key:
                continue
            if re.search(r"[\s-]", key):
                pattern = _phrase_pattern(key)
                if re.search(pattern, title):
                    scores[gid] += title_phrase
                elif re.search(pattern, abstract):
                    scores[gid] += abstract_phrase
            elif key not in _STOPWORDS:
                pattern = r"\b" + re.escape(key) + r"\w*\b"
                if re.search(pattern, title):
                    scores[gid] += title_token
                elif re.search(pattern, abstract):
                    scores[gid] += abstract_token

    for gid, keywords in (cfg or {}).get("fallback_keyword_boosts", {}).items():
        if gid not in scores:
            continue
        for keyword in keywords:
            pattern = _phrase_pattern(str(keyword))
            if re.search(pattern, title):
                scores[gid] += title_phrase
            elif re.search(pattern, abstract):
                scores[gid] += abstract_phrase

    return scores


def _score_genres(paper: dict, genres: list[dict],
                  cfg: dict | None = None) -> dict[str, float]:
    """Compute TF-IDF cosine similarity + category hint scores for each genre."""
    global _classifier_cache
    if _classifier_cache is None:
        _classifier_cache = _build_tfidf(genres)
    genre_tf, idf = _classifier_cache

    paper_vec = {k: v * idf.get(k, 0.0)
                 for k, v in Counter(_tokenize(
                     f"{paper['title']} {paper['abstract']}")).items()}
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
        for cat in paper.get("categories", []):
            if cat in strong_other and "other" in scores:
                scores["other"] += 1.0
            else:
                gid = hints.get(cat)
                if gid and gid in scores:
                    scores[gid] += 0.15
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
                   cfg: dict | None = None) -> list[dict]:
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
    result: list[dict] = []
    for gid in ranked[:max_genres]:
        if gid not in genre_map:
            continue
        if result and scores[gid] < best_score * sec_ratio:
            break
        result.append(genre_map[gid])
    result = result if result else ([fallback] if fallback else [])
    return postprocess_genres(paper, result, genres, cfg)


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
    text = f"{paper.get('title', '')} {paper.get('abstract', '')}".lower()
    for gid, keywords in cfg.get("force_genre_keywords", {}).items():
        if gid in selected_ids or gid not in genre_map:
            continue
        for keyword in keywords:
            pattern = r"\b" + re.escape(str(keyword).lower()) + r"\w*\b"
            if re.search(pattern, text):
                fallback_ids = {"other"}
                result = [g for g in result if g.get("id") not in fallback_ids]
                result.append(genre_map[gid])
                selected_ids.add(gid)
                break
    return result


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
    """Per-model pacing. Free-tier gemini-2.5-pro allows only 5 RPM, so it
    defaults to 13s spacing; other models use gemini_min_interval_sec."""
    intervals = cfg.get("gemini_min_intervals", {})
    if model in intervals:
        return float(intervals[model])
    base = float(cfg.get("gemini_min_interval_sec", 7))
    if "pro" in model:
        return max(13.0, base)
    return base


def _gemini_request(prompt: str, cfg: dict, model: str | None = None) -> str | None:
    """One paced, retried Gemini call. Marks the model dead for this run on
    persistent quota exhaustion (429) or sustained server overload (500/503);
    other models keep working."""
    model = model or cfg.get("gemini_model", "gemini-2.5-flash")
    if model in _gemini_dead_models:
        return None
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    min_interval = gemini_min_interval(cfg, model)
    max_retries = cfg.get("gemini_max_retries", 4)

    for attempt in range(max_retries + 1):
        wait = _last_gemini_calls.get(model, 0.0) + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_gemini_calls[model] = time.time()

        status, body = http_post_json(
            url, {"contents": [{"parts": [{"text": prompt}]}]})
        if status == 200:
            _gemini_fail_streaks[model] = 0
            try:
                data = json.loads(body)
                return (data["candidates"][0]["content"]["parts"][0]["text"]
                        .strip())
            except (KeyError, IndexError, json.JSONDecodeError):
                save_error_diagnostic(
                    "invalid_response", url=url, method="POST", status=200,
                    body=body)
                return None
        if status in (429, 500, 503) and attempt < max_retries:
            backoff = min(60, 10 * (2 ** attempt))
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
        elif status in (500, 503):
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
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, json.JSONDecodeError):
            save_error_diagnostic(
                "invalid_response", url=url, method="POST", status=200,
                body=body)
            print(f"[warn] {name} returned an unexpected response: "
                  f"{body[:500]!r}", file=sys.stderr)
            return None
    return None


def classifier_model_specs(cfg: dict) -> list[dict]:
    primary = cfg.get("gemini_model_primary") or cfg.get(
        "gemini_model", "gemini-2.5-flash")
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

    Output tokens are minimal (just genre IDs), so quota consumption is
    roughly 1/50 of the combined translate+classify request. Use this when
    translation is handled by the configured translator chain instead.

    `model` overrides the Gemini model (defaults to gemini_model_primary,
    then gemini_model). Returns a list of genre ID lists (empty list when
    Gemini fails for that entry).
    """
    model = model or cfg.get("gemini_model_primary") or cfg.get(
        "gemini_model", "gemini-2.5-flash")
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
    specs = classifier_model_specs(cfg)
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
            if isinstance(cached, dict) and "genre_ids" in cached:
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
    """Official Cloud Translation API (v2). Free tier: 500k chars/month."""
    global _google_dead, _last_google_call
    if _google_dead:
        return None
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    if not key:
        _google_dead = True
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
            sub = translate_gemini_batch(subset, cfg)
        elif backend == "deepl":
            sub = [translate_deepl(t, cfg) for t in subset]
        elif backend == "azure":
            sub = [translate_azure(t, cfg) for t in subset]
        elif backend == "google":
            sub = [translate_google(t, cfg) for t in subset]
        else:
            save_error_diagnostic(
                "configuration_error",
                body=f"unknown translator backend: {backend}")
            print(f"[warn] unknown translator '{backend}'", file=sys.stderr)
            continue
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
}


def dead_translators(cfg: dict) -> list[str]:
    """Backends in the configured chain that gave up for this run."""
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    dead = []
    for b in chain:
        if b == "gemini":
            # Gemini-as-translator uses gemini_model; dead flags are per model.
            if cfg.get("gemini_model", "gemini-2.5-flash") in _gemini_dead_models:
                dead.append(b)
        elif _TRANSLATOR_DEAD_FLAGS.get(b, lambda: False)():
            dead.append(b)
    return dead


def notify_translation_outage(deferred: int, dead: list[str]) -> None:
    """Warn the bot-emergency Discord channel when every translator backend
    in the chain is unavailable, so papers are being silently deferred."""
    webhook = os.environ.get("DISCORD_WEBHOOK_BOT_EMERGENCY", "")
    content = (
        "⚠️ All translation backends are unavailable "
        f"({', '.join(dead)}); {deferred} paper(s) deferred until "
        "translation recovers."
    )
    if not webhook:
        print(f"[warn] {content} (no DISCORD_WEBHOOK_BOT_EMERGENCY configured "
              "to send this notice)", file=sys.stderr)
        return
    status, body = http_post_json(webhook, {"content": content})
    if status not in (200, 204):
        print(f"[warn] failed to send translation-outage notice: "
              f"HTTP {status} {body[:200]!r}", file=sys.stderr)


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
    if translated:
        usage = " / ".join(f"{b}: {n}件" for b, n in translated.items())
        lines.append(f"🌐 翻訳成功(タイトル含む): {usage}")
    dead = report.get("dead_translators") or []
    if dead:
        lines.append(f"⚠️ この実行で停止した翻訳バックエンド: {', '.join(dead)}")
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
    for key in ("external_reviews", "external_pending", "external_cursors"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def entry_from_delivery(record: dict, genre_map: dict[str, dict]) -> dict:
    """Restore a normal in-memory entry from a durable delivery record."""
    return {
        "paper": record["paper"],
        "genres": [
            genre_map[gid] for gid in record.get("genre_ids", [])
            if gid in genre_map
        ],
        "jp": record.get("abstract_translated"),
        "jp_title": record.get("title_translated"),
        "need_tr": bool(record.get(
            "need_translation", record["paper"].get("abstract"))),
        "allow_untranslated": bool(record.get("allow_untranslated", False)),
        "llm_done": True,
        "classifier": record.get("classifier", "persisted-delivery"),
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
    backfill_raw = os.environ.get("EXTERNAL_ARXIV_LOOKBACK_DAYS", "").strip()
    if backfill_raw:
        try:
            backfill_days = int(backfill_raw)
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

    papers: dict[str, dict] = {}
    if not resume_only:
        for cat in cfg.get("feeds", ["quant-ph"]):
            try:
                for p in fetch_feed(cat):
                    p["source_feed"] = cat
                    papers.setdefault(p["id"], p)
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

    # Adjacent arXiv categories use custom API queries as a high-recall,
    # mechanical first stage. Their results are never sent through the normal
    # "other" fallback: a separate strict LLM review must explicitly accept
    # them into only the configured destination channels.
    external_candidates: dict[str, list[dict]] = {}
    external_rules = cfg.get("external_arxiv_queries", {})
    using_test_feed = bool(os.environ.get("ARXIV_TEST_FEED", ""))
    if (
        not resume_only
        and isinstance(external_rules, dict)
        and not using_test_feed
    ):
        for cat, rule in external_rules.items():
            if not isinstance(rule, dict) or not rule.get("enabled", True):
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
                and review.get("genre_ids")
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
        entry_from_delivery(record, genre_map)
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
        "gemini_model", "gemini-2.5-flash")
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
                    e["llm_done"] = True
                    e["classifier"] = cfg.get("gemini_model",
                                              "gemini-2.5-flash")
                    gemini_stats["entries_classified"] += 1

    # ---- path B: Gemini classify only, translate via DeepL/Google ---------
    # Used when classify_with_llm=true but "gemini" is NOT in translators.
    # Gemini output is ~genre IDs only, so quota usage is 1/50 of path A.
    #
    # A TF-IDF pre-screen routes the papers first (routing only; its labels
    # are never posted unless Gemini is entirely unavailable):
    #   - papers touching none of prescreen_defer_genres -> "priority" group,
    #     always classified by the configured primary model
    #   - the rest -> "deferred" group: also the primary model while the
    #     estimated request count fits gemini_primary_run_budget, otherwise
    #     the configured secondary model
    # Either group falls through to the other model when one is rate-limited
    # out mid-run (per-model circuit breaker).
    elif (llm_classify_only and not dry_run
          and gemini_stats["classifier_key_present"]):
        defer_ids = set(cfg.get("prescreen_defer_genres",
                                ["nisq", "hardware", "sensing",
                                 "foundations", "other"]))
        normal_entries = [e for e in entries if not e.get("llm_done")]
        for e in normal_entries:
            e["prescreen"] = classify_multi(e["paper"], genres, cfg)
            pre_ids = {g["id"] for g in e["prescreen"] if g}
            e["route"] = "defer" if pre_ids & defer_ids else "priority"
        priority_group = [
            e for e in normal_entries if e["route"] == "priority"]
        deferred_group = [
            e for e in normal_entries if e["route"] == "defer"]

        est_requests = (math.ceil(len(priority_group) / batch_size)
                        + math.ceil(len(deferred_group) / batch_size))
        budget = cfg.get("gemini_primary_run_budget", 60)
        primary_spec = {"provider": "gemini", "model": model_primary,
                        "name": model_primary}
        secondary_spec = {"provider": "gemini", "model": model_secondary,
                          "name": model_secondary}
        fallback_specs = [
            s for s in classifier_specs
            if classifier_spec_name(s) not in {model_primary, model_secondary}
        ]
        priority_chain = [primary_spec, secondary_spec, *fallback_specs]
        defer_chain = ([primary_spec, secondary_spec, *fallback_specs]
                       if est_requests <= budget
                       else [secondary_spec, *fallback_specs])
        print(f"[info] classification routing: priority={len(priority_group)}, "
              f"deferred={len(deferred_group)}, est_requests={est_requests}, "
              f"deferred group uses "
              f"{'primary' if est_requests <= budget else 'secondary'} model")

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
                        e["llm_done"] = True
                        e["classifier"] = model_name
                        gemini_stats["entries_classified"] += 1

        classify_group(priority_group, priority_chain)
        classify_group(deferred_group, defer_chain)

    # ---- fallback: TF-IDF classify (papers not yet classified) ------------
    # Reuses the pre-screen result when available (emergency fallback only).
    leftover = [e for e in entries if not e.get("llm_done")]
    for e in leftover:
        fallback_genres = e.get("prescreen") or classify_multi(
            e["paper"], genres, cfg)
        e["genres"] = postprocess_genres(
            e["paper"], fallback_genres, genres, cfg)
        e["classifier"] = "tfidf"
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
                if isinstance(cached, dict) and "genre_ids" in cached:
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
    } for paper in accepted_external)

    def finish_queue_phase(label: str) -> None:
        if source_failures:
            notify_run_report({
                "source": "arXiv取得・配信準備",
                "fetched": len(papers),
                "candidates": len(pending) + len(accepted_external),
                "messages": 0,
                "posted": [],
                "deferred": [],
                "failed": [],
                "source_failures": source_failures,
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
                if e["jp"] is None and not allowed:
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
    require_tr = cfg.get("require_translation", True)
    posted = deferred = 0
    posted_records: list[dict] = []
    deferred_records: list[dict] = []
    failed_records: list[dict] = []
    for e in entries:
        pid = e["paper"]["id"]
        delivery = deliveries.get(pid)
        if not isinstance(delivery, dict):
            continue
        if (e["need_tr"] and e["jp"] is None and require_tr
                and not e.get("allow_untranslated", False)):
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

    if deferred > 0:
        dead = dead_translators(cfg)
        chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
        if chain and len(dead) == len(chain):
            notify_translation_outage(deferred, dead)

    notify_run_report({
        "source": "arXiv新着通知",
        "fetched": len(papers),
        "candidates": len(pending) + len(accepted_external),
        "messages": posted,
        "posted": posted_records,
        "deferred": deferred_records,
        "failed": failed_records,
        "source_failures": source_failures,
        "gemini": gemini_stats,
        "classifier_counts": dict(Counter(
            e.get("classifier", "tfidf") for e in entries)),
        "tfidf_fallback": gemini_fallback,
        "translated": dict(_translation_success),
        "dead_translators": dead_translators(cfg),
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
