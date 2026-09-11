from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException

from ..importer.review import build_review_plan
from ..importer.scanner import scan_folder
from ..importer.staging import StagedImport
from .import_sessions import (
    ImportSession, BulkArtistSession,
    remove_staging_record as _remove_staging_record_service,
    cleanup_stale_imports as _cleanup_stale_imports_service,
    session_or_404 as _session_or_404_service,
    bulk_session_or_404 as _bulk_session_or_404_service,
    touch_import_activity as _touch_import_activity,
)
from .uploads import (
    cleanup_upload as _cleanup_upload,
    delete_browser_upload_session_state as _delete_browser_upload_session_state,
    new_pdf_cache as _new_pdf_cache,
)

def _existing_commit_result(database: Path, staged: StagedImport) -> dict | None:
    try:
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT id, author_id, series_id FROM imports WHERE staging_id = ?",
                (staged.staging_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "import_id": row[0],
            "author_id": row[1],
            "series_id": row[2],
            "copied_files": sum(len(group.media) for issue in staged.issues for group in issue.groups)
                + sum(len(group.media) for group in staged.series_extras),
        }
    except sqlite3.Error:
        return None



def _remove_staging_record(session: ImportSession) -> None:
    _remove_staging_record_service(session, _cleanup_upload)


def _cleanup_stale_imports(app: FastAPI, staging_root: Path, *, now: float | None = None) -> None:
    _cleanup_stale_imports_service(
        app, staging_root, now=now,
        delete_browser_upload_state=_delete_browser_upload_session_state,
        cleanup_upload=_cleanup_upload,
    )


def _session_or_404(app: FastAPI, session_id: str) -> ImportSession:
    return _session_or_404_service(app, session_id, cleanup_stale=_cleanup_stale_imports)


def _bulk_session_or_404(app: FastAPI, bulk_id: str) -> BulkArtistSession:
    return _bulk_session_or_404_service(app, bulk_id, cleanup_stale=_cleanup_stale_imports)


def _start_bulk_item(app: FastAPI, bulk_id: str) -> str | None:
    bulk = _bulk_session_or_404(app, bulk_id)
    if bulk.current_position >= len(bulk.selected):
        return None
    candidate_index = bulk.selected[bulk.current_position]
    candidate = bulk.candidates[candidate_index]
    pdf_cache_root = _new_pdf_cache(app.state.staging_root, bulk.upload_root)
    scan_source = candidate.path
    if candidate.path.is_file() and candidate.path.suffix.casefold() == ".pdf":
        wrapper = pdf_cache_root / "source" / candidate.path.stem
        wrapper.mkdir(parents=True, exist_ok=True)
        wrapped_pdf = wrapper / candidate.path.name
        shutil.copy2(candidate.path, wrapped_pdf)
        scan_source = wrapper
    try:
        scan = scan_folder(scan_source, pdf_cache_root=pdf_cache_root)
    except Exception:
        _cleanup_upload(pdf_cache_root)
        raise
    session_id = str(uuid4())
    app.state.import_sessions[session_id] = ImportSession(
        plan=build_review_plan(scan),
        author_default=bulk.author,
        series_default=candidate.name,
        bulk_id=bulk_id,
        upload_root=bulk.upload_root,
        pdf_cache_root=pdf_cache_root,
    )
    _touch_import_activity(app, app.state.import_sessions[session_id])
    return session_id



def _safe_import_folder(import_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path or ".").expanduser()
    candidate = relative.resolve() if relative.is_absolute() else (import_root / relative).resolve()
    try:
        candidate.relative_to(import_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Import folder must be inside the configured import root") from exc
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Import folder not found")
    return candidate


def _import_folder_listing(import_root: Path, relative_path: str) -> tuple[Path, str, list[dict[str, str]], str | None]:
    current = _safe_import_folder(import_root, relative_path)
    relative = current.relative_to(import_root)
    relative_text = "." if relative == Path(".") else relative.as_posix()
    folders = [
        {
            "name": child.name,
            "path": child.relative_to(import_root).as_posix(),
        }
        for child in sorted(
            (child for child in current.iterdir() if child.is_dir() and not child.name.startswith(".")),
            key=lambda child: child.name.casefold(),
        )
    ]
    parent = None
    if current != import_root:
        parent_path = current.parent.relative_to(import_root)
        parent = "." if parent_path == Path(".") else parent_path.as_posix()
    return current, relative_text, folders, parent


