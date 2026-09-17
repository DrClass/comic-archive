from __future__ import annotations

import mimetypes
import re
import tempfile
import hashlib
import logging
import os
import time
import multiprocessing
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Callable
from pathlib import Path

from .models import (
    MediaKind,
    ScannedGroup,
    ScannedImport,
    ScannedIssue,
    ScannedMedia,
    ScannedSeries,
    SuggestedRole,
)
from .sorting import natural_path_key

SUPPORTED_MEDIA: dict[str, tuple[MediaKind, str]] = {
    ".jpg": (MediaKind.IMAGE, "image/jpeg"),
    ".jpeg": (MediaKind.IMAGE, "image/jpeg"),
    ".png": (MediaKind.IMAGE, "image/png"),
    ".webp": (MediaKind.IMAGE, "image/webp"),
    ".gif": (MediaKind.IMAGE, "image/gif"),
    ".mp4": (MediaKind.VIDEO, "video/mp4"),
    # PDF is an accepted importer input. Safe single-image pages preserve embedded
    # JPEG/PNG data; other pages render to high-quality JPEG. PDFs are never committed.
    ".pdf": (MediaKind.IMAGE, "application/pdf"),
}

IGNORED_FILENAMES = {"thumbs.db", ".ds_store", "desktop.ini"}

PDF_RENDER_DPI = 150
PDF_RENDER_WORKERS = 2
PDF_JPEG_QUALITY = 98
PDF_CACHE_VERSION = 2
PDF_DIRECT_IMAGE_TYPES: dict[str, tuple[str, str]] = {
    "jpeg": (".jpg", "image/jpeg"),
    "jpg": (".jpg", "image/jpeg"),
    "png": (".png", "image/png"),
}

# These are suggestions only. The future import-review UI will always allow
# the user to override them.
_EXTRA_WORDS = {
    "extra",
    "extras",
    "bonus",
    "bonuses",
    "textless",
    "render",
    "renders",
    "animation",
    "animations",
    "cover",
    "covers",
}
_TOKENIZE = re.compile(r"[^a-z0-9]+")
_PRIMARY_CONTAINER_WORDS = {"page", "pages", "image", "images", "comic", "main", "primary"}


class FolderScanError(ValueError):
    pass


logger = logging.getLogger("comic_archive.scanner")
ScanProgress = Callable[[str, int, int, str], None]


@dataclass
class ScanIndex:
    source: Path
    direct_supported: dict[Path, list[Path]]
    child_dirs: dict[Path, list[Path]]
    media_dirs: set[Path]
    ignored: list[Path]
    media_metadata: dict[Path, tuple[MediaKind, str, int]]
    file_count: int
    supported_count: int
    directory_count: int

    # Paths placed in the index are already absolute/canonical because ``source``
    # is resolved once before scanning. Avoid Path.resolve() here: on a large
    # archive it turns an in-memory lookup into repeated filesystem work.
    def has_media(self, directory: Path) -> bool:
        return directory in self.media_dirs

    def files(self, directory: Path) -> list[Path]:
        return self.direct_supported.get(directory, [])

    def dirs(self, directory: Path) -> list[Path]:
        return self.child_dirs.get(directory, [])

    def metadata(self, path: Path) -> tuple[MediaKind, str, int] | None:
        return self.media_metadata.get(path)


