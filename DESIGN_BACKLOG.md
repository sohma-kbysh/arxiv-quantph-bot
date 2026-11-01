# 設計バックログ（監査項目10以降）

2026-07-30 の設計監査で挙げた項目のうち、1〜9 は `b70588f` で修正・本番デプロイ済み。
本書は **10 以降の構想と実施順序** をまとめたもの。2026-11-01に実装状況を更新した。未決の分類方針や対象カテゴリの拡張は、以下の限定的な改善とは分けて判断する。

---

## 2026-11-01 更新

- 分類順を `gemini-3.5-flash-lite` → `gemini-3.8-flash` → 決定的フォールバックへ簡素化。両モデル7秒間隔、thinkingはminimal/low、出力上限4096、再試行2回、過負荷打ち切り閾値1。無料利用可能量や実分類精度は実アカウントでの評価が必要。
- #11の一般語crypto追加を特定句へ限定し、#23の英語説明・正規化・キーワード証拠の重複排除を実装。
- #24の分類履歴、#17の方針fingerprintによるskip再評価、#25の経路別監査を実装。採用済み・配信済みの判定は変更しない。TF-IDFの未配信キューだけ次回discoverで再評価する。
- quant-phカテゴリAPIによる取りこぼし回収を追加。成功後20時間間隔、初回7日、最終成功からの経過＋重複2日を考慮し、全ページ成功時のみcursorを進める。投稿日窓より古い論文の新cross-listはRSSに依存する。
- 標準翻訳順をDeepL → Azure → Apps Scriptへ変更し、Google Cloudは明示的な有効化時だけ使用する。Apps Scriptの公開endpointは未デプロイで、Secret設定まではスキップする。SciRateのソース・workflowは今回の変更対象外。

## 0. 現況の再検証（2026-11-01 更新）

監査後の修正で結果的に解消された項目があるため、まず実コードで確認した。

| # | 項目 | 状態 |
|---|---|---|
| 26 | repostスクリプトの冪等性 | **解消済み** — `repost_genre_ids` / `repost_channels` で配信済みchannelをskip |
| 28 | state保存の非atomic・件数制限 | **半分解消** — stateは `atomic_write_json` かつ上限撤廃。ただし `posted_log.json` は `log[-5000:]` の切り詰めが残存（現在5000件で上限到達） |
| 30 | integration test不足 | **一部解消** — 配信・外部審査・経路別audit・分類fallbackのオフライン回帰試験を追加。ログ切り詰め後の監査や新モデルの実精度評価は残る |
| 22 | quant-ph優先がend-to-endでない | **緩和** — 配信が独立フェーズになり途中停止に強くなった。ただし外部LLMレビューは同一runの discover フェーズ内で quant-ph 配信より前に走る |
| 11, 23, 24 | 一般語crypto・決定的分類・判断履歴 | **解消済み**（詳細は各節） |
| 17, 25, 27 | 再評価・経路別audit・preview | **一部解消**（詳細は各節） |
| 10, 12〜16, 18〜21, 29 | 分類・翻訳方針 | **未決または未着手**。今回の改善で方針変更は行わない |

---

## 1. 全体の筋道

10以降は「どれから直すか」より **「どの順で直すと判断を間違えないか」** が重要。
分類方針（A）を先に触ると、良くなったのか悪くなったのかを測る手段がないまま本番へ出ることになる。したがって次の順序を推奨する。

```
Phase 0  観測と試験台        →  #24, #27  （＋#30の残り）
Phase 1  再評価可能性        →  #17
Phase 2  分類の権限モデル    →  #10 → #11, #12, #18, #19
Phase 3  マルチラベルの品質  →  #15, #16, #20
Phase 4  経路の対称化        →  #13, #14, #21, #25, #29
Phase 5  周辺整理            →  #23, #28残り
```

理由：

