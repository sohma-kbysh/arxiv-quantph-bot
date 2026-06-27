# arXiv quant-ph → Discord 通知 bot(日本語訳付き)

arXiv の公式 RSS フィード (`rss.arxiv.org/rss/quant-ph`) を平日1日3回取得し、論文を15ジャンルのいずれかに分類して、日本語訳 abstract とともに Discord の各チャンネルへ Webhook で投稿する。
Gemini API を用いて翻訳と分類を1リクエストで同時に行うため、API 消費量は最小限。**標準ライブラリのみで動作し、`pip install` は不要。**

---

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| `arxiv_bot.py` | 本体。標準ライブラリのみ使用 |
| `config.json` | 全設定(フィード、ジャンル定義、API挙動、分類パラメータ) |
| `seen_ids.json` | 投稿済み arXiv ID の記録(Actions が自動 commit、最大3000件) |
| `posted_log.json` | 投稿済み論文のメタデータログ(最大5000件、JSON 配列) |
| `test_feed.xml` | ローカルテスト用のサンプル RSS |
| `.github/workflows/notify.yml` | 実行スケジュールと Secret 参照の定義 |

---

## 実行スケジュール

GitHub Actions により**平日(月〜金 UTC = 火〜土 JST)に1日3回**自動実行される。

| UTC | JST | 目的 |
| --- | --- | --- |
| 01:05 | 10:05 | arXiv アナウンス直後(00:00 UTC 頃)の新着を捕捉 |
| 04:00 | 13:00 | 取りこぼし・遅延の補完 |
| 07:00 | 16:00 | 同上 |

`seen_ids.json` による重複排除があるため、同一論文が複数回実行で投稿されることはない。

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

**cross-list ポリシー(デフォルト: 全通過)**

primary カテゴリが `cross_deny_primary` リストに一致する場合のみ除外する。このリストはデフォルトで空(`[]`)のため、`hep-*`, `gr-qc`, `cond-mat.*` を含む**全 cross-list 論文が通過**する。除外したいカテゴリがあれば `cross_deny_primary` へ追加すること。

`cross_allow_primary` はホワイトリストであり、deny リストとの一致より優先される(deny 側に追加した上で例外を設けたいケース向け)。

### 3. ジャンル分類 + 翻訳(2段構え)

**主経路: Gemini 一括リクエスト**

`classify_with_llm: true`(デフォルト)かつ `translators` の先頭が `"gemini"` の場合、タイトルと abstract を `translate_batch_size`(デフォルト5)件ずつ Gemini に一括送信し、翻訳と分類を**1リクエスト**で完結させる。

- プロンプトには各ジャンルの `description`(自然言語の定義文)を全文渡すため、定型キーワードを含まない論文も内容で分類される
- 出力形式: `<<<k|genre_id>>>` または `<<<k|id1,id2>>>` (マルチラベルの場合)
- 1論文に複数ジャンルを割り当てられる(詳細は後述の「マルチラベル分類」を参照)

**フォールバック経路: TF-IDF コサイン類似度**

Gemini がクォータ枯渇等で利用不可の場合、または個別エントリを Gemini が返さなかった場合に使用する。

- ジャンルの `description` + `keywords` テキストを TF-IDF ベクトル化
- 論文の `title + abstract` との余弦類似度を各ジャンルで計算
- `category_genre_hints` による arXiv カテゴリヒント(スコアに +0.15)と `category_other_overrides` による強制 other 判定(スコアに +1.0)を適用
- 「量子(quantum)」のような全ジャンルに出現する語は IDF=0 になり、スコアに寄与しない

フォールバック経路での翻訳は、Gemini → DeepL → Google Cloud Translation の順にフォールバックチェーンで処理される。

### 4. マルチラベル分類

1論文を複数ジャンルに分類し、それぞれのチャンネルへ投稿できる。

- `classify_max_genres`(デフォルト2): 1論文に割り当てる最大ジャンル数
- `classify_secondary_ratio`(デフォルト0.7, TF-IDF フォールバック時のみ適用): 2番目以降のジャンルを採用するのは、そのスコアが最上位ジャンルのスコアの70%以上の場合のみ。弱い偶発的マッチで多チャンネルに投稿されることを防ぐ
- Gemini 経路では、LLM が複数ジャンルにまたがると判断した場合のみ複数 ID を返すよう指示している
- 同一 Webhook への重複投稿は `posted_webhooks` セットで排除される

### 5. 翻訳フォールバックチェーン

```
Gemini → DeepL → Google Cloud Translation
```

- 先頭から順に試行し、成功した時点で次の論文へ移る
- クォータ枯渇を検知したバックエンド(Gemini: 持続的 429、DeepL: 456、Google: 403/429)はその実行回では以後スキップされる(**circuit breaker**)
- 全段で翻訳できなかった論文は `require_translation: true`(デフォルト)の場合は投稿せず次回に持ち越す

### 6. Discord 投稿

論文ごとに分類されたジャンル数分の投稿を行う。各投稿間隔は1.2秒(Discord レート制限対策)。

`posted_log.json` に投稿済み論文のメタデータを記録する。記録内容:

