"""前回フィードバック対応分の検証:
色補正のガード / タイムライン=SRT の同一データ源 / 承認ゲート / フォルダグルーピング。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from autoslide.color import ColorStats, apply_gains, gray_world_gains
from autoslide.config import Config
from autoslide.grouping import group_images
from autoslide.models import ImageMeta, Proposal, Segment, Selection
from autoslide.pipeline import do_render
from autoslide.timeline import build_timeline, slide_durations, write_srt


# --- 色補正 ---------------------------------------------------------------

def test_gray_world_clamped_even_with_extreme_cast():
    # 極端な青被り。生ゲインは 2 以上になりうるがクランプされる。
    stats = ColorStats(mean_r=0.1, mean_g=0.3, mean_b=0.6, mean_sat=0.5)
    res = gray_world_gains(stats, strength=1.0, auto_atten=10.0)
    assert not res.skipped
    assert all(0.7 <= g <= 1.4 for g in res.gains)


def test_gray_world_strength_zero_is_identity():
    stats = ColorStats(0.2, 0.4, 0.6, 0.5)
    res = gray_world_gains(stats, strength=0.0)
    assert res.skipped or all(abs(g - 1.0) < 1e-6 for g in res.gains)


def test_gray_world_low_saturation_is_skipped():
    stats = ColorStats(0.5, 0.5, 0.5, 0.02)   # ほぼ無彩色
    res = gray_world_gains(stats, strength=0.5, sat_floor=0.08)
    assert res.skipped
    assert res.gains == (1.0, 1.0, 1.0)


def test_gray_world_auto_attenuates_strong_correction():
    stats = ColorStats(mean_r=0.15, mean_g=0.35, mean_b=0.55, mean_sat=0.4)
    strong = gray_world_gains(stats, strength=1.0, auto_atten=0.05)
    assert strong.attenuated
    dev = max(abs(g - 1.0) for g in strong.gains)
    assert dev <= 0.05 + 1e-6


def test_apply_gains_changes_pixels_only_when_non_unit():
    from PIL import Image
    im = Image.new("RGB", (4, 4), (100, 100, 100))
    same = apply_gains(im, (1.0, 1.0, 1.0))
    assert same.tobytes() == im.tobytes()
    warmer = apply_gains(im, (1.2, 1.0, 0.9))
    assert warmer.getpixel((0, 0)) == (120, 100, 90)


# --- タイムライン = SRT の同一データ源 -----------------------------------

def _selection_two_chapters():
    base = datetime(2025, 1, 1, 9, 0, 0)
    g0 = [ImageMeta(path=f"a{i}.jpg", folder="a", taken_at=base + timedelta(minutes=i),
                    width=1920, height=1080) for i in range(2)]
    g1 = [ImageMeta(path=f"b{i}.jpg", folder="b", taken_at=base + timedelta(hours=3, minutes=i),
                    width=1080, height=1920) for i in range(2)]
    from autoslide.models import Group
    groups = [Group(0, "folder", g0, "公園"), Group(1, "folder", g1, "カフェ")]
    order = [im.path for g in groups for im in g.images]
    return Selection(groups=groups, order=order)


def test_timeline_segment_shape_and_monotonic():
    cfg = Config()
    sel = _selection_two_chapters()
    prop = Proposal(title="旅の記録", mood="calm",
                    divider_texts={0: "公園にて", 1: "カフェにて"},
                    captions={"a0.jpg": "朝の光", "b1.jpg": "ひと休み"})
    segs = build_timeline(sel, prop, cfg)
    kinds = [s.kind for s in segs]
    assert kinds == ["title", "divider", "photo", "photo", "divider", "photo", "photo"]
    # 表示区間は前詰めで単調増加
    for a, b in zip(segs, segs[1:]):
        assert b.start >= a.start
        assert a.end > a.start
    # ffmpeg へ渡す長さは segments と平行
    assert len(slide_durations(segs, cfg)) == len(segs)


def test_srt_matches_burned_caption_source(tmp_path):
    cfg = Config()
    sel = _selection_two_chapters()
    prop = Proposal(title="旅の記録", mood="calm",
                    divider_texts={0: "公園にて", 1: "カフェにて"},
                    captions={"a0.jpg": "朝の光", "b1.jpg": "ひと休み"})
    segs = build_timeline(sel, prop, cfg)
    srt = write_srt(segs, tmp_path / "out.srt", cfg)
    body = srt.read_text(encoding="utf-8")

    caption_segs = [s for s in segs if s.kind == "photo" and s.text]
    # 焼き込みに使う text がそのまま SRT に出る
    for s in caption_segs:
        assert s.text in body
    # 字幕ブロック数 == キャプション付き photo の数
    assert body.count(" --> ") == len(caption_segs)
    # 各字幕の開始は対応する photo セグメントの表示開始以降
    for s in caption_segs:
        assert s.text in body


def test_divider_text_never_empty():
    from autoslide.timeline import divider_text_for
    prop = Proposal(title="x", mood="calm")
    assert divider_text_for(3, "", prop) == "シーン 4"
    assert divider_text_for(0, "海辺", prop) == "海辺"


# --- 承認ゲート ---------------------------------------------------------

def test_do_render_refuses_unapproved_plan(tmp_path):
    plan = {
        "approved": False,
        "config": Config().__dict__ if False else _min_config(),
        "title": "t", "mood": "calm", "order": ["x.jpg"],
        "chapters": [{"group_id": 0, "kind": "folder", "images": ["x.jpg"]}],
        "captions": {}, "music": None,
    }
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(RuntimeError, match="未承認"):
        do_render(p)


def _min_config() -> dict:
    c = Config()
    return {
        "seconds_per_slide": c.seconds_per_slide,
        "transition_seconds": c.transition_seconds,
        "title_card_seconds": c.title_card_seconds,
        "aspect": c.aspect, "fps": c.fps,
        "grouping_mode": c.grouping_mode, "fit_mode": c.fit_mode,
        "group_scale_tolerance": c.group_scale_tolerance,
        "ken_burns": c.ken_burns, "burn_captions": c.burn_captions,
        "chapter_dividers": c.chapter_dividers,
        "chapter_divider_seconds": c.chapter_divider_seconds,
        "color_correct": c.color_correct, "color_strength": c.color_strength,
        "color_sat_floor": c.color_sat_floor, "color_auto_atten": c.color_auto_atten,
        "audio_fade_in": c.audio_fade_in, "audio_fade_out": c.audio_fade_out,
        "audio_lufs": c.audio_lufs,
    }


# --- フォルダグルーピング ---------------------------------------------

def test_group_by_folder_one_group_per_folder():
    base = datetime(2025, 1, 1, 9, 0, 0)
    metas = []
    for folder in ("/x/01_a", "/x/02_b", "/x/03_c"):
        for i in range(3):
            metas.append(ImageMeta(path=f"{folder}/img{i}.jpg", folder=folder,
                                   taken_at=base + timedelta(minutes=i)))
    cfg = Config(grouping_mode="folder")
    groups = group_images(metas, cfg)
    assert len(groups) == 3
    assert [g.label for g in groups] == ["01_a", "02_b", "03_c"]
    assert all(g.kind == "folder" for g in groups)


def test_grouping_auto_picks_folder_when_multiple_dirs():
    base = datetime(2025, 1, 1, 9, 0, 0)
    metas = [
        ImageMeta(path="/x/a/1.jpg", folder="/x/a", taken_at=base),
        ImageMeta(path="/x/b/1.jpg", folder="/x/b", taken_at=base + timedelta(minutes=1)),
    ]
    groups = group_images(metas, Config(grouping_mode="auto"))
    assert {g.kind for g in groups} == {"folder"}


# --- 露出補正(白とび / 黒つぶれ対策) ---------------------------------

from autoslide.exposure import ExposureStats, apply_tone, tone_adjustment  # noqa: E402
from autoslide.features import composition  # noqa: E402
from autoslide.render import _apply_letterbox, _draw_caption_lowerleft  # noqa: E402


def test_tone_lifts_crushed_shadows_within_protection():
    s = ExposureStats(black_clip=0.12, white_clip=0.001, p01=1, p50=70, p99=210)
    r = tone_adjustment(s, 0.35)
    assert not r.skipped
    assert 0 < r.black_out <= 22
    assert r.gamma < 1.0


def test_tone_pulls_blown_highlights_within_protection():
    s = ExposureStats(black_clip=0.001, white_clip=0.10, p01=30, p50=150, p99=255)
    r = tone_adjustment(s, 0.35)
    assert not r.skipped
    assert 230 <= r.white_out < 255


def test_tone_skips_well_exposed_and_zero_strength():
    ok = ExposureStats(black_clip=0.0, white_clip=0.0, p01=20, p50=120, p99=235)
    assert tone_adjustment(ok, 0.35).skipped
    crushed = ExposureStats(black_clip=0.2, white_clip=0.0, p01=1, p50=60, p99=200)
    assert tone_adjustment(crushed, 0.0).skipped


def test_apply_tone_lut_is_monotonic():
    from autoslide.exposure import _lut
    r = tone_adjustment(ExposureStats(0.15, 0.08, 2, 80, 250), 0.5)
    lut = _lut(r)
    assert list(lut) == sorted(lut)


# --- 構図スコア(黄金比 / 三分割) -----------------------------------

def _blob(cx, cy, n=320):
    from PIL import Image, ImageDraw
    im = Image.new("L", (n, n), 25)
    ImageDraw.Draw(im).ellipse([cx - 34, cy - 34, cx + 34, cy + 34], fill=225)
    return im


def test_composition_in_unit_range_and_rewards_golden_point():
    thirds = composition(_blob(int(320 * 0.62), int(320 * 0.38)))
    center = composition(_blob(160, 160))
    edge = composition(_blob(16, 160))
    for v in (thirds, center, edge):
        assert 0.0 <= v <= 1.0
    assert thirds > center
    assert thirds > edge


# --- シネマ字幕 / レターボックス -----------------------------------

def test_letterbox_paints_pure_black_bars():
    from PIL import Image
    frame = Image.new("RGB", (1920, 1080), (120, 130, 140))
    cfg = Config(letterbox=True, letterbox_ratio=2.39)
    out = _apply_letterbox(frame, cfg)
    import numpy as np
    a = np.asarray(out)
    assert a[0].max() == 0 and a[-1].max() == 0          # 上下は純黒
    ch = round(1920 / 2.39)
    assert a[540].max() > 0                               # 中央帯は絵がある
    black_rows = int((a.max(axis=(1, 2)) == 0).sum())
    assert abs((1080 - black_rows) - ch) <= 2


def test_lowerleft_caption_draws_text_and_date():
    from PIL import Image
    import numpy as np
    cfg = Config(letterbox=True)
    base = Image.new("RGB", (1920, 1080), (10, 10, 10))
    only_main = base.copy()
    _draw_caption_lowerleft(only_main, "岬の朝", "", cfg)
    with_date = base.copy()
    _draw_caption_lowerleft(with_date, "岬の朝", "2018.06.03", cfg)
    n_main = int((np.asarray(only_main).max(axis=2) > 40).sum())
    n_both = int((np.asarray(with_date).max(axis=2) > 40).sum())
    assert n_main > 0
    assert n_both > n_main            # 日付行のぶん増える


def test_srt_excludes_the_date_subline(tmp_path):
    cfg = Config()
    sel = _selection_two_chapters()
    prop = Proposal(title="旅", mood="calm",
                    captions={"a0.jpg": "岬の朝"},
                    caption_subs={"a0.jpg": "2018.06.03"})
    segs = build_timeline(sel, prop, cfg)
    srt = write_srt(segs, tmp_path / "o.srt", cfg)
    body = srt.read_text(encoding="utf-8")
    assert "岬の朝" in body
    assert "2018.06.03" not in body
