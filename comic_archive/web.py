from __future__ import annotations

import sqlite3
import json
import secrets
import time
import shutil
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .importer.commit import CommitError, commit_staged_import, initialize_database
from .importer.review import ReviewError, ReviewPlan, ReviewRole, build_review_plan, flattened_folder_candidates
from .importer.scanner import FolderScanError, SUPPORTED_MEDIA, scan_folder
from .importer.sorting import natural_text_key
from .importer.bulk import BulkComicCandidate, discover_artist_comics
from .importer.staging import (
    StagedImport, StagedIssue, StagedSeries, StagedGroup, StagedMedia,
    StagingError, build_staged_import, create_staged_issue_extra,
    move_staged_media, remove_empty_staged_group,
)
from .library import read_library
from .maintenance import series_gaps
from .auth import get_or_create_session_secret, get_user, initialize_auth_database
from .database import connect_database
from .logging_config import configure_logging
from .routes.auth import register_auth_routes
from .routes.library import register_library_routes
from .routes.editing import register_editing_routes
from .routes.maintenance import register_maintenance_routes
from .routes.media import register_media_routes
from .routes.import_uploads import ImportUploadRouteDeps, register_import_upload_routes
from .routes.import_review import ImportReviewRouteDeps, register_import_review_routes
from .routes.import_workspace import ImportWorkspaceRouteDeps, register_import_workspace_routes
from .routes.import_finalize import ImportFinalizeRouteDeps, register_import_finalize_routes
from .library_views import (
    series_lineage as _series_lineage,
)
from .web_forms import form_data as _form_data, check_csrf_value as _check_csrf_value, bool_form as _bool_form
from .services.import_sessions import (
    ImportSession, BulkArtistSession,
    persist_import_session as _persist_import_session,
    delete_import_session_state as _delete_import_session_state,
    save_completed_import as _save_completed_import,
    load_completed_import as _load_completed_import,
    persist_bulk_session as _persist_bulk_session,
    delete_bulk_session_state as _delete_bulk_session_state,
    rescan_import_session as _rescan_import_session,
    touch_import_activity as _touch_import_activity,
    touch_bulk_activity as _touch_bulk_activity,
    remove_staging_record as _remove_staging_record_service,
    cleanup_stale_imports as _cleanup_stale_imports_service,
    session_or_404 as _session_or_404_service,
    bulk_session_or_404 as _bulk_session_or_404_service,
)


from .services.diagnostics import (
    log_memory_checkpoint as _log_memory_checkpoint,
    run_background_io as _run_background_io,
    logger,
)
from .services.uploads import (
    BrowserUploadSession,
    UPLOAD_STATE_CHECKPOINT_FILES,
    UPLOAD_WRITE_BUFFER_BYTES,
    persist_browser_upload_session as _persist_browser_upload_session,
    checkpoint_browser_upload_session as _checkpoint_browser_upload_session,
    delete_browser_upload_session_state as _delete_browser_upload_session_state,
    upload_session_or_404 as _upload_session_or_404,
    touch_browser_upload as _touch_browser_upload,
    save_upload_file as _save_upload_file,
    upload_top_level as _upload_top_level,
    save_uploaded_folder as _save_uploaded_folder,
    uploaded_source as _uploaded_source,
    cleanup_upload as _cleanup_upload,
    new_pdf_cache as _new_pdf_cache,
)

_TEMPLATE_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


UPLOAD_SWEEP_INTERVAL_SECONDS = 60 * 60


from .services.workspace import (
    _review_role_choices, _review_source_relative, _workspace_source_relative,
    _workspace_cache_state_signature, _workspace_invalidate_cache, _workspace_tree,
    _workspace_item_key, _workspace_node_metadata, _workspace_all_media,
    _workspace_origin_for_media, _workspace_media_targets_for_folder,
    _workspace_media_for_folder, _workspace_reorder_media,
    _workspace_sorted_review_items, _workspace_build_staged_import,
)

from .services.import_orchestration import (
    _existing_commit_result, _remove_staging_record, _cleanup_stale_imports,
    _session_or_404, _bulk_session_or_404, _start_bulk_item,
    _safe_import_folder, _import_folder_listing,
)

