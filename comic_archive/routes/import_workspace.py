from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..importer.review import ReviewError, ReviewRole
from ..importer.staging import StagedImport, StagingError


@dataclass(frozen=True)
class ImportWorkspaceRouteDeps:
    staging_root: Path
    templates: Any
    form_data: Callable[[Request], Awaitable[dict[str, str]]]
    session_or_404: Callable[[FastAPI, str], Any]
    touch_import_activity: Callable[[FastAPI, Any], None]
    workspace_tree: Callable[[Any], list[dict[str, Any]]]
    workspace_all_media: Callable[[Any], list[Any]]
    workspace_media_targets_for_folder: Callable[[Any, str], list[Any]]
    workspace_origin_for_media: Callable[[Any, str], str]
    workspace_sorted_review_items: Callable[..., list[Any]]
    workspace_item_key: Callable[[Any], str]
    workspace_source_relative: Callable[[Any, Any], str]
    workspace_build_staged_import: Callable[..., Any]
    run_background_io: Callable[..., Awaitable[Any]]
    log_memory_checkpoint: Callable[..., None]
    workspace_node_metadata: Callable[[Any, dict[str, Any]], dict[str, Any]]
    workspace_media_for_folder: Callable[[Any, str], list[Any]]
    workspace_cache_state_signature: Callable[[Any], str]
    workspace_reorder_media: Callable[[Any, list[str]], None]
    workspace_invalidate_cache: Callable[[Any], None]
    logger: logging.Logger


