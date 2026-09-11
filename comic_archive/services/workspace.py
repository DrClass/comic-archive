from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..importer.review import ReviewPlan, ReviewRole, flattened_folder_candidates
from ..importer.scanner import SUPPORTED_MEDIA
from ..importer.sorting import natural_text_key
from ..importer.staging import StagedImport, StagedIssue, StagedSeries, StagedGroup, StagedMedia, StagingError
from ..web_forms import bool_form as _bool_form
from .diagnostics import log_memory_checkpoint as _log_memory_checkpoint, logger
from .import_sessions import ImportSession

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


