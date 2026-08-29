"""スライド画像の準備 + ffmpeg でのレンダリング。

スライド構成: タイトル → (章の区切り → その章の写真…) × 章数。
各スライドは表示秒 + 前後のクロスフェード transition 秒。
出力: H.264 / yuv420p / +faststart の mp4 と、同じ文言・タイミングの out.srt。

方針: フォント不備・ffmpeg フィルタ不足は起動時に RuntimeError で停止する。
黒い無地スライドや文字欠けのまま生成しない。
"""

from __future__ import annotations

import json
import logging
import shutil
import statistics
import subprocess
import tempfile
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import fonts
from .color import apply_gains
from .config import Config
from .exposure import ToneResult, apply_tone
from .fonts import load_font
from .models import Proposal, Segment, Selection
from .music import MusicChoice
from .timeline import build_timeline, slide_durations, total_seconds, write_srt

log = logging.getLogger("autoslide.render")

_REQUIRED_FILTERS = ("xfade", "afade", "loudnorm", "apad")


def _require(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        raise RuntimeError(f"{tool} が見つかりません。Homebrew: brew install {tool}")
    return p


def check_ffmpeg_filters(ffmpeg: str | None = None) -> None:
    """必須フィルタが無ければ RuntimeError。無言フォールバックしない。"""
    ffmpeg = ffmpeg or _require("ffmpeg")
    out = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                         capture_output=True, text=True)
    have = out.stdout
    missing = [f for f in _REQUIRED_FILTERS if f" {f} " not in have]
    if missing:
        raise RuntimeError(
            f"この ffmpeg には必須フィルタがありません: {missing}。"
            " xfade/afade/loudnorm を含むビルド(Homebrew の ffmpeg 等)を使ってください。"
        )


# --- フレーム整形 ----------------------------------------------------------

def _cover_resize(im: Image.Image, w: int, h: int,
                  scale: float | None = None) -> tuple[Image.Image, float]:
    s = scale if scale is not None else max(w / im.width, h / im.height)
    nw, nh = max(1, round(im.width * s)), max(1, round(im.height * s))
    return im.resize((nw, nh), Image.LANCZOS), s


def _center_crop(im: Image.Image, w: int, h: int) -> Image.Image:
    left, top = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((left, top, left + w, top + h))