- **観測と試験台を先に整える。** #24の判断履歴と過去投稿に対する経路別auditは実装済み。全取得・翻訳・配信判断をまとめた副作用なしpreviewは#27として残る。
- **再評価の範囲を制御する。** #17で方針変更後のskipを再評価可能にした。採用済み・配信済みの分類は維持し、意図しない追い投稿を防ぐ。
- **Phase 2 が決まらないと Phase 3・4 の正解が決まらない。** 「最終判断は誰か」が未確定のまま個別keywordを調整しても、方針が揺れるたびに手戻りする。

---

## Phase 0 — 観測と試験台

### #24 ログでAI判断と後処理を区別できない — 解消済み

新しいキュー・投稿ログの `classification` に `policy`、`base_genre_ids`、`final_genre_ids`、`postprocess` を保存する。各補正は `rule`・`matched_terms`・`added_genre_ids`・`removed_genre_ids` を持つ。既存の `classifier` も残す。決定的分類はその出自を記録し、LLMの出力と混同しない。

古い投稿履歴には後から根拠を補完しない。補正の発火頻度や誤分類を集計するための情報は、今回以降の判定から蓄積する。

### #27 本番同等の分類を投稿なしで試せない — 一部解消

**現状**：過去投稿は `scripts/audit_classification.py` で本番と同じ取得経路の分類・後処理を再実行できる。Discord投稿・本番状態更新はなく、モデルの指定も可能。ただしAPI利用枠は消費する。新規取得から翻訳判定まで含む完全なpreviewは未実装。

- `--dry-run` = LLMを呼ばず TF-IDF のみ（本番と別物）
- `ARXIV_TEST_FEED` = 入力だけテスト、**投稿先は本番webhook・stateも本番**

**構想**：`--preview` フェーズを追加する。

- 本番と同一のLLMチェーン・後処理・翻訳判定まで実行
- Discord投稿と state 書き込みは行わず、`preview_<date>.json` に「この論文はこのchannelへ、この理由で」を出力
- 既存の3フェーズ実装（discover → translate → deliver）があるので、**deliver だけをno-op化する**のが最小実装
- 併せて `--dry-run` を `--tfidf-only` へ改名（意味と名前を一致させる）

**効果**：Phase 2以降の方針変更を、Discordを汚さずに本番同等で比較できる。**#30の残り（policy変更時の回帰）もこれに乗せられる。**
**コスト**：中。**リスク**：低（配信側は触らない）。

### #30 残り

`--preview` を土台に、次を追加：
- policy fingerprintが変わったskipの再評価を検証し、採用済み判定を維持する（#17）
- `posted_log` 切り詰め後に再投稿が起きないこと（#28残り）
- auditの経路一致は回帰試験済み。SciRateは今回変更せず、別途対象を判断する（#25）

---

## Phase 1 — 再評価可能性

### #17 分類cacheにpolicy versionがない — 一部解消

`external_reviews` の判定には分類設定・ルール・モデル構成から得る方針fingerprintを保存する。方針が変わった保存済みskipは、候補が再取得または保留から復元された際に再審査する。採用済み判定は有効なままとし、過去配信の変更・追い投稿は発生させない。

また、`reclassify_pending_fallback: true` では、どのチャンネルにも配信されていないTF-IDFキューを次回discoverで再評価する。一部でも配信済みなら分類を固定する。

**残る課題**：過去の全skipをAPI取得窓と無関係に再走査する仕組み、採用済み判定を明示的に再評価する安全な操作。これらは今回実装しない。

---

## Phase 2 — 分類の権限モデル（中核）

### #10 最終分類はAIだけではない

**現状の実態**：

| 経路 | AI前 | AI判定 | AI後の決定的補正 |
|---|---|---|---|
| quant-ph | モデル振り分けなし | Gemini 3.5 Flash Lite → Gemini 3.8 Flash | 特定句のcrypto追加、QEC隣接追加。quant-ph取得元のprimary強制otherはなし |
| 外部 | keyword query | 同上（最初の採用モデルで確定） | QEC隣接追加 |
| quant-ph全滅時 | — | TF-IDF が最終分類 | 同上 |

つまり「AIが決める」設計ではなく **「AI＋決定的後処理」**。

**選択肢**：

