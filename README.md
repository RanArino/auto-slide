# auto-slide

HDD の写真フォルダから、AI がスライドショー動画（mp4）を自動生成する。
グルーピング → 枚数選択（場所・類似を考慮）→ 構成/タイトル/感情の提案 → 人間可読な要約で対話レビュー → 承認 → BGM 選定・色補正・レンダリング（mp4 + srt）まで。

> このリポジトリは **動画生成まで** を実装。X 投稿は [PLAN.md](PLAN.md) の M8（未実装）。
>
> まず `autoslide doctor` で環境（ffmpeg フィルタ / CJK フォント / Pillow / HEIC 変換手段）を確認する。
> 必須項目が欠けたら明示エラーで止まる（文字が焼けないまま生成することはない）。

## できること

- フォルダを再帰走査し、EXIF（撮影日時・GPS・機種）を読んで索引化（SQLite キャッシュ、増分）
- **グルーピング**: `--grouping folder`（フォルダ整理を尊重）/ `exif`（撮影時刻ギャップ + GPS 近接）/ `auto`（写真が複数フォルダにまたがれば folder）
- **候補の絞り込み**: pHash で連写を 1 枚に間引き → グループへ枚数配分（平方根重みで偏り抑制）→ グループ内は farthest-point sampling で見た目が散るように選ぶ。`--seed` 固定で決定的
- **最終選択**: アルゴリズムのまま（`run`）か、候補プール（`candidates`）から **Claude が実際に写真を見て**選ぶ（`pick`）かを選べる
- **提案 → 対話レビュー**: タイトル・全体トーン・章見出し・区切りテキスト・キャプション・BGM ムード・色補正方針を提案。`autoslide summary` が**人間可読な要約**を出し、自然言語で直して再要約 → **明示承認**を経てから最終生成（未承認の `plan.json` は `render` が拒否）
- **写真の正規化**: 出力キャンバスは固定比率。`--fit auto`（既定: キャンバスと向きが逆の写真＝横動画の中の縦写真だけ `contain-blur`、他は `cover`）/ `cover`（全部いっぱいにクロップ）/ `contain-blur`（全部収めて同じ写真のぼかしを背景に）。同一グループ内は cover 拡大率を中央値付近にそろえる
- **構図スコア**: 黄金比／三分割の目安（0〜1、幾何ヒューリスティック）。分析段階で `candidates` のコンタクトシートと `summary` に `comp` として表示し、構図の良い写真に気付けるようにする（**選択ロジックは変えない、提示のみ**）
- **露出補正**: 白とびのロールオフ + 黒つぶれのシャドーリフト。ブラック／ホワイトポイントを保護し、強すぎる補正は自動抑制。強度 `exposure_strength`（0〜1, 既定 0.35）。露出良好な写真はスキップ
- **色補正**: グレーワールド白色補正。ゲインは 0.7〜1.4 にクランプ、低彩度はスキップ、強すぎる補正は自動抑制。強度 `color_strength`（0〜1, 既定 0.3）。露出→色の順で適用。提案時に `tone_preview/` に before/after、`plan.json` で個別に無効化可
- **BGM**: `assets/music_index.json` から mood 一致 → energy 近さ → 長さで選曲。トリム＋フェード＋ラウドネス正規化。音源が無ければ無音
- **レンダリング**: タイトルカード + 章ごとの区切りスライド + 1 枚 `seconds_per_slide` 秒（既定 4）+ クロスフェード。既定でシネマスコープの黒帯（`letterbox`, 2.39:1）。キャプションは下帯・左寄せの主文 + 小さな日付（`caption_style="lower-left"`、`"bar"` で従来の中央バー）。H.264 / yuv420p / +faststart の mp4 と、焼き込み主文と同一文言・同一タイミングの `out.srt`（日付副題は SRT に入れない）。文字は PIL で PNG に焼く（ffmpeg の drawtext に依存しない）。`--aspect` は `16:9` / `1:1` / `9:16`

## セットアップ

```bash
brew install ffmpeg
uv venv --python 3.14
uv pip install -e .           # 依存は pillow / numpy のみ
```

- HEIC を読む: `uv pip install -e '.[heic]'`
- **API キーは不要**。Claude Code のスキルから使えば、写真の選択もタイトル生成もセッション自身が行う。
- 例外として「Claude Code なしのヘッドレスで `run` / `plan` を回し、タイトルも自動生成したい」場合だけ
  `uv pip install -e '.[llm]'` ＋ `cp .env.example .env` で `ANTHROPIC_API_KEY` を設定。

