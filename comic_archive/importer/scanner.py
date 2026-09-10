from __future__ import annotations

import mimetypes
import re
import tempfile
import hashlib
import logging
import os
import time
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
    ".gif": (MediaKind.IMAGE, "image/gif"),
    ".mp4": (MediaKind.VIDEO, "video/mp4"),
    # PDF is an accepted importer input. It is expanded into PNG pages by
    # _scan_media and is never committed to the library as a PDF.
    ".pdf": (MediaKind.IMAGE, "application/pdf"),
}

IGNORED_FILENAMES = {"thumbs.db", ".ds_store", "desktop.ini"}

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
    file_count: int
    supported_count: int
    directory_count: int

    def has_media(self, directory: Path) -> bool:
        return directory.resolve() in self.media_dirs

    def files(self, directory: Path) -> list[Path]:
        return self.direct_supported.get(directory.resolve(), [])

    def dirs(self, directory: Path) -> list[Path]:
        return self.child_dirs.get(directory.resolve(), [])


def _build_scan_index(source: Path, progress: ScanProgress | None = None) -> ScanIndex:
    started = time.monotonic()
    direct_supported: dict[Path, list[Path]] = {}
    child_dirs: dict[Path, list[Path]] = {}
    media_dirs: set[Path] = set()
    ignored: list[Path] = []
    file_count = 0
    supported_count = 0
    directory_count = 0

    if progress:
        progress("indexing", 0, 0, "Indexing filesystem")
    for root_text, dir_names, file_names in os.walk(source):
        root = Path(root_text).resolve()
        directory_count += 1
        dirs = [root / name for name in dir_names]
        child_dirs[root] = dirs
        supported_here: list[Path] = []
        for name in file_names:
            path = root / name
            file_count += 1
            if not _is_ignored(path) and _media_info(path) is not None:
                supported_here.append(path)
                supported_count += 1
                current = root
                while True:
                    media_dirs.add(current)
                    if current == source:
                        break
                    try:
                        current = current.parent
                    except Exception:
                        break
            else:
                ignored.append(path)
        direct_supported[root] = supported_here
        if progress and directory_count % 250 == 0:
            progress("indexing", file_count, 0, f"Indexed {file_count:,} files in {directory_count:,} folders")

    elapsed = time.monotonic() - started
    logger.info(
        "scan index complete source=%s files=%d supported=%d dirs=%d ignored=%d elapsed=%.2fs",
        source, file_count, supported_count, directory_count, len(ignored), elapsed,
    )
    if progress:
        progress("indexing", file_count, file_count, f"Indexed {file_count:,} files in {directory_count:,} folders")
    return ScanIndex(source, direct_supported, child_dirs, media_dirs, ignored, file_count, supported_count, directory_count)


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
    resolved = directory.resolve()
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
        if structural_overrides and media_children[0].resolve() in structural_overrides:
            return current

        current = media_children[0]