- **(a) AI主体＋明示ポリシーのみ後処理**（監査時の推奨）
  基礎判定はAIチェーンだけ。後処理は「広義QECを必ず拾う」のような、**明示的に決めた編集方針だけ**に限定する。`force_genre_keywords` の汎用語は廃止。AI全滅時はTF-IDF投稿ではなく保留。
- **(b) 現状維持＋補正の精度改善**
  後処理は残し、#11・#18 の判定を厳しくする。
- **(c) 完全AI-only**
  後処理を全廃。取りこぼしはprompt改善で対応。QEC広義運用は prompt の `review_instructions` 側だけで表現する。

(a) が今回のQEC方針と最も整合する。(c) は完全性最優先の方針とは相性が悪い（LLMの揺れがそのまま欠落になる）。

**これが決まらないと以下が決まらない：#11, #12, #18, #19。**

### #11 crypto強制keywordが広すぎる — 解消済み

`blind`・`secure`・`security`・`verifiable`などの一般語単独を除去し、`blind quantum computation`、`verifiable quantum computation`、`secure delegation`、`quantum key distribution`など特定の15句に限定した。大文字小文字・ハイフン・一般的な複数形を正規化し、一致語と補正内容を判断履歴に残す。

metrologyの“blind directions”など、一般語だけを根拠にcryptoへ追加する経路は回帰試験で防ぐ。より広い編集方針の変更や過去投稿の訂正は今回の対象外。

### #12 classify_max_genres が上限として機能していない

**現状**：`classify_max_genres`（既定2）は **TF-IDFのランキング切り出しとpromptの文面にしか使われていない**。LLM出力を正規化する `normalize_genre_ids` は件数を切らない。さらに crypto / QEC を後付けするため、2026-11-01時点の保持ログ5000件中101件が3分類以上だった。

**選択肢**：
- **(a) 上限を撤廃してmulti-label前提を明文化**（現状の実態に名前を合わせる）。設定名を `classify_target_genres`（＝AIへの目安）に改名
- **(b) parser側で厳格に切る**。その場合 **#20（channel境界の重複）を先に詰める必要がある** — 切る順序で qit と network のどちらが落ちるかが決まってしまうため

**推奨**：(a)。完全性最優先の方針では、上限で落とすより多めに配信する方が整合する。

### #18 QEC強制補正が粗い

**現状**：`is_qec_adjacent_coding_paper` は `qec_adjacent_coding_terms`（53語）のいずれかが title+abstract に**1回でも出現すれば true**。「論文の実質的貢献が符号理論か」は見ていない。一方、未列挙の表現は取りこぼす。

**構想**：
- 「広く拾う」方針自体は維持（ユーザー方針）。ただし **タイトル出現 / abstract先頭N文字出現を重み付け** するなど、"incidental mention" と "substantive contribution" を分ける
- あるいは後処理では拾わず、**prompt側（`review_instructions`）に寄せる**。外部経路はすでにその形になっているので、quant-ph経路のprompt にも同じ指示を入れれば経路が揃う（→ #14, #25 とも整合）
- Phase 0 の #24 で「QEC後付けが何件・どの語で発火したか」を実測してから決めるのが安全

### #19 other の意味が経路で違う

**現状**：
- quant-ph経路：LLMのother判定やTF-IDF低信頼はotherになる。quant-ph取得元へのprimary強制otherは廃止済み
- 外部経路：other は禁止（`excluded_genres`）。合わなければ skip
- `other, qec` のような組合せも生成可能

**構想**：other を用途で分ける。
- `other`（＝量子だが既存channelに当てはまらない）
- 保留（＝分類できなかった。配信せず pending に積み、次回再評価）
- 対象外（＝そもそも量子でない。skip）

primary強制otherは #1 の修正で quant-ph feed には効かなくなったので、残るのは非quant-ph feed経路のみ。ここを「保留」に変えるかが判断点。

---

## Phase 3 — マルチラベルの品質

### #15 採否は2モデル合意、channelは1モデル確定

