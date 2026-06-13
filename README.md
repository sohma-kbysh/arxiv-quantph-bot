# arXiv quant-ph → Discord 通知bot(日本語abstract付き)

arXivの公式RSS(quant-ph)を1日1回取得し、量子情報に無関係なcross-list論文を
除外した上でキーワードによるジャンル分類を行い、abstractの日本語訳とともに
Discord Webhookへ投稿します。GitHub Actionsで動作し、全て無料枠内で運用できます。

## 構成

- `arxiv_bot.py` — 本体(標準ライブラリのみ。pip install不要)
- `config.json` — フィード、cross-list許可カテゴリ、ジャンル定義
- `seen_ids.json` — 投稿済みIDの記録(Actionsが自動commit)
- `.github/workflows/notify.yml` — 毎日 02:30 UTC(JST 11:30)、火〜土に実行

## セットアップ手順

1. **Discord側**:通知先チャンネルごとに
   「チャンネル設定 → 連携サービス → ウェブフック」でWebhook URLを作成。
   ジャンルごとにチャンネルを分けない場合は1本でよい
   (全ジャンルが `DISCORD_WEBHOOK_GENERAL` にフォールバックする)。

2. **翻訳APIキーの取得**:`config.json` の `"translators"` に列挙した
   順に試行され、前段で失敗した論文のみ後段へ回る(フォールバックチェーン)。
   既定は `["gemini", "deepl", "google"]`。キー未登録のバックエンドは
   自動的にスキップされるため、最低限Geminiのキーだけでも動作する。

   - **Gemini**(主翻訳・推奨): Google AI Studio でAPIキーを発行。
     無料枠あり、カード登録不要。Secret名 `GEMINI_API_KEY`
   - **DeepL**(第2段): DeepL API Free に登録(月50万文字まで無料)。
     Secret名 `DEEPL_API_KEY`
   - **Google Cloud Translation**(第3段): GCPでプロジェクトを作成し、
     Cloud Translation API を有効化してAPIキーを発行。月50万文字まで
     無料だが、**請求先アカウント(カード)の登録が必要**な点に注意。
     Secret名 `GOOGLE_TRANSLATE_API_KEY`

3. **GitHubリポジトリ**:このフォルダの内容をリポジトリとしてpushし、
   Settings → Secrets and variables → Actions に以下を登録:

   | Secret名 | 内容 |
   |---|---|
   | `DISCORD_WEBHOOK_GENERAL` | 必須。未分類論文・フォールバック先 |
   | `DISCORD_WEBHOOK_QEC` ほか | 任意。ジャンル別チャンネルのWebhook |
   | `GEMINI_API_KEY` または `DEEPL_API_KEY` | 翻訳用 |

4. Actionsタブから `workflow_dispatch` で手動実行して動作確認。

## ジャンル分類の仕組み

論文のジャンル分類は2段構えになっている。

1. **LLM分類(主経路)**: `"classify_with_llm": true`(既定)かつ翻訳チェーン
   先頭がGeminiの場合、翻訳リクエストと同一の呼び出しの中で、各abstractの
   ジャンルをGeminiに判定させる(リクエスト数は増えない)。各ジャンルの
   `description`(自然言語の定義文)が判定根拠として渡されるため、定型
   キーワードを含まない論文も内容に基づいて分類される。
2. **キーワード分類(フォールバック)**: Geminiがクォータ枯渇等で使えない
   場合、または個別エントリをLLMが返さなかった場合は、`keywords` への
   部分一致数が最大のジャンルへ割り当てる。

両経路とも、どのジャンルにも該当しなければ `general` に分類される。

## ローカルでの動作確認(休配日でも可)

arXivの新着アナウンスは平日のみで、週末・休日や更新前の時間帯はライブ
フィードが空になる(`0 fetched`)。同梱の `test_feed.xml` を使えば、
実データが無い時でもパース→フィルタ→分類→翻訳→Discord投稿の全経路を
確認できる。環境変数 `ARXIV_TEST_FEED` にファイルパスを指定するだけでよい。

```bash
export DISCORD_WEBHOOK_GENERAL="WebhookのURL"
export GEMINI_API_KEY="Geminiのキー"
export ARXIV_TEST_FEED=test_feed.xml
python3 arxiv_bot.py
```

`test_feed.xml` には各ジャンルに対応する6件(うち1件はcond-mat由来の
cross-listで、フィルタにより除外される)が含まれる。テスト投稿が
`seen_ids.json` に記録されるのを避けたい場合は、実行後にこのファイルを
`{"seen": []}` に戻すか、テスト用の別Webhookを使うこと。ライブ運用に
戻すときは `ARXIV_TEST_FEED` を unset する(`unset ARXIV_TEST_FEED`)。

## カスタマイズ

- **ジャンルの追加・変更**: `config.json` の `genres` 配列を編集。各ジャンルは
  `id`(LLM出力用の英数字ID)、`name`(表示名)、`description`(LLM分類の
  判定基準となる説明文)、`webhook_env`(配信先Webhookの環境変数名)、
  `keywords`(フォールバック分類用)を持つ。`id` は重複させないこと。
- **分類方式の切替**: `"classify_with_llm": false` にすると、LLM分類を使わず
  常にキーワード分類になる(翻訳はチェーン通り行われる)。
- **cross-listの許可範囲**: `cross_allow_primary`。primaryカテゴリが
  このリストに含まれないcross論文(cond-mat等)は投稿されない。
- **翻訳量の節約**: `"translate_only_matched": true` にすると、
  ジャンルにマッチした論文のみ翻訳する(DeepL無料枠運用時に有効)。
- **レート制限対策**: 翻訳は `translate_batch_size`(既定5)件を
  1リクエストにまとめて送信し、リクエスト間隔 `gemini_min_interval_sec`
  (既定7秒)・指数バックオフ再試行 `gemini_max_retries` 回で
  Gemini無料枠のRPM制限に対応する。クォータ枯渇を検知したバックエンド
  (Geminiの持続的429、DeepLの456、Googleの403/429)はその実行回では
  以後スキップされ(circuit breaker)、残りはチェーンの次段が引き受ける。
  全段で翻訳できなかった論文は投稿されず、次回実行時に持ち越される
  (`"require_translation": true` の場合)。英語のまま投稿してよいなら
  `false` にする。
- **原文の併記**: 既定では日本語訳のみを表示する。原文abstractも
  併記したい場合は `"show_original_abstract": true` にする。
- **フィード追加**: `feeds` に `"cs.IT"` 等を追加可能(重複は自動排除)。

## 留意事項

- RSSの`category`要素の先頭をprimaryカテゴリとみなすヒューリスティックを
  用いている。
- ジャンル分類はLLM(主)とキーワードマッチング(フォールバック)による
  ヒューリスティックであり、偽陽性・偽陰性は不可避。LLMの分類が不適切な
  場合は `description` を、フォールバックが不適切な場合は `keywords` を
  運用しながら調整すること。
- 無料APIの制限値(Gemini無料枠のRPD等)は変更され得るため、
  導入時に現行値を確認すること。
