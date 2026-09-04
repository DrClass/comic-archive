from __future__ import annotations

import mimetypes
import re
import tempfile
import hashlib
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


def _contains_supported_media(directory: Path) -> bool:
    return any(
        child.is_file() and not _is_ignored(child) and _media_info(child)
        for child in directory.rglob("*")
    )


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
) -> Path:
    """Collapse harmless single-directory wrappers around an import.

    Extra-like directories are never selected as wrappers. This prevents a
    folder named e.g. ``Extra Angles`` from accidentally becoming the comic's
    content root.
    """
    current = source

    while True:
        direct_media = [
            child
            for child in current.iterdir()
            if child.is_file() and not _is_ignored(child) and _media_info(child)
        ]
        if direct_media:
            return current

        all_media_children = [
            child
            for child in current.iterdir()
            if child.is_dir() and _contains_supported_media(child)
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
            width = max(4, len(str(document.page_count)))
            for page_index in range(document.page_count):
                name = f"{page_index + 1:0{width}d}.png"
                destination = output_dir / name
                if not destination.is_file():
                    page = document.load_page(page_index)
                    pixmap = page.get_pixmap(dpi=150, alpha=False)
                    pixmap.save(destination)
                rendered.append((destination, display_dir / name, MediaKind.IMAGE, "image/png"))
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


def _scan_extra_groups(directory: Path, relative_to: Path, pdf_cache_root: Path) -> list[ScannedGroup]:
    """Scan extra content while preserving folders as distinct named groups.

    An extra container such as ``Extras/`` may itself contain several separately
    named sets (for example ``Textless/`` and ``Extra Angles/``). Those remain
    separate groups instead of being flattened together. If a directory has
    media directly inside it, that direct media forms its own group as well.
    """
    groups: list[ScannedGroup] = []

    direct_files = [
        child for child in directory.iterdir()
        if child.is_file() and not _is_ignored(child) and _media_info(child)
    ]
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
        child for child in directory.iterdir()
        if child.is_dir() and _contains_supported_media(child)
    ]
    for child in sorted(child_dirs, key=lambda p: natural_path_key(p.relative_to(directory))):
        groups.extend(_scan_extra_groups(child, relative_to, pdf_cache_root))

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
) -> tuple[ScannedGroup | None, list[ScannedGroup]]:
    """Scan one issue while preserving meaningful child folders as groups.

    Direct files belong to the main comic. Extra-like folders are always kept
    separate, at any depth. When an issue already has direct page files, other
    sibling folders are also treated as extra groups by default instead of being
    silently flattened into primary content. Conventional page containers such
    as ``Pages``/``Images`` and explicit primary overrides remain primary.
    """
    direct_files = [
        child for child in directory.iterdir()
        if child.is_file() and not _is_ignored(child) and _media_info(child)
    ]
    primary_nested_files: list[Path] = []
    extra_dirs: list[Path] = []
    has_direct_primary = bool(direct_files)

    def walk_container(container: Path, *, force_primary: bool = False) -> None:
        for child in container.iterdir():
            if child.is_file():
                if not _is_ignored(child) and _media_info(child):
                    primary_nested_files.append(child)
                continue
            if not child.is_dir() or not _contains_supported_media(child):
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
        if not child.is_dir() or not _contains_supported_media(child):
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
        extras.extend(_scan_extra_groups(child, relative_to, pdf_cache_root))
    return primary, extras


def _candidate_series_children(
    content_root: Path,
    *,
    extra_overrides: set[Path] | None = None,
    primary_overrides: set[Path] | None = None,
) -> tuple[list[Path], list[Path]]:
    """Split media-bearing child folders into issue-like and series-extra candidates."""
    issue_dirs: list[Path] = []
    extra_dirs: list[Path] = []
    for child in content_root.iterdir():
        if not child.is_dir() or not _contains_supported_media(child):
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


def scan_folder(
    source: str | Path,
    *,
    extra_folders: list[str | Path] | None = None,
    primary_folders: list[str | Path] | None = None,
    subseries_folders: list[str | Path] | None = None,
    pdf_cache_root: str | Path | None = None,
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
    )
    direct_media_files = [
        child for child in content_root.iterdir()
        if child.is_file() and not _is_ignored(child) and _media_info(child)
    ]
    issue_dirs, series_extra_dirs = _candidate_series_children(
        content_root,
        extra_overrides=extra_overrides,
        primary_overrides=primary_overrides,
    )

    # Explicit sub-series hints force a series-shaped scan even when there is
    # only one child. Hints are source-relative and may be nested recursively.
    content_subseries = {
        path for path in subseries_overrides
        if path == content_root.resolve() or content_root.resolve() in path.parents
    }
    is_series = not direct_media_files and (
        len(issue_dirs) >= 2 or (len(issue_dirs) >= 1 and bool(series_extra_dirs)) or bool(content_subseries)
    )

    primary: ScannedGroup | None = None
    extras: list[ScannedGroup] = []
    issues: list[ScannedIssue] = []
    subseries: list[ScannedSeries] = []

    def scan_series_level(container: Path, series_path: Path) -> None:
        children = [
            child for child in container.iterdir()
            if child.is_dir() and _contains_supported_media(child)
        ]
        for child in sorted(children, key=lambda p: natural_path_key(p.relative_to(container))):
            child_rel_source = child.resolve()
            child_rel_content = child.relative_to(content_root)
            if _looks_like_extra(child, extra_overrides=extra_overrides, primary_overrides=primary_overrides):
                for group in _scan_extra_groups(child, content_root, pdf_cache_root):
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
    else:
        primary, extras = _scan_single_issue(
            content_root,
            content_root,
            extra_overrides=extra_overrides,
            primary_overrides=primary_overrides,
            pdf_cache_root=pdf_cache_root,
        )
    ignored_files = sorted(
        (
            path.relative_to(content_root)
            for path in content_root.rglob("*")
            if path.is_file() and (_is_ignored(path) or _media_info(path) is None)
        ),
        key=natural_path_key,
    )

    return ScannedImport(
        source=source,
        content_root=content_root,
        primary=primary,
        extras=extras,
        issues=issues,
        subseries=subseries,
        ignored_files=ignored_files,
    )
