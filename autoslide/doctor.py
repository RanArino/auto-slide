"""環境プリフライト。render / run はこれの必須チェックを内部で通す。

無言のフォールバック劣化をしないため、必須項目が欠けたら非ゼロ終了。
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .render import _REQUIRED_FILTERS

_OK = "  OK  "
_WARN = " WARN "
_FAIL = " FAIL "


def _line(tag: str, msg: str) -> str:
    return f"[{tag}] {msg}"


def _check_ffmpeg() -> tuple[bool, list[str]]:
    out: list[str] = []
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        p = shutil.which(tool)
        if p:
            out.append(_line(_OK, f"{tool}: {p}"))
        else:
            ok = False
            out.append(_line(_FAIL, f"{tool} が無い。'brew install ffmpeg'"))
    if shutil.which("ffmpeg"):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True)
        missing = [f for f in _REQUIRED_FILTERS if f" {f} " not in r.stdout]
        if missing:
            ok = False
            out.append(_line(_FAIL, f"ffmpeg に必須フィルタが無い: {missing}"))
        else:
            out.append(_line(_OK, f"ffmpeg フィルタ: {', '.join(_REQUIRED_FILTERS)}"))
    return ok, out


def _check_font() -> tuple[bool, list[str]]:
    from . import fonts

    try:
        p = fonts.require_font()
        return True, [_line(_OK, f"CJK フォント: {p}")]
    except RuntimeError as e:
        return False, [_line(_FAIL, str(e))]


def _check_heic() -> tuple[bool, list[str]]:
    from .scan import heic_support

    s = heic_support()
    if s:
        return True, [_line(_OK, f"HEIC 変換: {s}")]
    return True, [_line(_WARN, "HEIC を扱う手段が無い(pillow-heif / sips / heif-convert)。"
                               "HEIC を含むフォルダはその分スキップされる")]


def _check_pillow() -> tuple[bool, list[str]]:
    try:
        from PIL import features
        checks = {
            "jpg": features.check_codec("jpg"),
            "zlib": features.check_codec("zlib"),
            "freetype": features.check_module("freetype2"),
        }
        missing = [k for k, v in checks.items() if not v]
        if missing:
            return False, [_line(_FAIL, f"Pillow に機能が足りない: {missing}"
                                        "（'uv pip install --force-reinstall pillow' を試す）")]
        return True, [_line(_OK, f"Pillow: {', '.join(checks)}")]
    except Exception as e:  # noqa: BLE001
        return False, [_line(_FAIL, f"Pillow の確認に失敗: {e}")]


def run_doctor() -> int:
    print(f"platform: {sys.platform}")
    required_ok = True
    for check in (_check_ffmpeg, _check_font, _check_pillow, _check_heic):
        ok, lines = check()
        for ln in lines:
            print(ln)
        required_ok = required_ok and ok
    print()
    if required_ok:
        print("=> 必須項目はすべて OK。")
        return 0
    print("=> 必須項目が不足しています。上記 FAIL を解消してください。")
    return 1