def _build_scan_index(source: Path, progress: ScanProgress | None = None) -> ScanIndex:
    """Build the complete filesystem/media index in one iterative scandir pass.

    Earlier versions used os.walk plus Path.resolve() for every directory and
    propagated ``media_dirs`` by walking every supported file back through all
    ancestors. The latter is O(media * depth). This implementation records
    direct media once, then propagates media-bearing state bottom-up once per
    directory.
    """
    started = time.monotonic()
    direct_supported: dict[Path, list[Path]] = {}
    child_dirs: dict[Path, list[Path]] = {}
    media_metadata: dict[Path, tuple[MediaKind, str, int]] = {}
    ignored: list[Path] = []
    directory_order: list[Path] = []
    file_count = 0
    supported_count = 0

    if progress:
        progress("indexing", 0, 0, "Indexing filesystem")

    stack = [source]
    while stack:
        root = stack.pop()
        directory_order.append(root)
        dirs: list[Path] = []
        supported_here: list[Path] = []
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    path = root / entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append(path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        # Match the scanner's previous conservative behavior: an
                        # inaccessible filesystem entry cannot become media.
                        ignored.append(path)
                        continue

                    file_count += 1
                    info = None if _is_ignored(path) else _media_info(path)
                    if info is None:
                        ignored.append(path)
                        continue
                    kind, mime = info
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = path.stat().st_size
                    supported_here.append(path)
                    media_metadata[path] = (kind, mime, size)
                    supported_count += 1
        except OSError as exc:
            raise FolderScanError(f"Could not scan folder {root}: {exc}") from exc

        # Preserve deterministic traversal/classification without doing any more
        # filesystem enumeration later.
        dirs.sort(key=lambda item: natural_path_key(Path(item.name)))
        supported_here.sort(key=lambda item: natural_path_key(Path(item.name)))
        child_dirs[root] = dirs
        direct_supported[root] = supported_here
        stack.extend(reversed(dirs))

        directory_count = len(directory_order)
        if progress and (directory_count % 250 == 0 or file_count % 5000 == 0):
            progress("indexing", file_count, 0, f"Indexed {file_count:,} files in {directory_count:,} folders")

    # Propagate media-bearing state exactly once per directory. Children have
    # already been processed when walking this list in reverse discovery order.
    media_dirs: set[Path] = set()
    for directory in reversed(directory_order):
        if direct_supported.get(directory) or any(child in media_dirs for child in child_dirs.get(directory, ())):
            media_dirs.add(directory)

    directory_count = len(directory_order)
    elapsed = time.monotonic() - started
    logger.info(
        "scan index complete source=%s files=%d supported=%d dirs=%d ignored=%d elapsed=%.2fs rate=%.0f_files_s",
        source, file_count, supported_count, directory_count, len(ignored), elapsed,
        file_count / elapsed if elapsed else 0.0,
    )
    if progress:
        progress("indexing", file_count, file_count, f"Indexed {file_count:,} files in {directory_count:,} folders")
    return ScanIndex(
        source, direct_supported, child_dirs, media_dirs, ignored, media_metadata,
        file_count, supported_count, directory_count,
    )

def _is_ignored(path: Path) -> bool:
    return path.name.casefold() in IGNORED_FILENAMES or path.name.startswith("._")


def _media_info(path: Path) -> tuple[MediaKind, str] | None:
    extension = path.suffix.casefold()
    if extension in SUPPORTED_MEDIA:
        return SUPPORTED_MEDIA[extension]

    # Keep this deliberately conservative. mimetypes is only a fallback for
    # a known extension alias, not permission to import every image/video type.
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed == "image/jpeg":
        return MediaKind.IMAGE, guessed
    return None


def _contains_supported_media(directory: Path, index: ScanIndex | None = None) -> bool:
    if index is not None:
        return index.has_media(directory)
    return any(child.is_file() and not _is_ignored(child) and _media_info(child) for child in directory.rglob("*"))


def _direct_supported_files(directory: Path, index: ScanIndex | None = None) -> list[Path]:
    if index is not None:
        return list(index.files(directory))
    return [child for child in directory.iterdir() if child.is_file() and not _is_ignored(child) and _media_info(child)]


def _direct_child_dirs(directory: Path, index: ScanIndex | None = None) -> list[Path]:
    if index is not None:
        return list(index.dirs(directory))
    return [child for child in directory.iterdir() if child.is_dir()]


def _normalize_hint_paths(source: Path, values: list[str | Path] | None) -> set[Path]:
    """Resolve user-supplied folder-role overrides relative to the scan source."""
    if not values:
        return set()

    resolved: set[Path] = set()
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = source / path
        resolved.add(path.resolve())
    return resolved


def _looks_like_extra(
    directory: Path,
    *,
    extra_overrides: set[Path] | None = None,
    primary_overrides: set[Path] | None = None,
) -> bool:
    resolved = directory
    if primary_overrides and resolved in primary_overrides:
        return False
    if extra_overrides and resolved in extra_overrides:
        return True

    tokens = {token for token in _TOKENIZE.split(directory.name.casefold()) if token}
    return bool(tokens & _EXTRA_WORDS)


def _find_content_root(
    source: Path,
    *,
    extra_overrides: set[Path] | None = None,
    primary_overrides: set[Path] | None = None,
    structural_overrides: set[Path] | None = None,
    index: ScanIndex | None = None,
) -> Path:
    """Collapse harmless single-directory wrappers around an import.

    Extra-like directories are never selected as wrappers. This prevents a
    folder named e.g. ``Extra Angles`` from accidentally becoming the comic's
    content root.
    """
    current = source

    while True:
        direct_media = _direct_supported_files(current, index)
        if direct_media:
            return current

        all_media_children = [
            child
            for child in _direct_child_dirs(current, index)
            if _contains_supported_media(child, index)
        ]
        media_children = [
            child
            for child in all_media_children
            if not _looks_like_extra(
                child,
                extra_overrides=extra_overrides,
                primary_overrides=primary_overrides,
            )
        ]
        # Collapse only a genuinely harmless wrapper: one media-bearing child
        # total. If an extra-like sibling exists, the current directory carries
        # structure and must be retained for review.
        if len(all_media_children) != 1 or len(media_children) != 1:
            return current
        if structural_overrides and media_children[0] in structural_overrides:
            return current

        current = media_children[0]


def _rects_close(first, second, *, tolerance: float = 0.01) -> bool:
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(first, second))


