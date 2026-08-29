---
name: auto-slide
description: 写真フォルダからスライドショー動画(mp4)を作る。候補はアルゴリズムで絞り、最終的な「いい写真」の選択は Claude 自身が写真を見て行う。「スライドショーを作って」「この写真フォルダから動画」「いい写真を選んで動画に」「フォトムービー」等で使う。
---

# auto-slide

写真フォルダ → mp4。**候補の絞り込みはCLIのアルゴリズム**(重複除去・ブレ落とし・場所/類似の分散)、
**最終選択はあなた(Claude)が実際に画像を見て**行う。

## 前提

- カレントディレクトリが auto-slide リポジトリで、`autoslide` が使える(無ければ `uv pip install -e .`)。
- `ffmpeg` が必要(`brew install ffmpeg`)。
- **API キーは不要**。写真の選択もタイトル/感情/キャプションも、このセッション(Claude Code)自身が行う。

## 手順

### 1. 候補を出す

ユーザーにフォルダのパスと枚数 N(既定 8)、アスペクト(既定 16:9)を確認する。

```
autoslide candidates "<folder>" --count <N> --out out/<name> --aspect <16:9|1:1|9:16>
```

`out/<name>/candidates.json` と `out/<name>/candidates_00.jpg`(以降ページ)ができる。
候補が少なすぎる/多すぎるときは `--pool-factor 3.5` などで調整して再実行。

### 2. 写真を見て選ぶ ← ここがこのスキルの主目的

1. `out/<name>/candidates_*.jpg` を **Read で表示**して全体を把握する。番号(#1, #2 …)は candidates.json と一致。
2. 判断が微妙な番号は `candidates.json` の `pool[].thumb` のパスを **個別に Read** して細部(ピント・表情・傾き)を確認。
3. `candidates.json` の `groups`(= 撮影シーン/場所のまとまり)を見て、次を満たすよう **ちょうど N 枚**選ぶ:
   - 各 group から最低 1 枚。場所・構図が偏らないように。
   - **除外**: 明確なブレ、大きな露出破綻(白飛び/黒潰れ)、ほぼ同一構図の重複、水平が大きく傾いて見づらいもの。
   - **採用**: ピントが合って主題が明快、group 内の他と画が違う、光や表情が良いもの。
   - ユーザーに感情/雰囲気の希望があれば聞き、それに合う画を優先する。
4. 選んだ番号と、各 group の内訳・主な除外理由を一度ユーザーに提示してよい(任意)。

### 3. 選んだら selection にする

```
autoslide pick out/<name> --set <選んだ番号をカンマ区切り>
```

`out/<name>/selection.json`(`order` に最終的な並び順のパス)ができる。

### 4. タイトル・感情・キャプションを書く ← ここも Claude 自身が行う

`out/<name>/proposal.template.json` を開き、写真を見た印象と `selection.json` の `order` をもとに埋めて
`out/<name>/proposal.json` として保存する。スキーマ:

```json
{
  "title": "日本語の短いタイトル(30字以内)",
  "mood": "calm|nostalgic|melancholic|joyful|upbeat|dramatic のいずれか1語",
  "mood_note": "感情の補足(任意, 20字以内)",
  "music_style": "曲調の希望(例: ゆったりしたピアノ)",
  "group_labels": {"0": "シーンの見出し", "1": "..."},
  "captions": {"IMG_0007.jpg": "写真ごとの短い一言(任意)"},
  "hashtags": ["#タグ"]
}
```

- `captions` のキーはファイル名(basename)でも、`order` での 1 始まりの番号でもよい。付けたい写真だけでよい。
- `mood` は BGM 選定にも使われる。

### 5. 動画化

```
autoslide plan   "<folder>" --out out/<name> --selection out/<name>/selection.json --proposal out/<name>/proposal.json
autoslide render out/<name>/plan.json --out out/<name>/out.mp4
```

- `--proposal` を渡すと API は一切呼ばない。省くとフォールバック(フォルダ名＋日付、mood=calm)。
- `out/<name>/plan.json` は編集可。`title` / `captions` / `order` を直して `render` し直せる。
- BGM を付けるには `assets/music_index.json` に音源を登録(README 参照)。未登録なら無音。

### 6. 報告

生成した mp4 のパス、選んだ枚数と各 group の内訳、タイトル、除外した主な理由を簡潔に伝える。

## メモ

- API キーは使わない。写真の判断・タイトル・感情はすべてこのセッションが行う。
- 候補プールに無い写真は選べない。もっと欲しいときは `--count` を増やすか `--pool-factor` を上げて `candidates` から。
- `pick --set` の枚数が `--count` と違うと警告は出るが続行はする。
- 完全自動(Claude が写真を見ない)でよいなら `autoslide run "<folder>" --count N` 一発（この場合だけ、タイトル自動生成に API キーがあれば使う）。
