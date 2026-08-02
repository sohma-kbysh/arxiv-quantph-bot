# New arXiv quant-ph paper -> Discord notification bot (with configurable translation)

This bot fetches the official arXiv RSS feed (`rss.arxiv.org/rss/quant-ph`) three times a day (Monday-Saturday), classifies each paper into one or more of 15 genres, and posts it to every corresponding Discord channel through webhooks with a translated title and abstract. The embed footer of each post lists **all** genres assigned to the paper, so a multi-genre paper shows its full classification in every channel it appears in.

In the current standard setup, Gemini is used for **classification only**, while translation is attempted through DeepL -> Azure Translator -> Google Cloud Translation. Because Gemini only returns genre IDs, this setup uses less API quota than asking Gemini to translate as well. Classification itself is routed across two Gemini models by a TF-IDF pre-screen: most papers go to the primary model `gemini-2.5-flash`, with the rest falling to the secondary model `gemini-2.5-flash-lite` under budget or rate-limit pressure. If both Gemini models fail or hit quota, the classifier falls through to an OpenAI-compatible fallback, currently configured as Cerebras `gpt-oss-120b`. The bot uses **only the Python standard library**; `pip install` is not required.

Completeness is the primary design goal. Every run is split into three durable phases (discover -> translate -> deliver), and the delivery state is tracked **per paper and per channel**, so a partial failure retries only the channels that have not been confirmed yet. See "Execution phases and durable state" below.

After every run, the bot posts a **run report in Japanese to the bot-emergency channel** (`DISCORD_WEBHOOK_BOT_EMERGENCY`), including successful runs: which papers were posted to which channels, what was deferred for translation, and what failed. See "Monitoring: bot-emergency channel" below.

The default translation target is Japanese (`target_language: "ja"`), but it can be changed by editing `target_language` in `config.json`. If you choose a language that DeepL does not support, Azure and Google can still handle many target languages; set `translators` to `["azure", "google"]` or use `deepl_target_language` / `azure_target_language` / `google_target_language` for backend-specific language codes.

The checked-in `config.json` remains configured for the original Japanese Discord workflow: `target_language: "ja"`, `target_language_name: "Japanese"`, `translated_title_label: "邦題"`, `translators: ["deepl", "azure", "google"]`, and `require_translation: true`.

---

## File layout

| File | Role |
| --- | --- |
| `arxiv_bot.py` | Main bot. Uses only the Python standard library |
| `config.json` | All configuration: feeds, genre definitions, API behavior, classification parameters |
| `seen_ids.json` | Durable state: completed IDs, per-channel delivery queue, external-review cache, unreviewed external candidates, and per-source fetch cursors. Automatically committed by Actions |
| `posted_log.json` | Metadata log for posted papers. JSON array, capped at 5000 entries |
| `scirate_weekly.py` | Weekend bot that reposts popular weekly quant-ph papers from SciRate into the normal genre channels |
| `scirate_weekly_state.json` | Durable state for daily/weekly SciRate discovery, translation, delivery, and deduplication |
| `test_feed.xml` | Sample RSS feed for local testing |
| `tests/test_external_arxiv.py` | Tests for adjacent-category API queries, external review, and the QEC policy |
| `tests/test_delivery_reliability.py` | Tests for per-channel delivery receipts, resumable phases, and webhook error handling |
| `tests/test_repost_reliability.py` | Tests for repost idempotency and webhook validation |
| `tests/test_scirate_reliability.py` | Tests for the SciRate three-phase pipeline |
| `scripts/clean_discord_urls.py` | Helper script to find or delete arXiv URL posts in Discord channels |
| `scripts/lock_discord_channels.py` | Helper script to make selected Discord channels read/reaction-only for non-admin users |
| `scripts/rollback_posted_day.py` | Helper script to remove one local day from `posted_log.json` and `seen_ids.json` before reposting |
| `scripts/audit_classification.py` | Helper script that re-runs Gemini classification for already-posted papers and prints only the differences |
| `scripts/repost_missing_channels.py` | Posts already-published papers to genre channels missed by the original classification, reusing stored translations |
| `repost_plan.json` | Repost plan (paper id -> channels to add), generated from a classification audit |
| `DESIGN_BACKLOG.md` | Open design decisions and their proposed order of work |
| `.github/workflows/notify.yml` | GitHub Actions schedule and secret references for the main notifier |
| `.github/workflows/scirate_weekly.yml` | GitHub Actions schedule for the SciRate daily Top 3 and Sunday weekly 30+ digest |
| `.github/workflows/classification_audit.yml` | Manually triggered workflow that audits one day of past classifications |
| `.github/workflows/repost.yml` | Manually triggered workflow that posts papers to missing channels per a repost plan |

---

## Run schedule

GitHub Actions runs the main notifier **three times a day, Monday through Saturday** (cron day-of-week `1-6`; the JST time is the same calendar day).

| UTC | JST | Purpose |
| --- | --- | --- |
| 01:05 | 10:05 | Catch new papers soon after the arXiv announcement at around 00:00 UTC |
| 04:00 | 13:00 | Cover missed or delayed items |
| 07:00 | 16:00 | Same as above |

The workflow can also be run manually (`workflow_dispatch`) with three inputs.

| Input | Default | Description |
| --- | --- | --- |
| `use_test_feed` | `false` | Read `test_feed.xml` instead of the live RSS feed |
| `test_emergency_alert` | `false` | Send only a test message to the bot-emergency channel without running the notifier |
| `external_backfill_days` | `"0"` | One-time lookback in days for the adjacent-category API sources only. `0` keeps the configured window |

A separate workflow posts SciRate's weekday Top 3 and Sunday weekly 30+ digest to one dedicated channel.

| UTC | JST | Purpose |
| --- | --- | --- |
| Sunday 00:30 | Sunday 09:30 | Post popular quant-ph papers from the last 7 days on SciRate |

---

## Execution phases and durable state

Each run is split into three phases, and the state file is committed to the repository between phases. Every phase can be resumed independently, so an interrupted run continues from where it stopped instead of restarting.

```text
discover + classify -> commit -> translate -> commit -> deliver -> commit per-channel receipts
```

| Phase | Command | What it does |
| --- | --- | --- |
| Discover | `python3 arxiv_bot.py --discover-only` | Fetch RSS and adjacent-category API sources, classify, and write the delivery queue |
| Translate | `python3 arxiv_bot.py --translate-only` | Translate queued papers and store the translations in the queue |
| Deliver | `python3 arxiv_bot.py --deliver-only` | Post to Discord and record a receipt for every channel |

`--prepare-only` runs discovery and translation together. The phase flags are mutually exclusive, and none of them can be combined with `--dry-run`. Running `arxiv_bot.py` with no flag performs all three phases in one process, which is the usual way to run it locally.

What this buys:

- **Per-channel retries.** If a paper is delivered to `qec` but the `crypto` post fails, only `crypto` is retried on the next run. The paper is not marked complete until every assigned channel has a receipt.
- **Translation failures survive the feed.** Papers whose translation failed stay in the durable queue, so they are still retried after they drop off the arXiv RSS feed.
- **Unreviewed external candidates survive the lookback window.** Candidates without a completed review are stored with their metadata in `external_pending`.
- **Fetch cursors close outage gaps.** Each adjacent-category source records the timestamp of its last successful fetch in `external_cursors`. The next run expands its lookback to cover the elapsed time plus `cursor_overlap_days` (default 2), so an Actions or arXiv API outage longer than the normal window cannot silently skip papers.

Delivery is intentionally **at-least-once**: an unusual runner failure after Discord accepts a webhook but before its receipt is committed can produce a duplicate, because Discord webhooks do not provide an idempotency key. The design prefers a rare duplicate over a silent loss.

---

## Processing flow

### 1. Fetch sources

The bot fetches RSS feeds for the categories listed in `config.json` under `feeds` (currently `"quant-ph"`) and deduplicates papers by ID. If the same paper appears in multiple feeds, the first configured feed wins.

In addition, `external_arxiv_queries` retrieves mechanically narrowed candidates whose primary category is adjacent to quant-ph and which are not already cross-listed to quant-ph, through custom Atom API queries:

| Source | Usual destinations | Search terms |
| --- | --- | --- |
| `cs.CR` | `pqc` / `crypto` | 50 |
| `cs.CC` | `complexity` / `algo` | 9 |
| `cs.IT` | `qit` / `qec` / `network` | 6 |

The configured search terms are deliberately recall-oriented. Matching an API query never causes a post by itself. Long term lists are split across short API queries (`terms_per_query`, default 8), each query is paged until it crosses the configured lookback boundary, and the combined results are deduplicated. This avoids both unreliable oversized requests and silent truncation at one API result page.

Candidates go through a separate strict review using the same configured classifier chain as normal classification (primary Gemini, secondary Gemini, then configured OpenAI-compatible fallbacks). The source mappings above are soft hints, not output restrictions: `allow_all_genres` is enabled for every source, so external review can select any configured genre except those listed in `excluded_genres` (currently `other`), up to `external_classify_max_genres`.

The review prompt is completeness-oriented: a substantive secondary contribution or nontrivial application is enough, and `skip` is appropriate only when the connection appears solely in background, motivation, future work, citations, or comparison. A rejection is cached only after `external_skip_consensus` models independently return `skip`; if any reviewer selects a genre, the paper is accepted. A lone skip with no second working reviewer remains unreviewed for retry. Unlike normal quant-ph classification, an external candidate is never posted through TF-IDF or routed to `other` when LLM review cannot reach a decision.

The `qec` channel intentionally includes coding theory broadly whenever a paper has a non-incidental quantum, quantum-communication, or PQC connection. The code itself may be classical. `qec_adjacent_coding_terms` (currently 53 phrases such as `linear code`, `rank-metric code`, `syndrome decoding`, `self-orthogonal`) adds `qec` deterministically after classification, covering examples such as rank-metric and Gabidulin codes used in code-based cryptography.

Normal quant-ph papers always consume classifier and translation capacity first. External strict review starts only after quant-ph classification is finished, and every quant-ph translation is sorted ahead of every external translation regardless of genre priority.

External decisions are cached in `seen_ids.json` under `external_reviews`. Rejected IDs are not put in the global completed-ID set, so the paper can still be processed normally if it is later cross-listed to quant-ph. `lookback_days` prevents a large historical backfill when an external query is enabled for the first time. For a one-time manual backfill, set `EXTERNAL_ARXIV_LOOKBACK_DAYS` (or the matching `external_backfill_days` Actions input) to the desired number of days; the configured normal window remains unchanged.

### 2. Filtering

`should_post()` evaluates each paper using the following rules.

| announce_type | Behavior |
| --- | --- |
| `new` | Always passes as a new quant-ph paper |
| `cross` | Evaluated by the cross-list policy below |
| `replace` | Passes only when `include_replacements: true` |

**Cross-list posting policy (default: pass all)**

A cross-listed paper is excluded only when its primary category matches `cross_deny_primary`. This list is empty by default (`[]`), so **all cross-listed papers pass**, including `hep-*`, `gr-qc`, and `cond-mat.*`. Add categories to `cross_deny_primary` if you want to exclude them.

`cross_allow_primary` is a whitelist and takes priority over the denylist, for cases where you want exceptions after adding categories to the denylist.

**Cross-list classification policy**

Every paper obtained from the quant-ph RSS feed uses the normal AI classification and deterministic QEC/keyword policies, regardless of its primary category. Thus a `cs.IT`-primary paper cross-listed to quant-ph can reach QEC, QIT, or network instead of being overwritten to `other`.

- Papers from the quant-ph RSS feed: classified normally regardless of primary category
- Explicit external sources: high-recall search plus a strict LLM review of their own

`cross_classify_primary_as_quantph` and `category_other_overrides` remain only as a compatibility safeguard for callers that explicitly classify a non-quant-ph source through the normal post-processing function; the adjacent-category API path uses its own strict review.

### 3. Genre classification + translation (two paths)

**Primary path: Gemini classify-only, routed across two models by a TF-IDF pre-screen**

When `classify_with_llm: true` (default) and `GEMINI_API_KEY` is available, the bot sends titles and abstracts to Gemini in batches of `translate_batch_size` entries (default: 5) and asks Gemini to return only genre IDs.

Before any Gemini call, every pending paper is first run through the same TF-IDF classifier described below, but purely to decide **routing** -- this pre-screen result is never posted, except as the emergency fallback described below:

- Papers whose pre-screen genres touch none of `prescreen_defer_genres` (default: `nisq`, `hardware`, `sensing`, `foundations`, `other`) form the **priority group** and are always classified with the primary model `gemini_model_primary` (`gemini-2.5-flash`)
- The rest form the **deferred group**. They are also classified with the primary model as long as the estimated number of Gemini requests for the run (priority batches + deferred batches) fits `gemini_primary_run_budget` (default: 20); otherwise the deferred group uses the secondary model `gemini_model_secondary` (`gemini-2.5-flash-lite`) to stay inside the free-tier daily quota
- Each model has its own circuit breaker: persistent 429s or repeated 500/503s mark only that model dead for the rest of the run, and any papers still pending automatically fall through to the next classifier (`gemini-2.5-flash` -> `gemini-2.5-flash-lite` -> configured OpenAI-compatible fallbacks such as `gpt-oss-120b`). Only when every LLM classifier is unavailable does the bot post the TF-IDF pre-screen result directly (emergency fallback)
- `gemini_min_intervals` paces `gemini-2.5-flash` at 7s and `gemini-2.5-flash-lite` at 5s between requests; see "Notes" below for the underlying free-tier RPD/RPM limits
- The prompt includes the full natural-language `description` for every genre, so papers can be classified by meaning even when they do not contain fixed keywords
- Output format: `<<<k|genre_id>>>` or `<<<k|id1,id2>>>` for multi-label classification
- One paper can be assigned to multiple genres, including genres beyond its primary contribution whenever the paper has genuine value for that genre's readers too; see "Multi-label classification" below
- Quant-ph RSS cross-lists retain the LLM result and then receive the same deterministic QEC/keyword post-processing as primary quant-ph papers

**Fallback path: TF-IDF cosine similarity + keyword evidence scores**

Used when every LLM classifier is unavailable due to quota exhaustion or similar failures, or when the models do not return a result for an individual entry.

- Vectorizes each genre's `description` plus its single-word `keywords` with TF-IDF (multi-word keyword phrases are excluded from the vector and scored separately, see below)
- Computes cosine similarity against the paper's `title + abstract`
- Applies arXiv category hints from `category_genre_hints` (+0.15 to the target genre) and forced `other` handling from `category_other_overrides` (+1.0 to `other`)
- Words that appear in every genre get IDF=0 and do not affect the score; generic terms such as "quantum", "qubit", "state", and "system" are also stopworded out
- The tokenizer keeps ASCII words only, so the fallback effectively scores English text; Japanese genre descriptions contribute almost nothing to it

