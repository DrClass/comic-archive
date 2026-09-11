from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..importer.commit import CommitError
from ..importer.review import ReviewRole
from ..importer.scanner import FolderScanError
from ..importer.staging import StagingError


@dataclass(frozen=True)
class ImportFinalizeRouteDeps:
    staging_root: Path
    library_root: Path
    database_path: Path
    templates: Any
    form_data: Callable[[Request], Awaitable[dict[str, str]]]
    bool_form: Callable[[str], bool | None]
    session_or_404: Callable[[FastAPI, str], Any]
    bulk_session_or_404: Callable[[FastAPI, str], Any]
    touch_import_activity: Callable[[FastAPI, Any], None]
    touch_bulk_activity: Callable[[FastAPI, Any], None]
    load_completed_import: Callable[[FastAPI, str], Any]
    save_completed_import: Callable[[FastAPI, str, dict[str, Any]], None]
    delete_import_session_state: Callable[[FastAPI, str], None]
    delete_bulk_session_state: Callable[[FastAPI, str], None]
    remove_staging_record: Callable[[Any], None]
    cleanup_upload: Callable[[Path | None], None]
    start_bulk_item: Callable[[FastAPI, str], str | None]
    workspace_sorted_review_items: Callable[..., list[Any]]
    build_staged_import: Callable[..., Any]
    move_staged_media: Callable[..., None]
    create_staged_issue_extra: Callable[..., None]
    remove_empty_staged_group: Callable[..., None]
    existing_commit_result: Callable[[Path, Any], dict[str, Any] | None]
    commit_staged_import: Callable[..., Any]
    log_memory_checkpoint: Callable[..., None]
    logger: logging.Logger


