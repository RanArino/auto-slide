"""グルーピング: まず撮影時刻のギャップで分割し、各区間を GPS の近接でさらに分ける。

GPS が無い写真は「時間区間」だけでまとまる(場所は撮影時刻の近さで代理)。
"""

from __future__ import annotations

import logging
import math

from .config import Config
from .models import Group, ImageMeta

log = logging.getLogger("autoslide.grouping")


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6_371_000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _sort_key(im: ImageMeta):
    # 撮影時刻優先。無ければファイル名で安定ソート。
    return (0, im.taken_at.timestamp()) if im.taken_at else (1, im.path)


def _split_by_time(images: list[ImageMeta], gap_minutes: float) -> list[list[ImageMeta]]:
    if not images:
        return []
    ordered = sorted(images, key=_sort_key)
    segments: list[list[ImageMeta]] = [[ordered[0]]]
    gap = gap_minutes * 60
    for prev, cur in zip(ordered, ordered[1:]):
        if prev.taken_at and cur.taken_at and (cur.taken_at - prev.taken_at).total_seconds() > gap:
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


def _split_by_geo(images: list[ImageMeta], radius_m: float) -> list[list[ImageMeta]]:
    """貪欲クラスタリング: 既存クラスタ重心から radius 以内なら合流、無ければ新規。"""
    with_gps = [im for im in images if im.has_gps]
    without = [im for im in images if not im.has_gps]
    if not with_gps:
        return [images] if images else []

    clusters: list[list[ImageMeta]] = []
    centroids: list[tuple[float, float]] = []
    for im in with_gps:
        pt = (im.lat, im.lon)
        best_i, best_d = -1, radius_m
        for i, c in enumerate(centroids):
            d = _haversine_m(pt, c)
            if d <= best_d:
                best_i, best_d = i, d
        if best_i < 0:
            clusters.append([im])
            centroids.append(pt)
        else:
            clusters[best_i].append(im)
            k = len(clusters[best_i])
            cl = centroids[best_i]
            centroids[best_i] = (cl[0] + (pt[0] - cl[0]) / k, cl[1] + (pt[1] - cl[1]) / k)

    # GPS 無し写真は時間的に最も近いクラスタへ寄せる
    for im in without:
        target = _nearest_in_time(im, clusters)
        target.append(im)
    return clusters


def _nearest_in_time(im: ImageMeta, clusters: list[list[ImageMeta]]) -> list[ImageMeta]:
    if im.taken_at is None:
        return max(clusters, key=len)
    best, best_dt = clusters[0], float("inf")
    for cl in clusters:
        for other in cl:
            if other.taken_at:
                dt = abs((other.taken_at - im.taken_at).total_seconds())
                if dt < best_dt:
                    best, best_dt = cl, dt
    return best


def group_images(images: list[ImageMeta], cfg: Config) -> list[Group]:
    groups: list[Group] = []
    gid = 0
    for segment in _split_by_time(images, cfg.time_gap_minutes):
        geo_clusters = _split_by_geo(segment, cfg.geo_radius_meters)
        multi_geo = len(geo_clusters) > 1
        for cl in geo_clusters:
            cl_sorted = sorted(cl, key=_sort_key)
            gps_pts = [(im.lat, im.lon) for im in cl_sorted if im.has_gps]
            clat = sum(p[0] for p in gps_pts) / len(gps_pts) if gps_pts else None
            clon = sum(p[1] for p in gps_pts) / len(gps_pts) if gps_pts else None
            kind = "time+geo" if multi_geo and gps_pts else "time"
            groups.append(
                Group(
                    group_id=gid,
                    kind=kind,
                    images=cl_sorted,
                    centroid_lat=clat,
                    centroid_lon=clon,
                )
            )
            gid += 1

    groups.sort(key=lambda g: (g.t_start.timestamp() if g.t_start else float("inf"), g.group_id))
    for i, g in enumerate(groups):
        g.group_id = i
    log.info("grouping 完了: %d グループ", len(groups))
    return groups