On top of the cosine similarity, **keyword evidence scores** are added for direct keyword hits in the paper text:

| Evidence | Config key | Default bonus |
| --- | --- | --- |
| Keyword phrase found in the title | `fallback_title_phrase_bonus` | +0.35 |
| Keyword phrase found in the abstract | `fallback_abstract_phrase_bonus` | +0.18 |
| Single-word keyword found in the title | `fallback_title_token_bonus` | +0.10 |
| Single-word keyword found in the abstract | `fallback_abstract_token_bonus` | +0.03 |

`fallback_keyword_boosts` in `config.json` defines additional per-genre phrase lists (for example "cat qubit" -> `qec`, "barren plateau" -> `nisq`) that receive the phrase bonuses above. This lets the fallback catch papers whose wording does not overlap with the genre descriptions.

### 4. Multi-label classification

One paper can be classified into multiple genres and posted to each corresponding channel.

- `classify_max_genres` (default: 2): the genre count requested in the LLM prompt, and a hard cut on the TF-IDF path
- `classify_secondary_ratio` (default: 0.7; current config: 0.82, TF-IDF fallback only): secondary genres are accepted only when their score is at least this fraction of the top genre score, preventing weak accidental matches from causing multi-channel posts
- On the Gemini path, the prompt instructs the LLM to first choose the genre of the paper's primary contribution, then add further genres whenever the paper also has genuine value for researchers following that genre's channel -- for example, new error-correcting codes designed for transversal/fault-tolerant logic belong in both `qec` and `ft` -- but never to add a genre that is merely used as a tool or demonstration platform (e.g. a well-known algorithm simply run on quantum hardware is `hardware`, not `algo`). Duplicate genre IDs returned by the model are deduplicated while preserving order
- Deterministic post-processing can add genres after classification: `force_genre_keywords` and the broad `qec` coding-theory rule. Because of this, a posted paper can carry more genres than `classify_max_genres`
- **The embed footer of every post lists all assigned genres** (for example `quant-ph | 量子複雑性理論, 量子アルゴリズム | new`), so readers in one channel can see the paper's other classifications too

### 5. Translation fallback chain

Current standard setting:

```text
DeepL -> Azure Translator -> Google Cloud Translation
```

- Backends are tried in order; once one succeeds, the bot moves to the next paper
- For papers whose abstract translation succeeds, the same translation chain also creates a translated title separately from the English title
- Backends where quota exhaustion is detected (DeepL: 456, Google: 403/429, Gemini as translator: persistent 429) are skipped for the rest of that run (**circuit breaker**)
- If DeepL and Azure fail, Google is used only for papers outside `google_skip_translation_genres`. Papers that belong only to those skipped genres are posted in English instead of being deferred
- If every allowed backend fails and `require_translation: true` (default), the paper is not posted. It stays in the durable queue and is retried on the next run, even after it disappears from the RSS feed

### 6. Discord delivery

For each paper, the bot posts once for each assigned genre. It waits 1.2 seconds between posts to leave headroom for Discord webhook rate limits. The embed footer shows `primary category | all assigned genre names | announce_type`, so a paper classified into two genres shows both names in both channels.

Delivery is verified per channel:

- A receipt (`status: delivered` plus a timestamp) is written for each channel immediately after Discord accepts the post, and the state file is flushed at that point
- **A genre whose webhook secret is missing is treated as a delivery failure, not a silent success.** The bot does not fall back to another channel; the paper stays pending and the run report lists the channel as `(webhook未設定)`
- **Two genres resolving to the same webhook URL is also treated as a failure** (`(webhook重複)`), because a single post would otherwise be mistaken for two successful deliveries
- `DISCORD_WEBHOOK_GENERAL` is used only as a last-resort destination for a paper that ended up with no genre at all

`posted_log.json` records metadata for posted papers. Example entry:

```json
{
  "id": "2506.12345",
  "posted_at": "2025-06-24T01:10:00Z",
  "title": "...",
  "title_ja": "...",
  "title_translated": "...",
  "translation_language": "ja",
  "authors": "...",
  "link": "https://arxiv.org/abs/2506.12345",
  "primary": "quant-ph",
  "announce_type": "new",
  "genre_ids": ["qec", "ft"],
  "genre_names": ["誤り訂正・符号理論", "フォールトトレラント計算"],
  "classifier": "gemini-2.5-flash",
  "abstract_en": "...",
  "abstract_ja": "...",
  "abstract_translated": "..."
}
```

`classifier` records which model produced the classification: `"gemini-2.5-flash"`, `"gemini-2.5-flash-lite"`, `"gpt-oss-120b"`, or `"tfidf"` (emergency fallback). It records the model only; genres added afterwards by deterministic post-processing are not distinguished in the log today.

`title_translated`, `abstract_translated`, and `translation_language` are the language-neutral fields. `title_ja` and `abstract_ja` are still written for backward compatibility with older logs and the existing Japanese workflow.

### 7. Run report to the bot-emergency channel

After posting, the bot sends one summary embed (in Japanese) to `DISCORD_WEBHOOK_BOT_EMERGENCY` **on every run, including fully successful ones**, so the channel doubles as an execution log. The report contains:

- Number of papers fetched from the feed and number of new post candidates
- Number of papers posted successfully (and number of Discord messages), deferred for translation, and failed
- Classification stats: per-model paper counts, e.g. `🏷 分類: gemini-2.5-flash: 13件 / gemini-2.5-flash-lite: 21件 / TF-IDF: 2件`
- Translation stats: successful translations per backend (DeepL / Azure / Google), and any backend disabled by the circuit breaker during the run
- **The list of posted papers with the channels each one was sent to**, plus separate lists of deferred and failed papers

The embed is green when everything succeeded, orange when papers were deferred, and red when a Discord post failed. Long paper lists are truncated to fit Discord's embed limit. If `DISCORD_WEBHOOK_BOT_EMERGENCY` is not configured, the report is skipped with a log message.

---

## Failure handling

A source that cannot be fetched is never reported as "no new papers".

- Each phase runs with `continue-on-error`, so a failure in discovery still lets the workflow record what it has and surface the problem
- If discovery, translation, or delivery failed, a final workflow step **fails the run explicitly** (`exit 1`), so the Actions run shows red rather than a misleading green
- RSS and arXiv API failures are collected per source and reported to the bot-emergency channel
- Secrets are stripped from logs: URL query strings and Discord webhook path segments are redacted, and any environment variable whose name contains `SECRET`, `PASSWORD`, or `WEBHOOK`, or ends with `_KEY` or `_TOKEN`, is replaced with `<redacted>` inside error text before it is printed

---

## Monitoring: bot-emergency channel

The `DISCORD_WEBHOOK_BOT_EMERGENCY` webhook receives operational messages, all in Japanese:

| Message | When |
| --- | --- |
| ✅ / 🟡 / 🚨 Run report | Every run of the main notifier and the SciRate daily/weekly digest |
| ⚠️ Translation outage alert | When every translator backend in the chain has given up for the run and papers are being silently deferred |
| 🚨 Source or delivery failure | When an RSS feed, an arXiv API query, or a Discord delivery failed during the run |

To send a test message without running the notifier, run the `notify.yml` workflow manually with the `test_emergency_alert` input checked.

---

## Classification audit workflow

`.github/workflows/classification_audit.yml` (manual trigger only) re-runs Gemini classification for papers already recorded in `posted_log.json` and prints only the entries whose new classification differs from what was posted. Use it to spot-check a day's classifications after changing genre descriptions or prompts.

Per batch, the script tries `gemini_model_primary` and falls through to `gemini_model_secondary` on failure, same as the main bot's classification chain. Pass `--model <id>` (repeatable) to override this chain with a custom list of models, tried in order.

Inputs:

| Input | Default | Description |
| --- | --- | --- |
| `date` | (required) | Local date to audit, e.g. `2026-07-03` |
| `timezone` | `Asia/Tokyo` | Timezone used to group `posted_at` timestamps into local dates |

Local equivalent:

```bash
export GEMINI_API_KEY="..."
python3 scripts/audit_classification.py --date 2026-07-03 --timezone Asia/Tokyo
```

Note that the audit script applies the normal quant-ph post-processing to every paper, including papers that were originally accepted through the adjacent-category external path. Differences reported for those papers can therefore be artifacts of the audit rather than real classification changes.

After an audit, papers that gained genres can be posted to just those missing channels with the repost workflow (`repost.yml`, manual `workflow_dispatch`, inputs: `plan` path defaulting to `repost_plan.json` and `dry_run` defaulting to `true`). It reuses `title_translated` / `abstract_translated` from `posted_log.json` for each paper (no translation API calls), posts one embed per missing channel with the corrected full genre list in the footer, and updates the log entry's `genre_ids` / `genre_names` to the corrected classification (recording `repost_channels`, `repost_genre_ids`, and `reposted_at`).

The repost script is idempotent: channels already recorded as reposted are skipped, so re-running the same plan does not duplicate posts. It never touches `seen_ids.json`, deliberately skips channels whose webhook secret is missing instead of falling back to the general channel, and refuses channels that resolve to the same webhook URL. A run report in Japanese is sent to the bot-emergency channel.

Local equivalent:

```bash
python3 scripts/repost_missing_channels.py --plan repost_plan.json --dry-run
```

---

## SciRate daily and weekly channel

`.github/workflows/scirate_weekly.yml` sends all SciRate editorial content to
one dedicated webhook, `DISCORD_WEBHOOK_SCIRATE`. It never reposts SciRate
content into QEC, QIT, or any other normal genre channel.

| JST schedule | Selection | Discord format |
| --- | --- | --- |
| Monday-Friday 23:30 | Exact `pubdate` batch for that day; first three papers with at least `scirate_daily_min_scites` (default 1) | One Top 3 ranking embed |
| Sunday 23:30 | The seven-day window ending Sunday; every paper with at least `scirate_min_scites` (default 30) | One normal paper embed per qualifying paper |
| Saturday | No run | None |

Daily ties preserve SciRate's own deterministic order. If fewer than three
papers have a positive Scite count, the digest posts only those papers. An
empty daily API result means there was no eligible announcement batch and
produces no channel post. Weekly cards include their current Scite count and
mark papers that already appeared in a daily Top 3.

Production does **not** scrape SciRate HTML. It probes the JSON endpoint
proposed by upstream PR `scirate/scirate#535`, currently
`https://scirate.com/arxiv/quant-ph.json?date=YYYY-MM-DD&range=N&page=1`.
The official endpoint is always attempted first. If it is unavailable, an
optional trusted JSON relay can be configured with
`SCIRATE_RELAY_URL_TEMPLATE`; it must expose the same `papers` schema and
publish a complete single-page snapshot with mandatory `"complete": true`
and exact period metadata. If the relay requires authentication, store its
token as the Actions secret
`SCIRATE_RELAY_BEARER_TOKEN`; the bearer header is sent only to the relay.
This is intended for a low-rate snapshot produced by an already-authorized
lab or institutional environment, not for a Cloudflare-bypass proxy.

Until either source returns a valid `papers` array, the source is
`waiting_for_api` (official only) or `waiting_for_source` (relay configured);
only durable queued deliveries are processed, and the workflow exits
successfully with a bot-emergency notice. Every due period is
stored in `pending_discovery` before acquisition, including the very first
outage, and is caught up by the next run of the same mode. The official API is
probed again on every run, so it automatically takes precedence as soon as it
works. Already discovered posts and translations are also durable across
runs.

The client verifies the response date, requires `pubdate` within the exact
requested daily or weekly period, checks global descending score order, and
rejects a weekly result if it
cannot reach the 30-Scite boundary within `scirate_api_max_pages`. Set the
repository variable `SCIRATE_API_URL_TEMPLATE` if SciRate publishes a
different path; `{date}`, `{days}`, and `{page}` are supported. Pagination can
be adjusted with `SCIRATE_API_PAGE_SIZE` and `SCIRATE_API_MAX_PAGES`.
`SCIRATE_RELAY_URL_TEMPLATE` supports the same placeholders. Do not put a
credential in either URL; use the relay bearer-token secret instead.
The due period is tried first, followed by up to
`scirate_backlog_max_periods` (default 8) older pending periods. A failure in
either direction remains visible and cannot permanently starve the other.
For a complete one-file snapshot, the relay response can be as small as:

```json
{
  "date": "2026-08-03",
  "complete": true,
  "range_days": 1,
  "period_start": "2026-08-03",
  "period_end": "2026-08-03",
  "papers": [
    {
      "uid": "2608.01234",
      "scites_count": 7,
      "pubdate": "2026-08-03",
      "submit_date": "2026-08-02"
    }
  ]
}
```

`range_days`, `period_start`, and `period_end` must exactly match the requested
period; a weekly snapshot therefore declares 7 and the full Monday-Sunday
window. Rows must be in SciRate order (descending Scite count, including its
tie order). The bot rejects malformed rows, mismatched period metadata, a
wrong `pubdate`, duplicates, or a non-descending snapshot rather than silently
posting a partial ranking.

`scirate_weekly_state.json` owns daily/weekly deduplication and retry state.
The three durable phases remain `--discover-only`, `--translate-only`, and
`--deliver-only`. Existing translations and genre labels are reused from
`posted_log.json`; missing translations use the normal translation chain.
There is no new AI classification call because routing is always the dedicated
SciRate channel. Operational reports still go to bot-emergency.

Local check:

```bash
python3 scirate_weekly.py --mode daily --date 2026-08-03 --dry-run
python3 scirate_weekly.py --mode weekly --date 2026-08-09 --dry-run
```

`--html-file PATH` remains available only for local parser fixtures. There is
no production HTML fallback, CAPTCHA solver, proxy rotation, origin-IP access,
or User-Agent bypass for a 403. A relay must publish normalized JSON rather
than expose browser cookies or Cloudflare clearance tokens.

---

## Genre list (15 genres)

