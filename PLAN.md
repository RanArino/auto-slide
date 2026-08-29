# auto-slide 実装プラン

HDD 上の写真フォルダから、AI が自動でスライドショー動画（mp4）を作り、構成・タイトル・音楽を提案し、X に投稿するまでを行うパイプライン。

---

## 1. 全体像

```
scan  ──▶  features  ──▶  grouping  ──▶  selection  ──▶  proposal(LLM)  ──▶  music  ──▶  render  ──▶  post(X)
 |          |               |             |               |                  |          |          |
索引化      CLIP埋め込み     時間/場所/    N枚を          コンタクトシート    感情→曲     ffmpeg     チャンク
EXIF抽出   pHash/画質      類似で分割    場所・類似で    →Claude Vision     選定/整音   4秒/枚     アップロード
サムネ                                  バランス選択    構成/タイトル/感情            mp4      →投稿
```

- 各ステージは独立コマンド。中間成果物（`plan.json` 等）を介して疎結合にする。
- 特徴量・判断結果は SQLite にキャッシュし、再実行を高速化＆決定を再現可能にする（seed 固定）。
- **投稿は既定で dry-run**。`--post` を明示したときだけ実際に X へ出す。

---

## 2. 技術スタック

| 領域 | 採用 | 備考 |
|---|---|---|
| 言語 | Python 3.11+ | 画像/ML/ffmpeg 連携が最短 |
| パッケージ管理 | `uv` または `venv + pip` | |
| CLI | `typer` | サブコマンド構成 |
| EXIF/メタデータ | `exiftool`（subprocess）+ フォールバックで `Pillow`/`exifread` | GPS・撮影日時・機種 |
| 画像類似・意味 | CLIP（`open_clip`、ViT-B/32）| CPU 可、Apple Silicon は MPS。数千枚まで実用 |
| 近重複検出 | `imagehash`（pHash）| バースト撮影の間引き |
| 画質スコア | OpenCV（Laplacian 分散＝ブレ、露出ヒストグラム）| 任意で顔検出 |
| クラスタリング | `scikit-learn`（DBSCAN=地理、KMeans/medoids=類似）| |
| LLM | `anthropic` SDK、`claude-sonnet-5`（Vision）| コンタクトシート画像を渡す |
| 音楽 | ローカルのロイヤリティフリー音源ライブラリ（mood/BPM/energy タグ付き）| ライセンスは投稿前提で要確認 |
| 動画生成 | システム `ffmpeg`（subprocess、filter_complex を自前構築）| moviepy より安定・軽量 |
| X 投稿 | `httpx` で直接叩く（OAuth 1.0a user context）| `POST /2/media/upload` チャンク → `POST /2/tweets` |
| キャッシュ/状態 | SQLite（`sqlite3` 標準ライブラリ）| |
| 設定 | `config.toml` + `.env`（秘密情報）| |

システム依存（Homebrew）: `ffmpeg`, `exiftool`。

---

## 3. データモデル（`cache.db`）

```sql
images(
  path TEXT PRIMARY KEY, folder TEXT, taken_at TEXT, lat REAL, lon REAL,
  camera TEXT, width INT, height INT, phash TEXT, sharpness REAL,
  embedding BLOB,           -- float32 512次元
  thumb_path TEXT, indexed_at TEXT
)
runs(id TEXT PRIMARY KEY, created_at TEXT, folder TEXT, n_per_folder INT,
     seed INT, params_json TEXT)
groups(run_id TEXT, group_id INT, kind TEXT, label TEXT,
       centroid_lat REAL, centroid_lon REAL, t_start TEXT, t_end TEXT)
selections(run_id TEXT, image_path TEXT, group_id INT, order_idx INT, reason TEXT)
proposals(run_id TEXT, title TEXT, mood TEXT, music_track TEXT,
          structure_json TEXT, raw_llm_json TEXT)
```

---

## 4. CLI

