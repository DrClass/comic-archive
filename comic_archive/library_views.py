from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .database import connect_database
from .library import AuthorView, GroupView, IssueView, MediaView, SeriesView
from .permissions import AccessPolicy


def find_author(authors: Iterable[AuthorView], author_id: str) -> AuthorView | None:
    return next((author for author in authors if author.id == author_id), None)


def walk_series(series_items: Iterable[SeriesView]):
    for series in series_items:
        yield series
        yield from walk_series(series.children)


def walk_issues(series: SeriesView):
    yield from series.issues
    for child in series.children:
        yield from walk_issues(child)


def find_series(authors: Iterable[AuthorView], series_id: str) -> tuple[AuthorView, SeriesView] | None:
    for author in authors:
        for series in walk_series(author.series):
            if series.id == series_id:
                return author, series
    return None


def find_issue(authors: Iterable[AuthorView], issue_id: str) -> tuple[AuthorView, SeriesView, IssueView] | None:
    for author in authors:
        for series in walk_series(author.series):
            for issue in series.issues:
                if issue.id == issue_id:
                    return author, series, issue
    return None


def find_group(authors: Iterable[AuthorView], group_id: str) -> tuple[AuthorView, SeriesView, IssueView | None, GroupView] | None:
    for author in authors:
        for series in walk_series(author.series):
            for group in series.extras:
                if group.id == group_id:
                    return author, series, None, group
            for issue in series.issues:
                for group in issue.groups:
                    if group.id == group_id:
                        return author, series, issue, group
    return None


def primary_group(issue: IssueView) -> GroupView | None:
    return next((group for group in issue.groups if group.role == "primary"), None)


def first_image_media(issue: IssueView) -> MediaView | None:
    primary = primary_group(issue)
    if primary is None:
        return None
    return next((media for media in primary.media if media.mime_type.startswith("image/")), None)


def issue_preview_map(series: SeriesView, *, access: AccessPolicy | None = None) -> dict[str, MediaView]:
    previews: dict[str, MediaView] = {}
    for issue in series.issues:
        if access is not None and not access.issue(issue.id):
            continue
        media = first_image_media(issue)
        if media is not None:
            previews[issue.id] = media
    return previews


def series_preview_map(author: AuthorView, *, access: AccessPolicy | None = None) -> dict[str, MediaView]:
    previews: dict[str, MediaView] = {}

    def first_preview(series: SeriesView) -> MediaView | None:
        if access is not None and not access.series(series.id):
            return None
        for issue in series.issues:
            if access is not None and not access.issue(issue.id):
                continue
            media = first_image_media(issue)
            if media is not None:
                return media
        for child in series.children:
            media = first_preview(child)
            if media is not None:
                return media
        return None

    for series in walk_series(author.series):
        media = first_preview(series)
        if media is not None:
            previews[series.id] = media
    return previews


def series_issue_ids(series: SeriesView) -> list[str]:
    ids = [issue.id for issue in series.issues]
    for child in series.children:
        ids.extend(series_issue_ids(child))
    return ids


def series_reading_status(series: SeriesView, progress: dict[str, object]) -> str:
    issue_ids = series_issue_ids(series)
    if not issue_ids:
        return "unread"
    saved = [progress.get(issue_id) for issue_id in issue_ids]
    if all(item is not None and item.completed for item in saved):
        return "finished"
    if all(item is None for item in saved):
        return "unread"
    return "in-progress"


def series_status_map(author: AuthorView, progress: dict[str, object]) -> dict[str, str]:
    return {series.id: series_reading_status(series, progress) for series in walk_series(author.series)}


def author_preview_map(authors: list[AuthorView], *, access: AccessPolicy | None = None) -> dict[str, MediaView]:
    previews: dict[str, MediaView] = {}
    for author in authors:
        series_previews = series_preview_map(author, access=access)
        for series in author.series:
            media = series_previews.get(series.id)
            if media is not None:
                previews[author.id] = media
                break
    return previews


def series_lineage(author: AuthorView, series: SeriesView) -> list[SeriesView]:
    by_id = {item.id: item for item in walk_series(author.series)}
    lineage: list[SeriesView] = []
    current = series
    seen: set[str] = set()
    while current.parent_series_id and current.parent_series_id in by_id and current.id not in seen:
        seen.add(current.id)
        current = by_id[current.parent_series_id]
        lineage.append(current)
    lineage.reverse()
    return lineage


