"""scan / plan / summary / approve / render をつなぐ。plan.json の読み書きもここ。"""

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
from .color import ColorStats, apply_gains, gray_world_gains
from .config import Config
from .db import Cache
from .exposure import ToneResult, apply_tone, tone_adjustment
from .grouping import group_images
from .models import Group, ImageMeta, Proposal, Selection
from .music import choose_music
from .proposal import propose
from .render import render as render_video
from .scan import scan
from .selection import finalize, select, shortlist

log = logging.getLogger("autoslide.pipeline")

_MAX_TONE_PREVIEWS = 12


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


# --- 自動補正(露出 → 色)の下ごしらえ -----------------------------------

def _color_stats_for(meta: ImageMeta | None, path: str) -> ColorStats:
    if meta and meta.mean_r is not None:
        return ColorStats(meta.mean_r, meta.mean_g, meta.mean_b, meta.mean_sat)
    from .color import measure_path
    return measure_path(path)


def _gains_for(path: str, by_path: dict[str, ImageMeta], cfg: Config):
    return gray_world_gains(_color_stats_for(by_path.get(path), path), cfg.color_strength,
                            sat_floor=cfg.color_sat_floor, auto_atten=cfg.color_auto_atten)


def _tone_for(path: str, by_path: dict[str, ImageMeta], cfg: Config) -> ToneResult:
    from .exposure import measure_path
    meta = by_path.get(path)
    src = (meta.thumb_path if meta and meta.thumb_path else path)
    return tone_adjustment(measure_path(src), cfg.exposure_strength,
                           hi_thresh=cfg.exposure_hi_thresh, lo_thresh=cfg.exposure_lo_thresh)


def _build_color_block(selection: Selection, by_path: dict[str, ImageMeta],
                       cfg: Config) -> dict:
    per_image: dict[str, dict] = {}
    for path in selection.order:
        res = _gains_for(path, by_path, cfg)
        per_image[path] = {
            "gains": list(res.gains),
            "skipped": res.skipped,
            "attenuated": res.attenuated,
            "disabled": False,
        }
    return {"strength": cfg.color_strength, "per_image": per_image}


def _build_exposure_block(selection: Selection, by_path: dict[str, ImageMeta],
                          cfg: Config) -> dict:
    per_image: dict[str, dict] = {}
    for path in selection.order:
        res = _tone_for(path, by_path, cfg)
        per_image[path] = {
            "black_out": res.black_out,
            "white_out": res.white_out,
            "gamma": res.gamma,
            "skipped": res.skipped,
            "attenuated": res.attenuated,
            "disabled": False,
        }
    return {"strength": cfg.exposure_strength, "per_image": per_image}


def _build_tone_previews(order: list[str], by_path: dict[str, ImageMeta],
                         exposure: dict | None, color: dict | None,
                         cfg: Config, out_dir: Path) -> None:
    """before | after(露出→色) の横並び JPEG。exposure/color 双方の要約から参照する。"""
    from PIL import Image

    exp_per = (exposure or {}).get("per_image", {})
    col_per = (color or {}).get("per_image", {})
    made = 0
    for i, path in enumerate(order):
        if made >= _MAX_TONE_PREVIEWS:
            break
        e = exp_per.get(path, {})
        c = col_per.get(path, {})
        tone = ToneResult(e.get("black_out", 0.0), e.get("white_out", 255.0),
                          e.get("gamma", 1.0), e.get("skipped", True), e.get("attenuated", False))
        gains = tuple(c.get("gains", (1.0, 1.0, 1.0)))
        if tone.is_noop and all(abs(g - 1.0) < 1e-3 for g in gains):
            continue
        try:
            with Image.open(path) as im:
                im.load()
                before = im.convert("RGB")
            before.thumbnail((480, 480), Image.LANCZOS)
            after = apply_gains(apply_tone(before, tone), gains)
            w, h = before.size
            canvas = Image.new("RGB", (w * 2 + 8, h), (12, 12, 14))
            canvas.paste(before, (0, 0))
            canvas.paste(after, (w + 8, 0))
            dst = out_dir / "tone_preview" / f"{i:02d}.jpg"
            dst.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(dst, "JPEG", quality=88)
            if path in exp_per:
                exp_per[path]["preview"] = str(dst)
            if path in col_per:
                col_per[path]["preview"] = str(dst)
            made += 1
        except Exception as ex:  # noqa: BLE001
            log.warning("tone preview 失敗 %s: %s", path, ex)


# --- plan ---------------------------------------------------------------

