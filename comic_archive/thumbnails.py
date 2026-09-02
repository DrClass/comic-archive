from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


THUMBNAIL_MAX_SIZE = (320, 480)
THUMBNAIL_QUALITY = 78
THUMBNAIL_MIME = "image/jpeg"


@dataclass(slots=True)
class ThumbnailBuildResult:
    created: int = 0
    existing: int = 0
    skipped: int = 0
    failed: int = 0


def thumbnail_path_for_media(library_root: str | Path, media_id: str, stored_path: str) -> Path:
    library = Path(library_root).expanduser().resolve()
    media_path = library / stored_path
    return media_path.parent / "_thumbs" / f"{media_id}.jpg"


def create_thumbnail(
    source_path: str | Path,
    destination: str | Path,
    *,
    mime_type: str,
) -> bool:
    """Create a small static JPEG derivative. Originals are never modified."""
    if not mime_type.startswith("image/"):
        return False

    source = Path(source_path)
    destination = Path(destination)
    try:
        with Image.open(source) as image:
            # For animated images, Pillow starts on frame 0. We intentionally
            # use only the first frame for the grid thumbnail.
            if getattr(image, "is_animated", False):
                image.seek(0)
            image = ImageOps.exif_transpose(image)
            image.thumbnail(THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)

            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                output = background
            else:
                output = image.convert("RGB")

            destination.parent.mkdir(parents=True, exist_ok=True)
            output.save(
                destination,
                format="JPEG",
                quality=THUMBNAIL_QUALITY,
                optimize=True,
                progressive=True,
            )
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def ensure_thumbnail(
    library_root: str | Path,
    *,
    media_id: str,
    stored_path: str,
    mime_type: str,
) -> Path | None:
    if not mime_type.startswith("image/"):
        return None
    library = Path(library_root).expanduser().resolve()
    source = library / stored_path
    if not source.is_file():
        return None
    destination = thumbnail_path_for_media(library, media_id, stored_path)
    if destination.is_file():
        return destination
    return destination if create_thumbnail(source, destination, mime_type=mime_type) else None


def build_missing_thumbnails(
    database_path: str | Path,
    library_root: str | Path,
) -> ThumbnailBuildResult:
    database = Path(database_path).expanduser().resolve()
    library = Path(library_root).expanduser().resolve()
    result = ThumbnailBuildResult()

    with sqlite3.connect(database) as db:
        rows = db.execute(
            """SELECT id, stored_path, mime_type
               FROM media
               WHERE active = 1
               ORDER BY id"""
        ).fetchall()

    for media_id, stored_path, mime_type in rows:
        if not str(mime_type).startswith("image/"):
            result.skipped += 1
            continue
        destination = thumbnail_path_for_media(library, media_id, stored_path)
        if destination.is_file():
            result.existing += 1
            continue
        source = library / stored_path
        if not source.is_file():
            result.failed += 1
            continue
        if create_thumbnail(source, destination, mime_type=mime_type):
            result.created += 1
        else:
            result.failed += 1

    return result