## はじめての操作ガイド（コピペで実行）

はじめての人は、この順にターミナルへコピペすれば動画まで作れる。
**最初の 2 行だけ自分の環境に書き換える**。あとはそのままコピペでよい。
原本の写真は一切書き換えない（変換・作業ファイルは `.autoslide_cache/` と `out/<名前>/` にだけ作る）。

### 0. 準備（初回だけ）

```bash
brew install ffmpeg
uv venv --python 3.14
uv pip install -e .
```

iPhone の写真（HEIC）をそのまま使うなら、あわせて:

```bash
uv pip install -e '.[heic]'
```

### 1. 写真フォルダと名前を決める（ここだけ書き換える）

```bash
export PHOTOS="/Volumes/HDD/2025-11-旅行"   # ← 自分の写真フォルダに変更
export NAME="mymovie"                        # ← 出力の名前（半角英数）。出力は out/$NAME/ に貯まる
```

以降のコマンドは、このターミナルを閉じるまで **そのままコピペ** でよい。

### 2. 環境チェック

```bash
autoslide doctor
```

`doctor OK` が出れば準備完了。`FAIL` が出たら本節末尾の「困ったとき」を見る。

### 3A. パターン A：アルゴリズムにおまかせ（自分ひとりで完結）

```bash
autoslide run "$PHOTOS" --count 8 --out "out/$NAME"
```

章立て・キャプション・BGM・補正の **要約が表示されて止まる**。
内容を読んで、直したいところがあれば「4. 要約を見て直す」へ。良ければそのまま:

```bash
autoslide approve "out/$NAME/plan.json"
autoslide render  "out/$NAME/plan.json"
```

できあがりは `out/$NAME/out.mp4`（字幕ファイルは `out/$NAME/out.srt`）。

### 3B. パターン B：Claude に「いい写真」を選んでもらう（推奨）

Claude Code で **「$PHOTOS からスライドショーを作って」と頼むだけ**。
[auto-slide スキル](.claude/skills/auto-slide/SKILL.md)が下記を代わりに実行し、要約を提示して承認を待つ。

手で回す場合はこの順:

```bash
autoslide candidates "$PHOTOS" --count 8 --out "out/$NAME"
```

`out/$NAME/candidates_00.jpg`（以降ページ）を開き、使いたい写真の **番号**（#1, #2 …）を控える。

```bash
autoslide pick    "out/$NAME" --set 1,4,6,9,11,14,15,18   # ← 控えた番号に置き換える
autoslide plan    "$PHOTOS" --out "out/$NAME" --selection "out/$NAME/selection.json"
autoslide summary "out/$NAME/plan.json"
```

要約を読み、直したいところがあれば「4.」へ。良ければ:

```bash
autoslide approve "out/$NAME/plan.json"
autoslide render  "out/$NAME/plan.json"
```

### 4. 要約を見て直す（承認の前）

`out/$NAME/plan.json` はただのテキストファイル。よくある直しかた:

| やりたいこと | plan.json のどこを直すか |
|---|---|
| タイトルを変える | `"title"` の文言 |
| 全体のトーン／BGM ムード | `"overall_tone"` / `"mood"` |
| 写真の順番を変える | `"order"` の並びを入れ替え |
| 写真を 1 枚外す | `"order"` からそのパスを削除（`"chapters"` の該当 `images` からも） |
| 章（区切り）を消す | `"chapters"` から該当ブロックを削除 |
| キャプション文言 | `"captions"` の `"<写真パス>": "文言"` |
| キャプションの日付を消す | `"caption_sub"` のその値を `""` に |
| 色補正を弱く／切る | `"color"` の `"strength"` を `0.1` に（`0` で無効） |
| 露出補正を弱く／切る | `"exposure"` の `"strength"` を `0.1` に（`0` で無効） |
| この 1 枚だけ補正しない | `"color"` / `"exposure"` の `per_image["<パス>"]["disabled"]` を `true` |
| シネマの黒帯をやめる | `"config"` の `"letterbox"` を `false` |
| 字幕を中央バーに戻す | `"config"` の `"caption_style"` を `"bar"` |

直したら毎回この 3 つ:

```bash
autoslide summary "out/$NAME/plan.json"    # 反映を確認
autoslide approve "out/$NAME/plan.json"    # 内容に納得したら承認
autoslide render  "out/$NAME/plan.json"    # 動画化
```

