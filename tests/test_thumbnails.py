from pathlib import Path
import sqlite3

from PIL import Image

from comic_archive.importer.commit import commit_staged_import
from comic_archive.importer.review import build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import
from comic_archive.library import read_library
from comic_archive.thumbnails import build_missing_thumbnails, thumbnail_path_for_media


def test_commit_generates_thumbnail_and_backfill_is_idempotent(tmp_path: Path):
    source = tmp_path / "source" / "Standalone"
    source.mkdir(parents=True)
    Image.new("RGB", (1200, 1800), "white").save(source / "001.png")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Artist",
        series="Comic",
        issue_metadata={".": {}},
    )
    db = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    commit_staged_import(staged, library_root=library, database_path=db)

    media = read_library(db)[0].series[0].issues[0].groups[0].media[0]
    thumb = thumbnail_path_for_media(library, media.id, media.stored_path)
    assert thumb.is_file()

    first = build_missing_thumbnails(db, library)
    assert first.created == 0
    assert first.existing == 1

    thumb.unlink()
    second = build_missing_thumbnails(db, library)
    assert second.created == 1
    assert thumb.is_file()


def test_jpeg_thumbnail_uses_decoder_downsampling(tmp_path: Path, monkeypatch):
    """Large JPEG pages should be downsampled by the decoder before resize."""
    from PIL import JpegImagePlugin
    from comic_archive.thumbnails import create_thumbnail

    source = tmp_path / "page.jpg"
    destination = tmp_path / "thumb.jpg"
    Image.new("RGB", (2400, 3600), "white").save(source, "JPEG", quality=88)

    calls: list[tuple[str | None, tuple[int, int]]] = []
    original = JpegImagePlugin.JpegImageFile.draft

    def tracking_draft(self, mode, size):
        calls.append((mode, size))
        return original(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", tracking_draft)

    assert create_thumbnail(source, destination, mime_type="image/jpeg") is True
    assert calls
    assert calls[0] == ("RGB", (320, 480))
    with Image.open(destination) as thumb:
        assert thumb.width <= 320
        assert thumb.height <= 480