def _direct_pdf_image(page, document) -> tuple[bytes, str, str] | None:
    """Return a lossless direct page-image extraction when visual equivalence is clear.

    This intentionally accepts only the simple comic-PDF case: one unrotated,
    unmasked RGB/grayscale raster covering the complete uncropped page, with no
    text, vector drawings, annotations, or widgets layered over it. Anything
    ambiguous falls back to normal page rendering.
    """
    if page.rotation != 0 or not _rects_close(page.cropbox, page.mediabox):
        return None
    if page.first_annot is not None or page.first_widget is not None:
        return None
    if page.get_text("text").strip() or page.get_drawings():
        return None

    images = page.get_image_info(xrefs=True)
    if len(images) != 1:
        return None
    info = images[0]
    if not info.get("xref") or info.get("has-mask"):
        return None
    if info.get("bpc") != 8 or info.get("colorspace") not in {1, 3}:
        return None
    if not _rects_close(info["bbox"], page.rect):
        return None

    # A matching bounding box alone is not sufficient: a PDF may rotate or
    # mirror the underlying image into that box. Direct extraction must preserve
    # the same orientation the page renderer would show.
    a, b, c, d, e, f = (float(value) for value in info["transform"])
    if abs(b) > 0.01 or abs(c) > 0.01 or a <= 0 or d <= 0 or abs(e) > 0.01 or abs(f) > 0.01:
        return None
    if abs(a - page.rect.width) > 0.01 or abs(d - page.rect.height) > 0.01:
        return None

    extracted = document.extract_image(int(info["xref"]))
    image_type = str(extracted.get("ext", "")).casefold()
    output = PDF_DIRECT_IMAGE_TYPES.get(image_type)
    image = extracted.get("image")
    if output is None or not isinstance(image, (bytes, bytearray)) or not image:
        return None
    suffix, mime_type = output
    return bytes(image), suffix, mime_type


def _cached_pdf_page(output_dir: Path, stem: str) -> tuple[Path, str] | None:
    for suffix, mime_type in ((".jpg", "image/jpeg"), (".png", "image/png")):
        destination = output_dir / f"{stem}{suffix}"
        if destination.is_file():
            return destination, mime_type
    return None


@dataclass
class _PDFPageResult:
    destination: Path
    mime_type: str
    mode: str
    load_seconds: float = 0.0
    extract_seconds: float = 0.0
    raster_seconds: float = 0.0
    save_seconds: float = 0.0
    elapsed: float = 0.0


def _render_pdf_page(pdf_path: Path, page_index: int, destination: Path) -> _PDFPageResult:
    """Spawn-safe worker: only paths/numbers cross the process boundary."""
    import fitz

    started = time.monotonic()
    result = _PDFPageResult(destination, "image/jpeg", "rendered")
    # Open independently for each job, bounding retained MuPDF caches as well as
    # pixmaps. Never inherit the caller's document or rotating log handlers.
    with fitz.open(pdf_path) as document:
        page = document.load_page(page_index)
        result.load_seconds = time.monotonic() - started
        raster_started = time.monotonic()
        pixmap = page.get_pixmap(dpi=PDF_RENDER_DPI, alpha=False)
        result.raster_seconds = time.monotonic() - raster_started
        save_started = time.monotonic()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as handle:
                temporary = Path(handle.name)
            pixmap.save(temporary, output="jpg", jpg_quality=PDF_JPEG_QUALITY)
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        result.save_seconds = time.monotonic() - save_started
    result.elapsed = time.monotonic() - started
    return result


