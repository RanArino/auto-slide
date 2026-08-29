"""タイトル / 区切り / 写真の再生タイムラインを 1 箇所で組む。

焼き込みキャプションと SRT はどちらもこの Segment 列から作るので、
文言・タイミングが食い違うことがない。

スライド構成:  title → (chapter divider → そのグループの photo…) × グループ数
各スライドは表示秒数 + クロスフェード transition 秒だけ ffmpeg に読み込ませ、
xfade で offset を付けて連結する(render.build_filtergraph と一致)。
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .models import Proposal, Segment, Selection


def caption_for(image_path: str, proposal: Proposal) -> str:
    return (proposal.captions.get(image_path) or "").strip()


def caption_sub_for(image_path: str, proposal: Proposal) -> str:
    return (proposal.caption_subs.get(image_path) or "").strip()


def divider_text_for(group_id: int, label: str, proposal: Proposal) -> str:
    """区切りスライドの文字。空は許さない(呼び出し側でフォールバックを保証する)。"""
    return (
        (proposal.divider_texts.get(group_id) or "").strip()
        or (label or "").strip()
        or (proposal.group_labels.get(group_id) or "").strip()
        or f"シーン {group_id + 1}"
    )


def _content_seconds(kind: str, cfg: Config) -> float:
    if kind == "title":
        return cfg.title_card_seconds
    if kind == "divider":
        return cfg.chapter_divider_seconds
    return cfg.seconds_per_slide


def build_timeline(selection: Selection, proposal: Proposal, cfg: Config) -> list[Segment]:
    """スライド 1 枚 = Segment 1 個。start/end は最終動画上の表示区間。"""
    specs: list[Segment] = [Segment(kind="title", start=0.0, end=0.0, text=proposal.title)]

    running = 0
    for g in selection.groups:
        if cfg.chapter_dividers:
            specs.append(Segment(
                kind="divider", start=0.0, end=0.0,
                text=divider_text_for(g.group_id, g.label, proposal),
                group_id=g.group_id,
            ))
        for im in g.images:
            running += 1
            specs.append(Segment(
                kind="photo", start=0.0, end=0.0,
                text=caption_for(im.path, proposal),
                subtext=caption_sub_for(im.path, proposal),
                image_path=im.path, group_id=g.group_id, index=running,
            ))

    t = cfg.transition_seconds
    n = len(specs)
    padded = [_content_seconds(s.kind, cfg) + t for s in specs]      # ffmpeg へ渡す長さ
    # 累積と xfade offset(build_filtergraph と同じ式)
    cum = [0.0]
    for d in padded:
        cum.append(cum[-1] + d)
    total = cum[-1] - (n - 1) * t

    starts = [0.0] * n
    for k in range(1, n):
        starts[k] = cum[k] - k * t                                  # 遷移 k の開始時刻
    for k in range(n):
        specs[k].start = round(starts[k], 3)
        specs[k].end = round(starts[k + 1] if k + 1 < n else total, 3)
    return specs


def slide_durations(segments: list[Segment], cfg: Config) -> list[float]:
    """ffmpeg に -t で渡す各スライドの長さ(表示秒 + transition)。segments と平行。"""
    t = cfg.transition_seconds
    return [_content_seconds(s.kind, cfg) + t for s in segments]


def total_seconds(segments: list[Segment], cfg: Config) -> float:
    durs = slide_durations(segments, cfg)
    return sum(durs) - (len(durs) - 1) * cfg.transition_seconds


def _fmt_ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list[Segment], dst: str | Path, cfg: Config) -> Path:
    """キャプション付き photo セグメントだけを SRT にする。

    焼き込みと同じ text。表示区間は「そのスライドが完全に出てから次の遷移まで」。
    """
    dst = Path(dst)
    t = cfg.transition_seconds
    lines: list[str] = []
    idx = 0
    for seg in segments:
        if seg.kind != "photo" or not seg.text:
            continue
        idx += 1
        start = seg.start + (t if seg.start > 0 else 0.0)
        end = max(start + 0.5, seg.end)
        lines.append(str(idx))
        lines.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        lines.append(seg.text)
        lines.append("")
    dst.write_text("\n".join(lines), encoding="utf-8")
    return dst
