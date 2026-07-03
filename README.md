# New arXiv quant-ph paper -> Discord notification bot (with configurable translation)

This bot fetches the official arXiv RSS feed (`rss.arxiv.org/rss/quant-ph`) three times per weekday, classifies each paper into one of 15 genres, and posts it to the corresponding Discord channel through webhooks with a translated title and abstract.
In the current standard setup, Gemini is used for **classification only**, while translation is attempted through DeepL -> Azure Translator -> Google Cloud Translation. Because Gemini only returns genre IDs, this setup uses less API quota than asking Gemini to translate as well. The bot uses **only the Python standard library**; `pip install` is not required.

The default translation target is Japanese (`target_language: "ja"`), but it can be changed by editing `target_language` in `config.json`. If you choose a language that DeepL does not support, Azure and Google can still handle many target languages; set `translators` to `["azure", "google"]` or use `deepl_target_language` / `azure_target_language` / `google_target_language` for backend-specific language codes.

The checked-in `config.json` remains configured for the original Japanese Discord workflow: `target_language: "ja"`, `target_language_name: "Japanese"`, `translated_title_label: "邦題"`, `translators: ["deepl", "azure", "google"]`, and `require_translation: true`.

---

## File layout

| File | Role |
| --- | --- |
| `arxiv_bot.py` | Main bot. Uses only the Python standard library |
| `config.json` | All configuration: feeds, genre definitions, API behavior, classification parameters |
| `seen_ids.json` | Posted arXiv ID state. Automatically committed by Actions, capped at 3000 IDs |
| `posted_log.json` | Metadata log for posted papers. JSON array, capped at 5000 entries |
| `scirate_weekly.py` | Weekend bot that reposts popular weekly quant-ph papers from SciRate into the normal genre channels |
| `scirate_weekly_state.json` | Posted arXiv ID state for the SciRate weekend bot |
| `test_feed.xml` | Sample RSS feed for local testing |
| `scripts/clean_discord_urls.py` | Helper script to find or delete arXiv URL posts in Discord channels |
| `.github/workflows/notify.yml` | GitHub Actions schedule and secret references for the main notifier |
| `.github/workflows/scirate_weekly.yml` | GitHub Actions schedule for the SciRate weekend digest |

---

## Run schedule

GitHub Actions runs the main notifier **three times per weekday** (Monday-Friday UTC = Tuesday-Saturday JST).

| UTC | JST | Purpose |
| --- | --- | --- |
| 01:05 | 10:05 | Catch new papers soon after the arXiv announcement at around 00:00 UTC |
| 04:00 | 13:00 | Cover missed or delayed items |
| 07:00 | 16:00 | Same as above |

`seen_ids.json` prevents duplicate posting, so the same paper is not posted multiple times across runs.

On weekends, a separate workflow posts a SciRate weekly popular-paper digest.

| UTC | JST | Purpose |
| --- | --- | --- |
| Sunday 00:30 | Sunday 09:30 | Post popular quant-ph papers from the last 7 days on SciRate |

---

## Processing flow

```text
Fetch RSS -> filter -> genre classification + translation -> Discord post -> save state
```

### 1. Fetch RSS

The bot fetches RSS feeds for the categories listed in `config.json` under `feeds` (for example, `"quant-ph"`) and deduplicates papers by ID. If the same paper appears in multiple feeds, the later entry overwrites the earlier one.

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

Even for cross-listed papers that remain eligible for posting, normal genre classification is limited by `cross_classify_primary_as_quantph`. Currently, only `quant-ph` and `cs.CR` are treated as quant-ph-equivalent.

- primary is `quant-ph`: normal classification
- primary is `cs.CR`: normal classification, to catch blind/verifiable/secure/delegation topics and PQC
- primary is anything else: forced to `other`, regardless of Gemini or TF-IDF output

### 3. Genre classification + translation (two paths)

**Primary path: Gemini classify-only**

When `classify_with_llm: true` (default) and `GEMINI_API_KEY` is available, the bot sends titles and abstracts to Gemini in batches of `translate_batch_size` entries (default: 5) and asks Gemini to return only genre IDs.

- The prompt includes the full natural-language `description` for every genre, so papers can be classified by meaning even when they do not contain fixed keywords
- Output format: `<<<k|genre_id>>>` or `<<<k|id1,id2>>>` for multi-label classification
- One paper can be assigned to multiple genres; see "Multi-label classification" below
- Cross-listed papers whose primary category is not `quant-ph` or `cs.CR` are overwritten to `other` after Gemini classification