def _pdf_page_results(document, pdf_path: Path, output_dir: Path, stack: ExitStack):
    """Prepare sequentially, with a bounded ordered window of fallback jobs."""
    width = max(4, len(str(document.page_count)))
    pending: deque[tuple[_PDFPageResult, Future | None]] = deque()
    pool = None

    def completed():
        result, future = pending.popleft()
        if future is not None:
            worker = future.result()
            result.load_seconds += worker.load_seconds
            result.raster_seconds = worker.raster_seconds
            result.save_seconds = worker.save_seconds
            # Sum actual page work, excluding queue/wait time and other pages.
            result.elapsed += worker.elapsed
        return result

    for page_index in range(document.page_count):
        started = time.monotonic()
        stem = f"{page_index + 1:0{width}d}"
        cached = _cached_pdf_page(output_dir, stem)
        future = None
        if cached is not None:
            result = _PDFPageResult(*cached, "cached")
        else:
            load_started = time.monotonic()
            page = document.load_page(page_index)
            load_seconds = time.monotonic() - load_started
            extract_started = time.monotonic()
            direct = _direct_pdf_image(page, document)
            extract_seconds = time.monotonic() - extract_started
            if direct is not None:
                image_bytes, suffix, mime_type = direct
                destination = output_dir / f"{stem}{suffix}"
                save_started = time.monotonic()
                destination.write_bytes(image_bytes)
                result = _PDFPageResult(destination, mime_type, "extracted",
                                        save_seconds=time.monotonic() - save_started)
            else:
                result = _PDFPageResult(output_dir / f"{stem}.jpg", "image/jpeg", "rendered")
                if pool is None:
                    # Explicit spawn is safe even when invoked by the web app's
                    # background thread; fork would inherit live native state.
                    pool = ProcessPoolExecutor(max_workers=PDF_RENDER_WORKERS,
                                               mp_context=multiprocessing.get_context("spawn"))
                    stack.callback(pool.shutdown, wait=True, cancel_futures=True)
                future = pool.submit(_render_pdf_page, pdf_path, page_index, result.destination)
            result.load_seconds = load_seconds
            result.extract_seconds = extract_seconds
        result.elapsed = time.monotonic() - started
        pending.append((result, future))
        if len(pending) >= PDF_RENDER_WORKERS:
            yield completed()
    while pending:
        yield completed()


def _render_pdf_pages(
    pdf_path: Path,
    relative_to: Path,
    pdf_cache_root: Path,
    progress: ScanProgress | None = None,
) -> list[tuple[Path, Path, MediaKind, str]]:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise FolderScanError("PDF import requires PyMuPDF. Reinstall with the web/import dependencies.") from exc

    fingerprint = hashlib.sha256(
        (
            f"{pdf_path.resolve()}|{pdf_path.stat().st_size}|{pdf_path.stat().st_mtime_ns}"
            f"|cache={PDF_CACHE_VERSION}|dpi={PDF_RENDER_DPI}|format=jpg|quality={PDF_JPEG_QUALITY}|direct=jpeg,png"
        ).encode("utf-8")
    ).hexdigest()[:20]
    output_dir = pdf_cache_root / fingerprint
    output_dir.mkdir(parents=True, exist_ok=True)
    display_dir = pdf_path.relative_to(relative_to).parent / f"{pdf_path.stem}_pdf_pages"

    rendered: list[tuple[Path, Path, MediaKind, str]] = []
    open_started = time.monotonic()
    try:
        with fitz.open(pdf_path) as document, ExitStack() as workers:
            open_elapsed = time.monotonic() - open_started
            if document.page_count < 1:
                raise FolderScanError(f"PDF has no pages: {pdf_path}")
            page_count = document.page_count
            render_started = time.monotonic()
            logger.info(
                "pdf render started file=%s pages=%d source_bytes=%d open_elapsed=%.3fs",
                pdf_path, page_count, pdf_path.stat().st_size, open_elapsed,
            )
            if progress:
                progress("rendering_pdf", 0, page_count, f"Processing PDF: {pdf_path.name}")

            cached_pages = 0
            extracted_pages = 0
            rendered_pages = 0
            output_bytes = 0
            total_load_seconds = 0.0
            total_extract_seconds = 0.0
            total_raster_seconds = 0.0
            total_save_seconds = 0.0
            slowest_page = 0
            slowest_page_seconds = 0.0

            for page_number, result in enumerate(_pdf_page_results(document, pdf_path, output_dir, workers), 1):
                destination, mime_type = result.destination, result.mime_type
                cached_pages += result.mode == "cached"
                extracted_pages += result.mode == "extracted"
                rendered_pages += result.mode == "rendered"
                total_load_seconds += result.load_seconds
                total_extract_seconds += result.extract_seconds
                total_raster_seconds += result.raster_seconds
                total_save_seconds += result.save_seconds
                page_bytes = destination.stat().st_size
                output_bytes += page_bytes
                page_elapsed = result.elapsed
                if page_elapsed > slowest_page_seconds:
                    slowest_page = page_number
                    slowest_page_seconds = page_elapsed

                rendered.append((destination, display_dir / destination.name, MediaKind.IMAGE, mime_type))
                if progress:
                    progress("rendering_pdf", page_number, page_count, f"Processing PDF: {pdf_path.name}")
                if page_number % 25 == 0 or page_number == page_count:
                    logger.info(
                        "pdf render progress file=%s page=%d/%d extracted=%d rendered=%d cached=%d "
                        "output_bytes=%d elapsed=%.2fs",
                        pdf_path, page_number, page_count, extracted_pages, rendered_pages, cached_pages,
                        output_bytes, time.monotonic() - render_started,
                    )

            elapsed = time.monotonic() - render_started
            logger.info(
                "pdf render complete file=%s pages=%d extracted=%d rendered=%d cached=%d output_bytes=%d "
                "open_elapsed=%.3fs load_elapsed=%.3fs extract_elapsed=%.3fs raster_elapsed=%.3fs "
                "save_elapsed=%.3fs total_elapsed=%.3fs slowest_page=%d slowest_page_elapsed=%.3fs",
                pdf_path, page_count, extracted_pages, rendered_pages, cached_pages, output_bytes, open_elapsed,
                total_load_seconds, total_extract_seconds, total_raster_seconds, total_save_seconds, elapsed,
                slowest_page, slowest_page_seconds,
            )
    except FolderScanError:
        raise
    except Exception as exc:
        raise FolderScanError(f"Could not render PDF {pdf_path.name}: {exc}") from exc
    return rendered