`plan.json` を編集すると承認は自動で外れる。もう一度 `approve` してから `render` する。

### 4.5 再生成時のファイルの残し方

`autoslide render` は既定で `out.mp4` を**上書き**する。前のバージョンも残したい場合は
`--out` で別名を指定する。

| パターン | コマンド例 | 使う場面 |
|---|---|---|
| **A. 上書き**(既定) | `autoslide render "out/$NAME/plan.json"` | 微調整の試行錯誤中。前バージョンを残す必要がない |
| **B. 別ファイルで保存** | `autoslide render "out/$NAME/plan.json" --out "out/$NAME/out_v2.mp4"` | 前後を比較したい／複数案を残して選びたい／納品後の差し替え |

パターン B を使うときは `out_v2.mp4`, `out_v3.mp4` ... と連番にする。
字幕ファイルは指定した mp4 と同じ幹名(例: `out_v2.srt`)で出力される。

### 5. 困ったとき

| 症状 | 対処（コピペ） |
|---|---|
| `ffmpeg not found` | `brew install ffmpeg` |
| `doctor` が CJK フォントで FAIL | [assets/fonts/](assets/fonts/) に Noto Sans CJK JP を置く（[assets/fonts/README.md](assets/fonts/README.md)）。macOS のシステムフォントがあれば自動で使う |
| HEIC がスキップされると出る | `uv pip install -e '.[heic]'`（macOS は `sips` でも自動変換される） |
| 候補が少ない／多い | `autoslide candidates "$PHOTOS" --count 8 --out "out/$NAME" --pool-factor 3.5` |
| 最初からやり直す | `rm -rf "out/$NAME"` してから「2.」へ |

---

## 使い方

### A. アルゴリズムだけで一括（既定は要約提示で停止）

```bash
autoslide run /Volumes/HDD/2025-11-グレーバランスチェック-1 --count 8
```

`scan → group → select → propose → music` まで走り、**人間可読な要約を出して停止**する。
内容を確認して `autoslide approve` → `autoslide render`。
CI などで確認を挟まず一括生成したいときだけ `--yes` を付ける。

### B. Claude 自身に「いい写真」を選ばせる（推奨）

Claude Code で「このフォルダからスライドショーを作って」と頼むと [auto-slide スキル](.claude/skills/auto-slide/SKILL.md)が起動し、次を実行する:

```bash
autoslide doctor                                                        # 環境チェック
autoslide candidates /Volumes/HDD/<folder> --count 8 --out out/mymovie  # 候補プール + 番号付きコンタクトシート
#   → Claude が out/mymovie/candidates_*.jpg を見て、ブレ/露出/構図/重複を判断し 8 枚選ぶ
autoslide pick   out/mymovie --set 1,4,6,9,11,14,15,18                   # 選んだ番号 → selection.json
autoslide plan   /Volumes/HDD/<folder> --out out/mymovie --selection out/mymovie/selection.json
#   → plan.json（未承認）+ 人間可読な要約。Claude が要約を提示し、自然言語フィードバックで plan.json を直す
autoslide summary out/mymovie/plan.json                                 # 直すたびに再提示
autoslide approve out/mymovie/plan.json                                 # ユーザーが OK と言ってから
autoslide render  out/mymovie/plan.json --out out/mymovie/out.mp4       # out.mp4 + out.srt
```

アルゴリズムが候補を `count × 2.5` 枚（`--pool-factor` で調整）に絞り、その中から Claude が最終選択する。
候補プールにない写真は選べない。`ANTHROPIC_API_KEY` が無ければ決定的なフォールバック案になる。

### C. 手動で段階実行

```bash
autoslide scan    /Volumes/HDD/<folder>
autoslide plan    /Volumes/HDD/<folder> --count 8 --out out/mymovie
autoslide summary out/mymovie/plan.json     # 要約を見て plan.json を手直し
autoslide approve out/mymovie/plan.json
autoslide render  out/mymovie/plan.json --out out/mymovie/out.mp4
```

主なオプション:

