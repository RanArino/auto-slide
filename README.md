# auto-slide

HDD の写真フォルダから、AI がスライドショー動画（mp4）を自動生成する。
グルーピング → 枚数選択（場所・類似を考慮）→ 構成/タイトル/感情の提案 → BGM 選定 → レンダリングまで。

> このリポジトリは **動画生成まで** を実装。X 投稿は [PLAN.md](PLAN.md) の M8（未実装）。

## できること

- フォルダを再帰走査し、EXIF（撮影日時・GPS・機種）を読んで索引化（SQLite キャッシュ、増分）
- **グルーピング**: 撮影時刻のギャップで分割 → 各区間を GPS の近接でさらに分割（GPS 無しは時刻の近さで代理）
- **候補の絞り込み**: pHash で連写を 1 枚に間引き → グループへ枚数配分（平方根重みで偏り抑制）→ グループ内は farthest-point sampling で見た目が散るように選ぶ。`--seed` 固定で決定的
- **最終選択**: アルゴリズムのまま（`run`）か、候補プール（`candidates`）から **Claude が実際に写真を見て**選ぶ（`pick`）かを選べる
- **提案（タイトル・感情・キャプション）**: Claude Code のスキル経由なら、写真を見ているセッション自身が `proposal.json` に記入する（**API キー不要**）。ヘッドレスで回すときは `ANTHROPIC_API_KEY` があれば API が、無ければ決定的なフォールバック（フォルダ名＋日付、mood=calm）
- **BGM**: `assets/music_index.json` から mood 一致 → energy 近さ → 長さで選曲。トリム＋フェード＋ラウドネス正規化して合成。音源が無ければ無音
- **レンダリング**: タイトルカード + 1 枚 `seconds_per_slide` 秒（既定 4）+ クロスフェード。H.264 / yuv420p / +faststart の mp4。`--aspect` は `16:9` / `1:1` / `9:16`

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

## 使い方

### A. アルゴリズムだけで一括

```bash
autoslide run /Volumes/HDD/2025-11-グレーバランスチェック-1 --count 8
```

`scan → group → select（pHash重複除去＋farthest-point）→ propose → music → render` を一発。

### B. Claude 自身に「いい写真」を選ばせる（推奨）

Claude Code で「このフォルダからスライドショーを作って」と頼むと [auto-slide スキル](.claude/skills/auto-slide/SKILL.md)が起動し、次を実行する:

```bash
autoslide candidates /Volumes/HDD/<folder> --count 8 --out out/mymovie   # 候補プール + 番号付きコンタクトシート
#   → Claude が out/mymovie/candidates_*.jpg を見て、ブレ/露出/構図/重複を判断し 8 枚選ぶ
autoslide pick   out/mymovie --set 1,4,6,9,11,14,15,18                    # 選んだ番号 → selection.json
#   → Claude が out/mymovie/proposal.template.json を埋めて proposal.json として保存
autoslide plan   /Volumes/HDD/<folder> --out out/mymovie \
                 --selection out/mymovie/selection.json --proposal out/mymovie/proposal.json
autoslide render out/mymovie/plan.json --out out/mymovie/out.mp4
```

アルゴリズムが候補を `count × 2.5` 枚（`--pool-factor` で調整）に絞り、その中から Claude が最終選択する。
候補プールにない写真は選べない。`--proposal` を渡すので API は一切呼ばない。

### C. 手動で段階実行

```bash
autoslide scan   /Volumes/HDD/<folder>
autoslide plan   /Volumes/HDD/<folder> --count 8 --out out/mymovie
autoslide render out/mymovie/plan.json --out out/mymovie/out.mp4
```

主なオプション:

| オプション | 意味 |
|---|---|
| `--count N` | スライドに使う枚数（合計） |
| `--aspect 16:9\|1:1\|9:16` | 出力比率 |
| `--seconds 4` | 1 枚の表示秒数 |
| `--proposal FILE` | （plan）タイトル/感情/キャプションの JSON を渡す。API を呼ばない |
| `--selection FILE` | （plan）`pick` が作った selection.json を使い、アルゴリズム選択を飛ばす |
| `--pool-factor 2.5` | （candidates）候補プールを `count` の何倍にするか |
| `--no-llm` | （plan/run）API を呼ばずフォールバック案で作る（`--proposal` 指定時は不要） |
| `--music path.mp3` | BGM を明示指定（music_index を無視） |
| `--config config.toml` | 設定ファイル差し替え |

`plan.json` は人が確認・編集できる中間成果物。タイトルや `order`、`captions` を直接書き換えて `autoslide render` し直せる。

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
`time_gap_minutes`（グループ分割）, `geo_radius_meters`（地点クラスタ半径）,
`phash_hamming_max`（近重複判定）, `burn_captions`, `ken_burns`,
`audio_fade_in/out`, `audio_lufs`, `llm_model`, `seed`。

## 補足・既知の制約

- 類似度ベクトルは「16×16 グレースケール構造 + HSV ヒストグラム」の軽量特徴。より意味的な選択が要るなら
  [features.py](autoslide/features.py) の `embedding(path) -> np.ndarray` を CLIP 等に差し替える（他は変更不要）。
- クロスフェードのため各スライドの単独表示は実質 `seconds_per_slide − transition` 秒。
  完全に 4 秒見せたい場合は `transition_seconds = 0` に。
- `ken_burns` は config にあるが未実装（プレースホルダ）。
- キャプションは 15 文字程度を前提。長文は折り返さず溢れることがある。
- テスト: `.venv/bin/python -m pytest -q`

## 次の実装（PLAN.md）

- M6 の高度化: 感情の自動判定（CLIP ゼロショット）を LLM 無しでも動くように
- M8: X へのチャンクアップロード投稿（従量課金・dry-run 既定）
