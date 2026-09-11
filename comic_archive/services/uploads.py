from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from starlette.datastructures import UploadFile

from ..web_forms import check_csrf_value
from .diagnostics import logger, process_rss_mib
from .import_sessions import atomic_json_write, session_state_path, touch_upload_root

UPLOAD_INACTIVITY_SECONDS = 12 * 60 * 60
MAX_BROWSER_UPLOAD_FILE_BYTES = 16 * 1024 * 1024 * 1024
UPLOAD_STATE_CHECKPOINT_FILES = 25
UPLOAD_STATE_CHECKPOINT_SECONDS = 2.0
UPLOAD_WRITE_BUFFER_BYTES = 4 * 1024 * 1024


@dataclass
class BrowserUploadSession:
    upload_root: Path
    mode: str
    expected_files: int
    received_paths: set[str] = field(default_factory=set)
    last_activity: float = field(default_factory=time.time)
    cancelled: bool = False
    scan_progress: dict[str, object] = field(default_factory=dict)
    last_persisted_count: int = 0
    last_persisted_at: float = field(default_factory=time.monotonic)
    uploaded_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic)


def safe_upload_relative(filename: str) -> Path:
    normalized = filename.replace("\\", "/").lstrip("/")
    relative = PurePosixPath(normalized)
    if not normalized or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid uploaded folder path")
    return Path(*relative.parts)


def persist_browser_upload_session(app: FastAPI, upload_id: str, session: BrowserUploadSession) -> None:
    atomic_json_write(session_state_path(app, "browser_upload", upload_id), {
        "version": 1,
        "upload_root": str(session.upload_root),
        "mode": session.mode,
        "expected_files": session.expected_files,
        "received_paths": sorted(session.received_paths),
        "last_activity": session.last_activity,
        "cancelled": session.cancelled,
        "scan_progress": session.scan_progress,
    })


def reconcile_browser_upload_files(session: BrowserUploadSession) -> None:
    content_root = session.upload_root / "content"
    if not content_root.is_dir():
        return
    recovered: set[str] = set()
    for root, _dirs, files in os.walk(content_root):
        root_path = Path(root)
        for name in files:
            if name.endswith(".part"):
                continue
            path = root_path / name
            try:
                relative = path.relative_to(content_root).as_posix()
            except ValueError:
                continue
            recovered.add(relative)
    if recovered:
        session.received_paths.update(recovered)


def checkpoint_browser_upload_session(
    app: FastAPI,
    upload_id: str,
    session: BrowserUploadSession,
    *,
    force: bool = False,
) -> None:
    now = time.monotonic()
    count = len(session.received_paths)
    if not force:
        if (
            count - session.last_persisted_count < UPLOAD_STATE_CHECKPOINT_FILES
            and now - session.last_persisted_at < UPLOAD_STATE_CHECKPOINT_SECONDS
        ):
            return
    persist_browser_upload_session(app, upload_id, session)
    session.last_persisted_count = count
    session.last_persisted_at = now


