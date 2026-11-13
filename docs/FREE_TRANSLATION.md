# 無料枠を優先する翻訳経路

2026年11月1日の公式情報を確認した。Webサイトを無料で使えることと、botから利用できる公開APIが無料であることは区別する。

## Google Apps Scriptを追加する

`LanguageApp.translate`はGoogleが提供する正式なApps Scriptの翻訳機能で、Cloud Translation APIとは別のサービス。Google Cloudの課金有効化や有料プランの申し込みは不要。個人Googleアカウントでは5,000呼び出し/日、Google Workspaceでは20,000呼び出し/日が公表されている。上限はユーザー単位で、最初の要求から24時間後に更新される。1回のスクリプト実行は最大6分。

文字数無制限という保証はなく、短時間の呼び出し制限や入力長による失敗も考慮する。このbotのabstractの長さで動作と訳語を確認してから利用する。1つの`texts`要素につき翻訳機能を1回呼び出すので、タイトルとabstractを別々に送れば2回分を消費する。

公式資料: [LanguageApp](https://developers.google.com/apps-script/reference/language/language-app)、[利用上限](https://developers.google.com/apps-script/guides/services/quotas)、[Webアプリ](https://developers.google.com/apps-script/guides/web)。利用上限は変更される場合がある。

### 設定手順

1. [Apps Script](https://script.google.com/)で新しいプロジェクトを作り、`scripts/google_apps_script_translator.gs`の内容をエディタへ貼り付けて保存する。
2. **プロジェクトの設定 → スクリプト プロパティ**に、名前`API_TOKEN`で推測困難なランダム文字列を保存する。長さは32〜256文字。パスワード管理ソフト等で生成し、ソースコード、リポジトリ、ログへ書かない。
3. **デプロイ → 新しいデプロイ → ウェブアプリ**を選ぶ。**次のユーザーとして実行: 自分**、**アクセスできるユーザー: 全員（Anyone）**を指定する。Googleから求められた権限を、使用するコードを確認したうえで許可する。
4. 発行された`https://script.google.com/macros/s/…/exec`形式のURLを控える。`/dev`形式のテストURLは使用しない。組織の設定により「全員」を選べない場合、この方式をそのアカウントでは利用できない。
5. GitHubリポジトリの**Settings → Secrets and variables → Actions**に、`GOOGLE_APPS_SCRIPT_URL`と`GOOGLE_APPS_SCRIPT_TOKEN`を登録する。TOKENは手順2と完全に同じ値にする。
6. 通常botの`main_translators`には`apps_script`を追加済みで、通常通知Actionsの翻訳ステップにも両Secretを渡す設定がある。認証情報がない場合、この経路はスキップされる。SciRateの`translators`・workflowは従来のまま維持している。

GitHub ActionsからはGoogleへの対話ログインができないため、Webアプリ自体のアクセス設定は「全員」とする。実際の翻訳要求は`API_TOKEN`で認証する。URLだけを知っていても翻訳は実行できない。[ContentServiceの応答は`script.googleusercontent.com`の一時URLへリダイレクトされる](https://developers.google.com/apps-script/guides/content#redirects)ため、HTTPクライアントはリダイレクトを追跡する。

スクリプトを更新したら**デプロイを管理 → 編集 → 新しいバージョン**で反映する。スクリプトの保存だけでは既存デプロイに反映されない。このリポジトリのファイルを追加しても、Webアプリは自動で公開されない。

### 要求・応答

POSTするJSONの形式:

```json
{
  "token": "スクリプトプロパティと同じ秘密の値",
  "texts": ["A paper title", "An English abstract."],
  "source_language": "en",
  "target_language": "ja"
}
```

成功すると入力と同じ順序・件数の`{"translations":["…","…"]}`を返す。1要求は1〜5件、入力テキスト合計20,000文字まで。これはこのブリッジが設けた制限で、Google側の翻訳上限を保証するものではない。

失敗時は`{"error":"translation_failed","translations":[]}`等を返す。Apps ScriptのContentServiceではHTTPステータスを指定できないため、HTTP 200でも`error`がないか、翻訳が空でないか、件数が一致するかを確認する。途中の要素で失敗した場合、それ以前の翻訳は利用枠を消費していても結果を返さないため、再試行には追加の枠が必要になる。

## 既存サービスと候補

| サービス | 無料条件・制約 | このbotでの扱い |
| --- | --- | --- |
| Azure Translator F0 | 200万文字/月 | 現在401が出る場合、Azureポータルの有効なリソース・キー・リージョンの組み合わせを確認する。公開リポジトリから正しい認証情報を復元することはできない。 |
| Google Cloud Translation NMT | 50万文字/月相当の毎月$10控除。Basic/Advanced合算 | 超過分は有料。2026-11-12から通常botのチェーンに入れ、botが太平洋時間の月ごとに `google_monthly_char_limit`(45万字)で止める。Apps Scriptとは別の枠。 |
| DeepL | 旧API Freeは50万文字/月。新規受付終了。新しいDeveloperは累計100万文字で更新なし | 既存アカウントがどのプランか確認する。新規登録で毎月無料枠が得られると想定しない。 |
| Nani | 無料のWeb・デスクトップ版あり。外部向け公開APIなし | botの翻訳先としては採用しない。アプリ起動用URLも結果を返せない。 |
| MyMemory | 匿名5,000文字/日、有効な連絡先メール指定で50,000文字/日。1要求500**バイト**まで | 文単位への分割が必要。1日分の上限がある補助経路であり、日本語・専門用語の品質は要検証。 |
| LibreTranslate | 自分の計算機で動かすソフトは無料。公式ホストのAPIキーは有料、無料試用枠なし | 自前実行は可能だが、計算資源と稼働維持が必要。 |
| NICT TexTra | 非商用APIあり。学校利用は試験・評価範囲という条件、表示・利用条件あり。恒久的な数値枠は確認できず | 学術用途の評価候補。長期運用の無料基盤として即採用しない。arXivの既訳検索APIもある。 |

根拠: [Azure料金](https://azure.microsoft.com/en-us/pricing/details/translator/)、[Azure認証](https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/reference/authentication)、[Google Cloud料金](https://cloud.google.com/products/translate/pricing)、[DeepL APIプラン](https://support.deepl.com/hc/en-us/articles/360021200939-DeepL-API-plans)、[Nani公式FAQ](https://nani.now/en)、[Naniアプリ連携仕様](https://nani.now/en/blog/custom-url-scheme-translate)、[MyMemory上限](https://mymemory.translated.net/doc/usagelimits.php)、[MyMemory API仕様](https://mymemory.translated.net/doc/spec.php)、[LibreTranslate](https://portal.libretranslate.com/)、[TexTra規約](https://mt-auto-minhon-mlt.ucri.jgn-x.jp/content/policy/)、[NICTの論文検索API説明](https://www.nict.go.jp/publicity/topics/2025/06/11-1.html)。

月333万文字ならAzureとGoogle Cloudの無料分の合計250万文字だけでは足りない。旧DeepL無料枠が維持されても合計300万文字なので、追加経路の評価と、上限到達後に保留できるキューが必要になる。

2026-11-12時点の通常botの順序は `deepl → azure → google → apps_script → gemini`。Google Cloudは上記の上限まで使い、Geminiは分類用project(課金無効)の無料枠から1日 `gemini_translation_daily_request_limit`(100)回までを最後の予備として使う。不足分を確実に埋められるのは、このApps Scriptのデプロイである。
