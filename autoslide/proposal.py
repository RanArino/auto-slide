"""構成・タイトル・感情の提案。

コンタクトシート(番号付き格子画像)を作り、Claude Vision に投げて JSON を得る。
API キーが無い / 失敗したときは決定的なフォールバック案を返す(パイプラインは止めない)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import Config
from .fonts import load_font
from .models import Group, ImageMeta, Proposal, Selection

log = logging.getLogger("autoslide.proposal")

_CELL = 320
_COLS = 4
_PROMPT = """あなたはフォトムービーの編集者です。渡された番号付きコンタクトシート1枚には、
時系列・撮影場所ごとにグループ分けされた写真が写っています。次を必ず**JSONのみ**で返してください:

{
  "title": "日本語の短いタイトル(30文字以内)",
  "mood": "calm|nostalgic|upbeat|dramatic|melancholic|joyful のいずれか1語",
  "mood_note": "感情の補足(20文字以内)",
  "overall_tone": "動画全体のトーンを一文で(30文字以内)",
  "music_style": "曲調の指定(例: ゆったりしたピアノ)",
  "color_note": "色味の印象や補正の要否について一言(任意, 30文字以内)",
  "group_labels": {"0": "このグループの短い見出し", "1": "..."},
  "divider_texts": {"0": "章の区切りに出す短い言葉(12文字以内)", "1": "..."},
  "captions": {"1": "写真1の短い説明(15文字以内)", "2": "..."},
  "hashtags": ["#タグ", "#タグ"]
}

