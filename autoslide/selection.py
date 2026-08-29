"""N 枚選択。

2 段構成:
  shortlist() … アルゴリズムで候補プールに絞る(重複除去・ブレ落とし・場所/類似の分散)
  finalize()  … プールから選ばれた分だけで再生順を確定する

最終的に「どれを残すか」は shortlist が返したプールの中からしか選べない。
アルゴリズムだけで完結させたい場合は select()(= shortlist(factor=1.0) → finalize(全部))。
同じ seed なら shortlist の結果は決定的。
"""

from __future__ import annotations

import logging
import math

import numpy as np

from .config import Config
from .features import hamming
from .models import Candidate, CandidatePool, Group, ImageMeta, Selection

log = logging.getLogger("autoslide.selection")

_INF = float("inf")


def _dedupe(images: list[ImageMeta], hamming_max: int) -> list[ImageMeta]:
    """pHash のハミング距離で union-find し、各グループの代表(最大 sharpness)を返す。"""
    have_hash = [im for im in images if im.phash is not None]
    no_hash = [im for im in images if im.phash is None]
    n = len(have_hash)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if hamming(have_hash[i].phash, have_hash[j].phash) <= hamming_max:
                parent[find(i)] = find(j)

    buckets: dict[int, list[ImageMeta]] = {}
    for i, im in enumerate(have_hash):
        buckets.setdefault(find(i), []).append(im)

    reps = [max(b, key=lambda im: im.sharpness) for b in buckets.values()]
    return reps + no_hash


def _allocate(group_sizes: list[int], total: int) -> list[int]:
    """平方根重み付けで total 枚を配分。各グループ最低 1、ただし total が足りなければ上位から。"""
    k = len(group_sizes)
    if total <= 0 or k == 0:
        return [0] * k
    if total <= k:
        order = sorted(range(k), key=lambda i: -group_sizes[i])
        alloc = [0] * k
        for i in order[:total]:
            alloc[i] = 1
        return alloc

    weights = [math.sqrt(s) for s in group_sizes]
    wsum = sum(weights) or 1.0
    raw = [1 + (total - k) * w / wsum for w in weights]
    alloc = [min(int(r), group_sizes[i]) for i, r in enumerate(raw)]

    # 端数を残余の大きいグループへ配る
    while sum(alloc) < total:
        cand = [i for i in range(k) if alloc[i] < group_sizes[i]]
        if not cand:
            break
        i = max(cand, key=lambda i: raw[i] - alloc[i])
        alloc[i] += 1
    while sum(alloc) > total:
        i = max(range(k), key=lambda i: alloc[i] - raw[i] if alloc[i] > 1 else -1e9)
        alloc[i] -= 1
    return alloc


def _quality_filter(images: list[ImageMeta], keep_ratio: float = 0.8) -> list[ImageMeta]:
    if len(images) <= 3:
        return images
    sharp = sorted(images, key=lambda im: im.sharpness)
    cut = int(len(sharp) * (1 - keep_ratio))
    return sharp[cut:]


def _farthest_point(images: list[ImageMeta], k: int, seed: int) -> list[ImageMeta]:
    if k >= len(images):
        return images
    vecs = []
    for im in images:
        v = im.embedding
        vecs.append(v if v is not None else np.zeros(1, dtype=np.float32))
    dim = max(len(v) for v in vecs)
    mat = np.array([np.pad(v, (0, dim - len(v))) for v in vecs], dtype=np.float32)

    rng = np.random.default_rng(seed)
    start = int(rng.integers(len(images)))
    chosen = [start]
    dist = np.linalg.norm(mat - mat[start], axis=1)
    while len(chosen) < k:
        nxt = int(np.argmax(dist))
        if nxt in chosen:
            break
        chosen.append(nxt)
        dist = np.minimum(dist, np.linalg.norm(mat - mat[nxt], axis=1))
    return [images[i] for i in sorted(chosen)]


def _t_start_key(g: Group):
    return (g.t_start.timestamp() if g.t_start else _INF, g.group_id)


def shortlist(groups: list[Group], count: int, cfg: Config,
              pool_factor: float = 2.5, source: str = "") -> CandidatePool:
    """候補プールを作る。count の pool_factor 倍(上限=在庫)まで多めに拾う。"""
    deduped = [
        Group(g.group_id, g.kind, _dedupe(g.images, cfg.phash_hamming_max),
              g.label, g.centroid_lat, g.centroid_lon)
        for g in groups
    ]
    deduped = [g for g in deduped if g.images]
    deduped.sort(key=_t_start_key)
    if not deduped:
        return CandidatePool(source=source, count=count, candidates=[])

    avail = sum(len(g.images) for g in deduped)
    target = min(avail, max(count, len(deduped), math.ceil(count * pool_factor)))
    alloc = _allocate([len(g.images) for g in deduped], target)
    log.info("候補配分: %s (合計 %d / 在庫 %d)", alloc, sum(alloc), avail)

    cands: list[Candidate] = []
    idx = 0
    for g, k in zip(deduped, alloc):
        if k <= 0:
            continue
        pool = _quality_filter(g.images)
        if len(pool) < k:
            pool = g.images
        picked = _farthest_point(pool, k, cfg.seed + g.group_id)
        picked.sort(key=lambda im: (im.taken_at.timestamp() if im.taken_at else 0.0, im.path))
        for im in picked:
            idx += 1
            cands.append(Candidate(idx, im, g.group_id, g.kind,
                                   (g.centroid_lat, g.centroid_lon)))

    return CandidatePool(
        source=source,
        count=count,
        candidates=cands,
        group_sizes={g.group_id: len(g.images) for g in deduped},
        group_kinds={g.group_id: g.kind for g in deduped},
        group_centroids={g.group_id: (g.centroid_lat, g.centroid_lon) for g in deduped},
    )


def finalize(pool: CandidatePool, chosen: list[int] | list[str], cfg: Config) -> Selection:
    """プールから chosen(idx か path のリスト)だけを取り出し、再生順を確定する。"""
    by_idx = {c.idx: c for c in pool.candidates}
    by_path = {c.meta.path: c for c in pool.candidates}
    picked: list[Candidate] = []
    for x in chosen:
        c = by_idx.get(x) if isinstance(x, int) else by_path.get(x)
        if c is not None and c not in picked:
            picked.append(c)

    grouped: dict[int, list[Candidate]] = {}
    for c in picked:
        grouped.setdefault(c.group_id, []).append(c)

    out_groups: list[Group] = []
    reasons: dict[str, str] = {}
    for gid, cs in grouped.items():
        ims = sorted((c.meta for c in cs),
                     key=lambda im: (im.taken_at.timestamp() if im.taken_at else 0.0, im.path))
        cen = cs[0].group_centroid
        out_groups.append(Group(gid, cs[0].group_kind, ims, "", cen[0], cen[1]))
        for c in cs:
            reasons[c.meta.path] = f"group {gid} ({c.group_kind})"

    out_groups.sort(key=_t_start_key)
    order = [im.path for g in out_groups for im in g.images]
    log.info("selection 確定: %d 枚 / %d グループ", len(order), len(out_groups))
    return Selection(groups=out_groups, order=order, reasons=reasons)


def select(groups: list[Group], total: int, cfg: Config) -> Selection:
    """アルゴリズムだけで total 枚を選ぶ(候補=最終)。"""
    pool = shortlist(groups, total, cfg, pool_factor=1.0)
    return finalize(pool, [c.idx for c in pool.candidates], cfg)
