"""Bounded scheduling contracts plus real worker output/failure regressions."""
from concurrent.futures import Future
from io import BytesIO
import os
from pathlib import Path
import threading

import fitz
from PIL import Image
import pytest

from comic_archive.importer import scanner


def make_pdf(tmp_path, kinds):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "comic.pdf"
    image = BytesIO()
    Image.new("RGB", (120, 160), (20, 60, 100)).save(image, format="JPEG", quality=91)
    with fitz.open() as document:
        for index, kind in enumerate(kinds):
            page = document.new_page(width=120, height=160)
            if kind == "direct":
                page.insert_image(page.rect, stream=image.getvalue())
            else:
                page.insert_text((10, 30), f"Page {index + 1}")
        document.save(path)
    return path, image.getvalue()


class ControlledPool:
    """Finish the second job first, without relying on timing or native threads."""
    def __init__(self, *, max_workers, mp_context):
        assert max_workers == 2
        assert mp_context.get_start_method() == "spawn"
        self.jobs = []
        self.outstanding = []
        self.peak = 0
        self.closed = False

    def submit(self, function, *args):
        pool = self

        class Job(Future):
            def result(self, timeout=None):
                for job in reversed(pool.outstanding):
                    if not job.done():
                        try:
                            job.set_result(job.function(*job.args))
                        except Exception as exc:
                            job.set_exception(exc)
                pool.outstanding.remove(self)
                return super().result(timeout)

        job = Job()
        job.function, job.args = function, args
        self.jobs.append(args[1])
        self.outstanding.append(job)
        self.peak = max(self.peak, len(self.outstanding))
        assert self.peak <= 2
        return job

    def shutdown(self, *, wait, cancel_futures):
        assert wait and cancel_futures
        self.closed = True
        for job in self.outstanding:
            job.cancel()


