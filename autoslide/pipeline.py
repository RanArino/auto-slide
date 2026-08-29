"""scan / plan / render をつなぐ。plan.json の読み書きもここ。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .candidates import (
    pool_from_candidates_json,
    write_candidates,
    write_selection,
)
from .config import Config
from .db import Cache
from .grouping import group_images
from .models import Group, ImageMeta, Proposal, Selection
from .music import choose_music
from .proposal import propose
from .render import render as render_video
from .scan import scan
from .selection import finalize, select, shortlist

log = logging.getLogger("autoslide.pipeline")


def _cache_dir(base: Path) -> Path:
    return base / ".autoslide_cache"


def open_cache(source: Path) -> tuple[Cache, Path]:
    cdir = _cache_dir(Path.cwd())
    return Cache(cdir / "cache.db"), cdir


def do_scan(source: str | Path) -> None:
    source = Path(source)
    cache, cdir = open_cache(source)
    try:
        metas = scan(source, cache, cdir)
        log.info("索引済み: %d 枚", len(metas))
    finally:
        cache.close()


def _scan_cached(source: Path) -> list[ImageMeta]:
    cache, cdir = open_cache(source)
    try:
        return scan(source, cache, cdir)
    finally:
        cache.close()


def do_candidates(source: str | Path, count: int, cfg: Config, out_dir: Path,
                  pool_factor: float = 2.5) -> Path:
    """候補プールとコンタクトシートを out_dir に書く(最終選択は skill / 人が行う)。"""
    source = Path(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    metas = _scan_cached(source)
    if not metas:
        raise RuntimeError(f"画像が見つかりません: {source}")
    groups = group_images(metas, cfg)
    pool = shortlist(groups, count, cfg, pool_factor=pool_factor, source=str(source))
    if not pool.candidates:
        raise RuntimeError("候補が空です")
    return write_candidates(pool, out_dir, aspect_hint=cfg.aspect)


def do_pick(out_dir: str | Path, chosen_idx: list[int], cfg: Config) -> Path:
    """candidates.json の番号リストから selection.json を作る。"""
    out_dir = Path(out_dir)
    cand_path = out_dir / "candidates.json"
    if not cand_path.exists():
        raise FileNotFoundError(f"candidates.json がありません: {cand_path}(先に candidates を実行)")
    data = json.loads(cand_path.read_text(encoding="utf-8"))
    pool = pool_from_candidates_json(data)

    valid = {c.idx for c in pool.candidates}
    unknown = [i for i in chosen_idx if i not in valid]
    if unknown:
        raise ValueError(f"候補にない番号: {unknown}(範囲は 1〜{max(valid)})")
    if len(set(chosen_idx)) != data.get("count"):
        log.warning("選択 %d 枚(想定 %d 枚)", len(set(chosen_idx)), data.get("count"))

    selection = finalize(pool, list(dict.fromkeys(chosen_idx)), cfg)
    covered = {g.group_id for g in selection.groups}
    missing = sorted(set(pool.group_sizes) - covered)
    if missing:
        log.warning("未カバーのグループ: %s(場所が偏っている可能性)", missing)

    path = write_selection(out_dir, selection, list(dict.fromkeys(chosen_idx)),
                           source=data.get("source", str(out_dir)))
    log.info("selection.json を書き出し: %s (%d 枚)", path, len(selection.order))
    return path


def do_plan(source: str | Path, count: int, cfg: Config, out_dir: Path,
            use_llm: bool = True, music_override: str | None = None,
            assets_dir: Path | None = None, selection_path: str | Path | None = None,
            proposal_path: str | Path | None = None) -> Path:
    source = Path(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = assets_dir or (Path(__file__).resolve().parent.parent / "assets")

    metas = _scan_cached(source)
    if not metas:
        raise RuntimeError(f"画像が見つかりません: {source}")
    by_path = {m.path: m for m in metas}

    if selection_path:
        selection = _selection_from_json(
            json.loads(Path(selection_path).read_text(encoding="utf-8")), by_path
        )
        count = len(selection.order)
    else:
        groups = group_images(metas, cfg)
        selection = select(groups, count, cfg)
    if not selection.order:
        raise RuntimeError("選択結果が空です")

    proposal, sheet = propose(selection, by_path, source, cfg, out_dir,
                              use_llm=use_llm, proposal_path=proposal_path)

    total_seconds = cfg.title_card_seconds + len(selection.order) * cfg.seconds_per_slide
    music = choose_music(proposal.mood, total_seconds, assets_dir, override=music_override)

    plan_path = out_dir / "plan.json"
    plan_path.write_text(
        json.dumps(
            _plan_dict(source, count, cfg, selection, proposal, music, sheet),
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    log.info("plan を書き出し: %s", plan_path)
    return plan_path


def do_render(plan_path: str | Path, out_path: str | Path | None = None,
              keep_workdir: bool = False) -> Path:
    plan_path = Path(plan_path)
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    cfg = Config(**data["config"])
    selection, proposal = _plan_to_objects(data)

    from .music import MusicChoice

    music = None
    if data.get("music"):
        mp = Path(data["music"]["path"])
        if mp.exists():
            music = MusicChoice(mp, data["music"].get("duration"),
                                data["music"].get("license", ""), data["music"].get("mood", ""))
        else:
            log.warning("music が実在しません: %s。無音でレンダリング", mp)

    out_path = Path(out_path) if out_path else plan_path.parent / "out.mp4"
    return render_video(selection, proposal, cfg, music, out_path, keep_workdir=keep_workdir)


def do_run(source: str | Path, count: int, cfg: Config, out_dir: Path,
           use_llm: bool = True, music_override: str | None = None,
           assets_dir: Path | None = None, keep_workdir: bool = False) -> Path:
    plan_path = do_plan(source, count, cfg, out_dir, use_llm, music_override, assets_dir)
    return do_render(plan_path, out_dir / "out.mp4", keep_workdir=keep_workdir)


# --- plan.json (de)serialize -------------------------------------------------

def _plan_dict(source: Path, count: int, cfg: Config, selection: Selection,
               proposal: Proposal, music, sheet: Path) -> dict:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(source),
        "count_per_folder": count,
        "config": {
            "seconds_per_slide": cfg.seconds_per_slide,
            "transition_seconds": cfg.transition_seconds,
            "title_card_seconds": cfg.title_card_seconds,
            "aspect": cfg.aspect,
            "fps": cfg.fps,
            "ken_burns": cfg.ken_burns,
            "burn_captions": cfg.burn_captions,
            "audio_fade_in": cfg.audio_fade_in,
            "audio_fade_out": cfg.audio_fade_out,
            "audio_lufs": cfg.audio_lufs,
        },
        "title": proposal.title,
        "mood": proposal.mood,
        "music_style": proposal.music_style,
        "hashtags": proposal.hashtags,
        "contact_sheet": str(sheet),
        "music": None if music is None else {
            "path": str(music.path),
            "duration": music.duration,
            "license": music.license,
            "mood": music.mood,
        },
        "groups": [
            {
                "group_id": g.group_id,
                "kind": g.kind,
                "label": proposal.group_labels.get(g.group_id, ""),
                "centroid": [g.centroid_lat, g.centroid_lon]
                if g.centroid_lat is not None else None,
                "t_start": g.t_start.isoformat() if g.t_start else None,
                "images": [im.path for im in g.images],
            }
            for g in selection.groups
        ],
        "order": selection.order,
        "captions": proposal.captions,
        "reasons": selection.reasons,
    }


def _selection_from_json(data: dict, by_path: dict[str, ImageMeta]) -> Selection:
    """pick が書いた selection.json を、キャッシュ済みメタ付きの Selection に戻す。"""
    groups = []
    for g in data["groups"]:
        imgs = [by_path.get(p) or ImageMeta(path=p, folder=str(Path(p).parent))
                for p in g["images"]]
        cen = g.get("centroid") or (None, None)
        groups.append(Group(group_id=g["group_id"], kind=g.get("kind", ""), images=imgs,
                            centroid_lat=cen[0], centroid_lon=cen[1]))
    return Selection(groups=groups, order=data["order"], reasons=data.get("reasons", {}))


def _plan_to_objects(data: dict) -> tuple[Selection, Proposal]:
    groups = []
    for g in data["groups"]:
        imgs = [ImageMeta(path=p, folder=str(Path(p).parent)) for p in g["images"]]
        grp = Group(group_id=g["group_id"], kind=g["kind"], images=imgs, label=g.get("label", ""))
        groups.append(grp)
    selection = Selection(groups=groups, order=data["order"], reasons=data.get("reasons", {}))
    proposal = Proposal(
        title=data["title"],
        mood=data["mood"],
        music_style=data.get("music_style", ""),
        group_labels={g["group_id"]: g.get("label", "") for g in data["groups"]},
        captions=data.get("captions", {}),
        hashtags=data.get("hashtags", []),
    )
    return selection, proposal
