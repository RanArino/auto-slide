---
name: auto-slide
description: 写真フォルダからスライドショー動画(mp4)を作る。候補はアルゴリズムで絞り、最終的な「いい写真」の選択は Claude 自身が写真を見て行う。「スライドショーを作って」「この写真フォルダから動画」「いい写真を選んで動画に」「フォトムービー」等で使う。
---

# auto-slide

写真フォルダ → mp4 + srt。**候補の絞り込みはCLIのアルゴリズム**(重複除去・ブレ落とし・場所/類似の分散)、
**最終選択はあなた(Claude)が実際に画像を見て**行う。
**構成案は人間可読な要約として提示し、自然言語フィードバックで直し、承認を得てから最終生成する。**
無言で最終 MP4 まで進めない(未承認の plan.json は `render` が拒否する)。

## 前提

- カレントディレクトリが auto-slide リポジトリで、`autoslide` が使える(無ければ `uv pip install -e .`)。
- `ffmpeg` が必要(`brew install ffmpeg`)。
- **API キーは不要**。写真の選択もタイトル/感情/キャプションも、このセッション自身が行う。

## 手順

### 0. 環境チェック

```
autoslide doctor
```

FAIL があれば原因(ffmpeg フィルタ不足 / CJK フォント無し / Pillow 機能不足)を伝えて中断する。
文字が焼けない環境で無理に生成しない。

### 1. 候補を出す

ユーザーにフォルダのパスと枚数 N(既定 8)、アスペクト(既定 16:9)、
写真の見せ方(`--fit contain-blur` = 既定・全カット原寸を収めてぼかし背景 / `auto` = 向きが逆の写真だけぼかし背景 / `cover` = 全部いっぱいにクロップ)、
グルーピング(`--grouping folder` = フォルダ整理を尊重 / `exif` = 時刻・GPS / `auto`)を確認する。

```
autoslide candidates "<folder>" --count <N> --out out/<name> --aspect <16:9|1:1|9:16> [--fit ...] [--grouping ...]
```

`out/<name>/candidates.json` と `out/<name>/candidates_00.jpg`(以降ページ)ができる。
候補が少なすぎる/多すぎるときは `--pool-factor 3.5` などで調整して再実行。

### 2. 写真を見て選ぶ ← このスキルの主目的

1. `out/<name>/candidates_*.jpg` を **Read で表示**して全体を把握する。番号(#1, #2 …)は candidates.json と一致。
2. 微妙な番号は `candidates.json` の `pool[].thumb` を **個別に Read** して細部(ピント・表情・傾き)を確認。
3. `candidates.json` の `groups`(撮影シーン/場所のまとまり)を見て、**ちょうど N 枚**選ぶ:
   - 各 group から最低 1 枚。場所・構図が偏らないように。
   - **除外**: 明確なブレ、大きな露出破綻、ほぼ同一構図の重複、水平が大きく傾いたもの。
   - **採用**: ピントが合い主題が明快、group 内の他と画が違う、光や表情が良いもの。
   - コンタクトシートの `comp`(黄金比・三分割の目安、0〜1)が高いものを優先的に検討。
     ただし最終判断は実際の画を見て(スコアは幾何ヒューリスティック)。
   - ユーザーに感情/雰囲気の希望があれば聞き、それに合う画を優先する。

### 3. 構成案を作って提示する

```
autoslide pick  out/<name> --set <選んだ番号をカンマ区切り>
autoslide plan  "<folder>" --out out/<name> --selection out/<name>/selection.json
```

`plan` は `plan.json`(**未承認: `approved:false`**)と、章立て・キャプション・BGM・
色補正方針の入った案を作り、**人間可読な要約を出力する**(内部で `autoslide summary` を実行)。

1. その要約を**チャットにそのまま提示**する。
2. 露出→色の before/after があれば `out/<name>/tone_preview/*.jpg` を **Read で見せる**。
3. ユーザーの自然言語フィードバックを反映する:
   - 「7章目を削って」「順番を変えて」→ `plan.json` の `chapters` / `order` を Edit
   - 「もっと明るいトーンで」「タイトルを変えて」→ `plan.json` の `title` / `overall_tone` / `mood` を Edit、
     または `--grouping` 等を変えて `autoslide plan` を再実行
   - 「BGM をもっと静かに」→ `plan.json` の `music` を差し替え、または `autoslide plan --music <file>`
   - 「色補正を強く/かけない」→ `plan.json` の `color.strength`(0〜1)を Edit
   - 「露出補正を強く/かけない」→ `plan.json` の `exposure.strength`(0〜1)を Edit
   - 「この写真は補正しない」→ `plan.json` の `color.per_image["<path>"].disabled` /
     `exposure.per_image["<path>"].disabled` を `true` に
   - キャプションを付ける/直す → `plan.json` の `captions` を Edit(焼き込みと SRT の両方に反映)。
     日付の副題は `caption_sub`(EXIF 由来、`""` で消せる)
   - 「シネマの黒帯をやめる」→ `plan.json` の `config.letterbox` を `false`、
     「中央バー字幕にする」→ `config.caption_style` を `"bar"`
4. 直したら再度 `autoslide summary out/<name>/plan.json` を実行して**更新後の要約を提示**する。
5. これを納得いくまで繰り返す。**勝手に承認・生成しない。**
6. 1往復で済ませず、次の観点を能動的に確認する:
   - **章ごと**: 区切り文言・キャプションのトーン(硬い⇔柔らかい)は合っているか
   - **全体構成**: 枚数配分の偏り、始まり方・終わり方の印象
   - **見た目**: letterbox の有無、fit の方式、字幕スタイル(lower-left / bar)
   - **音**: BGM のムード・音量感の希望
   - **補正**: 色/露出補正の有無・強さ
   これらは AskUserQuestion でまとめて選択式に聞き、2〜3往復かけて詰めてから承認に進む。

### 4. 承認 → 動画化

ユーザーが明示的に OK と言ってから:

```
autoslide approve out/<name>/plan.json
autoslide render  out/<name>/plan.json --out out/<name>/out.mp4
```

- `out/<name>/out.mp4` と `out/<name>/out.srt`(焼き込みキャプションと同じ文言・タイミング)ができる。
- 既定 `--fit contain-blur` では、全カット原寸を収めてぼかし背景で余白を埋める(上下も左右も切らない)。
  レターボックス(`config.letterbox`)も既定 false。`auto` は向きが逆の写真だけ contain-blur・他は cover、
  `cover` は全カットいっぱいにクロップ。

### 5. 報告

生成した mp4 / srt のパス、選んだ枚数と各章の内訳、タイトル、除外した主な理由、
色補正をかけた枚数を簡潔に伝える。

## メモ

- 候補プールに無い写真は選べない。もっと欲しいときは `--count` を増やすか `--pool-factor` を上げて `candidates` から。
- `plan.json` を編集しても `approved` は自動では戻らない。内容を大きく変えたら
  再度 `autoslide summary` で確認し、ユーザーの再承認を得てから `render` する。
- `autoslide run "<folder>" --count N` は既定では `plan` + 要約提示で**停止**する。
  ユーザーが明示的に一括生成を望むときだけ `--yes` を付ける。
- 一度 `render` した plan.json を**再生成**するときは、既定で `out.mp4` を上書きせず
  `--out out/<name>/out_v2.mp4` のように連番の別ファイルにする(ユーザーが上書きを明示した時だけ既定名に戻す)。
