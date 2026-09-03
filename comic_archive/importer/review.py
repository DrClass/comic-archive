from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .models import ScannedGroup, ScannedImport, ScannedIssue


class ReviewRole(str, Enum):
    ISSUE = "issue"
    PRIMARY = "primary"
    ISSUE_EXTRA = "issue-extra"
    SERIES_EXTRA = "series-extra"


class ReviewError(ValueError):
    pass


@dataclass(slots=True)
class ReviewItem:
    """One user-correctable classification produced by the scanner.

    ``relative_path`` is the stable identifier used by the CLI/web review UI.
    The review stage changes classifications only; it never modifies source files.
    """

    relative_path: Path
    name: str
    default_role: ReviewRole
    role: ReviewRole
    media_count: int
    default_name: str | None = None
    issue_path: Path | None = None
    source_kind: str = "group"

    def __post_init__(self) -> None:
        if self.default_name is None:
            self.default_name = self.name

    @property
    def changed(self) -> bool:
        return self.role is not self.default_role or self.name != self.default_name


@dataclass(slots=True)
class ReviewPlan:
    scan: ScannedImport
    items: list[ReviewItem] = field(default_factory=list)

    def find(self, path: str | Path) -> ReviewItem:
        key = _normalise_relative(path)
        matches = [item for item in self.items if item.relative_path == key]
        if not matches:
            raise ReviewError(f"No review item at path: {key}")
        if len(matches) > 1:
            raise ReviewError(f"Ambiguous review path: {key}")
        return matches[0]

    def set_role(self, path: str | Path, role: ReviewRole | str) -> ReviewItem:
        item = self.find(path)
        if isinstance(role, str):
            try:
                role = ReviewRole(role.casefold())
            except ValueError as exc:
                choices = ", ".join(value.value for value in ReviewRole)
                raise ReviewError(f"Unknown role {role!r}; choose one of: {choices}") from exc

        _validate_role_for_item(item, role)
        item.role = role
        return item

    def set_name(self, path: str | Path, name: str) -> ReviewItem:
        item = self.find(path)
        value = name.strip()
        if not value:
            raise ReviewError("Content group name cannot be empty")
        item.name = value
        return item

    def reset_role(self, path: str | Path) -> ReviewItem:
        item = self.find(path)
        item.role = item.default_role
        return item

    def reset(self, path: str | Path) -> ReviewItem:
        item = self.find(path)
        item.role = item.default_role
        item.name = item.default_name or item.name
        return item

    def validation_errors(self) -> list[str]:
        errors: list[str] = []

        if self.scan.is_series_candidate:
            issue_count = sum(item.role is ReviewRole.ISSUE for item in self.items if item.source_kind == "issue")
            issue_count += sum(
                item.role is ReviewRole.ISSUE for item in self.items if item.source_kind == "group"
            )
            if issue_count == 0:
                errors.append("Series review has no folders classified as issues.")

        for item in self.items:
            if item.role is ReviewRole.ISSUE_EXTRA and item.issue_path is None:
                errors.append(
                    f"{item.relative_path}: issue-extra needs an issue owner. "
                    "Assign it to an issue in the future web review UI or classify it as series-extra."
                )
            if item.role is ReviewRole.PRIMARY and self.scan.is_series_candidate and item.issue_path is None:
                errors.append(f"{item.relative_path}: primary content in a series needs an issue owner.")

        return errors

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors()


def _normalise_relative(path: str | Path) -> Path:
    value = Path(path)
    if value == Path(""):
        return Path(".")
    if value.is_absolute():
        raise ReviewError("Review paths must be relative to the scanned content root")
    return value


def _group_media_count(group: ScannedGroup | None) -> int:
    return len(group.media) if group else 0


def _issue_media_count(issue: ScannedIssue) -> int:
    return _group_media_count(issue.primary) + sum(len(group.media) for group in issue.extras)


def _validate_role_for_item(item: ReviewItem, role: ReviewRole) -> None:
    if item.source_kind == "issue" and role in {ReviewRole.PRIMARY, ReviewRole.ISSUE_EXTRA}:
        raise ReviewError(
            f"{item.relative_path} is an issue container; it can be classified as "
            f"'{ReviewRole.ISSUE.value}' or '{ReviewRole.SERIES_EXTRA.value}', not '{role.value}'."
        )



@dataclass(frozen=True, slots=True)
class FlattenedFolderCandidate:
    """A source folder whose media is currently folded into primary content."""

    relative_path: Path
    display_path: Path
    media_count: int


def flattened_folder_candidates(scan: ScannedImport) -> list[FlattenedFolderCandidate]:
    """Return media-bearing folders currently flattened into primary groups.

    Paths are relative to ``scan.source`` so they can be passed directly back to
    ``scan_folder(extra_folders=...)`` before staging.
    """
    counts: dict[Path, int] = {}
    content_prefix = scan.content_root.relative_to(scan.source)

    def add_media(base: Path, media_items) -> None:
        for media in media_items:
            parent = media.relative_path.parent
            while parent != Path('.'):
                full = base / parent
                counts[full] = counts.get(full, 0) + 1
                parent = parent.parent

    if scan.is_series_candidate:
        for issue in scan.issues:
            if issue.primary is not None:
                add_media(issue.relative_path, issue.primary.media)
    elif scan.primary is not None:
        add_media(Path('.'), scan.primary.media)

    candidates: list[FlattenedFolderCandidate] = []
    for display_path, count in counts.items():
        source_path = content_prefix / display_path
        candidates.append(FlattenedFolderCandidate(source_path, display_path, count))
    candidates.sort(key=lambda item: tuple(part.casefold() for part in item.display_path.parts))
    return candidates

def build_review_plan(scan: ScannedImport) -> ReviewPlan:
    items: list[ReviewItem] = []

    if scan.is_series_candidate:
        for issue in scan.issues:
            items.append(
                ReviewItem(
                    relative_path=issue.relative_path,
                    name=issue.name,
                    default_role=ReviewRole.ISSUE,
                    role=ReviewRole.ISSUE,
                    media_count=_issue_media_count(issue),
                    source_kind="issue",
                )
            )
            for extra in issue.extras:
                items.append(
                    ReviewItem(
                        relative_path=extra.relative_path,
                        name=extra.name,
                        default_role=ReviewRole.ISSUE_EXTRA,
                        role=ReviewRole.ISSUE_EXTRA,
                        media_count=len(extra.media),
                        issue_path=issue.relative_path,
                        source_kind="group",
                    )
                )

        for extra in scan.extras:
            items.append(
                ReviewItem(
                    relative_path=extra.relative_path,
                    name=extra.name,
                    default_role=ReviewRole.SERIES_EXTRA,
                    role=ReviewRole.SERIES_EXTRA,
                    media_count=len(extra.media),
                    source_kind="group",
                )
            )
    else:
        if scan.primary is not None:
            items.append(
                ReviewItem(
                    relative_path=Path("."),
                    name=scan.content_root.name,
                    default_role=ReviewRole.PRIMARY,
                    role=ReviewRole.PRIMARY,
                    media_count=len(scan.primary.media),
                    source_kind="group",
                )
            )
        for extra in scan.extras:
            items.append(
                ReviewItem(
                    relative_path=extra.relative_path,
                    name=extra.name,
                    default_role=ReviewRole.ISSUE_EXTRA,
                    role=ReviewRole.ISSUE_EXTRA,
                    media_count=len(extra.media),
                    issue_path=Path("."),
                    source_kind="group",
                )
            )

    return ReviewPlan(scan=scan, items=items)