def _scan_media(
    files: list[Path],
    relative_to: Path,
    pdf_cache_root: Path,
    index: ScanIndex | None = None,
    progress: ScanProgress | None = None,
) -> list[ScannedMedia]:
    supported: list[tuple[Path, Path, MediaKind, str, int | None]] = []
    for path in files:
        indexed = index.metadata(path) if index is not None else None
        info = (indexed[0], indexed[1]) if indexed is not None else _media_info(path)
        if info is None:
            continue
        kind, mime = info
        if path.suffix.casefold() == ".pdf":
            for rendered_path, rendered_relative, rendered_kind, rendered_mime in _render_pdf_pages(path, relative_to, pdf_cache_root, progress):
                supported.append((rendered_path, rendered_relative, rendered_kind, rendered_mime, None))
        else:
            supported.append((
                path, path.relative_to(relative_to), kind, mime,
                indexed[2] if indexed is not None else None,
            ))

    supported.sort(key=lambda item: natural_path_key(item[1]))

    return [
        ScannedMedia(
            path=path,
            relative_path=relative_path,
            media_kind=kind,
            mime_type=mime,
            size_bytes=size if size is not None else path.stat().st_size,
            order=order,
        )
        for order, (path, relative_path, kind, mime, size) in enumerate(supported, start=1)
    ]


def _scan_extra_groups(
    directory: Path,
    relative_to: Path,
    pdf_cache_root: Path,
    index: ScanIndex | None = None,
    progress: ScanProgress | None = None,
) -> list[ScannedGroup]:
    """Scan extra content while preserving folders as distinct named groups.

    An extra container such as ``Extras/`` may itself contain several separately
    named sets (for example ``Textless/`` and ``Extra Angles/``). Those remain
    separate groups instead of being flattened together. If a directory has
    media directly inside it, that direct media forms its own group as well.
    """
    groups: list[ScannedGroup] = []

    direct_files = _direct_supported_files(directory, index)
    direct_media = _scan_media(direct_files, directory, pdf_cache_root, index, progress)
    if direct_media:
        groups.append(
            ScannedGroup(
                name=directory.name,
                relative_path=directory.relative_to(relative_to),
                suggested_role=SuggestedRole.EXTRA,
                media=direct_media,
            )
        )

    child_dirs = [
        child for child in _direct_child_dirs(directory, index)
        if _contains_supported_media(child, index)
    ]
    for child in sorted(child_dirs, key=lambda p: natural_path_key(p.relative_to(directory))):
        groups.extend(_scan_extra_groups(child, relative_to, pdf_cache_root, index, progress))

    return groups


def _looks_like_primary_container(directory: Path) -> bool:
    """Return True for conventional folders that merely contain main comic pages."""
    tokens = {token for token in _TOKENIZE.split(directory.name.casefold()) if token}
    return bool(tokens & _PRIMARY_CONTAINER_WORDS)


