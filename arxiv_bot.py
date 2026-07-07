#!/usr/bin/env python3
"""
arXiv quant-ph -> Discord notifier with translated abstracts.

- Fetches the official arXiv RSS feed (rss.arxiv.org/rss/quant-ph)
- Filters out cross-listed papers whose primary category is irrelevant
  (e.g. cond-mat.*) while keeping quantum-information-adjacent categories
- Classifies papers into user-defined genres. A TF-IDF pre-screen first
  routes papers: those touching only core-topic genres go to the primary
  Gemini model (gemini-2.5-pro), the rest to the secondary model
  (gemini-2.5-flash) when the pro request budget is tight or pro is
  rate-limited out. The TF-IDF result itself is only posted as an
  emergency fallback when Gemini is entirely unavailable.
- Translates abstracts via the configurable translator chain
  (default: DeepL -> Azure -> Google); each backend stops for the run on quota exhaustion
  (circuit breaker), and any paper left untranslated is deferred, never
  posted in English.
- Posts one Discord embed per paper via webhook (per-genre webhooks);
  multi-genre papers are posted to every matching channel and the embed
  footer lists all assigned genres
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

_last_gemini_calls: dict[str, float] = {}   # per-model request pacing
_gemini_dead_models: set[str] = set()       # models given up for this run
_gemini_fail_streaks: dict[str, int] = {}   # per-model overload streaks

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
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    valid_ids = {g["id"] for g in genres}
    max_genres = cfg.get("classify_max_genres", 2)
    prompt = (
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
    out = _gemini_request(prompt, cfg, model=model)
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
            results[k] = normalize_genre_ids(gids, valid_ids)
    return results


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


def translation_priority(entry: dict, cfg: dict) -> tuple[int, str]:
    """Sort key for translating higher-priority Discord channels first."""
    priority = cfg.get("translation_priority_genres") or []
    rank = {genre_id: i for i, genre_id in enumerate(priority)}
    genre_ids = [g["id"] for g in entry.get("genres", []) if g]
    best = min((rank.get(gid, len(rank)) for gid in genre_ids),
               default=len(rank))
    return best, entry["paper"]["id"]


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

    lines = [
        f"📥 フィード取得: {report.get('fetched', 0)}件 / "
        f"新規投稿対象: {report.get('candidates', 0)}件",
        f"📤 投稿成功: {len(posted)}論文({report.get('messages', 0)}メッセージ)"
        f" / ⏸ 翻訳持ち越し: {len(deferred)}件"
        f" / ❌ 投稿失敗: {len(failed)}件",
    ]
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

    if failed:
        icon, color = "🚨", 0xE74C3C
    elif deferred:
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
    entries: list[dict[str, Any]] = [
        {
            "paper": p,
            "genres": [],
            "jp": None,
            "jp_title": None,
            "need_tr": bool(p["abstract"]),
            "allow_untranslated": False,
        }
        for p in pending
    ]

    batch_size = max(1, cfg.get("translate_batch_size", 5))
    use_llm_cls = cfg.get("classify_with_llm", True)
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    llm_first = use_llm_cls and chain and chain[0] == "gemini"
    llm_classify_only = use_llm_cls and not llm_first
    genre_map = {g["id"]: g for g in genres}
    model_primary = cfg.get("gemini_model_primary") or cfg.get(
        "gemini_model", "gemini-2.5-flash")
    model_secondary = cfg.get("gemini_model_secondary") or model_primary
    gemini_stats = {
        "mode": "disabled",
        "model": f"{model_primary}+{model_secondary}"
                 if model_secondary != model_primary else model_primary,
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
    #     always classified by the primary model (gemini-2.5-pro)
    #   - the rest -> "deferred" group: also the primary model while the
    #     estimated request count fits gemini_primary_run_budget, otherwise
    #     the secondary model (gemini-2.5-flash)
    # Either group falls through to the other model when one is rate-limited
    # out mid-run (per-model circuit breaker).
    elif llm_classify_only and not dry_run and gemini_stats["key_present"]:
        defer_ids = set(cfg.get("prescreen_defer_genres",
                                ["nisq", "hardware", "sensing",
                                 "foundations", "other"]))
        for e in entries:
            e["prescreen"] = classify_multi(e["paper"], genres, cfg)
            pre_ids = {g["id"] for g in e["prescreen"] if g}
            e["route"] = "defer" if pre_ids & defer_ids else "priority"
        priority_group = [e for e in entries if e["route"] == "priority"]
        deferred_group = [e for e in entries if e["route"] == "defer"]

        est_requests = (math.ceil(len(priority_group) / batch_size)
                        + math.ceil(len(deferred_group) / batch_size))
        budget = cfg.get("gemini_primary_run_budget", 60)
        defer_chain = ([model_primary, model_secondary]
                       if est_requests <= budget
                       else [model_secondary])
        print(f"[info] classification routing: priority={len(priority_group)}, "
              f"deferred={len(deferred_group)}, est_requests={est_requests}, "
              f"deferred group uses "
              f"{'primary' if est_requests <= budget else 'secondary'} model")

        def classify_group(group: list[dict], model_chain: list[str]) -> None:
            limit = cfg.get("max_translate_chars", 2000)
            seen_models: set[str] = set()
            model_chain = [m for m in model_chain
                           if not (m in seen_models or seen_models.add(m))]
            for i in range(0, len(group), batch_size):
                chunk = group[i: i + batch_size]
                texts = [
                    f"Title: {e['paper']['title']}\n\n"
                    f"Abstract: {e['paper']['abstract'][:limit]}"
                    for e in chunk
                ]
                gemini_stats["entries_attempted"] += len(chunk)
                for model in model_chain:
                    if model in _gemini_dead_models:
                        continue
                    todo = [j for j, e in enumerate(chunk)
                            if not e.get("llm_done")]
                    if not todo:
                        break
                    gemini_stats["requests"] += 1
                    gid_lists = classify_gemini_batch(
                        [texts[j] for j in todo], cfg, genres, model=model)
                    for j, gids in zip(todo, gid_lists):
                        if not gids:
                            continue
                        e = chunk[j]
                        gs = [genre_map[g] for g in gids if g in genre_map]
                        e["genres"] = gs if gs else [genre_by_id(None, genres)]
                        e["genres"] = postprocess_genres(
                            e["paper"], e["genres"], genres, cfg)
                        e["llm_done"] = True
                        e["classifier"] = model
                        gemini_stats["entries_classified"] += 1

        classify_group(priority_group, [model_primary, model_secondary])
        classify_group(deferred_group, defer_chain)

    # ---- fallback: TF-IDF classify (papers not yet classified) ------------
    # Reuses the pre-screen result when available (emergency fallback only).
    leftover = [e for e in entries if not e.get("llm_done")]
    for e in leftover:
        e["genres"] = e.get("prescreen") or classify_multi(
            e["paper"], genres, cfg)
        e["classifier"] = "tfidf"
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
            f"disabled_models={sorted(_gemini_dead_models) or None}"
        )

    # ---- translation via chain (all papers without jp) --------------------
    # Covers path B (Gemini classify-only) and TF-IDF fallback papers.
    # Also covers path A papers where Gemini failed to return a translation.
    entries.sort(key=lambda e: translation_priority(e, cfg))
    if not dry_run:
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

    # ---- post ---------------------------------------------------------------
    require_tr = cfg.get("require_translation", True)
    posted = deferred = 0
    posted_records: list[dict] = []
    deferred_records: list[dict] = []
    failed_records: list[dict] = []
    for e in entries:
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
        posted_webhooks: set[str] = set()
        posted_channels: list[str] = []
        failed_channels: list[str] = []
        paper_logged = False
        # Footer shows every assigned genre, not just the channel posted to.
        genre_label = ", ".join(g["name"] for g in e["genres"] if g)
        for genre in e["genres"]:
            webhook, genre_name = resolve_webhook(genre)
            if not webhook or webhook in posted_webhooks:
                continue
            if post_to_discord(
                    webhook, e["paper"], genre_label or genre_name, e["jp"],
                    e.get("jp_title"), cfg):
                posted_webhooks.add(webhook)
                posted_channels.append(genre_name)
                if not paper_logged:
                    seen.add(e["paper"]["id"])
                    log.append({
                        "id": e["paper"]["id"],
                        "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime()),
                        "title": e["paper"]["title"],
                        "title_ja": e.get("jp_title"),
                        "title_translated": e.get("jp_title"),
                        "translation_language": target_language(cfg),
                        "authors": e["paper"]["authors"],
                        "link": e["paper"]["link"],
                        "primary": e["paper"]["primary"],
                        "announce_type": e["paper"]["announce_type"],
                        "genre_ids": [g["id"] for g in e["genres"] if g],
                        "genre_names": [g["name"] for g in e["genres"] if g],
                        "classifier": e.get("classifier", "tfidf"),
                        "abstract_en": e["paper"]["abstract"],
                        "abstract_ja": e["jp"],
                        "abstract_translated": e["jp"],
                    })
                    paper_logged = True
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

    if deferred > 0:
        dead = dead_translators(cfg)
        chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
        if chain and len(dead) == len(chain):
            notify_translation_outage(deferred, dead)

    notify_run_report({
        "source": "arXiv新着通知",
        "fetched": len(papers),
        "candidates": len(pending),
        "messages": posted,
        "posted": posted_records,
        "deferred": deferred_records,
        "failed": failed_records,
        "gemini": gemini_stats,
        "classifier_counts": dict(Counter(
            e.get("classifier", "tfidf") for e in entries)),
        "tfidf_fallback": gemini_fallback,
        "translated": dict(_translation_success),
        "dead_translators": dead_translators(cfg),
    }, cfg)

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
