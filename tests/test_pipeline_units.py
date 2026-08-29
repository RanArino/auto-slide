"""純粋ロジックの検証: 配分・farthest-point・時間分割・xfade フィルタ文字列。"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from autoslide.config import Config
from autoslide.grouping import _split_by_time
from autoslide.models import Group, ImageMeta
from autoslide.render import build_filtergraph
from autoslide.selection import (
    _allocate,
    _dedupe,
    _farthest_point,
    finalize,
    select,
    shortlist,
)


def _im(path, dt=None, emb=None, phash=0, sharp=1.0):
    return ImageMeta(path=path, folder="f", taken_at=dt,
                     embedding=None if emb is None else np.array(emb, dtype=np.float32),
                     phash=phash, sharpness=sharp)


def test_allocate_sums_to_total_and_respects_capacity():
    alloc = _allocate([10, 5, 1], 8)
    assert sum(alloc) == 8
    assert alloc[2] <= 1
    assert all(a >= 1 for a in alloc)


def test_allocate_when_total_below_group_count():
    alloc = _allocate([3, 2, 9, 1], 2)
    assert sum(alloc) == 2
    # 大きいグループが優先される
    assert alloc[2] == 1


def test_split_by_time_breaks_on_gap():
    base = datetime(2025, 1, 1, 9, 0, 0)
    imgs = [_im("a", base), _im("b", base + timedelta(minutes=5)),
            _im("c", base + timedelta(hours=3))]
    segs = _split_by_time(imgs, gap_minutes=30)
    assert [len(s) for s in segs] == [2, 1]


def test_dedupe_keeps_sharpest_per_bucket():
    imgs = [_im("a", phash=0b0000, sharp=1.0),
            _im("b", phash=0b0001, sharp=5.0),   # a と近い -> 同バケット
            _im("c", phash=0xFFFF, sharp=2.0)]
    reps = _dedupe(imgs, hamming_max=6)
    kept = sorted(im.path for im in reps)
    assert kept == ["b", "c"]


def test_farthest_point_picks_spread():
    imgs = [_im("a", emb=[0, 0]), _im("b", emb=[0, 0.01]),
            _im("c", emb=[1, 1]), _im("d", emb=[1, 0.99])]
    picked = {im.path for im in _farthest_point(imgs, 2, seed=0)}
    # 近接ペアから 1 枚ずつ = 必ず一方の端ともう一方の端
    assert picked in ({"a", "c"}, {"a", "d"}, {"b", "c"}, {"b", "d"})


def test_select_is_deterministic():
    base = datetime(2025, 1, 1, 9, 0, 0)
    # 実際の pHash は連写以外では十分離れる。各値を 16bit ずつずらして別バケットにする。
    imgs = [_im(f"p{i}", base + timedelta(minutes=i), emb=[i, -i],
                phash=0xFF << (8 * i), sharp=1.0 + i)
            for i in range(8)]
    g = Group(0, "time", imgs)
    cfg = Config()
    a = select([g], 4, cfg).order
    b = select([g], 4, cfg).order
    assert a == b
    assert len(a) == 4


def test_shortlist_oversamples_and_numbers_and_covers_groups():
    base = datetime(2025, 1, 1, 9, 0, 0)
    g0 = Group(0, "time", [_im(f"a{i}", base + timedelta(minutes=i), emb=[i, 0],
                               phash=0xF << (8 * i), sharp=1.0 + i) for i in range(6)])
    g1 = Group(1, "time", [_im(f"b{i}", base + timedelta(hours=4, minutes=i), emb=[0, i],
                               phash=0xF << (8 * (i + 6)), sharp=1.0 + i) for i in range(6)])
    pool = shortlist([g0, g1], count=4, cfg=Config(), pool_factor=2.5)
    # 4 * 2.5 = 10 <= 在庫12
    assert len(pool.candidates) == 10
    assert [c.idx for c in pool.candidates] == list(range(1, 11))   # 1 始まり連番
    assert {c.group_id for c in pool.candidates} == {0, 1}          # 両グループから
    assert pool.group_sizes == {0: 6, 1: 6}


def test_finalize_picks_only_chosen_and_orders_by_time():
    base = datetime(2025, 1, 1, 9, 0, 0)
    g0 = Group(0, "time", [_im(f"a{i}", base + timedelta(hours=4, minutes=i), emb=[i, 0],
                               phash=0xF << (8 * i), sharp=5.0) for i in range(4)])
    g1 = Group(1, "time", [_im(f"b{i}", base + timedelta(minutes=i), emb=[0, i],
                               phash=0xF << (8 * (i + 4)), sharp=5.0) for i in range(4)])
    pool = shortlist([g0, g1], count=8, cfg=Config(), pool_factor=1.0)
    chosen = [c.idx for c in pool.candidates][:3]
    sel = finalize(pool, chosen, Config())
    assert len(sel.order) == 3
    # g1 は g0 より前の時刻 -> 先に来る
    assert sel.groups[0].group_id == 1
    # 各グループ内は時刻昇順
    for g in sel.groups:
        ts = [im.taken_at for im in g.images]
        assert ts == sorted(ts)


def test_build_filtergraph_offsets():
    graph, label = build_filtergraph(3, [3.0, 4.75, 4.75], t=0.75, w=1920, h=1080, fps=30)
    assert label == "[vout]"
    assert "offset=2.250" in graph          # 3.0 - 1*0.75
    assert "offset=6.250" in graph          # 3.0 + 4.75 - 2*0.75
    assert graph.count("xfade") == 2


def test_build_filtergraph_single_input():
    graph, label = build_filtergraph(1, [4.0], t=0.75, w=1080, h=1080, fps=30)
    assert label == "[v0]"
    assert "xfade" not in graph
