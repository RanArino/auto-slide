"""スライド画像の準備 + ffmpeg でのレンダリング。

各スライドは表示 seconds_per_slide 秒 + 前後のクロスフェード transition 秒。
入力: タイトルカード + 並び順の写真。出力: H.264 / yuv420p / +faststart の mp4。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw

from .config import Config
from .fonts import load_font
from .models import Proposal, Selection
from .music import MusicChoice

log = logging.getLogger("autoslide.render")


def _require(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        raise RuntimeError(f"{tool} が見つかりません。Homebrew: brew install {tool}")
    return p


def _cover_crop(im: Image.Image, w: int, h: int) -> Image.Image:
    im = im.convert("RGB")
    scale = max(w / im.width, h / im.height)
    nw, nh = max(w, int(im.width * scale + 0.5)), max(h, int(im.height * scale + 0.5))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _draw_caption(im: Image.Image, text: str) -> None:
    w, h = im.size
    font = load_font(max(20, h // 28))
    draw = ImageDraw.Draw(im, "RGBA")
    wrapped = textwrap.fill(text, width=max(12, w // (font.size)))
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=6)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = font.size
    draw.rectangle([0, h - th - 2 * pad, w, h], fill=(0, 0, 0, 140))
    draw.multiline_text(((w - tw) / 2, h - th - pad), wrapped, font=font,
                        fill=(255, 255, 255, 255), spacing=6, align="center")


def _title_card(title: str, subtitle: str, w: int, h: int, dst: Path) -> None:
    card = Image.new("RGB", (w, h), (18, 18, 24))
    draw = ImageDraw.Draw(card)
    tfont = load_font(max(36, h // 12))
    sfont = load_font(max(20, h // 34))
    tw = textwrap.fill(title, width=max(8, int(w / (tfont.size * 0.62))))
    tb = draw.multiline_textbbox((0, 0), tw, font=tfont, spacing=10, align="center")
    th = tb[3] - tb[1]
    y = (h - th) / 2 - (sfont.size if subtitle else 0)
    draw.multiline_text((w / 2, y), tw, font=tfont, fill=(245, 245, 250),
                        anchor="ma", align="center", spacing=10)
    if subtitle:
        draw.text((w / 2, y + th + sfont.size), subtitle, font=sfont,
                  fill=(150, 150, 165), anchor="ma")
    card.save(dst, "PNG")


def _prepare_slides(selection: Selection, proposal: Proposal, cfg: Config,
                    workdir: Path) -> list[Path]:
    w, h = cfg.resolution
    paths: list[Path] = []

    subtitle = proposal.mood.split("/")[0].strip()
    tc = workdir / "slide_000.png"
    _title_card(proposal.title, subtitle, w, h, tc)
    paths.append(tc)

    for i, src in enumerate(selection.order, start=1):
        with Image.open(src) as im:
            im.load()
            frame = _cover_crop(im, w, h)
        if cfg.burn_captions and proposal.captions.get(src):
            _draw_caption(frame, proposal.captions[src])
        out = workdir / f"slide_{i:03d}.png"
        frame.save(out, "PNG")
        paths.append(out)
    return paths


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
           keep_workdir: bool = False) -> Path:
    ffmpeg = _require("ffmpeg")
    _require("ffprobe")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    w, h = cfg.resolution
    t = cfg.transition_seconds

    workdir = Path(tempfile.mkdtemp(prefix="autoslide_"))
    try:
        slides = _prepare_slides(selection, proposal, cfg, workdir)
        n = len(slides)
        durations = [cfg.title_card_seconds + t] + [cfg.seconds_per_slide + t] * (n - 1)
        total = sum(durations) - (n - 1) * t

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

        log.info("ffmpeg 実行: %d 入力, 目標尺 %.1fs", n, total)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.error("ffmpeg stderr:\n%s", proc.stderr[-4000:])
            raise RuntimeError(f"ffmpeg が失敗しました (code {proc.returncode})")

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
