from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .database import connect_database
from .permissions import AccessPolicy


@dataclass(slots=True)
class MediaView:
    id: str
    position: int
    stored_path: str
    original_relative_path: str
    mime_type: str
    media_kind: str
    size_bytes: int
    sha256: str | None


@dataclass(slots=True)
class GroupView:
    id: str
    name: str
    role: str
    relative_path: str
    media: list[MediaView] = field(default_factory=list)


@dataclass(slots=True)
class IssueView:
    id: str
    issue_number: str | None
    title: str | None
    complete: bool | None
    sort_order: int | None = None
    updated_at: str | None = None
    groups: list[GroupView] = field(default_factory=list)


@dataclass(slots=True)
class SeriesView:
    id: str
    title: str
    complete: bool | None = None
    updated_at: str | None = None
    parent_series_id: str | None = None
    sort_order: int | None = None
    children: list["SeriesView"] = field(default_factory=list)
    issues: list[IssueView] = field(default_factory=list)
    extras: list[GroupView] = field(default_factory=list)

    @property
    def total_pages(self) -> int:
        direct = sum(
            len(group.media)
            for issue in self.issues
            for group in issue.groups
            if group.role == "primary"
        )
        return direct + sum(child.total_pages for child in self.children)

    @property
    def total_extras(self) -> int:
        issue_extras = sum(
            len(group.media)
            for issue in self.issues
            for group in issue.groups
            if group.role != "primary"
        )
        series_extras = sum(len(group.media) for group in self.extras)
        return issue_extras + series_extras + sum(child.total_extras for child in self.children)

    @property
    def total_issues(self) -> int:
        return len(self.issues) + sum(child.total_issues for child in self.children)

    @property
    def effective_updated_at(self) -> str | None:
        values = [value for value in [self.updated_at, *(child.effective_updated_at for child in self.children)] if value]
        return max(values) if values else None


@dataclass(slots=True)
class AuthorView:
    id: str
    name: str
    series: list[SeriesView] = field(default_factory=list)

    @property
    def total_series(self) -> int:
        def count(items: list[SeriesView]) -> int:
            return sum(1 + count(item.children) for item in items)
        return count(self.series)


def _bool_or_none(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def read_library(database_path: str | Path, *, access: AccessPolicy | None = None) -> list[AuthorView]:
    with connect_database(database_path, row_factory=True) as db:
        authors: list[AuthorView] = []
        for author_row in db.execute("SELECT id, name FROM authors ORDER BY name COLLATE NOCASE"):
            if access is not None and not access.author(author_row["id"]):
                continue
            author = AuthorView(id=author_row["id"], name=author_row["name"])
            by_id: dict[str, SeriesView] = {}
            ordered: list[SeriesView] = []
            for series_row in db.execute(
                """SELECT id, title, complete, updated_at, parent_series_id, sort_order
                   FROM series WHERE author_id = ?
                   ORDER BY COALESCE(sort_order, 2147483647), title COLLATE NOCASE""",
                (author.id,),
            ):
                if access is not None and not access.series(series_row["id"]):
                    continue
                series = SeriesView(
                    id=series_row["id"], title=series_row["title"],
                    complete=_bool_or_none(series_row["complete"]), updated_at=series_row["updated_at"],
                    parent_series_id=series_row["parent_series_id"], sort_order=series_row["sort_order"],
                )
                for issue_row in db.execute(
                    """SELECT id, issue_number, title, complete, sort_order, updated_at
                       FROM issues WHERE series_id = ?
                       ORDER BY COALESCE(sort_order, 2147483647), COALESCE(issue_number, title, source_key) COLLATE NOCASE""",
                    (series.id,),
                ):
                    if access is not None and not access.issue(issue_row["id"]):
                        continue
                    issue = IssueView(
                        id=issue_row["id"], issue_number=issue_row["issue_number"], title=issue_row["title"],
                        complete=_bool_or_none(issue_row["complete"]), sort_order=issue_row["sort_order"],
                        updated_at=issue_row["updated_at"],
                    )
                    issue.groups = _read_groups(db, series.id, issue.id)
                    series.issues.append(issue)
                series.extras = _read_groups(db, series.id, None)
                by_id[series.id] = series
                ordered.append(series)
            for series in ordered:
                parent = by_id.get(series.parent_series_id) if series.parent_series_id else None
                if parent is None:
                    author.series.append(series)
                else:
                    parent.children.append(series)
            authors.append(author)
        return authors


def _read_groups(db: sqlite3.Connection, series_id: str, issue_id: str | None) -> list[GroupView]:
    if issue_id is None:
        rows = db.execute(
            """SELECT id, name, role, relative_path FROM content_groups
               WHERE series_id = ? AND issue_id IS NULL ORDER BY sort_order, name COLLATE NOCASE""",
            (series_id,),
        )
    else:
        rows = db.execute(
            """SELECT id, name, role, relative_path FROM content_groups
               WHERE series_id = ? AND issue_id = ? ORDER BY sort_order, name COLLATE NOCASE""",
            (series_id, issue_id),
        )
    groups: list[GroupView] = []
    for row in rows:
        group = GroupView(id=row["id"], name=row["name"], role=row["role"], relative_path=row["relative_path"])
        group.media = [
            MediaView(
                id=m["id"],
                position=m["position"],
                stored_path=m["stored_path"],
                original_relative_path=m["original_relative_path"],
                mime_type=m["mime_type"],
                media_kind=m["media_kind"],
                size_bytes=m["size_bytes"],
                sha256=m["sha256"],
            )
            for m in db.execute(
                """SELECT id, position, stored_path, original_relative_path, mime_type,
                          media_kind, size_bytes, sha256
                   FROM media WHERE group_id = ? AND active = 1 ORDER BY position""",
                (row["id"],),
            )
        ]
        groups.append(group)
    return groups
