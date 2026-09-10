from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..library import AuthorView, GroupView, IssueView, SeriesView, read_library
from ..library_views import (
    author_preview_map,
    find_author,
    find_group,
    find_issue,
    find_series,
    issue_preview_map,
    primary_group,
    search_library,
    series_issue_ids,
    series_lineage,
    series_preview_map,
    series_status_map,
    walk_issues,
)
from ..maintenance import series_gaps
from ..progress import get_continue_reading, get_progress, get_progress_map, reset_progress, save_progress
from ..web_forms import form_data


def register_library_routes(app: FastAPI, templates: Jinja2Templates, database: Path) -> None:
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        authors = read_library(database)
        series_count = sum(author.total_series for author in authors)
        issue_count = sum(series.total_issues for author in authors for series in author.series)
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={
                "authors": authors,
                "series_count": series_count,
                "issue_count": issue_count,
                "author_previews": author_preview_map(authors),
                "continue_reading": get_continue_reading(database, request.state.user.id),
            },
        )

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = ""):
        query = q.strip()
        results = search_library(database, query) if query else []
        return templates.TemplateResponse(request=request, name="search.html", context={"query": query, "results": results})

    @app.get("/authors/{author_id}", response_class=HTMLResponse)
    def author_page(request: Request, author_id: str):
        authors = read_library(database)
        author = find_author(authors, author_id)
        if author is None:
            raise HTTPException(status_code=404, detail="Author not found")
        issue_ids = [issue.id for series in author.series for issue in walk_issues(series)]
        progress = get_progress_map(database, request.state.user.id, issue_ids)
        return templates.TemplateResponse(
            request=request,
            name="author.html",
            context={
                "author": author,
                "series_previews": series_preview_map(author),
                "series_status": series_status_map(author, progress),
            },
        )

    @app.get("/series/{series_id}", response_class=HTMLResponse)
    def series_page(request: Request, series_id: str):
        authors = read_library(database)
        found = find_series(authors, series_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Series not found")
        author, series = found
        progress = get_progress_map(database, request.state.user.id, series_issue_ids(series))
        return templates.TemplateResponse(
            request=request,
            name="series.html",
            context={
                "author": author,
                "series": series,
                "issue_previews": issue_preview_map(series),
                "series_previews": series_preview_map(author),
                "lineage": series_lineage(author, series),
                "progress": progress,
                "series_status": series_status_map(author, progress),
                "missing_gaps": series_gaps(database, series.id),
            },
        )

    @app.get("/issues/{issue_id}", response_class=HTMLResponse)
    def issue_page(request: Request, issue_id: str):
        authors = read_library(database)
        found = find_issue(authors, issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        return templates.TemplateResponse(
            request=request,
            name="issue.html",
            context={
                "author": author,
                "series": series,
                "lineage": series_lineage(author, series),
                "issue": issue,
                "primary": primary_group(issue),
                "extras": [group for group in issue.groups if group.role != "primary"],
                "reading_progress": get_progress(database, request.state.user.id, issue.id),
            },
        )

    @app.get("/groups/{group_id}", response_class=HTMLResponse)
    def group_page(request: Request, group_id: str):
        authors = read_library(database)
        found = find_group(authors, group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        author, series, issue, group = found
        if group.role == "primary" and issue is not None:
            return RedirectResponse(f"/issues/{issue.id}", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="group.html",
            context={"author": author, "series": series, "issue": issue, "group": group},
        )

    def reader_response(
        request: Request,
        *,
        author: AuthorView,
        series: SeriesView,
        issue: IssueView | None,
        group: GroupView,
        page: int,
    ):
        if not group.media:
            raise HTTPException(status_code=404, detail="Content group has no readable media")
        page_index = max(0, min(page - 1, len(group.media) - 1))
        if group.role != "primary":
            back_url, back_label, reader_title = f"/groups/{group.id}", "Extra details", group.name
        elif issue is None:
            back_url, back_label, reader_title = f"/series/{series.id}", "Series details", group.name
        else:
            back_url, back_label = f"/issues/{issue.id}", "Issue details"
            reader_title = issue.title or (f"Issue {issue.issue_number}" if issue.issue_number else series.title)
        track_progress = issue is not None and group.role == "primary"
        if track_progress:
            save_progress(database, request.state.user.id, issue.id, page_index + 1, len(group.media))
        return templates.TemplateResponse(
            request=request,
            name="reader.html",
            context={
                "author": author,
                "series": series,
                "issue": issue,
                "group": group,
                "media": group.media,
                "page_index": page_index,
                "back_url": back_url,
                "back_label": back_label,
                "reader_title": reader_title,
                "track_progress": track_progress,
            },
        )

    @app.get("/read/{issue_id}", response_class=HTMLResponse)
    def reader(request: Request, issue_id: str, page: int | None = None):
        authors = read_library(database)
        found = find_issue(authors, issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        group = primary_group(issue)
        if group is None:
            raise HTTPException(status_code=404, detail="Issue has no readable primary media")
        if page is None:
            saved = get_progress(database, request.state.user.id, issue.id)
            page = saved.page if saved is not None and not saved.completed else 1
        return reader_response(request, author=author, series=series, issue=issue, group=group, page=page)

    @app.get("/read-group/{group_id}", response_class=HTMLResponse)
    def group_reader(request: Request, group_id: str, page: int = 1):
        authors = read_library(database)
        found = find_group(authors, group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        author, series, issue, group = found
        return reader_response(request, author=author, series=series, issue=issue, group=group, page=page)

    @app.post("/progress/{issue_id}")
    async def progress_save(request: Request, issue_id: str):
        form = await form_data(request)
        try:
            page = int(form.get("page", "1"))
            total_pages = int(form.get("total_pages", "1"))
            progress = save_progress(database, request.state.user.id, issue_id, page, total_pages)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"page": progress.page, "completed": progress.completed}

    @app.post("/progress/{issue_id}/reset")
    async def progress_reset(request: Request, issue_id: str):
        await form_data(request)
        reset_progress(database, request.state.user.id, issue_id)
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)
