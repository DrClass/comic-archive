from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .importer.commit import initialize_database


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
    issues: list[IssueView] = field(default_factory=list)
    extras: list[GroupView] = field(default_factory=list)

    @property
    def total_pages(self) -> int:
        return sum(
            len(group.media)
            for issue in self.issues
            for group in issue.groups
            if group.role == "primary"
        )

    @property
    def total_extras(self) -> int:
        issue_extras = sum(
            len(group.media)
            for issue in self.issues
            for group in issue.groups
            if group.role != "primary"
        )
        series_extras = sum(len(group.media) for group in self.extras)
        return issue_extras + series_extras


@dataclass(slots=True)
class AuthorView:
    id: str
    name: str
    series: list[SeriesView] = field(default_factory=list)


def _bool_or_none(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def read_library(database_path: str | Path) -> list[AuthorView]:
    database = initialize_database(database_path)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        authors: list[AuthorView] = []
        for author_row in db.execute("SELECT id, name FROM authors ORDER BY name COLLATE NOCASE"):
            author = AuthorView(id=author_row["id"], name=author_row["name"])
            for series_row in db.execute(
                "SELECT id, title, complete, updated_at FROM series WHERE author_id = ? ORDER BY title COLLATE NOCASE",
                (author.id,),
            ):
                series = SeriesView(id=series_row["id"], title=series_row["title"], complete=_bool_or_none(series_row["complete"]), updated_at=series_row["updated_at"])
                for issue_row in db.execute(
                    """SELECT id, issue_number, title, complete, sort_order, updated_at
                       FROM issues WHERE series_id = ?
                       ORDER BY COALESCE(sort_order, 2147483647), COALESCE(issue_number, title, source_key) COLLATE NOCASE""",
                    (series.id,),
                ):
                    issue = IssueView(
                        id=issue_row["id"],
                        issue_number=issue_row["issue_number"],
                        title=issue_row["title"],
                        complete=_bool_or_none(issue_row["complete"]),
                        sort_order=issue_row["sort_order"],
                        updated_at=issue_row["updated_at"],
                    )
                    issue.groups = _read_groups(db, series.id, issue.id)
                    series.issues.append(issue)
                series.extras = _read_groups(db, series.id, None)
                author.series.append(series)
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