```
autoslide scan   <PATH>                 # 画像を索引化・特徴量抽出（増分。既存はskip）
autoslide plan   <PATH> --count N [--seed S] [--aspect 16:9|1:1|9:16]
                                        # グルーピング→選択→LLM提案。plan.json と contact_sheet.png を出力
autoslide render <plan.json>            # out.mp4 を生成
autoslide post   <out.mp4> <plan.json> [--post]   # 既定 dry-run。--post で実投稿
autoslide run    <PATH> --count N [...]  # scan→plan→render→post(dry-run) を一括
```

`plan.json`（人が確認・編集できる中間成果物）:

```json
{
  "run_id": "2026-08-29T13-40-00",
  "source": "/Volumes/HDD/2025-11-グレーバランスチェック-1",
  "count_per_folder": 8,
  "aspect": "16:9",
  "seconds_per_slide": 4,
  "title": "秋の光、グレーの調べ",
  "mood": "calm / nostalgic",
  "music_track": "library/calm/quiet_morning.mp3",
  "groups": [
    { "group_id": 0, "label": "屋内・テストチャート", "images": ["...jpg", "..."] },
    { "group_id": 1, "label": "屋外・公園", "images": ["...jpg"] }
  ],
  "order": ["...jpg", "...jpg", "..."],
  "captions": { "...jpg": "..." },
  "hashtags": ["#写真", "#スライドショー"]
}
```

---

## 5. マイルストーン（各ステップに検証条件）

### M0. 骨組み
1. リポジトリ雛形（`pyproject.toml`, `autoslide/` パッケージ, `typer` エントリ）-> verify: `autoslide --help` が全サブコマンドを表示
2. `config.toml` / `.env.example` 追加、`ffmpeg`/`exiftool` の存在チェック -> verify: 未インストール時に明確なエラー

### M1. scan（索引化）
1. `<PATH>` を再帰走査し JPEG/HEIC を収集（RAW は M9 で判断）-> verify: 既知フォルダの枚数と一致
2. EXIF から `taken_at / lat / lon / camera / w / h` を抽出、GPS は度分秒→10進変換 -> verify: GPS 付き既知ファイルの座標が地図と一致
3. サムネ生成（長辺 512px）と `images` への upsert（増分：`indexed_at` 済みは skip）-> verify: 2 回目実行が数秒で終わる
4. GPS 欠損時は「フォルダ＋撮影時刻の近接」を場所代理指標にする方針をコードコメントで明示

### M2. features（特徴量）
1. CLIP 埋め込みをバッチ計算し `embedding` に保存（float32 512d、L2 正規化）-> verify: 類似写真同士の cos 類似度 > 無関係ペア
2. pHash 計算、ハミング距離 <= 閾値（既定 6）で「near-dup バケット」を形成 -> verify: バースト連写が同一バケットに入る
3. Laplacian 分散でブレスコア、露出の白飛び/黒潰れ率を算出 -> verify: 目視でブレ写真が下位に来る

### M3. grouping（分割）
1. 時間ギャップ分割：隣接ショットの時刻差が閾値（既定 30 分）超で区切る -> verify: 別日の撮影が別グループ
2. 地理クラスタリング：DBSCAN（`eps` ≈ 150m 相当の緯度経度、`haversine`）で場所グループ -> verify: 撮影地点マップと一致
3. グループ内を CLIP 埋め込みで KMeans（k = min(枚数, 目安 6)）してサブシーン化 -> verify: 屋内チャート/屋外など見た目が分かれる
4. グループを時刻昇順で並べ、`groups` に保存

### M4. selection（N 枚選択：場所・類似を意識）
1. near-dup バケットごとに画質最良の 1 枚だけ残す（代表化）-> verify: 重複がプランに出ない
2. 各グループへ N を配分（グループの枚数比 × 場所の多様性で重み付け）-> verify: 特定地点だけに偏らない
3. グループ内選択：品質フィルタ後、**farthest-point sampling**（埋め込み空間で最も離れた点を順に採る）で被りにくい多様な枚数を確定 -> verify: 選択集合の平均ペア類似度が「ランダム選択」より低い
4. 最終順序：グループ順（時刻）→ グループ内は時刻昇順。`selections` に `reason` 付きで保存 -> verify: `plan.json` の `order` が決定的（同 seed で不変）

