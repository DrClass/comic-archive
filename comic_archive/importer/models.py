from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


class SuggestedRole(str, Enum):
    PRIMARY = "primary"
    EXTRA = "extra"


@dataclass(frozen=True, slots=True)
class ScannedMedia:
    path: Path
    relative_path: Path
    media_kind: MediaKind
    mime_type: str
    size_bytes: int
    order: int


@dataclass(slots=True)
class ScannedGroup:
    name: str
    relative_path: Path
    suggested_role: SuggestedRole
    media: list[ScannedMedia] = field(default_factory=list)
    series_path: Path = Path(".")


@dataclass(slots=True)
class ScannedSeries:
    """A nested series container discovered beneath the import root."""

    name: str
    relative_path: Path
    parent_path: Path = Path(".")


@dataclass(slots=True)
class ScannedIssue:
    """A candidate issue discovered while scanning a series directory."""

    name: str
    relative_path: Path
    primary: ScannedGroup | None
    extras: list[ScannedGroup] = field(default_factory=list)
    series_path: Path = Path(".")

    @property
    def media_count(self) -> int:
        primary_count = len(self.primary.media) if self.primary else 0
        return primary_count + sum(len(group.media) for group in self.extras)


@dataclass(slots=True)
class ScannedImport:
    source: Path
    content_root: Path
    primary: ScannedGroup | None
    extras: list[ScannedGroup]
    issues: list[ScannedIssue] = field(default_factory=list)
    subseries: list[ScannedSeries] = field(default_factory=list)
    ignored_files: list[Path] = field(default_factory=list)

    @property
    def is_series_candidate(self) -> bool:
        return bool(self.issues)

    @property
    def media_count(self) -> int:
        primary_count = len(self.primary.media) if self.primary else 0
        issue_count = sum(issue.media_count for issue in self.issues)
        return primary_count + issue_count + sum(len(group.media) for group in self.extras)