def _scan_single_issue(
    directory: Path,
    relative_to: Path,
    *,
    extra_overrides: set[Path] | None = None,
    primary_overrides: set[Path] | None = None,
    separate_child_folders: bool = False,
    pdf_cache_root: Path,
    index: ScanIndex | None = None,
    progress: ScanProgress | None = None,
) -> tuple[ScannedGroup | None, list[ScannedGroup]]:
    """Scan one issue while preserving meaningful child folders as groups.

    Direct files belong to the main comic. Extra-like folders are always kept
    separate, at any depth. When an issue already has direct page files, other
    sibling folders are also treated as extra groups by default instead of being
    silently flattened into primary content. Conventional page containers such
    as ``Pages``/``Images`` and explicit primary overrides remain primary.
    """
    direct_files = _direct_supported_files(directory, index)
    primary_nested_files: list[Path] = []
    extra_dirs: list[Path] = []
    has_direct_primary = bool(direct_files)

    def walk_container(container: Path, *, force_primary: bool = False) -> None:
        # Direct media comes from the index; child enumeration never touches
        # the filesystem again.
        primary_nested_files.extend(_direct_supported_files(container, index))
        for child in _direct_child_dirs(container, index):
            if not _contains_supported_media(child, index):
                continue

            resolved = child
            explicitly_primary = bool(primary_overrides and resolved in primary_overrides)
            if _looks_like_extra(
                child,
                extra_overrides=extra_overrides,
                primary_overrides=primary_overrides,
            ):
                extra_dirs.append(child)
                continue

            if force_primary or explicitly_primary or _looks_like_primary_container(child):
                walk_container(child, force_primary=True)
                continue

            # If the issue already has its own direct pages, a separate sibling
            # folder represents separate material unless the user explicitly or
            # conventionally marked it as another primary page container.
            if separate_child_folders and has_direct_primary:
                extra_dirs.append(child)
            else:
                walk_container(child)

    for child in _direct_child_dirs(directory, index):
        if not _contains_supported_media(child, index):
            continue
        resolved = child
        explicitly_primary = bool(primary_overrides and resolved in primary_overrides)
        if _looks_like_extra(
            child,
            extra_overrides=extra_overrides,
            primary_overrides=primary_overrides,
        ):
            extra_dirs.append(child)
        elif explicitly_primary or _looks_like_primary_container(child):
            walk_container(child, force_primary=True)
        elif separate_child_folders and has_direct_primary:
            extra_dirs.append(child)
        else:
            walk_container(child)

    combined = direct_files + primary_nested_files
    primary_media = _scan_media(combined, directory, pdf_cache_root, index, progress) if combined else []

    primary = None
    if primary_media:
        primary = ScannedGroup(
            name="Primary",
            relative_path=directory.relative_to(relative_to),
            suggested_role=SuggestedRole.PRIMARY,
            media=primary_media,
        )

    extras: list[ScannedGroup] = []
    seen: set[Path] = set()
    for child in sorted(extra_dirs, key=lambda p: natural_path_key(p.relative_to(directory))):
        resolved = child
        if resolved in seen:
            continue
        seen.add(resolved)
        extras.extend(_scan_extra_groups(child, relative_to, pdf_cache_root, index, progress))
    return primary, extras


def _candidate_series_children(
    content_root: Path,
    *,
    extra_overrides: set[Path] | None = None,
    primary_overrides: set[Path] | None = None,
    index: ScanIndex | None = None,
) -> tuple[list[Path], list[Path]]:
    """Split media-bearing child folders into issue-like and series-extra candidates."""
    issue_dirs: list[Path] = []
    extra_dirs: list[Path] = []
    for child in _direct_child_dirs(content_root, index):
        if not _contains_supported_media(child, index):
            continue
        if _looks_like_extra(
            child,
            extra_overrides=extra_overrides,
            primary_overrides=primary_overrides,
        ):
            extra_dirs.append(child)
        else:
            issue_dirs.append(child)
    return issue_dirs, extra_dirs


def _looks_like_loose_series_extra(path: Path) -> bool:
    stem = path.stem.casefold().replace("-", " ").replace("_", " ")
    words = set(stem.split())
    return bool(words & {"cover", "covers", "poster", "promo", "promotional", "banner", "logo", "thumbnail", "thumb"})