### M5. proposal（構成・タイトル・感情：LLM）
1. 選択画像のコンタクトシート（格子状モンタージュ PNG、各サムネに番号）を生成 -> verify: 画像が開ける・番号が読める
2. Claude Vision へ送信。プロンプトで JSON 固定出力を要求：`title`（日本語）, `mood`（感情ラベル＋強度）, `structure`（グループごとの狙い・並び替え提案）, `captions`（番号→短文）, `music_style`, `hashtags` -> verify: スキーマ検証を通過、再試行 1 回で安定
3. LLM が並び替えを提案したら `order` に反映（画像追加はしない＝選択集合は固定）-> verify: 提案後も枚数が N のまま
4. `proposals` に生レスポンスごと保存

### M6. music（感情反映）
1. `assets/music/<mood>/` にタグ付き音源を配置、`music_index.json`（mood, bpm, energy, duration, license）を用意 -> verify: 各 mood に最低 1 曲
2. `mood` 文字列を正規化（calm / nostalgic / upbeat / dramatic / melancholic …）して曲候補を絞り、energy とスライド総尺に近いものを選ぶ -> verify: 想定 mood で期待カテゴリの曲が返る
3. 曲を総尺（= 枚数 × 4s ＋ タイトル/末尾）に合わせてトリム、頭 1s フェードイン・尾 2s フェードアウト、`loudnorm` で -14 LUFS -> verify: 書き出し音声の長さ・ラウドネスが規定値
4. 音源が無い mood はフォールバック曲＋警告（動画生成は止めない）

### M7. render（ffmpeg で mp4）
1. 各画像を `aspect` に letterbox/crop（既定は cover crop）、必要なら軽い Ken Burns（`zoompan`）-> verify: 出力解像度が 1920x1080（16:9 時）で歪みなし
2. スライド 4s、隣接間 `xfade`（crossfade 0.75s）で連結 -> verify: フレーム総数 ≈ (N × 4 − 交差分) × fps
3. 先頭にタイトルカード（`drawtext`、フォント同梱）、任意で各スライドに caption 焼き込み -> verify: 文字が枠内・改行される
4. 音声を `amix`/`-shortest` で multiplex、H.264 `yuv420p`、`+faststart`、CRF 20 -> verify: `ffprobe` で映像+音声トラック確認、QuickTime で再生可
5. X 向け制約チェック：長さ ≤ 2:20、サイズ ≤ 512MB、fps 30、寸法上限 -> verify: 制約超過なら明示エラーで停止

### M8. post（X 投稿）
1. `.env` から OAuth 1.0a キー読込、`POST /2/media/upload` を INIT→APPEND(5MB分割)→FINALIZE -> verify: `media_id` が返る
2. `processing_info` を STATE=succeeded までポーリング（指数バックオフ）-> verify: failed 時にエラー詳細を表示
3. `POST /2/tweets` に `text`（title ＋ hashtags）＋ `media.media_ids` -> verify: **dry-run では本文とペイロードを表示するだけ**、`--post` で実 ID を返す
4. 投稿結果（tweet id, url）を `runs` に記録
5. レート/課金メモをコメントで明記（→ §7）

### M9. 一括実行と仕上げ
1. `autoslide run` で scan→plan→render→post(dry-run) を連結、途中失敗で停止し再開可能に -> verify: HDD の実フォルダ 1 つで通し成功
2. RAW 対応の要否を確定（必要なら `rawpy` で現像を M2 前段に追加）
3. ログ（`rich`）、`--verbose`、失敗時の中間ファイル保持
4. README（前提インストール、キー設定、使用例）

---

## 6. 既定パラメータ（`config.toml`）