| ID | Name | Main topics |
| --- | --- | --- |
| `qec` | 誤り訂正・符号理論 | Stabilizer codes, surface codes, LDPC, decoder design, and quantum-adjacent classical coding theory |
| `ft` | フォールトトレラント計算 | Magic-state distillation, lattice surgery, resource estimates |
| `algo` | 量子アルゴリズム | Grover, Shor, quantum walks, phase estimation, HHL |
| `complexity` | 量子複雑性理論 | BQP, QMA, query complexity, local Hamiltonian |
| `nisq` | 変分・NISQアルゴリズム | VQE, QAOA, error mitigation, barren plateaus |
| `sim` | 量子シミュレーション | Hamiltonian simulation, Trotterization, quantum chemistry |
| `qml` | 量子機械学習 | QNN, quantum kernels, quantum reinforcement learning |
| `qit` | 量子情報理論 | Entanglement theory, resource theories, channel capacity |
| `network` | 量子ネットワーク・通信 | Quantum repeaters, entanglement distribution, quantum teleportation |
| `crypto` | 暗号・セキュリティ | QKD, DI-QKD, blind/verifiable/secure delegation, SMC/quantum auctions |
| `pqc` | 耐量子計算機暗号 | Lattice cryptography (LWE/Kyber), NIST PQC standardization |
| `hardware` | 量子ハードウェア・実装 | Superconducting systems, ion traps, Rydberg systems, spin qubits |
| `sensing` | 量子センシング・計測 | Heisenberg limit, quantum Fisher information, atomic clocks |
| `foundations` | 量子基礎・測定理論 | Bell inequalities, decoherence, quantum thermodynamics |
| `other` | その他・異分野 | Papers outside quantum information, such as hep-*, gr-qc, nucl-*, and general cond-mat |

A paper that matches none of the genres is routed to `other`. `DISCORD_WEBHOOK_GENERAL` is only the last-resort destination when a paper ends up with no genre object at all.

---

## Setup

### Quick start for your own Discord server

For the smallest working setup, you do not need to create all 15 genre channels.

1. Fork this repository.
2. Create one Discord webhook for a test or general channel.
3. Add that webhook URL as the `DISCORD_WEBHOOK_GENERAL` repository secret.
4. Add `GEMINI_API_KEY` if you want LLM-based classification. Without it, the bot falls back to TF-IDF classification.
5. Add at least one translation key, usually `AZURE_TRANSLATOR_KEY` for the largest free tier, `GOOGLE_TRANSLATE_API_KEY` for broad language coverage, or `DEEPL_API_KEY` for DeepL-supported languages.
6. Edit `config.json` if you want another language, for example `target_language: "fr"` and `translators: ["azure", "google"]`.
7. Run the workflow manually from the Actions tab once before relying on the schedule.

Useful official references:

- Discord webhook setup: [Intro to Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
- GitHub Actions secrets: [Using secrets in GitHub Actions](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions)
- Azure Translator language codes and API reference: [Translate method](https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/reference/v3/translate)
- Azure Translator pricing: [Pricing](https://azure.microsoft.com/en-us/pricing/details/translator/)
- Google Cloud Translation language codes: [Language support](https://cloud.google.com/translate/docs/languages)
- DeepL target language codes: [Languages supported](https://developers.deepl.com/docs/resources/supported-languages)
- Gemini API keys: [Google AI Studio](https://aistudio.google.com/)

### 1. Create Discord webhooks

For each destination channel, create a webhook from "Channel Settings -> Integrations -> Webhooks". Prepare a channel for each genre and register each URL as a GitHub Secret below.

If you do not want fine-grained genre channels, setting only `DISCORD_WEBHOOK_GENERAL` is enough; all papers will go there.

For genre-specific routing, create one webhook per channel and store each URL in the matching `DISCORD_WEBHOOK_*` secret. Give each genre its own webhook: two genres sharing one URL are rejected as a configuration error. Keep webhook URLs and API keys out of committed files.

### 2. Get API keys

Translation backends are tried in the order listed in `config.json` under `translators`. The current standard setting is `["deepl", "azure", "google"]`. **Unregistered backends are skipped automatically.**

| Backend | Purpose | Free tier | Secret name |
| --- | --- | --- | --- |
| Gemini | Classification only | Free tier available, no card required | `GEMINI_API_KEY` |
| Cerebras `gpt-oss-120b` | Classification only (fallback after Gemini) | Free tier available | `CEREBRAS_API_KEY` |
| DeepL | Translation | Free up to 500k characters/month | `DEEPL_API_KEY` |
| Azure Translator | Translation | Free up to 2M characters/month on F0 | `AZURE_TRANSLATOR_KEY` + `AZURE_TRANSLATOR_REGION` |
| Google Cloud Translation | Translation | Free up to 500k characters/month (**billing account required**) | `GOOGLE_TRANSLATE_API_KEY` |

You can create a Gemini API key in [Google AI Studio](https://aistudio.google.com/). Add a Cerebras key to let classification fall back to `gpt-oss-120b` when both Gemini models are unavailable. If no LLM classifier key is available, classification falls back to TF-IDF.

### 3. Register GitHub Secrets

Register the following under `Settings -> Secrets and variables -> Actions -> New repository secret`.

**Webhooks (all 15 genres + general + bot-emergency + SciRate)**

```text
DISCORD_WEBHOOK_GENERAL
DISCORD_WEBHOOK_BOT_EMERGENCY   # run reports and outage alerts (recommended)
DISCORD_WEBHOOK_SCIRATE         # daily Top 3 and weekly 30+ digests
DISCORD_WEBHOOK_QEC
DISCORD_WEBHOOK_FT
DISCORD_WEBHOOK_ALGO
DISCORD_WEBHOOK_COMPLEXITY
DISCORD_WEBHOOK_NISQ
DISCORD_WEBHOOK_SIM
DISCORD_WEBHOOK_QML
DISCORD_WEBHOOK_QIT
DISCORD_WEBHOOK_NETWORK
DISCORD_WEBHOOK_CRYPTO
DISCORD_WEBHOOK_PQC
DISCORD_WEBHOOK_HARDWARE
DISCORD_WEBHOOK_SENSING
DISCORD_WEBHOOK_FOUNDATIONS
DISCORD_WEBHOOK_OTHER
```

Optional SciRate relay authentication:

```text
SCIRATE_RELAY_BEARER_TOKEN      # sent only to the exact HTTPS relay URL
```

Set the relay endpoint separately as the repository variable
`SCIRATE_RELAY_URL_TEMPLATE`. Redirects are rejected, and the token is never
sent to the official SciRate endpoint.

**API keys (only the ones you use)**

```text
GEMINI_API_KEY
CEREBRAS_API_KEY         # optional gpt-oss-120b classifier fallback
DEEPL_API_KEY            # optional
AZURE_TRANSLATOR_KEY     # optional
AZURE_TRANSLATOR_REGION  # optional unless your Azure resource requires it
GOOGLE_TRANSLATE_API_KEY # optional
```

A genre whose webhook secret is missing is reported as a delivery failure and retried, so register a webhook for every genre you actually classify into, or remove that genre from `config.json`.

### 4. Test the workflow

Open the Actions tab -> `workflow_dispatch` -> "Run workflow" to run it manually.

---

## Local checks

### Unit tests

```bash
python3 -m pytest tests/ -q
```

The same suite runs as the first step of both scheduled workflows, so a broken change fails before it can post anything.

### dry-run mode (no API usage)

This mode does not call Discord, any LLM, or any translation API. It prints only TF-IDF classification results to stdout, so it checks plumbing rather than production classification.

```bash
python3 arxiv_bot.py --dry-run
```

Because this fetches the live arXiv feed before classifying, run it on a weekday after the arXiv announcement (around 10:00 JST or later). On weekends and holidays, the feed may be empty.

### Translation-only local test with test_feed.xml

This checks translation into another language without calling Discord and without updating `seen_ids.json` or `posted_log.json`.

```bash
export AZURE_TRANSLATOR_KEY="..."
export AZURE_TRANSLATOR_REGION="japaneast"  # use your Azure resource region if required
export ARXIV_TEST_FEED=test_feed.xml

python3 - <<'PY'
import arxiv_bot

cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
cfg.update({
    "translators": ["azure"],
    "target_language": "de",
    "target_language_name": "German",
    "translated_title_label": "Deutscher Titel",
    "show_translated_title": True,
})

paper = arxiv_bot.fetch_feed("quant-ph")[0]
title_de = arxiv_bot.translate_batch([paper["title"]], cfg)[0]
abstract_de = arxiv_bot.translate_batch([paper["abstract"]], cfg)[0]

print("ID:", paper["id"])
print("German title:", title_de)
print("German abstract:", abstract_de)
PY

unset ARXIV_TEST_FEED
```

### Full Discord test with test_feed.xml

This reads a local RSS file and exercises the full path, including translation and Discord posting. Note that `ARXIV_TEST_FEED` changes the input only: posts still go to the configured production webhooks and still update the production state files.

```bash
export GEMINI_API_KEY="..."
export DEEPL_API_KEY="..."
export DISCORD_WEBHOOK_GENERAL="..."   # URL for a test channel
export ARXIV_TEST_FEED=test_feed.xml
python3 arxiv_bot.py
```

If you do not want test posts recorded in `seen_ids.json`, reset `seen_ids.json` to `{"seen": []}` after the test. Before returning to live operation, run `unset ARXIV_TEST_FEED`.

---

## Customization

### Add or edit genres

Edit the `genres` array in `config.json`. Fields for each genre object:

| Key | Required | Description |
| --- | --- | --- |
| `id` | yes | Alphanumeric characters and underscores only. Must be unique. Also used as the LLM's output ID |
| `name` | yes | Genre name shown in the Discord embed. The default config uses Japanese names |
| `description` | yes | **Decision basis for LLM classification**. Detailed descriptions with clear boundaries against other genres improve classification accuracy |
| `webhook_env` | yes | Environment variable name registered as a Secret, for example `"DISCORD_WEBHOOK_QEC"` |
| `keywords` | yes | Word list used by the TF-IDF fallback |

When adding a genre, also register the corresponding Discord channel webhook as a Secret and add it to the `env:` section in `.github/workflows/notify.yml`, `scirate_weekly.yml`, and `repost.yml`.

### Classification parameters

Classification-related settings in `config.json`:

| Key | Default (current config) | Description |
| --- | --- | --- |
| `classify_with_llm` | `true` | Use TF-IDF fallback every time when set to `false` |
| `classify_min_score` | `0.05` (`0.08`) | Minimum TF-IDF score to accept |
| `classify_max_genres` | `2` | Genre count requested from the LLM, and a hard cut on the TF-IDF path |
| `classify_secondary_ratio` | `0.7` (`0.82`) | Minimum score ratio for secondary genres when using TF-IDF |
| `force_genre_keywords` | `{}` | Add the target genre when specified words appear in the title or abstract |
| `qec_adjacent_coding_terms` | 53 phrases | Coding-theory phrases that deterministically add `qec` after classification |
| `fallback_keyword_boosts` | `{}` | Per-genre phrase lists that add keyword evidence scores on the TF-IDF fallback path |
| `fallback_title_phrase_bonus` | `0.35` | Score bonus for a keyword phrase found in the title |
| `fallback_abstract_phrase_bonus` | `0.18` | Score bonus for a keyword phrase found in the abstract |
| `fallback_title_token_bonus` | `0.10` | Score bonus for a single-word keyword found in the title |
| `fallback_abstract_token_bonus` | `0.03` | Score bonus for a single-word keyword found in the abstract |
| `gemini_model_primary` | `"gemini-2.5-flash"` | Primary classification model |
| `gemini_model_secondary` | `"gemini-2.5-flash-lite"` | Used for the deferred group when the estimated request count exceeds `gemini_primary_run_budget`, and as a fallback when the primary model is rate-limited out |
| `llm_classification_fallbacks` | Cerebras `gpt-oss-120b` | Optional OpenAI-compatible classifier fallbacks tried after both Gemini models fail |
| `gemini_min_intervals` | flash: `7`, flash-lite: `5` | Per-model request spacing in seconds, overriding `gemini_min_interval_sec` |
| `gemini_primary_run_budget` | `20` | Estimated-request threshold above which the deferred group switches to the secondary model |
| `prescreen_defer_genres` | `["nisq","hardware","sensing","foundations","other"]` | TF-IDF pre-screen genres that route a paper into the deferred group |
| `external_classify_max_genres` | `3` | Maximum genre count for a completeness-oriented external review |
| `external_skip_consensus` | `2` | Independent LLM `skip` votes required before an external paper is rejected |
| `external_arxiv_queries` | three adjacent-category queries | Per-category custom arXiv terms, soft genre hints, recency window, API page size, query chunk size, and review criteria. Set `enabled: false` to disable one source |

Per-source keys inside `external_arxiv_queries`:

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Disable this source without deleting its configuration |
| `candidate_genres` | per source | Soft hints named in the review prompt, not an output restriction |
| `allow_all_genres` | `true` | Allow the review to select any genre outside `excluded_genres` |
| `excluded_genres` | `["other"]` | Genres the external review may never select |
| `lookback_days` | `4` | Normal recency window |
| `cursor_overlap_days` | `2` | Extra days added on top of the gap since the last successful fetch |
| `max_results` | `100` | arXiv API page size |
| `terms_per_query` | `8` | Number of search terms per API request |
| `terms` | per source | Recall-oriented search terms |
| `review_instructions` | per source | Source-specific guidance appended to the review prompt |

The run log prints LLM usage. Example:

```text
[info] Gemini usage: mode=classify-only, model=gemini-2.5-flash+gemini-2.5-flash-lite+gpt-oss-120b, requests=17, classified=82/82, tfidf_fallback=0, disabled_models=None
```

### arXiv category hints

Settings that help classify cross-listed papers from their primary category:

- `cross_classify_primary_as_quantph`: compatibility restriction for explicitly non-quant-ph callers of normal post-processing. Papers actually obtained from the quant-ph RSS are classified regardless of primary category
- `category_genre_hints`: category -> genre ID mapping. Matching papers receive +0.15 to the target genre score
- `category_other_overrides`: additional primary categories to explicitly treat as `other` on the compatibility path

### Forced crypto keywords

If a word listed in `force_genre_keywords.crypto` appears in the title or abstract, `crypto` is added to the LLM/TF-IDF result. The current list aims to avoid missing topics around verifiable, blind, secure, and delegated quantum computation:

```text
blind, blindness, verifiable, verifiability, secure, securely, security,
delegated quantum computation, secure delegation, blind delegation,
verifiable delegation, untrusted server, malicious server, client-server
```

Matching is a prefix-tolerant word match over the whole title and abstract, so single generic words in this list (`blind`, `secure`, `security`, `verifiable`) can also fire on unrelated papers. See `DESIGN_BACKLOG.md` for the open decision on narrowing this list.

### Cross-list filtering

| Key | Description |
| --- | --- |
| `cross_deny_primary` | Exclude cross-listed papers whose primary category matches this list. Default is empty, meaning all pass |
| `cross_allow_primary` | Whitelist that takes priority over the denylist |

### Translation and posting settings

| Key | Default | Description |
| --- | --- | --- |
| `translators` | `["deepl","azure","google"]` | Translation backend order |
| `target_language` | `"ja"` | Translation target language code. Passed to each translation backend unless overridden |
| `target_language_name` | `"Japanese"` | Human-readable target language name used in LLM translation prompts |
| `deepl_target_language` | unset | Optional DeepL-specific target language code, such as `JA`, `EN-US`, or `PT-BR` |
| `azure_target_language` | unset | Optional Azure-specific target language code. Defaults to `target_language` |
| `azure_translator_endpoint` | unset | Optional Azure endpoint. Defaults to `https://api.cognitive.microsofttranslator.com` |
| `google_target_language` | unset | Optional Google-specific target language code. Defaults to `target_language` |
| `translated_title_label` | `"邦題"` | Label shown before the translated title in Discord embeds |
| `translate_batch_size` | `5` | Number of papers grouped into one request |
| `max_translate_chars` | `2000` | Maximum abstract length passed to translation backends. Longer abstracts are truncated |
| `azure_min_interval_sec` | `1.2` | Minimum spacing between Azure Translator requests |
| `azure_max_retries` | `4` | Retries for Azure Translator 429 rate-limit responses |
| `google_min_interval_sec` | `1.2` | Minimum spacing between Google Translate requests |
| `google_max_retries` | `3` | Retries for Google Translate 429 / user-rate-limit responses |
| `translation_priority_genres` | 15 genre IDs | Genre priority used after classification when choosing translation/posting order |
| `translate_only_matched` | `false` | When `true`, papers with no classified genre are not translated, saving API usage |
| `google_skip_translation_genres` | `["other","foundations","sensing","nisq"]` | When only Google remains, papers whose genres are all in this list are posted in English to save Google quota |
| `require_translation` | `true` | `true`: papers whose translation failed are retried later / `false`: post in English |
| `show_translated_title` | `true` | Show the translated title at the beginning of the Discord embed body |
| `show_original_abstract` | `false` | Include the English abstract in addition to the translated abstract |
| `include_replacements` | `false` | Post replacement papers when set to `true` |
| `scirate_daily_top_n` | `3` | Number of papers in the weekday SciRate ranking |
| `scirate_daily_min_scites` | `1` | Daily minimum; zero-score papers do not fill empty ranks |
| `scirate_min_scites` | `30` | Minimum Scite count for the Sunday weekly digest |
| `scirate_api_url_template` | SciRate PR #535 path | JSON API template; supports `{date}`, `{days}`, and `{page}` |
| `scirate_api_max_pages` | `20` | Safety ceiling; exceeding it before the score boundary rejects a partial digest |
| `scirate_api_page_size` | `50` | Expected upstream page size used to detect the final page |
| `scirate_backlog_max_periods` | `8` | Older failed periods retried per run after the due period |
| `gemini_model` | `"gemini-2.5-flash"` | Legacy default model; now used only as the Gemini-as-translator model on the combined translate-and-classify path. Classification uses `gemini_model_primary` / `gemini_model_secondary` instead |
| `gemini_min_interval_sec` | `7` | Minimum interval between Gemini requests, in seconds (fallback when a model has no entry in `gemini_min_intervals`) |
| `gemini_max_retries` | `4` | Maximum retries for temporary errors |
| `gemini_overload_giveup` | `2` | Open the circuit breaker after this many consecutive overload errors |

---

## Security

- No credential is stored in this repository. Every webhook URL and API key is supplied at runtime through GitHub Actions Secrets or local environment variables
- `.gitignore` excludes `.env` and `error_diagnostics.jsonl`, which are the two files most likely to capture live values locally
- All Discord IDs shown in this README and in the helper scripts are placeholders, not real channels or servers
- Error logs are redacted before printing: URL query strings are removed, Discord webhook path segments become `<redacted>`, and any environment variable whose name contains `SECRET`, `PASSWORD`, or `WEBHOOK`, or ends with `_KEY` or `_TOKEN`, is stripped out of error text
- `posted_log.json` and `seen_ids.json` are committed to the repository. They contain arXiv metadata (IDs, titles, authors, abstracts, translations) only, and no operational credentials. If you fork this bot for a private Discord community, keep in mind that this log makes the bot's full posting history public
- If a webhook URL is ever committed by accident, rotate it in Discord immediately; rewriting the git history is not sufficient, because the old value may already be cached

---

## Notes

- The bot treats the first RSS `<category>` element as the primary category. This is a heuristic from observed RSS behavior, not an arXiv API guarantee.
- Genre classification is heuristic, using an LLM chain as the primary path and TF-IDF as fallback, so misclassification is unavoidable. The quality of `description` directly affects classification accuracy; for genres with fuzzy boundaries, write explicit boundary conditions.
- The final genre set is not decided by the LLM alone. Deterministic post-processing can add `crypto` and the broad `qec` coding-theory label after classification, and the log does not currently distinguish those additions from the model's own output.
- The default checked-in configuration is intentionally Japanese. Multilingual behavior is opt-in through `target_language` and related settings, so changing the code does not change the default Japanese Discord workflow.
- Azure Translator's F0 tier includes 2M free characters/month, which makes it a useful middle fallback before Google. For an Azure-only setup, use `translators: ["azure"]` and set `target_language` to an Azure-supported language code such as `fr`, `de`, `ko`, or `zh-Hans`.
- Google Cloud Translation supports many target languages and remains the final fallback in the default chain. To reduce Google usage, papers posted only to `other`, `foundations`, `sensing`, and `nisq` are posted in English when DeepL/Azure cannot translate them first.
- `gemini-2.5-pro` was removed from the Gemini API free tier (it now returns 429 with zero quota unless billing is enabled), which is why the default Gemini classification chain is `gemini-2.5-flash` -> `gemini-2.5-flash-lite`. Free-tier RPD (requests per day) quotas for the remaining models have also been repeatedly reduced -- some accounts report `gemini-2.5-flash` limited to as few as ~20 requests/day -- so check the current values for your account in [Google AI Studio](https://aistudio.google.com/) when setting it up. The per-model circuit breaker plus the `gemini-2.5-flash-lite` and `gpt-oss-120b` fallbacks keep classification working when the primary model's quota runs out mid-run.
- Completed IDs, unfinished per-channel deliveries, external reviews, pending candidates, and fetch cursors in `seen_ids.json` are not truncated, because truncating them can cause missed retries or duplicate reposts. `posted_log.json` remains a bounded 5000-entry presentation/audit log.
- Adjacent-category completeness currently covers papers whose **primary** category is `cs.CR`, `cs.CC`, or `cs.IT`. Papers primary to other categories, such as `math.IT` or `cs.DS`, are only picked up when they are cross-listed to quant-ph.

---

## Helper: cleaning Discord URL posts

`scripts/clean_discord_urls.py` is a helper script that finds bot/webhook posts containing arXiv URLs in a specified channel. It is dry-run by default, and deletes messages only when `--delete` is passed.

```bash
export DISCORD_BOT_TOKEN="actual Discord Bot Token"
export DISCORD_CHANNEL_ID="numeric channel ID"
python3 scripts/clean_discord_urls.py
python3 scripts/clean_discord_urls.py --delete
```

To delete only today's posts in Japan time:

```bash
python3 scripts/clean_discord_urls.py --today
python3 scripts/clean_discord_urls.py --today --delete
```

For multiple channels, use a comma-separated env var or repeat `--channel-id`:

```bash
export DISCORD_CHANNEL_IDS="111111111111111111,222222222222222222"
python3 scripts/clean_discord_urls.py --today
```

To target all bot channels, store every channel ID once in `DISCORD_ALL_CHANNEL_IDS` and use `--all-channels`:

```bash
export DISCORD_ALL_CHANNEL_IDS="111111111111111111,222222222222222222,333333333333333333"
python3 scripts/clean_discord_urls.py --all-channels --today
python3 scripts/clean_discord_urls.py --all-channels --today --delete
```

To target specific channels and a custom Japan-time range:

```bash
python3 scripts/clean_discord_urls.py \
  --channel-id 111111111111111111 \
  --channel-id 222222222222222222 \
  --since "2026-07-03T13:55" \
  --until "2026-07-03T16:30"

python3 scripts/clean_discord_urls.py \
  --channel-id 111111111111111111 \
  --channel-id 222222222222222222 \
  --since "2026-07-03T13:55" \
  --until "2026-07-03T16:30" \
  --delete
```

After deleting Discord messages, remove the same local day from the bot state so the notifier can repost those papers:

```bash
python3 scripts/rollback_posted_day.py --today
python3 scripts/rollback_posted_day.py --today --write
```

Note: a webhook URL is not a Bot Token and cannot be used with this script. Deleting old messages requires a Discord Bot with `View Channels`, `Read Message History`, and `Manage Messages` permissions.

---

## Helper: locking Discord channels

`scripts/lock_discord_channels.py` makes selected Discord channels read/reaction-only by updating the `@everyone` channel overwrite: message/thread sending is denied, while adding reactions is allowed. It is dry-run by default; pass `--apply` to update Discord.

The bot token needs `Manage Channels` permission.

```bash
export DISCORD_BOT_TOKEN="actual Discord Bot Token"
export DISCORD_GUILD_ID="numeric server ID"

python3 scripts/lock_discord_channels.py --names \
  fault-tolerant-computation \
  quantum-error-correction-code \
  quantum-machine-learning \
  cryptography-and-security \
  quantum-complexity-theory \
  post-quantum-cryptography \
  quantum-algorithm \
  quantum-simulation \
  nisq-algorithm \
  hardware-and-implementation \
  quantum-sensing \
  quantum-information-theory \
  quantum-foundations \
  others \
  bot-emergency

python3 scripts/lock_discord_channels.py --names \
  fault-tolerant-computation \
  quantum-error-correction-code \
  quantum-machine-learning \
  cryptography-and-security \
  quantum-complexity-theory \
  post-quantum-cryptography \
  quantum-algorithm \
  quantum-simulation \
  nisq-algorithm \
  hardware-and-implementation \
  quantum-sensing \
  quantum-information-theory \
  quantum-foundations \
  others \
  bot-emergency \
  --apply
```

For an all-channel lock with `random` kept open:

```bash
python3 scripts/lock_discord_channels.py --all-text-channels --exclude-names random
python3 scripts/lock_discord_channels.py --all-text-channels --exclude-names random --apply
```

Note: users with `Administrator` bypass channel permission overwrites. Webhooks can still post to their own channels.

---

# 日本語

# arXiv quant-ph → Discord 通知 bot(翻訳付き)

arXiv の公式 RSS フィード (`rss.arxiv.org/rss/quant-ph`) を月〜土の1日3回取得し、論文を15ジャンルのうち1つ以上に分類して、翻訳済みタイトル・abstract 訳とともに対応する Discord の各チャンネルへ Webhook で投稿する。各投稿の embed footer には、投稿先チャンネルのジャンルだけでなく**その論文に割り当てられた全ジャンル名**が表示されるため、複数ジャンルの論文はどのチャンネルで見ても分類の全体が分かる。

現在の標準運用では、Gemini は**分類のみ**に使い、翻訳は DeepL → Azure Translator → Google Cloud Translation の順に試行する。Gemini の出力はジャンル ID だけなので、翻訳まで Gemini に任せる構成より API 消費を抑えやすい。分類自体は TF-IDF による事前分類で2つの Gemini モデルへ振り分けられ、多くの論文はプライマリモデル `gemini-2.5-flash` で、予算やレート制限の圧迫時は残りがセカンダリモデル `gemini-2.5-flash-lite` で分類される。両方の Gemini モデルが失敗・quota 到達した場合は、OpenAI 互換 API のフォールバックへ流れ、現在は Cerebras `gpt-oss-120b` が設定されている。**標準ライブラリのみで動作し、`pip install` は不要。**

設計上の最優先事項は completeness(取りこぼさないこと)である。実行は「発見 → 翻訳 → 配信」の3つの永続フェーズに分割され、配信状態は**論文ごと・チャンネルごと**に記録される。したがって一部が失敗しても、まだ確認できていないチャンネルだけを再試行する。詳細は後述の「実行フェーズと永続状態」を参照。

また、毎回の実行後に **bot-emergency チャンネル(`DISCORD_WEBHOOK_BOT_EMERGENCY`)へ日本語の実行レポート**を投稿する。成功時も含めて毎回投稿されるため、どの論文がどのチャンネルへ送られたか・翻訳持ち越し・投稿失敗を実行ログとして追える。詳細は後述の「監視: bot-emergency チャンネル」を参照。

デフォルトの翻訳先は日本語(`target_language: "ja"`)だが、`config.json` の `target_language` を変更すれば他言語へ翻訳できる。DeepL は Azure / Google より対応言語が少ないため、DeepL 非対応言語を使う場合は `translators` を `["azure", "google"]` にするか、`deepl_target_language` / `azure_target_language` / `google_target_language` でバックエンドごとの言語コードを指定する。

このリポジトリに含まれる `config.json` は、従来の日本語 Discord 運用のままになっている。具体的には `target_language: "ja"`, `target_language_name: "Japanese"`, `translated_title_label: "邦題"`, `translators: ["deepl", "azure", "google"]`, `require_translation: true`。

---

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| `arxiv_bot.py` | 本体。標準ライブラリのみ使用 |
| `config.json` | 全設定(フィード、ジャンル定義、API挙動、分類パラメータ) |
| `seen_ids.json` | 永続状態: 完了ID、チャンネル別配信queue、外部審査キャッシュ、未審査の外部候補、取得元別cursor(Actions が自動commit) |
| `posted_log.json` | 投稿済み論文のメタデータログ(最大5000件、JSON 配列) |
| `scirate_weekly.py` | SciRateの日次Top 3と週次30+を専用チャンネルへ投稿するbot |
| `scirate_weekly_state.json` | SciRateの日次・週次取得、翻訳、配信、重複排除の永続状態 |
| `test_feed.xml` | ローカルテスト用のサンプル RSS |
| `tests/test_external_arxiv.py` | 隣接カテゴリAPIクエリ・外部審査・QECポリシーのテスト |
| `tests/test_delivery_reliability.py` | チャンネル別receipt、フェーズ再開、webhookエラー処理のテスト |
| `tests/test_repost_reliability.py` | 追い投稿の冪等性とwebhook検証のテスト |
| `tests/test_scirate_reliability.py` | SciRate 3フェーズパイプラインのテスト |
| `scripts/clean_discord_urls.py` | Discord チャンネル内の arXiv URL 投稿を検索・削除する補助スクリプト |
| `scripts/lock_discord_channels.py` | 指定した Discord チャンネルを非管理者には閲覧・リアクション専用にする補助スクリプト |
| `scripts/rollback_posted_day.py` | 再投稿前に `posted_log.json` と `seen_ids.json` から指定日の状態を戻す補助スクリプト |
| `scripts/audit_classification.py` | 投稿済み論文の Gemini 分類を再実行し、差分だけを表示する補助スクリプト |
| `scripts/repost_missing_channels.py` | 分類修正後、取り逃したジャンルチャンネルへ保存済み翻訳を使って追い投稿する補助スクリプト |
| `repost_plan.json` | 追い投稿プラン(論文ID → 追加チャンネル)。分類監査の結果から生成 |
| `DESIGN_BACKLOG.md` | 未決の設計判断と、その着手順序の構想 |
| `.github/workflows/notify.yml` | 実行スケジュールと Secret 参照の定義 |
| `.github/workflows/scirate_weekly.yml` | SciRate平日Top 3・日曜週次30+の実行スケジュール |
| `.github/workflows/classification_audit.yml` | 過去1日分の分類を監査する手動実行ワークフロー |
| `.github/workflows/repost.yml` | 追い投稿プランに従って不足チャンネルへ投稿する手動実行ワークフロー |

---

## 実行スケジュール

GitHub Actions により**月〜土に1日3回**自動実行される(cron の曜日指定は `1-6`。JST でも同じ日付の午前〜午後に走る)。

| UTC | JST | 目的 |
| --- | --- | --- |
| 01:05 | 10:05 | arXiv アナウンス直後(00:00 UTC 頃)の新着を捕捉 |
| 04:00 | 13:00 | 取りこぼし・遅延の補完 |
| 07:00 | 16:00 | 同上 |

`workflow_dispatch` による手動実行では3つの入力が使える。

| 入力 | デフォルト | 説明 |
| --- | --- | --- |
| `use_test_feed` | `false` | 本番 RSS の代わりに `test_feed.xml` を読み込む |
| `test_emergency_alert` | `false` | 通知本体を動かさず、bot-emergency チャンネルへテスト送信だけを行う |
| `external_backfill_days` | `"0"` | 隣接カテゴリAPIの取得元だけを指定日数まで遡る一回限りの設定。`0` なら通常の設定値のまま |

別workflowが、SciRateの平日Top 3と日曜の週次30+を1つの専用チャンネルへ投稿する。

| UTC | JST | 目的 |
| --- | --- | --- |
| 日曜 00:30 | 日曜 09:30 | SciRate の直近7日 quant-ph 人気論文を補完 |

---

## 実行フェーズと永続状態

各実行は3つのフェーズに分割され、フェーズの合間に状態ファイルをリポジトリへ commit する。各フェーズは独立して再開できるため、途中で中断した実行は最初からやり直さずに続きから再開する。

```text
発見 + 分類 → commit → 翻訳 → commit → 配信 → チャンネル別receiptをcommit
```

| フェーズ | コマンド | 処理内容 |
| --- | --- | --- |
| 発見 | `python3 arxiv_bot.py --discover-only` | RSS と隣接カテゴリAPIを取得して分類し、配信queueを書き出す |
| 翻訳 | `python3 arxiv_bot.py --translate-only` | queue の論文を翻訳し、翻訳結果を queue に保存する |
| 配信 | `python3 arxiv_bot.py --deliver-only` | Discord へ投稿し、チャンネルごとに receipt を記録する |

`--prepare-only` は発見と翻訳をまとめて実行する。フェーズフラグは排他であり、いずれも `--dry-run` と併用できない。フラグなしで `arxiv_bot.py` を実行すると1プロセスで3フェーズすべてを行う(ローカル実行の通常形)。

これにより得られるもの:

- **チャンネル単位の再試行。** ある論文が `qec` には配信できて `crypto` で失敗した場合、次回は `crypto` だけを再試行する。割り当てられた全チャンネルの receipt が揃うまで、その論文は完了扱いにしない。
- **翻訳失敗がフィードより長生きする。** 翻訳に失敗した論文は永続queueに残るため、arXiv の RSS から消えた後も再試行される。
- **未審査の外部候補が取得日数を過ぎても残る。** 審査が完了していない候補は、論文メタデータごと `external_pending` に保存される。
- **取得cursorが障害の穴を埋める。** 隣接カテゴリの各取得元は、最後に取得へ成功した時刻を `external_cursors` に記録する。次回はその経過時間に `cursor_overlap_days`(デフォルト2日)を足した日数まで遡るため、Actions や arXiv API の障害が通常の取得窓を超えても、論文が黙って抜け落ちることはない。

配信保証は意図的に **at-least-once** である。Discord が webhook を受理した直後、receipt を commit する前に runner が異常終了した場合だけは、Discord webhook に idempotency key がないため重複し得る。まれな重複よりも、黙った欠落を避ける設計を優先している。

---

## 処理フロー

### 1. 取得

`config.json` の `feeds` に列挙したカテゴリ(現在は `"quant-ph"`)の RSS を順に取得し、論文を ID でまとめる。複数フィードで同一論文が登場した場合は、設定上先にあるフィードを優先する。

加えて `external_arxiv_queries` により、arXiv API のカスタム Atom クエリから、primary が隣接カテゴリで、かつ quant-ph へ cross-list されていない候補を機械的に絞って取得する。

| 取得元 | 主な行き先 | 検索語数 |
| --- | --- | --- |
| `cs.CR` | `pqc` / `crypto` | 50 |
| `cs.CC` | `complexity` / `algo` | 9 |
| `cs.IT` | `qit` / `qec` / `network` | 6 |

検索語は取りこぼしを抑えるため再現率寄りに設定してあり、検索に一致しただけでは投稿しない。長い検索語リストは短いAPIクエリへ分割し(`terms_per_query`、デフォルト8)、各クエリを取得日数の境界までページングしてから重複排除する。これにより長すぎるURLによる取得不安定と、APIの1ページ上限による欠落の両方を避ける。

候補は通常分類と同じ分類器チェーン(プライマリ Gemini → セカンダリ Gemini → 設定済み OpenAI 互換フォールバック)による独立審査へ送られる。上記の取得元別ジャンルは優先ヒントであり、出力制限ではない。全取得元で `allow_all_genres` が有効なため、外部審査では `excluded_genres`(現在は `other`)以外の全ジャンルから `external_classify_max_genres` 件まで選べる。

審査プロンプトは completeness 優先で、実質的な副次貢献や非自明な応用も採用し、量子との関係が背景・動機・将来課題・引用・比較だけの場合に限って `skip` する。却下を保存するには `external_skip_consensus` 個のモデルが独立に `skip` する必要があり、1モデルでもジャンルを選べば採用する。1件の `skip` しか得られず再審査モデルが使えない場合は却下せず、未審査のまま残す。通常の quant-ph 分類とは異なり、外部候補については LLM 審査が決着しない場合に TF-IDF で投稿したり `other` へ流したりしない。

`qec` は量子誤り訂正符号だけでなく、量子計算・量子通信・PQCとの非自明な接点がある符号理論全般を含む。符号自体は古典でもよい。`qec_adjacent_coding_terms`(現在53語。`linear code`, `rank-metric code`, `syndrome decoding`, `self-orthogonal` など)に一致した場合、分類後に決定的ルールで `qec` を補う。これにより、code-based cryptography で使われる rank-metric・Gabidulin 符号のような論文も拾える。

分類器と翻訳APIの容量は常に通常の quant-ph 論文を優先する。外部候補の厳密審査は quant-ph の分類がすべて完了した後に開始し、翻訳もジャンル優先度にかかわらず全 quant-ph 論文を全外部論文より先に処理する。

判定結果は `seen_ids.json` の `external_reviews` に保存する。却下した ID は全体の完了IDには入れないため、後日 quant-ph に cross-list された場合は通常どおり処理できる。`lookback_days` は初回有効化時の大量の過去論文投稿を防ぐ。一回限り遡る場合は `EXTERNAL_ARXIV_LOOKBACK_DAYS`(Actions の `external_backfill_days` 入力)に日数を指定でき、通常設定の取得日数は変更されない。

### 2. フィルタリング

`should_post()` が各論文を以下の基準で判定する。

| announce_type | 挙動 |
| --- | --- |
| `new` | 常に通過(quant-ph 新着) |
| `cross` | 後述の cross-list ポリシーで判定 |
| `replace` | `include_replacements: true` のときのみ通過 |

**cross-list 投稿ポリシー(デフォルト: 全通過)**

primary カテゴリが `cross_deny_primary` リストに一致する場合のみ除外する。このリストはデフォルトで空(`[]`)のため、`hep-*`, `gr-qc`, `cond-mat.*` を含む**全 cross-list 論文が通過**する。除外したいカテゴリがあれば `cross_deny_primary` へ追加すること。

`cross_allow_primary` はホワイトリストであり、deny リストとの一致より優先される(deny 側に追加した上で例外を設けたいケース向け)。

**cross-list 分類ポリシー**

quant-ph RSS から取得した論文は、primary category に関係なく通常のAI分類と決定的なQEC/keyword後処理を行う。したがって、primary が `cs.IT` でも QEC・QIT・network などへ分類でき、`other` には上書きしない。

- quant-ph RSS 由来: primary に関係なく通常分類
- 明示的な外部source: 専用の高recall検索と厳密LLM審査を通す

`cross_classify_primary_as_quantph` と `category_other_overrides` は、quant-ph 以外の source を通常後処理へ明示的に渡す互換経路だけの制限として残る。隣接カテゴリAPI経路は独自の厳密審査を使う。

### 3. ジャンル分類 + 翻訳(2段構え)

**主経路: Gemini classify-only(TF-IDF 事前分類で2モデルへ振り分け)**

`classify_with_llm: true`(デフォルト)かつ `GEMINI_API_KEY` がある場合、タイトルと abstract を `translate_batch_size`(デフォルト5)件ずつ Gemini に一括送信し、ジャンル ID だけを返させる。

Gemini を呼ぶ前に、後述の TF-IDF 分類器で全論文を一度事前分類するが、これは**振り分け専用**であり、後述の緊急フォールバックを除いてこの事前分類結果自体が投稿されることはない。

- 事前分類のジャンルが `prescreen_defer_genres`(デフォルト: `nisq`, `hardware`, `sensing`, `foundations`, `other`)のいずれにも触れない論文は**優先グループ**となり、常にプライマリモデル `gemini_model_primary`(`gemini-2.5-flash`)で分類する
- 残りは**繰り延べグループ**。この実行での推定リクエスト数(優先グループのバッチ数+繰り延べグループのバッチ数)が `gemini_primary_run_budget`(デフォルト20)以内であれば、繰り延べグループもプライマリモデルで分類する。超える場合はセカンダリモデル `gemini_model_secondary`(`gemini-2.5-flash-lite`)を使い、無料枠の日次クォータ内に収める
- モデルごとに独立した circuit breaker を持つ: 持続的な 429、または 500/503 の連続発生は、そのモデルのみをこの実行で停止させる。未分類の論文は自動的に次の分類器へフォールスルーする(`gemini-2.5-flash` → `gemini-2.5-flash-lite` → `gpt-oss-120b` などの OpenAI 互換フォールバック)。すべての LLM 分類器が利用不可の場合のみ、TF-IDF 事前分類の結果をそのまま投稿する(緊急フォールバック)
- `gemini_min_intervals` により `gemini-2.5-flash` は7秒間隔、`gemini-2.5-flash-lite` は5秒間隔でペーシングされる。無料枠の RPD/RPM の詳細は後述の「留意事項」を参照
- プロンプトには各ジャンルの `description`(自然言語の定義文)を全文渡すため、定型キーワードを含まない論文も内容で分類される
- 出力形式: `<<<k|genre_id>>>` または `<<<k|id1,id2>>>` (マルチラベルの場合)
- 1論文に複数ジャンルを割り当てられる。主要な貢献ジャンルだけでなく、そのジャンルの読者にとっても論文が本当に価値を持つ場合は追加のジャンルも割り当てられる(詳細は後述の「マルチラベル分類」を参照)
- quant-ph RSS 由来の cross-list 論文は LLM 結果を保持し、primary quant-ph 論文と同じ QEC/keyword 後処理を受ける

**フォールバック経路: TF-IDF コサイン類似度 + キーワード加点**

すべての LLM 分類器がクォータ枯渇等で利用不可の場合、または個別エントリをモデルが返さなかった場合に使用する。

- ジャンルの `description` + `keywords` のうち1単語のキーワードを TF-IDF ベクトル化(複数語のフレーズはベクトルから除外し、後述の加点で別途評価する)
- 論文の `title + abstract` との余弦類似度を各ジャンルで計算
- `category_genre_hints` による arXiv カテゴリヒント(スコアに +0.15)と `category_other_overrides` による強制 other 判定(スコアに +1.0)を適用
- 全ジャンルに出現する語は IDF=0 になりスコアに寄与しない。さらに "quantum", "qubit", "state", "system" のような一般語はストップワードとして除外される
- トークナイザは ASCII の語のみを拾うため、実質的に英語テキストのスコアリングになる。日本語のジャンル説明文はほとんど寄与しない

余弦類似度に加えて、論文本文中のキーワード直接ヒットに対する**キーワード加点**が入る:

| 根拠 | 設定キー | デフォルト加点 |
| --- | --- | --- |
| タイトル中のキーワードフレーズ | `fallback_title_phrase_bonus` | +0.35 |
| abstract 中のキーワードフレーズ | `fallback_abstract_phrase_bonus` | +0.18 |
| タイトル中の1単語キーワード | `fallback_title_token_bonus` | +0.10 |
| abstract 中の1単語キーワード | `fallback_abstract_token_bonus` | +0.03 |

`config.json` の `fallback_keyword_boosts` には、ジャンルごとの追加フレーズリスト(例: "cat qubit" → `qec`、"barren plateau" → `nisq`)を定義でき、上記のフレーズ加点が適用される。これにより、ジャンル `description` と語彙が重ならない論文もフォールバック経路で拾える。

### 4. マルチラベル分類

1論文を複数ジャンルに分類し、それぞれのチャンネルへ投稿できる。

- `classify_max_genres`(デフォルト2): LLM プロンプトで要求するジャンル数であり、TF-IDF 経路では上限として実際に切る
- `classify_secondary_ratio`(デフォルト0.7、現行設定0.82。TF-IDF フォールバック時のみ適用): 2番目以降のジャンルを採用するのは、そのスコアが最上位ジャンルのスコアのこの比率以上の場合のみ。弱い偶発的マッチで多チャンネルに投稿されることを防ぐ
- Gemini 経路では、まず論文の主要な貢献に該当するジャンルを選ばせ、そのうえでそのジャンルのチャンネル読者にとっても論文が本当に価値を持つ場合に限り、さらにジャンルを追加させる。例えばトランスバーサル/フォールトトレラント論理のために設計された新しい誤り訂正符号は `qec` と `ft` の両方に該当する。ただし、単にツールやデモの土台として使われているだけのジャンルは追加しない(例: 既知のアルゴリズムを量子ハードウェア上で実行しただけの論文は `algo` ではなく `hardware`)。モデルが同じ ID を重複して返した場合は、順序を保ったまま重複排除される
- 分類後の決定的な後処理がジャンルを追加することがある(`force_genre_keywords` と広義 `qec` の符号理論ルール)。このため、投稿された論文が `classify_max_genres` より多いジャンルを持つことがある
- **各投稿の embed footer には割り当てられた全ジャンル名が表示される**(例: `quant-ph | 量子複雑性理論, 量子アルゴリズム | new`)。あるチャンネルの読者にも、その論文の他の分類が分かる

### 5. 翻訳フォールバックチェーン

現在の標準設定:

```text
DeepL → Azure Translator → Google Cloud Translation
```

- 先頭から順に試行し、成功した時点で次の論文へ移る
- abstract の翻訳に成功した投稿対象論文について、英語タイトルとは別に翻訳済みタイトルも同じ翻訳チェーンで作成する
- クォータ枯渇を検知したバックエンド(DeepL: 456、Google: 403/429、翻訳役の Gemini: 持続的 429)はその実行回では以後スキップされる(**circuit breaker**)
- DeepL と Azure が失敗した場合、Google は `google_skip_translation_genres` の対象外論文にだけ使う。対象ジャンルだけに属する論文は、持ち越さず英語原文で投稿する
- 許可された全段で翻訳できなかった論文は `require_translation: true`(デフォルト)の場合は投稿しない。永続queueに残り、RSS から消えた後も次回以降に再試行される

### 6. Discord 配信

論文ごとに分類されたジャンル数分の投稿を行う。各投稿間隔は1.2秒(Discord レート制限対策)。embed footer は `primaryカテゴリ | 割り当てられた全ジャンル名 | announce_type` の形式で、2ジャンルに分類された論文はどちらのチャンネルでも両方のジャンル名が表示される。

配信はチャンネルごとに検証される:

- Discord が投稿を受理した直後にチャンネルごとの receipt(`status: delivered` と時刻)を書き込み、その時点で状態ファイルを書き出す
- **Webhook secret が未設定のジャンルは、黙って成功扱いにせず配信失敗として扱う。** 他チャンネルへのフォールバックはせず、その論文は未完了のまま残り、実行レポートには `(webhook未設定)` と表示される
- **2つのジャンルが同じ Webhook URL に解決される場合も失敗として扱う**(`(webhook重複)`)。1件しか投稿されないのに2チャンネル配信成功と誤認されるのを防ぐため
- `DISCORD_WEBHOOK_GENERAL` は、ジャンルが1つも付かなかった論文に対する最後の逃し先としてのみ使う

`posted_log.json` に投稿済み論文のメタデータを記録する。記録内容:

```json
{
  "id": "2506.12345",
  "posted_at": "2025-06-24T01:10:00Z",
  "title": "...",
  "title_ja": "...",
  "title_translated": "...",
  "translation_language": "ja",
  "authors": "...",
  "link": "https://arxiv.org/abs/2506.12345",
  "primary": "quant-ph",
  "announce_type": "new",
  "genre_ids": ["qec", "ft"],
  "genre_names": ["誤り訂正・符号理論", "フォールトトレラント計算"],
  "classifier": "gemini-2.5-flash",
  "abstract_en": "...",
  "abstract_ja": "...",
  "abstract_translated": "..."
}
```

`classifier` はどのモデルが分類したかを記録する: `"gemini-2.5-flash"`、`"gemini-2.5-flash-lite"`、`"gpt-oss-120b"`、`"tfidf"`(緊急フォールバック)のいずれか。記録されるのはモデル名だけで、分類後に決定的な後処理が追加したジャンルは現状ログ上で区別できない。

`title_translated`, `abstract_translated`, `translation_language` は多言語対応用の汎用フィールド。`title_ja` と `abstract_ja` は、既存の日本語ログや従来運用との互換性のために引き続き保存される。

### 7. bot-emergency チャンネルへの実行レポート

投稿処理の後、**成功時も含めて毎回**、`DISCORD_WEBHOOK_BOT_EMERGENCY` へ日本語のサマリー embed を1件送信する。bot-emergency チャンネルが実行ログを兼ねる。レポートの内容:

- フィードから取得した論文数と、新規投稿対象の論文数
- 投稿に成功した論文数(と Discord メッセージ数)、翻訳持ち越し数、投稿失敗数
- 分類の内訳: モデル別の投稿論文数(例: `🏷 分類: gemini-2.5-flash: 13件 / gemini-2.5-flash-lite: 21件 / TF-IDF: 2件`)
- 翻訳の内訳: バックエンド別(DeepL / Azure / Google)の成功数と、実行中に circuit breaker で停止したバックエンド
- **投稿した論文の一覧と、それぞれの送信先チャンネル**。翻訳持ち越し・投稿失敗の論文一覧も別掲

embed の色は、全成功なら緑、持ち越しありなら橙、Discord 投稿失敗ありなら赤。論文一覧が長い場合は Discord の embed 上限に収まるよう省略される。`DISCORD_WEBHOOK_BOT_EMERGENCY` が未設定の場合、レポートはログにメッセージを出してスキップされる。

---

## 失敗時の扱い

取得できなかった取得元を「新着なし」として報告することはない。

- 各フェーズは `continue-on-error` で実行されるため、発見フェーズが失敗しても、取得できた分を記録しつつ問題を表面化できる
- 発見・翻訳・配信のいずれかが失敗した場合、最後のワークフローステップが**明示的に run を失敗させる**(`exit 1`)。したがって Actions の実行結果が誤って緑にならない
- RSS と arXiv API の失敗は取得元ごとに集計され、bot-emergency チャンネルへ報告される
- ログからは secret が除去される。URL のクエリ文字列と Discord webhook のパス要素は伏字にし、環境変数名に `SECRET` / `PASSWORD` / `WEBHOOK` を含むもの、または `_KEY` / `_TOKEN` で終わるものの値は、出力前にエラーテキスト中で `<redacted>` に置換される

---

## 監視: bot-emergency チャンネル

`DISCORD_WEBHOOK_BOT_EMERGENCY` の Webhook には、運用メッセージがすべて日本語で届く。

| メッセージ | タイミング |
| --- | --- |
| ✅ / 🟡 / 🚨 実行レポート | 通常通知とSciRate日次・週次ダイジェストの毎回の実行後 |
| ⚠️ 翻訳全停止アラート | チェーン内の全翻訳バックエンドがその実行で停止し、論文が黙って持ち越されているとき |
| 🚨 取得・配信失敗 | RSS フィード、arXiv API クエリ、Discord 配信のいずれかがその実行で失敗したとき |

通知本体を動かさずにテスト送信したい場合は、`notify.yml` を手動実行して `test_emergency_alert` にチェックを入れる。

---

## 分類監査ワークフロー

`.github/workflows/classification_audit.yml`(手動実行のみ)は、`posted_log.json` に記録済みの論文に対して Gemini 分類を再実行し、投稿時と分類が変わったエントリだけを表示する。ジャンルの `description` やプロンプトを変更した後に、特定の日の分類をスポットチェックする用途。

バッチごとに、本体の分類チェーンと同様、まず `gemini_model_primary` を試し、失敗すると `gemini_model_secondary` にフォールスルーする。`--model <id>` を(繰り返し指定可能で)渡すと、このチェーンを任意のモデル列に上書きできる。

入力:

| 入力 | デフォルト | 説明 |
| --- | --- | --- |
| `date` | (必須) | 監査するローカル日付。例: `2026-07-03` |
| `timezone` | `Asia/Tokyo` | `posted_at` をローカル日付にまとめる際のタイムゾーン |

ローカルでの実行:

```bash
export GEMINI_API_KEY="..."
python3 scripts/audit_classification.py --date 2026-07-03 --timezone Asia/Tokyo
```

なお、監査スクリプトは全論文に通常の quant-ph 後処理を適用する。隣接カテゴリの外部経路で採用された論文も同様に扱われるため、それらの論文で報告される差分は、実際の分類変更ではなく監査側の見かけ上の差分である場合がある。

監査の結果、ジャンルが追加された論文は、追い投稿ワークフロー(`repost.yml`、手動 `workflow_dispatch`、入力: `plan` パス(デフォルト `repost_plan.json`)、`dry_run`(デフォルト `true`))で不足チャンネルにだけ投稿できる。`posted_log.json` の `title_translated` / `abstract_translated` を再利用するため翻訳 API は呼び出さず、不足チャンネルごとに、footer に修正後の全ジャンル一覧を載せた embed を1件投稿する。ログエントリの `genre_ids` / `genre_names` は修正後の分類に更新される(`repost_channels`、`repost_genre_ids`、`reposted_at` を記録)。

追い投稿スクリプトは冪等である。追い投稿済みとして記録されたチャンネルはスキップされるため、同じプランを再実行しても重複投稿しない。`seen_ids.json` には一切触れず、Webhook secret が未設定のチャンネルは general チャンネルへのフォールバックはせず意図的にスキップし、同じ Webhook URL に解決されるチャンネルは拒否する。実行後は日本語の実行レポートが bot-emergency チャンネルへ送られる。

ローカルでの実行:

```bash
python3 scripts/repost_missing_channels.py --plan repost_plan.json --dry-run
```

---

## SciRate 日次・週次専用チャンネル

`.github/workflows/scirate_weekly.yml` は SciRate の編集コンテンツをすべて `DISCORD_WEBHOOK_SCIRATE` の1チャンネルへ送る。QEC・QITなど通常の分類チャンネルには再投稿しない。

| 日本時間 | 対象 | 投稿形式 |
| --- | --- | --- |
| 月〜金 23:30 | その日の `pubdate` と厳密に一致し、`scirate_daily_min_scites`（既定1）以上の上位3本 | Top 3を1件のランキングembedに集約 |
| 日曜 23:30 | 日曜を終端とする7日間で `scirate_min_scites`（既定30）以上の全論文 | 論文ごとに通常形式のembedを1件 |
| 土曜 | 実行なし | なし |

日次の同点順位はSciRate自身の決定的な並びをそのまま使う。1 Scite以上が3本未満なら、0 Sciteの論文で埋めずに該当本数だけ投稿する。APIが空なら、その日は対象となる発表バッチなしとしてチャンネル投稿を行わない。週次カードには現在のScite数を付け、日次Top 3に掲載済みならその旨も表示する。

本番ではSciRateのHTMLをスクレイピングしない。upstream PR `scirate/scirate#535` で提案されているJSON endpoint（現在は `https://scirate.com/arxiv/quant-ph.json?date=YYYY-MM-DD&range=N&page=1`）を毎回最優先で確認する。公式endpointが利用できない場合に限り、同じ`papers` schemaを返す信頼済みJSON中継をRepository Variable `SCIRATE_RELAY_URL_TEMPLATE`で指定できる。1ページで完結するsnapshotには`"complete": true`と厳密な期間metadataを必須とする。認証が必要ならActions Secret `SCIRATE_RELAY_BEARER_TOKEN`を使い、Bearer headerは中継先にだけ送信する。これは姉妹研究室など、通常に取得できる環境が1日1回生成する低頻度snapshot用であり、Cloudflare回避proxy用ではない。

有効な`papers`配列が返らない間は、公式のみなら`waiting_for_api`、中継設定済みなら`waiting_for_source`とし、永続キューだけを処理してbot-emergencyに案内を出し、正常終了する。初回障害を含め、取得前に対象期間を必ず`pending_discovery`へ保存するため、後続runで自動的に追いつく。公式APIは毎回先に再試行するので、利用可能になった時点で中継から自動復帰する。取得済み投稿と翻訳も永続キューから再開する。

日次ではresponse dateと各行の`pubdate`を検証する。全体のスコア降順も検証し、週次で`scirate_api_max_pages`以内に30 Scitesの境界まで取得できなければ、不完全な結果を投稿せず失敗させる。SciRateが別pathを公開した場合はRepository Variable `SCIRATE_API_URL_TEMPLATE`を変更する。`{date}`・`{days}`・`{page}`を使用できる。paginationは`SCIRATE_API_PAGE_SIZE`と`SCIRATE_API_MAX_PAGES`で調整できる。中継URLも同じplaceholderを使う。URLにcredentialを埋め込まず、Bearer token secretを使う。

各runでは当期を最初に試し、その後`scirate_backlog_max_periods`（既定8）件まで古いpending期間を再試行する。当期だけ未生成でも過去分を回収でき、壊れた過去1件が当期を塞ぐこともない。どちらの失敗も後続成功で消さず、運用レポートと`pending_discovery`に残す。

1ファイルで完結する中継snapshotは、上記英語節のJSON例のように`date`・`complete: true`・`range_days`・`period_start`・`period_end`・`papers`を返す。週次なら7および月曜〜日曜の全期間と厳密一致させる。各行には最低限`uid`・`scites_count`・`pubdate`を入れ、SciRateの順序（Scite数降順および同点順）を保持する。不正な行、期間metadata不一致、`pubdate`範囲外、重複ID、降順崩れは不完全投稿せずsnapshot全体を拒否する。

日次・週次の重複排除とretryは`scirate_weekly_state.json`が管理する。3フェーズ（`--discover-only` / `--translate-only` / `--deliver-only`）は維持する。`posted_log.json`に翻訳・分類表示名があれば再利用し、翻訳がなければ通常の翻訳chainを使う。投稿先は常にSciRate専用チャンネルなので、新たなAI分類は呼ばない。運用レポートだけは引き続きbot-emergencyへ送る。

ローカル確認:

```bash
python3 scirate_weekly.py --mode daily --date 2026-08-03 --dry-run
python3 scirate_weekly.py --mode weekly --date 2026-08-09 --dry-run
```

`--html-file PATH` はローカルfixtureによるparser確認専用として残している。本番ではHTML fallback、CAPTCHA solver、proxy rotation、origin IP直撃、403回避用のUser-Agent偽装は行わない。中継にはbrowser cookieやCloudflare clearance tokenではなく、正規化したJSONだけを置く。

---

## ジャンル一覧(15種)

| ID | 名称 | 主なトピック |
| --- | --- | --- |
| `qec` | 誤り訂正・符号理論 | 安定化符号・表面符号・LDPC・デコーダ設計、および量子隣接の古典符号理論 |
| `ft` | フォールトトレラント計算 | マジックステート蒸留・格子手術・資源推定 |
| `algo` | 量子アルゴリズム | Grover・Shor・量子ウォーク・位相推定・HHL |
| `complexity` | 量子複雑性理論 | BQP・QMA・クエリ複雑性・局所ハミルトニアン |
| `nisq` | 変分・NISQアルゴリズム | VQE・QAOA・エラー緩和・バレンプラトー |
| `sim` | 量子シミュレーション | ハミルトニアンシミュレーション・Trotter・量子化学 |
| `qml` | 量子機械学習 | QNN・量子カーネル・量子強化学習 |
| `qit` | 量子情報理論 | エンタングルメント理論・資源理論・通信路容量 |
| `network` | 量子ネットワーク・通信 | 量子中継器・エンタングルメント分配・量子テレポーテーション |
| `crypto` | 暗号・セキュリティ | QKD・DI-QKD・blind/verifiable/secure delegation・SMC/量子オークション |
| `pqc` | 耐量子計算機暗号 | 格子暗号(LWE/Kyber)・NIST PQC 標準化 |
| `hardware` | 量子ハードウェア・実装 | 超伝導・イオントラップ・Rydberg・スピン量子ビット |
| `sensing` | 量子センシング・計測 | ハイゼンベルク限界・量子フィッシャー情報・原子時計 |
| `foundations` | 量子基礎・測定理論 | Bell不等式・デコヒーレンス・量子熱力学 |
| `other` | その他・異分野 | hep-*・gr-qc・nucl-*・cond-mat(一般)など量子情報外の論文 |

どのジャンルにも該当しない論文は `other` へ送られる。`DISCORD_WEBHOOK_GENERAL` は、ジャンルオブジェクトが1つも付かなかった場合の最後の逃し先としてのみ使われる。

---

## セットアップ

### 自分の Discord サーバーで使う最小構成

最小構成では、15ジャンルすべてのチャンネルを作る必要はない。

1. このリポジトリを fork する。
2. テスト用または general 用チャンネルに Discord Webhook を1つ作る。
3. その Webhook URL を `DISCORD_WEBHOOK_GENERAL` という repository secret として登録する。
4. LLM による分類を使う場合は `GEMINI_API_KEY` を登録する。未設定の場合は TF-IDF 分類にフォールバックする。
5. 翻訳キーを少なくとも1つ登録する。無料枠の大きさを優先するなら通常は `AZURE_TRANSLATOR_KEY`、対応言語の広さを優先するなら `GOOGLE_TRANSLATE_API_KEY`、DeepL 対応言語だけでよければ `DEEPL_API_KEY`。
6. 他言語で使う場合は `config.json` を編集する。例: `target_language: "fr"` と `translators: ["azure", "google"]`。
7. schedule に任せる前に、Actions タブから一度手動実行して確認する。

公式リファレンス:

- Discord Webhook の作成: [Intro to Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
- GitHub Actions Secrets: [Using secrets in GitHub Actions](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions)
- Azure Translator の API と言語コード: [Translate method](https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/reference/v3/translate)
- Azure Translator の料金: [Pricing](https://azure.microsoft.com/en-us/pricing/details/translator/)
- Google Cloud Translation の言語コード: [Language support](https://cloud.google.com/translate/docs/languages)
- DeepL の翻訳先言語コード: [Languages supported](https://developers.deepl.com/docs/resources/supported-languages)
- Gemini API キー: [Google AI Studio](https://aistudio.google.com/)

### 1. Discord Webhook の作成

通知先チャンネルごとに「チャンネル設定 → 連携サービス → ウェブフック」で Webhook URL を作成する。各ジャンルに対応するチャンネルを用意し、それぞれの URL を後述の Secret として登録する。

ジャンルを細かく分けずに運用する場合は `DISCORD_WEBHOOK_GENERAL` のみ設定すれば全論文がそこへ届く。

ジャンル別に振り分けたい場合は、チャンネルごとに Webhook を作り、対応する `DISCORD_WEBHOOK_*` secret に URL を入れる。ジャンルごとに別々の Webhook を用意すること。2つのジャンルが同じ URL を共有している場合は設定エラーとして拒否される。Webhook URL や API キーは repository に commit しないこと。

### 2. API キーの取得

`config.json` の `translators` に列挙した順に試行される。現在の標準設定は `["deepl", "azure", "google"]`。**未登録のバックエンドは自動スキップされる。**

| バックエンド | 用途 | 無料枠 | Secret 名 |
| --- | --- | --- | --- |
| Gemini | 分類のみ | 無料枠あり(カード登録不要) | `GEMINI_API_KEY` |
| Cerebras `gpt-oss-120b` | 分類のみ(Gemini 後のフォールバック) | 無料枠あり | `CEREBRAS_API_KEY` |
| DeepL | 翻訳 | 月50万文字まで無料 | `DEEPL_API_KEY` |
| Azure Translator | 翻訳 | F0 で月200万文字まで無料 | `AZURE_TRANSLATOR_KEY` + `AZURE_TRANSLATOR_REGION` |
| Google Cloud Translation | 翻訳 | 月50万文字まで無料(**請求先アカウント必須**) | `GOOGLE_TRANSLATE_API_KEY` |

Gemini API キーは [Google AI Studio](https://aistudio.google.com/) で発行できる。Cerebras キーを追加すると、Gemini 2モデルが使えない場合に `gpt-oss-120b` へフォールバックする。LLM 分類キーが何もない場合は TF-IDF 分類にフォールバックする。

### 3. GitHub Secrets の登録

`Settings → Secrets and variables → Actions → New repository secret` に以下を登録する。

**Webhook (全15ジャンル + general + bot-emergency + SciRate)**

```text
DISCORD_WEBHOOK_GENERAL
DISCORD_WEBHOOK_BOT_EMERGENCY   # 実行レポート・障害アラート用(推奨)
DISCORD_WEBHOOK_SCIRATE         # 日次Top 3・週次30+専用
DISCORD_WEBHOOK_QEC
DISCORD_WEBHOOK_FT
DISCORD_WEBHOOK_ALGO
DISCORD_WEBHOOK_COMPLEXITY
DISCORD_WEBHOOK_NISQ
DISCORD_WEBHOOK_SIM
DISCORD_WEBHOOK_QML
DISCORD_WEBHOOK_QIT
DISCORD_WEBHOOK_NETWORK
DISCORD_WEBHOOK_CRYPTO
DISCORD_WEBHOOK_PQC
DISCORD_WEBHOOK_HARDWARE
DISCORD_WEBHOOK_SENSING
DISCORD_WEBHOOK_FOUNDATIONS
DISCORD_WEBHOOK_OTHER
```

SciRate中継に認証が必要な場合のみ、次のSecretも登録する。

```text
SCIRATE_RELAY_BEARER_TOKEN      # 指定したHTTPS中継URLにだけ送信
```

中継endpointはSecretではなくRepository Variable
`SCIRATE_RELAY_URL_TEMPLATE`へ分離して登録する。redirectは拒否し、tokenを
公式SciRate endpointへ送ることはない。

**API キー(使うもののみ)**

```text
GEMINI_API_KEY
CEREBRAS_API_KEY         # gpt-oss-120b 分類フォールバック用、省略可
DEEPL_API_KEY            # 省略可
AZURE_TRANSLATOR_KEY     # 省略可
AZURE_TRANSLATOR_REGION  # Azure resource が要求する場合に設定
GOOGLE_TRANSLATE_API_KEY # 省略可
```

Webhook secret が未設定のジャンルは配信失敗として報告され再試行されるため、実際に分類先となるジャンルにはすべて Webhook を登録するか、そのジャンルを `config.json` から削除すること。

### 4. 動作確認

Actions タブ → `workflow_dispatch` → 「Run workflow」で手動実行する。

---

## ローカル動作確認

### ユニットテスト

```bash
python3 -m pytest tests/ -q
```

同じテストは両方の定期ワークフローの最初のステップでも実行されるため、壊れた変更は投稿が起きる前に失敗する。

### dry-run モード(API不使用)

Discord・LLM・翻訳 API を一切呼び出さず、TF-IDF による分類結果だけを標準出力に表示する。本番の分類そのものではなく、配管の確認用である。

```bash
python3 arxiv_bot.py --dry-run
```

実際の arXiv フィードを取得して分類するため、平日かつ arXiv アナウンス後(JST 10:00 頃以降)に実行する必要がある。週末・休日はフィードが空になる。

### test_feed.xml を使った翻訳のみのローカルテスト

Discord へ投稿せず、`seen_ids.json` や `posted_log.json` も更新せずに、別言語への翻訳だけを確認できる。

```bash
export AZURE_TRANSLATOR_KEY="..."
export AZURE_TRANSLATOR_REGION="japaneast"  # Azure resource の region が必要な場合
export ARXIV_TEST_FEED=test_feed.xml

python3 - <<'PY'
import arxiv_bot

cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
cfg.update({
    "translators": ["azure"],
    "target_language": "de",
    "target_language_name": "German",
    "translated_title_label": "Deutscher Titel",
    "show_translated_title": True,
})

paper = arxiv_bot.fetch_feed("quant-ph")[0]
title_de = arxiv_bot.translate_batch([paper["title"]], cfg)[0]
abstract_de = arxiv_bot.translate_batch([paper["abstract"]], cfg)[0]

print("ID:", paper["id"])
print("German title:", title_de)
print("German abstract:", abstract_de)
PY

unset ARXIV_TEST_FEED
```

### test_feed.xml を使った Discord フルテスト

ローカルの RSS ファイルを読み込み、翻訳・Discord 投稿まで含む全経路を確認できる。ただし `ARXIV_TEST_FEED` が変えるのは入力だけであり、投稿先は設定済みの本番 Webhook、更新されるのも本番の状態ファイルである点に注意。

```bash
export GEMINI_API_KEY="..."
export DEEPL_API_KEY="..."
export DISCORD_WEBHOOK_GENERAL="..."   # テスト用チャンネルのURL
export ARXIV_TEST_FEED=test_feed.xml
python3 arxiv_bot.py
```

テスト投稿が `seen_ids.json` に記録されるのを避けたい場合は、実行後に `seen_ids.json` を `{"seen": []}` に戻すこと。ライブ運用に戻す際は `unset ARXIV_TEST_FEED`。

---

## カスタマイズ

### ジャンルの追加・変更

`config.json` の `genres` 配列を編集する。各ジャンルオブジェクトのフィールド:

| キー | 必須 | 説明 |
| --- | --- | --- |
| `id` | ○ | 英数字・アンダースコアのみ。重複不可。LLM の出力 ID としても使われる |
| `name` | ○ | Discord embed に表示されるジャンル名。標準設定では日本語名称 |
| `description` | ○ | **LLM 分類の判定根拠**。詳細かつ他ジャンルとの境界を明示する文が分類精度を高める |
| `webhook_env` | ○ | Secret に登録した環境変数名(例: `"DISCORD_WEBHOOK_QEC"`) |
| `keywords` | ○ | TF-IDF フォールバック時に使用する語のリスト |

ジャンルを追加した場合は対応する Discord チャンネルの Webhook を Secret に登録し、`.github/workflows/notify.yml`、`scirate_weekly.yml`、`repost.yml` の `env:` セクションにも追記すること。

### 分類パラメータ

`config.json` で調整できる分類関連の設定:

| キー | デフォルト(現行設定) | 説明 |
| --- | --- | --- |
| `classify_with_llm` | `true` | `false` にすると常に TF-IDF フォールバックを使用 |
| `classify_min_score` | `0.05`(`0.08`) | TF-IDF スコアの採用下限 |
| `classify_max_genres` | `2` | LLM に要求するジャンル数。TF-IDF 経路では上限として実際に切る |
| `classify_secondary_ratio` | `0.7`(`0.82`) | TF-IDF 使用時、2番目以降のジャンルを採用するための最低スコア比率 |
| `force_genre_keywords` | `{}` | 指定語がタイトル/abstractに出た場合、分類結果へ該当ジャンルを追加 |
| `qec_adjacent_coding_terms` | 53語 | 分類後に決定的に `qec` を追加する符号理論の語句 |
| `fallback_keyword_boosts` | `{}` | TF-IDF フォールバック時にキーワード加点するジャンル別フレーズリスト |
| `fallback_title_phrase_bonus` | `0.35` | タイトル中のキーワードフレーズへの加点 |
| `fallback_abstract_phrase_bonus` | `0.18` | abstract 中のキーワードフレーズへの加点 |
| `fallback_title_token_bonus` | `0.10` | タイトル中の1単語キーワードへの加点 |
| `fallback_abstract_token_bonus` | `0.03` | abstract 中の1単語キーワードへの加点 |
| `gemini_model_primary` | `"gemini-2.5-flash"` | 分類のプライマリモデル |
| `gemini_model_secondary` | `"gemini-2.5-flash-lite"` | 推定リクエスト数が `gemini_primary_run_budget` を超えた場合に繰り延べグループで使用。プライマリモデルがレート制限で停止した際のフォールバック先にもなる |
| `llm_classification_fallbacks` | Cerebras `gpt-oss-120b` | 両方の Gemini モデルが失敗した後に試す OpenAI 互換の分類フォールバック |
| `gemini_min_intervals` | flash: `7`, flash-lite: `5` | モデルごとのリクエスト間隔(秒)。`gemini_min_interval_sec` を上書きする |
| `gemini_primary_run_budget` | `20` | この推定リクエスト数を超えると繰り延べグループがセカンダリモデルに切り替わる閾値 |
| `prescreen_defer_genres` | `["nisq","hardware","sensing","foundations","other"]` | 論文を繰り延べグループへ振り分ける TF-IDF 事前分類のジャンル |
| `external_classify_max_genres` | `3` | completeness優先の外部審査で割り当てる最大ジャンル数 |
| `external_skip_consensus` | `2` | 外部論文の却下に必要な独立LLMの`skip`票数 |
| `external_arxiv_queries` | 隣接3カテゴリのクエリ | カテゴリごとのAPI検索語、ジャンルの優先ヒント、取得日数、APIページサイズ、クエリ分割数、審査基準。個別に止める場合は `enabled: false` |

`external_arxiv_queries` の取得元ごとのキー:

| キー | デフォルト | 説明 |
| --- | --- | --- |
| `enabled` | `true` | 設定を消さずにこの取得元だけ停止する |
| `candidate_genres` | 取得元ごと | 審査プロンプトで示す優先ヒント。出力制限ではない |
| `allow_all_genres` | `true` | `excluded_genres` 以外の全ジャンルを審査で選べるようにする |
| `excluded_genres` | `["other"]` | 外部審査が決して選べないジャンル |
| `lookback_days` | `4` | 通常の取得日数 |
| `cursor_overlap_days` | `2` | 最後に取得成功してからの経過日数に上乗せする日数 |
| `max_results` | `100` | arXiv API のページサイズ |
| `terms_per_query` | `8` | 1リクエストあたりの検索語数 |
| `terms` | 取得元ごと | 再現率寄りの検索語 |
| `review_instructions` | 取得元ごと | 審査プロンプトに追加する取得元固有の指示 |

実行ログには LLM の利用状況が出力される。例:

```text
[info] Gemini usage: mode=classify-only, model=gemini-2.5-flash+gemini-2.5-flash-lite+gpt-oss-120b, requests=17, classified=82/82, tfidf_fallback=0, disabled_models=None
```

### arXiv カテゴリヒント

cross-list 論文の primary カテゴリから分類を補助する設定。

- `cross_classify_primary_as_quantph`: quant-ph 以外の source を通常後処理へ明示的に渡す場合の互換制限。quant-ph RSS から実際に取得した論文は primary に関係なく通常分類する
- `category_genre_hints`: カテゴリ → ジャンル ID のマッピング。該当カテゴリの論文は指定ジャンルのスコアが +0.15 される
- `category_other_overrides`: 互換経路で追加で明示的に `other` 扱いしたい primary カテゴリ

### crypto 強制キーワード

`force_genre_keywords.crypto` に含まれる語がタイトルまたは abstract に出た場合、LLM/TF-IDF の結果に `crypto` を追加する。現在は、verifiable / blind / secure / delegated quantum computation 周辺の取り漏らしを避けることを狙って、以下の語を入れている。

```text
blind, blindness, verifiable, verifiability, secure, securely, security,
delegated quantum computation, secure delegation, blind delegation,
verifiable delegation, untrusted server, malicious server, client-server
```

マッチングはタイトルと abstract 全体に対する前方一致寄りの単語一致であるため、このリスト中の単独の一般語(`blind`, `secure`, `security`, `verifiable`)は無関係な論文でも発火し得る。このリストを絞るかどうかの未決事項は `DESIGN_BACKLOG.md` を参照。

### cross-list フィルタ

| キー | 説明 |
| --- | --- |
| `cross_deny_primary` | このカテゴリが primary の cross-list 論文を除外する(デフォルト空=全通過) |
| `cross_allow_primary` | deny リストより優先されるホワイトリスト |

### 翻訳・投稿設定

| キー | デフォルト | 説明 |
| --- | --- | --- |
| `translators` | `["deepl","azure","google"]` | 翻訳バックエンドの試行順 |
| `target_language` | `"ja"` | 翻訳先言語コード。個別指定がない場合は各翻訳バックエンドに渡される |
| `target_language_name` | `"Japanese"` | LLM 翻訳プロンプトで使う翻訳先言語名 |
| `deepl_target_language` | 未設定 | DeepL 専用の翻訳先言語コード。例: `JA`, `EN-US`, `PT-BR` |
| `azure_target_language` | 未設定 | Azure 専用の翻訳先言語コード。未設定時は `target_language` を使う |
| `azure_translator_endpoint` | 未設定 | Azure endpoint。未設定時は `https://api.cognitive.microsofttranslator.com` |
| `google_target_language` | 未設定 | Google 専用の翻訳先言語コード。未設定時は `target_language` を使う |
| `translated_title_label` | `"邦題"` | Discord embed で翻訳済みタイトルの前に表示するラベル |
| `translate_batch_size` | `5` | 1リクエストにまとめる論文数 |
| `max_translate_chars` | `2000` | 翻訳バックエンドに渡す abstract の最大文字数(超過は切り捨て) |
| `azure_min_interval_sec` | `1.2` | Azure Translator リクエスト間の最小間隔(秒) |
| `azure_max_retries` | `4` | Azure Translator の 429 rate limit 応答に対するリトライ回数 |
| `google_min_interval_sec` | `1.2` | Google Translate リクエスト間の最小間隔(秒) |
| `google_max_retries` | `3` | Google Translate の 429 / user-rate-limit 応答に対するリトライ回数 |
| `translation_priority_genres` | 15ジャンルID | 分類後に翻訳・投稿するジャンル優先順 |
| `translate_only_matched` | `false` | `true` にするとジャンル未分類論文は翻訳しない(API節約) |
| `google_skip_translation_genres` | `["other","foundations","sensing","nisq"]` | Google だけが残った場合、このリスト内のジャンルだけに属する論文は英語原文で投稿して Google quota を節約する |
| `require_translation` | `true` | `true`: 翻訳失敗論文は次回へ持ち越す / `false`: 英語のまま投稿 |
| `show_translated_title` | `true` | `true` にすると Discord embed 本文の先頭に翻訳済みタイトルを表示する |
| `show_original_abstract` | `false` | `true` にすると翻訳文に加えて英語 abstract も embed に含める |
| `include_replacements` | `false` | `true` にすると差替え論文(replace)も投稿する |
| `scirate_daily_top_n` | `3` | 平日の日次ランキングに載せる本数 |
| `scirate_daily_min_scites` | `1` | 日次の最低Scite数。0件の論文で空き順位を埋めない |
| `scirate_min_scites` | `30` | 日曜の週次ダイジェストで投稿する最低Scite数 |
| `scirate_api_url_template` | SciRate PR #535 path | JSON API template。`{date}`・`{days}`・`{page}`を使用可能 |
| `scirate_api_max_pages` | `20` | threshold境界前の不完全取得を拒否するための安全上限 |
| `scirate_api_page_size` | `50` | 最終ページ判定に使う想定upstream page size |
| `scirate_backlog_max_periods` | `8` | 当期取得後に1 runで再試行する過去の失敗期間数 |
| `gemini_model` | `"gemini-2.5-flash"` | レガシーなデフォルト値。Gemini が翻訳も兼ねる「翻訳+分類」経路でのみ使用される。分類には `gemini_model_primary` / `gemini_model_secondary` を使う |
| `gemini_min_interval_sec` | `7` | Gemini リクエスト間の最小間隔(秒)。`gemini_min_intervals` に該当モデルの指定がない場合のフォールバック値 |
| `gemini_max_retries` | `4` | 一時的エラー時のリトライ上限回数 |
| `gemini_overload_giveup` | `2` | 過負荷エラーが連続したら circuit breaker を開く閾値 |

---

## セキュリティ

- このリポジトリに認証情報は一切保存されていない。Webhook URL と API キーはすべて、実行時に GitHub Actions Secrets またはローカルの環境変数から渡される
- `.gitignore` は `.env` と `error_diagnostics.jsonl` を除外している。ローカルで実際の値を含みやすいのはこの2ファイルである
- この README と補助スクリプトに出てくる Discord ID はすべてプレースホルダであり、実在のチャンネルやサーバーではない
- エラーログは出力前に伏字化される。URL のクエリ文字列は除去され、Discord webhook のパス要素は `<redacted>` になり、環境変数名に `SECRET` / `PASSWORD` / `WEBHOOK` を含むもの、または `_KEY` / `_TOKEN` で終わるものの値はエラーテキストから取り除かれる
- `posted_log.json` と `seen_ids.json` はリポジトリに commit される。内容は arXiv のメタデータ(ID、タイトル、著者、abstract、翻訳)だけで、運用上の認証情報は含まれない。ただし非公開の Discord コミュニティ向けに fork する場合、このログによって bot の投稿履歴が全部公開される点は意識しておくこと
- 万一 Webhook URL を commit してしまった場合は、ただちに Discord 側で再発行すること。古い値がすでにキャッシュされている可能性があるため、git 履歴の書き換えだけでは不十分である

---

## 留意事項

- RSS の `<category>` 要素の先頭を primary カテゴリとみなすヒューリスティックを使用している(arXiv API の保証ではなく経験則)。
- ジャンル分類は LLM チェーン(主)と TF-IDF(フォールバック)によるヒューリスティックであり、誤分類は不可避。`description` の記述精度が分類精度に直結するため、境界が曖昧なジャンルは境界条件を明示した文章にすること。
- 最終的なジャンルは LLM だけで決まっているわけではない。分類後の決定的な後処理が `crypto` と広義 `qec` の符号理論ラベルを追加することがあり、ログ上ではその追加分とモデル自身の出力を現状区別できない。
- チェックインされている標準設定は意図的に日本語運用のまま。多言語動作は `target_language` と関連設定を変更した場合のみ有効になるため、多言語対応は既存の日本語 Discord ワークフローを変更しない。
- Azure Translator の F0 は月200万文字まで無料なので、Google の前に挟む中間フォールバックとして有用。Azure のみで使う場合は `translators: ["azure"]` とし、`target_language` に `fr`, `de`, `ko`, `zh-Hans` などの Azure 対応言語コードを設定する。
- Google Cloud Translation は多くの言語に対応しており、標準チェーンでは最後のフォールバックとして使う。Google 使用量を抑えるため、DeepL/Azure で翻訳できなかった `other`, `foundations`, `sensing`, `nisq` のみに投稿される論文は英語原文で投稿する。
- `gemini-2.5-pro` は Gemini API の無料枠から外れ(課金を有効にしない限り 429・クォータ0を返すようになった)、そのためデフォルトの Gemini 分類チェーンは `gemini-2.5-flash` → `gemini-2.5-flash-lite` になっている。残ったモデルの無料枠 RPD(1日あたりリクエスト数)クォータもたびたび引き下げられており、`gemini-2.5-flash` が1日あたり約20リクエストしか使えないと報告するアカウントもあるため、導入時は [Google AI Studio](https://aistudio.google.com/) でそのアカウントの現行値を確認すること。モデルごとの circuit breaker と `gemini-2.5-flash-lite` / `gpt-oss-120b` フォールバックにより、実行途中でプライマリモデルのクォータが尽きても分類は継続する。
- `seen_ids.json` の完了ID、未完了のチャンネル別配信状態、外部審査、未審査候補、取得cursorは、再送漏れ・古い論文の再投稿を防ぐため切り捨てない。表示・監査用の `posted_log.json` だけは最新5000件に制限する。
- 隣接カテゴリの completeness が現在カバーするのは、**primary** が `cs.CR`, `cs.CC`, `cs.IT` の論文である。`math.IT` や `cs.DS` など他カテゴリが primary の論文は、quant-ph へ cross-list された場合にのみ拾われる。

---

## 補助: Discord URL 投稿の掃除

`scripts/clean_discord_urls.py` は、指定チャンネルから arXiv URL を含む bot/webhook 投稿を探す補助スクリプト。デフォルトは dry-run で、`--delete` を付けた場合のみ削除する。

```bash
export DISCORD_BOT_TOKEN="実際のDiscord Bot Token"
export DISCORD_CHANNEL_ID="数値のチャンネルID"
python3 scripts/clean_discord_urls.py
python3 scripts/clean_discord_urls.py --delete
```

日本時間の今日の投稿だけを削除する場合:

```bash
python3 scripts/clean_discord_urls.py --today
python3 scripts/clean_discord_urls.py --today --delete
```

複数チャンネルを対象にする場合は、カンマ区切りの環境変数か、`--channel-id` の複数指定を使う:

```bash
export DISCORD_CHANNEL_IDS="111111111111111111,222222222222222222"
python3 scripts/clean_discord_urls.py --today
```

全 bot チャンネルを対象にする場合は、全チャンネルIDを一度 `DISCORD_ALL_CHANNEL_IDS` に入れて `--all-channels` を使う:

```bash
export DISCORD_ALL_CHANNEL_IDS="111111111111111111,222222222222222222,333333333333333333"
python3 scripts/clean_discord_urls.py --all-channels --today
python3 scripts/clean_discord_urls.py --all-channels --today --delete
```

対象チャンネルと日本時間の範囲を指定する場合:

```bash
python3 scripts/clean_discord_urls.py \
  --channel-id 111111111111111111 \
  --channel-id 222222222222222222 \
  --since "2026-07-03T13:55" \
  --until "2026-07-03T16:30"

python3 scripts/clean_discord_urls.py \
  --channel-id 111111111111111111 \
  --channel-id 222222222222222222 \
  --since "2026-07-03T13:55" \
  --until "2026-07-03T16:30" \
  --delete
```

Discord 側を削除した後、同じ日付を bot の state からも消すと、その論文を再投稿できる:

```bash
python3 scripts/rollback_posted_day.py --today
python3 scripts/rollback_posted_day.py --today --write
```

注意: Webhook URL は Bot Token ではないため、このスクリプトには使えない。過去メッセージ削除には、`View Channels`, `Read Message History`, `Manage Messages` を持つ Discord Bot が必要。

---

## 補助: Discord チャンネルを閲覧・リアクション専用にする

`scripts/lock_discord_channels.py` は、指定チャンネルの `@everyone` 権限上書きを更新し、メッセージ送信・スレッド送信を禁止しつつ、リアクション追加は許可する。デフォルトは dry-run で、`--apply` を付けた場合のみ Discord 側を更新する。

Bot には `Manage Channels` 権限が必要。

```bash
export DISCORD_BOT_TOKEN="実際のDiscord Bot Token"
export DISCORD_GUILD_ID="数値のサーバーID"

python3 scripts/lock_discord_channels.py --names \
  fault-tolerant-computation \
  quantum-error-correction-code \
  quantum-machine-learning \
  cryptography-and-security \
  quantum-complexity-theory \
  post-quantum-cryptography \
  quantum-algorithm \
  quantum-simulation \
  nisq-algorithm \
  hardware-and-implementation \
  quantum-sensing \
  quantum-information-theory \
  quantum-foundations \
  others \
  bot-emergency

python3 scripts/lock_discord_channels.py --names \
  fault-tolerant-computation \
  quantum-error-correction-code \
  quantum-machine-learning \
  cryptography-and-security \
  quantum-complexity-theory \
  post-quantum-cryptography \
  quantum-algorithm \
  quantum-simulation \
  nisq-algorithm \
  hardware-and-implementation \
  quantum-sensing \
  quantum-information-theory \
  quantum-foundations \
  others \
  bot-emergency \
  --apply
```

`random` だけ開けたまま全チャンネルをロックする場合:

```bash
python3 scripts/lock_discord_channels.py --all-text-channels --exclude-names random
python3 scripts/lock_discord_channels.py --all-text-channels --exclude-names random --apply
```

注意: `Administrator` 権限を持つユーザーはチャンネル権限の上書きを回避する。Webhook は引き続き自分のチャンネルへ投稿できる。
