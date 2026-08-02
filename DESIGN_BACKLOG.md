# 設計バックログ（監査項目10以降）

2026-07-30 の設計監査で挙げた項目のうち、1〜9 は `b70588f` で修正・本番デプロイ済み。
本書は **10 以降の構想と実施順序** をまとめたもの。実装は未着手で、着手するかは項目ごとにユーザーが決める。

---

## 0. 現況の再検証（2026-07-30 時点、`c4be2fd`）

監査後の修正で結果的に解消された項目があるため、まず実コードで確認した。

| # | 項目 | 状態 |
|---|---|---|
| 26 | repostスクリプトの冪等性 | **解消済み** — `repost_genre_ids` / `repost_channels` で配信済みchannelをskip |
| 28 | state保存の非atomic・件数制限 | **半分解消** — stateは `atomic_write_json` かつ上限撤廃。ただし `posted_log.json` は `log[-5000:]` の切り詰めが残存（現在2154件、未発火） |
| 30 | integration test不足 | **一部解消** — `test_delivery_reliability` / `test_repost_reliability` / `test_scirate_reliability` を追加。policy変更・切り詰め後再投稿・audit整合は未カバー |
| 22 | quant-ph優先がend-to-endでない | **緩和** — 配信が独立フェーズになり途中停止に強くなった。ただし外部LLMレビューは同一runの discover フェーズ内で quant-ph 配信より前に走る |
| 10〜21, 23〜25, 27, 29 | — | **未着手** |

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

- **Phase 0 がないと Phase 2 以降を評価できない。** 現在ログには最終genreしか残らず（#24）、「AIがそう判断した」のか「後処理が足した」のか区別できない。また本番同等の分類を投稿なしで試す手段がない（#27）ため、方針変更の影響を事前に測れない。
- **Phase 1 がないと Phase 2 以降が過去に届かない。** 分類cacheのキーは `category:arxiv_id` のみで policy version を含まない（#17）。方針を変えても過去の accept/skip は凍結されたまま。特に skip は永久に再評価されない。
- **Phase 2 が決まらないと Phase 3・4 の正解が決まらない。** 「最終判断は誰か」が未確定のまま個別keywordを調整しても、方針が揺れるたびに手戻りする。

---

## Phase 0 — 観測と試験台

### #24 ログでAI判断と後処理を区別できない

**現状**：`posted_log.json` の `classifier` は `"gemini-2.5-flash"` のような単一モデル名のみ。
`postprocess_genres` による primary強制other・crypto強制追加・QEC追加は記録されない。先の crypto 誤配信（metrologyの "blind directions" 等）もログ上は Gemini の判断に見える。

**構想**：分類結果を「決定の履歴」として持つ。

```json
"classification": {
  "model_votes": [{"model": "gemini-2.5-flash", "genre_ids": ["qit"]}],
  "base_genre_ids": ["qit"],
  "postprocess": [
    {"rule": "force_genre_keywords.crypto", "matched": "blind", "added": ["crypto"]},
    {"rule": "qec_adjacent_coding", "matched": "linear code", "added": ["qec"]}
  ],
  "final_genre_ids": ["qit", "crypto", "qec"]
}
```

`classifier` は後方互換のため残す。`postprocess_genres` / `apply_forced_genres` / `apply_external_qec_policy` が理由を返す形に変える。

**効果**：これ単体で「後処理が何件・どのkeywordで発火しているか」を実データから集計できるようになり、#11・#18 の是非を推測でなく実測で決められる。
**コスト**：小〜中（純粋な追加、既存挙動を変えない）。**リスク**：低。

### #27 本番同等の分類を投稿なしで試せない

**現状**：
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
- policy version 変更時に過去分が再評価されること（#17と同時）
- `posted_log` 切り詰め後に再投稿が起きないこと（#28残り）
- audit / SciRate が本番と同じ分類を出すこと（#25）

---

## Phase 1 — 再評価可能性

### #17 分類cacheにpolicy versionがない

**現状**：`external_reviews` のキーは `f"{category}:{paper['id']}"`。genre定義・prompt・QECルール・モデル構成を変えても過去の判定は再評価されない。特に skip はそのまま固定される。

**構想**：
- `config.json` に `classification_policy_version`（整数 or ハッシュ）を持つ
- prompt本文・genre定義・`qec_adjacent_coding_terms`・`force_genre_keywords`・モデル構成から**自動でハッシュを算出**する方が運用が楽（手動更新は必ず忘れる）
- cacheエントリに `policy` を保存し、不一致なら「再評価対象」とする
- 全件再評価はAPI量が跳ねるので、**skipのみ再評価** / **accept含め全件** を選べるようにする

**判断が必要**：policy変更時に既存 accept の genre も更新するか（＝過去投稿の追い投稿が発生しうる）、それとも新規判定にのみ適用するか。

**コスト**：中。**リスク**：中（再評価の範囲を誤ると大量の追い投稿になる。`--preview` 必須）。

---

## Phase 2 — 分類の権限モデル（中核）

### #10 最終分類はAIだけではない

**現状の実態**：

| 経路 | AI前 | AI判定 | AI後の決定的補正 |
|---|---|---|---|
| quant-ph | TF-IDFでモデル振り分け | Flash → Flash Lite → Cerebras | crypto強制追加、QEC隣接追加、（cross-list以外は）primary強制other |
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

### #11 crypto強制keywordが広すぎる

