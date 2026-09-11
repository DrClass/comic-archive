from __future__ import annotations

import asyncio
import sqlite3
import json
import secrets
import time
import shutil
import logging
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from starlette.datastructures import UploadFile
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
from .routes.auth import register_auth_routes
from .routes.library import register_library_routes
from .routes.editing import register_editing_routes
from .routes.maintenance import register_maintenance_routes
from .routes.media import register_media_routes
from .routes.import_uploads import ImportUploadRouteDeps, register_import_upload_routes
from .routes.import_review import ImportReviewRouteDeps, register_import_review_routes
from .routes.import_workspace import ImportWorkspaceRouteDeps, register_import_workspace_routes
from .library_views import (
    series_lineage as _series_lineage,
)
from .web_forms import form_data as _form_data, check_csrf_value as _check_csrf_value, bool_form as _bool_form


_TEMPLATE_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

UPLOAD_INACTIVITY_SECONDS = 12 * 60 * 60
UPLOAD_SWEEP_INTERVAL_SECONDS = 60 * 60
MAX_BROWSER_UPLOAD_FILE_BYTES = 16 * 1024 * 1024 * 1024
UPLOAD_STATE_CHECKPOINT_FILES = 25
UPLOAD_STATE_CHECKPOINT_SECONDS = 2.0
UPLOAD_WRITE_BUFFER_BYTES = 4 * 1024 * 1024
logger = logging.getLogger("comic_archive.web")


def _configure_diagnostic_logging() -> None:
    """Ensure importer diagnostics always reach stderr/journald at INFO."""
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "_comic_archive_diag", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._comic_archive_diag = True  # type: ignore[attr-defined]
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("CA_DIAG %(message)s"))
        logger.addHandler(handler)


_configure_diagnostic_logging()