**Fallback path: TF-IDF cosine similarity**

Used when Gemini is unavailable due to quota exhaustion or similar failures, or when Gemini does not return a result for an individual entry.

- Vectorizes each genre's `description` + `keywords` text with TF-IDF
- Computes cosine similarity against the paper's `title + abstract`
- Applies arXiv category hints from `category_genre_hints` (+0.15 to the target genre) and forced `other` handling from `category_other_overrides` (+1.0 to `other`)
- Words that appear in every genre, such as "quantum", get IDF=0 and do not affect the score

Translation on the fallback path uses the backends listed in `translators`. The current standard order is DeepL -> Azure Translator -> Google Cloud Translation.

### 4. Multi-label classification

One paper can be classified into multiple genres and posted to each corresponding channel.

- `classify_max_genres` (default: 2): maximum number of genres assigned to one paper
- `classify_secondary_ratio` (default: 0.7, TF-IDF fallback only): secondary genres are accepted only when their score is at least 70% of the top genre score, preventing weak accidental matches from causing multi-channel posts
- On the Gemini path, the prompt instructs the LLM to return multiple IDs only when the paper genuinely spans multiple genres
- `force_genre_keywords`: adds a configured genre when specified words appear in the title or abstract
- Duplicate posts to the same webhook are removed with the `posted_webhooks` set

### 5. Translation fallback chain

Current standard setting:

```text
DeepL -> Azure Translator -> Google Cloud Translation
```

- Backends are tried in order; once one succeeds, the bot moves to the next paper
- For papers whose abstract translation succeeds, the same translation chain also creates a translated title separately from the English title
- Backends where quota exhaustion is detected (Gemini: persistent 429, DeepL: 456, Google: 403/429) are skipped for the rest of that run (**circuit breaker**)
- If DeepL and Azure fail, Google is used only for papers outside `google_skip_translation_genres`. Papers that belong only to those skipped genres are posted in English instead of being deferred.
- If every allowed backend fails and `require_translation: true` (default), the paper is not posted and is retried on the next run

### 6. Discord posting

For each paper, the bot posts once for each assigned genre. It waits 1.2 seconds between posts to leave headroom for Discord webhook rate limits.

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
  "abstract_en": "...",
  "abstract_ja": "...",
  "abstract_translated": "..."
}
```

`title_translated`, `abstract_translated`, and `translation_language` are the language-neutral fields. `title_ja` and `abstract_ja` are still written for backward compatibility with older logs and the existing Japanese workflow.

---

## SciRate weekend digest

The weekend workflow `.github/workflows/scirate_weekly.yml` fetches `https://scirate.com/arxiv/quant-ph?range=7` and targets only papers whose `Scite!` count is at least `scirate_min_scites`. The default threshold is 30.

Unlike the normal new-paper notifier, the SciRate weekend digest deduplicates with `scirate_weekly_state.json` instead of `seen_ids.json`. This allows papers that were already posted on weekdays to be reposted on the weekend as popular papers.

It uses the same genres and webhooks as the normal notifier.

- If `posted_log.json` already has classification history for the same arXiv ID, saved `genre_ids` are reused
- If no classification history exists, Gemini classify-only is used
- If Gemini is unavailable, the bot falls back to TF-IDF
- If translated `title_translated` / `abstract_translated` values exist in `posted_log.json` for the same `translation_language`, they are reused
- If no translation exists, the same DeepL -> Azure Translator -> Google Cloud Translation chain is used

SciRate posts add a `SciRate` field to the normal embed and show the number of Scites from the last 7 days.

Local check:

```bash
python3 scirate_weekly.py --dry-run
```

Some environments receive HTTP 403 when accessing SciRate directly. In that case, the script prints a warning and exits without posting to Discord or updating state.

---

## Genre list (15 genres)

| ID | Name | Main topics |
| --- | --- | --- |
| `qec` | 誤り訂正・符号理論 | Stabilizer codes, surface codes, LDPC, decoder design |
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

If none of the genres match, the paper is sent to `DISCORD_WEBHOOK_GENERAL` as a fallback.

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

For genre-specific routing, create one webhook per channel and store each URL in the matching `DISCORD_WEBHOOK_*` secret. Keep webhook URLs and API keys out of committed files.

### 2. Get API keys

