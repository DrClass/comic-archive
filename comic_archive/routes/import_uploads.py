from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class ImportUploadRouteDeps:
    staging_root: Path
    form_data: Callable[[Request], Awaitable[dict[str, str]]]
    browser_upload_session: Callable[..., Any]
    touch_browser_upload: Callable[[Any], None]
    persist_browser_upload_session: Callable[[FastAPI, str, Any], None]
    upload_session_or_404: Callable[[FastAPI, str], Any]
    save_upload_file: Callable[[Request, Any], Awaitable[str]]
    checkpoint_browser_upload_session: Callable[..., None]
    delete_browser_upload_session_state: Callable[[FastAPI, str], None]
    cleanup_upload: Callable[[Path | None], None]
    upload_top_level: Callable[[Any], str | None]
    uploaded_source: Callable[[Path, str | None], Path]
    log_memory_checkpoint: Callable[..., None]
    run_background_io: Callable[..., Awaitable[Any]]
    discover_artist_comics: Callable[..., Any]
    bulk_artist_session: Callable[..., Any]
    touch_bulk_activity: Callable[[FastAPI, Any], None]
    new_pdf_cache: Callable[[Path, Path | None], Path]
    scan_folder: Callable[..., Any]
    build_review_plan: Callable[..., Any]
    import_session: Callable[..., Any]
    touch_import_activity: Callable[[FastAPI, Any], None]
    folder_scan_error: type[Exception]
    logger: Any


