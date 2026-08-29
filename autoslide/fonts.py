"""日本語が出るフォントを探す。見つからなければ PIL の既定フォント。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


@lru_cache(maxsize=32)
def font_path() -> str | None:
    for p in _CANDIDATES:
        if Path(p).exists():
            return p
    return None


@lru_cache(maxsize=64)
def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    p = font_path()
    if p:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default(size)