def _blur_bg(im: Image.Image, w: int, h: int) -> Image.Image:
    bg, _ = _cover_resize(im, w, h)
    bg = _center_crop(bg, w, h)
    return bg.filter(ImageFilter.GaussianBlur(radius=max(6, max(w, h) // 45)))


def _contain_blur(im: Image.Image, w: int, h: int) -> Image.Image:
    bg = _blur_bg(im, w, h)
    fg = im.copy()
    fg.thumbnail((w, h), Image.LANCZOS)
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return bg


def _normalized_cover(im: Image.Image, w: int, h: int, use_scale: float) -> Image.Image:
    resized, _ = _cover_resize(im, w, h, use_scale)
    if resized.width >= w and resized.height >= h:
        return _center_crop(resized, w, h)
    # 拡大率をそろえた結果キャンバスを覆いきれない → ぼかし背景で埋める(黒帯を出さない)
    bg = _blur_bg(im, w, h)
    bg.paste(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
    return bg


def _fits_as_contain(im: Image.Image, w: int, h: int) -> bool:
    """写真とキャンバスの向きが逆(縦写真×横キャンバス等)か。
    auto はこれだけ contain-blur にする。同じ向きの 4:3/3:2 等は cover のまま。"""
    return (im.width < im.height) != (w < h)


def _photo_frame(im: Image.Image, w: int, h: int, cfg: Config,
                 scale_window: tuple[float, float] | None) -> Image.Image:
    im = im.convert("RGB")
    if cfg.fit_mode == "contain-blur":
        return _contain_blur(im, w, h)
    if cfg.fit_mode == "auto" and _fits_as_contain(im, w, h):
        return _contain_blur(im, w, h)
    cover_scale = max(w / im.width, h / im.height)
    use = cover_scale
    if scale_window:
        lo, hi = scale_window
        use = min(max(cover_scale, lo), hi)
    return _normalized_cover(im, w, h, use)


def _group_scale_windows(selection: Selection, w: int, h: int,
                         cfg: Config) -> dict[int, tuple[float, float]]:
    """同一グループ内の cover 拡大率を中央値 ±tol にそろえるための窓。"""
    tol = cfg.group_scale_tolerance
    windows: dict[int, tuple[float, float]] = {}
    for g in selection.groups:
        scales = []
        for im in g.images:
            iw, ih = im.width, im.height
            if not (iw and ih):
                try:
                    with Image.open(im.path) as x:
                        iw, ih = x.size
                except Exception:  # noqa: BLE001
                    continue
            scales.append(max(w / iw, h / ih))
        if not scales:
            continue
        med = statistics.median(scales)
        windows[g.group_id] = (med * (1 - tol), med * (1 + tol))
    return windows


# --- レターボックス ----------------------------------------------------

def _apply_letterbox(frame: Image.Image, cfg: Config) -> Image.Image:
    """シネマスコープの黒帯。中央に w×ch のコンテンツ帯、上下は純黒。"""
    if not cfg.letterbox:
        return frame
    w, h = frame.size
    ch = min(h, round(w / cfg.letterbox_ratio))
    if ch >= h:
        return frame
    band, _ = _cover_resize(frame, w, ch)
    band = _center_crop(band, w, ch)
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(band, (0, (h - ch) // 2))
    return canvas


def _content_bottom(cfg: Config, w: int, h: int) -> int:
    """キャプションを載せる下端 y(レターボックス下帯の少し内側)。"""
    if not cfg.letterbox:
        return h
    ch = min(h, round(w / cfg.letterbox_ratio))
    return (h + ch) // 2 if ch < h else h


# --- キャプション ------------------------------------------------------

def _draw_caption_bar(im: Image.Image, text: str, sub: str = "") -> None:
    """従来スタイル: 下いっぱいの半透明バー + 中央寄せ。"""
    w, h = im.size
    if sub:
        text = f"{text}\n{sub}" if text else sub
    font = load_font(max(20, h // 28))
    draw = ImageDraw.Draw(im, "RGBA")
    wrapped = textwrap.fill(text, width=max(12, w // font.size))
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=6)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = font.size
    draw.rectangle([0, h - th - 2 * pad, w, h], fill=(0, 0, 0, 140))
    draw.multiline_text(((w - tw) / 2, h - th - pad), wrapped, font=font,
                        fill=(255, 255, 255, 255), spacing=6, align="center")


def _draw_caption_lowerleft(im: Image.Image, text: str, sub: str, cfg: Config) -> None:
    """参考画像スタイル: 下帯・左寄せの主文 + 小さな日付。バーは敷かない。"""
    w, h = im.size
    if not text and not (sub and cfg.caption_date_always):
        return
    left = int(w * 0.055)
    # レターボックス下帯の縁に載る位置。帯が無いときは下端から少し内側。
    baseline = _content_bottom(cfg, w, h) - (int(h * 0.02) if cfg.letterbox else int(h * 0.06))
    draw = ImageDraw.Draw(im, "RGBA")

    sub_font = load_font(max(16, h // 45))
    main_font = load_font(max(22, h // 24)) if text else None

    y = baseline
    if sub:
        sb = draw.textbbox((0, 0), sub, font=sub_font)
        y -= sb[3] - sb[1]
        _text_with_shadow(draw, (left, y), sub, sub_font, (225, 225, 230, 255))
        y -= int(h * 0.012)
    if text and main_font is not None:
        mb = draw.textbbox((0, 0), text, font=main_font)
        y -= mb[3] - mb[1]
        _text_with_shadow(draw, (left, y), text, main_font, (255, 255, 255, 255))


def _text_with_shadow(draw: ImageDraw.ImageDraw, xy, text, font, fill) -> None:
    x, y = xy
    for dx, dy in ((2, 2), (1, 1)):
        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 160))
    draw.text((x, y), text, font=font, fill=fill)


def _text_card(main: str, sub: str, w: int, h: int, dst: Path,
               main_divisor: int, rule: bool = False) -> None:
    card = Image.new("RGB", (w, h), (18, 18, 24))
    draw = ImageDraw.Draw(card)
    tfont = load_font(max(30, h // main_divisor))
    sfont = load_font(max(20, h // 34))
    tw = textwrap.fill(main, width=max(8, int(w / (tfont.size * 0.62))))
    tb = draw.multiline_textbbox((0, 0), tw, font=tfont, spacing=10, align="center")
    th = tb[3] - tb[1]
    y = (h - th) / 2 - (sfont.size if sub else 0)
    draw.multiline_text((w / 2, y), tw, font=tfont, fill=(245, 245, 250),
                        anchor="ma", align="center", spacing=10)
    if rule:
        draw.line([(w * 0.4, y - tfont.size * 0.6), (w * 0.6, y - tfont.size * 0.6)],
                  fill=(120, 120, 140), width=max(2, h // 400))
    if sub:
        draw.text((w / 2, y + th + sfont.size), sub, font=sfont,
                  fill=(150, 150, 165), anchor="ma")
    card.save(dst, "PNG")


def _title_card(title: str, subtitle: str, w: int, h: int, dst: Path) -> None:
    _text_card(title, subtitle, w, h, dst, main_divisor=12)


def _divider_card(text: str, w: int, h: int, dst: Path) -> None:
    if not text.strip():
        raise RuntimeError("区切りスライドのテキストが空です(黒い無地は許容しません)")
    _text_card(text, "", w, h, dst, main_divisor=16, rule=True)


# --- スライド生成 --------------------------------------------------------

def _prepare_slides(selection: Selection, proposal: Proposal, cfg: Config,
                    segments: list[Segment], workdir: Path,
                    color_map: dict[str, tuple[float, float, float]] | None,
                    tone_map: dict[str, ToneResult] | None) -> list[Path]:
    w, h = cfg.resolution
    windows = _group_scale_windows(selection, w, h, cfg)
    subtitle = (proposal.overall_tone or proposal.mood).split("/")[0].strip()

    paths: list[Path] = []
    for i, seg in enumerate(segments):
        out = workdir / f"slide_{i:03d}.png"
        if seg.kind == "title":
            _title_card(proposal.title, subtitle, w, h, out)
            _finish_card(out, cfg)
        elif seg.kind == "divider":
            _divider_card(seg.text, w, h, out)
            _finish_card(out, cfg)
        else:
            with Image.open(seg.image_path) as im:
                im.load()
                frame = _photo_frame(im, w, h, cfg, windows.get(seg.group_id or -1))
            if tone_map and seg.image_path in tone_map:          # 露出補正(先)
                frame = apply_tone(frame, tone_map[seg.image_path])
            if color_map and seg.image_path in color_map:        # 色補正(後)
                frame = apply_gains(frame, color_map[seg.image_path])
            frame = _apply_letterbox(frame, cfg)                 # 黒帯
            if cfg.burn_captions and (seg.text or seg.subtext):  # キャプションは帯の上
                if cfg.caption_style == "bar":
                    _draw_caption_bar(frame, seg.text, seg.subtext)
                else:
                    _draw_caption_lowerleft(frame, seg.text, seg.subtext, cfg)
            frame.save(out, "PNG")
        paths.append(out)
    return paths


def _finish_card(path: Path, cfg: Config) -> None:
    if not cfg.letterbox:
        return
    with Image.open(path) as im:
        im.load()
        _apply_letterbox(im, cfg).save(path, "PNG")


# --- ffmpeg ------------------------------------------------------------

def build_filtergraph(n_inputs: int, durations: list[float], t: float,
                      w: int, h: int, fps: int) -> tuple[str, str]:
    """xfade チェーンの filter_complex 文字列と最終ラベルを返す。"""
    parts = []
    for i in range(n_inputs):
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
    if n_inputs == 1:
        return ";".join(parts), "[v0]"

    prev = "[v0]"
    cum = durations[0]
    for k in range(1, n_inputs):
        offset = cum - k * t
        out = f"[x{k}]" if k < n_inputs - 1 else "[vout]"
        parts.append(
            f"{prev}[v{k}]xfade=transition=fade:duration={t:.3f}:"
            f"offset={offset:.3f}{out}"
        )
        prev = out
        cum += durations[k]
    return ";".join(parts), "[vout]"


def _audio_args(music: MusicChoice, total: float, cfg: Config,
                audio_input_index: int) -> tuple[list[str], list[str], str]:
    fin, fout = cfg.audio_fade_in, cfg.audio_fade_out
    chain = (
        f"[{audio_input_index}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={fin:.2f},"
        f"afade=t=out:st={max(0.0, total - fout):.3f}:d={fout:.2f},"
        f"loudnorm=I={cfg.audio_lufs}:TP=-1.5:LRA=11,"
        f"apad[aout]"
    )
    return (["-i", str(music.path)], ["-map", "[aout]"], chain)


def render(selection: Selection, proposal: Proposal, cfg: Config,
           music: MusicChoice | None, out_path: str | Path,
           keep_workdir: bool = False,
           color_map: dict[str, tuple[float, float, float]] | None = None,
           tone_map: dict[str, ToneResult] | None = None) -> Path:
    ffmpeg = _require("ffmpeg")
    _require("ffprobe")
    fonts.assert_can_render()          # フォント無し/CJK 不可なら停止
    check_ffmpeg_filters(ffmpeg)       # フィルタ不足なら停止

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    w, h = cfg.resolution
    t = cfg.transition_seconds

    segments = build_timeline(selection, proposal, cfg)
    durations = slide_durations(segments, cfg)
    total = total_seconds(segments, cfg)
    n = len(segments)

    workdir = Path(tempfile.mkdtemp(prefix="autoslide_"))
    try:
        slides = _prepare_slides(selection, proposal, cfg, segments, workdir,
                                 color_map, tone_map)

        cmd = [ffmpeg, "-y"]
        for d, s in zip(durations, slides):
            cmd += ["-loop", "1", "-framerate", str(cfg.fps), "-t", f"{d:.3f}", "-i", str(s)]

        graph, vlabel = build_filtergraph(n, durations, t, w, h, cfg.fps)
        map_args = ["-map", vlabel]
        audio_codec: list[str] = []
        if music:
            in_args, a_map, a_chain = _audio_args(music, total, cfg, n)
            cmd += in_args
            graph = graph + ";" + a_chain
            map_args += a_map
            audio_codec = ["-c:a", "aac", "-b:a", "192k"]

        cmd += ["-filter_complex", graph, *map_args, "-shortest",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(cfg.fps),
                "-movflags", "+faststart", *audio_codec, str(out_path)]

        log.info("ffmpeg 実行: %d スライド, 目標尺 %.1fs", n, total)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.error("ffmpeg stderr:\n%s", proc.stderr[-4000:])
            raise RuntimeError(f"ffmpeg が失敗しました (code {proc.returncode})")

        srt = write_srt(segments, out_path.with_suffix(".srt"), cfg)
        log.info("字幕を書き出し: %s", srt.name)
        _probe(out_path)
        return out_path
    finally:
        if keep_workdir:
            log.info("作業ディレクトリ: %s", workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def _probe(path: Path) -> None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        info = json.loads(out.stdout)
        dur = float(info.get("format", {}).get("duration", 0))
        streams = ", ".join(
            f"{s.get('codec_type')}:{s.get('codec_name')}" for s in info.get("streams", [])
        )
        log.info("出力 %s / %.1fs / %s", path.name, dur, streams)
    except Exception:  # noqa: BLE001
        log.info("出力 %s (ffprobe 解析はスキップ)", path.name)
