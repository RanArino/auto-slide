"""設定の読み込み。config.toml + 既定値をマージして Config を返す。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

ASPECTS = {
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "9:16": (1080, 1920),
}


@dataclass
class Config:
    seconds_per_slide: float = 4.0
    transition_seconds: float = 0.75
    title_card_seconds: float = 2.5
    aspect: str = "16:9"
    fps: int = 30

    time_gap_minutes: float = 30.0
    geo_radius_meters: float = 150.0
    phash_hamming_max: int = 6

    ken_burns: bool = False
    burn_captions: bool = False

    audio_fade_in: float = 1.0
    audio_fade_out: float = 2.0
    audio_lufs: float = -14.0

    llm_model: str = "claude-sonnet-5"
    seed: int = 42

    @property
    def resolution(self) -> tuple[int, int]:
        if self.aspect not in ASPECTS:
            raise ValueError(f"aspect は {list(ASPECTS)} のいずれか: {self.aspect!r}")
        return ASPECTS[self.aspect]

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        data: dict = {}
        if path:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"config が見つかりません: {p}")
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"config に未知のキー: {sorted(unknown)}")
        return cls(**data)