**現状**：`force_genre_keywords.crypto` に `blind` / `blindness` / `verifiable` / `verifiability` / `secure` / `securely` / `security` を含む14語。マッチは `\b<語>\w*\b` を title+abstract 全体に適用し、**1回でも出れば crypto を追加し、同時に `other` を除去する**。

実際の誤配信：metrology の "blind directions"、chirality の "blind"、数学的な "verifiable certificates"、"symbol-blind channel estimation"。

**構想**（(a)または(b)を選んだ場合）：
- 単語単独マッチ（`blind`, `secure`, `security`, `verifiable`）を削除し、**句のみ残す**（`blind quantum computation`, `delegated quantum computation`, `untrusted server` 等）。現状すでに句形の候補が7語あり、これらは誤爆していない
- 「AIがcryptoを落としたが句が存在する」場合のみ追加、という条件に狭める
- Phase 0 の #24 が入っていれば、**変更前に「どの語が何件発火しているか」を実測してから削る**ことができる

**コスト**：小（configのみ）。**リスク**：低。ただし削りすぎると取りこぼしが増えるため、`--preview` で before/after 比較する。

### #12 classify_max_genres が上限として機能していない

**現状**：`classify_max_genres`（既定2）は **TF-IDFのランキング切り出しとpromptの文面にしか使われていない**。LLM出力を正規化する `normalize_genre_ids` は件数を切らない。さらに crypto / QEC を後付けするため、通常ログだけで3分類以上が30件ある。

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
- quant-ph経路：分類不能・primary強制・TF-IDF低信頼を**全部 other** に入れる
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

**コスト**：中。**リスク**：API quota（Flash 無料枠）。`gemini_primary_run_budget: 20` との兼ね合いを要確認。

### #16 skip判定の独立性が低い

**現状**：Flash と Flash Lite は同系列・同prompt・同abstract。2つが skip すると Cerebras まで到達しない。単独skip票は pending に保存されず、次回ゼロからやり直し（`single_skip_pending` という統計はあるが再利用されていない）。

**構想**：
- 合意を取るモデルを **系列違いにする**（Flash + Cerebras を必須にし、Flash Lite は3番手）
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

### #25 audit / SciRate が外部経路を再現しない

**現状**：`scripts/audit_classification.py` は全論文に `postprocess_genres`（quant-ph経路）を適用する（[audit_classification.py:145](scripts/audit_classification.py#L145)）。外部由来の cs.IT primary 論文などは監査時に other へ変わり、**監査結果に偽の差分が出る**。

※ このファイルはユーザーの未コミット作業中のため、着手前に現状を確認すること。

**構想**：分類の入口を1本の関数に集約する。

```python
classify_paper(paper, route="quantph"|"external"|"scirate", cfg, genres) -> ClassificationResult
```

audit も SciRate も本番もこれを呼ぶ。#24 の decision trace もこの戻り値に含める。
→ #14 と同時にやると効率がよい。

### #29 翻訳方針が経路間で非対称

**現状**：
- `google_skip_translation_genres` の genre は Google 翻訳が禁止。DeepL/Azure が失敗すると `allow_untranslated = True` になり、`require_translation: true` でも**英語のまま投稿される**
- タイトルだけ翻訳失敗した場合の再試行がない
- SciRate は Google除外設定を無視する

**構想**：
- 翻訳失敗は「英語で投稿」ではなく **pending に積んで次回再試行**（#3 で作った queue がそのまま使える）
- N回失敗したら英語投稿にフォールバックし、その旨を run report に出す
- SciRate も同じ `google_translation_allowed` を通す

**コスト**：小〜中（queue基盤は既にある）。**リスク**：低。

---

## Phase 5 — 周辺整理

### #23 TF-IDF の実態は英語keyword分類

**現状**：`_tokenize` が `[a-z][a-z0-9]*` のみ拾うため、genre `description` の日本語はTF-IDFにほぼ寄与しない。実質、英語keyword一致に近い。

**判断が必要**：TF-IDF を今後どう位置づけるか。
- Phase 2 で「AI全滅時は保留」を選ぶなら、TF-IDF は **モデル振り分け（prescreen）専用**になり、日本語対応は不要。設定名とREADMEをそれに合わせるだけでよい
- 最終fallbackとして残すなら、genre ごとに英語keywordを明示的に持たせる（日本語descriptionに依存しない）

実績上、直近200件で TF-IDF 最終fallbackは0件。優先度は低い。

### #28 残り：posted_log の切り詰め

`posted_log.json` は `log[-5000:]` で切り詰められる（現在2154件）。state 側は上限撤廃済みだが、ログ側は残っている。
`posted_log` は重複防止の権威ではない（state が権威）ので correctness には直結しないが、`repost_missing_channels.py` が参照するため、**古い論文の追い投稿判定が5000件を超えると効かなくなる**。年次でのアーカイブ分割を検討。

---

## 2. 先に決めてほしいこと

以降を進めるにあたり、ユーザー判断が必要なのは次の4点。これが決まれば残りは機械的に決まる。

1. **#10 分類の権限モデル** — (a) AI主体＋明示ポリシーのみ後処理 / (b) 現状維持＋補正精度改善 / (c) 完全AI-only
2. **#12 genre数の上限** — 上限撤廃（multi-label前提）か、厳格な上限か（後者なら #20 の定義整理が必要）
3. **#13 外部ルールの矢印** — 厳密なroutingか、主要候補にすぎないか
4. **#21 完全性の主張範囲** — 3カテゴリのprimaryのままか、拡張するか

Phase 0（#24, #27）は上記のどれを選んでも必要になるため、**判断を待たずに着手できる**。
