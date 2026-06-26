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
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

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


def classify(paper: dict, genres: list[dict]) -> dict | None:
    """Return the genre with the most keyword hits in title+abstract."""
    text = f"{paper['title']} {paper['abstract']}".lower()
    best, best_score = None, 0
    for g in genres:
        score = sum(1 for kw in g["keywords"] if kw.lower() in text)
        if score > best_score:
            best, best_score = g, score
    return best  # None => uncategorized ("general")


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
# Tag form for the combined translate+classify call: <<<k|genre_id>>>
BATCH_TAG_CLS = re.compile(r"<<<(\d+)\s*\|\s*([A-Za-z0-9_]+)>>>")


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
        genres: list[dict]) -> list[tuple[str | None, str | None]]:
    """Translate AND classify several abstracts in a single Gemini request.

    Returns a list of (japanese_text, genre_id) tuples, each element None
    when unavailable (e.g. the model omitted that entry).
    """
    numbered = "\n\n".join(
        f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    valid_ids = {g["id"] for g in genres} | {"general"}
    prompt = (
        f"以下に{len(texts)}件の量子情報科学分野のarXiv論文abstract(英語)を示す。"
        "各abstractについて次の2つを行え。\n"
        "(1) 内容を最もよく表すジャンルIDを下記の一覧から厳密に1つ選ぶ。\n"
        "(2) abstractを、専門用語は標準的な訳語(必要なら英語併記)を用いて"
        "学術的な日本語に翻訳する。\n\n"
        "[ジャンル一覧]\n"
        + _genre_menu(genres)
        + "\n\n[出力形式]\n"
        "各エントリの訳文の直前に、対応する入力番号kと選んだジャンルIDを"
        "<<<k|genre_id>>> の形式で付すこと(例: <<<1|qec>>>)。"
        "genre_idは一覧のIDをそのまま用い、タグと訳文以外の文字列"
        "(前置き・後書き・見出し)を一切含めないこと。\n\n"
        + numbered
    )
    out = _gemini_request(prompt, cfg)
    results: list[tuple[str | None, str | None]] = [(None, None)] * len(texts)
    if not out:
        return results
    parts = BATCH_TAG_CLS.split(out)
    # parts = [preamble, '1', 'qec', text1, '2', 'algo', text2, ...]
    for k_str, gid, body in zip(parts[1::3], parts[2::3], parts[3::3]):
        try:
            k = int(k_str) - 1
        except ValueError:
            continue
        if 0 <= k < len(texts):
            t = body.strip()
            genre_id = gid if gid in valid_ids else None
            results[k] = (t or None, genre_id)
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
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"seen": []})
    seen = set(state["seen"])
    log: list[dict] = load_json(LOG_PATH, [])
    genres = cfg.get("genres", [])

    papers: dict[str, dict] = {}
    for cat in cfg.get("feeds", ["quant-ph"]):
        try:
            for p in fetch_feed(cat):
                papers.setdefault(p["id"], p)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] feed {cat} failed: {e}", file=sys.stderr)
        time.sleep(3)  # be polite to arXiv

    # ---- determine which papers to post (filtering only) ------------------
    pending = []  # papers passing should_post, not yet seen
    for pid, paper in papers.items():
        if pid in seen or not should_post(paper, cfg):
            continue
        pending.append(paper)

    # Each entry carries the paper plus its resolved genre + translation.
    entries = [{"paper": p, "genre": None, "jp": None, "need_tr":
                bool(p["abstract"])} for p in pending]

    batch_size = max(1, cfg.get("translate_batch_size", 5))
    use_llm_cls = cfg.get("classify_with_llm", True)
    chain = cfg.get("translators") or [cfg.get("translator", "gemini")]
    llm_first = use_llm_cls and chain and chain[0] == "gemini"

    # ---- primary path: Gemini translate + classify in one request ---------
    # Only attempted when Gemini heads the translator chain; otherwise we go
    # straight to keyword classification + the translator chain.
    if llm_first:
        for i in range(0, len(entries), batch_size):
            chunk = entries[i: i + batch_size]
            limit = cfg.get("max_translate_chars", 2000)
            abstracts = [
                f"Title: {e['paper']['title']}\n\nAbstract: {e['paper']['abstract'][:limit]}"
                for e in chunk
            ]
            pairs = translate_classify_gemini_batch(abstracts, cfg, genres)
            for e, (jp, gid) in zip(chunk, pairs):
                if jp:
                    e["jp"] = jp
                    e["genre"] = genre_by_id(gid, genres)
                    e["llm_done"] = True

    # ---- fallback path: keyword classify + translator chain ---------------
    # Applies to papers the LLM step did not fully handle (Gemini skipped,
    # quota-exhausted, or an entry the model omitted/failed to translate).
    leftover = [e for e in entries if not e.get("llm_done")]
    for e in leftover:
        # Keyword-based genre as a stand-in for LLM classification.
        e["genre"] = classify(e["paper"], genres)
    to_tr = [e for e in leftover if e["need_tr"] and e["jp"] is None and (
        e["genre"] is not None or not cfg.get("translate_only_matched", False))]
    for i in range(0, len(to_tr), batch_size):
        chunk = to_tr[i: i + batch_size]
        abstracts = [e["paper"]["abstract"] for e in chunk]
        for e, jp in zip(chunk, translate_batch(abstracts, cfg)):
            e["jp"] = jp

    # ---- post ---------------------------------------------------------------
    require_tr = cfg.get("require_translation", True)
    posted = deferred = 0
    for e in entries:
        webhook, genre_name = resolve_webhook(e["genre"])
        if not webhook:
            print("[error] no webhook configured", file=sys.stderr)
            sys.exit(1)
        if e["need_tr"] and e["jp"] is None and require_tr:
            deferred += 1   # not marked seen -> retried on the next run
            continue
        if post_to_discord(webhook, e["paper"], genre_name, e["jp"], cfg):
            seen.add(e["paper"]["id"])
            posted += 1
            log.append({
                "id": e["paper"]["id"],
                "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "title": e["paper"]["title"],
                "authors": e["paper"]["authors"],
                "link": e["paper"]["link"],
                "primary": e["paper"]["primary"],
                "announce_type": e["paper"]["announce_type"],
                "genre_id": e["genre"]["id"] if e["genre"] else "general",
                "genre_name": genre_name,
                "abstract_en": e["paper"]["abstract"],
                "abstract_ja": e["jp"],
            })
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