```json
{
  "id": "2506.12345",
  "posted_at": "2025-06-24T01:10:00Z",
  "title": "...",
  "authors": "...",
  "link": "https://arxiv.org/abs/2506.12345",
  "primary": "quant-ph",
  "announce_type": "new",
  "genre_ids": ["qec", "ft"],
  "genre_names": ["誤り訂正・符号理論", "フォールトトレラント計算"],
  "abstract_en": "...",
  "abstract_ja": "..."
}
```

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
| `crypto` | 量子暗号・QKD | QKD・DI-QKD・量子乱数生成 |
| `pqc` | 耐量子計算機暗号 | 格子暗号(LWE/Kyber)・NIST PQC標準化 |
| `hardware` | 量子ハードウェア・実装 | 超伝導・イオントラップ・Rydberg・スピン量子ビット |
| `sensing` | 量子センシング・計測 | ハイゼンベルク限界・量子フィッシャー情報・原子時計 |
| `foundations` | 量子基礎・測定理論 | Bell不等式・デコヒーレンス・量子熱力学 |
| `other` | その他・異分野 | hep-*・gr-qc・nucl-*・cond-mat(一般)など量子情報外の論文 |

上記のどのジャンルにも該当しない場合は `DISCORD_WEBHOOK_GENERAL` へ送られる(フォールバック)。

---

## セットアップ

### 1. Discord Webhook の作成

通知先チャンネルごとに「チャンネル設定 → 連携サービス → ウェブフック」で Webhook URL を作成する。各ジャンルに対応するチャンネルを用意し、それぞれの URL を後述の Secret として登録する。

ジャンルを細かく分けずに運用する場合は `DISCORD_WEBHOOK_GENERAL` のみ設定すれば全論文がそこへ届く。

### 2. API キーの取得

`config.json` の `translators` に列挙した順に試行される。デフォルトは `["gemini", "deepl", "google"]`。**未登録のバックエンドは自動スキップされるため、Gemini キーのみでも動作する。**

| バックエンド | 用途 | 無料枠 | Secret 名 |
| --- | --- | --- | --- |
| Gemini | **推奨。翻訳 + 分類を1回で実行** | 無料枠あり(カード登録不要) | `GEMINI_API_KEY` |
| DeepL | 翻訳のみ(第2段) | 月50万文字まで無料 | `DEEPL_API_KEY` |
| Google Cloud Translation | 翻訳のみ(第3段) | 月50万文字まで無料(**請求先アカウント必須**) | `GOOGLE_TRANSLATE_API_KEY` |

Gemini API キーは [Google AI Studio](https://aistudio.google.com/) で発行できる。

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

### test_feed.xml を使ったフルテスト

ローカルの RSS ファイルを読み込み、翻訳・Discord 投稿まで含む全経路を確認できる。

```bash
export GEMINI_API_KEY="..."
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
| `name` | ○ | Discord embed に表示される日本語名称 |
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

### arXiv カテゴリヒント

cross-list 論文の primary カテゴリから分類を補助する設定。

- `category_genre_hints`: カテゴリ → ジャンル ID のマッピング。該当カテゴリの論文は指定ジャンルのスコアが +0.15 される
- `category_other_overrides`: このリストのカテゴリが primary の論文は `other` スコアが +1.0 され、実質的に強制 `other` になる

### cross-list フィルタ

| キー | 説明 |
| --- | --- |
| `cross_deny_primary` | このカテゴリが primary の cross-list 論文を除外する(デフォルト空=全通過) |
| `cross_allow_primary` | deny リストより優先されるホワイトリスト |

### 翻訳・投稿設定

| キー | デフォルト | 説明 |
| --- | --- | --- |
| `translators` | `["gemini","deepl","google"]` | 翻訳バックエンドの試行順 |
| `translate_batch_size` | `5` | 1リクエストにまとめる論文数 |
| `max_translate_chars` | `2000` | Gemini に渡す abstract の最大文字数(超過は切り捨て) |
| `translate_only_matched` | `false` | `true` にするとジャンル未分類論文は翻訳しない(API節約) |
| `require_translation` | `true` | `true`: 翻訳失敗論文は次回へ持ち越す / `false`: 英語のまま投稿 |
| `show_original_abstract` | `false` | `true` にすると日本語訳に加えて英語 abstract も embed に含める |
| `include_replacements` | `false` | `true` にすると差替え論文(replace)も投稿する |
| `gemini_model` | `"gemini-2.0-flash"` | 使用する Gemini モデル ID |
| `gemini_min_interval_sec` | `7` | Gemini リクエスト間の最小間隔(秒) |
| `gemini_max_retries` | `4` | 一時的エラー時のリトライ上限回数 |
| `gemini_overload_giveup` | `2` | 過負荷エラーが連続したら circuit breaker を開く閾値 |

---

## 留意事項

- RSS の `<category>` 要素の先頭を primary カテゴリとみなすヒューリスティックを使用している(arXiv API の保証ではなく経験則)。
- ジャンル分類は Gemini(主)と TF-IDF(フォールバック)によるヒューリスティックであり、誤分類は不可避。`description` の記述精度が Gemini 分類の精度に直結するため、境界が曖昧なジャンルは境界条件を明示した文章にすること。
- Gemini 無料枠の RPD(1日あたりリクエスト数)制限は変更される可能性があるため、導入時に [Google AI Studio](https://aistudio.google.com/) で現行値を確認すること。
- `seen_ids.json` は最新3000件、`posted_log.json` は最新5000件を保持し、それ以前のエントリは自動的に切り捨てられる。
