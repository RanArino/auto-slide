"""フォルダ走査・EXIF 抽出・サムネ生成。結果は Cache に保存する。"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image

from .db import Cache
from .features import compute_features
from .models import ImageMeta

log = logging.getLogger("autoslide.scan")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}
THUMB_LONG_EDGE = 512

try:  # HEIC は任意
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

_DT_TAGS = {
    ExifTags.Base.DateTimeOriginal.value: 1,
    ExifTags.Base.DateTimeDigitized.value: 2,
    ExifTags.Base.DateTime.value: 3,
}


def _parse_exif_datetime(value: str) -> datetime | None:
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def _gps_to_decimal(coord, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in coord)
        dec = d + m / 60 + s / 3600
        if str(ref).upper() in ("S", "W"):
            dec = -dec
        return dec
    except Exception:
        return None


def _read_exif(img: Image.Image) -> tuple[datetime | None, float | None, float | None, str | None]:
    taken_at = lat = lon = camera = None
    try:
        exif = img.getexif()
    except Exception:
        return None, None, None, None

    # 撮影日時: 優先度の高いタグから
    best_priority = 99
    for tag, priority in _DT_TAGS.items():
        raw = exif.get(tag)
        if raw and priority < best_priority:
            dt = _parse_exif_datetime(str(raw))
            if dt:
                taken_at, best_priority = dt, priority
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        for tag, priority in _DT_TAGS.items():
            raw = sub.get(tag)
            if raw and priority < best_priority:
                dt = _parse_exif_datetime(str(raw))
                if dt:
                    taken_at, best_priority = dt, priority
    except Exception:
        pass

    camera = exif.get(ExifTags.Base.Model.value)
    camera = str(camera).strip() if camera else None

    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if gps:
            lat = _gps_to_decimal(gps.get(2), gps.get(1))
            lon = _gps_to_decimal(gps.get(4), gps.get(3))
    except Exception:
        pass
    return taken_at, lat, lon, camera


def _thumb_path(cache_dir: Path, src: Path) -> Path:
    h = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:16]
    return cache_dir / "thumbs" / f"{h}.jpg"


def _make_thumb(img: Image.Image, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    t = img.convert("RGB")
    t.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE), Image.LANCZOS)
    t.save(dst, "JPEG", quality=88)


def iter_image_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and ".autoslide_cache" not in p.parts:
            yield p


def scan(root: str | Path, cache: Cache, cache_dir: Path) -> list[ImageMeta]:
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"パスが存在しません: {root}")

    metas: list[ImageMeta] = []
    n_cached = n_new = 0
    for src in iter_image_files(root):
        mtime = src.stat().st_mtime
        cached = cache.get(str(src), mtime)
        if cached:
            n_cached += 1
            metas.append(_meta_from_row(cached))
            continue

        try:
            with Image.open(src) as img:
                img.load()
                w, h = img.size
                taken_at, lat, lon, camera = _read_exif(img)
                thumb = _thumb_path(cache_dir, src)
                _make_thumb(img, thumb)
        except Exception as e:  # 壊れたファイルはスキップ
            log.warning("読み込み失敗 %s: %s", src, e)
            continue

        phash, sharpness, embedding = compute_features(thumb)
        meta = ImageMeta(
            path=str(src),
            folder=str(src.parent),
            taken_at=taken_at,
            lat=lat,
            lon=lon,
            camera=camera,
            width=w,
            height=h,
            thumb_path=str(thumb),
            phash=phash,
            sharpness=sharpness,
            embedding=embedding,
        )
        cache.upsert(
            dict(
                path=meta.path,
                mtime=mtime,
                folder=meta.folder,
                taken_at=taken_at.isoformat() if taken_at else None,
                lat=lat,
                lon=lon,
                camera=camera,
                width=w,
                height=h,
                thumb_path=meta.thumb_path,
                phash=phash,
                sharpness=sharpness,
                embedding=embedding,
            )
        )
        metas.append(meta)
        n_new += 1

    cache.commit()
    log.info("scan 完了: %d 枚 (新規 %d / キャッシュ %d)", len(metas), n_new, n_cached)
    return metas


def _meta_from_row(row: dict) -> ImageMeta:
    taken_at = None
    if row.get("taken_at"):
        try:
            taken_at = datetime.fromisoformat(row["taken_at"])
        except ValueError:
            taken_at = None
    return ImageMeta(
        path=row["path"],
        folder=row.get("folder") or str(Path(row["path"]).parent),
        taken_at=taken_at,
        lat=row.get("lat"),
        lon=row.get("lon"),
        camera=row.get("camera"),
        width=row.get("width") or 0,
        height=row.get("height") or 0,
        thumb_path=row.get("thumb_path"),
        phash=row.get("phash"),
        sharpness=row.get("sharpness") or 0.0,
        embedding=row.get("embedding"),
    )
