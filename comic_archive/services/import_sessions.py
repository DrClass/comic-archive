from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException

from ..importer.bulk import BulkComicCandidate
from ..importer.review import ReviewPlan, build_review_plan
from ..importer.scanner import FolderScanError, scan_folder
from ..importer.staging import StagedGroup, StagedImport, StagedIssue, StagedMedia, StagedSeries


SESSION_INACTIVITY_SECONDS = 12 * 60 * 60


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


def session_state_dir(app: FastAPI) -> Path:
    root = app.state.staging_root / "session_state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_state_path(app: FastAPI, kind: str, session_id: str) -> Path:
    return session_state_dir(app) / f"{kind}_{session_id}.json"


def atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def staged_from_dict(data: dict) -> StagedImport:
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


def persist_import_session(app: FastAPI, session_id: str, session: ImportSession) -> None:
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
    atomic_json_write(session_state_path(app, "import", session_id), payload)


def restore_import_session(app: FastAPI, session_id: str) -> ImportSession | None:
    path = session_state_path(app, "import", session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_activity = float(data.get("last_activity", 0))
        if time.time() - last_activity >= SESSION_INACTIVITY_SECONDS:
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
            staged=staged_from_dict(data["staged"]) if data.get("staged") else None,
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


def delete_import_session_state(app: FastAPI, session_id: str) -> None:
    try:
        session_state_path(app, "import", session_id).unlink(missing_ok=True)
    except OSError:
        pass


def save_completed_import(app: FastAPI, session_id: str, payload: dict) -> None:
    data = dict(payload)
    data["last_activity"] = time.time()
    atomic_json_write(session_state_path(app, "completed", session_id), data)


def load_completed_import(app: FastAPI, session_id: str) -> dict | None:
    path = session_state_path(app, "completed", session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("last_activity", 0)) >= SESSION_INACTIVITY_SECONDS:
            path.unlink(missing_ok=True)
            return None
        return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def persist_bulk_session(app: FastAPI, bulk_id: str, bulk: BulkArtistSession) -> None:
    atomic_json_write(session_state_path(app, "bulk", bulk_id), {
        "version": 1, "source": str(bulk.source), "author": bulk.author,
        "candidates": [{"path": str(c.path), "name": c.name, "media_count": c.media_count} for c in bulk.candidates],
        "selected": bulk.selected, "current_position": bulk.current_position,
        "imported_series": bulk.imported_series, "skipped_series": bulk.skipped_series,
        "upload_root": str(bulk.upload_root) if bulk.upload_root else None,
        "last_activity": bulk.last_activity,
    })


def restore_bulk_session(app: FastAPI, bulk_id: str) -> BulkArtistSession | None:
    path = session_state_path(app, "bulk", bulk_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_activity = float(data.get("last_activity", 0))
        if time.time() - last_activity >= SESSION_INACTIVITY_SECONDS:
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


def delete_bulk_session_state(app: FastAPI, bulk_id: str) -> None:
    try:
        session_state_path(app, "bulk", bulk_id).unlink(missing_ok=True)
    except OSError:
        pass


def touch_upload_root(path: Path | None, now: float | None = None) -> None:
    if path is None or not path.exists():
        return
    stamp = time.time() if now is None else now
    try:
        import os
        os.utime(path, (stamp, stamp))
    except OSError:
        pass


def rescan_import_session(session: ImportSession) -> None:
    scan = scan_folder(
        session.plan.scan.source,
        extra_folders=sorted(session.extra_folder_overrides),
        primary_folders=sorted(session.primary_folder_overrides),
        subseries_folders=sorted(session.subseries_folder_overrides),
        pdf_cache_root=session.pdf_cache_root,
    )
    session.plan = build_review_plan(scan)


def touch_import_activity(app: FastAPI, session: ImportSession) -> None:
    now = time.time()
    session.last_activity = now
    touch_upload_root(session.upload_root, now)
    session_id = next((key for key, value in app.state.import_sessions.items() if value is session), None)
    if session_id:
        persist_import_session(app, session_id, session)
    if session.bulk_id:
        bulk = app.state.bulk_import_sessions.get(session.bulk_id)
        if bulk is not None:
            bulk.last_activity = now
            touch_upload_root(bulk.upload_root, now)
            persist_bulk_session(app, session.bulk_id, bulk)


def touch_bulk_activity(app: FastAPI, bulk: BulkArtistSession) -> None:
    now = time.time()
    bulk.last_activity = now
    touch_upload_root(bulk.upload_root, now)
    bulk_id = next((key for key, value in app.state.bulk_import_sessions.items() if value is bulk), None)
    if bulk_id:
        persist_bulk_session(app, bulk_id, bulk)


def remove_staging_record(session: ImportSession, cleanup_upload: Callable[[Path | None], None]) -> None:
    cleanup_upload(session.pdf_cache_root)
    session.pdf_cache_root = None
    if session.staging_path is not None:
        try:
            session.staging_path.unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_stale_imports(
    app: FastAPI,
    staging_root: Path,
    *,
    now: float | None = None,
    delete_browser_upload_state: Callable[[FastAPI, str], None],
    cleanup_upload: Callable[[Path | None], None],
) -> None:
    current = time.time() if now is None else now
    cutoff = current - SESSION_INACTIVITY_SECONDS

    stale_upload_ids = [
        upload_id for upload_id, upload in app.state.upload_sessions.items()
        if upload.last_activity < cutoff
    ]
    upload_roots_to_delete: set[Path] = set()
    for upload_id in stale_upload_ids:
        upload = app.state.upload_sessions.pop(upload_id, None)
        delete_browser_upload_state(app, upload_id)
        if upload is not None:
            upload_roots_to_delete.add(upload.upload_root)

    stale_bulk_ids = [
        bulk_id for bulk_id, bulk in app.state.bulk_import_sessions.items()
        if bulk.last_activity < cutoff
    ]
    for bulk_id in stale_bulk_ids:
        bulk = app.state.bulk_import_sessions.pop(bulk_id, None)
        delete_bulk_session_state(app, bulk_id)
        if bulk and bulk.upload_root:
            upload_roots_to_delete.add(bulk.upload_root)

    for session_id, session in list(app.state.import_sessions.items()):
        bulk_is_stale = session.bulk_id in stale_bulk_ids
        if session.last_activity < cutoff or bulk_is_stale:
            app.state.import_sessions.pop(session_id, None)
            delete_import_session_state(app, session_id)
            remove_staging_record(session, cleanup_upload)
            if session.upload_root and (session.bulk_id is None or bulk_is_stale):
                upload_roots_to_delete.add(session.upload_root)

    for root in upload_roots_to_delete:
        cleanup_upload(root)

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
                    cleanup_upload(child)
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


def session_or_404(
    app: FastAPI,
    session_id: str,
    *,
    cleanup_stale: Callable[[FastAPI, Path], None],
) -> ImportSession:
    session = app.state.import_sessions.get(session_id)
    if session is None:
        session = restore_import_session(app, session_id)
    if session is not None and time.time() - session.last_activity >= SESSION_INACTIVITY_SECONDS:
        cleanup_stale(app, app.state.staging_root)
        session = app.state.import_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Import session expired or was cleaned up after 12 hours of inactivity")
    return session


def bulk_session_or_404(
    app: FastAPI,
    bulk_id: str,
    *,
    cleanup_stale: Callable[[FastAPI, Path], None],
) -> BulkArtistSession:
    session = app.state.bulk_import_sessions.get(bulk_id)
    if session is None:
        session = restore_bulk_session(app, bulk_id)
    if session is not None and time.time() - session.last_activity >= SESSION_INACTIVITY_SECONDS:
        cleanup_stale(app, app.state.staging_root)
        session = app.state.bulk_import_sessions.get(bulk_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Bulk import session expired or was cleaned up after 12 hours of inactivity")
    return session
