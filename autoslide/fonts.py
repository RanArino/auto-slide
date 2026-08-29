"""日本語が焼けるフォントを解決する。

方針: **無言のフォールバックをしない**。CJK が描画できるフォントが無ければ
require_font() / assert_can_render() が RuntimeError を投げ、セットアップ時点で気づける。
同梱フォント(assets/fonts/ に置いた .otf/.ttf/.ttc)を最優先で使う。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# 同梱が無いときに探すシステムフォント(CJK 収録のもの)
_SYSTEM_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/ArialHB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansJP-Regular.otf",
]

_PROBE = "あ日本語Ag"  # かな + 漢字 + ラテン。全部出れば CJK 対応とみなす


def _bundled() -> list[str]:
    if not _BUNDLED_DIR.is_dir():
        return []
    out = []
    for ext in ("*.otf", "*.ttf", "*.ttc", "*.OTF", "*.TTF", "*.TTC"):
        out += [str(p) for p in sorted(_BUNDLED_DIR.glob(ext))]
    return out


def _can_render(path: str) -> bool:
    try:
        font = ImageFont.truetype(path, 48)
    except Exception:
        return False
    img = Image.new("L", (400, 80), 0)
    ImageDraw.Draw(img).text((4, 4), _PROBE, font=font, fill=255)
    bbox = img.getbbox()
    if bbox is None:
        return False
    # ラテンのみのフォントだと幅が極端に狭い / 豆腐だけだと箱状になる。
    # かなを含む _PROBE が十分な幅で描ければ CJK 収録と判断する。
    return (bbox[2] - bbox[0]) > 120


@lru_cache(maxsize=1)
def font_path() -> str | None:
    for p in _bundled() + _SYSTEM_CANDIDATES:
        if Path(p).exists() and _can_render(p):
            return p
    return None


def require_font() -> str:
    p = font_path()
    if p is None:
        raise RuntimeError(
            "日本語を描画できるフォントが見つかりません。"
            f"CJK フォント(.otf/.ttf/.ttc)を {_BUNDLED_DIR} に置くか、"
            "システムに Noto Sans CJK / ヒラギノ等を入れてください。"
        )
    return p


def assert_can_render() -> None:
    """レンダリング前ゲート。フォントが無い/CJK が出ないなら例外。"""
    require_font()


@lru_cache(maxsize=64)
def load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(require_font(), size)
