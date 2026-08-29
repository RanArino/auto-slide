"""設定の読み込み。config.toml + 既定値をマージして Config を返す。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

ASPECTS = {
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "9:16": (1080, 1920),
}


@dataclass
class Config:
    seconds_per_slide: float = 4.0
    transition_seconds: float = 0.75
    title_card_seconds: float = 2.5
    aspect: str = "16:9"
    fps: int = 30

    time_gap_minutes: float = 30.0
    geo_radius_meters: float = 150.0
    phash_hamming_max: int = 6

    # グルーピング方針: "folder"(フォルダ整理を尊重) / "exif"(時刻・GPSで分割) / "auto"
    grouping_mode: str = "auto"

    # 写真の正規化: "auto"(キャンバスと向きが逆の写真だけ contain-blur、他は cover) /
    # "cover"(キャンバスいっぱいにクロップ) / "contain-blur"(収めてぼかし背景)
    fit_mode: str = "auto"
    # 同一グループ内で cover 拡大率を中央値の ±この割合にそろえる
    group_scale_tolerance: float = 0.15

    ken_burns: bool = False
    burn_captions: bool = True

    # キャプションの見た目: "lower-left"(下帯左・主文+日付) / "bar"(下いっぱいの中央バー)
    caption_style: str = "lower-left"
    caption_date_always: bool = False  # 本文が無くても日付だけ焼く
    # シネマスコープの黒帯(レターボックス)
    letterbox: bool = True
    letterbox_ratio: float = 2.39

    # 章ごとの区切りスライド
    chapter_dividers: bool = True
    chapter_divider_seconds: float = 2.0

    # グレーワールド白色補正
    color_correct: bool = True
    color_strength: float = 0.3        # 0〜1。既定は控えめ
    color_sat_floor: float = 0.08      # 平均彩度がこれ未満なら補正しない
    color_auto_atten: float = 0.25     # ゲイン偏差がこれを超えたら自動で弱める

    # 露出補正(白とびのロールオフ / 黒つぶれのリフト)
    exposure_correct: bool = True
    exposure_strength: float = 0.35    # 0〜1。既定は控えめ
    exposure_hi_thresh: float = 0.02   # 白飛び画素率がこれを超えたらハイライトを引く
    exposure_lo_thresh: float = 0.02   # 黒潰れ画素率がこれを超えたらシャドーを持ち上げる

    audio_fade_in: float = 1.0
    audio_fade_out: float = 2.0
    audio_lufs: float = -14.0

    llm_model: str = "claude-sonnet-5"
    seed: int = 42

    GROUPING_MODES = ("folder", "exif", "auto")
    FIT_MODES = ("cover", "contain-blur", "auto")
    CAPTION_STYLES = ("lower-left", "bar")

    @property
    def resolution(self) -> tuple[int, int]:
        if self.aspect not in ASPECTS:
            raise ValueError(f"aspect は {list(ASPECTS)} のいずれか: {self.aspect!r}")
        return ASPECTS[self.aspect]

    def __post_init__(self) -> None:
        if self.grouping_mode not in self.GROUPING_MODES:
            raise ValueError(
                f"grouping_mode は {list(self.GROUPING_MODES)} のいずれか: {self.grouping_mode!r}"
            )
        if self.fit_mode not in self.FIT_MODES:
            raise ValueError(f"fit_mode は {list(self.FIT_MODES)} のいずれか: {self.fit_mode!r}")
        if self.caption_style not in self.CAPTION_STYLES:
            raise ValueError(
                f"caption_style は {list(self.CAPTION_STYLES)} のいずれか: {self.caption_style!r}"
            )
        if self.letterbox_ratio <= 1.0:
            raise ValueError(f"letterbox_ratio は 1.0 より大きい値: {self.letterbox_ratio!r}")
        self.color_strength = max(0.0, min(1.0, self.color_strength))
        self.exposure_strength = max(0.0, min(1.0, self.exposure_strength))

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        data: dict = {}
        if path:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"config が見つかりません: {p}")
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"config に未知のキー: {sorted(unknown)}")
        return cls(**data)
