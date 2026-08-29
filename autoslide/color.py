"""グレーワールド白色補正。

各チャンネルの平均をグレー基準へ寄せるゲインを求め、控えめに適用する。
誤爆を避けるためのガード:
  - ゲインは [GAIN_MIN, GAIN_MAX] にクランプ
  - 平均彩度が低い画像はスキップ(色被りの判断が不安定なため)
  - ゲイン偏差が大きすぎる場合は自動で弱める
補正強度 strength(0〜1)で恒等方向へ線形補間する。既定は弱め。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

GAIN_MIN, GAIN_MAX = 0.7, 1.4


@dataclass
class ColorStats:
    mean_r: float
    mean_g: float
    mean_b: float
    mean_sat: float  # 0〜1

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.mean_r, self.mean_g, self.mean_b, self.mean_sat)


@dataclass
class GainResult:
    gains: tuple[float, float, float]
    skipped: bool          # 低彩度などで補正しない
    attenuated: bool        # 自動抑制がかかった

    @property
    def is_noop(self) -> bool:
        return self.skipped or all(abs(g - 1.0) < 1e-3 for g in self.gains)


def measure(img: Image.Image) -> ColorStats:
    """小さめに縮小して平均 RGB と平均彩度を測る。"""
    rgb = img.convert("RGB")
    rgb.thumbnail((256, 256), Image.LANCZOS)
    arr = np.asarray(rgb, dtype=np.float32) / 255.0
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    return ColorStats(
        mean_r=float(mean_rgb[0]),
        mean_g=float(mean_rgb[1]),
        mean_b=float(mean_rgb[2]),
        mean_sat=float(sat.mean()),
    )


def measure_path(path: str | Path) -> ColorStats:
    with Image.open(path) as im:
        im.load()
        return measure(im)


def gray_world_gains(
    stats: ColorStats,
    strength: float,
    *,
    sat_floor: float = 0.08,
    auto_atten: float = 0.25,
) -> GainResult:
    strength = max(0.0, min(1.0, strength))
    means = np.array([stats.mean_r, stats.mean_g, stats.mean_b], dtype=np.float64)

    if strength <= 0.0 or stats.mean_sat < sat_floor or float(means.min()) < 1e-4:
        return GainResult((1.0, 1.0, 1.0), skipped=True, attenuated=False)

    gray = float(means.mean())
    raw = gray / means                                   # 生ゲイン
    gain = 1.0 + (raw - 1.0) * strength                  # 恒等方向へ補間

    # 強すぎる補正の自動抑制
    max_dev = float(np.max(np.abs(gain - 1.0)))
    attenuated = False
    if max_dev > auto_atten:
        gain = 1.0 + (gain - 1.0) * (auto_atten / max_dev)
        attenuated = True

    gain = np.clip(gain, GAIN_MIN, GAIN_MAX)
    return GainResult(tuple(round(float(g), 4) for g in gain), skipped=False, attenuated=attenuated)


def apply_gains(img: Image.Image, gains: tuple[float, float, float]) -> Image.Image:
    if all(abs(g - 1.0) < 1e-3 for g in gains):
        return img.convert("RGB")
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    arr *= np.array(gains, dtype=np.float32).reshape(1, 1, 3)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def preview_pair(src: str | Path, gains: tuple[float, float, float], dst: str | Path,
                 max_edge: int = 480) -> Path:
    """before | after を横並びにした JPEG を書く(提案確認用)。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im.load()
        before = im.convert("RGB")
    before.thumbnail((max_edge, max_edge), Image.LANCZOS)
    after = apply_gains(before, gains)
    w, h = before.size
    canvas = Image.new("RGB", (w * 2 + 8, h), (12, 12, 14))
    canvas.paste(before, (0, 0))
    canvas.paste(after, (w + 8, 0))
    canvas.save(dst, "JPEG", quality=88)
    return dst
