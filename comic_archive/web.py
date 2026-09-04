from __future__ import annotations

import sqlite3
import json
import secrets
import time
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import parse_qs
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
from .importer.staging import StagedImport, StagingError, build_staged_import, create_staged_issue_extra, move_staged_media, remove_empty_staged_group
from .library import AuthorView, GroupView, IssueView, MediaView, SeriesView, read_library
from .editing import EditError, create_issue_extra_group, edit_issue, edit_series, get_history, move_extra_group, move_media_to_group, rename_author, rename_group, reorder_issues, reorder_media, set_media_active
from .thumbnails import THUMBNAIL_MIME, ensure_thumbnail
from .progress import get_continue_reading, get_progress, get_progress_map, reset_progress, save_progress
from .maintenance import build_maintenance_report, series_gaps, set_intentional_gap
from .auth import (
    AuthError, authenticate, change_password, create_user, get_or_create_session_secret,
    get_user, list_users, reset_password, set_user_active, set_user_admin,
)


_TEMPLATE_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

UPLOAD_INACTIVITY_SECONDS = 12 * 60 * 60
UPLOAD_SWEEP_INTERVAL_SECONDS = 60 * 60


@dataclass
class BrowserUploadSession:
    upload_root: Path
    mode: str
    expected_files: int
    received_paths: set[str] = field(default_factory=set)
    last_activity: float = field(default_factory=time.time)
    cancelled: bool = False


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
    workspace_media_targets: dict[str, str] = field(default_factory=dict)


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


def _find_author(authors: Iterable[AuthorView], author_id: str) -> AuthorView | None:
    return next((author for author in authors if author.id == author_id), None)


def _walk_series(series_items: Iterable[SeriesView]):
    for series in series_items:
        yield series
        yield from _walk_series(series.children)


def _find_series(authors: Iterable[AuthorView], series_id: str) -> tuple[AuthorView, SeriesView] | None:
    for author in authors:
        for series in _walk_series(author.series):
            if series.id == series_id:
                return author, series
    return None


def _find_issue(authors: Iterable[AuthorView], issue_id: str) -> tuple[AuthorView, SeriesView, IssueView] | None:
    for author in authors:
        for series in _walk_series(author.series):
            for issue in series.issues:
                if issue.id == issue_id:
                    return author, series, issue
    return None


def _find_group(authors: Iterable[AuthorView], group_id: str) -> tuple[AuthorView, SeriesView, IssueView | None, GroupView] | None:
    for author in authors:
        for series in _walk_series(author.series):
            for group in series.extras:
                if group.id == group_id:
                    return author, series, None, group
            for issue in series.issues:
                for group in issue.groups:
                    if group.id == group_id:
                        return author, series, issue, group
    return None


def _primary_group(issue: IssueView) -> GroupView | None:
    return next((group for group in issue.groups if group.role == "primary"), None)


def _first_image_media(issue: IssueView) -> MediaView | None:
    primary = _primary_group(issue)
    if primary is None:
        return None
    return next((media for media in primary.media if media.mime_type.startswith("image/")), None)


def _issue_preview_map(series: SeriesView) -> dict[str, MediaView]:
    previews: dict[str, MediaView] = {}
    for issue in series.issues:
        media = _first_image_media(issue)
        if media is not None:
            previews[issue.id] = media
    return previews


def _series_preview_map(author: AuthorView) -> dict[str, MediaView]:
    previews: dict[str, MediaView] = {}

    def first_preview(series: SeriesView) -> MediaView | None:
        for issue in series.issues:
            media = _first_image_media(issue)
            if media is not None:
                return media
        for child in series.children:
            media = first_preview(child)
            if media is not None:
                return media
        return None

    for series in _walk_series(author.series):
        media = first_preview(series)
        if media is not None:
            previews[series.id] = media
    return previews


def _series_lineage(author: AuthorView, series: SeriesView) -> list[SeriesView]:
    by_id = {item.id: item for item in _walk_series(author.series)}
    lineage: list[SeriesView] = []
    current = series
    seen: set[str] = set()
    while current.parent_series_id and current.parent_series_id in by_id and current.id not in seen:
        seen.add(current.id)
        current = by_id[current.parent_series_id]
        lineage.append(current)
    lineage.reverse()
    return lineage


def _media_record(database: Path, media_id: str) -> tuple[str, str] | None:
    database = initialize_database(database)
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT stored_path, mime_type FROM media WHERE id = ? AND active = 1",
            (media_id,),
        ).fetchone()
    return None if row is None else (row[0], row[1])