def create_app(
    database_path: str | Path = "./comic_archive.sqlite3",
    library_root: str | Path = "./library",
    staging_root: str | Path = "./staging",
    secure_cookies: bool = False,
    import_root: str | Path | None = None,
    log_file: str | Path | None = None,
    log_level: str = "INFO",
) -> FastAPI:
    log_path = configure_logging(
        log_file if log_file is not None else Path(database_path).expanduser().absolute().parent / "logs" / "comic-archive.log",
        level=log_level,
    )
    database = initialize_database(database_path)
    initialize_auth_database(database)
    library = Path(library_root).expanduser().resolve()
    staging = Path(staging_root).expanduser().resolve()
    imports = database.parent if import_root is None else Path(import_root).expanduser().resolve()

    app = FastAPI(title="Comic Archive", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database_path = database
    app.state.log_file = log_path
    app.state.library_root = library
    app.state.staging_root = staging
    app.state.import_root = imports
    app.state.upload_sessions: dict[str, BrowserUploadSession] = {}
    app.state.import_sessions: dict[str, ImportSession] = {}
    app.state.bulk_import_sessions: dict[str, BulkArtistSession] = {}
    app.state.login_attempts: dict[str, list[float]] = {}
    app.state.import_progress: dict[str, dict[str, object]] = {}
    app.state.staging_progress: dict[str, dict[str, object]] = {}
    app.state.last_upload_sweep = 0.0
    logger.info(
        "diagnostics enabled database=%s staging=%s upload_checkpoint_files=%d upload_buffer_mib=%.1f",
        database, staging, UPLOAD_STATE_CHECKPOINT_FILES, UPLOAD_WRITE_BUFFER_BYTES / (1024 * 1024),
    )
    _log_memory_checkpoint("app-startup")

    @app.middleware("http")
    async def require_authentication(request: Request, call_next):
        now = time.time()
        if now - app.state.last_upload_sweep >= UPLOAD_SWEEP_INTERVAL_SECONDS:
            _cleanup_stale_imports(app, staging, now=now)
            app.state.last_upload_sweep = now
        if "csrf_token" not in request.session:
            request.session["csrf_token"] = secrets.token_urlsafe(32)

        path = request.url.path
        if path in {"/login", "/favicon.ico"}:
            return await call_next(request)

        user_id = request.session.get("user_id")
        if not user_id:
            # Anonymous requests to protected paths must not rotate the CSRF
            # token. Browsers can make background requests (for example, a
            # favicon request) while the login form is open; rotating here
            # would make the already-rendered login form immediately stale.
            return RedirectResponse("/login", status_code=303)

        user = get_user(database, user_id)
        if (
            user is None
            or request.session.get("session_version") != user.session_version
        ):
            # Clear invalid authentication state while preserving the current
            # anonymous CSRF token so the subsequent login form remains valid.
            csrf_token = request.session.get("csrf_token") or secrets.token_urlsafe(32)
            request.session.clear()
            request.session["csrf_token"] = csrf_token
            return RedirectResponse("/login", status_code=303)

        request.state.user = user
        admin_only = (
            path.startswith("/import")
            or path.startswith("/admin")
            or path.startswith("/maintenance")
            or path == "/history"
            or "/edit" in path
            or (request.method == "POST" and (
                path.startswith("/media/")
                or path.startswith("/groups/")
                or path.startswith("/issues/")
                or path.startswith("/series/")
            ))
        )
        if admin_only and not user.is_admin:
            return HTMLResponse("Administrator access required", status_code=403)
        response = await call_next(request)
        # Import workspace state is durable, not merely in-process. Persist
        # after handling the request so mutations made by autosave, drag/drop,
        # role changes, group edits, and bulk progress are captured immediately.
        if path.startswith("/import"):
            for active_id, active_session in list(app.state.import_sessions.items()):
                _persist_import_session(app, active_id, active_session)
            for active_id, active_bulk in list(app.state.bulk_import_sessions.items()):
                _persist_bulk_session(app, active_id, active_bulk)
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # Avoid redirecting the browser's automatic favicon request through
        # the authentication flow. A real favicon can replace this later.
        return Response(status_code=204)

    register_auth_routes(app, templates, database)

    register_library_routes(app, templates, database)

    register_editing_routes(app, templates, database, library)

    register_maintenance_routes(app, templates, database, library)

    register_media_routes(app, database, library)

    register_import_upload_routes(
        app,
        ImportUploadRouteDeps(
            staging_root=staging,
            form_data=_form_data,
            browser_upload_session=BrowserUploadSession,
            touch_browser_upload=_touch_browser_upload,
            persist_browser_upload_session=_persist_browser_upload_session,
            upload_session_or_404=_upload_session_or_404,
            save_upload_file=_save_upload_file,
            checkpoint_browser_upload_session=_checkpoint_browser_upload_session,
            delete_browser_upload_session_state=_delete_browser_upload_session_state,
            cleanup_upload=_cleanup_upload,
            upload_top_level=_upload_top_level,
            uploaded_source=_uploaded_source,
            log_memory_checkpoint=_log_memory_checkpoint,
            run_background_io=_run_background_io,
            discover_artist_comics=discover_artist_comics,
            bulk_artist_session=BulkArtistSession,
            touch_bulk_activity=_touch_bulk_activity,
            new_pdf_cache=_new_pdf_cache,
            scan_folder=scan_folder,
            build_review_plan=build_review_plan,
            import_session=ImportSession,
            touch_import_activity=_touch_import_activity,
            folder_scan_error=FolderScanError,
            logger=logger,
        ),
    )

    register_import_review_routes(
        app,
        ImportReviewRouteDeps(
            import_root=imports,
            staging_root=staging,
            templates=templates,
            form_data=_form_data,
            safe_import_folder=_safe_import_folder,
            import_folder_listing=_import_folder_listing,
            save_uploaded_folder=_save_uploaded_folder,
            uploaded_source=_uploaded_source,
            cleanup_upload=_cleanup_upload,
            discover_artist_comics=discover_artist_comics,
            bulk_artist_session=BulkArtistSession,
            touch_bulk_activity=_touch_bulk_activity,
            bulk_session_or_404=_bulk_session_or_404,
            start_bulk_item=_start_bulk_item,
            new_pdf_cache=_new_pdf_cache,
            scan_folder=scan_folder,
            build_review_plan=build_review_plan,
            import_session=ImportSession,
            touch_import_activity=_touch_import_activity,
            session_or_404=_session_or_404,
            load_completed_import=_load_completed_import,
            workspace_tree=_workspace_tree,
            workspace_media_for_folder=_workspace_media_for_folder,
            workspace_media_targets_for_folder=_workspace_media_targets_for_folder,
            workspace_node_metadata=_workspace_node_metadata,
            flattened_folder_candidates=flattened_folder_candidates,
            rescan_import_session=_rescan_import_session,
            review_role_choices=_review_role_choices,
            review_source_relative=_review_source_relative,
            review_error=ReviewError,
            folder_scan_error=FolderScanError,
        ),
    )

    register_import_workspace_routes(
        app,
        ImportWorkspaceRouteDeps(
            staging_root=staging,
            templates=templates,
            form_data=_form_data,
            session_or_404=_session_or_404,
            touch_import_activity=_touch_import_activity,
            workspace_tree=_workspace_tree,
            workspace_all_media=_workspace_all_media,
            workspace_media_targets_for_folder=_workspace_media_targets_for_folder,
            workspace_origin_for_media=_workspace_origin_for_media,
            workspace_sorted_review_items=_workspace_sorted_review_items,
            workspace_item_key=_workspace_item_key,
            workspace_source_relative=_workspace_source_relative,
            workspace_build_staged_import=_workspace_build_staged_import,
            run_background_io=_run_background_io,
            log_memory_checkpoint=_log_memory_checkpoint,
            workspace_node_metadata=_workspace_node_metadata,
            workspace_media_for_folder=_workspace_media_for_folder,
            workspace_cache_state_signature=_workspace_cache_state_signature,
            workspace_reorder_media=_workspace_reorder_media,
            workspace_invalidate_cache=_workspace_invalidate_cache,
            logger=logger,
        ),
    )


    register_import_finalize_routes(
        app,
        ImportFinalizeRouteDeps(
            staging_root=staging,
            library_root=library,
            database_path=database,
            templates=templates,
            form_data=_form_data,
            bool_form=_bool_form,
            session_or_404=_session_or_404,
            bulk_session_or_404=_bulk_session_or_404,
            touch_import_activity=_touch_import_activity,
            touch_bulk_activity=_touch_bulk_activity,
            load_completed_import=_load_completed_import,
            save_completed_import=_save_completed_import,
            delete_import_session_state=_delete_import_session_state,
            delete_bulk_session_state=_delete_bulk_session_state,
            remove_staging_record=_remove_staging_record,
            cleanup_upload=_cleanup_upload,
            start_bulk_item=_start_bulk_item,
            workspace_sorted_review_items=_workspace_sorted_review_items,
            build_staged_import=build_staged_import,
            move_staged_media=move_staged_media,
            create_staged_issue_extra=create_staged_issue_extra,
            remove_empty_staged_group=remove_empty_staged_group,
            existing_commit_result=_existing_commit_result,
            commit_staged_import=commit_staged_import,
            log_memory_checkpoint=_log_memory_checkpoint,
            logger=logger,
        ),
    )


    @app.get("/health")
    def health():
        return {"ok": True}

    app.add_middleware(
        SessionMiddleware,
        secret_key=get_or_create_session_secret(database),
        session_cookie="comic_archive_session",
        same_site="lax",
        https_only=secure_cookies,
        max_age=60 * 60 * 24 * 30,
    )
    return app