def _process_memory_snapshot() -> dict[str, float | int | None]:
    values: dict[str, float | int | None] = {
        "rss_mib": None, "anon_mib": None, "file_mib": None,
        "vmsize_mib": None, "threads": None,
    }
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if key == "Threads" and parts:
                values["threads"] = int(parts[0])
            elif key in {"VmRSS", "RssAnon", "RssFile", "VmSize"} and parts:
                mapped = {"VmRSS": "rss_mib", "RssAnon": "anon_mib", "RssFile": "file_mib", "VmSize": "vmsize_mib"}[key]
                values[mapped] = int(parts[0]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return values


def _process_rss_mib() -> float | None:
    value = _process_memory_snapshot().get("rss_mib")
    return float(value) if value is not None else None


def _log_memory_checkpoint(label: str, **context: object) -> None:
    memory = _process_memory_snapshot()
    suffix = " ".join(f"{key}={value}" for key, value in context.items())
    logger.info(
        "memory checkpoint label=%s rss_mib=%s anon_mib=%s file_mib=%s vmsize_mib=%s threads=%s%s%s",
        label,
        f"{memory['rss_mib']:.1f}" if isinstance(memory['rss_mib'], float) else "unknown",
        f"{memory['anon_mib']:.1f}" if isinstance(memory['anon_mib'], float) else "unknown",
        f"{memory['file_mib']:.1f}" if isinstance(memory['file_mib'], float) else "unknown",
        f"{memory['vmsize_mib']:.1f}" if isinstance(memory['vmsize_mib'], float) else "unknown",
        memory['threads'] if memory['threads'] is not None else "unknown",
        " " if suffix else "", suffix,
    )


async def _run_background_io(func, /, *args, **kwargs):
    """Run one long import operation without blocking ASGI or holding process shutdown open.

    Import scans are coarse, infrequent jobs. A short-lived daemon thread avoids
    tying the single Uvicorn event loop to filesystem/PDF work and also avoids
    per-TestClient executor threads lingering after tests finish.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def runner() -> None:
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            loop.call_soon_threadsafe(future.set_exception, exc)
        else:
            loop.call_soon_threadsafe(future.set_result, result)

    thread = threading.Thread(target=runner, name=f"comic-archive-{getattr(func, '__name__', 'worker')}", daemon=True)
    thread.start()
    return await future


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


@dataclass
class ImportSession:
    plan: ReviewPlan
    staged: StagedImport | None = None
    staging_path: Path | None = None
    author_default: str | None = None
    series_default: str | None = None
    bulk_id: str | None = None
    upload_root: Path | None = None
    pdf_cache_root: Path | None = None
    last_activity: float = field(default_factory=time.time)
    extra_folder_overrides: set[str] = field(default_factory=set)
    subseries_folder_overrides: set[str] = field(default_factory=set)
    primary_folder_overrides: set[str] = field(default_factory=set)
    container_folder_overrides: set[str] = field(default_factory=set)
    folder_order_overrides: dict[str, list[str]] = field(default_factory=dict)
    workspace_author: str | None = None
    workspace_series: str | None = None
    workspace_series_complete: str = ""
    workspace_metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    workspace_virtual_groups: dict[str, dict[str, str]] = field(default_factory=dict)
    workspace_virtual_nodes: dict[str, dict[str, str]] = field(default_factory=dict)
    workspace_media_targets: dict[str, str] = field(default_factory=dict)
    workspace_role_overrides: dict[str, str] = field(default_factory=dict)
    workspace_parent_overrides: dict[str, str] = field(default_factory=dict)
    workspace_ignored_media: set[str] = field(default_factory=set)
    # Runtime virtual-workspace cache. The filesystem/scanner seeds this once;
    # normal workspace reads operate from these indexes until an edit changes
    # the lightweight workspace signature.
    workspace_cache_signature: str | None = None
    workspace_cached_tree: list[dict[str, object]] | None = None
    workspace_cached_folder_media: dict[str, list[str]] = field(default_factory=dict)
    workspace_cached_media_index: dict[str, object] = field(default_factory=dict)
    workspace_cached_media_owner: dict[str, str] = field(default_factory=dict)
    workspace_cache_builds: int = 0
    workspace_ownership_dirty: bool = False


@dataclass
class BulkArtistSession:
    source: Path
    author: str
    candidates: list[BulkComicCandidate]
    selected: list[int] = field(default_factory=list)
    current_position: int = 0
    imported_series: list[tuple[str, str]] = field(default_factory=list)
    skipped_series: list[str] = field(default_factory=list)
    upload_root: Path | None = None
    last_activity: float = field(default_factory=time.time)


def _safe_upload_relative(filename: str) -> Path:
    normalized = filename.replace("\\", "/").lstrip("/")
    relative = PurePosixPath(normalized)
    if not normalized or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid uploaded folder path")
    return Path(*relative.parts)


def _persist_browser_upload_session(app: FastAPI, upload_id: str, session: BrowserUploadSession) -> None:
    _atomic_json_write(_session_state_path(app, "browser_upload", upload_id), {
        "version": 1,
        "upload_root": str(session.upload_root),
        "mode": session.mode,
        "expected_files": session.expected_files,
        "received_paths": sorted(session.received_paths),
        "last_activity": session.last_activity,
        "cancelled": session.cancelled,
        "scan_progress": session.scan_progress,
    })




def _reconcile_browser_upload_files(session: BrowserUploadSession) -> None:
    """Recover completed files from disk after a process restart.

    Browser upload state is checkpointed periodically for throughput. Completed
    files written since the last checkpoint are authoritative on disk and are
    added back to received_paths here. .part files are deliberately ignored.
    """
    content_root = session.upload_root / "content"
    if not content_root.is_dir():
        return
    recovered: set[str] = set()
    for root, _dirs, files in __import__("os").walk(content_root):
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


def _checkpoint_browser_upload_session(app: FastAPI, upload_id: str, session: BrowserUploadSession, *, force: bool = False) -> None:
    now = time.monotonic()
    count = len(session.received_paths)
    if not force:
        if count - session.last_persisted_count < UPLOAD_STATE_CHECKPOINT_FILES and now - session.last_persisted_at < UPLOAD_STATE_CHECKPOINT_SECONDS:
            return
    _persist_browser_upload_session(app, upload_id, session)
    session.last_persisted_count = count
    session.last_persisted_at = now


def _restore_browser_upload_session(app: FastAPI, upload_id: str) -> BrowserUploadSession | None:
    path = _session_state_path(app, "browser_upload", upload_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_activity = float(data.get("last_activity", 0))
        if time.time() - last_activity >= UPLOAD_INACTIVITY_SECONDS:
            path.unlink(missing_ok=True)
            return None
        expected_root = (app.state.staging_root / "uploads" / upload_id).resolve()
        upload_root = Path(data["upload_root"]).resolve()
        if upload_root != expected_root or not (upload_root / "content").is_dir():
            return None
        mode = str(data["mode"] )
        expected_files = int(data["expected_files"] )
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
        _reconcile_browser_upload_files(session)
        session.last_persisted_count = len(session.received_paths)
        session.last_persisted_at = time.monotonic()
        app.state.upload_sessions[upload_id] = session
        return session
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _delete_browser_upload_session_state(app: FastAPI, upload_id: str) -> None:
    try:
        _session_state_path(app, "browser_upload", upload_id).unlink(missing_ok=True)
    except OSError:
        pass


def _upload_session_or_404(app: FastAPI, upload_id: str) -> BrowserUploadSession:
    session = app.state.upload_sessions.get(upload_id)
    if session is None:
        session = _restore_browser_upload_session(app, upload_id)
    if session is not None and time.time() - session.last_activity >= UPLOAD_INACTIVITY_SECONDS:
        app.state.upload_sessions.pop(upload_id, None)
        _delete_browser_upload_session_state(app, upload_id)
        _cleanup_upload(session.upload_root)
        session = None
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session expired or was cleaned up after 12 hours of inactivity")
    return session


def _touch_browser_upload(session: BrowserUploadSession) -> None:
    session.last_activity = time.time()
    _touch_upload_root(session.upload_root, session.last_activity)


async def _save_upload_file(request: Request, session: BrowserUploadSession) -> str:
    if session.cancelled:
        raise HTTPException(status_code=409, detail="Upload session was cancelled")

    _check_csrf_value(request, request.headers.get("x-csrf-token", ""))
    relative_text = request.query_params.get("relative_path", "").strip()
    if not relative_text:
        raise HTTPException(status_code=400, detail="Missing relative_path")
    relative = _safe_upload_relative(relative_text)
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
        _touch_browser_upload(session)
        logger.info("upload retry already complete path=%s received=%d/%d", normalized, len(session.received_paths), session.expected_files)
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
            raise HTTPException(status_code=400, detail=f"Incomplete upload body: received {received_bytes} of {expected_size} bytes")
        if session.cancelled:
            temporary.unlink(missing_ok=True)
            _cleanup_upload(session.upload_root)
            raise HTTPException(status_code=409, detail="Upload session was cancelled")
        temporary.replace(destination)
        session.received_paths.add(normalized)
        session.uploaded_bytes += received_bytes
        _touch_browser_upload(session)
        count = len(session.received_paths)
        elapsed = time.monotonic() - started
        if count == 1 or count == session.expected_files or count % 100 == 0 or received_bytes >= 100 * 1024 * 1024:
            logger.info(
                "upload progress path=%s bytes=%d elapsed=%.2fs received=%d/%d rss_mib=%s",
                normalized, received_bytes, elapsed, count, session.expected_files,
                f"{_process_rss_mib():.1f}" if _process_rss_mib() is not None else "unknown",
            )
        return normalized
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        logger.exception("upload file failed path=%s bytes_received=%d expected_size=%s", normalized, received_bytes, expected_size)
        raise
    finally:
        if session.cancelled:
            _cleanup_upload(session.upload_root)


def _upload_top_level(session: BrowserUploadSession) -> str | None:
    top_levels = {
        Path(relative).parts[0]
        for relative in session.received_paths
        if Path(relative).parts
    }
    return next(iter(top_levels)) if len(top_levels) == 1 else None


async def _save_uploaded_folder(request: Request, staging_root: Path) -> tuple[Path, str | None]:
    """Backward-compatible whole-folder endpoint used by older clients/tests.

    The browser UI uses the resumable upload-session endpoints instead.
    """
    form = await request.form(max_files=100000, max_fields=1000)
    _check_csrf_value(request, str(form.get("csrf_token", "")))
    files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
    if not files:
        raise HTTPException(status_code=400, detail="Choose a folder containing files.")

    upload_root = staging_root / "uploads" / str(uuid4())
    content_root = upload_root / "content"
    content_root.mkdir(parents=True, exist_ok=False)
    top_levels: set[str] = set()
    try:
        for upload in files:
            relative = _safe_upload_relative(upload.filename or "")
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


def _uploaded_source(upload_root: Path, top_level: str | None) -> Path:
    content = upload_root / "content"
    if top_level:
        candidate = content / top_level
        if candidate.is_dir():
            return candidate
    return content


def _cleanup_upload(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)



def _new_pdf_cache(staging_root: Path, upload_root: Path | None = None) -> Path:
    if upload_root is not None:
        root = upload_root / "pdf-rendered" / str(uuid4())
    else:
        root = staging_root / "pdf-rendered" / str(uuid4())
    root.mkdir(parents=True, exist_ok=False)
    return root

def _review_role_choices(item, *, is_series: bool) -> list[ReviewRole]:
    if item.source_kind == "subseries":
        return [ReviewRole.SUBSERIES]
    if item.source_kind == "issue":
        return [ReviewRole.ISSUE, ReviewRole.SERIES_EXTRA]
    if is_series and item.issue_path is None:
        return [ReviewRole.SERIES_EXTRA, ReviewRole.ISSUE]
    if is_series:
        return [ReviewRole.ISSUE_EXTRA, ReviewRole.PRIMARY, ReviewRole.SERIES_EXTRA]
    return [ReviewRole.ISSUE_EXTRA, ReviewRole.PRIMARY, ReviewRole.SERIES_EXTRA]


def _review_source_relative(scan, relative_path: Path) -> str:
    prefix = scan.content_root.relative_to(scan.source)
    return str(prefix / relative_path) if prefix != Path('.') else str(relative_path)


def _workspace_source_relative(scan, path: Path) -> str:
    rel = path.resolve().relative_to(scan.source.resolve())
    return "." if rel == Path(".") else rel.as_posix()


def _workspace_content_relative(scan, source_relative: str) -> Path | None:
    source_path = (scan.source / source_relative).resolve() if source_relative != "." else scan.source.resolve()
    try:
        return source_path.relative_to(scan.content_root.resolve())
    except ValueError:
        return None


def _workspace_role_map(plan: ReviewPlan) -> dict[str, tuple[str, object | None]]:
    scan = plan.scan
    roles: dict[str, tuple[str, object | None]] = {}
    root_key = _workspace_source_relative(scan, scan.content_root)
    roles[root_key] = ("Series" if scan.is_series_candidate else "Issue", None)
    for item in plan.items:
        key = _review_source_relative(scan, item.relative_path).replace("\\", "/")
        label = {
            ReviewRole.SUBSERIES: "Sub-Series",
            ReviewRole.ISSUE: "Issue",
            ReviewRole.ISSUE_EXTRA: "Issue-Extras",
            ReviewRole.SERIES_EXTRA: "Series-Extras",
            ReviewRole.PRIMARY: "Primary Pages",
        }[item.role]
        if key == root_key and item.role is ReviewRole.PRIMARY:
            continue
        roles[key] = (label, item)
    for candidate in flattened_folder_candidates(scan):
        key = str(candidate.relative_path).replace("\\", "/")
        roles.setdefault(key, ("Primary Pages", None))
    return roles


def _workspace_has_media(path: Path) -> bool:
    try:
        return any(
            child.is_file() and child.suffix.casefold() in SUPPORTED_MEDIA
            for child in path.rglob("*")
        )
    except OSError:
        return False


def _workspace_direct_media_count(path: Path) -> int:
    try:
        return sum(
            1 for child in path.iterdir()
            if child.is_file() and child.suffix.casefold() in SUPPORTED_MEDIA
        )
    except OSError:
        return 0


def _workspace_sort_children(session: ImportSession, parent_key: str, children: list[Path]) -> list[Path]:
    override = session.folder_order_overrides.get(parent_key, [])
    rank = {value: index for index, value in enumerate(override)}
    return sorted(
        children,
        key=lambda child: (
            rank.get(_workspace_source_relative(session.plan.scan, child), len(rank) + 1),
            natural_text_key(child.name),
        ),
    )


def _workspace_tree_uncached(session: ImportSession, *, include_virtual: bool = True) -> list[dict[str, object]]:
    """Return the editable virtual import tree.

    The scanned filesystem seeds this tree, but workspace role/parent overrides and
    workspace-created folders become authoritative afterwards. Nothing here moves
    source files on disk.
    """
    scan = session.plan.scan
    role_map = _workspace_role_map(session.plan)
    root = scan.content_root.resolve()
    root_key = _workspace_source_relative(scan, root)
    nodes_by_key: dict[str, dict[str, object]] = {}

    source_paths = [root]
    try:
        source_paths.extend(
            path for path in root.rglob("*")
            if path.is_dir() and _workspace_has_media(path)
        )
    except OSError:
        pass
    source_paths = sorted(set(source_paths), key=lambda path: (len(path.parts), natural_text_key(path.as_posix())))
    included_keys = {_workspace_source_relative(scan, path) for path in source_paths}

    def source_parent(path: Path) -> str | None:
        if path == root:
            return None
        parent = path.parent
        while True:
            key = _workspace_source_relative(scan, parent)
            if key in included_keys:
                return key
            if parent == root or parent == parent.parent:
                return root_key
            parent = parent.parent

    for path in source_paths:
        key = _workspace_source_relative(scan, path)
        base_role, item = role_map.get(key, ("Container", None))
        if key in session.container_folder_overrides:
            base_role = "Container"
        role = session.workspace_role_overrides.get(key, base_role)
        parent = session.workspace_parent_overrides.get(key, source_parent(path))
        nodes_by_key[key] = {
            "path": key, "name": path.name, "role": role,
            "direct_files": _workspace_direct_media_count(path), "is_root": key == root_key,
            "item": item, "parent": parent, "virtual": False, "synthetic": False,
        }

    # Scanner-created logical nodes (for example one issue per loose root PDF)
    # may not correspond to a physical directory. Expose them in the virtual
    # tree exactly like filesystem folders so they can be edited and moved.
    for key, (base_role, item) in role_map.items():
        if key in nodes_by_key or item is None:
            continue
        role = session.workspace_role_overrides.get(key, base_role)
        parent = root_key
        source_kind = getattr(item, "source_kind", None)
        if source_kind == "issue":
            series_path = str(getattr(item, "series_path", Path("."))).replace("\\", "/")
            parent = root_key if series_path in {"", "."} else _review_source_relative(scan, Path(series_path)).replace("\\", "/")
        elif source_kind == "subseries":
            parent_path = str(getattr(item, "series_path", Path("."))).replace("\\", "/")
            parent = root_key if parent_path in {"", "."} else _review_source_relative(scan, Path(parent_path)).replace("\\", "/")
        elif source_kind == "group":
            issue_path = getattr(item, "issue_path", None)
            series_path = getattr(item, "series_path", Path("."))
            if issue_path is not None:
                parent = _review_source_relative(scan, Path(issue_path)).replace("\\", "/")
            elif str(series_path) not in {"", "."}:
                parent = _review_source_relative(scan, Path(series_path)).replace("\\", "/")
        parent = session.workspace_parent_overrides.get(key, parent)
        media_count = len(_workspace_media_for_folder_base(session, key)) if False else 0
        nodes_by_key[key] = {
            "path": key, "name": getattr(item, "name", Path(key).name), "role": role,
            "direct_files": 0, "is_root": False, "item": item, "parent": parent,
            "virtual": True, "synthetic": False, "seeded": True,
        }

    if include_virtual:
        for node_id, data in session.workspace_virtual_nodes.items():
            key = f"synthetic:{node_id}"
            legacy_kind = data.get("kind", "folder")
            default_role = "Issue" if legacy_kind == "issue" else ("Sub-Series" if legacy_kind == "sub-series" else "Unassigned")
            role = session.workspace_role_overrides.get(key, data.get("role", default_role))
            parent = session.workspace_parent_overrides.get(key, data.get("parent", root_key))
            nodes_by_key[key] = {
                "path": key, "name": data.get("name", "New folder"), "role": role,
                "direct_files": sum(1 for target in session.workspace_media_targets.values() if target == key),
                "is_root": False, "item": None, "parent": parent, "virtual": True, "synthetic": True,
            }
        # Legacy custom issue-extra groups are also ordinary virtual folders in
        # the new tree. Keep their stored shape for backward compatibility.
        for group_id, group in session.workspace_virtual_groups.items():
            key = f"virtual:{group_id}"
            nodes_by_key[key] = {
                "path": key, "name": group["name"],
                "role": session.workspace_role_overrides.get(key, "Issue-Extras"),
                "direct_files": sum(1 for target in session.workspace_media_targets.values() if target == key),
                "is_root": False, "item": None,
                "parent": session.workspace_parent_overrides.get(key, group.get("owner", root_key)),
                "virtual": True, "synthetic": False,
            }

    for key, node in nodes_by_key.items():
        display_name = session.workspace_metadata.get(key, {}).get("display_name", "").strip()
        if display_name:
            node["name"] = display_name

    # Drop impossible parent references back to the physical root. Cycles are
    # rejected by the move endpoint, but this keeps old/recovered state usable.
    for key, node in nodes_by_key.items():
        if key == root_key:
            node["parent"] = None
        elif node.get("parent") not in nodes_by_key:
            node["parent"] = root_key

    children: dict[str | None, list[str]] = {}
    for key, node in nodes_by_key.items():
        children.setdefault(node.get("parent"), []).append(key)

    def child_sort(parent: str | None, key: str):
        override = session.folder_order_overrides.get(parent or ".", [])
        try:
            rank = override.index(key)
        except ValueError:
            rank = len(override) + 1000
        node = nodes_by_key[key]
        virtual_order = 999999
        if key.startswith("synthetic:"):
            virtual_order = int(session.workspace_virtual_nodes.get(key.split(":",1)[1], {}).get("sort_order", "999999"))
        return (rank, virtual_order, natural_text_key(str(node["name"])))

    for parent in children:
        children[parent].sort(key=lambda key: child_sort(parent, key))

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    def visit(key: str, depth: int) -> None:
        if key in seen:
            return
        seen.add(key)
        node = dict(nodes_by_key[key])
        kids = children.get(key, [])
        node["depth"] = depth
        node["child_count"] = len(kids)
        result.append(node)
        for child in kids:
            visit(child, depth + 1)

    visit(root_key, 0)
    # Recovered malformed state should remain visible instead of disappearing.
    for key in nodes_by_key:
        if key not in seen:
            visit(key, 1)

    return result


def _workspace_cache_state_signature(session: ImportSession) -> str:
    """Cheap signature of logical workspace state; no filesystem access."""
    payload = {
        "folder_order": session.folder_order_overrides,
        "metadata_names": {k: v.get("display_name", "") for k, v in session.workspace_metadata.items()},
        "virtual_groups": session.workspace_virtual_groups,
        "virtual_nodes": session.workspace_virtual_nodes,
        "media_targets": session.workspace_media_targets,
        "role_overrides": session.workspace_role_overrides,
        "parent_overrides": session.workspace_parent_overrides,
        "ignored_media": sorted(session.workspace_ignored_media),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _workspace_invalidate_cache(session: ImportSession) -> None:
    session.workspace_cache_signature = None
    session.workspace_cached_tree = None
    session.workspace_cached_folder_media.clear()
    session.workspace_cached_media_index.clear()
    session.workspace_cached_media_owner.clear()
    session.workspace_ownership_dirty = False


def _workspace_ensure_cache(session: ImportSession) -> None:
    signature = _workspace_cache_state_signature(session)
    if session.workspace_cached_tree is not None and session.workspace_cache_signature == signature:
        return

    # This is the one expensive ownership pass. It happens after ingestion or a
    # real workspace edit, never merely because the user clicked another folder.
    cache_started = time.monotonic()
    _log_memory_checkpoint("workspace-cache-start", cache_build=session.workspace_cache_builds + 1)
    tree = _workspace_tree_uncached(session)
    media_items = _workspace_all_media(session)
    media_index = {str(item.path): item for item in media_items}
    by_folder: dict[str, list[str]] = {str(node["path"]): [] for node in tree}
    owner_by_media: dict[str, str] = {}
    for item in media_items:
        media_path = str(item.path)
        if media_path in session.workspace_ignored_media:
            continue
        owner = session.workspace_media_targets.get(media_path)
        if owner is None:
            owner = _workspace_origin_for_media_in_tree(session, media_path, tree)
        if owner in by_folder:
            by_folder[owner].append(media_path)
            owner_by_media[media_path] = owner

    for paths in by_folder.values():
        paths.sort(key=lambda value: getattr(media_index[value], "order", 0))
    for node in tree:
        node["direct_files"] = len(by_folder.get(str(node["path"]), []))

    session.workspace_cached_tree = tree
    session.workspace_cached_folder_media = by_folder
    session.workspace_cached_media_index = media_index
    session.workspace_cached_media_owner = owner_by_media
    session.workspace_ownership_dirty = False
    session.workspace_cache_signature = signature
    session.workspace_cache_builds += 1
    logger.info(
        "workspace cache built build=%d nodes=%d media=%d owners=%d folders=%d elapsed=%.2fs",
        session.workspace_cache_builds, len(tree), len(media_items), len(owner_by_media), len(by_folder), time.monotonic() - cache_started,
    )
    _log_memory_checkpoint(
        "workspace-cache-complete", cache_build=session.workspace_cache_builds,
        nodes=len(tree), media=len(media_items), owners=len(owner_by_media), folders=len(by_folder),
    )


def _workspace_ensure_authoritative_cache(session: ImportSession) -> None:
    if session.workspace_ownership_dirty:
        _workspace_invalidate_cache(session)
    _workspace_ensure_cache(session)


def _workspace_tree(session: ImportSession, *, include_virtual: bool = True) -> list[dict[str, object]]:
    # include_virtual=False is only used by legacy ownership helpers during a
    # rebuild. Normal UI/staging reads always consume the virtual cache.
    if not include_virtual:
        return _workspace_tree_uncached(session, include_virtual=False)
    _workspace_ensure_cache(session)
    return session.workspace_cached_tree or []



def _workspace_item_key(session: ImportSession, item) -> str:
    return _review_source_relative(session.plan.scan, item.relative_path).replace("\\", "/")


def _workspace_node_metadata(session: ImportSession, node: dict[str, object]) -> dict[str, str]:
    key = str(node["path"])
    saved = dict(session.workspace_metadata.get(key, {}))
    role = str(node["role"])
    item = node.get("item")
    if role == "Issue":
        default_label = getattr(item, "name", None) or str(node["name"])
        if node.get("synthetic"):
            default_label = saved.get("issue_number", "")
        saved.setdefault("issue_number", default_label)
        saved.setdefault("title", "")
        saved.setdefault("complete", "")
    elif role == "Sub-Series":
        saved.setdefault("title", getattr(item, "name", None) or str(node["name"]))
        saved.setdefault("complete", "")
    elif role in {"Issue-Extras", "Series-Extras"}:
        saved.setdefault("title", getattr(item, "name", None) or str(node["name"]))
    return saved


def _workspace_virtual_node(session: ImportSession, group_id: str, group: dict[str, str], depth: int) -> dict[str, object]:
    key = f"virtual:{group_id}"
    return {
        "path": key,
        "name": group["name"],
        "depth": depth,
        "role": "Issue-Extras",
        "direct_files": sum(1 for target in session.workspace_media_targets.values() if target == key),
        "child_count": 0,
        "is_root": False,
        "item": None,
        "parent": group["owner"],
        "virtual": True,
    }


def _workspace_all_media(session: ImportSession) -> list[object]:
    result: list[object] = []
    scan = session.plan.scan
    if scan.primary:
        result.extend(scan.primary.media)
    for group in scan.extras:
        result.extend(group.media)
    for issue in scan.issues:
        if issue.primary:
            result.extend(issue.primary.media)
        for group in issue.extras:
            result.extend(group.media)
    seen: set[str] = set()
    unique = []
    for item in result:
        key = str(item.path)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _workspace_origin_for_media_in_tree(session: ImportSession, media_path: str, tree: list[dict[str, object]]) -> str | None:
    # A scanned issue can contain media that also belongs to a more specific
    # nested group (for example ``Issue/Textless``). Prefer the deepest matching
    # semantic node so both the workspace UI and staging use the same ownership.
    matches: list[tuple[int, int, str]] = []
    role_rank = {
        "Issue-Extras": 5, "Series-Extras": 5, "Primary Pages": 4,
        "Issue": 3, "Sub-Series": 2, "Series": 1,
    }
    for node in tree:
        # Scanner-seeded logical nodes (notably one issue per loose PDF) are
        # virtual only because they have no physical directory. They still own
        # their scanned media. Skip only genuinely synthetic/user-created
        # virtual folders, which have no source media until files are moved in.
        if node.get("virtual") and not node.get("seeded"):
            continue
        if node["is_root"] and node["role"] not in {"Issue", "Primary Pages"}:
            continue
        role = str(node.get("role"))
        if role not in role_rank:
            continue
        for media in _workspace_media_for_folder_base(session, str(node["path"])):
            if str(media.path) == media_path:
                matches.append((int(node.get("depth", 0)), role_rank.get(role, 0), str(node["path"])))
                break
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][2]


def _workspace_origin_for_media(session: ImportSession, media_path: str) -> str | None:
    return _workspace_origin_for_media_in_tree(session, media_path, _workspace_tree(session, include_virtual=False))


def _workspace_media_targets_for_folder(session: ImportSession, folder_key: str) -> list[tuple[str, str]]:
    tree = _workspace_tree(session)
    node = next((item for item in tree if str(item["path"]) == folder_key), None)
    if node is None:
        return []
    choices: list[tuple[str, str]] = []
    for candidate in tree:
        role = str(candidate["role"])
        if role in {"Issue", "Primary Pages", "Issue-Extras", "Series-Extras"}:
            suffix = {
                "Issue": "Main comic", "Primary Pages": "Primary pages",
                "Issue-Extras": "Issue extras", "Series-Extras": "Series extras",
            }[role]
            choices.append((str(candidate["path"]), f"{candidate['name']} — {suffix}"))
    return choices


def _workspace_media_for_folder_base(session: ImportSession, folder_key: str) -> list[object]:
    scan = session.plan.scan
    content_rel = _workspace_content_relative(scan, folder_key)
    if content_rel is None:
        return []
    role_map = _workspace_role_map(session.plan)
    role_item = role_map.get(folder_key)
    item = role_item[1] if role_item else None

    if item is not None:
        if getattr(item, "source_kind", None) == "issue":
            issue = next((issue for issue in scan.issues if issue.relative_path == item.relative_path), None)
            return list(issue.primary.media) if issue and issue.primary else []
        if getattr(item, "source_kind", None) == "group":
            for issue in scan.issues:
                if issue.primary and issue.primary.relative_path == item.relative_path:
                    return list(issue.primary.media)
                for group in issue.extras:
                    if group.relative_path == item.relative_path:
                        return list(group.media)
            for group in scan.extras:
                if group.relative_path == item.relative_path:
                    return list(group.media)
        if getattr(item, "source_kind", None) == "subseries":
            return []

    # Flattened page containers do not have their own ReviewItem yet. Select
    # only the primary media whose virtual relative path sits below this folder.
    media_items = []
    if scan.is_series_candidate:
        for issue in scan.issues:
            if not issue.primary:
                continue
            issue_key = issue.relative_path
            try:
                suffix = content_rel.relative_to(issue_key)
            except ValueError:
                continue
            for media in issue.primary.media:
                if suffix == Path(".") or suffix in media.relative_path.parents or media.relative_path.parent == suffix:
                    media_items.append(media)
    elif scan.primary:
        for media in scan.primary.media:
            if content_rel == Path(".") or content_rel in media.relative_path.parents or media.relative_path.parent == content_rel:
                media_items.append(media)
    return media_items



def _workspace_media_for_folder(session: ImportSession, folder_key: str) -> list[object]:
    _workspace_ensure_cache(session)
    if session.workspace_ownership_dirty:
        # Classification-only edits deliberately defer the expensive global
        # ownership rebuild. For the selected folder, derive a local view from
        # its scanner-seeded media plus explicit user moves. This keeps role
        # changes and navigation responsive even for very large comics.
        media_index = session.workspace_cached_media_index
        local_paths = {str(item.path) for item in _workspace_media_for_folder_base(session, folder_key)}
        local_paths.update(path for path, target in session.workspace_media_targets.items() if target == folder_key)
        visible = []
        for path in local_paths:
            target = session.workspace_media_targets.get(path)
            if target is not None and target != folder_key:
                continue
            item = media_index.get(path)
            if item is not None:
                visible.append(item)
        visible.sort(key=lambda item: getattr(item, "order", 0))
        return visible
    return [
        session.workspace_cached_media_index[path]
        for path in session.workspace_cached_folder_media.get(folder_key, [])
        if path in session.workspace_cached_media_index
    ]


def _workspace_reorder_media(session: ImportSession, ordered_source_paths: list[str]) -> None:
    if not ordered_source_paths:
        return
    rank = {value: index for index, value in enumerate(ordered_source_paths)}

    def reorder(items: list) -> None:
        matching = [item for item in items if str(item.path) in rank]
        if len(matching) != len(ordered_source_paths):
            return
        items.sort(key=lambda item: (rank.get(str(item.path), len(rank) + item.order), item.order))
        items[:] = [replace(item, order=index) for index, item in enumerate(items, start=1)]

    if session.plan.scan.primary:
        reorder(session.plan.scan.primary.media)
    for group in session.plan.scan.extras:
        reorder(group.media)
    for issue in session.plan.scan.issues:
        if issue.primary:
            reorder(issue.primary.media)
        for group in issue.extras:
            reorder(group.media)


def _workspace_sorted_review_items(session: ImportSession, role: ReviewRole) -> list:
    items = [item for item in session.plan.items if item.role is role]
    def key(item):
        source_key = _review_source_relative(session.plan.scan, item.relative_path).replace("\\", "/")
        parent = str(Path(source_key).parent).replace("\\", "/")
        if parent == "": parent = "."
        order = session.folder_order_overrides.get(parent, [])
        try: rank = order.index(source_key)
        except ValueError: rank = len(order) + 1000
        return (parent.casefold(), rank, item.relative_path.as_posix().casefold())
    return sorted(items, key=key)



def _workspace_build_staged_import(
    session: ImportSession,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> StagedImport:
    _workspace_ensure_authoritative_cache(session)
    def report(phase: str, current: int, total: int, detail: str) -> None:
        if progress_callback is not None:
            progress_callback(phase, current, total, detail)

    report("workspace", 0, 0, "Preparing workspace tree")
    tree = _workspace_tree(session)
    if not tree:
        raise StagingError("Import workspace is empty")
    nodes = {str(node["path"]): node for node in tree}
    root_path = str(tree[0]["path"])
    report("workspace", len(tree), len(tree), f"Prepared {len(tree):,} workspace folders")

    def ancestors(path: str):
        current = str(nodes.get(path, {}).get("parent") or "")
        seen: set[str] = set()
        while current and current in nodes and current not in seen:
            seen.add(current)
            yield nodes[current]
            current = str(nodes[current].get("parent") or "")

    def ignored_node(path: str) -> bool:
        node = nodes.get(path)
        if node is None:
            return False
        if str(node["role"]) == "Ignore":
            return True
        return any(str(parent["role"]) == "Ignore" for parent in ancestors(path))

    root_series_candidates = []
    for node in tree:
        if str(node["role"]) != "Series" or ignored_node(str(node["path"])):
            continue
        if not any(str(parent["role"]) in {"Series", "Sub-Series"} for parent in ancestors(str(node["path"]))):
            root_series_candidates.append(node)
    if not root_series_candidates and str(nodes[root_path]["role"]) in {"Issue", "Primary Pages"}:
        # Backward-compatible one-shot: the upload root doubles as the logical
        # series container and its sole issue until the user restructures it.
        root_series_candidates = [nodes[root_path]]
    if len(root_series_candidates) != 1:
        raise StagingError("Choose exactly one top-level folder as Series. The upload root may instead be a Container.")
    logical_root = root_series_candidates[0]
    logical_root_path = str(logical_root["path"])

    author = (session.workspace_author or session.author_default or "").strip()
    if not author:
        raise StagingError("Author is required")
    root_meta = _workspace_node_metadata(session, logical_root)
    if logical_root_path == root_path:
        series_title = (session.workspace_series or session.series_default or str(logical_root["name"])).strip()
        series_complete = _bool_form(session.workspace_series_complete)
    else:
        series_title = (root_meta.get("title") or str(logical_root["name"])).strip()
        series_complete = _bool_form(root_meta.get("complete", ""))
    if not series_title:
        raise StagingError("Series title is required")

    staged = StagedImport(
        schema_version=2,
        staging_id=str(uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        source_path=str(session.plan.scan.source),
        content_root=str(session.plan.scan.content_root),
        author=author,
        series=series_title,
        import_kind="series",
        series_complete=series_complete,
        issues=[], subseries=[], series_extras=[],
    )

    def nearest(path: str, roles: set[str]) -> dict[str, object] | None:
        node = nodes.get(path)
        if node and str(node["role"]) in roles:
            return node
        return next((parent for parent in ancestors(path) if str(parent["role"]) in roles), None)

    series_key_by_path = {logical_root_path: "."}
    sibling_counter: dict[str, int] = {}
    for node in tree:
        path = str(node["path"])
        if path == logical_root_path or ignored_node(path) or str(node["role"]) != "Sub-Series":
            continue
        parent_series = next((parent for parent in ancestors(path) if str(parent["role"]) in {"Series", "Sub-Series"}), None)
        if parent_series is None:
            raise StagingError(f"Sub-series {node['name']} is not inside a Series")
        parent_path = str(parent_series["path"])
        if parent_path not in series_key_by_path:
            # Tree traversal should normally ensure the parent has already been seen.
            raise StagingError(f"Sub-series {node['name']} has an invalid parent hierarchy")
        parent_key = series_key_by_path[parent_path]
        sibling_counter[parent_key] = sibling_counter.get(parent_key, 0) + 1
        meta = _workspace_node_metadata(session, node)
        title = (meta.get("title") or str(node["name"])).strip()
        if not title:
            raise StagingError("Sub-series title cannot be empty")
        staged.subseries.append(StagedSeries(
            source_key=path, title=title, parent_key=parent_key,
            complete=_bool_form(meta.get("complete", "")), sort_order=sibling_counter[parent_key],
        ))
        series_key_by_path[path] = path

    report("structure", len(staged.subseries), len(staged.subseries), f"Built {len(staged.subseries):,} sub-series")

    issue_by_path: dict[str, StagedIssue] = {}
    issue_counter: dict[str, int] = {}
    for node in tree:
        path = str(node["path"])
        if ignored_node(path) or str(node["role"]) != "Issue":
            continue
        parent_series = next((parent for parent in ancestors(path) if str(parent["role"]) in {"Series", "Sub-Series"}), None)
        if parent_series is None and (path == logical_root_path or any(str(parent["path"]) == logical_root_path for parent in ancestors(path))):
            series_key = "."
        elif parent_series is None:
            raise StagingError(f"Issue {node['name']} must be inside a Series or Sub-Series")
        else:
            parent_path = str(parent_series["path"])
            series_key = series_key_by_path.get(parent_path)
        if series_key is None:
            raise StagingError(f"Issue {node['name']} references an invalid series hierarchy")
        issue_counter[series_key] = issue_counter.get(series_key, 0) + 1
        meta = _workspace_node_metadata(session, node)
        issue = StagedIssue(
            source_key=path,
            issue_number=(meta.get("issue_number") or "").strip() or None,
            title=(meta.get("title") or "").strip() or None,
            complete=_bool_form(meta.get("complete", "")),
            sort_order=issue_counter[series_key], series_key=series_key, groups=[],
        )
        staged.issues.append(issue)
        issue_by_path[path] = issue

    report("structure", len(staged.issues), len(staged.issues), f"Built {len(staged.issues):,} issues")

    # A traditional one-shot scan starts with the root as Issue. The virtual
    # tree model requires a logical Series container; preserve compatibility by
    # synthesizing one issue when the logical root itself still has primary media
    # and there are no explicit issue nodes.
    if not staged.issues:
        root_media = [m for m in _workspace_all_media(session) if str(m.path) not in session.workspace_ignored_media]
        if root_media and logical_root_path == root_path:
            issue = StagedIssue(
                source_key=".", issue_number=None, title=None, complete=None,
                sort_order=1, series_key=".", groups=[],
            )
            staged.issues.append(issue)
            issue_by_path[logical_root_path] = issue

    def staged_media(item, order: int) -> StagedMedia:
        return StagedMedia(
            source_path=str(item.path), relative_path=str(item.relative_path),
            mime_type=item.mime_type, media_kind=item.media_kind.value,
            size_bytes=item.size_bytes, order=order,
        )

    # Accumulate media by semantic virtual folder.
    report("ownership", 0, 0, "Resolving media ownership")
    all_media = _workspace_all_media(session)
    buckets: dict[str, list[object]] = {}
    unassigned: list[str] = []
    total_media = len(all_media)
    for media_index, item in enumerate(all_media, start=1):
        source = str(item.path)
        if source in session.workspace_ignored_media:
            continue
        # The workspace cache already resolved automatic ownership. Reuse it
        # instead of rescanning every semantic node for every media item during
        # validation. Explicit user targets still take precedence.
        target = session.workspace_media_targets.get(source) or session.workspace_cached_media_owner.get(source)
        if target is None:
            # Defensive fallback for restored/legacy state not represented in
            # the current cache; normal validation should not hit this path.
            target = _workspace_origin_for_media(session, source)
        if not target or target not in nodes:
            unassigned.append(str(item.relative_path))
            continue
        if ignored_node(target):
            continue
        role = str(nodes[target]["role"])
        if role in {"Issue", "Primary Pages", "Issue-Extras", "Series-Extras"}:
            buckets.setdefault(target, []).append(item)
        else:
            unassigned.append(str(item.relative_path))
        if media_index % 250 == 0 or media_index == total_media:
            report("ownership", media_index, total_media, f"Resolved {media_index:,} of {total_media:,} media files")
    if unassigned:
        preview = ", ".join(unassigned[:5])
        more = f" (+{len(unassigned)-5} more)" if len(unassigned) > 5 else ""
        raise StagingError(f"Files need a destination or must be ignored: {preview}{more}")

    report("groups", 0, len(buckets), f"Building groups from {len(buckets):,} media destinations")
    primary_by_issue: dict[str, list[object]] = {path: [] for path in issue_by_path}
    extra_specs: list[tuple[dict[str, object], list[object]]] = []
    series_extra_specs: list[tuple[dict[str, object], list[object]]] = []
    for target, media_items in buckets.items():
        node = nodes[target]
        role = str(node["role"])
        if role in {"Issue", "Primary Pages"}:
            issue_node = nearest(target, {"Issue"})
            if issue_node is None and target == logical_root_path and logical_root_path in issue_by_path:
                issue_path = logical_root_path
            elif issue_node is None:
                raise StagingError(f"Primary pages folder {node['name']} is not inside an Issue")
            else:
                issue_path = str(issue_node["path"])
            if issue_path not in issue_by_path:
                raise StagingError(f"Primary pages folder {node['name']} references an invalid Issue")
            primary_by_issue.setdefault(issue_path, []).extend(media_items)
        elif role == "Issue-Extras":
            issue_node = next((parent for parent in ancestors(target) if str(parent["role"]) == "Issue"), None)
            if issue_node is None:
                raise StagingError(f"Issue extras folder {node['name']} must be inside an Issue")
            extra_specs.append((node, media_items))
        elif role == "Series-Extras":
            series_node = next((parent for parent in ancestors(target) if str(parent["role"]) in {"Series", "Sub-Series"}), None)
            if series_node is None:
                raise StagingError(f"Series extras folder {node['name']} must be inside a Series")
            series_extra_specs.append((node, media_items))

    report("validating", 0, len(issue_by_path), f"Validating {len(issue_by_path):,} issues")
    for issue_path, issue in issue_by_path.items():
        media_items = primary_by_issue.get(issue_path, [])
        if not media_items:
            raise StagingError(f"Issue {issue.issue_number or issue.title or issue_path} has no primary pages")
        media_items = sorted(media_items, key=lambda item: item.order)
        issue.groups.append(StagedGroup(
            name="Primary content", relative_path=f"workspace/{abs(hash(issue_path))}/primary",
            role=ReviewRole.PRIMARY.value, owner_type="issue", owner_key=issue.source_key,
            media=[staged_media(item, index) for index, item in enumerate(media_items, 1)],
        ))

    for node, media_items in extra_specs:
        path = str(node["path"])
        issue_node = next(parent for parent in ancestors(path) if str(parent["role"]) == "Issue")
        issue = issue_by_path[str(issue_node["path"])]
        if not media_items:
            raise StagingError(f"Empty content group: {node['name']}")
        meta = _workspace_node_metadata(session, node)
        issue.groups.append(StagedGroup(
            name=(meta.get("title") or str(node["name"])).strip(),
            relative_path=f"workspace/{abs(hash(path))}/extras",
            role=ReviewRole.ISSUE_EXTRA.value, owner_type="issue", owner_key=issue.source_key,
            media=[staged_media(item, index) for index, item in enumerate(sorted(media_items, key=lambda x: x.order), 1)],
        ))

    for node, media_items in series_extra_specs:
        path = str(node["path"])
        series_node = next(parent for parent in ancestors(path) if str(parent["role"]) in {"Series", "Sub-Series"})
        owner_path = str(series_node["path"])
        owner_key = series_key_by_path.get(owner_path)
        if owner_key is None:
            raise StagingError(f"Series extras folder {node['name']} references an invalid Series")
        if not media_items:
            raise StagingError(f"Empty content group: {node['name']}")
        meta = _workspace_node_metadata(session, node)
        staged.series_extras.append(StagedGroup(
            name=(meta.get("title") or str(node["name"])).strip(),
            relative_path=f"workspace/{abs(hash(path))}/series-extras",
            role=ReviewRole.SERIES_EXTRA.value, owner_type="series", owner_key=owner_key,
            media=[staged_media(item, index) for index, item in enumerate(sorted(media_items, key=lambda x: x.order), 1)],
        ))

    group_count = sum(len(issue.groups) for issue in staged.issues) + len(staged.series_extras)
    report("complete", total_media, total_media, f"Staging model ready: {len(staged.issues):,} issues, {group_count:,} groups, {total_media:,} media files")
    return staged


def _session_state_dir(app: FastAPI) -> Path:
    root = app.state.staging_root / "session_state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_state_path(app: FastAPI, kind: str, session_id: str) -> Path:
    return _session_state_dir(app) / f"{kind}_{session_id}.json"


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _staged_from_dict(data: dict) -> StagedImport:
    def media(d: dict) -> StagedMedia:
        return StagedMedia(**d)
    def group(d: dict) -> StagedGroup:
        return StagedGroup(
            name=d["name"], relative_path=d["relative_path"], role=d["role"],
            owner_type=d["owner_type"], owner_key=d.get("owner_key"),
            media=[media(item) for item in d.get("media", [])],
        )
    return StagedImport(
        schema_version=data["schema_version"], staging_id=data["staging_id"],
        created_at=data["created_at"], source_path=data["source_path"],
        content_root=data["content_root"], author=data["author"], series=data["series"],
        import_kind=data["import_kind"], series_complete=data.get("series_complete"),
        issues=[StagedIssue(
            source_key=item["source_key"], issue_number=item.get("issue_number"),
            title=item.get("title"), complete=item.get("complete"),
            sort_order=item.get("sort_order"), series_key=item.get("series_key", "."),
            groups=[group(g) for g in item.get("groups", [])],
        ) for item in data.get("issues", [])],
        subseries=[StagedSeries(**item) for item in data.get("subseries", [])],
        series_extras=[group(g) for g in data.get("series_extras", [])],
    )


def _persist_import_session(app: FastAPI, session_id: str, session: ImportSession) -> None:
    payload = {
        "version": 1,
        "last_activity": session.last_activity,
        "source": str(session.plan.scan.source),
        "author_default": session.author_default,
        "series_default": session.series_default,
        "bulk_id": session.bulk_id,
        "upload_root": str(session.upload_root) if session.upload_root else None,
        "pdf_cache_root": str(session.pdf_cache_root) if session.pdf_cache_root else None,
        "staging_path": str(session.staging_path) if session.staging_path else None,
        "staged": session.staged.to_dict() if session.staged else None,
        "extra_folder_overrides": sorted(session.extra_folder_overrides),
        "subseries_folder_overrides": sorted(session.subseries_folder_overrides),
        "primary_folder_overrides": sorted(session.primary_folder_overrides),
        "container_folder_overrides": sorted(session.container_folder_overrides),
        "folder_order_overrides": session.folder_order_overrides,
        "workspace_author": session.workspace_author,
        "workspace_series": session.workspace_series,
        "workspace_series_complete": session.workspace_series_complete,
        "workspace_metadata": session.workspace_metadata,
        "workspace_virtual_groups": session.workspace_virtual_groups,
        "workspace_virtual_nodes": session.workspace_virtual_nodes,
        "workspace_media_targets": session.workspace_media_targets,
        "workspace_role_overrides": session.workspace_role_overrides,
        "workspace_parent_overrides": session.workspace_parent_overrides,
        "workspace_ignored_media": sorted(session.workspace_ignored_media),
    }
    _atomic_json_write(_session_state_path(app, "import", session_id), payload)


def _restore_import_session(app: FastAPI, session_id: str) -> ImportSession | None:
    path = _session_state_path(app, "import", session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_activity = float(data.get("last_activity", 0))
        if time.time() - last_activity >= UPLOAD_INACTIVITY_SECONDS:
            return None
        source = Path(data["source"])
        if not source.exists():
            return None
        pdf_cache = Path(data["pdf_cache_root"]) if data.get("pdf_cache_root") else None
        scan = scan_folder(
            source,
            extra_folders=data.get("extra_folder_overrides", []),
            primary_folders=data.get("primary_folder_overrides", []),
            subseries_folders=data.get("subseries_folder_overrides", []),
            pdf_cache_root=pdf_cache,
        )
        session = ImportSession(
            plan=build_review_plan(scan),
            staged=_staged_from_dict(data["staged"]) if data.get("staged") else None,
            staging_path=Path(data["staging_path"]) if data.get("staging_path") else None,
            author_default=data.get("author_default"), series_default=data.get("series_default"),
            bulk_id=data.get("bulk_id"),
            upload_root=Path(data["upload_root"]) if data.get("upload_root") else None,
            pdf_cache_root=pdf_cache, last_activity=last_activity,
            extra_folder_overrides=set(data.get("extra_folder_overrides", [])),
            subseries_folder_overrides=set(data.get("subseries_folder_overrides", [])),
            primary_folder_overrides=set(data.get("primary_folder_overrides", [])),
            container_folder_overrides=set(data.get("container_folder_overrides", [])),
            folder_order_overrides={k: list(v) for k, v in data.get("folder_order_overrides", {}).items()},
            workspace_author=data.get("workspace_author"), workspace_series=data.get("workspace_series"),
            workspace_series_complete=data.get("workspace_series_complete", ""),
            workspace_metadata=data.get("workspace_metadata", {}),
            workspace_virtual_groups=data.get("workspace_virtual_groups", {}),
            workspace_virtual_nodes=data.get("workspace_virtual_nodes", {}),
            workspace_media_targets=data.get("workspace_media_targets", {}),
            workspace_role_overrides=data.get("workspace_role_overrides", {}),
            workspace_parent_overrides=data.get("workspace_parent_overrides", {}),
            workspace_ignored_media=set(data.get("workspace_ignored_media", [])),
        )
        app.state.import_sessions[session_id] = session
        return session
    except (OSError, ValueError, KeyError, TypeError, FolderScanError, json.JSONDecodeError):
        return None


def _delete_import_session_state(app: FastAPI, session_id: str) -> None:
    try:
        _session_state_path(app, "import", session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _completed_import_path(app: FastAPI, session_id: str) -> Path:
    return _session_state_path(app, "completed", session_id)


def _save_completed_import(app: FastAPI, session_id: str, payload: dict) -> None:
    data = dict(payload)
    data["last_activity"] = time.time()
    _atomic_json_write(_completed_import_path(app, session_id), data)


def _load_completed_import(app: FastAPI, session_id: str) -> dict | None:
    path = _completed_import_path(app, session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("last_activity", 0)) >= UPLOAD_INACTIVITY_SECONDS:
            path.unlink(missing_ok=True)
            return None
        return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


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


def _persist_bulk_session(app: FastAPI, bulk_id: str, bulk: BulkArtistSession) -> None:
    _atomic_json_write(_session_state_path(app, "bulk", bulk_id), {
        "version": 1, "source": str(bulk.source), "author": bulk.author,
        "candidates": [{"path": str(c.path), "name": c.name, "media_count": c.media_count} for c in bulk.candidates],
        "selected": bulk.selected, "current_position": bulk.current_position,
        "imported_series": bulk.imported_series, "skipped_series": bulk.skipped_series,
        "upload_root": str(bulk.upload_root) if bulk.upload_root else None,
        "last_activity": bulk.last_activity,
    })


def _restore_bulk_session(app: FastAPI, bulk_id: str) -> BulkArtistSession | None:
    path = _session_state_path(app, "bulk", bulk_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_activity = float(data.get("last_activity", 0))
        if time.time() - last_activity >= UPLOAD_INACTIVITY_SECONDS:
            return None
        bulk = BulkArtistSession(
            source=Path(data["source"]), author=data["author"],
            candidates=[BulkComicCandidate(Path(c["path"]), c["name"], int(c["media_count"])) for c in data.get("candidates", [])],
            selected=[int(v) for v in data.get("selected", [])], current_position=int(data.get("current_position", 0)),
            imported_series=[tuple(v) for v in data.get("imported_series", [])],
            skipped_series=list(data.get("skipped_series", [])),
            upload_root=Path(data["upload_root"]) if data.get("upload_root") else None,
            last_activity=last_activity,
        )
        app.state.bulk_import_sessions[bulk_id] = bulk
        return bulk
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _delete_bulk_session_state(app: FastAPI, bulk_id: str) -> None:
    try:
        _session_state_path(app, "bulk", bulk_id).unlink(missing_ok=True)
    except OSError:
        pass


def _touch_upload_root(path: Path | None, now: float | None = None) -> None:
    if path is None or not path.exists():
        return
    stamp = time.time() if now is None else now
    try:
        import os
        os.utime(path, (stamp, stamp))
    except OSError:
        pass


def _rescan_import_session(session: ImportSession) -> None:
    scan = scan_folder(
        session.plan.scan.source,
        extra_folders=sorted(session.extra_folder_overrides),
        primary_folders=sorted(session.primary_folder_overrides),
        subseries_folders=sorted(session.subseries_folder_overrides),
        pdf_cache_root=session.pdf_cache_root,
    )
    session.plan = build_review_plan(scan)


def _touch_import_activity(app: FastAPI, session: ImportSession) -> None:
    now = time.time()
    session.last_activity = now
    _touch_upload_root(session.upload_root, now)
    session_id = next((key for key, value in app.state.import_sessions.items() if value is session), None)
    if session_id:
        _persist_import_session(app, session_id, session)
    if session.bulk_id:
        bulk = app.state.bulk_import_sessions.get(session.bulk_id)
        if bulk is not None:
            bulk.last_activity = now
            _touch_upload_root(bulk.upload_root, now)
            _persist_bulk_session(app, session.bulk_id, bulk)


def _touch_bulk_activity(app: FastAPI, bulk: BulkArtistSession) -> None:
    now = time.time()
    bulk.last_activity = now
    _touch_upload_root(bulk.upload_root, now)
    bulk_id = next((key for key, value in app.state.bulk_import_sessions.items() if value is bulk), None)
    if bulk_id:
        _persist_bulk_session(app, bulk_id, bulk)


def _remove_staging_record(session: ImportSession) -> None:
    _cleanup_upload(session.pdf_cache_root)
    session.pdf_cache_root = None
    if session.staging_path is not None:
        try:
            session.staging_path.unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup_stale_imports(app: FastAPI, staging_root: Path, *, now: float | None = None) -> None:
    current = time.time() if now is None else now
    cutoff = current - UPLOAD_INACTIVITY_SECONDS

    stale_upload_ids = [
        upload_id for upload_id, upload in app.state.upload_sessions.items()
        if upload.last_activity < cutoff
    ]
    upload_roots_to_delete: set[Path] = set()
    for upload_id in stale_upload_ids:
        upload = app.state.upload_sessions.pop(upload_id, None)
        _delete_browser_upload_session_state(app, upload_id)
        if upload is not None:
            upload_roots_to_delete.add(upload.upload_root)

    stale_bulk_ids = [
        bulk_id for bulk_id, bulk in app.state.bulk_import_sessions.items()
        if bulk.last_activity < cutoff
    ]
    for bulk_id in stale_bulk_ids:
        bulk = app.state.bulk_import_sessions.pop(bulk_id, None)
        _delete_bulk_session_state(app, bulk_id)
        if bulk and bulk.upload_root:
            upload_roots_to_delete.add(bulk.upload_root)

    for session_id, session in list(app.state.import_sessions.items()):
        bulk_is_stale = session.bulk_id in stale_bulk_ids
        if session.last_activity < cutoff or bulk_is_stale:
            app.state.import_sessions.pop(session_id, None)
            _delete_import_session_state(app, session_id)
            _remove_staging_record(session)
            if session.upload_root and (session.bulk_id is None or bulk_is_stale):
                upload_roots_to_delete.add(session.upload_root)

    for root in upload_roots_to_delete:
        _cleanup_upload(root)

    uploads_root = staging_root / "uploads"
    if uploads_root.is_dir():
        active_roots = {
            item.upload_root.resolve()
            for item in (
                list(app.state.upload_sessions.values())
                + list(app.state.import_sessions.values())
                + list(app.state.bulk_import_sessions.values())
            )
            if item.upload_root is not None and item.upload_root.exists()
        }
        for child in uploads_root.iterdir():
            if not child.is_dir():
                continue
            try:
                if child.resolve() in active_roots:
                    continue
                if child.stat().st_mtime < cutoff:
                    _cleanup_upload(child)
            except OSError:
                continue

    state_root = staging_root / "session_state"
    if state_root.is_dir():
        for state_file in state_root.glob("*.json"):
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                if float(data.get("last_activity", 0)) < cutoff:
                    state_file.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                try:
                    if state_file.stat().st_mtime < cutoff:
                        state_file.unlink(missing_ok=True)
                except OSError:
                    pass

    active_staging_paths = {
        session.staging_path.resolve()
        for session in app.state.import_sessions.values()
        if session.staging_path is not None and session.staging_path.exists()
    }
    if staging_root.is_dir():
        for child in staging_root.glob("*.json"):
            try:
                if child.resolve() in active_staging_paths:
                    continue
                if child.stat().st_mtime < cutoff:
                    child.unlink(missing_ok=True)
            except OSError:
                continue


def _session_or_404(app: FastAPI, session_id: str) -> ImportSession:
    session = app.state.import_sessions.get(session_id)
    if session is None:
        session = _restore_import_session(app, session_id)
    if session is not None and time.time() - session.last_activity >= UPLOAD_INACTIVITY_SECONDS:
        _cleanup_stale_imports(app, app.state.staging_root)
        session = app.state.import_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Import session expired or was cleaned up after 12 hours of inactivity")
    return session


def _bulk_session_or_404(app: FastAPI, bulk_id: str) -> BulkArtistSession:
    session = app.state.bulk_import_sessions.get(bulk_id)
    if session is None:
        session = _restore_bulk_session(app, bulk_id)
    if session is not None and time.time() - session.last_activity >= UPLOAD_INACTIVITY_SECONDS:
        _cleanup_stale_imports(app, app.state.staging_root)
        session = app.state.bulk_import_sessions.get(bulk_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Bulk import session expired or was cleaned up after 12 hours of inactivity")
    return session


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


def create_app(
    database_path: str | Path = "./comic_archive.sqlite3",
    library_root: str | Path = "./library",
    staging_root: str | Path = "./staging",
    secure_cookies: bool = False,
    import_root: str | Path | None = None,
) -> FastAPI:
    database = initialize_database(database_path)
    initialize_auth_database(database)
    library = Path(library_root).expanduser().resolve()
    staging = Path(staging_root).expanduser().resolve()
    imports = database.parent if import_root is None else Path(import_root).expanduser().resolve()

    app = FastAPI(title="Comic Archive", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database_path = database
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
                or (path.startswith("/series/") and "/missing/" in path)
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


    @app.get("/import/{session_id}/metadata", response_class=HTMLResponse)
    def import_metadata(request: Request, session_id: str):
        if _load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)
        session = _session_or_404(app, session_id)
        _touch_import_activity(app, session)
        if session.plan.scan.is_series_candidate:
            issue_items = _workspace_sorted_review_items(session, ReviewRole.ISSUE)
            subseries_items = _workspace_sorted_review_items(session, ReviewRole.SUBSERIES)
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
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        author = form.get("author", "").strip()
        series = form.get("series", "").strip()
        issue_metadata: dict[str, dict[str, object]] = {}
        subseries_metadata: dict[str, dict[str, object]] = {}
        if session.plan.scan.is_series_candidate:
            issue_items = _workspace_sorted_review_items(session, ReviewRole.ISSUE)
            subseries_items = _workspace_sorted_review_items(session, ReviewRole.SUBSERIES)
            for index, item in enumerate(subseries_items):
                subseries_metadata[str(item.relative_path)] = {
                    "complete": form.get(f"subseries_complete_{index}", ""),
                    "sort_order": form.get(f"subseries_order_{index}", str(index + 1)),
                }
            for index, item in enumerate(issue_items):
                complete = form.get(f"complete_{index}", "")
                issue_metadata[str(item.relative_path)] = {
                    "issue_number": form.get(f"issue_number_{index}", ""),
                    "title": form.get(f"title_{index}", ""),
                    "complete": complete,
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
            staged = build_staged_import(
                session.plan, author=author, series=series, issue_metadata=issue_metadata,
                subseries_metadata=subseries_metadata,
                series_complete=_bool_form(form.get("series_complete", "")),
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
        if _load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)
        session = _session_or_404(app, session_id)
        _touch_import_activity(app, session)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="import_organize.html",
            context={"session_id": session_id, "staged": session.staged, "error": None},
        )

    @app.post("/import/{session_id}/organize", response_class=HTMLResponse)
    async def import_organize_save(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        form = await _form_data(request)
        _touch_import_activity(app, session)
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
                move_staged_media(session.staged, issue_key, source_path, target_path)

        try:
            # Every organizer submit saves pending file movements first. This
            # prevents Create/Remove group actions from discarding unsaved work.
            save_assignments()
            if action.startswith("create:"):
                issue_index = int(action.split(":", 1)[1])
                issue = session.staged.issues[issue_index]
                create_staged_issue_extra(
                    session.staged,
                    issue.source_key,
                    form.get(f"new_group_{issue_index}", ""),
                )
            elif action.startswith("remove-empty:"):
                _, issue_text, group_text = action.split(":", 2)
                issue = session.staged.issues[int(issue_text)]
                group = issue.groups[int(group_text)]
                remove_empty_staged_group(session.staged, issue.source_key, group.relative_path)
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
        session = _session_or_404(app, session_id)
        await _form_data(request)
        _touch_import_activity(app, session)
        return Response(status_code=204)

    @app.post("/import/bulk/{bulk_id}/keepalive")
    async def bulk_keepalive(request: Request, bulk_id: str):
        bulk = _bulk_session_or_404(app, bulk_id)
        await _form_data(request)
        _touch_bulk_activity(app, bulk)
        return Response(status_code=204)

    @app.get("/import/{session_id}/confirm", response_class=HTMLResponse)
    def import_confirm(request: Request, session_id: str):
        if _load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)
        session = _session_or_404(app, session_id)
        _touch_import_activity(app, session)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="import_confirm.html",
            context={"session_id": session_id, "staged": session.staged, "error": None, "duplicate": False, "is_bulk": bool(session.bulk_id)},
        )

    @app.post("/import/{session_id}/skip")
    async def import_skip(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        if not session.bulk_id:
            raise HTTPException(status_code=400, detail="Only bulk-import comics can be skipped")
        await _form_data(request)
        _touch_import_activity(app, session)

        bulk_id = session.bulk_id
        bulk = _bulk_session_or_404(app, bulk_id)
        skipped_name = (
            session.staged.series
            if session.staged is not None
            else session.series_default or session.plan.scan.content_root.name
        )
        bulk.skipped_series.append(skipped_name)
        _remove_staging_record(session)
        app.state.import_sessions.pop(session_id, None)
        _delete_import_session_state(app, session_id)
        bulk.current_position += 1
        _touch_bulk_activity(app, bulk)

        try:
            next_session_id = _start_bulk_item(app, bulk_id)
        except (FolderScanError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if next_session_id is not None:
            return RedirectResponse(f"/import/{next_session_id}/review", status_code=303)

        _cleanup_upload(bulk.upload_root)
        bulk.upload_root = None
        _delete_bulk_session_state(app, bulk_id)
        return RedirectResponse(f"/import/bulk/{bulk_id}/done", status_code=303)

    @app.get("/import/{session_id}/done", response_class=HTMLResponse)
    def import_done(request: Request, session_id: str):
        receipt = _load_completed_import(app, session_id)
        if receipt is None:
            session = _session_or_404(app, session_id)
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
            if _load_completed_import(app, session_id) is not None:
                return {"phase": "complete", "current": 1, "total": 1, "detail": "Import complete"}
            return {"phase": "waiting", "current": 0, "total": 0, "detail": "Waiting to start"}
        return progress

    @app.post("/import/{session_id}/commit", response_class=HTMLResponse)
    async def import_commit(request: Request, session_id: str):
        # A completed receipt makes this endpoint idempotent. Double-clicks, refreshes,
        # and browser POST replays all resolve to the already-completed result.
        if _load_completed_import(app, session_id) is not None:
            return RedirectResponse(f"/import/{session_id}/done", status_code=303)

        session = _session_or_404(app, session_id)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        allow_duplicate = form.get("allow_duplicate") == "yes"
        commit_started = time.monotonic()
        last_commit_phase: list[str | None] = [None]
        def report_progress(phase: str, current: int, total: int, detail: str) -> None:
            app.state.import_progress[session_id] = {
                "phase": phase, "current": current, "total": total, "detail": detail,
                "updated_at": time.time(),
            }
            if phase != last_commit_phase[0] or (total > 0 and current > 0 and (current == total or current % 1000 == 0)):
                logger.info(
                    "commit progress session_id=%s phase=%s current=%d total=%d detail=%s elapsed=%.2fs",
                    session_id, phase, current, total, detail, time.monotonic() - commit_started,
                )
                _log_memory_checkpoint(f"commit-{phase}", session_id=session_id, current=current, total=total)
                last_commit_phase[0] = phase

        app.state.import_progress[session_id] = {
            "phase": "starting", "current": 0, "total": 0,
            "detail": "Starting import", "updated_at": time.time(),
        }
        _log_memory_checkpoint("commit-start", session_id=session_id)
        try:
            result = await asyncio.to_thread(
                commit_staged_import,
                session.staged,
                library_root=library,
                database_path=database,
                allow_duplicate=allow_duplicate,
                progress_callback=report_progress,
            )
            result_payload = {
                "import_id": result.import_id, "author_id": result.author_id,
                "series_id": result.series_id, "copied_files": result.copied_files,
            }
        except CommitError as exc:
            # If a previous request actually committed but its response was lost, recover
            # from the imports table instead of making the user restart the workflow.
            existing = _existing_commit_result(database, session.staged)
            if existing is not None and "already been committed" in str(exc):
                result_payload = existing
            else:
                message = str(exc)
                duplicate = "Possible duplicate import detected" in message
                return templates.TemplateResponse(
                    request=request,
                    name="import_confirm.html",
                    context={"session_id": session_id, "staged": session.staged, "error": message, "duplicate": duplicate, "is_bulk": bool(session.bulk_id)},
                    status_code=409 if duplicate else 400,
                )

        receipt = {
            "author": session.staged.author,
            "series": session.staged.series,
            "result": result_payload,
            "next_url": None, "bulk_progress": None, "bulk_error": None,
        }

        if session.bulk_id:
            bulk = _bulk_session_or_404(app, session.bulk_id)
            if not any(series_id == result_payload["series_id"] for _, series_id in bulk.imported_series):
                bulk.imported_series.append((session.staged.series, result_payload["series_id"]))
                bulk.current_position += 1
            try:
                next_session_id = _start_bulk_item(app, session.bulk_id)
            except (FolderScanError, OSError) as exc:
                receipt["bulk_error"] = str(exc)
                receipt["bulk_progress"] = f"{bulk.current_position} of {len(bulk.selected)}"
                next_session_id = None
            if next_session_id is not None:
                receipt["next_url"] = f"/import/{next_session_id}/review"
                receipt["bulk_progress"] = f"{bulk.current_position} of {len(bulk.selected)}"
            elif bulk.current_position >= len(bulk.selected):
                _cleanup_upload(bulk.upload_root)
                bulk.upload_root = None
                _delete_bulk_session_state(app, session.bulk_id)
        else:
            _cleanup_upload(session.upload_root)

        _save_completed_import(app, session_id, receipt)
        app.state.import_sessions.pop(session_id, None)
        _delete_import_session_state(app, session_id)
        _remove_staging_record(session)

        if session.bulk_id and receipt["next_url"] is None and receipt["bulk_error"] is None:
            return RedirectResponse(f"/import/bulk/{session.bulk_id}/done", status_code=303)
        return RedirectResponse(f"/import/{session_id}/done", status_code=303)

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
