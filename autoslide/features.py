"""画像特徴量。

- embedding : 見た目の類似度用ベクトル(構造16x16 + HSVヒストグラム)。L2正規化済み。
  CLIP 等に差し替える場合はこの関数のシグネチャ (path -> np.ndarray) を保てばよい。
- phash     : 64bit 知覚ハッシュ(近重複=連写の検出用)。
- sharpness : ラプラシアン分散(ブレ検出用)。
"""

from __future__ import annotations

import numpy as np
from PIL import Image

_DCT_N = 32
_HIST_BINS = (8, 4, 4)  # H, S, V


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    m[0, :] *= np.sqrt(1.0 / n)
    m[1:, :] *= np.sqrt(2.0 / n)
    return m


_DCT = _dct_matrix(_DCT_N)


def phash(img: Image.Image) -> int:
    g = np.asarray(img.convert("L").resize((_DCT_N, _DCT_N), Image.LANCZOS), dtype=np.float64)
    coeff = _DCT @ g @ _DCT.T
    block = coeff[:8, :8]
    med = np.median(block)
    bits = (block > med).flatten()
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def sharpness(img: Image.Image) -> float:
    g = np.asarray(img.convert("L").resize((128, 128), Image.LANCZOS), dtype=np.float64)
    lap = (
        -4 * g
        + np.roll(g, 1, 0)
        + np.roll(g, -1, 0)
        + np.roll(g, 1, 1)
        + np.roll(g, -1, 1)
    )
    return float(lap[1:-1, 1:-1].var())


def embedding(img: Image.Image) -> np.ndarray:
    rgb = img.convert("RGB")

    # 構造成分: 16x16 グレースケール
    struct = np.asarray(rgb.resize((16, 16), Image.LANCZOS).convert("L"), dtype=np.float32).flatten()
    struct -= struct.mean()
    n = np.linalg.norm(struct)
    struct = struct / n if n > 1e-6 else struct

    # 色成分: HSV 3次元ヒストグラム
    hsv = np.asarray(rgb.resize((64, 64), Image.LANCZOS).convert("HSV"), dtype=np.float32) / 255.0
    hist, _ = np.histogramdd(
        hsv.reshape(-1, 3),
        bins=_HIST_BINS,
        range=[(0, 1), (0, 1), (0, 1)],
    )
    hist = hist.flatten().astype(np.float32)
    n = np.linalg.norm(hist)
    hist = hist / n if n > 1e-6 else hist

    vec = np.concatenate([struct, hist]).astype(np.float32)
    n = np.linalg.norm(vec)
    return vec / n if n > 1e-6 else vec


def compute_features(thumb_path: str) -> tuple[int, float, np.ndarray]:
    with Image.open(thumb_path) as img:
        img.load()
        return phash(img), sharpness(img), embedding(img)
