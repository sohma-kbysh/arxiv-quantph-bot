#!/usr/bin/env python3
"""
arXiv quant-ph -> Discord notifier with Japanese abstract translation.

- Fetches the official arXiv RSS feed (rss.arxiv.org/rss/quant-ph)
- Filters out cross-listed papers whose primary category is irrelevant
  (e.g. cond-mat.*) while keeping quantum-information-adjacent categories
- Classifies papers into user-defined genres. Primary path: the Gemini
  call translates AND classifies in one request. Fallback path (Gemini
  unavailable): keyword matching for the genre + the translator chain.
- Translates abstracts into Japanese via Gemini -> DeepL -> Google
  (configurable chain); each backend stops for the run on quota exhaustion
  (circuit breaker), and any paper left untranslated is deferred, never
  posted in English.
- Posts one Discord embed per paper via webhook (per-genre webhooks)

Standard library only. Designed to run once per day on GitHub Actions.
"""

import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
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

USER_AGENT = "arxiv-quantph-discord-bot/1.0 (personal research notifier)"


# ---------------------------------------------------------------- utilities

def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


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
        return e.code, e.read()
    except urllib.error.URLError as e:
        print(f"[warn] Connection error for {url}: {e.reason}", file=sys.stderr)
        return 0, b""
    except Exception as e:
        print(f"[warn] Unexpected request error for {url}: {e}", file=sys.stderr)
        return 0, b""


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


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
    "must both only even more most some any all one two new no".split()
)
_classifier_cache = None  # (genre_tf, idf) precomputed once per run


def _tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9]*", text.lower())
            if w not in _STOPWORDS and len(w) > 2]


def _build_tfidf(genres: list[dict]) -> tuple[dict, dict]:
    """Build TF vectors and IDF weights from genre descriptions + keywords.

    Terms appearing in all genres get IDF=0 (e.g. "quantum"), so only
    discriminative vocabulary contributes to similarity scores.
    """
    tf: dict[str, Counter] = {}
    for g in genres:
        words = _tokenize(
            f"{g.get('description', '')} {' '.join(g.get('keywords', []))}"
        )
        tf[g["id"]] = Counter(words)
    N = len(tf)
    df: dict[str, int] = {}
    for vec in tf.values():
        for term in vec:
            df[term] = df.get(term, 0) + 1
    idf = {t: math.log(N / d) for t, d in df.items() if d < N}
    return tf, idf


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
    quantph_equivalent = cfg.get(
        "cross_classify_primary_as_quantph", ["quant-ph", "cs.CR"])
    if primary and not category_matches(primary, quantph_equivalent):
        fallback = genre_by_id("other", genres)
        return [fallback] if fallback else result

    if category_matches(primary, cfg.get("category_other_overrides", [])):
        fallback = genre_by_id("other", genres)
        return [fallback] if fallback else result

    return apply_forced_genres(paper, result, genres, cfg)


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

_last_gemini_call = 0.0
_gemini_dead = False        # True when Gemini is given up for this run
_gemini_fail_streak = 0     # consecutive post-backoff overload failures

BATCH_TAG = re.compile(r"<<<(\d+)>>>")
# Tag form for the combined translate+classify call: <<<k|genre_id>>> or <<<k|id1,id2>>>
BATCH_TAG_CLS = re.compile(r"<<<(\d+)\s*\|\s*([A-Za-z0-9_,\s]+?)>>>")