def _render_pdf_pages(pdf_path: Path, relative_to: Path, pdf_cache_root: Path) -> list[tuple[Path, Path, MediaKind, str]]:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise FolderScanError("PDF import requires PyMuPDF. Reinstall with the web/import dependencies.") from exc

    fingerprint = hashlib.sha256(
        f"{pdf_path.resolve()}|{pdf_path.stat().st_size}|{pdf_path.stat().st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:20]
    output_dir = pdf_cache_root / fingerprint
    output_dir.mkdir(parents=True, exist_ok=True)
    display_dir = pdf_path.relative_to(relative_to).parent / f"{pdf_path.stem}_pdf_pages"

    rendered: list[tuple[Path, Path, MediaKind, str]] = []
    try:
        with fitz.open(pdf_path) as document:
            if document.page_count < 1:
                raise FolderScanError(f"PDF has no pages: {pdf_path}")
            render_started = time.monotonic()
            logger.info("pdf render started file=%s pages=%d", pdf_path, document.page_count)
            width = max(4, len(str(document.page_count)))
            for page_index in range(document.page_count):
                name = f"{page_index + 1:0{width}d}.png"
                destination = output_dir / name
                if not destination.is_file():
                    page = document.load_page(page_index)
                    pixmap = page.get_pixmap(dpi=150, alpha=False)
                    pixmap.save(destination)
                rendered.append((destination, display_dir / name, MediaKind.IMAGE, "image/png"))
                if (page_index + 1) % 25 == 0 or page_index + 1 == document.page_count:
                    logger.info(
                        "pdf render progress file=%s page=%d/%d elapsed=%.2fs",
                        pdf_path, page_index + 1, document.page_count, time.monotonic() - render_started,
                    )
    except FolderScanError:
        raise
    except Exception as exc:
        raise FolderScanError(f"Could not render PDF {pdf_path.name}: {exc}") from exc
    return rendered


def _scan_media(files: list[Path], relative_to: Path, pdf_cache_root: Path) -> list[ScannedMedia]:
    supported: list[tuple[Path, Path, MediaKind, str]] = []
    for path in files:
        info = _media_info(path)
        if info is None:
            continue
        kind, mime = info
        if path.suffix.casefold() == ".pdf":
            supported.extend(_render_pdf_pages(path, relative_to, pdf_cache_root))
        else:
            supported.append((path, path.relative_to(relative_to), kind, mime))

    supported.sort(key=lambda item: natural_path_key(item[1]))

    return [
        ScannedMedia(
            path=path,
            relative_path=relative_path,
            media_kind=kind,
            mime_type=mime,
            size_bytes=path.stat().st_size,
            order=index,
        )
        for index, (path, relative_path, kind, mime) in enumerate(supported, start=1)
    ]


def _scan_extra_groups(directory: Path, relative_to: Path, pdf_cache_root: Path, index: ScanIndex | None = None) -> list[ScannedGroup]:
    """Scan extra content while preserving folders as distinct named groups.

    An extra container such as ``Extras/`` may itself contain several separately
    named sets (for example ``Textless/`` and ``Extra Angles/``). Those remain
    separate groups instead of being flattened together. If a directory has
    media directly inside it, that direct media forms its own group as well.
    """
    groups: list[ScannedGroup] = []

    direct_files = _direct_supported_files(directory, index)
    direct_media = _scan_media(direct_files, directory, pdf_cache_root)
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
        groups.extend(_scan_extra_groups(child, relative_to, pdf_cache_root, index))

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
        for child in container.iterdir():
            if child.is_file():
                if not _is_ignored(child) and _media_info(child):
                    primary_nested_files.append(child)
                continue
            if not child.is_dir() or not _contains_supported_media(child, index):
                continue

            resolved = child.resolve()
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

    for child in directory.iterdir():
        if not child.is_dir() or not _contains_supported_media(child, index):
            continue
        resolved = child.resolve()
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
    primary_media = _scan_media(combined, directory, pdf_cache_root) if combined else []

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
        resolved = child.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        extras.extend(_scan_extra_groups(child, relative_to, pdf_cache_root, index))
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
    for child in content_root.iterdir():
        if not child.is_dir() or not _contains_supported_media(child, index):
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
        if path == content_root.resolve() or content_root.resolve() in path.parents
    }
    direct_pdfs = [path for path in direct_media_files if path.suffix.casefold() == ".pdf"]
    pdf_only_series = len(direct_pdfs) >= 2 and len(direct_pdfs) == len(direct_media_files)
    loose_extras_only = bool(direct_media_files) and all(_looks_like_loose_series_extra(path) for path in direct_media_files)
    is_series = (not direct_media_files or pdf_only_series or loose_extras_only) and (
        len(issue_dirs) >= 2 or (len(issue_dirs) >= 1 and bool(series_extra_dirs)) or bool(content_subseries) or pdf_only_series
    )

    primary: ScannedGroup | None = None
    extras: list[ScannedGroup] = []
    issues: list[ScannedIssue] = []
    subseries: list[ScannedSeries] = []

    def scan_series_level(container: Path, series_path: Path) -> None:
        children = [
            child for child in _direct_child_dirs(container, index)
            if _contains_supported_media(child, index)
        ]
        for child in sorted(children, key=lambda p: natural_path_key(p.relative_to(container))):
            child_rel_source = child.resolve()
            child_rel_content = child.relative_to(content_root)
            if _looks_like_extra(child, extra_overrides=extra_overrides, primary_overrides=primary_overrides):
                for group in _scan_extra_groups(child, content_root, pdf_cache_root, index):
                    group.series_path = series_path
                    extras.append(group)
                continue
            if child_rel_source in subseries_overrides:
                subseries.append(ScannedSeries(name=child.name, relative_path=child_rel_content, parent_path=series_path))
                scan_series_level(child, child_rel_content)
                continue
            issue_primary, issue_extras = _scan_single_issue(
                child,
                content_root,
                extra_overrides=extra_overrides,
                primary_overrides=primary_overrides,
                separate_child_folders=True,
                pdf_cache_root=pdf_cache_root,
                index=index,
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

    if is_series:
        scan_series_level(content_root, Path('.'))
        # Multiple loose PDFs at a series root are much more likely to be
        # separate issues than one continuous page stream. Seed one logical
        # issue per PDF; the virtual workspace can regroup/reorder them freely.
        if pdf_only_series:
            for pdf_path in sorted(direct_pdfs, key=lambda p: natural_path_key(p.relative_to(content_root))):
                media = _scan_media([pdf_path], content_root, pdf_cache_root)
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
                media=_scan_media(direct_media_files, content_root, pdf_cache_root), series_path=Path('.'),
            ))
    else:
        primary, extras = _scan_single_issue(
            content_root,
            content_root,
            extra_overrides=extra_overrides,
            primary_overrides=primary_overrides,
            pdf_cache_root=pdf_cache_root,
            index=index,
        )
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