def register_import_workspace_routes(app: FastAPI, deps: ImportWorkspaceRouteDeps) -> None:
    staging = deps.staging_root
    templates = deps.templates
    logger = deps.logger

    _form_data = deps.form_data
    _session_or_404 = deps.session_or_404
    _touch_import_activity = deps.touch_import_activity
    _workspace_tree = deps.workspace_tree
    _workspace_all_media = deps.workspace_all_media
    _workspace_media_targets_for_folder = deps.workspace_media_targets_for_folder
    _workspace_origin_for_media = deps.workspace_origin_for_media
    _workspace_sorted_review_items = deps.workspace_sorted_review_items
    _workspace_item_key = deps.workspace_item_key
    _workspace_source_relative = deps.workspace_source_relative
    _workspace_build_staged_import = deps.workspace_build_staged_import
    _run_background_io = deps.run_background_io
    _log_memory_checkpoint = deps.log_memory_checkpoint
    _workspace_node_metadata = deps.workspace_node_metadata
    _workspace_media_for_folder = deps.workspace_media_for_folder
    _workspace_cache_state_signature = deps.workspace_cache_state_signature
    _workspace_reorder_media = deps.workspace_reorder_media
    _workspace_invalidate_cache = deps.workspace_invalidate_cache

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
        if "display_name" in form:
            display_name = form.get("display_name", "").strip()
            if not display_name:
                raise HTTPException(status_code=400, detail="Folder name cannot be empty")
            session.workspace_metadata.setdefault(folder, {})["display_name"] = display_name
            if folder.startswith("synthetic:"):
                data = session.workspace_virtual_nodes.get(folder.split(":", 1)[1])
                if data is not None: data["name"] = display_name
            elif folder.startswith("virtual:"):
                data = session.workspace_virtual_groups.get(folder.split(":", 1)[1])
                if data is not None: data["name"] = display_name
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
                if str(node["path"]).startswith("synthetic:"):
                    node_id = str(node["path"]).split(":", 1)[1]
                    if node_id in session.workspace_virtual_nodes:
                        session.workspace_virtual_nodes[node_id]["name"] = data.get("issue_number") or data.get("title") or session.workspace_virtual_nodes[node_id].get("name", "Issue")
            elif role in {"Series", "Sub-Series"}:
                title = form.get("title", "").strip()
                if not title:
                    raise HTTPException(status_code=400, detail="Series title cannot be empty")
                data["title"] = title
                value = form.get("complete", "").strip()
                if value not in {"", "yes", "no"}:
                    raise HTTPException(status_code=400, detail="Invalid completeness value")
                data["complete"] = value
                if str(node["path"]).startswith("synthetic:"):
                    node_id = str(node["path"]).split(":", 1)[1]
                    if node_id in session.workspace_virtual_nodes:
                        session.workspace_virtual_nodes[node_id]["name"] = title
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

    @app.post("/import/{session_id}/workspace/create-node")
    async def import_workspace_create_node(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        parent = form.get("parent", "").strip()
        kind = form.get("kind", "folder").strip()
        name = form.get("name", "").strip() or "New folder"
        if kind not in {"folder", "issue", "sub-series"}:
            raise HTTPException(status_code=400, detail="Invalid workspace folder type")
        tree = _workspace_tree(session)
        parent_node = next((node for node in tree if str(node["path"]) == parent), None)
        if parent_node is None:
            raise HTTPException(status_code=400, detail="Choose a valid parent folder")
        node_id = str(uuid4())
        sibling_count = sum(1 for data in session.workspace_virtual_nodes.values() if data.get("parent") == parent)
        default_role = "Unassigned"
        if kind == "issue": default_role = "Issue"
        elif kind == "sub-series": default_role = "Sub-Series"
        session.workspace_virtual_nodes[node_id] = {
            "parent": parent, "kind": "folder", "name": name,
            "role": default_role, "sort_order": str(100000 + sibling_count),
        }
        key = f"synthetic:{node_id}"
        session.workspace_role_overrides[key] = default_role
        if default_role == "Issue":
            session.workspace_metadata[key] = {"issue_number": name, "title": "", "complete": ""}
        elif default_role == "Sub-Series":
            session.workspace_metadata[key] = {"title": name, "complete": ""}
        return JSONResponse({"node_path": key})

    @app.post("/import/{session_id}/workspace/move-node")
    async def import_workspace_move_node(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        folder = form.get("folder_path", "").strip()
        new_parent = form.get("parent", "").strip()
        tree = _workspace_tree(session)
        nodes = {str(node["path"]): node for node in tree}
        if folder not in nodes or new_parent not in nodes:
            raise HTTPException(status_code=400, detail="Unknown workspace folder")
        if nodes[folder]["is_root"]:
            raise HTTPException(status_code=400, detail="The upload root cannot be moved")
        if folder == new_parent:
            raise HTTPException(status_code=400, detail="A folder cannot contain itself")
        cursor = new_parent
        while cursor:
            if cursor == folder:
                raise HTTPException(status_code=400, detail="A folder cannot be moved inside its own descendant")
            cursor = str(nodes.get(cursor, {}).get("parent") or "")
        old_parent = str(nodes[folder].get("parent") or ".")
        session.workspace_parent_overrides[folder] = new_parent
        # Remove stale sibling-order entries so the new parent's natural order
        # is used until the user explicitly drags it into position.
        for parent_key in {old_parent, new_parent}:
            current = session.folder_order_overrides.get(parent_key)
            if current:
                session.folder_order_overrides[parent_key] = [value for value in current if value != folder]
        if folder.startswith("synthetic:"):
            data = session.workspace_virtual_nodes.get(folder.split(":",1)[1])
            if data is not None: data["parent"] = new_parent
        elif folder.startswith("virtual:"):
            data = session.workspace_virtual_groups.get(folder.split(":",1)[1])
            if data is not None: data["owner"] = new_parent
        return Response(status_code=204)

    @app.post("/import/{session_id}/workspace/ignore-media")
    async def import_workspace_ignore_media(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        try:
            source_paths = json.loads(form.get("source_paths", "[]"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid page selection") from exc
        all_media = {str(media.path) for media in _workspace_all_media(session)}
        if not isinstance(source_paths, list) or not source_paths or any(path not in all_media for path in source_paths):
            raise HTTPException(status_code=400, detail="Select valid files to ignore")
        ignore = form.get("ignore", "yes") != "no"
        for path in source_paths:
            if ignore:
                session.workspace_ignored_media.add(path)
            else:
                session.workspace_ignored_media.discard(path)
        return Response(status_code=204)


    @app.post("/import/{session_id}/workspace/remove-node")
    async def import_workspace_remove_node(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        folder = form.get("folder_path", "").strip()
        if not folder.startswith("synthetic:"):
            raise HTTPException(status_code=400, detail="Only workspace-created issues/sub-series can be removed here")
        node_id = folder.split(":", 1)[1]
        if node_id not in session.workspace_virtual_nodes:
            raise HTTPException(status_code=404, detail="Workspace item not found")
        if any(str(node.get("parent") or "") == folder for node in _workspace_tree(session) if str(node["path"]) != folder):
            raise HTTPException(status_code=400, detail="Move child folders out first")
        if any(group.get("owner") == folder for group in session.workspace_virtual_groups.values()):
            raise HTTPException(status_code=400, detail="Remove child extra groups first")
        if any(target == folder for target in session.workspace_media_targets.values()):
            raise HTTPException(status_code=400, detail="Move pages out of this issue before removing it")
        session.workspace_virtual_nodes.pop(node_id, None)
        session.workspace_metadata.pop(folder, None)
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
        allowed = {
            str(node["path"]) for node in _workspace_tree(session)
            if str(node["role"]) in {"Issue", "Primary Pages", "Issue-Extras", "Series-Extras"}
        }
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
        valid_targets = {str(node["path"]) for node in _workspace_tree(session) if str(node["role"]) in {"Issue", "Primary Pages", "Issue-Extras", "Series-Extras"}}
        for source_path in source_paths:
            if source_path not in all_media:
                raise HTTPException(status_code=400, detail="Unknown page")
            if target not in valid_targets:
                raise HTTPException(status_code=400, detail="Invalid page destination")

        for source_path in source_paths:
            origin = _workspace_origin_for_media(session, source_path)
            if target == origin:
                session.workspace_media_targets.pop(source_path, None)
            else:
                session.workspace_media_targets[source_path] = target
        return Response(status_code=204)

    def move_staged_media_anywhere(staged: StagedImport, source_path: str, target_issue_key: str, target_group_path: str) -> None:
        target_issue = next((issue for issue in staged.issues if issue.source_key == target_issue_key), None)
        if target_issue is None:
            raise StagingError(f"Target staged issue not found: {target_issue_key}")
        target_group = next((group for group in target_issue.groups if group.relative_path == target_group_path), None)
        if target_group is None:
            raise StagingError(f"Target staged group not found: {target_group_path}")
        source_group = None
        media_item = None
        for issue in staged.issues:
            for group in issue.groups:
                for media in group.media:
                    if media.source_path == source_path:
                        source_group, media_item = group, media
                        break
                if media_item is not None:
                    break
            if media_item is not None:
                break
        if media_item is None or source_group is None:
            raise StagingError(f"Staged media not found: {source_path}")
        if source_group is target_group:
            return
        source_group.media.remove(media_item)
        target_group.media.append(media_item)
        for group in (source_group, target_group):
            group.media.sort(key=lambda item: item.order)
            for index, item in enumerate(group.media, start=1):
                item.order = index

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

        stage_started = time.monotonic()
        last_phase: list[str | None] = [None]

        def report_stage_progress(phase: str, current: int, total: int, detail: str) -> None:
            app.state.staging_progress[session_id] = {
                "phase": phase, "current": current, "total": total, "detail": detail,
                "updated_at": time.time(),
            }
            # Log phase transitions plus periodic large ownership steps without
            # flooding journald for every media item.
            if phase != last_phase[0] or (total > 0 and current > 0 and (current == total or current % 1000 == 0)):
                logger.info(
                    "stage progress session_id=%s phase=%s current=%d total=%d detail=%s elapsed=%.2fs",
                    session_id, phase, current, total, detail, time.monotonic() - stage_started,
                )
                _log_memory_checkpoint(
                    f"stage-{phase}", session_id=session_id, current=current, total=total,
                    tree_nodes=len(session.workspace_cached_tree or []),
                    cache_media=len(session.workspace_cached_media_index),
                )
                last_phase[0] = phase

        app.state.staging_progress[session_id] = {
            "phase": "starting", "current": 0, "total": 0,
            "detail": "Starting validation", "updated_at": time.time(),
        }
        _log_memory_checkpoint("stage-start", session_id=session_id)
        try:
            staged = await _run_background_io(
                _workspace_build_staged_import, session, report_stage_progress
            )
            report_stage_progress("saving", 0, 0, "Writing staging record")
            staging.mkdir(parents=True, exist_ok=True)
            staging_path = await _run_background_io(staged.save, staging / f"{staged.staging_id}.json")
            session.staged = staged
            session.staging_path = staging_path
            app.state.staging_progress[session_id] = {
                "phase": "complete", "current": 1, "total": 1,
                "detail": "Validation complete", "updated_at": time.time(),
            }
            logger.info(
                "stage complete session_id=%s issues=%d subseries=%d media=%d elapsed=%.2fs",
                session_id, len(staged.issues), len(staged.subseries),
                sum(len(group.media) for issue in staged.issues for group in issue.groups) + sum(len(group.media) for group in staged.series_extras),
                time.monotonic() - stage_started,
            )
            _log_memory_checkpoint("stage-complete", session_id=session_id)
        except (StagingError, ReviewError) as exc:
            app.state.staging_progress[session_id] = {
                "phase": "error", "current": 0, "total": 0,
                "detail": str(exc), "updated_at": time.time(),
            }
            logger.warning("stage failed session_id=%s elapsed=%.2fs error=%s", session_id, time.monotonic() - stage_started, exc)
            _log_memory_checkpoint("stage-error", session_id=session_id)
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
                    "selected_media_total": len(_workspace_media_for_folder(session, selected)),
                    "selected_media_offset": 0, "selected_media_page_size": 250,
                    "selected_metadata": _workspace_node_metadata(session, selected_node),
                    "media_targets": _workspace_media_targets_for_folder(session, selected),
                    "author_value": session.workspace_author or "", "series_value": session.workspace_series or "",
                    "series_complete_value": session.workspace_series_complete, "ignored_media": session.workspace_ignored_media, "errors": [str(exc)],
                }, status_code=400,
            )
        return RedirectResponse(f"/import/{session_id}/confirm", status_code=303)

    @app.post("/import/{session_id}/workspace/role")
    async def import_workspace_role(request: Request, session_id: str):
        session = _session_or_404(app, session_id)
        form = await _form_data(request)
        _touch_import_activity(app, session)
        folder_key = form.get("folder_path", "").strip()
        raw_role = form.get("role", "").strip()
        role_labels = {
            "series": "Series", "sub-series": "Sub-Series", "issue": "Issue",
            "issue-extras": "Issue-Extras", "series-extras": "Series-Extras",
            "primary-pages": "Primary Pages", "container": "Container",
            "ignore": "Ignore", "unassigned": "Unassigned",
        }
        if not folder_key or raw_role not in role_labels:
            raise HTTPException(status_code=400, detail="Invalid workspace folder role")
        tree = _workspace_tree(session)
        node = next((item for item in tree if str(item["path"]) == folder_key), None)
        if node is None:
            raise HTTPException(status_code=404, detail="Workspace folder not found")
        new_role = role_labels[raw_role]
        # The physical import root may be a Series or a neutral Container. A
        # child Series can then become the logical import root.
        if node["is_root"] and new_role not in {"Series", "Issue", "Container", "Ignore"}:
            raise HTTPException(status_code=400, detail="The upload root can be a Series, Issue, Container, or Ignore")
        session.workspace_role_overrides[folder_key] = new_role
        # Role changes alter semantic ownership, but rebuilding ownership for the
        # entire comic here makes a single dropdown change scale with every media
        # file. Patch the cached tree immediately and defer the authoritative
        # ownership rebuild until validation or another ownership-sensitive step.
        if session.workspace_cached_tree is not None:
            for cached_node in session.workspace_cached_tree:
                if str(cached_node["path"]) == folder_key:
                    cached_node["role"] = new_role
                    break
            session.workspace_cache_signature = _workspace_cache_state_signature(session)
            session.workspace_ownership_dirty = True
        # Preserve the old virtual-group owner field for recovered older sessions.
        if folder_key.startswith("virtual:") and new_role == "Issue-Extras":
            group = session.workspace_virtual_groups.get(folder_key.split(":",1)[1])
            if group is not None:
                group["owner"] = str(node.get("parent") or "")
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
        _workspace_invalidate_cache(session)
        return Response(status_code=204)
