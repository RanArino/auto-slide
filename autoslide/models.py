"""パイプライン内で受け渡すデータ構造。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np


@dataclass
class ImageMeta:
    path: str
    folder: str
    taken_at: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    camera: str | None = None
    width: int = 0
    height: int = 0
    thumb_path: str | None = None
    phash: int | None = None
    sharpness: float = 0.0
    embedding: np.ndarray | None = None
    mean_r: float | None = None
    mean_g: float | None = None
    mean_b: float | None = None
    mean_sat: float | None = None
    composition: float | None = None

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class Group:
    group_id: int
    kind: str                       # "time+geo" / "time" など
    images: list[ImageMeta]
    label: str = ""
    centroid_lat: float | None = None
    centroid_lon: float | None = None

    @property
    def t_start(self) -> datetime | None:
        ts = [im.taken_at for im in self.images if im.taken_at]
        return min(ts) if ts else None

    @property
    def t_end(self) -> datetime | None:
        ts = [im.taken_at for im in self.images if im.taken_at]
        return max(ts) if ts else None


@dataclass
class Candidate:
    """最終選択の候補。番号(idx)はコンタクトシートと candidates.json で一致する。"""

    idx: int
    meta: ImageMeta
    group_id: int
    group_kind: str
    group_centroid: tuple[float | None, float | None] = (None, None)
    group_label: str = ""


@dataclass
class CandidatePool:
    source: str
    count: int                               # 最終的に選ぶ枚数
    candidates: list[Candidate]              # group 順 → 時刻順、idx は 1 始まり連番
    group_sizes: dict[int, int] = field(default_factory=dict)
    group_kinds: dict[int, str] = field(default_factory=dict)
    group_labels: dict[int, str] = field(default_factory=dict)
    group_centroids: dict[int, tuple[float | None, float | None]] = field(default_factory=dict)


@dataclass
class Selection:
    groups: list[Group]                      # 選択後・順序確定済み
    order: list[str] = field(default_factory=list)   # 画像パスの再生順
    reasons: dict[str, str] = field(default_factory=dict)


@dataclass
class Proposal:
    title: str
    mood: str
    music_style: str = ""
    overall_tone: str = ""
    color_note: str = ""
    group_labels: dict[int, str] = field(default_factory=dict)
    divider_texts: dict[int, str] = field(default_factory=dict)
    captions: dict[str, str] = field(default_factory=dict)
    caption_subs: dict[str, str] = field(default_factory=dict)   # 写真パス -> 日付など副題
    hashtags: list[str] = field(default_factory=list)
    raw: dict | None = None


@dataclass
class Segment:
    """タイムライン上の 1 区間。焼き込み PNG と SRT はどちらもここから作る。"""

    kind: str                       # "title" / "divider" / "photo"
    start: float
    end: float
    text: str = ""                  # title/divider の表示文字、photo のキャプション主文
    subtext: str = ""               # photo キャプションの副題(日付など。SRT には入れない)
    image_path: str | None = None
    group_id: int | None = None
    index: int = 0                  # photo の 1 始まり通し番号

    @property
    def duration(self) -> float:
        return self.end - self.start