def do_plan(source: str | Path, count: int, cfg: Config, out_dir: Path,
            use_llm: bool = True, music_override: str | None = None,
            assets_dir: Path | None = None, selection_path: str | Path | None = None,
            proposal_path: str | Path | None = None,
            refresh_proposal: bool = False) -> Path:
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
                              use_llm=use_llm, proposal_path=proposal_path,
                              refresh=refresh_proposal)
    # 日付副題(EXIF 由来、編集可)
    proposal.caption_subs = {
        p: by_path[p].taken_at.strftime("%Y.%m.%d")
        for p in selection.order
        if by_path.get(p) and by_path[p].taken_at
    }

    n_dividers = len(selection.groups) if cfg.chapter_dividers else 0
    total_seconds = (cfg.title_card_seconds
                     + n_dividers * cfg.chapter_divider_seconds
                     + len(selection.order) * cfg.seconds_per_slide)
    music = choose_music(proposal.mood, total_seconds, assets_dir, override=music_override)

    color = _build_color_block(selection, by_path, cfg) if cfg.color_correct else None
    exposure = _build_exposure_block(selection, by_path, cfg) if cfg.exposure_correct else None
    if color or exposure:
        _build_tone_previews(selection.order, by_path, exposure, color, cfg, out_dir)

    composition = {
        p: round(by_path[p].composition, 3)
        for p in selection.order
        if by_path.get(p) and by_path[p].composition is not None
    }

    plan_path = out_dir / "plan.json"
    plan_path.write_text(
        json.dumps(
            _plan_dict(source, count, cfg, selection, proposal, music, sheet,
                       color, exposure, composition),
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    log.info("plan を書き出し: %s (未承認)", plan_path)
    return plan_path


def do_summary(plan_path: str | Path) -> str:
    from .summary import render_summary

    data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    return render_summary(data, plan_path=Path(plan_path))


def do_approve(plan_path: str | Path, approved: bool = True) -> Path:
    plan_path = Path(plan_path)
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    data["approved"] = bool(approved)
    plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("plan.json approved=%s", data["approved"])
    return plan_path


def do_doctor() -> int:
    from .doctor import run_doctor

    return run_doctor()


def do_render(plan_path: str | Path, out_path: str | Path | None = None,
              keep_workdir: bool = False, force: bool = False) -> Path:
    plan_path = Path(plan_path)
    data = json.loads(plan_path.read_text(encoding="utf-8"))

    if data.get("approved") is not True and not force:
        raise RuntimeError(
            "plan.json が未承認です。'autoslide summary' で内容を確認し、"
            "問題なければ 'autoslide approve' で承認してから render してください"
            "(レビューを飛ばすなら --force)。"
        )

    cfg = Config(**data["config"])
    selection, proposal = _plan_to_objects(data)

    color_map = _color_map_from_plan(data) if cfg.color_correct else None
    tone_map = _tone_map_from_plan(data) if cfg.exposure_correct else None

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
    return render_video(selection, proposal, cfg, music, out_path,
                        keep_workdir=keep_workdir, color_map=color_map, tone_map=tone_map)


def do_run(source: str | Path, count: int, cfg: Config, out_dir: Path,
           use_llm: bool = True, music_override: str | None = None,
           assets_dir: Path | None = None, keep_workdir: bool = False,
           approve: bool = False) -> Path:
    plan_path = do_plan(source, count, cfg, out_dir, use_llm, music_override, assets_dir)
    if not approve:
        log.info("plan まで完了。内容を確認して 'autoslide approve' → 'autoslide render' してください")
        print(do_summary(plan_path))
        return plan_path
    do_approve(plan_path, True)
    return do_render(plan_path, out_dir / "out.mp4", keep_workdir=keep_workdir)


# --- plan.json (de)serialize -------------------------------------------------

def _config_subset(cfg: Config) -> dict:
    return {
        "seconds_per_slide": cfg.seconds_per_slide,
        "transition_seconds": cfg.transition_seconds,
        "title_card_seconds": cfg.title_card_seconds,
        "aspect": cfg.aspect,
        "fps": cfg.fps,
        "grouping_mode": cfg.grouping_mode,
        "fit_mode": cfg.fit_mode,
        "group_scale_tolerance": cfg.group_scale_tolerance,
        "ken_burns": cfg.ken_burns,
        "burn_captions": cfg.burn_captions,
        "caption_style": cfg.caption_style,
        "caption_date_always": cfg.caption_date_always,
        "letterbox": cfg.letterbox,
        "letterbox_ratio": cfg.letterbox_ratio,
        "chapter_dividers": cfg.chapter_dividers,
        "chapter_divider_seconds": cfg.chapter_divider_seconds,
        "color_correct": cfg.color_correct,
        "color_strength": cfg.color_strength,
        "color_sat_floor": cfg.color_sat_floor,
        "color_auto_atten": cfg.color_auto_atten,
        "exposure_correct": cfg.exposure_correct,
        "exposure_strength": cfg.exposure_strength,
        "exposure_hi_thresh": cfg.exposure_hi_thresh,
        "exposure_lo_thresh": cfg.exposure_lo_thresh,
        "audio_fade_in": cfg.audio_fade_in,
        "audio_fade_out": cfg.audio_fade_out,
        "audio_lufs": cfg.audio_lufs,
    }


def _plan_dict(source: Path, count: int, cfg: Config, selection: Selection,
               proposal: Proposal, music, sheet: Path, color: dict | None,
               exposure: dict | None, composition: dict | None) -> dict:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "approved": False,
        "source": str(source),
        "count_per_folder": count,
        "config": _config_subset(cfg),
        "title": proposal.title,
        "mood": proposal.mood,
        "overall_tone": proposal.overall_tone,
        "color_note": proposal.color_note,
        "music_style": proposal.music_style,
        "hashtags": proposal.hashtags,
        "contact_sheet": str(sheet),
        "music": None if music is None else {
            "path": str(music.path),
            "duration": music.duration,
            "license": music.license,
            "mood": music.mood,
        },
        "chapters": [
            {
                "group_id": g.group_id,
                "kind": g.kind,
                "label": proposal.group_labels.get(g.group_id, ""),
                "divider_text": proposal.divider_texts.get(g.group_id, ""),
                "centroid": [g.centroid_lat, g.centroid_lon]
                if g.centroid_lat is not None else None,
                "t_start": g.t_start.isoformat() if g.t_start else None,
                "images": [im.path for im in g.images],
            }
            for g in selection.groups
        ],
        "order": selection.order,
        "captions": proposal.captions,
        "caption_sub": proposal.caption_subs,
        "composition": composition or {},
        "color": color,
        "exposure": exposure,
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
                            label=g.get("label", ""),
                            centroid_lat=cen[0], centroid_lon=cen[1]))
    return Selection(groups=groups, order=data["order"], reasons=data.get("reasons", {}))