def search_library(database: Path, query: str, *, limit: int = 100, access: AccessPolicy | None = None, include_restricted: bool = False) -> list[dict[str, object]]:
    query = query.strip()
    if not query:
        return []

    pattern = f"%{query}%"
    results: list[dict[str, object]] = []

    with connect_database(database, row_factory=True) as db:
        if access is not None and not include_restricted:
            access.register_sql(db)
        else:
            for name, count in (("can_view_author", 1), ("can_view_series", 1), ("can_view_issue", 1), ("can_view_group", 2)):
                db.create_function(name, count, lambda *args: True)
        for row in db.execute(
            """SELECT id, name
               FROM authors
               WHERE name LIKE ? COLLATE NOCASE AND can_view_author(id)
               ORDER BY CASE WHEN name = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                        name COLLATE NOCASE
               LIMIT ?""",
            (pattern, query, limit),
        ):
            results.append({"type": "Author", "title": row["name"], "context": None, "url": f"/authors/{row['id']}"})

        for row in db.execute(
            """SELECT s.id, s.title, a.name AS author_name
               FROM series s
               JOIN authors a ON a.id = s.author_id
               WHERE s.title LIKE ? COLLATE NOCASE AND can_view_series(s.id)
               ORDER BY CASE WHEN s.title = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                        a.name COLLATE NOCASE, s.title COLLATE NOCASE
               LIMIT ?""",
            (pattern, query, limit),
        ):
            results.append({"type": "Series", "title": row["title"], "context": row["author_name"], "url": f"/series/{row['id']}", "restricted": access is not None and not access.series(row["id"])})

        for row in db.execute(
            """SELECT i.id, i.issue_number, i.title AS issue_title,
                      s.title AS series_title, a.name AS author_name
               FROM issues i
               JOIN series s ON s.id = i.series_id
               JOIN authors a ON a.id = s.author_id
               WHERE (COALESCE(i.issue_number, '') LIKE ? COLLATE NOCASE
                  OR COALESCE(i.title, '') LIKE ? COLLATE NOCASE)
                 AND can_view_issue(i.id)
               ORDER BY a.name COLLATE NOCASE, s.title COLLATE NOCASE,
                        COALESCE(i.issue_number, i.title, i.source_key) COLLATE NOCASE
               LIMIT ?""",
            (pattern, pattern, limit),
        ):
            if row["issue_number"]:
                title = f"Issue {row['issue_number']}"
                if row["issue_title"]:
                    title += f" — {row['issue_title']}"
            elif row["issue_title"]:
                title = row["issue_title"]
            else:
                title = "One-shot"
            results.append({
                "type": "Issue",
                "title": title,
                "context": f"{row['author_name']} / {row['series_title']}",
                "url": f"/issues/{row['id']}",
                "restricted": access is not None and not access.issue(row["id"]),
            })

        for row in db.execute(
            """SELECT g.id, g.name, g.issue_id, g.role, g.series_id,
                      s.title AS series_title, a.name AS author_name,
                      i.issue_number, i.title AS issue_title
               FROM content_groups g
               JOIN series s ON s.id = g.series_id
               JOIN authors a ON a.id = s.author_id
               LEFT JOIN issues i ON i.id = g.issue_id
               WHERE g.role != 'primary'
                 AND can_view_group(g.series_id, g.issue_id)
                 AND g.name LIKE ? COLLATE NOCASE
               ORDER BY a.name COLLATE NOCASE, s.title COLLATE NOCASE, g.name COLLATE NOCASE
               LIMIT ?""",
            (pattern, limit),
        ):
            context = f"{row['author_name']} / {row['series_title']}"
            if row["issue_id"]:
                issue_label = row["issue_number"] or row["issue_title"] or "One-shot"
                context += f" / {issue_label}"
            results.append({"type": "Extra", "title": row["name"], "context": context, "url": f"/groups/{row['id']}", "restricted": access is not None and not access.group(row["series_id"], row["issue_id"])})

    qfold = query.casefold()
    type_rank = {"Author": 0, "Series": 1, "Issue": 2, "Extra": 3}
    results.sort(key=lambda item: (
        0 if str(item["title"]).casefold() == qfold else 1,
        type_rank.get(str(item["type"]), 9),
        str(item["title"]).casefold(),
        str(item.get("context") or "").casefold(),
    ))
    return results[:limit]
