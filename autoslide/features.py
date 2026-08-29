"""画像特徴量。

- embedding : 見た目の類似度用ベクトル(構造16x16 + HSVヒストグラム)。L2正規化済み。
  CLIP 等に差し替える場合はこの関数のシグネチャ (path -> np.ndarray) を保てばよい。
- phash     : 64bit 知覚ハッシュ(近重複=連写の検出用)。
- sharpness : ラプラシアン分散(ブレ検出用)。
"""

from __future__ import annotations

import math

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


_PHI = (0.382, 0.618)


def composition(img: Image.Image) -> float:
    """黄金比／三分割の目安スコア(0〜1、決定的)。

    これは**幾何ヒューリスティック**であって美的モデルではない。
    勾配強度を顕著性の代理として、その重心が黄金分割の交点付近にあるか、
    中心に寄りすぎていないか、φ ライン(地平線など)に整列しているか、
    被写体が画面端で見切れていないかを見る。
    """
    n = 160
    g = np.asarray(img.convert("L").resize((n, n), Image.LANCZOS), dtype=np.float32)
    gy, gx = np.gradient(g)
    sal = np.hypot(gx, gy)
    total = float(sal.sum())
    if total < 1e-6:
        return 0.0
    s = sal / total

    ax = (np.arange(n, dtype=np.float32) + 0.5) / n     # 0〜1
    cx = float((s.sum(axis=0) * ax).sum())
    cy = float((s.sum(axis=1) * ax).sum())

    # 顕著性重心がパワーポイント(4 交点)にどれだけ近いか
    d_gp = min(math.hypot(cx - fx, cy - fy) for fx in _PHI for fy in _PHI)
    point_term = math.exp(-((d_gp / 0.16) ** 2))       # 1 = 交点上

    # 中心から適度に外す(中央寄り／端寄りの両方を減点)
    d_c = math.hypot(cx - 0.5, cy - 0.5)
    center_term = min(1.0, d_c / 0.18) * (1.0 - min(1.0, max(0.0, (d_c - 0.40) / 0.12)))

    # φ ライン帯への整列(水平線など)
    band = 0.06
    col_mass = s.sum(axis=0)
    row_mass = s.sum(axis=1)
    line_term = 0.0
    for f in _PHI:
        line_term += float(col_mass[np.abs(ax - f) < band].sum())
        line_term += float(row_mass[np.abs(ax - f) < band].sum())
    line_term = min(1.0, line_term / 1.2)

    # 外周 5% に顕著性が寄っている＝見切れ、を減点
    m = max(1, int(0.05 * n))
    border = float(s[:m].sum() + s[-m:].sum() + s[:, :m].sum() + s[:, -m:].sum())
    edge_term = 1.0 - min(1.0, border * 1.5)

    score = 0.40 * point_term + 0.22 * center_term + 0.23 * line_term + 0.15 * edge_term
    return float(np.clip(score, 0.0, 1.0))


def compute_features(
    thumb_path: str,
) -> tuple[int, float, np.ndarray, tuple[float, float, float, float], float]:
    """(phash, sharpness, embedding, (mean_r,g,b, sat), composition) を返す。"""
    from .color import measure

    with Image.open(thumb_path) as img:
        img.load()
        return (phash(img), sharpness(img), embedding(img),
                measure(img).as_tuple(), composition(img))
