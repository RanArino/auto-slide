"""感情ラベルから BGM を選ぶ。

assets/music_index.json (下記スキーマ) を読み、mood 一致 → energy 近さ → 長さで候補を絞る。
音源が無ければ None(=無音でレンダリング)。--music で明示指定も可能。

music_index.json の 1 要素:
  {"file": "calm/quiet_morning.mp3", "mood": "calm", "energy": 0.3,
   "bpm": 70, "duration": 128.0, "license": "CC-BY 4.0 / Artist"}
※ license が空の音源は投稿用途では使わないこと。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("autoslide.music")

# LLM が返す mood 語 → 目安 energy
_MOOD_ENERGY = {
    "calm": 0.25, "nostalgic": 0.4, "melancholic": 0.35,
    "joyful": 0.7, "upbeat": 0.8, "dramatic": 0.75,
}
_SYNONYMS = {
    "peaceful": "calm", "relaxed": "calm", "quiet": "calm",
    "sentimental": "nostalgic", "warm": "nostalgic",
    "sad": "melancholic", "somber": "melancholic",
    "happy": "joyful", "cheerful": "joyful", "bright": "joyful",
    "energetic": "upbeat", "lively": "upbeat",
    "epic": "dramatic", "intense": "dramatic",
}


@dataclass
class MusicChoice:
    path: Path
    duration: float | None
    license: str
    mood: str


def _norm_mood(mood: str) -> str:
    m = mood.split("/")[0].strip().lower()
    return _SYNONYMS.get(m, m)


def choose_music(mood: str, target_seconds: float, assets_dir: Path,
                 override: str | Path | None = None) -> MusicChoice | None:
    if override:
        p = Path(override)
        if not p.exists():
            raise FileNotFoundError(f"--music が見つかりません: {p}")
        return MusicChoice(p, None, "(user supplied)", _norm_mood(mood))

    index = assets_dir / "music_index.json"
    if not index.exists():
        log.warning("music_index.json 無し。無音でレンダリングします")
        return None
    try:
        entries = json.loads(index.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("music_index.json 読み込み失敗: %s", e)
        return None
    if not entries:
        log.warning("音源が登録されていません。無音でレンダリングします")
        return None

    want = _norm_mood(mood)
    want_energy = _MOOD_ENERGY.get(want, 0.4)

    def score(e: dict) -> tuple:
        same_mood = 0 if _norm_mood(e.get("mood", "")) == want else 1
        energy_gap = abs(float(e.get("energy", 0.5)) - want_energy)
        dur = float(e.get("duration", 0) or 0)
        too_short = 1 if dur and dur + 1 < target_seconds else 0
        return (same_mood, too_short, energy_gap, -dur)

    best = min(entries, key=score)
    path = assets_dir / "music" / best["file"]
    if not path.exists():
        log.warning("音源ファイルが実在しません: %s。無音にします", path)
        return None
    lic = str(best.get("license", "")).strip()
    if not lic:
        log.warning("ライセンス未記載の音源のため使用しません: %s", path)
        return None
    log.info("BGM: %s (mood=%s, license=%s)", path.name, best.get("mood"), lic)
    return MusicChoice(path, float(best["duration"]) if best.get("duration") else None, lic, want)
