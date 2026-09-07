from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .scanner import SUPPORTED_MEDIA, FolderScanError
from .sorting import natural_path_key


@dataclass(slots=True)
class BulkComicCandidate:
    path: Path
    name: str
    media_count: int


def discover_artist_comics(source: str | Path) -> tuple[Path, list[BulkComicCandidate]]:
    root = Path(source).expanduser().resolve()
    if not root.exists():
        raise FolderScanError(f"Artist folder does not exist: {root}")
    if not root.is_dir():
        raise FolderScanError(f"Artist source is not a folder: {root}")

    candidates: list[BulkComicCandidate] = []
    # Immediate child folders remain normal comic candidates. Root-level PDFs
    # are also exposed as one-comic candidates; the web importer wraps them in
    # temporary staging storage when selected, leaving the source untouched.
    children = sorted(root.iterdir(), key=lambda p: natural_path_key(Path(p.name)))
    for child in children:
        if child.is_dir():
            count = sum(
                1
                for path in child.rglob("*")
                if path.is_file() and path.suffix.casefold() in SUPPORTED_MEDIA
            )
            if count:
                candidates.append(BulkComicCandidate(path=child.resolve(), name=child.name, media_count=count))
        elif child.is_file() and child.suffix.casefold() == ".pdf":
            candidates.append(BulkComicCandidate(path=child.resolve(), name=child.stem, media_count=1))
    if not candidates:
        raise FolderScanError("No comic folders or root-level PDFs containing supported media were found directly inside this artist folder.")
    return root, candidates