def _gemini_request(prompt: str, cfg: dict) -> str | None:
    """One paced, retried Gemini call. Sets _gemini_dead on persistent
    quota exhaustion (429) or sustained server overload (500/503)."""
    global _last_gemini_call, _gemini_dead, _gemini_fail_streak
    if _gemini_dead:
        return None
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    model = cfg.get("gemini_model", "gemini-2.5-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    min_interval = cfg.get("gemini_min_interval_sec", 7)
    max_retries = cfg.get("gemini_max_retries", 4)

    for attempt in range(max_retries + 1):
        wait = _last_gemini_call + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_gemini_call = time.time()

        status, body = http_post_json(
            url, {"contents": [{"parts": [{"text": prompt}]}]})
        if status == 200:
            _gemini_fail_streak = 0
            try:
                data = json.loads(body)
                return (data["candidates"][0]["content"]["parts"][0]["text"]
                        .strip())
            except (KeyError, IndexError, json.JSONDecodeError):
                return None
        if status in (429, 500, 503) and attempt < max_retries:
            backoff = min(60, 10 * (2 ** attempt))
            print(f"[warn] Gemini HTTP {status}; retry in {backoff}s "
                  f"({attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(backoff)
            continue
        # Full backoff exhausted (or a non-retryable status).
        if status == 429:
            # Daily quota (requests per day) exhausted: never recovers today.
            print("[warn] Gemini daily quota appears exhausted; "
                  "skipping Gemini for the rest of this run.", file=sys.stderr)
            _gemini_dead = True
        elif status in (500, 503):
            # Server overload. One request surviving full backoff is bad
            # enough; if it keeps happening, stop hammering Gemini for this
            # run and let the translator chain fall through to DeepL/Google.
            _gemini_fail_streak += 1
            print(f"[warn] Gemini HTTP {status} after full backoff "
                  f"(streak {_gemini_fail_streak}/"
                  f"{cfg.get('gemini_overload_giveup', 2)})", file=sys.stderr)
            if _gemini_fail_streak >= cfg.get("gemini_overload_giveup", 2):
                print("[warn] Gemini appears overloaded; skipping Gemini for "
                      "the rest of this run (falling back to next translator).",
                      file=sys.stderr)
                _gemini_dead = True
        else:
            print(f"[warn] Gemini HTTP {status}: {body[:200]!r}",
                  file=sys.stderr)
        return None
    return None


def translate_gemini_batch(texts: list[str], cfg: dict) -> list[str | None]:
    """Translate several abstracts in one request using <<<k>>> delimiters."""
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    prompt = (
        f"以下に{len(texts)}件の量子情報科学分野のarXiv論文abstract(英語)を示す。"
        "各々を、専門用語は標準的な訳語(必要なら英語併記)を用いて"
        "学術的な日本語に翻訳せよ。\n"
        "出力では各訳文の直前に対応する番号タグ <<<k>>> をそのまま付し、"
        "タグと訳文以外の文字列(前置き・後書き)を一切含めないこと。\n\n"
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


def translate_classify_gemini_batch(
        texts: list[str], cfg: dict,
        genres: list[dict]) -> list[tuple[str | None, list[str]]]:
    """Translate AND classify several abstracts in a single Gemini request.

    Returns a list of (japanese_text, genre_ids) tuples.
    genre_ids is a list of 1-N valid genre id strings (empty on failure).
    """
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    valid_ids = {g["id"] for g in genres}
    max_genres = cfg.get("classify_max_genres", 2)
    prompt = (
        f"以下に{len(texts)}件の量子情報科学分野のarXiv論文のタイトルとabstract(英語)を示す。"
        "各論文について次の2つを行え。\n"
        f"(1) 各ジャンルの説明文を精読し、論文の主要な貢献を最もよく表すジャンルIDを"
        "下記の一覧から選ぶ。\n"
        "    - 1つのジャンルに明確に帰属する場合は1つのみ選ぶ。\n"
        f"    - 複数ジャンルにまたがり両方が主要な貢献である場合のみ、"
        f"優先度順に最大{max_genres}個までカンマ区切りで選ぶ(例: qec,ft)。\n"
        "    - 迷う場合は1つに絞ること。説明文に合致しない場合はotherを選ぶ。\n"
        "(2) abstractを、専門用語は標準的な訳語(必要なら英語併記)を用いて"
        "学術的な日本語に翻訳する。\n\n"
        "[ジャンル一覧]\n"
        + _genre_menu(genres)
        + "\n\n[出力形式]\n"
        "各エントリの訳文の直前に <<<k|genre_id>>> を付すこと。"
        "複数ジャンルの場合は <<<k|id1,id2>>> 形式(例: <<<1|qec,ft>>>)。"
        "タグと訳文以外の文字列(前置き・後書き・見出し)を一切含めないこと。\n\n"
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
            gids = [g for g in gids if g in valid_ids]
            results[k] = (t or None, gids)
    return results


def classify_gemini_batch(
        texts: list[str], cfg: dict,
        genres: list[dict]) -> list[list[str]]:
    """Classify papers using Gemini without translating (classification only).

    Output tokens are minimal (just genre IDs), so quota consumption is
    roughly 1/50 of the combined translate+classify request. Use this when
    translation is handled by DeepL or Google Cloud Translation instead.

    Returns a list of genre ID lists (empty list when Gemini fails for that entry).
    """
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    valid_ids = {g["id"] for g in genres}
    max_genres = cfg.get("classify_max_genres", 2)
    prompt = (
        f"以下に{len(texts)}件の量子情報科学分野のarXiv論文のタイトルとabstract(英語)を示す。"
        "各論文について次を行え。\n"
        f"各ジャンルの説明文を精読し、論文の主要な貢献を最もよく表すジャンルIDを"
        "下記の一覧から選ぶ。\n"
        "    - 1つのジャンルに明確に帰属する場合は1つのみ選ぶ。\n"
        f"    - 複数ジャンルにまたがり両方が主要な貢献である場合のみ、"
        f"優先度順に最大{max_genres}個までカンマ区切りで選ぶ(例: qec,ft)。\n"
        "    - 迷う場合は1つに絞ること。説明文に合致しない場合はotherを選ぶ。\n\n"
        "[ジャンル一覧]\n"
        + _genre_menu(genres)
        + "\n\n[出力形式]\n"
        "各エントリについて <<<k>>> の直後にジャンルIDのみを出力すること。"
        "複数の場合はカンマ区切り(例: <<<1>>> qec,ft)。"
        "ジャンルID・タグ・改行以外の文字列を一切含めないこと。\n\n"
        + numbered
    )
    out = _gemini_request(prompt, cfg)
    results: list[list[str]] = [[] for _ in range(len(texts))]
    if not out:
        return results
    for match in re.finditer(r"<<<(\d+)>>>[\s:-]*([A-Za-z0-9_][A-Za-z0-9_,\s]*)", out):
        try:
            k = int(match.group(1)) - 1
        except ValueError:
            continue
        if 0 <= k < len(texts):
            gids = [g.strip() for g in match.group(2).split(",")]
            results[k] = [g for g in gids if g in valid_ids]
    return results


_deepl_dead = False
_google_dead = False


def translate_deepl(text: str, cfg: dict) -> str | None:
    global _deepl_dead
    if _deepl_dead:
        return None
    key = os.environ.get("DEEPL_API_KEY", "")
    if not key:
        return None
    data = urllib.parse.urlencode(
        {"text": text, "target_lang": "JA", "source_lang": "EN"}
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
            body = json.loads(resp.read())
            return body["translations"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        print(f"[warn] DeepL HTTP {e.code}", file=sys.stderr)
        if e.code == 456:  # monthly quota exhausted on the free plan
            print("[warn] DeepL monthly quota exhausted; "
                  "skipping DeepL for the rest of this run.", file=sys.stderr)
            _deepl_dead = True
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] DeepL error: {e}", file=sys.stderr)
        return None


def translate_google(text: str, cfg: dict) -> str | None:
    """Official Cloud Translation API (v2). Free tier: 500k chars/month."""
    global _google_dead
    if _google_dead:
        return None
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    if not key:
        return None
    url = ("https://translation.googleapis.com/language/translate/v2"
           f"?key={urllib.parse.quote(key)}")
    status, body = http_post_json(
        url, {"q": text, "source": "en", "target": "ja", "format": "text"})
    if status == 200:
        try:
            data = json.loads(body)
            return data["data"]["translations"][0]["translatedText"].strip()
        except (KeyError, IndexError, json.JSONDecodeError):
            return None
    print(f"[warn] Google Translate HTTP {status}: {body[:200]!r}",
          file=sys.stderr)
    if status in (403, 429):  # quota / billing problem: stop hammering
        print("[warn] Google Translate quota/credential problem; "
              "skipping Google for the rest of this run.", file=sys.stderr)
        _google_dead = True
    return None


def translate_batch(texts: list[str], cfg: dict) -> list[str | None]:
    """Translate a chunk of abstracts through the configured backend chain.

    Each backend only receives the items that all previous backends
    failed to translate.
    """
    limit = cfg.get("max_translate_chars", 2000)
    texts = [t[:limit] for t in texts]
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]

    results: list[str | None] = [None] * len(texts)
    for backend in chain:
        missing = [i for i, r in enumerate(results) if r is None]
        if not missing:
            break
        subset = [texts[i] for i in missing]
        if backend == "gemini":
            sub = translate_gemini_batch(subset, cfg)
        elif backend == "deepl":
            sub = [translate_deepl(t, cfg) for t in subset]
        elif backend == "google":
            sub = [translate_google(t, cfg) for t in subset]
        else:
            print(f"[warn] unknown translator '{backend}'", file=sys.stderr)
            continue
        for i, r in zip(missing, sub):
            results[i] = r
    return results


# ----------------------------------------------------------------- discord

def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def post_to_discord(webhook: str, paper: dict, genre_name: str,
                    jp_abstract: str | None, cfg: dict) -> bool:
    desc = jp_abstract if jp_abstract else paper["abstract"]
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
    """Return (webhook_url, genre_name); fall back to the general webhook."""
    general = os.environ.get("DISCORD_WEBHOOK_GENERAL", "")
    if genre is None:
        return general, "general"
    url = os.environ.get(genre.get("webhook_env", ""), "") or general
    return url, genre["name"]


# -------------------------------------------------------------------- main

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"seen": []})
    seen = set() if dry_run else set(state["seen"])
    log: list[dict] = load_json(LOG_PATH, [])
    genres = cfg.get("genres", [])

    papers: dict[str, dict] = {}
    for cat in cfg.get("feeds", ["quant-ph"]):
        try:
            for p in fetch_feed(cat):
                papers.setdefault(p["id"], p)
        except Exception as err:  # noqa: BLE001
            print(f"[warn] feed {cat} failed: {err}", file=sys.stderr)
        if not dry_run:
            time.sleep(3)  # be polite to arXiv

    # ---- determine which papers to post (filtering only) ------------------
    pending = []  # papers passing should_post, not yet seen
    for pid, paper in papers.items():
        if pid in seen or not should_post(paper, cfg):
            continue
        pending.append(paper)

    # Each entry carries the paper plus its resolved genres + translation.
    # genres is always a non-empty list; fallback genre is "other".
    entries: list[dict[str, Any]] = [{"paper": p, "genres": [], "jp": None, "need_tr":
                                     bool(p["abstract"])} for p in pending]

    batch_size = max(1, cfg.get("translate_batch_size", 5))
    use_llm_cls = cfg.get("classify_with_llm", True)
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    llm_first = use_llm_cls and chain and chain[0] == "gemini"
    llm_classify_only = use_llm_cls and not llm_first
    genre_map = {g["id"]: g for g in genres}
    gemini_stats = {
        "mode": "disabled",
        "model": cfg.get("gemini_model", "gemini-2.5-flash"),
        "key_present": bool(os.environ.get("GEMINI_API_KEY", "")),
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
        for i in range(0, len(entries), batch_size):
            chunk = entries[i: i + batch_size]
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
                    gemini_stats["entries_classified"] += 1

    # ---- path B: Gemini classify only, translate via DeepL/Google ---------
    # Used when classify_with_llm=true but "gemini" is NOT in translators.
    # Gemini output is ~genre IDs only, so quota usage is 1/50 of path A.
    elif llm_classify_only and not dry_run and gemini_stats["key_present"]:
        for i in range(0, len(entries), batch_size):
            chunk = entries[i: i + batch_size]
            limit = cfg.get("max_translate_chars", 2000)
            abstracts = [
                f"Title: {e['paper']['title']}\n\nAbstract: {e['paper']['abstract'][:limit]}"
                for e in chunk
            ]
            gemini_stats["requests"] += 1
            gemini_stats["entries_attempted"] += len(chunk)
            gid_lists = classify_gemini_batch(abstracts, cfg, genres)
            for e, gids in zip(chunk, gid_lists):
                if gids:
                    gs = [genre_map[g] for g in gids if g in genre_map]
                    e["genres"] = gs if gs else [genre_by_id(None, genres)]
                    e["genres"] = postprocess_genres(
                        e["paper"], e["genres"], genres, cfg)
                    e["llm_done"] = True
                    gemini_stats["entries_classified"] += 1

    # ---- fallback: TF-IDF classify (papers not yet classified) ------------
    leftover = [e for e in entries if not e.get("llm_done")]
    for e in leftover:
        e["genres"] = classify_multi(e["paper"], genres, cfg)
    gemini_fallback = len(leftover)

    if dry_run:
        print("[info] Gemini usage: skipped (dry-run; TF-IDF only)")
    elif not use_llm_cls:
        print("[info] Gemini usage: skipped (classify_with_llm=false)")
    elif not gemini_stats["key_present"]:
        print("[info] Gemini usage: skipped (GEMINI_API_KEY missing); "
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
            f"disabled_for_run={_gemini_dead}"
        )

    # ---- translation via chain (all papers without jp) --------------------
    # Covers path B (Gemini classify-only) and TF-IDF fallback papers.
    # Also covers path A papers where Gemini failed to return a translation.
    if not dry_run:
        to_tr = [e for e in entries if e["need_tr"] and e["jp"] is None and (
            e["genres"] or not cfg.get("translate_only_matched", False))]
        for i in range(0, len(to_tr), batch_size):
            chunk = to_tr[i: i + batch_size]
            abstracts = [e["paper"]["abstract"] for e in chunk]
            for e, jp in zip(chunk, translate_batch(abstracts, cfg)):
                e["jp"] = jp

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

    # ---- post ---------------------------------------------------------------
    require_tr = cfg.get("require_translation", True)
    posted = deferred = 0
    for e in entries:
        if e["need_tr"] and e["jp"] is None and require_tr:
            deferred += 1
            continue
        posted_webhooks: set[str] = set()
        paper_logged = False
        for genre in e["genres"]:
            webhook, genre_name = resolve_webhook(genre)
            if not webhook or webhook in posted_webhooks:
                continue
            if post_to_discord(webhook, e["paper"], genre_name, e["jp"], cfg):
                posted_webhooks.add(webhook)
                if not paper_logged:
                    seen.add(e["paper"]["id"])
                    log.append({
                        "id": e["paper"]["id"],
                        "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime()),
                        "title": e["paper"]["title"],
                        "authors": e["paper"]["authors"],
                        "link": e["paper"]["link"],
                        "primary": e["paper"]["primary"],
                        "announce_type": e["paper"]["announce_type"],
                        "genre_ids": [g["id"] for g in e["genres"] if g],
                        "genre_names": [g["name"] for g in e["genres"] if g],
                        "abstract_en": e["paper"]["abstract"],
                        "abstract_ja": e["jp"],
                    })
                    paper_logged = True
                posted += 1
            time.sleep(1.2)  # Discord webhook rate limit headroom

    # Keep the state file bounded.
    state["seen"] = sorted(seen)[-3000:]
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")
    LOG_PATH.write_text(
        json.dumps(log[-5000:], indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"posted {posted} papers ({len(papers)} fetched, "
          f"{deferred} deferred for retry)")


if __name__ == "__main__":
    main()