写真の枚数や順番は変えないでください。captions のキーは写真番号(1始まり)です。
group_labels / divider_texts のキーはグループ番号です。"""

_MOODS = {"calm", "nostalgic", "upbeat", "dramatic", "melancholic", "joyful"}


def build_contact_sheet(order: list[str], images: dict[str, ImageMeta], dst: Path) -> Path:
    n = len(order)
    rows = (n + _COLS - 1) // _COLS
    font = load_font(22)
    sheet = Image.new("RGB", (_COLS * _CELL, rows * _CELL), (18, 18, 20))
    draw = ImageDraw.Draw(sheet)
    for idx, path in enumerate(order):
        r, c = divmod(idx, _COLS)
        x0, y0 = c * _CELL, r * _CELL
        meta = images.get(path)
        src = (meta.thumb_path if meta and meta.thumb_path else path)
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((_CELL - 16, _CELL - 44), Image.LANCZOS)
                sheet.paste(im, (x0 + 8, y0 + 34))
        except Exception as e:  # noqa: BLE001
            log.warning("コンタクトシート描画失敗 %s: %s", src, e)
        draw.rectangle([x0, y0, x0 + _CELL - 1, y0 + _CELL - 1], outline=(60, 60, 66))
        draw.text((x0 + 8, y0 + 6), f"#{idx + 1}", fill=(240, 240, 245), font=font)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst, "JPEG", quality=90)
    return dst


def _date_range(groups: list[Group]) -> str:
    starts = [g.t_start for g in groups if g.t_start]
    if not starts:
        return ""
    lo, hi = min(starts), max(g.t_end for g in groups if g.t_end)
    if lo.date() == hi.date():
        return lo.strftime("%Y-%m-%d")
    return f"{lo.strftime('%Y-%m-%d')} 〜 {hi.strftime('%Y-%m-%d')}"


def _fallback(selection: Selection, source: Path) -> Proposal:
    name = re.sub(r"[-_]+", " ", source.name).strip() or "スライドショー"
    dr = _date_range(selection.groups)
    title = f"{name}" + (f"（{dr}）" if dr else "")
    labels = {}
    dividers = {}
    for g in selection.groups:
        loc = "屋外" if g.centroid_lat is not None else "シーン"
        base = (g.label or f"{loc} {g.group_id + 1}").strip()
        labels[g.group_id] = base
        dividers[g.group_id] = base            # 区切りテキストは必ず埋める(空にしない)
    return Proposal(
        title=title[:30],
        mood="calm",
        overall_tone="おだやかな振り返り",
        music_style="ゆったりしたピアノ",
        color_note="",
        group_labels=labels,
        divider_texts=dividers,
        captions={},
        caption_subs={},
        hashtags=["#スライドショー", "#写真"],
        raw={"fallback": True},
    )


def _call_llm(sheet_path: Path, model: str) -> dict | None:
    try:
        import anthropic
    except Exception:
        log.info("anthropic 未インストール。フォールバック案を使用（--proposal で明示指定できます）")
        return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY 未設定。フォールバック案を使用（--proposal で明示指定できます）")
        return None

    data = base64.standard_b64encode(sheet_path.read_bytes()).decode()
    client = anthropic.Anthropic()
    for attempt in (1, 2):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/jpeg", "data": data}},
                        {"type": "text", "text": _PROMPT},
                    ],
                }],
            )
            text = "".join(b.text for b in msg.content if b.type == "text")
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                raise ValueError("JSON が見つからない")
            return json.loads(m.group(0))
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 呼び出し失敗 (試行 %d): %s", attempt, e)
    return None


def _coerce(raw: dict, selection: Selection, source: Path) -> Proposal:
    fb = _fallback(selection, source)
    mood = str(raw.get("mood", "")).strip().lower()
    if mood not in _MOODS:
        mood = fb.mood
    labels = {}
    dividers = {}
    raw_labels = raw.get("group_labels") or {}
    raw_dividers = raw.get("divider_texts") or {}
    for g in selection.groups:
        v = raw_labels.get(str(g.group_id))
        labels[g.group_id] = str(v).strip() if v else fb.group_labels.get(g.group_id, "")
        dv = raw_dividers.get(str(g.group_id))
        dividers[g.group_id] = (
            str(dv).strip() if dv
            else labels[g.group_id] or fb.divider_texts.get(g.group_id, f"シーン {g.group_id + 1}")
        )
    raw_caps = raw.get("captions") or {}
    captions = {}
    for i, path in enumerate(selection.order, start=1):
        v = raw_caps.get(str(i)) or raw_caps.get(Path(path).name) or raw_caps.get(path)
        if v:
            captions[path] = str(v).strip()
    tags = raw.get("hashtags") or fb.hashtags
    tags = [str(t).strip() for t in tags if str(t).strip()][:5]
    note = str(raw.get("mood_note", "")).strip()
    return Proposal(
        title=(str(raw.get("title", "")).strip() or fb.title)[:40],
        mood=mood + (f" / {note}" if note else ""),
        overall_tone=str(raw.get("overall_tone", "")).strip() or fb.overall_tone,
        color_note=str(raw.get("color_note", "")).strip(),
        music_style=str(raw.get("music_style", "")).strip() or fb.music_style,
        group_labels=labels,
        divider_texts=dividers,
        captions=captions,
        caption_subs={},
        hashtags=tags,
        raw=raw,
    )


def _selection_hash(order: list[str]) -> str:
    return hashlib.sha1("\n".join(order).encode("utf-8")).hexdigest()[:16]


def propose(selection: Selection, images: dict[str, ImageMeta], source: str | Path,
            cfg: Config, out_dir: Path, use_llm: bool = True,
            proposal_path: str | Path | None = None,
            refresh: bool = False) -> tuple[Proposal, Path]:
    """タイトル・感情・キャプション・区切りテキストを決める。

    優先順位:
      1. proposal_path が指定されていればその JSON を使う(Claude Code が書いたもの)
      2. 選択集合が同じ Vision 結果キャッシュがあれば再利用(refresh で無効化)
      3. use_llm かつ ANTHROPIC_API_KEY があれば Claude API に問い合わせ、結果をキャッシュ
      4. どれも無ければ決定的なフォールバック(フォルダ名＋日付、mood=calm)
    """
    source = Path(source)
    sheet = build_contact_sheet(selection.order, images, out_dir / "contact_sheet.jpg")

    raw = None
    origin = "fallback"
    cache_file = out_dir / ".proposal_cache" / f"{_selection_hash(selection.order)}.json"
    if proposal_path:
        raw = json.loads(Path(proposal_path).read_text(encoding="utf-8"))
        origin = "file"
    elif use_llm and cache_file.exists() and not refresh:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        origin = "cache"
    elif use_llm:
        raw = _call_llm(sheet, cfg.llm_model)
        if raw:
            origin = "api"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    proposal = _coerce(raw, selection, source) if raw else _fallback(selection, source)
    log.info("proposal: title=%r mood=%r (%s)", proposal.title, proposal.mood, origin)
    return proposal, sheet
