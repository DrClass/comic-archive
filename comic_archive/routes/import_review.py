from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


@dataclass(frozen=True)
class ImportReviewRouteDeps:
    import_root: Path
    staging_root: Path
    templates: Any
    form_data: Callable[[Request], Awaitable[dict[str, str]]]
    safe_import_folder: Callable[[Path, str], Path]
    import_folder_listing: Callable[..., Any]
    save_uploaded_folder: Callable[..., Awaitable[Any]]
    uploaded_source: Callable[[Path, str | None], Path]
    cleanup_upload: Callable[[Path | None], None]
    discover_artist_comics: Callable[..., Any]
    bulk_artist_session: Callable[..., Any]
    touch_bulk_activity: Callable[[FastAPI, Any], None]
    bulk_session_or_404: Callable[[FastAPI, str], Any]
    start_bulk_item: Callable[[FastAPI, str], str | None]
    new_pdf_cache: Callable[..., Path]
    scan_folder: Callable[..., Any]
    build_review_plan: Callable[..., Any]
    import_session: Callable[..., Any]
    touch_import_activity: Callable[[FastAPI, Any], None]
    session_or_404: Callable[[FastAPI, str], Any]
    load_completed_import: Callable[[FastAPI, str], Any]
    workspace_tree: Callable[[Any], list[dict[str, Any]]]
    workspace_media_for_folder: Callable[[Any, str], list[Any]]
    workspace_media_targets_for_folder: Callable[[Any, str], list[Any]]
    workspace_node_metadata: Callable[[Any, dict[str, Any]], dict[str, Any]]
    flattened_folder_candidates: Callable[[Any], list[Any]]
    rescan_import_session: Callable[[Any], None]
    review_role_choices: Callable[..., list[Any]]
    review_source_relative: Callable[[Any, Path], str]
    review_error: type[Exception]
    folder_scan_error: type[Exception]