```toml
seconds_per_slide   = 4
transition_seconds  = 0.75
aspect              = "16:9"
title_card_seconds  = 2.5
time_gap_minutes    = 30       # グループ分割
geo_eps_meters      = 150      # 地理クラスタ
phash_hamming_max   = 6        # 近重複
ken_burns           = true
audio_lufs          = -14.0
llm_model           = "claude-sonnet-5"
seed                = 42
```

---

## 7. 要確認・判断が必要な点

- **HDD のフォルダ構造**：「1 フォルダ = 1 撮影/イベント」で確定か。ネストした下位フォルダは 1 スライドショーに束ねる？分ける？
- **ファイル形式**：JPEG/HEIC のみか、RAW（CR2/NEF/ARW）も対象か。RAW は現像処理が増える。
- **アスペクト比**：X タイムライン想定は 16:9／1:1／9:16 のどれを既定にするか。
- **音楽ライセンス**：手持ちのロイヤリティフリー音源ライブラリはあるか。無ければ調達先（X 投稿＝商用配信相当なのでライセンス条項の確認が必須）。
- **感情の決め方**：自動（CLIP ゼロショット or Claude 判定）に任せるか、run ごとに人が mood を指定するか。
- **投稿の自動化度**：常に人がレビューしてから投稿か、条件を満たせば完全自動投稿まで許すか。
- **X API 前提**：2026年2月6日以降、新規開発者向け無料枠は廃止され従量課金（投稿 1 件 $0.015、リンク含むと $0.20、読み取り $0.005/件・月200万上限）。既存の Basic $200/月・Pro $5,000/月は継続契約者のみ。動画は必ず `POST /2/media/upload` のチャンクアップロード経由。アカウントの現行プランと、月あたり投稿本数の想定を確認したい。

---

## 8. リポジトリ構成（予定）

```
auto-slide/
  pyproject.toml
  config.toml
  .env.example
  README.md
  autoslide/
    __init__.py
    cli.py            # typer エントリ
    scan.py           # 走査・EXIF・サムネ
    features.py       # CLIP / pHash / 画質
    grouping.py       # 時間・地理・類似クラスタ
    selection.py      # N枚選択（FPS・場所配分）
    proposal.py       # コンタクトシート生成・Claude 呼び出し
    music.py          # mood→曲・整音
    render.py         # ffmpeg filter_complex 構築
    post.py           # X media upload / tweets
    db.py             # SQLite ラッパ
    config.py
  assets/
    fonts/
    music/<mood>/*.mp3
    music_index.json
  tests/
```

---

## 9. 主なリスクと対応

| リスク | 対応 |
|---|---|
| GPS 情報が無い写真が多い | フォルダ＋時刻近接を場所代理に。地理クラスタは任意扱いにフォールバック |
| CLIP 計算が遅い（大量枚数） | バッチ＋SQLite キャッシュ、`scan` と `plan` を分離、MPS/GPU 自動検出 |
| LLM 出力が不安定 | JSON スキーマ検証＋1 回リトライ＋失敗時は既定タイトルで続行 |
| 音楽ライセンス | 同梱音源に `license` フィールド必須、未確認音源は投稿パイプラインでブロック |
| X API 課金・レート | dry-run 既定、投稿前に想定課金を表示、429 は指数バックオフ |
| ffmpeg フィルタの複雑化 | フィルタグラフ生成をユニットテスト（文字列＋短尺ダミーで実レンダ 1 本） |

---

**出典（X API 現況）:**
- [X (Twitter) API Pricing in 2026: All Tiers — Postproxy](https://postproxy.dev/blog/x-api-pricing-2026/)
- [X API Pricing in 2026: Every Tier Explained (Pay-As-You-Go) — We Are Founders](https://www.wearefounders.uk/the-x-api-price-hike-a-blow-to-indie-hackers/)
- [How to Get X API Key: Complete 2026 Guide — Elfsight](https://elfsight.com/blog/how-to-get-x-twitter-api-key-in-2026/)