**現状**：外部レビューで、最初に非skipを返したモデルの genre がそのまま確定する（`accepted_decisions[j]` が埋まると以降のモデルはそのpaperをskip）。skip だけ `external_skip_consensus: 2` を要求。
つまり **「通すか」の完全性は2重化されているのに、「どのchannelか」の完全性は1モデル任せ**。

**構想**：
- **(a) 和集合**：全モデルを呼び、genre の union を採る。完全性最優先とは最も整合。API量は最大2〜3倍
- **(b) 条件付き2モデル目**：1モデル目が1ラベルしか返さなかった場合のみ2モデル目を呼び union を採る。コスト増を抑えつつ取りこぼしを減らす
- (c) 現状維持

**推奨**：(b)。QEC取りこぼし（今回の4本）はまさに「1モデルが1ラベルしか返さない」ケースだった。

**コスト**：中。**リスク**：API quota。現行の2モデルを追加呼び出しする量を実測し、アカウントのRPM/TPM/RPDとの兼ね合いを確認する。

### #16 skip判定の独立性が低い

**現状**：Gemini 3.5 Flash Lite と Gemini 3.8 Flashは同じ提供元・同prompt・同abstractであり、skip誤りの相関は残る。単独skip票は次回へ持ち越さず、再実行時は再審査する（`single_skip_pending`は統計のみ）。

**構想**：
- モデル数だけを独立性とみなさず、プロンプト・証拠・人手評価を変えた場合のskip再現率を測る
- 単独skip票を pending に保存し、次回は「1票持ち越し」から再開する（現状の再計算を無駄にしない）
- #17 の policy version と組み合わせ、方針変更時は skip 票を破棄して再評価する

### #20 channel境界の重複

**現状の重複**：qit / network（量子通信路容量）、algo / sim（Hamiltonian simulation, QSP, LCU）、qec / ft（論理操作, concatenated code, 資源推定）、qit / foundations（entanglement, resource theory）。

multi-label 前提（#12(a)）なら問題にならない。**上限を設ける（#12(b)）場合のみ、genre定義の `description` を排他的に書き直す必要がある。**
→ #12 の決定に従属。単独で着手する意味は薄い。

---

## Phase 4 — 経路の対称化

### #13 cs.CR → pqc 等は制約ではなく単なる候補

**現状**：3ルールすべて `allow_all_genres: true`。`candidate_genres` は prompt 内の「最有力候補」ヒントに過ぎず、`excluded_genres`（other）以外はすべて許可されている。
したがって cs.CR → hardware/network/algo/qec、cs.IT → crypto/algo、cs.CC → その他すべて が実際に起きうる（今回のバックフィルでも cs.CR から hardware/network へ配信されている）。

**判断が必要**：「cs.CR → pqc」を**厳密なroutingとして意図していたのか**、それとも「主に pqc だが他もあり得る」だったのか。前者なら `allow_all_genres: false` にする。後者なら現状維持で、ドキュメント（README）の記述を実態に合わせる。

### #14 外部経路だけ channel の意味が広い

**現状**：`review_instructions` が明示的に意味を広げている。例：cs.CR では "hardware for PQC accelerators"、"network for QKD routing"。結果として古典PQC-TLSがnetwork、古典Gabidulin復号がalgo、PQCアクセラレータがhardwareへ入る。
一方 `config.json` の genre `description`（＝quant-ph経路のprompt根拠）は「量子ビット物理実装」のままで、**channelの意味が取得経路に依存している。**

**構想**：genre定義を単一の真実にする。
- 意図的に広げるなら、genre `description` 側を「量子・PQCの実装」等へ広げ、両経路が同じ定義を読む
- 広げないなら `review_instructions` から該当指示を外す
→ これを直すと #25（audit / SciRate が外部経路を再現しない）も自然に解ける。

### #21 外部completenessが3カテゴリのprimaryに限定

**現状**：`cs.CR` / `cs.CC` / `cs.IT` の primary のみ。したがって math.IT primary で cs.IT cross-list、cs.DS の量子アルゴリズム、eess.SP の量子通信などは対象外。「全arXivに対する完全性」ではない。