| オプション | 意味 |
|---|---|
| `--count N` | スライドに使う枚数（合計） |
| `--aspect 16:9\|1:1\|9:16` | 出力比率 |
| `--seconds 4` | 1 枚の表示秒数 |
| `--fit auto\|cover\|contain-blur` | 写真の正規化方法（既定 auto=向きが逆の写真だけ contain-blur） |
| `--grouping folder\|exif\|auto` | 章分けの方針 |
| `--proposal FILE` | （plan）タイトル/感情/キャプションの JSON を渡す。API を呼ばない |
| `--selection FILE` | （plan）`pick` が作った selection.json を使い、アルゴリズム選択を飛ばす |
| `--refresh-proposal` | （plan）Vision 結果キャッシュを無視して問い合わせ直す |
| `--pool-factor 2.5` | （candidates）候補プールを `count` の何倍にするか |
| `--no-llm` | （plan/run）API を呼ばずフォールバック案で作る（`--proposal` 指定時は不要） |
| `--music path.mp3` | BGM を明示指定（music_index を無視） |
| `--force` | （render）未承認でも生成する。対話レビューを飛ばす（非推奨） |
| `--yes` | （run）確認を挟まず承認して render まで通す |
| `--config config.toml` | 設定ファイル差し替え |

`plan.json` は人が確認・編集できる中間成果物。`title` / `overall_tone` / `mood` / `chapters` /
`order` / `captions` / `caption_sub`（日付）/ `color.strength` / `exposure.strength` /
`{color,exposure}.per_image[].disabled` / `config.letterbox` / `config.caption_style` を
書き換えて `autoslide summary` で再確認 → `autoslide approve` → `autoslide render` し直せる。
`captions` は焼き込み主文と `out.srt` の唯一の文言源（`caption_sub` の日付は SRT に入らない）。

## BGM ライブラリ

`assets/music/<mood>/*.mp3` に音源を置き、`assets/music_index.json` に登録する:

```json
[
  {"file": "calm/quiet_morning.mp3", "mood": "calm", "energy": 0.3,
   "bpm": 70, "duration": 128.0, "license": "CC-BY 4.0 / Artist Name"}
]
```

`license` が空のエントリは選曲されない（投稿・配布を想定するため）。mood は
`calm / nostalgic / melancholic / joyful / upbeat / dramatic`。

## 設定（config.toml）

`seconds_per_slide`, `transition_seconds`, `title_card_seconds`, `aspect`, `fps`,
`time_gap_minutes` / `geo_radius_meters` / `phash_hamming_max`（グループ分割・近重複）,
`grouping_mode`（folder/exif/auto）, `fit_mode`（auto/cover/contain-blur）, `group_scale_tolerance`,
`chapter_dividers` / `chapter_divider_seconds`（章の区切りスライド）,
`caption_style`（lower-left/bar）/ `caption_date_always` / `letterbox` / `letterbox_ratio`（シネマ字幕・黒帯）,
`color_correct` / `color_strength` / `color_sat_floor` / `color_auto_atten`（色補正）,
`exposure_correct` / `exposure_strength` / `exposure_hi_thresh` / `exposure_lo_thresh`（露出補正）,
`burn_captions`, `ken_burns`, `audio_fade_in/out`, `audio_lufs`, `llm_model`, `seed`。

## 補足・既知の制約

- 類似度ベクトルは「16×16 グレースケール構造 + HSV ヒストグラム」の軽量特徴。より意味的な選択が要るなら
  [features.py](autoslide/features.py) の `embedding(path) -> np.ndarray` を CLIP 等に差し替える（他は変更不要）。
- クロスフェードのため各スライドの単独表示は実質 `seconds_per_slide − transition` 秒。
  完全に 4 秒見せたい場合は `transition_seconds = 0` に。
- 文字は [assets/fonts/](assets/fonts/) の同梱フォント（無ければシステムの CJK フォント）で PNG に焼く。
  どちらも無ければ `autoslide doctor` / `render` が明示エラーで止まる。
- `ken_burns` は config にあるが未実装（プレースホルダ）。動きの演出は非ゴール。
- 構図スコア（`comp`）は勾配重心が黄金比の交点／ラインに近いか等を見る幾何ヒューリスティック。
  美的モデルではないので目安として使い、最終判断は実際の画で。
- 露出補正は輝度ヒストグラムのクリップ量から控えめな levels 変換をかけるだけ。
  局所的なコントラスト復元（トーンマッピング）はしない。
- HEIC は `pillow-heif`（`.[heic]`）が無ければ macOS の `sips`、次に `heif-convert`(libheif) で
  作業コピー（`.autoslide_cache/heic_jpeg/`）を作って処理する。原本は書き換えない。
- テスト: `.venv/bin/python -m pytest -q`

## 次の実装（PLAN.md）

- M6 の高度化: 感情の自動判定（CLIP ゼロショット）を LLM 無しでも動くように
- M8: X へのチャンクアップロード投稿（従量課金・dry-run 既定）