def scan_folder(
    source: str | Path,
    *,
    extra_folders: list[str | Path] | None = None,
    primary_folders: list[str | Path] | None = None,
    subseries_folders: list[str | Path] | None = None,
    pdf_cache_root: str | Path | None = None,
    progress: ScanProgress | None = None,
) -> ScannedImport:
    source = Path(source).expanduser().resolve()
    if pdf_cache_root is None:
        pdf_cache_root = Path(tempfile.mkdtemp(prefix="comic-archive-pdf-"))
    else:
        pdf_cache_root = Path(pdf_cache_root).expanduser().resolve()
    pdf_cache_root.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        raise FolderScanError(f"Folder does not exist: {source}")
    if not source.is_dir():
        raise FolderScanError(f"Source is not a folder: {source}")

    scan_started = time.monotonic()
    logger.info("scan started source=%s pdf_cache=%s", source, pdf_cache_root)
    index = _build_scan_index(source, progress)
    if index.supported_count == 0:
        logger.warning("scan source contains no supported media source=%s files=%d dirs=%d", source, index.file_count, index.directory_count)
    if progress:
        progress("classifying", 0, index.directory_count, "Classifying folders and issues")

    extra_overrides = _normalize_hint_paths(source, extra_folders)
    primary_overrides = _normalize_hint_paths(source, primary_folders)
    subseries_overrides = _normalize_hint_paths(source, subseries_folders)
    overlap = (extra_overrides & primary_overrides) | (extra_overrides & subseries_overrides) | (primary_overrides & subseries_overrides)
    if overlap:
        paths = ", ".join(str(path) for path in sorted(overlap))
        raise FolderScanError(f"Folder has conflicting import roles: {paths}")

    content_root = _find_content_root(
        source,
        extra_overrides=extra_overrides,
        primary_overrides=primary_overrides,
        structural_overrides=subseries_overrides,
        index=index,
    )
    direct_media_files = _direct_supported_files(content_root, index)
    issue_dirs, series_extra_dirs = _candidate_series_children(
        content_root,
        extra_overrides=extra_overrides,
        primary_overrides=primary_overrides,
        index=index,
    )

    # Explicit sub-series hints force a series-shaped scan even when there is
    # only one child. Hints are source-relative and may be nested recursively.
    content_subseries = {
        path for path in subseries_overrides
        if path == content_root or content_root in path.parents
    }
    direct_pdfs = [path for path in direct_media_files if path.suffix.casefold() == ".pdf"]
    pdf_only_series = len(direct_pdfs) >= 2 and len(direct_pdfs) == len(direct_media_files)
    loose_extras_only = bool(direct_media_files) and all(_looks_like_loose_series_extra(path) for path in direct_media_files)
    is_series = (not direct_media_files or pdf_only_series or loose_extras_only) and (
        len(issue_dirs) >= 2 or (len(issue_dirs) >= 1 and bool(series_extra_dirs)) or bool(content_subseries) or pdf_only_series
    )
    logger.info(
        "scan classification shape source=%s content_root=%s series=%s direct_media=%d issue_dirs=%d series_extra_dirs=%d subseries_hints=%d pdf_only_series=%s",
        source, content_root, is_series, len(direct_media_files), len(issue_dirs), len(series_extra_dirs), len(content_subseries), pdf_only_series,
    )

    primary: ScannedGroup | None = None
    extras: list[ScannedGroup] = []
    issues: list[ScannedIssue] = []
    subseries: list[ScannedSeries] = []

    classified_dirs = 0
    classify_started = time.monotonic()

    def report_classification(path: Path, detail: str) -> None:
        nonlocal classified_dirs
        classified_dirs += 1
        if progress and (classified_dirs == 1 or classified_dirs % 25 == 0):
            progress(
                "classifying", classified_dirs, index.directory_count,
                f"{detail}: {path.name} ({classified_dirs:,} folders processed)",
            )
        if classified_dirs % 250 == 0:
            logger.info(
                "scan classification progress source=%s processed_dirs=%d total_dirs=%d issues=%d extras=%d subseries=%d elapsed=%.2fs",
                source, classified_dirs, index.directory_count, len(issues), len(extras), len(subseries), time.monotonic() - classify_started,
            )

    def scan_series_level(container: Path, series_path: Path) -> None:
        children = [
            child for child in _direct_child_dirs(container, index)
            if _contains_supported_media(child, index)
        ]
        for child in sorted(children, key=lambda p: natural_path_key(p.relative_to(container))):
            child_rel_source = child
            child_rel_content = child.relative_to(content_root)
            if _looks_like_extra(child, extra_overrides=extra_overrides, primary_overrides=primary_overrides):
                report_classification(child, "Scanning series extras")
                started = time.monotonic()
                groups = _scan_extra_groups(child, content_root, pdf_cache_root, index, progress)
                for group in groups:
                    group.series_path = series_path
                    extras.append(group)
                duration = time.monotonic() - started
                if duration >= 1.0:
                    logger.warning("slow scan series-extra path=%s groups=%d elapsed=%.2fs", child, len(groups), duration)
                continue
            if child_rel_source in subseries_overrides:
                report_classification(child, "Scanning sub-series")
                subseries.append(ScannedSeries(name=child.name, relative_path=child_rel_content, parent_path=series_path))
                scan_series_level(child, child_rel_content)
                continue
            report_classification(child, "Scanning issue")
            started = time.monotonic()
            issue_primary, issue_extras = _scan_single_issue(
                child,
                content_root,
                extra_overrides=extra_overrides,
                primary_overrides=primary_overrides,
                separate_child_folders=True,
                pdf_cache_root=pdf_cache_root,
                index=index,
                progress=progress,
            )
            if issue_primary is not None:
                issue_primary.series_path = series_path
            for group in issue_extras:
                group.series_path = series_path
            issues.append(ScannedIssue(
                name=child.name,
                relative_path=child_rel_content,
                primary=issue_primary,
                extras=issue_extras,
                series_path=series_path,
            ))
            duration = time.monotonic() - started
            if duration >= 1.0:
                logger.warning(
                    "slow scan issue path=%s primary_media=%d extra_groups=%d elapsed=%.2fs",
                    child, len(issue_primary.media) if issue_primary else 0, len(issue_extras), duration,
                )

    if is_series:
        scan_series_level(content_root, Path('.'))
        # Multiple loose PDFs at a series root are much more likely to be
        # separate issues than one continuous page stream. Seed one logical
        # issue per PDF; the virtual workspace can regroup/reorder them freely.
        if pdf_only_series:
            for pdf_path in sorted(direct_pdfs, key=lambda p: natural_path_key(p.relative_to(content_root))):
                media = _scan_media([pdf_path], content_root, pdf_cache_root, index, progress)
                issue_key = Path(f"__pdf_issue__/{pdf_path.stem}")
                primary_group = ScannedGroup(
                    name=pdf_path.stem, relative_path=issue_key, suggested_role=SuggestedRole.PRIMARY,
                    media=media, series_path=Path('.'),
                )
                issues.append(ScannedIssue(
                    name=pdf_path.stem, relative_path=issue_key, primary=primary_group, extras=[], series_path=Path('.'),
                ))
        elif loose_extras_only and direct_media_files:
            extras.append(ScannedGroup(
                name="Series extras", relative_path=Path("__loose_series_extras__"),
                suggested_role=SuggestedRole.EXTRA,
                media=_scan_media(direct_media_files, content_root, pdf_cache_root, index, progress), series_path=Path('.'),
            ))
    else:
        if progress:
            progress("classifying", 0, index.directory_count, f"Scanning one-shot: {content_root.name}")
        one_shot_started = time.monotonic()
        primary, extras = _scan_single_issue(
            content_root,
            content_root,
            extra_overrides=extra_overrides,
            primary_overrides=primary_overrides,
            pdf_cache_root=pdf_cache_root,
            index=index,
            progress=progress,
        )
        logger.info(
            "scan one-shot complete path=%s primary_media=%d extra_groups=%d elapsed=%.2fs",
            content_root, len(primary.media) if primary else 0, len(extras), time.monotonic() - one_shot_started,
        )
    if progress:
        progress("finalizing", index.directory_count, index.directory_count, "Finalizing scan results")
    ignored_files = sorted(
        (path.relative_to(content_root) for path in index.ignored if content_root == path.parent or content_root in path.parents),
        key=natural_path_key,
    )
    elapsed = time.monotonic() - scan_started
    logger.info(
        "scan complete source=%s content_root=%s series=%s issues=%d subseries=%d extras=%d supported=%d ignored=%d elapsed=%.2fs",
        source, content_root, is_series, len(issues), len(subseries), len(extras), index.supported_count, len(ignored_files), elapsed,
    )
    if progress:
        progress("complete", index.directory_count, index.directory_count, f"Scan complete: {index.supported_count:,} media files, {len(issues):,} issues")

    return ScannedImport(
        source=source,
        content_root=content_root,
        primary=primary,
        extras=extras,
        issues=issues,
        subseries=subseries,
        ignored_files=ignored_files,
    )
