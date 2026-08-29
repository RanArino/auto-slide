"""候補プールの入出力。

- render_candidate_sheets : 番号付きコンタクトシート(3x4/ページ、判断できる大きさ)
- write_candidates        : candidates.json + シートを out_dir に書く
- pool_from_candidates_json : candidates.json を CandidatePool に戻す(pick 用)
- write_selection         : finalize 済み Selection を selection.json に書く(plan 用)
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from .fonts import load_font
from .models import Candidate, CandidatePool, ImageMeta, Selection

log = logging.getLogger("autoslide.candidates")

_COLS, _ROWS, _CELL = 3, 4, 460


def render_candidate_sheets(pool: CandidatePool, out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    per_page = _COLS * _ROWS
    num = load_font(22)
    sub = load_font(15)
    names: list[str] = []
    pages = max(1, math.ceil(len(pool.candidates) / per_page))
    for p in range(pages):
        chunk = pool.candidates[p * per_page:(p + 1) * per_page]
        if not chunk:
            break
        rows = max(1, math.ceil(len(chunk) / _COLS))
        sheet = Image.new("RGB", (_COLS * _CELL, rows * _CELL), (18, 18, 20))
        d = ImageDraw.Draw(sheet)
        for i, c in enumerate(chunk):
            r, cc = divmod(i, _COLS)
            x0, y0 = cc * _CELL, r * _CELL
            src = c.meta.thumb_path or c.meta.path
            try:
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    im.thumbnail((_CELL - 16, _CELL - 58), Image.LANCZOS)
                    sheet.paste(im, (x0 + 8, y0 + 48))
            except Exception as e:  # noqa: BLE001
                log.warning("シート描画失敗 %s: %s", src, e)
            d.rectangle([x0, y0, x0 + _CELL - 1, y0 + _CELL - 1], outline=(70, 70, 78))
            d.text((x0 + 10, y0 + 12), f"#{c.idx}", fill=(245, 245, 250), font=num)
            tstr = c.meta.taken_at.strftime("%m/%d %H:%M") if c.meta.taken_at else "時刻不明"
            info = f"g{c.group_id} · {tstr} · sharp {c.meta.sharpness:.0f}"
            if c.meta.composition is not None:
                info += f" · comp {c.meta.composition:.2f}"
            if c.meta.lat is not None:
                info += " · GPS"
            d.text((x0 + 62, y0 + 16), info, fill=(155, 155, 165), font=sub)
        name = f"candidates_{p:02d}.jpg"
        sheet.save(out_dir / name, "JPEG", quality=90)
        names.append(name)
    return names


def write_candidates(pool: CandidatePool, out_dir: Path, aspect_hint: str = "16:9") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = render_candidate_sheets(pool, out_dir)
    groups = {}
    for gid, size in pool.group_sizes.items():
        cen = pool.group_centroids.get(gid, (None, None))
        groups[str(gid)] = {
            "kind": pool.group_kinds.get(gid, ""),
            "label": pool.group_labels.get(gid, ""),
            "pool_size": sum(1 for c in pool.candidates if c.group_id == gid),
            "total_size": size,
            "centroid": [cen[0], cen[1]] if cen[0] is not None else None,
        }
    data = {
        "source": pool.source,
        "count": pool.count,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "aspect_hint": aspect_hint,
        "contact_sheets": sheets,
        "groups": groups,
        "pool": [
            {
                "idx": c.idx,
                "path": c.meta.path,
                "thumb": c.meta.thumb_path,
                "group_id": c.group_id,
                "group_kind": c.group_kind,
                "taken_at": c.meta.taken_at.isoformat() if c.meta.taken_at else None,
                "lat": c.meta.lat,
                "lon": c.meta.lon,
                "sharpness": round(c.meta.sharpness, 1),
                "composition": round(c.meta.composition, 3) if c.meta.composition is not None else None,
            }
            for c in pool.candidates
        ],
        "guidance": (
            f"各 group から最低1枚。場所・構図が偏らないよう、ちょうど {pool.count} 枚を選ぶ。"
            " 明確なブレ・大きな露出破綻・ほぼ同一構図・大きな傾きは除外。"
            " 採用は ピントが合い主題が明快でグループ内の他と違う画。"
            " comp は黄金比／三分割の目安(0〜1)。高いほど構図が整っている候補。"
        ),
    }
    path = out_dir / "candidates.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Claude Code が写真を見て埋めるためのテンプレート(API キー不要)
    template = {
        "title": "",
        "mood": "calm|nostalgic|melancholic|joyful|upbeat|dramatic のいずれか1語に置き換える",
        "mood_note": "",
        "music_style": "",
        "group_labels": {str(gid): "" for gid in sorted(pool.group_sizes)},
        "captions": {},
        "hashtags": [],
    }
    tpath = out_dir / "proposal.template.json"
    if not tpath.exists():
        tpath.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info("候補 %d 枚 / シート %d 枚 → %s", len(pool.candidates), len(sheets), out_dir)
    return path


def pool_from_candidates_json(data: dict) -> CandidatePool:
    cands: list[Candidate] = []
    cents: dict[int, tuple] = {}
    for gid, g in (data.get("groups") or {}).items():
        c = g.get("centroid")
        cents[int(gid)] = (tuple(c) if c else (None, None))
    glabels = {int(k): v.get("label", "") for k, v in (data.get("groups") or {}).items()}
    for e in data["pool"]:
        meta = ImageMeta(
            path=e["path"],
            folder=str(Path(e["path"]).parent),
            taken_at=datetime.fromisoformat(e["taken_at"]) if e.get("taken_at") else None,
            lat=e.get("lat"),
            lon=e.get("lon"),
            thumb_path=e.get("thumb"),
            sharpness=e.get("sharpness") or 0.0,
            composition=e.get("composition"),
        )
        gid = e["group_id"]
        cands.append(Candidate(e["idx"], meta, gid, e.get("group_kind", ""),
                               cents.get(gid, (None, None)), glabels.get(gid, "")))
    return CandidatePool(
        source=data.get("source", ""),
        count=int(data.get("count", len(cands))),
        candidates=cands,
        group_sizes={int(k): v.get("total_size", v.get("pool_size", 0))
                     for k, v in (data.get("groups") or {}).items()},
        group_kinds={int(k): v.get("kind", "") for k, v in (data.get("groups") or {}).items()},
        group_labels=glabels,
        group_centroids=cents,
    )


def write_selection(out_dir: Path, selection: Selection, picked_idx: list[int],
                    source: str) -> Path:
    data = {
        "source": source,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "picked_idx": picked_idx,
        "order": selection.order,
        "reasons": selection.reasons,
        "groups": [
            {
                "group_id": g.group_id,
                "kind": g.kind,
                "label": g.label,
                "centroid": [g.centroid_lat, g.centroid_lon]
                if g.centroid_lat is not None else None,
                "images": [im.path for im in g.images],
            }
            for g in selection.groups
        ],
    }
    path = out_dir / "selection.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