Translation backends are tried in the order listed in `config.json` under `translators`. The current standard setting is `["deepl", "azure", "google"]`. **Unregistered backends are skipped automatically.**

| Backend | Purpose | Free tier | Secret name |
| --- | --- | --- | --- |
| Gemini | Classification only | Free tier available, no card required | `GEMINI_API_KEY` |
| DeepL | Translation | Free up to 500k characters/month | `DEEPL_API_KEY` |
| Azure Translator | Translation | Free up to 2M characters/month on F0 | `AZURE_TRANSLATOR_KEY` + `AZURE_TRANSLATOR_REGION` |
| Google Cloud Translation | Translation | Free up to 500k characters/month (**billing account required**) | `GOOGLE_TRANSLATE_API_KEY` |

You can create a Gemini API key in [Google AI Studio](https://aistudio.google.com/). If no Gemini key is available, classification falls back to TF-IDF.

### 3. Register GitHub Secrets

Register the following under `Settings -> Secrets and variables -> Actions -> New repository secret`.

**Webhooks (all 15 genres + general)**

```text
DISCORD_WEBHOOK_GENERAL
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

**API keys (only the ones you use)**

```text
GEMINI_API_KEY
DEEPL_API_KEY            # optional
AZURE_TRANSLATOR_KEY     # optional
AZURE_TRANSLATOR_REGION  # optional unless your Azure resource requires it
GOOGLE_TRANSLATE_API_KEY # optional
```

Posting to genres whose Secret is missing is skipped automatically.

### 4. Test the workflow

Open the Actions tab -> `workflow_dispatch` -> "Run workflow" to run it manually.

---

## Local checks

### dry-run mode (recommended, no API usage)

This mode does not call Discord or any translation API. It prints only TF-IDF classification results to stdout.

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

This reads a local RSS file and exercises the full path, including translation and Discord posting.

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
| `id` | yes | Alphanumeric characters and underscores only. Must be unique. Also used as Gemini's output ID |
| `name` | yes | Genre name shown in the Discord embed. The default config uses Japanese names |
| `description` | yes | **Decision basis for Gemini classification**. Detailed descriptions with clear boundaries against other genres improve classification accuracy |
| `webhook_env` | yes | Environment variable name registered as a Secret, for example `"DISCORD_WEBHOOK_QEC"` |
| `keywords` | yes | Word list used by the TF-IDF fallback |

When adding a genre, also register the corresponding Discord channel webhook as a Secret and add it to the `env:` section in `.github/workflows/notify.yml`.

### Classification parameters

Classification-related settings in `config.json`:

| Key | Default | Description |
| --- | --- | --- |
| `classify_with_llm` | `true` | Use TF-IDF fallback every time when set to `false` |
| `classify_min_score` | `0.05` | Minimum TF-IDF score to accept |
| `classify_max_genres` | `2` | Maximum number of genres assigned to one paper |
| `classify_secondary_ratio` | `0.7` | Minimum score ratio for secondary genres when using TF-IDF |
| `force_genre_keywords` | `{}` | Add the target genre when specified words appear in the title or abstract |

The run log prints Gemini usage. Example:

```text
[info] Gemini usage: mode=classify-only, model=gemini-2.5-flash-lite, requests=17, classified=82/82, tfidf_fallback=0, disabled_for_run=False
```

### arXiv category hints

Settings that help classify cross-listed papers from their primary category:

- `cross_classify_primary_as_quantph`: only papers whose primary category is in this list are normally classified. In the default setup, only `quant-ph` and `cs.CR` are included. Papers cross-listed into quant-ph from other primary categories are classified as `other`
- `category_genre_hints`: category -> genre ID mapping. Matching papers receive +0.15 to the target genre score
- `category_other_overrides`: additional primary categories to explicitly treat as `other`

### Forced crypto keywords

If a word listed in `force_genre_keywords.crypto` appears in the title or abstract, `crypto` is added to the Gemini/TF-IDF result. The current list includes terms such as the following to avoid missing topics around verifiable quantum computation, blind quantum computation, secure quantum computation, and delegated quantum computation.

```text
blind, verifiable, secure, delegated quantum computation,
secure delegation, blind delegation, verifiable delegation,
untrusted server, malicious server, client-server
```

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
| `target_language_name` | `"Japanese"` | Human-readable target language name used in Gemini translation prompts |
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
| `translation_priority_genres` | `["ft","qec","complexity","qml","crypto","pqc","network","algo","sim","nisq","hardware","sensing","qit","foundations","other"]` | Genre priority used after classification when choosing translation/posting order |
| `translate_only_matched` | `false` | When `true`, papers with no classified genre are not translated, saving API usage |
| `google_skip_translation_genres` | `["other","foundations","sensing","nisq"]` | When only Google remains, papers whose genres are all in this list are posted in English to save Google quota |
| `require_translation` | `true` | `true`: papers whose translation failed are retried later / `false`: post in English |
| `show_translated_title` | `true` | Show the translated title at the beginning of the Discord embed body |
| `show_original_abstract` | `false` | Include the English abstract in addition to the translated abstract |
| `include_replacements` | `false` | Post replacement papers when set to `true` |
| `scirate_range_days` | `7` | Date range used by the SciRate weekend digest |
| `scirate_min_scites` | `30` | Minimum Scite count for the SciRate weekend digest |
| `gemini_model` | `"gemini-2.5-flash-lite"` | Gemini model ID used for classification |
| `gemini_min_interval_sec` | `7` | Minimum interval between Gemini requests, in seconds |
| `gemini_max_retries` | `4` | Maximum retries for temporary errors |
| `gemini_overload_giveup` | `2` | Open the circuit breaker after this many consecutive overload errors |

---

## Notes

- The bot treats the first RSS `<category>` element as the primary category. This is a heuristic from observed RSS behavior, not an arXiv API guarantee.
- Genre classification is heuristic, using Gemini as the primary path and TF-IDF as fallback, so misclassification is unavoidable. The quality of `description` directly affects Gemini classification accuracy; for genres with fuzzy boundaries, write explicit boundary conditions.
- The default checked-in configuration is intentionally Japanese. Multilingual behavior is opt-in through `target_language` and related settings, so changing the code does not change the default Japanese Discord workflow.
- Azure Translator's F0 tier includes 2M free characters/month, which makes it a useful middle fallback before Google. For an Azure-only setup, use `translators: ["azure"]` and set `target_language` to an Azure-supported language code such as `fr`, `de`, `ko`, or `zh-Hans`.
- Google Cloud Translation supports many target languages and remains the final fallback in the default chain. To reduce Google usage, papers posted only to `other`, `foundations`, `sensing`, and `nisq` are posted in English when DeepL/Azure cannot translate them first.
- Gemini free-tier RPD (requests per day) limits may change, so check the current values in [Google AI Studio](https://aistudio.google.com/) when setting it up.
- `seen_ids.json` keeps the latest 3000 IDs and `posted_log.json` keeps the latest 5000 entries. Older entries are truncated automatically.

---

## Helper: cleaning Discord URL posts

`scripts/clean_discord_urls.py` is a helper script that finds bot/webhook posts containing arXiv URLs in a specified channel. It is dry-run by default, and deletes messages only when `--delete` is passed.

```bash
export DISCORD_BOT_TOKEN="actual Discord Bot Token"
export DISCORD_CHANNEL_ID="numeric channel ID"
python3 scripts/clean_discord_urls.py
python3 scripts/clean_discord_urls.py --delete
```

Note: a webhook URL is not a Bot Token and cannot be used with this script. Deleting old messages requires a Discord Bot with `View Channels`, `Read Message History`, and `Manage Messages` permissions.

---

# 日本語

# arXiv quant-ph → Discord 通知 bot(翻訳付き)

arXiv の公式 RSS フィード (`rss.arxiv.org/rss/quant-ph`) を平日1日3回取得し、論文を15ジャンルのいずれかに分類して、翻訳済みタイトル・abstract 訳とともに Discord の各チャンネルへ Webhook で投稿する。
現在の標準運用では、Gemini は**分類のみ**に使い、翻訳は DeepL → Azure Translator → Google Cloud Translation の順に試行する。Gemini の出力はジャンル ID だけなので、翻訳まで Gemini に任せる構成より API 消費を抑えやすい。**標準ライブラリのみで動作し、`pip install` は不要。**

デフォルトの翻訳先は日本語(`target_language: "ja"`)だが、`config.json` の `target_language` を変更すれば他言語へ翻訳できる。DeepL は Azure / Google より対応言語が少ないため、DeepL 非対応言語を使う場合は `translators` を `["azure", "google"]` にするか、`deepl_target_language` / `azure_target_language` / `google_target_language` でバックエンドごとの言語コードを指定する。

このリポジトリに含まれる `config.json` は、従来の日本語 Discord 運用のままになっている。具体的には `target_language: "ja"`, `target_language_name: "Japanese"`, `translated_title_label: "邦題"`, `translators: ["deepl", "azure", "google"]`, `require_translation: true`。

---

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| `arxiv_bot.py` | 本体。標準ライブラリのみ使用 |
| `config.json` | 全設定(フィード、ジャンル定義、API挙動、分類パラメータ) |
| `seen_ids.json` | 投稿済み arXiv ID の記録(Actions が自動 commit、最大3000件) |
| `posted_log.json` | 投稿済み論文のメタデータログ(最大5000件、JSON 配列) |
| `scirate_weekly.py` | SciRate の週間人気 quant-ph 論文を通常ジャンルへ再投稿する週末用 bot |
| `scirate_weekly_state.json` | SciRate 週末投稿済み arXiv ID の記録 |
| `test_feed.xml` | ローカルテスト用のサンプル RSS |
| `scripts/clean_discord_urls.py` | Discord チャンネル内の arXiv URL 投稿を検索・削除する補助スクリプト |
| `.github/workflows/notify.yml` | 実行スケジュールと Secret 参照の定義 |
| `.github/workflows/scirate_weekly.yml` | SciRate 週末ダイジェストの実行スケジュール |

---

## 実行スケジュール

GitHub Actions により**平日(月〜金 UTC = 火〜土 JST)に1日3回**自動実行される。

| UTC | JST | 目的 |
| --- | --- | --- |
| 01:05 | 10:05 | arXiv アナウンス直後(00:00 UTC 頃)の新着を捕捉 |
| 04:00 | 13:00 | 取りこぼし・遅延の補完 |
| 07:00 | 16:00 | 同上 |

`seen_ids.json` による重複排除があるため、同一論文が複数回実行で投稿されることはない。

週末には SciRate の週間人気論文も別 workflow で投稿する。

| UTC | JST | 目的 |
| --- | --- | --- |
| 日曜 00:30 | 日曜 09:30 | SciRate の直近7日 quant-ph 人気論文を補完 |

---

## 処理フロー

```
RSS 取得 → フィルタリング → ジャンル分類 + 翻訳 → Discord 投稿 → 状態保存
```

### 1. RSS 取得

`config.json` の `feeds` に列挙したカテゴリ(`"quant-ph"` 等)の RSS を順に取得し、論文を ID でまとめる。複数フィードで同一論文が登場した場合は後から来た方が上書きされる(重複排除)。

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

投稿対象に残った cross-list 論文でも、通常のジャンル分類を行う primary は `cross_classify_primary_as_quantph` で制限する。現在は `quant-ph` と `cs.CR` のみを quant-ph と同列に扱う。

- primary が `quant-ph`: 通常分類
- primary が `cs.CR`: 通常分類。blind/verifiable/secure/delegation 系や PQC を拾うため、quant-ph と同列に扱う
- primary がそれ以外: Gemini/TF-IDF の結果に関係なく `other` に分類

### 3. ジャンル分類 + 翻訳(2段構え)

**主経路: Gemini classify-only**

`classify_with_llm: true`(デフォルト)かつ `GEMINI_API_KEY` がある場合、タイトルと abstract を `translate_batch_size`(デフォルト5)件ずつ Gemini に一括送信し、ジャンル ID だけを返させる。

- プロンプトには各ジャンルの `description`(自然言語の定義文)を全文渡すため、定型キーワードを含まない論文も内容で分類される
- 出力形式: `<<<k|genre_id>>>` または `<<<k|id1,id2>>>` (マルチラベルの場合)
- 1論文に複数ジャンルを割り当てられる(詳細は後述の「マルチラベル分類」を参照)
- primary が `quant-ph` / `cs.CR` 以外の cross-list 論文は、Gemini の分類後に `other` へ上書きされる

**フォールバック経路: TF-IDF コサイン類似度**

Gemini がクォータ枯渇等で利用不可の場合、または個別エントリを Gemini が返さなかった場合に使用する。

- ジャンルの `description` + `keywords` テキストを TF-IDF ベクトル化
- 論文の `title + abstract` との余弦類似度を各ジャンルで計算
- `category_genre_hints` による arXiv カテゴリヒント(スコアに +0.15)と `category_other_overrides` による強制 other 判定(スコアに +1.0)を適用
- 「量子(quantum)」のような全ジャンルに出現する語は IDF=0 になり、スコアに寄与しない

フォールバック経路での翻訳は、`translators` に設定された順に処理される。現在の標準設定は DeepL → Azure Translator → Google Cloud Translation。

### 4. マルチラベル分類

1論文を複数ジャンルに分類し、それぞれのチャンネルへ投稿できる。

- `classify_max_genres`(デフォルト2): 1論文に割り当てる最大ジャンル数
- `classify_secondary_ratio`(デフォルト0.7, TF-IDF フォールバック時のみ適用): 2番目以降のジャンルを採用するのは、そのスコアが最上位ジャンルのスコアの70%以上の場合のみ。弱い偶発的マッチで多チャンネルに投稿されることを防ぐ
- Gemini 経路では、LLM が複数ジャンルにまたがると判断した場合のみ複数 ID を返すよう指示している
- `force_genre_keywords`: タイトル/abstract に指定語が含まれる場合、LLM/TF-IDF の結果に指定ジャンルを追加する
- 同一 Webhook への重複投稿は `posted_webhooks` セットで排除される

### 5. 翻訳フォールバックチェーン

現在の標準設定:

```
DeepL → Azure Translator → Google Cloud Translation
```

- 先頭から順に試行し、成功した時点で次の論文へ移る
- abstract の翻訳に成功した投稿対象論文について、英語タイトルとは別に翻訳済みタイトルも同じ翻訳チェーンで作成する
- クォータ枯渇を検知したバックエンド(Gemini: 持続的 429、DeepL: 456、Google: 403/429)はその実行回では以後スキップされる(**circuit breaker**)
- DeepL と Azure が失敗した場合、Google は `google_skip_translation_genres` の対象外論文にだけ使う。対象ジャンルだけに属する論文は、持ち越さず英語原文で投稿する。
- 許可された全段で翻訳できなかった論文は `require_translation: true`(デフォルト)の場合は投稿せず次回に持ち越す

### 6. Discord 投稿

論文ごとに分類されたジャンル数分の投稿を行う。各投稿間隔は1.2秒(Discord レート制限対策)。

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
  "abstract_en": "...",
  "abstract_ja": "...",
  "abstract_translated": "..."
}
```

`title_translated`, `abstract_translated`, `translation_language` は多言語対応用の汎用フィールド。`title_ja` と `abstract_ja` は、既存の日本語ログや従来運用との互換性のために引き続き保存される。

---

## SciRate 週末ダイジェスト

週末用の `.github/workflows/scirate_weekly.yml` は、`https://scirate.com/arxiv/quant-ph?range=7` を取得し、`Scite!` 数が `scirate_min_scites` 以上の論文だけを対象にする。デフォルトは30以上。

通常の新着通知とは違い、SciRate 週末ダイジェストは `seen_ids.json` ではなく `scirate_weekly_state.json` で重複排除する。これにより、平日にすでに投稿済みの論文でも、週末に「人気論文」として再掲できる。

分類は通常通知と同じジャンル・Webhookを使う。

- `posted_log.json` に同じ arXiv ID の分類履歴がある場合は、保存済みの `genre_ids` を再利用する
- 分類履歴がない場合は Gemini classify-only を使う
- Gemini が使えない場合は TF-IDF にフォールバックする
- 同じ `translation_language` の `title_translated` / `abstract_translated` が `posted_log.json` にあれば再利用する
- 未翻訳の場合は通常通知と同じ DeepL → Azure Translator → Google Cloud Translation チェーンで翻訳する

SciRate投稿には通常の embed に加えて `SciRate` フィールドが付き、直近7日での Scite 数を表示する。

ローカル確認:

```bash
python3 scirate_weekly.py --dry-run
```

SciRate が直接取得できない環境では HTTP 403 になることがある。その場合は警告を出して終了し、Discord投稿や状態更新は行わない。

---

## ジャンル一覧(15種)

| ID | 名称 | 主なトピック |
| --- | --- | --- |
| `qec` | 誤り訂正・符号理論 | 安定化符号・表面符号・LDPC・デコーダ設計 |
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

上記のどのジャンルにも該当しない場合は `DISCORD_WEBHOOK_GENERAL` へ送られる(フォールバック)。

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

ジャンル別に振り分けたい場合は、チャンネルごとに Webhook を作り、対応する `DISCORD_WEBHOOK_*` secret に URL を入れる。Webhook URL や API キーは repository に commit しないこと。

### 2. API キーの取得

`config.json` の `translators` に列挙した順に試行される。現在の標準設定は `["deepl", "azure", "google"]`。**未登録のバックエンドは自動スキップされる。**

| バックエンド | 用途 | 無料枠 | Secret 名 |
| --- | --- | --- | --- |
| Gemini | 分類のみ | 無料枠あり(カード登録不要) | `GEMINI_API_KEY` |
| DeepL | 翻訳 | 月50万文字まで無料 | `DEEPL_API_KEY` |
| Azure Translator | 翻訳 | F0 で月200万文字まで無料 | `AZURE_TRANSLATOR_KEY` + `AZURE_TRANSLATOR_REGION` |
| Google Cloud Translation | 翻訳 | 月50万文字まで無料(**請求先アカウント必須**) | `GOOGLE_TRANSLATE_API_KEY` |

Gemini API キーは [Google AI Studio](https://aistudio.google.com/) で発行できる。Gemini キーがない場合は TF-IDF 分類にフォールバックする。

### 3. GitHub Secrets の登録

`Settings → Secrets and variables → Actions → New repository secret` に以下を登録する。

**Webhook (全15ジャンル + general)**

```
DISCORD_WEBHOOK_GENERAL
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

**API キー(使うもののみ)**

```
GEMINI_API_KEY
DEEPL_API_KEY            # 省略可
AZURE_TRANSLATOR_KEY     # 省略可
AZURE_TRANSLATOR_REGION  # Azure resource が要求する場合に設定
GOOGLE_TRANSLATE_API_KEY # 省略可
```

Secret が未設定のジャンルへの投稿は自動的にスキップされる。

### 4. 動作確認

Actions タブ → `workflow_dispatch` → 「Run workflow」で手動実行する。

---

## ローカル動作確認

### dry-run モード(推奨・API不使用)

Discord や翻訳 API を一切呼び出さず、TF-IDF による分類結果だけを標準出力に表示する。

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

ローカルの RSS ファイルを読み込み、翻訳・Discord 投稿まで含む全経路を確認できる。

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
| `id` | ○ | 英数字・アンダースコアのみ。重複不可。Gemini の出力 ID としても使われる |
| `name` | ○ | Discord embed に表示されるジャンル名。標準設定では日本語名称 |
| `description` | ○ | **Gemini 分類の判定根拠**。詳細かつ他ジャンルとの境界を明示する文が分類精度を高める |
| `webhook_env` | ○ | Secret に登録した環境変数名(例: `"DISCORD_WEBHOOK_QEC"`) |
| `keywords` | ○ | TF-IDF フォールバック時に使用する語のリスト |

ジャンルを追加した場合は対応する Discord チャンネルの Webhook を Secret に登録し、`.github/workflows/notify.yml` の `env:` セクションにも追記すること。

### 分類パラメータ

`config.json` で調整できる分類関連の設定:

| キー | デフォルト | 説明 |
| --- | --- | --- |
| `classify_with_llm` | `true` | `false` にすると常に TF-IDF フォールバックを使用 |
| `classify_min_score` | `0.05` | TF-IDF スコアの採用下限 |
| `classify_max_genres` | `2` | 1論文に割り当てる最大ジャンル数 |
| `classify_secondary_ratio` | `0.7` | TF-IDF 使用時、2番目以降のジャンルを採用するための最低スコア比率 |
| `force_genre_keywords` | `{}` | 指定語がタイトル/abstractに出た場合、分類結果へ該当ジャンルを追加 |

実行ログには Gemini の利用状況が出力される。例:

```text
[info] Gemini usage: mode=classify-only, model=gemini-2.5-flash-lite, requests=17, classified=82/82, tfidf_fallback=0, disabled_for_run=False
```

### arXiv カテゴリヒント

cross-list 論文の primary カテゴリから分類を補助する設定。

- `cross_classify_primary_as_quantph`: primary がこのリストにある論文だけ通常分類する。デフォルト運用では `quant-ph` と `cs.CR` のみ。その他の primary から quant-ph へ cross-list された論文は `other` に分類される
- `category_genre_hints`: カテゴリ → ジャンル ID のマッピング。該当カテゴリの論文は指定ジャンルのスコアが +0.15 される
- `category_other_overrides`: 追加で明示的に `other` 扱いしたい primary カテゴリ

### crypto 強制キーワード

`force_genre_keywords.crypto` に含まれる語がタイトルまたは abstract に出た場合、Gemini/TF-IDF の結果に `crypto` を追加する。現在は、verifiable quantum computation / blind quantum computation / secure quantum computation / delegated quantum computation 周辺の取り漏らしを避けるため、以下のような語を入れている。

```text
blind, verifiable, secure, delegated quantum computation,
secure delegation, blind delegation, verifiable delegation,
untrusted server, malicious server, client-server
```

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
| `target_language_name` | `"Japanese"` | Gemini 翻訳プロンプトで使う翻訳先言語名 |
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
| `translation_priority_genres` | `["ft","qec","complexity","qml","crypto","pqc","network","algo","sim","nisq","hardware","sensing","qit","foundations","other"]` | 分類後に翻訳・投稿するジャンル優先順 |
| `translate_only_matched` | `false` | `true` にするとジャンル未分類論文は翻訳しない(API節約) |
| `google_skip_translation_genres` | `["other","foundations","sensing","nisq"]` | Google だけが残った場合、このリスト内のジャンルだけに属する論文は英語原文で投稿して Google quota を節約する |
| `require_translation` | `true` | `true`: 翻訳失敗論文は次回へ持ち越す / `false`: 英語のまま投稿 |
| `show_translated_title` | `true` | `true` にすると Discord embed 本文の先頭に翻訳済みタイトルを表示する |
| `show_original_abstract` | `false` | `true` にすると翻訳文に加えて英語 abstract も embed に含める |
| `include_replacements` | `false` | `true` にすると差替え論文(replace)も投稿する |
| `scirate_range_days` | `7` | SciRate 週末ダイジェストで見る期間 |
| `scirate_min_scites` | `30` | SciRate 週末ダイジェストで投稿対象にする最低 Scite 数 |
| `gemini_model` | `"gemini-2.5-flash-lite"` | 分類に使用する Gemini モデル ID |
| `gemini_min_interval_sec` | `7` | Gemini リクエスト間の最小間隔(秒) |
| `gemini_max_retries` | `4` | 一時的エラー時のリトライ上限回数 |
| `gemini_overload_giveup` | `2` | 過負荷エラーが連続したら circuit breaker を開く閾値 |

---

## 留意事項

- RSS の `<category>` 要素の先頭を primary カテゴリとみなすヒューリスティックを使用している(arXiv API の保証ではなく経験則)。
- ジャンル分類は Gemini(主)と TF-IDF(フォールバック)によるヒューリスティックであり、誤分類は不可避。`description` の記述精度が Gemini 分類の精度に直結するため、境界が曖昧なジャンルは境界条件を明示した文章にすること。
- チェックインされている標準設定は意図的に日本語運用のまま。多言語動作は `target_language` と関連設定を変更した場合のみ有効になるため、今回の多言語対応は既存の日本語 Discord ワークフローを変更しない。
- Azure Translator の F0 は月200万文字まで無料なので、Google の前に挟む中間フォールバックとして有用。Azure のみで使う場合は `translators: ["azure"]` とし、`target_language` に `fr`, `de`, `ko`, `zh-Hans` などの Azure 対応言語コードを設定する。
- Google Cloud Translation は多くの言語に対応しており、標準チェーンでは最後のフォールバックとして使う。Google 使用量を抑えるため、DeepL/Azure で翻訳できなかった `other`, `foundations`, `sensing`, `nisq` のみに投稿される論文は英語原文で投稿する。
- Gemini 無料枠の RPD(1日あたりリクエスト数)制限は変更される可能性があるため、導入時に [Google AI Studio](https://aistudio.google.com/) で現行値を確認すること。
- `seen_ids.json` は最新3000件、`posted_log.json` は最新5000件を保持し、それ以前のエントリは自動的に切り捨てられる。

---

## 補助: Discord URL 投稿の掃除

`scripts/clean_discord_urls.py` は、指定チャンネルから arXiv URL を含む bot/webhook 投稿を探す補助スクリプト。デフォルトは dry-run で、`--delete` を付けた場合のみ削除する。

```bash
export DISCORD_BOT_TOKEN="実際のDiscord Bot Token"
export DISCORD_CHANNEL_ID="数値のチャンネルID"
python3 scripts/clean_discord_urls.py
python3 scripts/clean_discord_urls.py --delete
```

注意: Webhook URL は Bot Token ではないため、このスクリプトには使えない。過去メッセージ削除には、`View Channels`, `Read Message History`, `Manage Messages` を持つ Discord Bot が必要。
