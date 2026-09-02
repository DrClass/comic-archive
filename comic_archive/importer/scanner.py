from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from .models import (
    MediaKind,
    ScannedGroup,
    ScannedImport,
    ScannedIssue,
    ScannedMedia,
    SuggestedRole,
)
from .sorting import natural_path_key

SUPPORTED_MEDIA: dict[str, tuple[MediaKind, str]] = {
    ".jpg": (MediaKind.IMAGE, "image/jpeg"),
    ".jpeg": (MediaKind.IMAGE, "image/jpeg"),
    ".png": (MediaKind.IMAGE, "image/png"),
    ".gif": (MediaKind.IMAGE, "image/gif"),
    ".mp4": (MediaKind.VIDEO, "video/mp4"),
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

        current = media_children[0]


def _scan_media(files: list[Path], relative_to: Path) -> list[ScannedMedia]:
    supported: list[tuple[Path, MediaKind, str]] = []
    for path in files:
        info = _media_info(path)
        if info is None:
            continue
        kind, mime = info
        supported.append((path, kind, mime))

    supported.sort(key=lambda item: natural_path_key(item[0].relative_to(relative_to)))

    return [
        ScannedMedia(
            path=path,
            relative_path=path.relative_to(relative_to),
            media_kind=kind,
            mime_type=mime,
            size_bytes=path.stat().st_size,
            order=index,
        )
        for index, (path, kind, mime) in enumerate(supported, start=1)
    ]


def _scan_extra_groups(directory: Path, relative_to: Path) -> list[ScannedGroup]:
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
    direct_media = _scan_media(direct_files, directory)
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
        groups.extend(_scan_extra_groups(child, relative_to))

    return groups


def _scan_single_issue(
    directory: Path,
    relative_to: Path,
    *,
    extra_overrides: set[Path] | None = None,
    primary_overrides: set[Path] | None = None,
) -> tuple[ScannedGroup | None, list[ScannedGroup]]:
    """Scan one issue-like directory without flattening extra-like folders."""
    direct_files = [child for child in directory.iterdir() if child.is_file()]
    primary_media = _scan_media(direct_files, directory)

    # Non-extra subfolders are allowed to be page containers (e.g. Pages/).
    # Their media is folded into primary. Extra-like subfolders are kept apart.
    extra_dirs: list[Path] = []
    primary_nested_files: list[Path] = []
    for child in directory.iterdir():
        if not child.is_dir() or not _contains_supported_media(child):
            continue
        if _looks_like_extra(
            child,
            extra_overrides=extra_overrides,
            primary_overrides=primary_overrides,
        ):
            extra_dirs.append(child)
        else:
            primary_nested_files.extend(path for path in child.rglob("*") if path.is_file())

    if primary_nested_files:
        combined = [path for path in direct_files if _media_info(path)] + primary_nested_files
        primary_media = _scan_media(combined, directory)

    primary = None
    if primary_media:
        primary = ScannedGroup(
            name="Primary",
            relative_path=directory.relative_to(relative_to),
            suggested_role=SuggestedRole.PRIMARY,
            media=primary_media,
        )

    extras: list[ScannedGroup] = []
    for child in sorted(extra_dirs, key=lambda p: natural_path_key(p.relative_to(directory))):
        extras.extend(_scan_extra_groups(child, relative_to))
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
) -> ScannedImport:
    source = Path(source).expanduser().resolve()
    if not source.exists():
        raise FolderScanError(f"Folder does not exist: {source}")
    if not source.is_dir():
        raise FolderScanError(f"Source is not a folder: {source}")

    extra_overrides = _normalize_hint_paths(source, extra_folders)
    primary_overrides = _normalize_hint_paths(source, primary_folders)
    overlap = extra_overrides & primary_overrides
    if overlap:
        paths = ", ".join(str(path) for path in sorted(overlap))
        raise FolderScanError(f"Folder cannot be both primary and extra: {paths}")

    content_root = _find_content_root(
        source,
        extra_overrides=extra_overrides,
        primary_overrides=primary_overrides,
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

    # A root with multiple issue-like children is a plausible series. A root
    # with one issue-like child plus explicit extra-like siblings also carries
    # series structure and must not be flattened into one issue.
    is_series = not direct_media_files and (
        len(issue_dirs) >= 2 or (len(issue_dirs) >= 1 and bool(series_extra_dirs))
    )

    primary: ScannedGroup | None = None
    extras: list[ScannedGroup] = []
    issues: list[ScannedIssue] = []

    if is_series:
        for issue_dir in sorted(issue_dirs, key=lambda p: natural_path_key(p.relative_to(content_root))):
            issue_primary, issue_extras = _scan_single_issue(
                issue_dir,
                content_root,
                extra_overrides=extra_overrides,
                primary_overrides=primary_overrides,
            )
            issues.append(
                ScannedIssue(
                    name=issue_dir.name,
                    relative_path=issue_dir.relative_to(content_root),
                    primary=issue_primary,
                    extras=issue_extras,
                )
            )

        extras = []
        for child in sorted(series_extra_dirs, key=lambda p: natural_path_key(p.relative_to(content_root))):
            extras.extend(_scan_extra_groups(child, content_root))
    else:
        primary, extras = _scan_single_issue(
            content_root,
            content_root,
            extra_overrides=extra_overrides,
            primary_overrides=primary_overrides,
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
        ignored_files=ignored_files,
    )