def _plan_to_objects(data: dict) -> tuple[Selection, Proposal]:
    chapters = data.get("chapters") or data.get("groups") or []
    groups = []
    for g in chapters:
        imgs = [ImageMeta(path=p, folder=str(Path(p).parent)) for p in g["images"]]
        grp = Group(group_id=g["group_id"], kind=g.get("kind", ""), images=imgs,
                    label=g.get("label", ""))
        cen = g.get("centroid") or (None, None)
        grp.centroid_lat, grp.centroid_lon = cen[0], cen[1]
        groups.append(grp)
    selection = Selection(groups=groups, order=data["order"], reasons=data.get("reasons", {}))
    proposal = Proposal(
        title=data["title"],
        mood=data["mood"],
        overall_tone=data.get("overall_tone", ""),
        color_note=data.get("color_note", ""),
        music_style=data.get("music_style", ""),
        group_labels={g["group_id"]: g.get("label", "") for g in chapters},
        divider_texts={g["group_id"]: g.get("divider_text", "") for g in chapters},
        captions=data.get("captions", {}),
        caption_subs=data.get("caption_sub", {}),
        hashtags=data.get("hashtags", []),
    )
    return selection, proposal


def _color_map_from_plan(data: dict) -> dict[str, tuple[float, float, float]]:
    out: dict[str, tuple[float, float, float]] = {}
    per = ((data.get("color") or {}).get("per_image")) or {}
    for path, e in per.items():
        if e.get("disabled") or e.get("skipped"):
            continue
        g = e.get("gains")
        if g and len(g) == 3 and any(abs(float(x) - 1.0) > 1e-3 for x in g):
            out[path] = (float(g[0]), float(g[1]), float(g[2]))
    return out


def _tone_map_from_plan(data: dict) -> dict[str, ToneResult]:
    out: dict[str, ToneResult] = {}
    per = ((data.get("exposure") or {}).get("per_image")) or {}
    for path, e in per.items():
        if e.get("disabled") or e.get("skipped"):
            continue
        res = ToneResult(float(e.get("black_out", 0.0)), float(e.get("white_out", 255.0)),
                         float(e.get("gamma", 1.0)), False, bool(e.get("attenuated", False)))
        if not res.is_noop:
            out[path] = res
    return out