def restore_browser_upload_session(app: FastAPI, upload_id: str) -> BrowserUploadSession | None:
    path = session_state_path(app, "browser_upload", upload_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_activity = float(data["last_activity"])
        if time.time() - last_activity >= UPLOAD_INACTIVITY_SECONDS:
            return None
        expected_root = (app.state.staging_root / "uploads" / upload_id).resolve()
        upload_root = Path(data["upload_root"]).resolve()
        if upload_root != expected_root or not (upload_root / "content").is_dir():
            return None
        mode = str(data["mode"])
        expected_files = int(data["expected_files"])
        if mode not in {"single", "bulk"} or expected_files < 1 or expected_files > 100000:
            return None
        session = BrowserUploadSession(
            upload_root=upload_root,
            mode=mode,
            expected_files=expected_files,
            received_paths={str(value) for value in data.get("received_paths", [])},
            last_activity=last_activity,
            cancelled=bool(data.get("cancelled", False)),
            scan_progress=dict(data.get("scan_progress", {})),
        )
        if session.cancelled:
            return None
        reconcile_browser_upload_files(session)
        session.last_persisted_count = len(session.received_paths)
        session.last_persisted_at = time.monotonic()
        app.state.upload_sessions[upload_id] = session
        return session
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def delete_browser_upload_session_state(app: FastAPI, upload_id: str) -> None:
    try:
        session_state_path(app, "browser_upload", upload_id).unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_upload(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def upload_session_or_404(app: FastAPI, upload_id: str) -> BrowserUploadSession:
    session = app.state.upload_sessions.get(upload_id)
    if session is None:
        session = restore_browser_upload_session(app, upload_id)
    if session is not None and time.time() - session.last_activity >= UPLOAD_INACTIVITY_SECONDS:
        app.state.upload_sessions.pop(upload_id, None)
        delete_browser_upload_session_state(app, upload_id)
        cleanup_upload(session.upload_root)
        session = None
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session expired or was cleaned up after 12 hours of inactivity")
    return session


def touch_browser_upload(session: BrowserUploadSession) -> None:
    session.last_activity = time.time()
    touch_upload_root(session.upload_root, session.last_activity)


async def save_upload_file(request: Request, session: BrowserUploadSession) -> str:
    if session.cancelled:
        raise HTTPException(status_code=409, detail="Upload session was cancelled")

    check_csrf_value(request, request.headers.get("x-csrf-token", ""))
    relative_text = request.query_params.get("relative_path", "").strip()
    if not relative_text:
        raise HTTPException(status_code=400, detail="Missing relative_path")
    relative = safe_upload_relative(relative_text)
    normalized = relative.as_posix()

    expected_size_text = request.headers.get("x-file-size", "")
    try:
        expected_size = int(expected_size_text) if expected_size_text else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid X-File-Size header") from exc
    if expected_size is not None and (expected_size < 0 or expected_size > MAX_BROWSER_UPLOAD_FILE_BYTES):
        raise HTTPException(status_code=413, detail="File exceeds the 16 GiB browser-upload limit")

    content_root = session.upload_root / "content"
    destination = (content_root / relative).resolve()
    try:
        destination.relative_to(content_root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid uploaded folder path") from exc

    if normalized in session.received_paths and destination.is_file():
        touch_browser_upload(session)
        logger.info(
            "upload retry already complete path=%s received=%d/%d",
            normalized,
            len(session.received_paths),
            session.expected_files,
        )
        return normalized
    if normalized not in session.received_paths and len(session.received_paths) >= session.expected_files:
        raise HTTPException(status_code=409, detail="Upload session already received its expected number of files")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    received_bytes = 0
    started = time.monotonic()
    try:
        with temporary.open("wb") as target:
            pending = bytearray()
            async for chunk in request.stream():
                if not chunk:
                    continue
                received_bytes += len(chunk)
                if received_bytes > MAX_BROWSER_UPLOAD_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="File exceeds the 16 GiB browser-upload limit")
                if expected_size is not None and received_bytes > expected_size:
                    raise HTTPException(status_code=400, detail="Received more bytes than declared by X-File-Size")
                pending.extend(chunk)
                if len(pending) >= UPLOAD_WRITE_BUFFER_BYTES:
                    payload = bytes(pending)
                    pending.clear()
                    await asyncio.to_thread(target.write, payload)
            if pending:
                await asyncio.to_thread(target.write, bytes(pending))
        if expected_size is not None and received_bytes != expected_size:
            raise HTTPException(
                status_code=400,
                detail=f"Incomplete upload body: received {received_bytes} of {expected_size} bytes",
            )
        if session.cancelled:
            temporary.unlink(missing_ok=True)
            cleanup_upload(session.upload_root)
            raise HTTPException(status_code=409, detail="Upload session was cancelled")
        temporary.replace(destination)
        session.received_paths.add(normalized)
        session.uploaded_bytes += received_bytes
        touch_browser_upload(session)
        count = len(session.received_paths)
        elapsed = time.monotonic() - started
        if count == 1 or count == session.expected_files or count % 100 == 0 or received_bytes >= 100 * 1024 * 1024:
            rss = process_rss_mib()
            logger.info(
                "upload progress path=%s bytes=%d elapsed=%.2fs received=%d/%d rss_mib=%s",
                normalized,
                received_bytes,
                elapsed,
                count,
                session.expected_files,
                f"{rss:.1f}" if rss is not None else "unknown",
            )
        return normalized
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        logger.exception(
            "upload file failed path=%s bytes_received=%d expected_size=%s",
            normalized,
            received_bytes,
            expected_size,
        )
        raise
    finally:
        if session.cancelled:
            cleanup_upload(session.upload_root)


def upload_top_level(session: BrowserUploadSession) -> str | None:
    top_levels = {Path(relative).parts[0] for relative in session.received_paths if Path(relative).parts}
    return next(iter(top_levels)) if len(top_levels) == 1 else None


async def save_uploaded_folder(request: Request, staging_root: Path) -> tuple[Path, str | None]:
    """Backward-compatible whole-folder endpoint used by older clients/tests."""
    form = await request.form(max_files=100000, max_fields=1000)
    check_csrf_value(request, str(form.get("csrf_token", "")))
    files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
    if not files:
        raise HTTPException(status_code=400, detail="Choose a folder containing files.")

    upload_root = staging_root / "uploads" / str(uuid4())
    content_root = upload_root / "content"
    content_root.mkdir(parents=True, exist_ok=False)
    top_levels: set[str] = set()
    try:
        for upload in files:
            relative = safe_upload_relative(upload.filename or "")
            top_levels.add(relative.parts[0])
            destination = (content_root / relative).resolve()
            try:
                destination.relative_to(content_root.resolve())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid uploaded folder path") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as target:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
            await upload.close()
        top_level = next(iter(top_levels)) if len(top_levels) == 1 else None
        return upload_root, top_level
    except Exception:
        shutil.rmtree(upload_root, ignore_errors=True)
        raise


def uploaded_source(upload_root: Path, top_level: str | None) -> Path:
    content = upload_root / "content"
    if top_level:
        candidate = content / top_level
        if candidate.is_dir():
            return candidate
    return content


def new_pdf_cache(staging_root: Path, upload_root: Path | None = None) -> Path:
    if upload_root is not None:
        root = upload_root / "pdf-rendered" / str(uuid4())
    else:
        root = staging_root / "pdf-rendered" / str(uuid4())
    root.mkdir(parents=True, exist_ok=False)
    return root
