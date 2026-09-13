from __future__ import annotations

import sqlite3
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


THUMBNAIL_MAX_SIZE = (320, 480)
THUMBNAIL_QUALITY = 78
THUMBNAIL_MIME = "image/jpeg"
logger = logging.getLogger(__name__)


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
    temporary: Path | None = None
    try:
        with Image.open(source) as image:
            # For animated images, Pillow starts on frame 0. We intentionally
            # use only the first frame for the grid thumbnail.
            animated = bool(getattr(image, "is_animated", False))
            if animated:
                image.seek(0)

            # JPEG decoders can downsample while decoding. Comic pages are often
            # multi-megapixel JPEGs, while our derivative is only 320x480.
            # Asking Pillow for a decoder-level draft avoids materializing the
            # full-resolution pixel buffer only to immediately shrink it.
            # Other formats retain the existing decode path.
            if not animated and image.format == "JPEG":
                image.draft("RGB", THUMBNAIL_MAX_SIZE)

            # Mutating in place avoids an additional full image copy before the
            # final resize, which is especially helpful for large source pages.
            ImageOps.exif_transpose(image, in_place=True)
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
            # Publish only a complete derivative. A unique sibling file keeps
            # replacement atomic and concurrent requests from sharing a temp.
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=f".{destination.name}.",
                suffix=".tmp", delete=False,
            ) as target:
                temporary = Path(target.name)
                output.save(
                    target,
                    format="JPEG",
                    quality=THUMBNAIL_QUALITY,
                    optimize=True,
                    progressive=True,
                )
            temporary.replace(destination)
        return True
    except Exception:
        # Derivative failures must not abort imports or a rebuild batch.
        # Process-control exceptions still propagate through the finally block.
        logger.warning("Thumbnail generation failed source=%s destination=%s", source, destination, exc_info=True)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Thumbnail temporary file cleanup failed path=%s", temporary, exc_info=True)


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
