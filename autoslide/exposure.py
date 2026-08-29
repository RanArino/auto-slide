"""露出補正: 白とびのロールオフと黒つぶれのリフト。

グレーワールド色補正([color.py])と同じ枠組み。輝度ヒストグラムから
クリップ量を測り、控えめな levels 変換(black_out / white_out / gamma)を返す。
ブラックポイントとホワイトポイントは保護する(眠い画にしすぎない)。
strength(0〜1)で恒等方向へスケール。既定は控えめ。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

BLACK_OUT_MAX = 22          # シャドーリフトの上限(これ以上黒を持ち上げない)
WHITE_OUT_MIN = 230         # ハイライト圧縮の下限(これ以上白を落とさない)
_K_HI = 60.0                # 白とび→white_out 引き下げ量の係数
_K_LO = 55.0                # 黒つぶれ→black_out 持ち上げ量の係数


@dataclass
class ExposureStats:
    black_clip: float       # 0〜1
    white_clip: float       # 0〜1
    p01: float              # 輝度パーセンタイル(0〜255)
    p50: float
    p99: float

    def as_tuple(self) -> tuple[float, float, float, float, float]:
        return (self.black_clip, self.white_clip, self.p01, self.p50, self.p99)


@dataclass
class ToneResult:
    black_out: float        # 出力の黒レベル(0〜255)
    white_out: float        # 出力の白レベル(0〜255)
    gamma: float
    skipped: bool
    attenuated: bool

    @property
    def is_noop(self) -> bool:
        return self.skipped or (
            self.black_out < 0.5 and self.white_out > 254.5 and abs(self.gamma - 1.0) < 1e-3
        )


def measure(img: Image.Image) -> ExposureStats:
    rgb = img.convert("RGB")
    rgb.thumbnail((256, 256), Image.LANCZOS)
    arr = np.asarray(rgb, dtype=np.float32)
    luma = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    flat = luma.reshape(-1)
    return ExposureStats(
        black_clip=float(np.mean(flat <= 4.0)),
        white_clip=float(np.mean(flat >= 251.0)),
        p01=float(np.percentile(flat, 1)),
        p50=float(np.percentile(flat, 50)),
        p99=float(np.percentile(flat, 99)),
    )


def measure_path(path: str | Path) -> ExposureStats:
    with Image.open(path) as im:
        im.load()
        return measure(im)


def tone_adjustment(
    stats: ExposureStats,
    strength: float,
    *,
    hi_thresh: float = 0.02,
    lo_thresh: float = 0.02,
) -> ToneResult:
    strength = max(0.0, min(1.0, strength))
    black_out, white_out, gamma = 0.0, 255.0, 1.0

    blown = stats.white_clip > hi_thresh
    crushed = stats.black_clip > lo_thresh or (stats.p01 < 6.0 and stats.p50 < 110.0)
    well_exposed = (
        not blown and not crushed and 40.0 < stats.p50 < 200.0
        and stats.white_clip < hi_thresh and stats.black_clip < lo_thresh
    )
    if strength <= 0.0 or well_exposed:
        return ToneResult(0.0, 255.0, 1.0, skipped=True, attenuated=False)

    if blown:
        white_out = 255.0 - _K_HI * strength * min(1.0, stats.white_clip / 0.15)
    if crushed:
        black_out = _K_LO * strength * min(1.0, max(stats.black_clip, 0.02) / 0.15)
        gamma = 1.0 - 0.25 * strength

    # ブラック/ホワイトポイント保護
    attenuated = black_out > BLACK_OUT_MAX or white_out < WHITE_OUT_MIN
    black_out = min(black_out, BLACK_OUT_MAX)
    white_out = max(white_out, WHITE_OUT_MIN)

    if black_out < 0.5 and white_out > 254.5 and abs(gamma - 1.0) < 1e-3:
        return ToneResult(0.0, 255.0, 1.0, skipped=True, attenuated=False)
    return ToneResult(round(black_out, 2), round(white_out, 2), round(gamma, 4),
                      skipped=False, attenuated=attenuated)


def _lut(res: ToneResult) -> np.ndarray:
    i = np.arange(256, dtype=np.float32) / 255.0
    out = res.black_out + (res.white_out - res.black_out) * np.power(i, res.gamma)
    return np.clip(out, 0, 255).astype(np.uint8)


def apply_tone(img: Image.Image, res: ToneResult) -> Image.Image:
    rgb = img.convert("RGB")
    if res.is_noop:
        return rgb
    table = _lut(res).tolist()
    return rgb.point(table * 3)   # R, G, B に同一 LUT
