from pathlib import Path
import sqlite3
import pytest

from PIL import Image

from comic_archive.importer.commit import commit_staged_import
from comic_archive.importer.review import build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import
from comic_archive.library import read_library
from comic_archive.thumbnails import build_missing_thumbnails, thumbnail_path_for_media
from comic_archive.thumbnails import create_thumbnail, ensure_thumbnail


def test_thumbnail_exception_does_not_abort_commit_and_rebuild_can_retry(tmp_path, monkeypatch, caplog):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("001.png", "002.png"):
        Image.new("RGB", (40, 60), "white").save(source / name)
    originals = {p.name: p.read_bytes() for p in source.iterdir()}
    staged = build_staged_import(build_review_plan(scan_folder(source)), author="Artist", series="Comic")
    database, library = tmp_path / "db.sqlite3", tmp_path / "library"
    original_save = Image.Image.save

    def fail_save(*args, **kwargs):
        raise RuntimeError("injected thumbnail failure")

    with monkeypatch.context() as patch:
        patch.setattr(Image.Image, "save", fail_save)
        result = commit_staged_import(staged, library_root=library, database_path=database)
    assert result.copied_files == 2
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT staging_id FROM imports").fetchall() == [(staged.staging_id,)]
        rows = db.execute("SELECT id, stored_path, source_path, mime_type FROM media ORDER BY id").fetchall()
    assert len(rows) == 2
    for _, stored, original, _ in rows:
        assert (library / stored).read_bytes() == originals[Path(original).name]
    assert {p.name: p.read_bytes() for p in source.iterdir()} == originals
    assert "injected thumbnail failure" in caplog.text
    assert str(library) in caplog.text

    calls = 0
    def fail_first_save(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first rebuild failure")
        return original_save(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Image.Image, "save", fail_first_save)
        rebuilt = build_missing_thumbnails(database, library)
    assert (rebuilt.failed, rebuilt.created) == (1, 1)
    with monkeypatch.context() as patch:
        patch.setattr(Image.Image, "save", fail_save)
        media_id, stored, _, mime = rows[0]
        assert ensure_thumbnail(library, media_id=media_id, stored_path=stored, mime_type=mime) is None
    retried = build_missing_thumbnails(database, library)
    assert (retried.failed, retried.created, retried.existing) == (0, 1, 1)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError, OSError, KeyboardInterrupt, SystemExit])
def test_failed_thumbnail_write_preserves_destination_and_cleans_temp(tmp_path, monkeypatch, existing, failure):
    source, destination = tmp_path / "page.png", tmp_path / "thumb.jpg"
    Image.new("RGB", (40, 60), "white").save(source)
    source_before = source.read_bytes()
    if existing:
        Image.new("RGB", (10, 10), "red").save(destination)
    previous = destination.read_bytes() if existing else None

    def partial_save(self, target, **kwargs):
        if hasattr(target, "write"):
            target.write(b"partial jpeg")
        else:
            Path(target).write_bytes(b"partial jpeg")
        raise failure("injected write failure")

    monkeypatch.setattr(Image.Image, "save", partial_save)
    if issubclass(failure, Exception):
        assert create_thumbnail(source, destination, mime_type="image/png") is False
    else:
        with pytest.raises(failure):
            create_thumbnail(source, destination, mime_type="image/png")
    assert (destination.read_bytes() if destination.exists() else None) == previous
    assert source.read_bytes() == source_before
    assert set(tmp_path.iterdir()) == ({source, destination} if existing else {source})


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