def install_pool(monkeypatch):
    pools = []

    def create(**kwargs):
        pool = ControlledPool(**kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr(scanner, "ProcessPoolExecutor", create)
    return pools


def test_pdf_mixed_pages_schedule_only_fallback_and_preserve_order(tmp_path, monkeypatch, caplog):
    path, original_jpeg = make_pdf(tmp_path, ["render", "render", "direct", "render", "direct"])
    original_pdf = path.read_bytes()
    original_stat = path.stat()
    pools = install_pool(monkeypatch)
    caller = threading.get_ident()
    direct = scanner._direct_pdf_image
    checks = []

    def extract(page, document):
        assert threading.get_ident() == caller
        checks.append(page.number)
        return direct(page, document)

    monkeypatch.setattr(scanner, "_direct_pdf_image", extract)
    events = []
    caplog.set_level("INFO", logger="comic_archive.scanner")

    def progress(phase, current, total, message):
        assert threading.get_ident() == caller
        if phase == "rendering_pdf":
            events.append(current)

    result = scanner.scan_folder(path.parent, pdf_cache_root=tmp_path / "cache", progress=progress)
    assert checks == list(range(5))
    assert len(pools) == 1 and pools[0].jobs == [0, 1, 3]
    assert pools[0].peak == 2 and pools[0].closed
    assert events == list(range(6))
    media = result.primary.media
    assert [item.path.name for item in media] == [f"{i:04}.jpg" for i in range(1, 6)]
    with fitz.open(path) as document:
        for index, item in enumerate(media):
            if index in {2, 4}:
                assert item.path.read_bytes() == original_jpeg
            else:
                expected = tmp_path / f"expected-{index}.jpg"
                document[index].get_pixmap(dpi=150, alpha=False).save(expected, output="jpg", jpg_quality=98)
                assert item.path.read_bytes() == expected.read_bytes()
    assert path.read_bytes() == original_pdf
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    summary = next(r.message for r in caplog.records if "pdf render complete" in r.message)
    assert "extracted=2 rendered=3 cached=0" in summary
    assert f"output_bytes={sum(item.path.stat().st_size for item in media)}" in summary


@pytest.mark.parametrize("kind", ["direct", "render"])
def test_pdf_cache_and_direct_only_never_start_workers(tmp_path, monkeypatch, kind):
    path, _ = make_pdf(tmp_path, [kind, kind])
    pools = install_pool(monkeypatch)
    first = scanner.scan_folder(path.parent, pdf_cache_root=tmp_path / "cache")
    assert len(pools) == (kind == "render")
    before = [(item.path.read_bytes(), item.path.stat().st_mtime_ns) for item in first.primary.media]

    def forbidden(*args, **kwargs):
        pytest.fail("Cached pages must not extract or start a pool")

    monkeypatch.setattr(scanner, "ProcessPoolExecutor", forbidden)
    monkeypatch.setattr(scanner, "_direct_pdf_image", forbidden)
    second = scanner.scan_folder(path.parent, pdf_cache_root=tmp_path / "cache")
    assert [(item.path.read_bytes(), item.path.stat().st_mtime_ns) for item in second.primary.media] == before


def test_pdf_worker_failure_waits_for_shutdown_and_retry_reuses_completed_pages(tmp_path, monkeypatch):
    path, _ = make_pdf(tmp_path, ["render"] * 3)
    pools = install_pool(monkeypatch)
    render = scanner._render_pdf_page

    def fail(pdf, index, destination):
        if index == 0:
            raise OSError("injected render failure")
        return render(pdf, index, destination)

    monkeypatch.setattr(scanner, "_render_pdf_page", fail)
    with pytest.raises(scanner.FolderScanError, match="injected render failure"):
        scanner.scan_folder(path.parent, pdf_cache_root=tmp_path / "cache")
    assert pools[0].closed and pools[0].jobs == [0, 1]
    cached = next((tmp_path / "cache").rglob("0002.jpg"))
    stamp = cached.stat().st_mtime_ns
    monkeypatch.setattr(scanner, "_render_pdf_page", render)
    result = scanner.scan_folder(path.parent, pdf_cache_root=tmp_path / "cache")
    assert len(result.primary.media) == 3
    assert pools[1].jobs == [0, 2] and pools[1].closed
    assert cached.stat().st_mtime_ns == stamp


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_pdf_worker_atomic_save_cleans_partial_files(tmp_path, monkeypatch, error):
    path, _ = make_pdf(tmp_path, ["render"])
    destination = tmp_path / "page.jpg"
    destination.write_bytes(b"existing derivative")

    def fail(self, filename, **kwargs):
        Path(filename).write_bytes(b"partial JPEG")
        raise error("injected save failure")

    monkeypatch.setattr(fitz.Pixmap, "save", fail)
    with pytest.raises(error, match="injected save failure"):
        scanner._render_pdf_page(path, 0, destination)
    assert destination.read_bytes() == b"existing derivative"
    assert not list(tmp_path.glob("*.part"))


def test_pdf_progress_error_shuts_down_workers(tmp_path, monkeypatch):
    path, _ = make_pdf(tmp_path, ["render"] * 4)
    pools = install_pool(monkeypatch)

    def progress(phase, current, *_):
        if phase == "rendering_pdf" and current == 1:
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        scanner.scan_folder(path.parent, pdf_cache_root=tmp_path / "cache", progress=progress)
    assert pools[0].closed and pools[0].jobs == [0, 1]


def test_pdf_pool_startup_error_is_reported_without_cache_output(tmp_path, monkeypatch):
    path, _ = make_pdf(tmp_path, ["render"])

    def fail(**kwargs):
        raise OSError("worker startup denied")

    monkeypatch.setattr(scanner, "ProcessPoolExecutor", fail)
    with pytest.raises(scanner.FolderScanError, match="Could not render PDF comic.pdf: worker startup denied"):
        scanner.scan_folder(path.parent, pdf_cache_root=tmp_path / "cache")
    assert not list((tmp_path / "cache").rglob("*.jpg"))


def test_non_pdf_scan_never_starts_workers_or_converts_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    originals = {"01.jpg": b"jpeg", "02.png": b"png", "03.gif": b"gif", "04.mp4": b"video"}
    for name, data in originals.items():
        (source / name).write_bytes(data)

    def forbidden(**kwargs):
        pytest.fail("Non-PDF media must not start workers")

    monkeypatch.setattr(scanner, "ProcessPoolExecutor", forbidden)
    result = scanner.scan_folder(source, pdf_cache_root=tmp_path / "cache")
    assert {item.path.name: item.path.read_bytes() for item in result.primary.media} == originals
    assert all(item.path.parent == source for item in result.primary.media)


def test_pdf_spawned_worker_runs_outside_parent_and_matches_sequential_output(tmp_path):
    path, _ = make_pdf(tmp_path, ["render"])
    destination = tmp_path / "spawned.jpg"
    with scanner.ProcessPoolExecutor(max_workers=2, mp_context=scanner.multiprocessing.get_context("spawn")) as pool:
        assert pool.submit(os.getpid).result(timeout=30) != os.getpid()
        result = pool.submit(scanner._render_pdf_page, path, 0, destination).result(timeout=30)
    expected = tmp_path / "expected.jpg"
    with fitz.open(path) as document:
        document[0].get_pixmap(dpi=150, alpha=False).save(expected, output="jpg", jpg_quality=98)
    assert destination.read_bytes() == expected.read_bytes()
    assert result.mode == "rendered" and result.raster_seconds > 0 and result.save_seconds > 0