**構想**：
- **(a) カテゴリ追加**：`math.IT`, `cs.DS`, `eess.SP`, `cond-mat.*` 等を段階的に追加。1カテゴリごとに候補数とAPI量を実測してから広げる
- **(b) primary限定をやめる**：cross-list も含める。ただし候補数が跳ねるので LLM コストが問題になる
- **(c) 現状維持し、README に「対象は3カテゴリのprimary」と明記**（完全性の主張範囲を正直にする）

**推奨**：まず (c) で主張範囲を正しくし、その上で (a) を1カテゴリずつ。`--preview` があれば追加時の影響を投稿前に測れる。

### #25 audit / SciRate が外部経路を再現しない — auditは解消済み

監査はログの `source_feed`・`announce_type`・`classification_context` を復元する。quant-phには通常分類・後処理、外部候補には `review_external_candidates` の本番審査・skip合意・QEC方針を使う。旧ログの `announce_type: external` も復元し、未知のルールは推測せず未分類とする。

外部のcs.IT論文が監査時だけotherへ変わる偽差分と、ラベル順序だけの差分を防ぐ回帰試験を追加した。SciRateの実装・workflowは変更していない。経路の全体共通化は必要になった時点で判断する。

### #29 翻訳方針が経路間で非対称 — メインbotは一部解消

メインbotは `require_translation: true` の場合、低優先ジャンルでも未翻訳なら保留する。表示タイトルも `require_translated_title: true` で翻訳必須とした。旧 `allow_untranslated` が残るキューも、`allow_untranslated_low_priority: true` を明示しない限り例外を解除する。

標準チェーンはDeepL → Azure → Apps Script。Google Cloud Translationは控除超過課金の可能性があるため自動チェーンから除外し、adapterだけ明示有効化用に残す。`google_skip_translation_genres` はその任意経路の対象制限であり、翻訳必須を解除しない。

SciRateのソース・workflowは変更せず、経路間の完全な統一は今回行わない。Apps ScriptのWebアプリ公開とSecret設定も運用者の作業として残る。

---

## Phase 5 — 周辺整理

### #23 TF-IDF の実態は英語keyword分類 — 改善済み

各genreに英語の `classifier_description` を追加し、日本語説明に依存しないベクトルを作る。大文字小文字・ハイフン・一般的な単数複数形を正規化し、同じキーワード証拠の重複加点を避ける。TF-IDFによるモデル振り分けは廃止し、2モデルが分類できない場合の最終fallbackとして維持する。

2026年10月の保持ログでは2407件中248件（10.3%）がTF-IDFだった。新しいfallbackの実精度はオフライン回帰に加えて今後の標本監査で測定する。

### #28 残り：posted_log の切り詰め

`posted_log.json` は `log[-5000:]` で切り詰められる（現在5000件で上限到達）。state 側は上限撤廃済みだが、ログ側は残っている。
`posted_log` は重複防止の権威ではない（state が権威）ので correctness には直結しないが、`repost_missing_channels.py` が参照するため、**古い論文の追い投稿判定が5000件を超えると効かなくなる**。年次でのアーカイブ分割を検討。

---

## 2. 先に決めてほしいこと

以降を進めるにあたり、ユーザー判断が必要なのは次の4点。これが決まれば残りは機械的に決まる。

1. **#10 分類の権限モデル** — (a) AI主体＋明示ポリシーのみ後処理 / (b) 現状維持＋補正精度改善 / (c) 完全AI-only
2. **#12 genre数の上限** — 上限撤廃（multi-label前提）か、厳格な上限か（後者なら #20 の定義整理が必要）
3. **#13 外部ルールの矢印** — 厳密なroutingか、主要候補にすぎないか
4. **#21 完全性の主張範囲** — 3カテゴリのprimaryのままか、拡張するか

判断履歴（#24）と経路別の過去投稿auditは実装済み。未決方針を変更する前に、これらで現在の判定を測り、必要なら完全なpreview（#27）を追加する。