def register_import_upload_routes(app: FastAPI, deps: ImportUploadRouteDeps) -> None:
    """Register resumable browser-upload endpoints for the importer.

    This module owns the HTTP surface while importer/session internals remain in
    the existing service code for this refactor slice. Keeping the boundary
    explicit lets later milestones move those internals without changing URLs.
    """
    staging = deps.staging_root

    @app.post("/import/upload-session", response_class=JSONResponse)
    async def create_upload_session(request: Request):
        form = await deps.form_data(request)
        mode = form.get("mode", "single").strip()
        if mode not in {"single", "bulk"}:
            raise HTTPException(status_code=400, detail="Invalid upload mode")
        try:
            expected_files = int(form.get("expected_files", "0"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid file count") from exc
        if expected_files < 1 or expected_files > 100000:
            raise HTTPException(status_code=400, detail="File count must be between 1 and 100000")

        upload_id = str(uuid4())
        upload_root = staging / "uploads" / upload_id
        (upload_root / "content").mkdir(parents=True, exist_ok=False)
        session = deps.browser_upload_session(
            upload_root=upload_root,
            mode=mode,
            expected_files=expected_files,
        )
        app.state.upload_sessions[upload_id] = session
        deps.touch_browser_upload(session)
        deps.persist_browser_upload_session(app, upload_id, session)
        deps.logger.info(
            "upload session created id=%s mode=%s expected_files=%d root=%s",
            upload_id, mode, expected_files, upload_root,
        )
        return JSONResponse({"upload_id": upload_id, "expected_files": expected_files})

    @app.post("/import/upload-session/{upload_id}/file", response_class=JSONResponse)
    async def upload_session_file(request: Request, upload_id: str):
        upload = deps.upload_session_or_404(app, upload_id)
        relative = await deps.save_upload_file(request, upload)
        deps.checkpoint_browser_upload_session(app, upload_id, upload)
        elapsed = max(0.001, time.monotonic() - upload.started_at)
        throughput_mib_s = upload.uploaded_bytes / (1024 * 1024) / elapsed
        return JSONResponse({
            "relative_path": relative,
            "received_files": len(upload.received_paths),
            "expected_files": upload.expected_files,
            "throughput_mib_s": round(throughput_mib_s, 2),
        })

    @app.get("/import/upload-session/{upload_id}/status", response_class=JSONResponse)
    async def upload_session_status(request: Request, upload_id: str):
        upload = deps.upload_session_or_404(app, upload_id)
        return JSONResponse({
            "received_files": len(upload.received_paths),
            "expected_files": upload.expected_files,
            "scan": upload.scan_progress,
        })

    @app.post("/import/upload-session/{upload_id}/cancel", response_class=JSONResponse)
    async def cancel_upload_session(request: Request, upload_id: str):
        await deps.form_data(request)
        upload = deps.upload_session_or_404(app, upload_id)
        upload.cancelled = True
        app.state.upload_sessions.pop(upload_id, None)
        deps.delete_browser_upload_session_state(app, upload_id)
        deps.cleanup_upload(upload.upload_root)
        return JSONResponse({"cancelled": True})

    @app.post("/import/upload-session/{upload_id}/finalize", response_class=JSONResponse)
    async def finalize_upload_session(request: Request, upload_id: str):
        await deps.form_data(request)
        upload = deps.upload_session_or_404(app, upload_id)
        deps.touch_browser_upload(upload)
        deps.checkpoint_browser_upload_session(app, upload_id, upload, force=True)
        if len(upload.received_paths) != upload.expected_files:
            return JSONResponse(
                {"error": (
                    f"Upload is incomplete: received {len(upload.received_paths)} "
                    f"of {upload.expected_files} files."
                )},
                status_code=409,
            )

        top_level = deps.upload_top_level(upload)
        source_path = deps.uploaded_source(upload.upload_root, top_level)
        deps.log_memory_checkpoint(
            "upload-transfer-complete", upload_id=upload_id, files=len(upload.received_paths)
        )
        try:
            if upload.mode == "bulk":
                deps.logger.info("bulk discovery started upload_id=%s source=%s", upload_id, source_path)
                upload.scan_progress = {
                    "phase": "discovering", "current": 0, "total": 0,
                    "message": "Discovering comics",
                }
                source, candidates = await deps.run_background_io(deps.discover_artist_comics, source_path)
                deps.logger.info("bulk discovery complete upload_id=%s candidates=%d", upload_id, len(candidates))
                bulk_id = str(uuid4())
                app.state.bulk_import_sessions[bulk_id] = deps.bulk_artist_session(
                    source=source,
                    author=source.name,
                    candidates=candidates,
                    upload_root=upload.upload_root,
                )
                deps.touch_bulk_activity(app, app.state.bulk_import_sessions[bulk_id])
                redirect = f"/import/bulk/{bulk_id}"
            else:
                pdf_cache_root = deps.new_pdf_cache(staging, upload.upload_root)
                deps.logger.info("scan requested upload_id=%s source=%s", upload_id, source_path)
                deps.log_memory_checkpoint("scan-start", upload_id=upload_id)
                last_scan_phase: list[str | None] = [None]

                def scan_progress(phase: str, current: int, total: int, message: str) -> None:
                    upload.scan_progress = {
                        "phase": phase, "current": current, "total": total, "message": message,
                    }
                    if phase != last_scan_phase[0] or (
                        current > 0 and (current == total or current % 1000 == 0)
                    ):
                        deps.logger.info(
                            "scan progress upload_id=%s phase=%s current=%d total=%d message=%s",
                            upload_id, phase, current, total, message,
                        )
                        deps.log_memory_checkpoint(
                            f"scan-{phase}", upload_id=upload_id, current=current, total=total
                        )
                        last_scan_phase[0] = phase

                scan = await deps.run_background_io(
                    deps.scan_folder,
                    source_path,
                    pdf_cache_root=pdf_cache_root,
                    progress=scan_progress,
                )
                deps.log_memory_checkpoint("scan-complete", upload_id=upload_id)
                upload.scan_progress = {
                    "phase": "review", "current": 0, "total": 0,
                    "message": "Building review workspace",
                }
                deps.log_memory_checkpoint("review-plan-start", upload_id=upload_id)
                plan = await deps.run_background_io(deps.build_review_plan, scan)
                deps.logger.info("review plan complete upload_id=%s items=%d", upload_id, len(plan.items))
                deps.log_memory_checkpoint(
                    "review-plan-complete", upload_id=upload_id, review_items=len(plan.items)
                )
                session_id = str(uuid4())
                app.state.import_sessions[session_id] = deps.import_session(
                    plan=plan,
                    series_default=scan.content_root.name,
                    upload_root=upload.upload_root,
                    pdf_cache_root=pdf_cache_root,
                )
                deps.touch_import_activity(app, app.state.import_sessions[session_id])
                redirect = f"/import/{session_id}/review"
        except (deps.folder_scan_error, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        app.state.upload_sessions.pop(upload_id, None)
        deps.delete_browser_upload_session_state(app, upload_id)
        deps.logger.info("upload finalized id=%s mode=%s redirect=%s", upload_id, upload.mode, redirect)
        deps.log_memory_checkpoint("upload-finalized", upload_id=upload_id, mode=upload.mode)
        return JSONResponse({"redirect": redirect})
