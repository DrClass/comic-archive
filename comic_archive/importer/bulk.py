from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .scanner import SUPPORTED_MEDIA, FolderScanError


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
    for child in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        count = sum(
            1
            for path in child.rglob("*")
            if path.is_file() and path.suffix.casefold() in SUPPORTED_MEDIA
        )
        if count:
            candidates.append(BulkComicCandidate(path=child.resolve(), name=child.name, media_count=count))
    if not candidates:
        raise FolderScanError("No comic folders containing supported media were found directly inside this artist folder.")
    return root, candidates