def _safe_library_file(library_root: Path, stored_path: str) -> Path:
    candidate = (library_root / stored_path).resolve()
    try:
        candidate.relative_to(library_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return candidate


async def _form_data(request: Request, *, check_csrf: bool = True) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    form = {key: values[-1] if values else "" for key, values in parsed.items()}
    if check_csrf:
        expected = request.session.get("csrf_token", "")
        supplied = form.get("csrf_token", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")
    return form


def _check_csrf_value(request: Request, supplied: str) -> None:
    expected = request.session.get("csrf_token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def _safe_upload_relative(filename: str) -> Path:
    normalized = filename.replace("\\", "/").lstrip("/")
    relative = PurePosixPath(normalized)
    if not normalized or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid uploaded folder path")
    return Path(*relative.parts)


def _upload_session_or_404(app: FastAPI, upload_id: str) -> BrowserUploadSession:
    session = app.state.upload_sessions.get(upload_id)
    if session is not None and time.time() - session.last_activity >= UPLOAD_INACTIVITY_SECONDS:
        app.state.upload_sessions.pop(upload_id, None)
        _cleanup_upload(session.upload_root)
        session = None
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session expired, was cleaned up, or the server was restarted")
    return session


def _touch_browser_upload(session: BrowserUploadSession) -> None:
    session.last_activity = time.time()
    _touch_upload_root(session.upload_root, session.last_activity)


async def _save_upload_file(request: Request, session: BrowserUploadSession) -> str:
    if session.cancelled:
        raise HTTPException(status_code=409, detail="Upload session was cancelled")
    form = await request.form(max_files=2, max_fields=10)
    _check_csrf_value(request, str(form.get("csrf_token", "")))
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status_code=400, detail="Upload request must contain one file.")

    relative_text = str(form.get("relative_path", "")).strip() or (upload.filename or "")
    relative = _safe_upload_relative(relative_text)
    normalized = relative.as_posix()
    content_root = session.upload_root / "content"
    destination = (content_root / relative).resolve()
    try:
        destination.relative_to(content_root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid uploaded folder path") from exc

    if normalized in session.received_paths and destination.is_file():
        await upload.close()
        _touch_browser_upload(session)
        return normalized
    if normalized not in session.received_paths and len(session.received_paths) >= session.expected_files:
        await upload.close()
        raise HTTPException(status_code=409, detail="Upload session already received its expected number of files")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with temporary.open("wb") as target:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        if session.cancelled:
            temporary.unlink(missing_ok=True)
            _cleanup_upload(session.upload_root)
            raise HTTPException(status_code=409, detail="Upload session was cancelled")
        temporary.replace(destination)
        session.received_paths.add(normalized)
        _touch_browser_upload(session)
        return normalized
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        await upload.close()
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


def _workspace_tree(session: ImportSession, *, include_virtual: bool = True) -> list[dict[str, object]]:
    scan = session.plan.scan
    role_map = _workspace_role_map(session.plan)
    root = scan.content_root
    nodes: list[dict[str, object]] = []

    def visit(path: Path, depth: int) -> None:
        key = _workspace_source_relative(scan, path)
        role, item = role_map.get(key, ("Container", None))
        if key in session.container_folder_overrides:
            role = "Container"
        children = [child for child in path.iterdir() if child.is_dir() and _workspace_has_media(child)]
        children = _workspace_sort_children(session, key, children)
        nodes.append({
            "path": key,
            "name": path.name,
            "depth": depth,
            "role": role,
            "direct_files": _workspace_direct_media_count(path),
            "child_count": len(children),
            "is_root": path.resolve() == root.resolve(),
            "item": item,
            "parent": _workspace_source_relative(scan, path.parent) if path.resolve() != root.resolve() else None,
        })
        for child in children:
            visit(child, depth + 1)
        if include_virtual and role == "Issue":
            for group_id, group in session.workspace_virtual_groups.items():
                if group.get("owner") == key:
                    nodes.append(_workspace_virtual_node(session, group_id, group, depth + 1))

    visit(root, 0)
    return nodes



def _workspace_item_key(session: ImportSession, item) -> str:
    return _review_source_relative(session.plan.scan, item.relative_path).replace("\\", "/")


def _workspace_node_metadata(session: ImportSession, node: dict[str, object]) -> dict[str, str]:
    key = str(node["path"])
    saved = dict(session.workspace_metadata.get(key, {}))
    role = str(node["role"])
    item = node.get("item")
    if role == "Issue":
        default_label = getattr(item, "name", None) or str(node["name"])
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


def _workspace_origin_for_media(session: ImportSession, media_path: str) -> str | None:
    for node in _workspace_tree(session, include_virtual=False):
        if node["is_root"] and node["role"] not in {"Issue", "Primary Pages"}:
            continue
        for media in _workspace_media_for_folder_base(session, str(node["path"])):
            if str(media.path) == media_path:
                return str(node["path"])
    return None


def _workspace_media_targets_for_folder(session: ImportSession, folder_key: str) -> list[tuple[str, str]]:
    tree = _workspace_tree(session)
    node = next((item for item in tree if str(item["path"]) == folder_key), None)
    if node is None:
        return []
    if str(node["role"]) == "Issue":
        owner = folder_key
    elif str(node["role"]) == "Issue-Extras":
        owner = str(node.get("parent") or "")
        # A real nested extras folder's parent can be a page container. Resolve
        # ownership from the review item when possible.
        item = node.get("item")
        issue_path = getattr(item, "issue_path", None) if item is not None else None
        if issue_path is not None:
            owner = _review_source_relative(session.plan.scan, issue_path).replace("\\", "/")
    else:
        return []
    choices: list[tuple[str, str]] = [(owner, "Main comic")]
    for candidate in tree:
        if str(candidate["role"]) != "Issue-Extras":
            continue
        candidate_owner = str(candidate.get("parent") or "")
        item = candidate.get("item")
        issue_path = getattr(item, "issue_path", None) if item is not None else None
        if issue_path is not None:
            candidate_owner = _review_source_relative(session.plan.scan, issue_path).replace("\\", "/")
        if candidate_owner == owner:
            choices.append((str(candidate["path"]), str(candidate["name"])))
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
    if folder_key.startswith("virtual:"):
        return [
            item for item in _workspace_all_media(session)
            if session.workspace_media_targets.get(str(item.path)) == folder_key
        ]
    base = _workspace_media_for_folder_base(session, folder_key)
    result = [
        item for item in base
        if session.workspace_media_targets.get(str(item.path), folder_key) == folder_key
    ]
    # Include pages explicitly moved here from another group.
    existing = {str(item.path) for item in result}
    for item in _workspace_all_media(session):
        if session.workspace_media_targets.get(str(item.path)) == folder_key and str(item.path) not in existing:
            result.append(item)
    result.sort(key=lambda item: item.order)
    return result


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
    if session.bulk_id:
        bulk = app.state.bulk_import_sessions.get(session.bulk_id)
        if bulk is not None:
            bulk.last_activity = now
            _touch_upload_root(bulk.upload_root, now)


def _touch_bulk_activity(bulk: BulkArtistSession) -> None:
    now = time.time()
    bulk.last_activity = now
    _touch_upload_root(bulk.upload_root, now)


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
        if upload is not None:
            upload_roots_to_delete.add(upload.upload_root)

    stale_bulk_ids = [
        bulk_id for bulk_id, bulk in app.state.bulk_import_sessions.items()
        if bulk.last_activity < cutoff
    ]
    for bulk_id in stale_bulk_ids:
        bulk = app.state.bulk_import_sessions.pop(bulk_id, None)
        if bulk and bulk.upload_root:
            upload_roots_to_delete.add(bulk.upload_root)

    for session_id, session in list(app.state.import_sessions.items()):
        bulk_is_stale = session.bulk_id in stale_bulk_ids
        if session.last_activity < cutoff or bulk_is_stale:
            app.state.import_sessions.pop(session_id, None)
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
    if session is not None and time.time() - session.last_activity >= UPLOAD_INACTIVITY_SECONDS:
        _cleanup_stale_imports(app, app.state.staging_root)
        session = app.state.import_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Import session expired, was cleaned up, or the server was restarted")
    return session


def _bulk_session_or_404(app: FastAPI, bulk_id: str) -> BulkArtistSession:
    session = app.state.bulk_import_sessions.get(bulk_id)
    if session is not None and time.time() - session.last_activity >= UPLOAD_INACTIVITY_SECONDS:
        _cleanup_stale_imports(app, app.state.staging_root)
        session = app.state.bulk_import_sessions.get(bulk_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Bulk import session expired, was cleaned up, or the server was restarted")
    return session


def _start_bulk_item(app: FastAPI, bulk_id: str) -> str | None:
    bulk = _bulk_session_or_404(app, bulk_id)
    if bulk.current_position >= len(bulk.selected):
        return None
    candidate_index = bulk.selected[bulk.current_position]
    candidate = bulk.candidates[candidate_index]
    pdf_cache_root = _new_pdf_cache(app.state.staging_root, bulk.upload_root)
    try:
        scan = scan_folder(candidate.path, pdf_cache_root=pdf_cache_root)
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
    return session_id



def _all_media_for_group(database: Path, group_id: str) -> list[dict[str, object]]:
    database = initialize_database(database)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id, position, original_relative_path, mime_type, media_kind, size_bytes, active
               FROM media WHERE group_id = ? ORDER BY active DESC, position, id""",
            (group_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _author_choices(database: Path) -> list[dict[str, str]]:
    database = initialize_database(database)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT id, name FROM authors ORDER BY name COLLATE NOCASE")]


def _series_choices(database: Path) -> list[dict[str, str]]:
    database = initialize_database(database)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            """SELECT s.id, s.title, a.name AS author_name
               FROM series s JOIN authors a ON a.id = s.author_id
               ORDER BY a.name COLLATE NOCASE, s.title COLLATE NOCASE"""
        )]


def _issues_for_series(database: Path, series_id: str) -> list[dict[str, object]]:
    database = initialize_database(database)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id, issue_number, title, sort_order FROM issues WHERE series_id = ?
               ORDER BY COALESCE(sort_order, 2147483647), COALESCE(issue_number, title, source_key) COLLATE NOCASE""",
            (series_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _bool_form(value: str) -> bool | None:
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


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


def _search_library(database: Path, query: str, *, limit: int = 100) -> list[dict[str, object]]:
    query = query.strip()
    if not query:
        return []

    database = initialize_database(database)
    pattern = f"%{query}%"
    results: list[dict[str, object]] = []

    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row

        for row in db.execute(
            """SELECT id, name
               FROM authors
               WHERE name LIKE ? COLLATE NOCASE
               ORDER BY CASE WHEN name = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                        name COLLATE NOCASE
               LIMIT ?""",
            (pattern, query, limit),
        ):
            results.append({
                "type": "Author",
                "title": row["name"],
                "context": None,
                "url": f"/authors/{row['id']}",
            })

        for row in db.execute(
            """SELECT s.id, s.title, a.name AS author_name
               FROM series s
               JOIN authors a ON a.id = s.author_id
               WHERE s.title LIKE ? COLLATE NOCASE
               ORDER BY CASE WHEN s.title = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                        a.name COLLATE NOCASE, s.title COLLATE NOCASE
               LIMIT ?""",
            (pattern, query, limit),
        ):
            results.append({
                "type": "Series",
                "title": row["title"],
                "context": row["author_name"],
                "url": f"/series/{row['id']}",
            })

        for row in db.execute(
            """SELECT i.id, i.issue_number, i.title AS issue_title,
                      s.title AS series_title, a.name AS author_name
               FROM issues i
               JOIN series s ON s.id = i.series_id
               JOIN authors a ON a.id = s.author_id
               WHERE COALESCE(i.issue_number, '') LIKE ? COLLATE NOCASE
                  OR COALESCE(i.title, '') LIKE ? COLLATE NOCASE
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
            })

        for row in db.execute(
            """SELECT g.id, g.name, g.issue_id, g.role,
                      s.title AS series_title, a.name AS author_name,
                      i.issue_number, i.title AS issue_title
               FROM content_groups g
               JOIN series s ON s.id = g.series_id
               JOIN authors a ON a.id = s.author_id
               LEFT JOIN issues i ON i.id = g.issue_id
               WHERE g.role != 'primary'
                 AND g.name LIKE ? COLLATE NOCASE
               ORDER BY a.name COLLATE NOCASE, s.title COLLATE NOCASE, g.name COLLATE NOCASE
               LIMIT ?""",
            (pattern, limit),
        ):
            context = f"{row['author_name']} / {row['series_title']}"
            if row["issue_id"]:
                issue_label = row["issue_number"] or row["issue_title"] or "One-shot"
                context += f" / {issue_label}"
            results.append({
                "type": "Extra",
                "title": row["name"],
                "context": context,
                "url": f"/groups/{row['id']}",
            })

    # Prefer exact title/name hits and then stable alphabetical-ish presentation,
    # while keeping result types easy to scan.
    qfold = query.casefold()
    type_rank = {"Author": 0, "Series": 1, "Issue": 2, "Extra": 3}
    results.sort(
        key=lambda item: (
            0 if str(item["title"]).casefold() == qfold else 1,
            type_rank.get(str(item["type"]), 9),
            str(item["title"]).casefold(),
            str(item.get("context") or "").casefold(),
        )
    )
    return results[:limit]


def create_app(
    database_path: str | Path = "./comic_archive.sqlite3",
    library_root: str | Path = "./library",
    staging_root: str | Path = "./staging",
    secure_cookies: bool = False,
    import_root: str | Path | None = None,
) -> FastAPI:
    database = Path(database_path).expanduser().resolve()
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
    app.state.last_upload_sweep = 0.0

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
        return await call_next(request)

        user_id = request.session.get("user_id")
        user = get_user(database, user_id) if user_id else None
        if user is None:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)

        request.state.user = user
        admin_only = (
            path.startswith("/import")
            or path.startswith("/admin")
            or path == "/history"
            or "/edit" in path
            or (path.startswith("/media/") and request.method == "POST")
        )
        if admin_only and not user.is_admin:
            return HTMLResponse("Administrator access required", status_code=403)
        return await call_next(request)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # Avoid redirecting the browser's automatic favicon request through
        # the authentication flow. A real favicon can replace this later.
        return Response(status_code=204)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if request.session.get("user_id") and get_user(database, request.session["user_id"]):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        form = await _form_data(request)
        username = form.get("username", "").strip()
        client_host = request.client.host if request.client else "unknown"
        key = f"{client_host}|{username.casefold()}"
        now = time.monotonic()
        recent = [stamp for stamp in app.state.login_attempts.get(key, []) if now - stamp < 300]
        app.state.login_attempts[key] = recent
        if len(recent) >= 5:
            return templates.TemplateResponse(
                request=request, name="login.html",
                context={"error": "Too many failed login attempts. Try again in a few minutes."}, status_code=429,
            )

        user = authenticate(database, username, form.get("password", ""))
        if user is None:
            recent.append(now)
            app.state.login_attempts[key] = recent
            return templates.TemplateResponse(
                request=request, name="login.html",
                context={"error": "Invalid username or password."}, status_code=401,
            )

        app.state.login_attempts.pop(key, None)
        csrf_token = request.session.get("csrf_token") or secrets.token_urlsafe(32)
        request.session.clear()
        request.session["csrf_token"] = csrf_token
        request.session["user_id"] = user.id
        request.session["session_version"] = user.session_version
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        await _form_data(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="users.html",
            context={"users": list_users(database), "error": None},
        )

    @app.post("/admin/users", response_class=HTMLResponse)
    async def users_create(request: Request):
        form = await _form_data(request)
        try:
            create_user(
                database,
                form.get("username", ""),
                form.get("password", ""),
                is_admin=form.get("is_admin") == "yes",
            )
        except AuthError as exc:
            return templates.TemplateResponse(
                request=request, name="users.html",
                context={"users": list_users(database), "error": str(exc)}, status_code=400,
            )
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/reset-password", response_class=HTMLResponse)
    async def user_reset_password(request: Request, user_id: str):
        form = await _form_data(request)
        try:
            reset_password(database, user_id, form.get("password", ""))
        except AuthError as exc:
            return templates.TemplateResponse(
                request=request, name="users.html",
                context={"users": list_users(database), "error": str(exc)}, status_code=400,
            )
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/toggle-active")
    async def user_toggle_active(request: Request, user_id: str):
        await _form_data(request)
        target = next((u for u in list_users(database) if u.id == user_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Account not found")
        current = request.state.user
        if target.id == current.id and target.active:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        set_user_active(database, user_id, not target.active)
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/toggle-admin")
    async def user_toggle_admin(request: Request, user_id: str):
        await _form_data(request)
        target = next((u for u in list_users(database) if u.id == user_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Account not found")
        if target.id == request.state.user.id and target.is_admin:
            raise HTTPException(status_code=400, detail="You cannot remove your own administrator access")
        set_user_admin(database, user_id, not target.is_admin)
        return RedirectResponse("/admin/users", status_code=303)

    @app.get("/account/password", response_class=HTMLResponse)
    def password_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="change_password.html", context={"error": None, "success": None}
        )

    @app.post("/account/password", response_class=HTMLResponse)
    async def password_save(request: Request):
        form = await _form_data(request)
        new_password = form.get("new_password", "")
        if new_password != form.get("confirm_password", ""):
            return templates.TemplateResponse(
                request=request, name="change_password.html",
                context={"error": "New passwords do not match.", "success": None}, status_code=400,
            )
        try:
            user = change_password(
                database,
                request.state.user.id,
                form.get("current_password", ""),
                new_password,
            )
        except AuthError as exc:
            return templates.TemplateResponse(
                request=request, name="change_password.html",
                context={"error": str(exc), "success": None}, status_code=400,
            )
        request.session["session_version"] = user.session_version
        return templates.TemplateResponse(
            request=request, name="change_password.html",
            context={"error": None, "success": "Password changed. Other existing sessions for this account are now invalid."},
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        authors = read_library(database)
        series_count = sum(len(author.series) for author in authors)
        issue_count = sum(len(series.issues) for author in authors for series in author.series)
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={
                "authors": authors,
                "series_count": series_count,
                "issue_count": issue_count,
                "continue_reading": get_continue_reading(database, request.state.user.id),
            },
        )

    @app.post("/import/upload-session", response_class=JSONResponse)
    async def create_upload_session(request: Request):
        form = await _form_data(request)
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
        session = BrowserUploadSession(
            upload_root=upload_root,
            mode=mode,
            expected_files=expected_files,
        )
        app.state.upload_sessions[upload_id] = session
        _touch_browser_upload(session)
        return JSONResponse({"upload_id": upload_id, "expected_files": expected_files})

    @app.post("/import/upload-session/{upload_id}/file", response_class=JSONResponse)
    async def upload_session_file(request: Request, upload_id: str):
        upload = _upload_session_or_404(app, upload_id)
        relative = await _save_upload_file(request, upload)
        return JSONResponse({
            "relative_path": relative,
            "received_files": len(upload.received_paths),
            "expected_files": upload.expected_files,
        })

    @app.post("/import/upload-session/{upload_id}/cancel", response_class=JSONResponse)
    async def cancel_upload_session(request: Request, upload_id: str):
        await _form_data(request)
        upload = _upload_session_or_404(app, upload_id)
        upload.cancelled = True
        app.state.upload_sessions.pop(upload_id, None)
        _cleanup_upload(upload.upload_root)
        return JSONResponse({"cancelled": True})

    @app.post("/import/upload-session/{upload_id}/finalize", response_class=JSONResponse)
    async def finalize_upload_session(request: Request, upload_id: str):
        await _form_data(request)
        upload = _upload_session_or_404(app, upload_id)
        _touch_browser_upload(upload)
        if len(upload.received_paths) != upload.expected_files:
            return JSONResponse(
                {
                    "error": (
                        f"Upload is incomplete: received {len(upload.received_paths)} "
                        f"of {upload.expected_files} files."
                    )
                },
                status_code=409,
            )

        top_level = _upload_top_level(upload)
        source_path = _uploaded_source(upload.upload_root, top_level)
        try:
            if upload.mode == "bulk":
                source, candidates = discover_artist_comics(source_path)
                bulk_id = str(uuid4())
                app.state.bulk_import_sessions[bulk_id] = BulkArtistSession(
                    source=source,
                    author=source.name,
                    candidates=candidates,
                    upload_root=upload.upload_root,
                )
                _touch_bulk_activity(app.state.bulk_import_sessions[bulk_id])
                redirect = f"/import/bulk/{bulk_id}"
            else:
                pdf_cache_root = _new_pdf_cache(staging, upload.upload_root)
                scan = scan_folder(source_path, pdf_cache_root=pdf_cache_root)
                plan = build_review_plan(scan)
                session_id = str(uuid4())
                app.state.import_sessions[session_id] = ImportSession(
                    plan=plan,
                    series_default=scan.content_root.name,
                    upload_root=upload.upload_root,
                    pdf_cache_root=pdf_cache_root,
                )
                _touch_import_activity(app, app.state.import_sessions[session_id])
                redirect = f"/import/{session_id}/review"
        except (FolderScanError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        app.state.upload_sessions.pop(upload_id, None)
        return JSONResponse({"redirect": redirect})

    @app.get("/import/bulk", response_class=HTMLResponse)
    def bulk_import_start(request: Request, folder: str = ""):
        selected = _safe_import_folder(imports, folder) if folder else None
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
            upload_root, top_level = await _save_uploaded_folder(request, staging)
            source_path = _uploaded_source(upload_root, top_level)
            source, candidates = discover_artist_comics(source_path)
        except HTTPException:
            _cleanup_upload(upload_root)
            raise
        except (FolderScanError, OSError) as exc:
            _cleanup_upload(upload_root)
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_start.html",
                context={"error": str(exc)},
                status_code=400,
            )

        bulk_id = str(uuid4())
        app.state.bulk_import_sessions[bulk_id] = BulkArtistSession(
            source=source,
            author=source.name,
            candidates=candidates,
            upload_root=upload_root,
        )
        _touch_bulk_activity(app.state.bulk_import_sessions[bulk_id])
        return RedirectResponse(f"/import/bulk/{bulk_id}", status_code=303)

    @app.post("/import/bulk/scan", response_class=HTMLResponse)
    async def bulk_import_scan(request: Request):
        form = await _form_data(request)
        selected_path = form.get("selected_path", "").strip() or form.get("source_path", "").strip()
        try:
            source_path = _safe_import_folder(imports, selected_path)
            source, candidates = discover_artist_comics(source_path)
        except (FolderScanError, OSError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_start.html",
                context={"error": str(exc), "selected_path": selected_path},
                status_code=400,
            )
        bulk_id = str(uuid4())
        app.state.bulk_import_sessions[bulk_id] = BulkArtistSession(
            source=source,
            author=form.get("author", "").strip() or source.name,
            candidates=candidates,
        )
        return RedirectResponse(f"/import/bulk/{bulk_id}", status_code=303)

    @app.get("/import/bulk/{bulk_id}", response_class=HTMLResponse)
    def bulk_import_choose(request: Request, bulk_id: str):
        bulk = _bulk_session_or_404(app, bulk_id)
        _touch_bulk_activity(bulk)
        return templates.TemplateResponse(
            request=request,
            name="import_bulk_choose.html",
            context={"bulk_id": bulk_id, "bulk": bulk, "error": None},
        )

    @app.post("/import/bulk/{bulk_id}/start", response_class=HTMLResponse)
    async def bulk_import_begin(request: Request, bulk_id: str):
        bulk = _bulk_session_or_404(app, bulk_id)
        form = await _form_data(request)
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
        _touch_bulk_activity(bulk)
        try:
            session_id = _start_bulk_item(app, bulk_id)
        except (FolderScanError, OSError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="import_bulk_choose.html",
                context={"bulk_id": bulk_id, "bulk": bulk, "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.get("/import/bulk/{bulk_id}/done", response_class=HTMLResponse)
    def bulk_import_done(request: Request, bulk_id: str):
        bulk = _bulk_session_or_404(app, bulk_id)
        return templates.TemplateResponse(
            request=request,
            name="import_bulk_done.html",
            context={"bulk": bulk},
        )

    @app.get("/import", response_class=HTMLResponse)
    def import_start(request: Request, folder: str = ""):
        selected = _safe_import_folder(imports, folder) if folder else None
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
        current, relative, folders, parent = _import_folder_listing(imports, path)
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
            upload_root, top_level = await _save_uploaded_folder(request, staging)
            source_path = _uploaded_source(upload_root, top_level)
            pdf_cache_root = _new_pdf_cache(staging, upload_root)
            scan = scan_folder(source_path, pdf_cache_root=pdf_cache_root)
            plan = build_review_plan(scan)
        except HTTPException:
            _cleanup_upload(upload_root)
            raise
        except (FolderScanError, OSError) as exc:
            _cleanup_upload(upload_root)
            return templates.TemplateResponse(
                request=request,
                name="import_start.html",
                context={"error": str(exc)},
                status_code=400,
            )

        session_id = str(uuid4())
        app.state.import_sessions[session_id] = ImportSession(
            plan=plan,
            series_default=scan.content_root.name,
            upload_root=upload_root,
            pdf_cache_root=pdf_cache_root,
        )
        _touch_import_activity(app, app.state.import_sessions[session_id])
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.post("/import/scan", response_class=HTMLResponse)
    async def import_scan(request: Request):
        form = await _form_data(request)
        selected_path = form.get("selected_path", "").strip() or form.get("source_path", "").strip()
        try:
            source_path = _safe_import_folder(imports, selected_path)
            pdf_cache_root = _new_pdf_cache(staging)
            scan = scan_folder(source_path, pdf_cache_root=pdf_cache_root)
            plan = build_review_plan(scan)
        except (FolderScanError, OSError) as exc:
            return templates.TemplateResponse(request=request, name="import_start.html", context={"error": str(exc), "selected_path": selected_path}, status_code=400)
        session_id = str(uuid4())
        app.state.import_sessions[session_id] = ImportSession(plan=plan, pdf_cache_root=pdf_cache_root)
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.get("/import/{session_id}/review", response_class=HTMLResponse)
    def import_review(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        _touch_import_activity(app, session)
        tree = _workspace_tree(session)
        requested = request.query_params.get("folder", "")
        selected = requested if any(node["path"] == requested for node in tree) else str(tree[0]["path"])
        selected_node = next(node for node in tree if node["path"] == selected)
        media = _workspace_media_for_folder(session, selected)
        return templates.TemplateResponse(
            request=request,
            name="import_workspace.html",
            context={
                "session_id": session_id,
                "plan": session.plan,
                "tree": tree,
                "selected": selected,
                "selected_node": selected_node,
                "selected_media": media,
                "selected_metadata": _workspace_node_metadata(session, selected_node),
                "media_targets": _workspace_media_targets_for_folder(session, selected),
                "media_target_paths": {path for path, _ in _workspace_media_targets_for_folder(session, selected)},
                "author_value": session.workspace_author if session.workspace_author is not None else (session.author_default or ""),
                "series_value": session.workspace_series if session.workspace_series is not None else (session.series_default or session.plan.scan.content_root.name),
                "series_complete_value": session.workspace_series_complete,
                "errors": [],
            },
        )

    @app.post("/import/{session_id}/workspace/metadata")
    async def import_workspace_metadata(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        folder = form.get("folder_path", "").strip()
        tree = _workspace_tree(session)
        node = next((item for item in tree if str(item["path"]) == folder), None)
        if node is None:
            raise HTTPException(status_code=400, detail="Unknown workspace folder")
        if node["is_root"]:
            session.workspace_author = form.get("author", session.workspace_author or session.author_default or "").strip()
            session.workspace_series = form.get("series", session.workspace_series or session.series_default or session.plan.scan.content_root.name).strip()
            value = form.get("series_complete", session.workspace_series_complete).strip()
            if value not in {"", "yes", "no"}:
                raise HTTPException(status_code=400, detail="Invalid completeness value")
            session.workspace_series_complete = value
            if str(node["role"]) == "Issue":
                data = session.workspace_metadata.setdefault(folder, {})
                data["issue_number"] = form.get("issue_number", data.get("issue_number", "")).strip()
                data["title"] = form.get("title", data.get("title", "")).strip()
                issue_complete = form.get("issue_complete", data.get("complete", "")).strip()
                if issue_complete not in {"", "yes", "no"}:
                    raise HTTPException(status_code=400, detail="Invalid issue completeness value")
                data["complete"] = issue_complete
        else:
            data = session.workspace_metadata.setdefault(folder, {})
            role = str(node["role"])
            if role == "Issue":
                data["issue_number"] = form.get("issue_number", "").strip()
                data["title"] = form.get("title", "").strip()
                value = form.get("complete", "").strip()
                if value not in {"", "yes", "no"}:
                    raise HTTPException(status_code=400, detail="Invalid completeness value")
                data["complete"] = value
            elif role == "Sub-Series":
                title = form.get("title", "").strip()
                if not title:
                    raise HTTPException(status_code=400, detail="Sub-series title cannot be empty")
                data["title"] = title
                value = form.get("complete", "").strip()
                if value not in {"", "yes", "no"}:
                    raise HTTPException(status_code=400, detail="Invalid completeness value")
                data["complete"] = value
            elif role in {"Issue-Extras", "Series-Extras"}:
                title = form.get("title", "").strip()
                if not title:
                    raise HTTPException(status_code=400, detail="Group name cannot be empty")
                if str(node["path"]).startswith("virtual:"):
                    group_id = str(node["path"]).split(":", 1)[1]
                    session.workspace_virtual_groups[group_id]["name"] = title
                else:
                    item = node.get("item")
                    if item is not None:
                        session.plan.set_name(item.relative_path, title)
                data["title"] = title
        return Response(status_code=204)

    @app.post("/import/{session_id}/workspace/create-group")
    async def import_workspace_create_group(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        owner = form.get("owner", "").strip()
        name = form.get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Extra group name cannot be empty")
        tree = _workspace_tree(session)
        node = next((item for item in tree if str(item["path"]) == owner and str(item["role"]) == "Issue"), None)
        if node is None:
            raise HTTPException(status_code=400, detail="Extra groups must be created under an issue")
        existing = [
            str(item["name"]).casefold() for item in tree
            if str(item.get("parent") or "") == owner and str(item["role"]) == "Issue-Extras"
        ]
        if name.casefold() in existing:
            raise HTTPException(status_code=400, detail="This issue already has an extra group with that name")
        group_id = str(uuid4())
        session.workspace_virtual_groups[group_id] = {"owner": owner, "name": name}
        return JSONResponse({"group_path": f"virtual:{group_id}"})

    @app.post("/import/{session_id}/workspace/remove-group")
    async def import_workspace_remove_group(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        folder = form.get("folder_path", "").strip()
        if not folder.startswith("virtual:"):
            raise HTTPException(status_code=400, detail="Only groups created in this workspace can be removed here")
        group_id = folder.split(":", 1)[1]
        if group_id not in session.workspace_virtual_groups:
            raise HTTPException(status_code=404, detail="Workspace group not found")
        # Return assigned pages to their original scanned groups.
        for source_path, target in list(session.workspace_media_targets.items()):
            if target == folder:
                session.workspace_media_targets.pop(source_path, None)
        session.workspace_virtual_groups.pop(group_id, None)
        session.workspace_metadata.pop(folder, None)
        return Response(status_code=204)

    @app.post("/import/{session_id}/workspace/media-target")
    async def import_workspace_media_target(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        source_path = form.get("source_path", "").strip()
        target = form.get("target", "").strip()
        item = next((media for media in _workspace_all_media(session) if str(media.path) == source_path), None)
        if item is None:
            raise HTTPException(status_code=400, detail="Unknown page")
        allowed = {value for node in _workspace_tree(session) for value, _ in _workspace_media_targets_for_folder(session, str(node["path"]))}
        if target not in allowed:
            raise HTTPException(status_code=400, detail="Invalid target group")
        origin = _workspace_origin_for_media(session, source_path)
        if target == origin:
            session.workspace_media_targets.pop(source_path, None)
        else:
            session.workspace_media_targets[source_path] = target
        return Response(status_code=204)

    @app.post("/import/{session_id}/workspace/media-targets")
    async def import_workspace_media_targets(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        target = form.get("target", "").strip()
        try:
            source_paths = json.loads(form.get("source_paths", "[]"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid page selection") from exc
        if not isinstance(source_paths, list) or not source_paths or any(not isinstance(value, str) for value in source_paths):
            raise HTTPException(status_code=400, detail="Select at least one page")
        if len(set(source_paths)) != len(source_paths):
            raise HTTPException(status_code=400, detail="Duplicate pages in selection")

        all_media = {str(media.path): media for media in _workspace_all_media(session)}
        for source_path in source_paths:
            if source_path not in all_media:
                raise HTTPException(status_code=400, detail="Unknown page")
            current = session.workspace_media_targets.get(source_path) or _workspace_origin_for_media(session, source_path)
            if current is None:
                raise HTTPException(status_code=400, detail="Could not determine page group")
            allowed = {value for value, _ in _workspace_media_targets_for_folder(session, current)}
            if target not in allowed:
                raise HTTPException(status_code=400, detail="Pages can only be moved within their current issue")

        for source_path in source_paths:
            origin = _workspace_origin_for_media(session, source_path)
            if target == origin:
                session.workspace_media_targets.pop(source_path, None)
            else:
                session.workspace_media_targets[source_path] = target
        return Response(status_code=204)

    @app.post("/import/{session_id}/workspace/finalize", response_class=HTMLResponse)
    async def import_workspace_finalize(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        # Save the root metadata from the final form as well, so a user can type
        # and immediately continue without needing a blur/change event first.
        session.workspace_author = form.get("author", session.workspace_author or session.author_default or "").strip()
        session.workspace_series = form.get("series", session.workspace_series or session.series_default or session.plan.scan.content_root.name).strip()
        session.workspace_series_complete = form.get("series_complete", session.workspace_series_complete).strip()
        issue_metadata: dict[str, dict[str, object]] = {}
        subseries_metadata: dict[str, dict[str, object]] = {}
        issue_items = _workspace_sorted_review_items(session, ReviewRole.ISSUE) if session.plan.scan.is_series_candidate else [None]
        subseries_items = _workspace_sorted_review_items(session, ReviewRole.SUBSERIES) if session.plan.scan.is_series_candidate else []
        if session.plan.scan.is_series_candidate:
            for index, item in enumerate(subseries_items, start=1):
                key = _workspace_item_key(session, item)
                meta = session.workspace_metadata.get(key, {})
                # The staged model gets its title from ReviewItem.name.
                title = meta.get("title", item.name).strip()
                if title:
                    item.name = title
                subseries_metadata[str(item.relative_path)] = {"complete": meta.get("complete", ""), "sort_order": index}
            for index, item in enumerate(issue_items, start=1):
                key = _workspace_item_key(session, item)
                meta = session.workspace_metadata.get(key, {})
                issue_metadata[str(item.relative_path)] = {
                    "issue_number": meta.get("issue_number", item.name),
                    "title": meta.get("title", ""),
                    "complete": meta.get("complete", ""),
                    "sort_order": index,
                }
        else:
            root_key = _workspace_source_relative(session.plan.scan, session.plan.scan.content_root)
            meta = session.workspace_metadata.get(root_key, {})
            issue_metadata["."] = {
                "issue_number": meta.get("issue_number", ""),
                "title": meta.get("title", ""),
                "complete": meta.get("complete", ""),
                "sort_order": 1,
            }
        # Reapply editable folder/group labels after any rescans that may have
        # rebuilt the ReviewPlan while the workspace remained open.
        for node in _workspace_tree(session):
            path = str(node["path"])
            if path.startswith("virtual:"):
                continue
            meta = session.workspace_metadata.get(path, {})
            title = meta.get("title", "").strip()
            item = node.get("item")
            if title and item is not None and str(node["role"]) in {"Sub-Series", "Issue-Extras", "Series-Extras"}:
                item.name = title

        try:
            staged = build_staged_import(
                session.plan,
                author=session.workspace_author or "",
                series=session.workspace_series or "",
                issue_metadata=issue_metadata,
                subseries_metadata=subseries_metadata,
                series_complete=_bool_form(session.workspace_series_complete),
            )
            # Add workspace-created groups, then apply page moves into either
            # existing or virtual groups.
            virtual_paths: dict[str, str] = {}
            for group_id, group in session.workspace_virtual_groups.items():
                owner_key = group["owner"]
                owner_item = next((item for item in issue_items if item is not None and _workspace_item_key(session, item) == owner_key), None)
                if owner_item is None and not session.plan.scan.is_series_candidate:
                    staged_owner = "."
                elif owner_item is not None:
                    staged_owner = str(owner_item.relative_path)
                else:
                    raise StagingError(f"Could not resolve issue owner for extra group {group['name']}")
                virtual_paths[f"virtual:{group_id}"] = create_staged_issue_extra(staged, staged_owner, group["name"])
            tree = _workspace_tree(session)
            real_target_paths: dict[str, tuple[str, str]] = {}
            for node in tree:
                path = str(node["path"])
                if str(node["role"]) not in {"Issue", "Issue-Extras"} or path.startswith("virtual:"):
                    continue
                item = node.get("item")
                if str(node["role"]) == "Issue":
                    owner_key = str(item.relative_path) if item is not None else "."
                    staged_issue = next((issue for issue in staged.issues if issue.source_key == owner_key), None)
                    if staged_issue:
                        primary = next((group for group in staged_issue.groups if group.role == ReviewRole.PRIMARY.value), None)
                        if primary:
                            real_target_paths[path] = (owner_key, primary.relative_path)
                elif item is not None and getattr(item, "issue_path", None) is not None:
                    owner_key = str(item.issue_path)
                    staged_issue = next((issue for issue in staged.issues if issue.source_key == owner_key), None)
                    if staged_issue:
                        group = next((group for group in staged_issue.groups if group.relative_path == str(item.relative_path)), None)
                        if group:
                            real_target_paths[path] = (owner_key, group.relative_path)
            for source_path, target in session.workspace_media_targets.items():
                if target in virtual_paths:
                    virtual_group = virtual_paths[target]
                    group_info = session.workspace_virtual_groups[target.split(":", 1)[1]]
                    owner_item = next((item for item in issue_items if item is not None and _workspace_item_key(session, item) == group_info["owner"]), None)
                    owner_key = str(owner_item.relative_path) if owner_item is not None else "."
                    move_staged_media(staged, owner_key, source_path, virtual_group)
                elif target in real_target_paths:
                    owner_key, group_path = real_target_paths[target]
                    move_staged_media(staged, owner_key, source_path, group_path)
            empty = [group.name for issue in staged.issues for group in issue.groups if not group.media]
            if empty:
                raise StagingError(
                    "Empty content group" + ("s" if len(empty) != 1 else "") + ": " + ", ".join(empty) +
                    ". Add pages to the group or remove it before continuing."
                )
            staging.mkdir(parents=True, exist_ok=True)
            staging_path = staged.save(staging / f"{staged.staging_id}.json")
            session.staged = staged
            session.staging_path = staging_path
        except (StagingError, ReviewError) as exc:
            tree = _workspace_tree(session)
            requested = form.get("selected", "")
            selected = requested if any(str(node["path"]) == requested for node in tree) else str(tree[0]["path"])
            selected_node = next(node for node in tree if str(node["path"]) == selected)
            return templates.TemplateResponse(
                request=request,
                name="import_workspace.html",
                context={
                    "session_id": session_id, "plan": session.plan, "tree": tree, "selected": selected,
                    "selected_node": selected_node, "selected_media": _workspace_media_for_folder(session, selected),
                    "selected_metadata": _workspace_node_metadata(session, selected_node),
                    "media_targets": _workspace_media_targets_for_folder(session, selected),
                    "author_value": session.workspace_author or "", "series_value": session.workspace_series or "",
                    "series_complete_value": session.workspace_series_complete, "errors": [str(exc)],
                }, status_code=400,
            )
        return RedirectResponse(f"/import/{session_id}/confirm", status_code=303)

    @app.post("/import/{session_id}/workspace/role")
    async def import_workspace_role(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        folder_key = form.get("folder_path", "").strip()
        role = form.get("role", "").strip()
        if not folder_key or role not in {"sub-series", "issue", "issue-extras", "series-extras", "primary-pages", "container"}:
            raise HTTPException(status_code=400, detail="Invalid workspace folder role")
        candidate = (session.plan.scan.source / folder_key).resolve() if folder_key != "." else session.plan.scan.source.resolve()
        try:
            candidate.relative_to(session.plan.scan.source.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid workspace folder path") from exc
        if not candidate.is_dir() or candidate.resolve() == session.plan.scan.content_root.resolve():
            raise HTTPException(status_code=400, detail="The import root role is determined by the comic structure")

        old_extra = set(session.extra_folder_overrides)
        old_primary = set(session.primary_folder_overrides)
        old_subseries = set(session.subseries_folder_overrides)
        old_container = set(session.container_folder_overrides)
        session.extra_folder_overrides.discard(folder_key)
        session.primary_folder_overrides.discard(folder_key)
        session.subseries_folder_overrides.discard(folder_key)
        session.container_folder_overrides.discard(folder_key)
        if role in {"issue-extras", "series-extras"}:
            session.extra_folder_overrides.add(folder_key)
        elif role == "sub-series":
            session.subseries_folder_overrides.add(folder_key)
        elif role in {"primary-pages", "container"}:
            session.primary_folder_overrides.add(folder_key)
            if role == "container":
                session.container_folder_overrides.add(folder_key)
        try:
            _rescan_import_session(session)
            mapped = _workspace_role_map(session.plan).get(folder_key)
            if role == "series-extras" and mapped and mapped[1] is not None:
                session.plan.set_role(mapped[1].relative_path, ReviewRole.SERIES_EXTRA)
            elif role == "issue-extras" and mapped and mapped[1] is not None:
                session.plan.set_role(mapped[1].relative_path, ReviewRole.ISSUE_EXTRA)
            elif role == "issue":
                if not mapped or mapped[0] != "Issue":
                    raise ReviewError(
                        "That folder cannot become an issue at its current level. "
                        "If it is inside another issue, first make its parent a sub-series."
                    )
        except (FolderScanError, ReviewError, OSError) as exc:
            session.extra_folder_overrides = old_extra
            session.primary_folder_overrides = old_primary
            session.subseries_folder_overrides = old_subseries
            session.container_folder_overrides = old_container
            _rescan_import_session(session)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/import/{session_id}/review?folder={folder_key}", status_code=303)

    @app.post("/import/{session_id}/workspace/folder-order")
    async def import_workspace_folder_order(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        parent = form.get("parent", ".").strip() or "."
        try:
            ordered = json.loads(form.get("ordered_paths", "[]"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid folder ordering") from exc
        if not isinstance(ordered, list) or not all(isinstance(item, str) for item in ordered):
            raise HTTPException(status_code=400, detail="Invalid folder ordering")
        tree = _workspace_tree(session)
        siblings = {str(node["path"]) for node in tree if not node["is_root"] and (node["parent"] or ".") == parent}
        if set(ordered) != siblings:
            raise HTTPException(status_code=400, detail="Folder order must contain every sibling exactly once")
        session.folder_order_overrides[parent] = ordered
        return Response(status_code=204)

    @app.post("/import/{session_id}/workspace/media-order")
    async def import_workspace_media_order(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        folder = form.get("folder_path", "").strip()
        try:
            ordered = json.loads(form.get("ordered_paths", "[]"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid page ordering") from exc
        if not isinstance(ordered, list) or not all(isinstance(item, str) for item in ordered):
            raise HTTPException(status_code=400, detail="Invalid page ordering")
        current = _workspace_media_for_folder(session, folder)
        if set(ordered) != {str(item.path) for item in current} or len(ordered) != len(current):
            raise HTTPException(status_code=400, detail="Page order must contain every displayed page exactly once")
        _workspace_reorder_media(session, ordered)
        return Response(status_code=204)

    @app.post("/import/{session_id}/review/mark-extra", response_class=HTMLResponse)
    async def import_review_mark_extra(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        relative_path = form.get("folder_path", "").strip()
        candidates = {str(item.relative_path): item for item in flattened_folder_candidates(session.plan.scan)}
        if relative_path not in candidates:
            raise HTTPException(status_code=400, detail="Folder is not an available flattened folder")
        session.extra_folder_overrides.add(relative_path)
        try:
            _rescan_import_session(session)
        except (FolderScanError, OSError) as exc:
            session.extra_folder_overrides.discard(relative_path)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.post("/import/{session_id}/review/mark-subseries", response_class=HTMLResponse)
    async def import_review_mark_subseries(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
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
            _rescan_import_session(session)
        except (FolderScanError, OSError) as exc:
            session.subseries_folder_overrides.discard(relative_path)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/import/{session_id}/review", status_code=303)

    @app.post("/import/{session_id}/review", response_class=HTMLResponse)
    async def import_review_save(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        errors: list[str] = []
        for index, item in enumerate(session.plan.items):
            try:
                session.plan.set_name(item.relative_path, form.get(f"name_{index}", item.name))
                session.plan.set_role(item.relative_path, form.get(f"role_{index}", item.role.value))
            except ReviewError as exc:
                errors.append(str(exc))
        errors.extend(session.plan.validation_errors())
        if errors:
            role_choices = {
                index: _review_role_choices(item, is_series=session.plan.scan.is_series_candidate)
                for index, item in enumerate(session.plan.items)
            }
            return templates.TemplateResponse(
                request=request,
                name="import_review.html",
                context={
                    "session_id": session_id, "plan": session.plan, "role_choices": role_choices, "errors": errors,
                    "flattened_folders": [
                        item for item in flattened_folder_candidates(session.plan.scan)
                        if str(item.relative_path) not in session.extra_folder_overrides
                    ],
                    "subseries_candidates": {
                        index: _review_source_relative(session.plan.scan, item.relative_path)
                        for index, item in enumerate(session.plan.items)
                        if item.source_kind == "issue"
                    },
                },
                status_code=400,
            )
        return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)

    @app.get("/import/{session_id}/metadata", response_class=HTMLResponse)
    def import_metadata(request: Request, session_id: str):
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
        _touch_bulk_activity(bulk)
        return Response(status_code=204)

    @app.get("/import/{session_id}/confirm", response_class=HTMLResponse)
    def import_confirm(request: Request, session_id: str):
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
        bulk.current_position += 1
        _touch_bulk_activity(bulk)

        try:
            next_session_id = _start_bulk_item(app, bulk_id)
        except (FolderScanError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if next_session_id is not None:
            return RedirectResponse(f"/import/{next_session_id}/review", status_code=303)

        _cleanup_upload(bulk.upload_root)
        bulk.upload_root = None
        return RedirectResponse(f"/import/bulk/{bulk_id}/done", status_code=303)

    @app.post("/import/{session_id}/commit", response_class=HTMLResponse)
    async def import_commit(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        if session.staged is None:
            return RedirectResponse(f"/import/{session_id}/metadata", status_code=303)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        allow_duplicate = form.get("allow_duplicate") == "yes"
        try:
            result = commit_staged_import(
                session.staged,
                library_root=library,
                database_path=database,
                allow_duplicate=allow_duplicate,
            )
        except CommitError as exc:
            message = str(exc)
            duplicate = "Possible duplicate import detected" in message
            return templates.TemplateResponse(
                request=request,
                name="import_confirm.html",
                context={"session_id": session_id, "staged": session.staged, "error": message, "duplicate": duplicate, "is_bulk": bool(session.bulk_id)},
                status_code=409 if duplicate else 400,
            )
        app.state.import_sessions.pop(session_id, None)
        _remove_staging_record(session)
        if session.bulk_id:
            bulk = _bulk_session_or_404(app, session.bulk_id)
            bulk.imported_series.append((session.staged.series, result.series_id))
            bulk.current_position += 1
            try:
                next_session_id = _start_bulk_item(app, session.bulk_id)
            except (FolderScanError, OSError) as exc:
                return templates.TemplateResponse(
                    request=request,
                    name="import_done.html",
                    context={
                        "result": result,
                        "staged": session.staged,
                        "bulk_error": str(exc),
                        "next_url": None,
                        "bulk_progress": f"{bulk.current_position} of {len(bulk.selected)}",
                    },
                    status_code=400,
                )
            if next_session_id is not None:
                return templates.TemplateResponse(
                    request=request,
                    name="import_done.html",
                    context={
                        "result": result,
                        "staged": session.staged,
                        "next_url": f"/import/{next_session_id}/review",
                        "bulk_progress": f"{bulk.current_position} of {len(bulk.selected)}",
                        "bulk_error": None,
                    },
                )
            _cleanup_upload(bulk.upload_root)
            bulk.upload_root = None
            return RedirectResponse(f"/import/bulk/{session.bulk_id}/done", status_code=303)

        _cleanup_upload(session.upload_root)
        return templates.TemplateResponse(
            request=request,
            name="import_done.html",
            context={"result": result, "staged": session.staged, "next_url": None, "bulk_progress": None, "bulk_error": None},
        )

    @app.get("/maintenance", response_class=HTMLResponse)
    def maintenance_page(request: Request):
        report = build_maintenance_report(database, library)
        return templates.TemplateResponse(
            request=request,
            name="maintenance.html",
            context={"report": report},
        )

    @app.post("/series/{series_id}/missing/{issue_number}")
    async def set_missing_issue_state(request: Request, series_id: str, issue_number: int):
        form = await _form_data(request)
        intentional = form.get("intentional") == "yes"
        try:
            set_intentional_gap(
                database,
                series_id,
                issue_number,
                intentional,
                form.get("note", ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target = form.get("return_to", f"/series/{series_id}")
        if not target.startswith("/"):
            target = f"/series/{series_id}"
        return RedirectResponse(target, status_code=303)

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = ""):
        query = q.strip()
        results = _search_library(database, query) if query else []
        return templates.TemplateResponse(
            request=request,
            name="search.html",
            context={"query": query, "results": results},
        )

    @app.get("/authors/{author_id}", response_class=HTMLResponse)
    def author_page(request: Request, author_id: str):
        authors = read_library(database)
        author = _find_author(authors, author_id)
        if author is None:
            raise HTTPException(status_code=404, detail="Author not found")
        return templates.TemplateResponse(
            request=request,
            name="author.html",
            context={"author": author, "series_previews": _series_preview_map(author)},
        )

    @app.get("/series/{series_id}", response_class=HTMLResponse)
    def series_page(request: Request, series_id: str):
        authors = read_library(database)
        found = _find_series(authors, series_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Series not found")
        author, series = found
        return templates.TemplateResponse(
            request=request,
            name="series.html",
            context={
                "author": author,
                "series": series,
                "issue_previews": _issue_preview_map(series),
                "series_previews": _series_preview_map(author),
                "lineage": _series_lineage(author, series),
                "progress": get_progress_map(database, request.state.user.id, [issue.id for issue in series.issues]),
                "missing_gaps": series_gaps(database, series.id),
            },
        )

    @app.get("/issues/{issue_id}", response_class=HTMLResponse)
    def issue_page(request: Request, issue_id: str):
        authors = read_library(database)
        found = _find_issue(authors, issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        return templates.TemplateResponse(
            request=request,
            name="issue.html",
            context={
                "author": author,
                "series": series,
                "lineage": _series_lineage(author, series),
                "issue": issue,
                "primary": _primary_group(issue),
                "extras": [group for group in issue.groups if group.role != "primary"],
                "reading_progress": get_progress(database, request.state.user.id, issue.id),
            },
        )

    @app.get("/groups/{group_id}", response_class=HTMLResponse)
    def group_page(request: Request, group_id: str):
        authors = read_library(database)
        found = _find_group(authors, group_id)
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

    def _reader_response(request: Request, *, author: AuthorView, series: SeriesView, issue: IssueView | None, group: GroupView, page: int):
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
                "author": author, "series": series, "issue": issue, "group": group,
                "media": group.media, "page_index": page_index, "back_url": back_url,
                "back_label": back_label, "reader_title": reader_title,
                "track_progress": track_progress,
            },
        )

    @app.get("/read/{issue_id}", response_class=HTMLResponse)
    def reader(request: Request, issue_id: str, page: int | None = None):
        authors = read_library(database)
        found = _find_issue(authors, issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        group = _primary_group(issue)
        if group is None:
            raise HTTPException(status_code=404, detail="Issue has no readable primary media")
        if page is None:
            saved = get_progress(database, request.state.user.id, issue.id)
            page = saved.page if saved is not None and not saved.completed else 1
        return _reader_response(request, author=author, series=series, issue=issue, group=group, page=page)

    @app.get("/read-group/{group_id}", response_class=HTMLResponse)
    def group_reader(request: Request, group_id: str, page: int = 1):
        authors = read_library(database)
        found = _find_group(authors, group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        author, series, issue, group = found
        return _reader_response(request, author=author, series=series, issue=issue, group=group, page=page)

    @app.post("/progress/{issue_id}")
    async def progress_save(request: Request, issue_id: str):
        form = await _form_data(request)
        try:
            page = int(form.get("page", "1"))
            total_pages = int(form.get("total_pages", "1"))
            progress = save_progress(database, request.state.user.id, issue_id, page, total_pages)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"page": progress.page, "completed": progress.completed}

    @app.post("/progress/{issue_id}/reset")
    async def progress_reset(request: Request, issue_id: str):
        await _form_data(request)
        reset_progress(database, request.state.user.id, issue_id)
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)

    @app.get("/authors/{author_id}/edit", response_class=HTMLResponse)
    def author_edit_page(request: Request, author_id: str):
        author = _find_author(read_library(database), author_id)
        if author is None:
            raise HTTPException(status_code=404, detail="Author not found")
        return templates.TemplateResponse(request=request, name="edit_author.html", context={"author": author, "error": None})

    @app.post("/authors/{author_id}/edit", response_class=HTMLResponse)
    async def author_edit_save(request: Request, author_id: str):
        author = _find_author(read_library(database), author_id)
        if author is None:
            raise HTTPException(status_code=404, detail="Author not found")
        form = await _form_data(request)
        try:
            rename_author(database, author_id, form.get("name", ""))
        except EditError as exc:
            return templates.TemplateResponse(request=request, name="edit_author.html", context={"author": author, "error": str(exc)}, status_code=400)
        return RedirectResponse(f"/authors/{author_id}", status_code=303)

    @app.get("/series/{series_id}/edit", response_class=HTMLResponse)
    def series_edit_page(request: Request, series_id: str):
        found = _find_series(read_library(database), series_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Series not found")
        author, series = found
        return templates.TemplateResponse(
            request=request, name="edit_series.html",
            context={
                "author": author, "series": series, "authors": _author_choices(database),
                "parent_choices": [choice for choice in _series_choices(database) if choice["author_name"] == author.name and choice["id"] != series.id],
                "error": None,
            },
        )

    @app.post("/series/{series_id}/edit", response_class=HTMLResponse)
    async def series_edit_save(request: Request, series_id: str):
        found = _find_series(read_library(database), series_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Series not found")
        author, series = found
        form = await _form_data(request)
        try:
            edit_series(
                database, series_id, title=form.get("title", ""),
                author_id=form.get("author_id", author.id),
                complete=_bool_form(form.get("complete", "")),
                parent_series_id=form.get("parent_series_id", "") or None,
            )
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
            return templates.TemplateResponse(
                request=request, name="edit_series.html",
                context={
                    "author": author, "series": series, "authors": _author_choices(database),
                    "parent_choices": [choice for choice in _series_choices(database) if choice["author_name"] == author.name and choice["id"] != series.id],
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.get("/issues/{issue_id}/edit", response_class=HTMLResponse)
    def issue_edit_page(request: Request, issue_id: str):
        found = _find_issue(read_library(database), issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        return templates.TemplateResponse(
            request=request, name="edit_issue.html",
            context={
                "author": author,
                "series": series,
                "issue": issue,
                "primary": _primary_group(issue),
                "series_choices": _series_choices(database),
                "error": None,
            },
        )

    @app.post("/issues/{issue_id}/edit", response_class=HTMLResponse)
    async def issue_edit_save(request: Request, issue_id: str):
        found = _find_issue(read_library(database), issue_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        author, series, issue = found
        form = await _form_data(request)
        try:
            edit_issue(
                database, issue_id,
                issue_number=form.get("issue_number", "").strip() or None,
                title=form.get("title", "").strip() or None,
                complete=_bool_form(form.get("complete", "")),
                series_id=form.get("series_id", series.id),
            )
        except EditError as exc:
            return templates.TemplateResponse(
                request=request, name="edit_issue.html",
                context={
                    "author": author,
                    "series": series,
                    "issue": issue,
                    "primary": _primary_group(issue),
                    "series_choices": _series_choices(database),
                    "error": str(exc),
                },
                status_code=400,
            )
        # Issue may have moved to another series; resolve its new parent.
        refreshed = _find_issue(read_library(database), issue_id)
        return RedirectResponse(f"/issues/{issue_id}" if refreshed else "/", status_code=303)

    @app.post("/issues/{issue_id}/groups")
    async def issue_group_create(request: Request, issue_id: str):
        form = await _form_data(request)
        try:
            create_issue_extra_group(database, issue_id, form.get("name", ""))
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/issues/{issue_id}/edit", status_code=303)

    @app.post("/groups/{group_id}/move-media")
    async def group_move_media(request: Request, group_id: str):
        found = _find_group(read_library(database), group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        _, _, issue, _ = found
        if issue is None:
            raise HTTPException(status_code=400, detail="Series extras cannot move individual media into issue groups")
        form = await _form_data(request)
        selected = [
            media_id
            for key, media_id in ((key, key.removeprefix("move_")) for key in form)
            if key.startswith("move_") and form.get(key) == "yes"
        ]
        target_group_id = form.get("target_group_id", "")
        try:
            move_media_to_group(database, selected, target_group_id)
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.get("/groups/{group_id}/edit", response_class=HTMLResponse)
    def group_edit_page(request: Request, group_id: str):
        found = _find_group(read_library(database), group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        author, series, issue, group = found
        return templates.TemplateResponse(
            request=request, name="edit_group.html",
            context={
                "author": author, "series": series, "issue": issue, "group": group,
                "series_choices": _series_choices(database),
                "target_issues": _issues_for_series(database, series.id),
                "all_media": _all_media_for_group(database, group.id),
                "issue_groups": issue.groups if issue else [],
                "error": None,
            },
        )

    @app.post("/groups/{group_id}/edit", response_class=HTMLResponse)
    async def group_edit_save(request: Request, group_id: str):
        found = _find_group(read_library(database), group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Content group not found")
        author, series, issue, group = found
        form = await _form_data(request)
        try:
            if group.role != "primary":
                rename_group(database, group_id, form.get("name", group.name))
                target = form.get("owner", "series")
                if target == "series":
                    move_extra_group(database, group_id, series_id=series.id, issue_id=None)
                elif target.startswith("issue:"):
                    target_issue = target.split(":", 1)[1]
                    move_extra_group(database, group_id, series_id=series.id, issue_id=target_issue)
            active_media = [m for m in _all_media_for_group(database, group_id) if m["active"]]
            if active_media:
                ordered = sorted(
                    active_media,
                    key=lambda m: (
                        int(form.get(f"position_{m['id']}", m["position"])) if str(form.get(f"position_{m['id']}", m["position"])).isdigit() else int(m["position"]),
                        int(m["position"]),
                    ),
                )
                reorder_media(database, group_id, [str(m["id"]) for m in ordered])
        except (EditError, ValueError) as exc:
            return templates.TemplateResponse(
                request=request, name="edit_group.html",
                context={
                    "author": author, "series": series, "issue": issue, "group": group,
                    "series_choices": _series_choices(database),
                    "target_issues": _issues_for_series(database, series.id),
                    "all_media": _all_media_for_group(database, group.id),
                    "issue_groups": issue.groups if issue else [],
                    "error": str(exc),
                }, status_code=400,
            )
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.post("/media/{media_id}/remove")
    async def media_remove(request: Request, media_id: str, group_id: str):
        await _form_data(request)
        try:
            set_media_active(database, media_id, False)
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.post("/media/{media_id}/restore")
    async def media_restore(request: Request, media_id: str, group_id: str):
        await _form_data(request)
        try:
            set_media_active(database, media_id, True)
        except EditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="history.html",
            context={"history": get_history(database)},
        )

    @app.get("/thumbnail/{media_id}")
    def thumbnail(media_id: str):
        record = _media_record(database, media_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Media not found")
        stored_path, mime_type = record
        path = ensure_thumbnail(
            library,
            media_id=media_id,
            stored_path=stored_path,
            mime_type=mime_type,
        )
        if path is None:
            raise HTTPException(status_code=404, detail="Thumbnail unavailable")
        return FileResponse(
            path,
            media_type=THUMBNAIL_MIME,
            headers={"Cache-Control": "private, max-age=604800"},
        )

    @app.get("/media/{media_id}")
    def media(media_id: str):
        record = _media_record(database, media_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Media not found")
        stored_path, mime_type = record
        path = _safe_library_file(library, stored_path)
        return FileResponse(path, media_type=mime_type)

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