def register_import_finalize_routes(app: FastAPI, deps: ImportFinalizeRouteDeps) -> None:
    staging = deps.staging_root
    library = deps.library_root
    database = deps.database_path
    templates = deps.templates
    logger = deps.logger

    @app.get("/import/{session_id}/metadata", response_class=HTMLResponse)
    def import_metadata(request: Request, session_id: str):
        if deps.load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)
        session = deps.session_or_404(app, session_id)
        deps.touch_import_activity(app, session)
        if session.plan.scan.is_series_candidate:
            issue_items = deps.workspace_sorted_review_items(session, ReviewRole.ISSUE)
            subseries_items = deps.workspace_sorted_review_items(session, ReviewRole.SUBSERIES)
        else:
            issue_items = [None]
            subseries_items = []
        return templates.TemplateResponse(
            request=request,
            name="import_metadata.html",
            context={
                "session_id": session_id,
                "plan": session.plan,
                "issue_items": issue_items,
                "subseries_items": subseries_items,
                "series_default": session.series_default or session.plan.scan.content_root.name,
                "author_value": session.author_default or "",
                "series_value": session.series_default or "",
                "series_complete_value": "",
                "error": None,
            },
        )

    @app.post("/import/{session_id}/metadata", response_class=HTMLResponse)
    async def import_metadata_save(request: Request, session_id: str):
        session = deps.session_or_404(app, session_id)
        form = await deps.form_data(request)
        deps.touch_import_activity(app, session)
        author = form.get("author", "").strip()
        series = form.get("series", "").strip()
        issue_metadata: dict[str, dict[str, object]] = {}
        subseries_metadata: dict[str, dict[str, object]] = {}
        if session.plan.scan.is_series_candidate:
            issue_items = deps.workspace_sorted_review_items(session, ReviewRole.ISSUE)
            subseries_items = deps.workspace_sorted_review_items(session, ReviewRole.SUBSERIES)
            for index, item in enumerate(subseries_items):
                subseries_metadata[str(item.relative_path)] = {
                    "complete": form.get(f"subseries_complete_{index}", ""),
                    "sort_order": form.get(f"subseries_order_{index}", str(index + 1)),
                }
            for index, item in enumerate(issue_items):
                issue_metadata[str(item.relative_path)] = {
                    "issue_number": form.get(f"issue_number_{index}", ""),
                    "title": form.get(f"title_{index}", ""),
                    "complete": form.get(f"complete_{index}", ""),
                    "sort_order": form.get(f"sort_order_{index}", str(index + 1)),
                }
        else:
            issue_items = [None]
            subseries_items = []
            issue_metadata["."] = {
                "issue_number": form.get("issue_number_0", ""),
                "title": form.get("title_0", ""),
                "complete": form.get("complete_0", ""),
                "sort_order": form.get("sort_order_0", "1"),
            }
        try:
            staged = deps.build_staged_import(
                session.plan,
                author=author,
                series=series,
                issue_metadata=issue_metadata,
                subseries_metadata=subseries_metadata,
                series_complete=deps.bool_form(form.get("series_complete", "")),
            )
            staging.mkdir(parents=True, exist_ok=True)
            staging_path = staged.save(staging / f"{staged.staging_id}.json")
            session.staged = staged
            session.staging_path = staging_path
        except StagingError as exc:
            return templates.TemplateResponse(
                request=request,
                name="import_metadata.html",
                context={
                    "session_id": session_id,
                    "plan": session.plan,
                    "issue_items": issue_items,
                    "subseries_items": subseries_items,
                    "series_default": series or session.plan.scan.content_root.name,
                    "error": str(exc),
                    "author_value": author,
                    "series_value": series,
                    "series_complete_value": form.get("series_complete", ""),
                },
                status_code=400,
            )
        return RedirectResponse(f"/import/{session_id}/organize", status_code=303)

    @app.get("/import/{session_id}/organize", response_class=HTMLResponse)
    def import_organize(request: Request, session_id: str):
        if deps.load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)
        session = deps.session_or_404(app, session_id)
        deps.touch_import_activity(app, session)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="import_organize.html",
            context={"session_id": session_id, "staged": session.staged, "error": None},
        )

    @app.post("/import/{session_id}/organize", response_class=HTMLResponse)
    async def import_organize_save(request: Request, session_id: str):
        session = deps.session_or_404(app, session_id)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        form = await deps.form_data(request)
        deps.touch_import_activity(app, session)
        action = form.get("action", "continue")

        def save_assignments() -> None:
            assignments: list[tuple[str, str, str]] = []
            for issue_index, issue in enumerate(session.staged.issues):
                media_index = 0
                for group in issue.groups:
                    for media in list(group.media):
                        target_path = form.get(
                            f"target_{issue_index}_{media_index}",
                            group.relative_path,
                        )
                        assignments.append((issue.source_key, media.source_path, target_path))
                        media_index += 1
            for issue_key, source_path, target_path in assignments:
                deps.move_staged_media(session.staged, issue_key, source_path, target_path)

        try:
            save_assignments()
            if action.startswith("create:"):
                issue_index = int(action.split(":", 1)[1])
                issue = session.staged.issues[issue_index]
                deps.create_staged_issue_extra(
                    session.staged,
                    issue.source_key,
                    form.get(f"new_group_{issue_index}", ""),
                )
            elif action.startswith("remove-empty:"):
                _, issue_text, group_text = action.split(":", 2)
                issue = session.staged.issues[int(issue_text)]
                group = issue.groups[int(group_text)]
                deps.remove_empty_staged_group(session.staged, issue.source_key, group.relative_path)
            else:
                empty = [
                    group.name
                    for issue in session.staged.issues
                    for group in issue.groups
                    if not group.media
                ]
                if empty:
                    names = ", ".join(empty)
                    raise StagingError(
                        f"Empty content group{'s' if len(empty) != 1 else ''}: {names}. "
                        "Move files into the group, remove the empty extra group, or continue editing before import."
                    )
        except (StagingError, ValueError, IndexError) as exc:
            if session.staging_path is not None:
                session.staged.save(session.staging_path)
            return templates.TemplateResponse(
                request=request,
                name="import_organize.html",
                context={"session_id": session_id, "staged": session.staged, "error": str(exc)},
                status_code=400,
            )

        if session.staging_path is not None:
            session.staged.save(session.staging_path)
        if action.startswith("create:") or action.startswith("remove-empty:"):
            return RedirectResponse(f"/import/{session_id}/organize", status_code=303)
        return RedirectResponse(f"/import/{session_id}/confirm", status_code=303)

    @app.post("/import/{session_id}/keepalive")
    async def import_keepalive(request: Request, session_id: str):
        session = deps.session_or_404(app, session_id)
        await deps.form_data(request)
        deps.touch_import_activity(app, session)
        return Response(status_code=204)

    @app.post("/import/bulk/{bulk_id}/keepalive")
    async def bulk_keepalive(request: Request, bulk_id: str):
        bulk = deps.bulk_session_or_404(app, bulk_id)
        await deps.form_data(request)
        deps.touch_bulk_activity(app, bulk)
        return Response(status_code=204)

    @app.get("/import/{session_id}/confirm", response_class=HTMLResponse)
    def import_confirm(request: Request, session_id: str):
        if deps.load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)
        session = deps.session_or_404(app, session_id)
        deps.touch_import_activity(app, session)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="import_confirm.html",
            context={
                "session_id": session_id,
                "staged": session.staged,
                "error": None,
                "duplicate": False,
                "is_bulk": bool(session.bulk_id),
            },
        )

    @app.post("/import/{session_id}/skip")
    async def import_skip(request: Request, session_id: str):
        session = deps.session_or_404(app, session_id)
        if not session.bulk_id:
            raise HTTPException(status_code=400, detail="Only bulk-import comics can be skipped")
        await deps.form_data(request)
        deps.touch_import_activity(app, session)

        bulk_id = session.bulk_id
        bulk = deps.bulk_session_or_404(app, bulk_id)
        skipped_name = (
            session.staged.series
            if session.staged is not None
            else session.series_default or session.plan.scan.content_root.name
        )
        bulk.skipped_series.append(skipped_name)
        deps.remove_staging_record(session)
        app.state.import_sessions.pop(session_id, None)
        deps.delete_import_session_state(app, session_id)
        bulk.current_position += 1
        deps.touch_bulk_activity(app, bulk)

        try:
            next_session_id = deps.start_bulk_item(app, bulk_id)
        except (FolderScanError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if next_session_id is not None:
            return RedirectResponse(f"/import/{next_session_id}/review", status_code=303)

        deps.cleanup_upload(bulk.upload_root)
        bulk.upload_root = None
        deps.delete_bulk_session_state(app, bulk_id)
        return RedirectResponse(f"/import/bulk/{bulk_id}/done", status_code=303)

    @app.get("/import/{session_id}/done", response_class=HTMLResponse)
    def import_done(request: Request, session_id: str):
        receipt = deps.load_completed_import(app, session_id)
        if receipt is None:
            session = deps.session_or_404(app, session_id)
            if session.staged is None:
                return RedirectResponse(f"/import/{session_id}/review", status_code=303)
            return RedirectResponse(f"/import/{session_id}/confirm", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="import_done.html",
            context={
                "result": type("CompletedResult", (), receipt["result"])(),
                "staged": type("CompletedStaged", (), {"author": receipt["author"], "series": receipt["series"]})(),
                "next_url": receipt.get("next_url"),
                "bulk_progress": receipt.get("bulk_progress"),
                "bulk_error": receipt.get("bulk_error"),
            },
        )

    @app.get("/import/{session_id}/staging-progress")
    def import_staging_progress(session_id: str):
        progress = app.state.staging_progress.get(session_id)
        if progress is None:
            return {"phase": "waiting", "current": 0, "total": 0, "detail": "Waiting to start"}
        return progress

    @app.get("/import/{session_id}/commit-progress")
    def import_commit_progress(session_id: str):
        progress = app.state.import_progress.get(session_id)
        if progress is None:
            if deps.load_completed_import(app, session_id) is not None:
                return {"phase": "complete", "current": 1, "total": 1, "detail": "Import complete"}
            return {"phase": "waiting", "current": 0, "total": 0, "detail": "Waiting to start"}
        return progress

    @app.post("/import/{session_id}/commit", response_class=HTMLResponse)
    async def import_commit(request: Request, session_id: str):
        if deps.load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)

        session = deps.session_or_404(app, session_id)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        form = await deps.form_data(request)
        deps.touch_import_activity(app, session)
        allow_duplicate = form.get("allow_duplicate") == "yes"
        commit_started = time.monotonic()
        last_commit_phase: list[str | None] = [None]

        def report_progress(phase: str, current: int, total: int, detail: str) -> None:
            app.state.import_progress[session_id] = {
                "phase": phase,
                "current": current,
                "total": total,
                "detail": detail,
                "updated_at": time.time(),
            }
            if phase != last_commit_phase[0] or (
                total > 0 and current > 0 and (current == total or current % 1000 == 0)
            ):
                logger.info(
                    "commit progress session_id=%s phase=%s current=%d total=%d detail=%s elapsed=%.2fs",
                    session_id,
                    phase,
                    current,
                    total,
                    detail,
                    time.monotonic() - commit_started,
                )
                deps.log_memory_checkpoint(
                    f"commit-{phase}", session_id=session_id, current=current, total=total
                )
                last_commit_phase[0] = phase

        app.state.import_progress[session_id] = {
            "phase": "starting",
            "current": 0,
            "total": 0,
            "detail": "Starting import",
            "updated_at": time.time(),
        }
        deps.log_memory_checkpoint("commit-start", session_id=session_id)
        try:
            result = await asyncio.to_thread(
                deps.commit_staged_import,
                session.staged,
                library_root=library,
                database_path=database,
                allow_duplicate=allow_duplicate,
                progress_callback=report_progress,
            )
            result_payload = {
                "import_id": result.import_id,
                "author_id": result.author_id,
                "series_id": result.series_id,
                "copied_files": result.copied_files,
            }
        except CommitError as exc:
            existing = deps.existing_commit_result(database, session.staged)
            if existing is not None and "already been committed" in str(exc):
                result_payload = existing
            else:
                message = str(exc)
                duplicate = "Possible duplicate import detected" in message
                return templates.TemplateResponse(
                    request=request,
                    name="import_confirm.html",
                    context={
                        "session_id": session_id,
                        "staged": session.staged,
                        "error": message,
                        "duplicate": duplicate,
                        "is_bulk": bool(session.bulk_id),
                    },
                    status_code=409 if duplicate else 400,
                )

        receipt = {
            "author": session.staged.author,
            "series": session.staged.series,
            "result": result_payload,
            "next_url": None,
            "bulk_progress": None,
            "bulk_error": None,
        }

        if session.bulk_id:
            bulk = deps.bulk_session_or_404(app, session.bulk_id)
            if not any(series_id == result_payload["series_id"] for _, series_id in bulk.imported_series):
                bulk.imported_series.append((session.staged.series, result_payload["series_id"]))
                bulk.current_position += 1
            try:
                next_session_id = deps.start_bulk_item(app, session.bulk_id)
            except (FolderScanError, OSError) as exc:
                receipt["bulk_error"] = str(exc)
                receipt["bulk_progress"] = f"{bulk.current_position} of {len(bulk.selected)}"
                next_session_id = None
            if next_session_id is not None:
                receipt["next_url"] = f"/import/{next_session_id}/review"
                receipt["bulk_progress"] = f"{bulk.current_position} of {len(bulk.selected)}"
            elif bulk.current_position >= len(bulk.selected):
                deps.cleanup_upload(bulk.upload_root)
                bulk.upload_root = None
                deps.delete_bulk_session_state(app, session.bulk_id)
        else:
            deps.cleanup_upload(session.upload_root)

        deps.save_completed_import(app, session_id, receipt)
        app.state.import_sessions.pop(session_id, None)
        deps.delete_import_session_state(app, session_id)
        deps.remove_staging_record(session)

        if session.bulk_id and receipt["next_url"] is None and receipt["bulk_error"] is None:
            return RedirectResponse(f"/import/bulk/{session.bulk_id}/done", status_code=303)
        return RedirectResponse(f"/import/{session_id}/done", status_code=303)