def register_import_review_routes(app: FastAPI, deps: ImportReviewRouteDeps) -> None:
    imports = deps.import_root
    staging = deps.staging_root
    templates = deps.templates

    @app.get("/import/bulk", response_class=HTMLResponse)
    def bulk_import_start(request: Request, folder: str = ""):
        selected = deps.safe_import_folder(imports, folder) if folder else None
        return templates.TemplateResponse(
            request=request,
            name="import_bulk_start.html",
            context={
                "error": None,
                "selected_path": folder,
                "selected_label": str(selected.relative_to(imports)) if selected else None,
                "import_root": imports,
            },
        )

    @app.post("/import/bulk/upload", response_class=HTMLResponse)
    async def bulk_import_upload(request: Request):
        upload_root: Path | None = None
        try:
            upload_root, top_level = await deps.save_uploaded_folder(request, staging)
            source_path = deps.uploaded_source(upload_root, top_level)
            source, candidates = deps.discover_artist_comics(source_path)
        except HTTPException:
            deps.cleanup_upload(upload_root)
            raise
        except (deps.folder_scan_error, OSError) as exc:
            deps.cleanup_upload(upload_root)
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_start.html",
                context={"error": str(exc)},
                status_code=400,
            )

        bulk_id = str(uuid4())
        app.state.bulk_import_sessions[bulk_id] = deps.bulk_artist_session(
            source=source,
            author=source.name,
            candidates=candidates,
            upload_root=upload_root,
        )
        deps.touch_bulk_activity(app, app.state.bulk_import_sessions[bulk_id])
        return RedirectResponse(f"/import/bulk/{bulk_id}", status_code=303)

    @app.post("/import/bulk/scan", response_class=HTMLResponse)
    async def bulk_import_scan(request: Request):
        form = await deps.form_data(request)
        selected_path = form.get("selected_path", "").strip() or form.get("source_path", "").strip()
        try:
            source_path = deps.safe_import_folder(imports, selected_path)
            source, candidates = deps.discover_artist_comics(source_path)
        except (deps.folder_scan_error, OSError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_start.html",
                context={"error": str(exc), "selected_path": selected_path},
                status_code=400,
            )
        bulk_id = str(uuid4())
        app.state.bulk_import_sessions[bulk_id] = deps.bulk_artist_session(
            source=source,
            author=form.get("author", "").strip() or source.name,
            candidates=candidates,
        )
        deps.touch_bulk_activity(app, app.state.bulk_import_sessions[bulk_id])
        return RedirectResponse(f"/import/bulk/{bulk_id}", status_code=303)

    @app.get("/import/bulk/{bulk_id}", response_class=HTMLResponse)
    def bulk_import_choose(request: Request, bulk_id: str):
        bulk = deps.bulk_session_or_404(app, bulk_id)
        deps.touch_bulk_activity(app, bulk)
        return templates.TemplateResponse(
            request=request,
            name="import_bulk_choose.html",
            context={"bulk_id": bulk_id, "bulk": bulk, "error": None},
        )

    @app.post("/import/bulk/{bulk_id}/start", response_class=HTMLResponse)
    async def bulk_import_begin(request: Request, bulk_id: str):
        bulk = deps.bulk_session_or_404(app, bulk_id)
        form = await deps.form_data(request)
        selected = [
            index for index in range(len(bulk.candidates))
            if form.get(f"comic_{index}") == "yes"
        ]
        author = form.get("author", "").strip()
        if not author:
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_choose.html",
                context={"bulk_id": bulk_id, "bulk": bulk, "error": "Author is required."},
                status_code=400,
            )
        if not selected:
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_choose.html",
                context={"bulk_id": bulk_id, "bulk": bulk, "error": "Choose at least one comic folder."},
                status_code=400,
            )
        bulk.author = author
        bulk.selected = selected
        bulk.current_position = 0
        bulk.imported_series.clear()
        bulk.skipped_series.clear()
        deps.touch_bulk_activity(app, bulk)
        try:
            session_id = deps.start_bulk_item(app, bulk_id)
        except (deps.folder_scan_error, OSError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_choose.html",
                context={"bulk_id": bulk_id, "bulk": bulk, "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.get("/import/bulk/{bulk_id}/done", response_class=HTMLResponse)
    def bulk_import_done(request: Request, bulk_id: str):
        bulk = deps.bulk_session_or_404(app, bulk_id)
        return templates.TemplateResponse(
            request=request,
            name="import_bulk_done.html",
            context={"bulk": bulk},
        )

    @app.get("/import", response_class=HTMLResponse)
    def import_start(request: Request, folder: str = ""):
        selected = deps.safe_import_folder(imports, folder) if folder else None
        return templates.TemplateResponse(
            request=request,
            name="import_start.html",
            context={
                "error": None,
                "selected_path": folder,
                "selected_label": str(selected.relative_to(imports)) if selected else None,
                "import_root": imports,
            },
        )

    @app.get("/import/browse", response_class=HTMLResponse)
    def import_browse(request: Request, path: str = ".", mode: str = "single"):
        if mode not in {"single", "bulk"}:
            raise HTTPException(status_code=400, detail="Invalid folder-browser mode")
        current, relative, folders, parent = deps.import_folder_listing(imports, path)
        return templates.TemplateResponse(
            request=request,
            name="import_browse.html",
            context={
                "mode": mode,
                "current": current,
                "relative": relative,
                "folders": folders,
                "parent": parent,
                "import_root": imports,
                "select_url": "/import/bulk" if mode == "bulk" else "/import",
            },
        )

    @app.post("/import/upload", response_class=HTMLResponse)
    async def import_upload(request: Request):
        upload_root: Path | None = None
        try:
            upload_root, top_level = await deps.save_uploaded_folder(request, staging)
            source_path = deps.uploaded_source(upload_root, top_level)
            pdf_cache_root = deps.new_pdf_cache(staging, upload_root)
            scan = deps.scan_folder(source_path, pdf_cache_root=pdf_cache_root)
            plan = deps.build_review_plan(scan)
        except HTTPException:
            deps.cleanup_upload(upload_root)
            raise
        except (deps.folder_scan_error, OSError) as exc:
            deps.cleanup_upload(upload_root)
            return templates.TemplateResponse(
                request=request,
                name="import_start.html",
                context={"error": str(exc)},
                status_code=400,
            )

        session_id = str(uuid4())
        app.state.import_sessions[session_id] = deps.import_session(
            plan=plan,
            series_default=scan.content_root.name,
            upload_root=upload_root,
            pdf_cache_root=pdf_cache_root,
        )
        deps.touch_import_activity(app, app.state.import_sessions[session_id])
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.post("/import/scan", response_class=HTMLResponse)
    async def import_scan(request: Request):
        form = await deps.form_data(request)
        selected_path = form.get("selected_path", "").strip() or form.get("source_path", "").strip()
        try:
            source_path = deps.safe_import_folder(imports, selected_path)
            pdf_cache_root = deps.new_pdf_cache(staging)
            scan = deps.scan_folder(source_path, pdf_cache_root=pdf_cache_root)
            plan = deps.build_review_plan(scan)
        except (deps.folder_scan_error, OSError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="import_start.html",
                context={"error": str(exc), "selected_path": selected_path},
                status_code=400,
            )
        session_id = str(uuid4())
        app.state.import_sessions[session_id] = deps.import_session(plan=plan, pdf_cache_root=pdf_cache_root)
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.get("/import/{session_id}/review", response_class=HTMLResponse)
    def import_review(request: Request, session_id: str):
        if deps.load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)
        session = deps.session_or_404(app, session_id)
        deps.touch_import_activity(app, session)
        tree = deps.workspace_tree(session)
        requested = request.query_params.get("folder", "")
        selected = requested if any(node["path"] == requested for node in tree) else str(tree[0]["path"])
        selected_node = next(node for node in tree if node["path"] == selected)
        media = deps.workspace_media_for_folder(session, selected)
        media_total = len(media)
        try:
            media_offset = max(0, int(request.query_params.get("media_offset", "0")))
        except ValueError:
            media_offset = 0
        media_page_size = 250
        if media_offset >= media_total and media_total:
            media_offset = max(0, ((media_total - 1) // media_page_size) * media_page_size)
        media_page = media[media_offset:media_offset + media_page_size]
        media_targets = deps.workspace_media_targets_for_folder(session, selected)
        return templates.TemplateResponse(
            request=request,
            name="import_workspace.html",
            context={
                "session_id": session_id,
                "plan": session.plan,
                "tree": tree,
                "selected": selected,
                "selected_node": selected_node,
                "selected_media": media_page,
                "selected_media_total": media_total,
                "selected_media_offset": media_offset,
                "selected_media_page_size": media_page_size,
                "selected_metadata": deps.workspace_node_metadata(session, selected_node),
                "media_targets": media_targets,
                "media_target_paths": {path for path, _ in media_targets},
                "author_value": session.workspace_author if session.workspace_author is not None else (session.author_default or ""),
                "series_value": session.workspace_series if session.workspace_series is not None else (session.series_default or session.plan.scan.content_root.name),
                "series_complete_value": session.workspace_series_complete,
                "ignored_media": session.workspace_ignored_media,
                "errors": [],
            },
        )

    @app.post("/import/{session_id}/review/mark-extra", response_class=HTMLResponse)
    async def import_review_mark_extra(request: Request, session_id: str):
        session = deps.session_or_404(app, session_id)
        form = await deps.form_data(request)
        deps.touch_import_activity(app, session)
        relative_path = form.get("folder_path", "").strip()
        candidates = {str(item.relative_path): item for item in deps.flattened_folder_candidates(session.plan.scan)}
        if relative_path not in candidates:
            raise HTTPException(status_code=400, detail="Folder is not an available flattened folder")
        session.extra_folder_overrides.add(relative_path)
        try:
            deps.rescan_import_session(session)
        except (deps.folder_scan_error, OSError) as exc:
            session.extra_folder_overrides.discard(relative_path)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.post("/import/{session_id}/review/mark-subseries", response_class=HTMLResponse)
    async def import_review_mark_subseries(request: Request, session_id: str):
        session = deps.session_or_404(app, session_id)
        form = await deps.form_data(request)
        deps.touch_import_activity(app, session)
        relative_path = form.get("folder_path", "").strip()
        if not relative_path:
            raise HTTPException(status_code=400, detail="Missing folder path")
        candidate = (session.plan.scan.source / relative_path).resolve()
        try:
            candidate.relative_to(session.plan.scan.source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid folder path") from exc
        if not candidate.is_dir():
            raise HTTPException(status_code=400, detail="Sub-series folder not found")
        session.subseries_folder_overrides.add(relative_path)
        try:
            deps.rescan_import_session(session)
        except (deps.folder_scan_error, OSError) as exc:
            session.subseries_folder_overrides.discard(relative_path)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.post("/import/{session_id}/review", response_class=HTMLResponse)
    async def import_review_save(request: Request, session_id: str):
        session = deps.session_or_404(app, session_id)
        form = await deps.form_data(request)
        deps.touch_import_activity(app, session)
        errors: list[str] = []
        for index, item in enumerate(session.plan.items):
            try:
                session.plan.set_name(item.relative_path, form.get(f"name_{index}", item.name))
                session.plan.set_role(item.relative_path, form.get(f"role_{index}", item.role.value))
            except deps.review_error as exc:
                errors.append(str(exc))
        errors.extend(session.plan.validation_errors())
        if errors:
            role_choices = {
                index: deps.review_role_choices(item, is_series=session.plan.scan.is_series_candidate)
                for index, item in enumerate(session.plan.items)
            }
            return templates.TemplateResponse(
                request=request,
                name="import_review.html",
                context={
                    "session_id": session_id,
                    "plan": session.plan,
                    "role_choices": role_choices,
                    "errors": errors,
                    "flattened_folders": [
                        item for item in deps.flattened_folder_candidates(session.plan.scan)
                        if str(item.relative_path) not in session.extra_folder_overrides
                    ],
                    "subseries_candidates": {
                        index: deps.review_source_relative(session.plan.scan, item.relative_path)
                        for index, item in enumerate(session.plan.items)
                        if item.source_kind == "issue"
                    },
                },
                status_code=400,
            )
        return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
