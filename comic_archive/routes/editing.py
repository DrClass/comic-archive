from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import connect_database
from ..editing import (
    EditError, create_issue_extra_group, delete_series, edit_issue, edit_series,
    get_history, move_extra_group, move_media_to_group, rename_author, rename_group,
    reorder_issues, reorder_media, set_media_active,
)
from ..library import read_library
from ..library_views import (
    find_author, find_group, find_issue, find_series, primary_group, walk_series,
)
from ..web_forms import form_data, bool_form


def _all_media_for_group(database: Path, group_id: str) -> list[dict[str, object]]:
    with connect_database(database, row_factory=True) as db:
        rows = db.execute(
            """SELECT id, position, original_relative_path, mime_type, media_kind, size_bytes, active
               FROM media WHERE group_id = ? ORDER BY active DESC, position, id""",
            (group_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _author_choices(database: Path) -> list[dict[str, str]]:
    with connect_database(database, row_factory=True) as db:
        return [dict(row) for row in db.execute("SELECT id, name FROM authors ORDER BY name COLLATE NOCASE")]


def _series_choices(database: Path) -> list[dict[str, str]]:
    with connect_database(database, row_factory=True) as db:
        return [dict(row) for row in db.execute(
            """SELECT s.id, s.title, a.name AS author_name
               FROM series s JOIN authors a ON a.id = s.author_id
               ORDER BY a.name COLLATE NOCASE, s.title COLLATE NOCASE"""
        )]


def _issues_for_series(database: Path, series_id: str) -> list[dict[str, object]]:
    with connect_database(database, row_factory=True) as db:
        rows = db.execute(
            """SELECT id, issue_number, title, sort_order FROM issues WHERE series_id = ?
               ORDER BY COALESCE(sort_order, 2147483647), COALESCE(issue_number, title, source_key) COLLATE NOCASE""",
            (series_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def register_editing_routes(app: FastAPI, templates: Jinja2Templates, database: Path, library: Path) -> None:
    @app.get("/authors/{author_id}/edit", response_class=HTMLResponse)
    def author_edit_page(request: Request, author_id: str):
        author = find_author(read_library(database), author_id)
        if author is None:
            raise HTTPException(status_code=404, detail="Author not found")
        return templates.TemplateResponse(request=request, name="edit_author.html", context={"author": author, "error": None})

    @app.post("/authors/{author_id}/edit", response_class=HTMLResponse)
    async def author_edit_save(request: Request, author_id: str):
        author = find_author(read_library(database), author_id)
        if author is None:
            raise HTTPException(status_code=404, detail="Author not found")
        form = await form_data(request)
        try:
            rename_author(database, author_id, form.get("name", ""))
        except EditError as exc:
            return templates.TemplateResponse(request=request, name="edit_author.html", context={"author": author, "error": str(exc)}, status_code=400)
        return RedirectResponse(f"/authors/{author_id}", status_code=303)

    @app.get("/series/{series_id}/edit", response_class=HTMLResponse)
    def series_edit_page(request: Request, series_id: str):
        found = find_series(read_library(database), series_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Series not found")
        author, series = found
        return templates.TemplateResponse(request=request, name="edit_series.html", context={
            "author": author, "series": series, "authors": _author_choices(database),
            "parent_choices": [choice for choice in _series_choices(database) if choice["author_name"] == author.name and choice["id"] != series.id],
            "error": None,
        })

    @app.post("/series/{series_id}/edit", response_class=HTMLResponse)
    async def series_edit_save(request: Request, series_id: str):
        found = find_series(read_library(database), series_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Series not found")
        author, series = found
        form = await form_data(request)
        try:
            edit_series(database, series_id, title=form.get("title", ""), author_id=form.get("author_id", author.id),
                        complete=bool_form(form.get("complete", "")), parent_series_id=form.get("parent_series_id", "") or None)
            if series.issues:
                ranked_issues: list[tuple[int, int, str]] = []
                for index, issue in enumerate(series.issues):
                    raw_order = form.get(f"issue_order_{issue.id}", str(index + 1)).strip()
                    try:
                        order_value = int(raw_order)
                    except ValueError as exc:
                        raise EditError("Issue order must use whole numbers") from exc
                    ranked_issues.append((order_value, index, issue.id))
                ranked_issues.sort()
                reorder_issues(database, series_id, [issue_id for _, _, issue_id in ranked_issues])
        except EditError as exc:
            return templates.TemplateResponse(request=request, name="edit_series.html", context={
                "author": author, "series": series, "authors": _author_choices(database),
                "parent_choices": [choice for choice in _series_choices(database) if choice["author_name"] == author.name and choice["id"] != series.id],
                "error": str(exc),
            }, status_code=400)
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/delete", response_class=HTMLResponse)
    async def series_delete(request: Request, series_id: str):
        form = await form_data(request)
        authors = read_library(database)
        found = find_series(authors, series_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Series not found")
        author, series = found
        try:
            author_id, parent_id = delete_series(database, library, series_id, confirmation=form.get("confirmation", ""))
        except EditError as exc:
            parent_choices = [item for item in walk_series(author.series) if item.id != series.id]
            return templates.TemplateResponse(request=request, name="edit_series.html", context={
                "author": author, "series": series, "authors": authors, "parent_choices": parent_choices, "error": str(exc)
            }, status_code=400)
        return RedirectResponse(f"/series/{parent_id}" if parent_id else f"/authors/{author_id}", status_code=303)

    @app.get("/issues/{issue_id}/edit", response_class=HTMLResponse)
    def issue_edit_page(request: Request, issue_id: str):
        found = find_issue(read_library(database), issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        return templates.TemplateResponse(request=request, name="edit_issue.html", context={
            "author": author, "series": series, "issue": issue, "primary": primary_group(issue),
            "series_choices": _series_choices(database), "error": None,
        })

    @app.post("/issues/{issue_id}/edit", response_class=HTMLResponse)
    async def issue_edit_save(request: Request, issue_id: str):
        found = find_issue(read_library(database), issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        form = await form_data(request)
        try:
            edit_issue(database, issue_id, issue_number=form.get("issue_number", "").strip() or None,
                       title=form.get("title", "").strip() or None, complete=bool_form(form.get("complete", "")),
                       series_id=form.get("series_id", series.id))
        except EditError as exc:
            return templates.TemplateResponse(request=request, name="edit_issue.html", context={
                "author": author, "series": series, "issue": issue, "primary": primary_group(issue),
                "series_choices": _series_choices(database), "error": str(exc),
            }, status_code=400)
        refreshed = find_issue(read_library(database), issue_id)
        return RedirectResponse(f"/issues/{issue_id}" if refreshed else "/", status_code=303)

    @app.post("/issues/{issue_id}/groups")
    async def issue_group_create(request: Request, issue_id: str):
        form = await form_data(request)
        try:
            create_issue_extra_group(database, issue_id, form.get("name", ""))
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/issues/{issue_id}/edit", status_code=303)

    @app.post("/groups/{group_id}/move-media")
    async def group_move_media(request: Request, group_id: str):
        found = find_group(read_library(database), group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        _, _, issue, _ = found
        if issue is None:
            raise HTTPException(status_code=400, detail="Series extras cannot move individual media into issue groups")
        form = await form_data(request)
        selected = [media_id for key, media_id in ((key, key.removeprefix("move_")) for key in form)
                    if key.startswith("move_") and form.get(key) == "yes"]
        try:
            move_media_to_group(database, selected, form.get("target_group_id", ""))
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.get("/groups/{group_id}/edit", response_class=HTMLResponse)
    def group_edit_page(request: Request, group_id: str):
        found = find_group(read_library(database), group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        author, series, issue, group = found
        return templates.TemplateResponse(request=request, name="edit_group.html", context={
            "author": author, "series": series, "issue": issue, "group": group,
            "series_choices": _series_choices(database), "target_issues": _issues_for_series(database, series.id),
            "all_media": _all_media_for_group(database, group.id), "issue_groups": issue.groups if issue else [], "error": None,
        })

    @app.post("/groups/{group_id}/edit", response_class=HTMLResponse)
    async def group_edit_save(request: Request, group_id: str):
        found = find_group(read_library(database), group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        author, series, issue, group = found
        form = await form_data(request)
        try:
            if group.role != "primary":
                rename_group(database, group_id, form.get("name", group.name))
                target = form.get("owner", "series")
                if target == "series":
                    move_extra_group(database, group_id, series_id=series.id, issue_id=None)
                elif target.startswith("issue:"):
                    move_extra_group(database, group_id, series_id=series.id, issue_id=target.split(":", 1)[1])
            active_media = [m for m in _all_media_for_group(database, group_id) if m["active"]]
            if active_media:
                ordered = sorted(active_media, key=lambda m: (
                    int(form.get(f"position_{m['id']}", m["position"])) if str(form.get(f"position_{m['id']}", m["position"])).isdigit() else int(m["position"]),
                    int(m["position"]),
                ))
                reorder_media(database, group_id, [str(m["id"]) for m in ordered])
        except (EditError, ValueError) as exc:
            return templates.TemplateResponse(request=request, name="edit_group.html", context={
                "author": author, "series": series, "issue": issue, "group": group,
                "series_choices": _series_choices(database), "target_issues": _issues_for_series(database, series.id),
                "all_media": _all_media_for_group(database, group.id), "issue_groups": issue.groups if issue else [], "error": str(exc),
            }, status_code=400)
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.post("/media/{media_id}/remove")
    async def media_remove(request: Request, media_id: str, group_id: str):
        await form_data(request)
        try:
            set_media_active(database, media_id, False)
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.post("/media/{media_id}/restore")
    async def media_restore(request: Request, media_id: str, group_id: str):
        await form_data(request)
        try:
            set_media_active(database, media_id, True)
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request):
        return templates.TemplateResponse(request=request, name="history.html", context={"history": get_history(database)})
