from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from .database import connect_database


class EditError(RuntimeError):
    pass


_UNSET = object()


def _connect(database_path: str | Path) -> sqlite3.Connection:
    db = connect_database(database_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _audit(
    db: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    before: Any,
    after: Any,
) -> None:
    if before == after:
        return
    db.execute(
        """INSERT INTO audit_log(entity_type, entity_id, action, before_json, after_json)
           VALUES (?, ?, ?, ?, ?)""",
        (
            entity_type,
            entity_id,
            action,
            json.dumps(before, sort_keys=True, ensure_ascii=False),
            json.dumps(after, sort_keys=True, ensure_ascii=False),
        ),
    )


def _row_dict(row: sqlite3.Row, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: row[key] for key in keys}


def _recompute_issue_fingerprint(db: sqlite3.Connection, issue_id: str) -> None:
    hashes = db.execute(
        """SELECT m.sha256
           FROM content_groups g
           JOIN media m ON m.group_id = g.id
           WHERE g.issue_id = ? AND m.active = 1 AND m.sha256 IS NOT NULL
           ORDER BY g.sort_order, m.position""",
        (issue_id,),
    ).fetchall()
    if not hashes:
        db.execute("UPDATE issues SET content_fingerprint = NULL WHERE id = ?", (issue_id,))
        return
    digest = hashlib.sha256()
    for (file_hash,) in hashes:
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\0")
    db.execute(
        "UPDATE issues SET content_fingerprint = ? WHERE id = ?",
        (digest.hexdigest(), issue_id),
    )


def rename_author(database_path: str | Path, author_id: str, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise EditError("Author name cannot be empty")
    with _connect(database_path) as db:
        row = db.execute("SELECT id, name FROM authors WHERE id = ?", (author_id,)).fetchone()
        if not row:
            raise EditError(f"Author not found: {author_id}")
        before = _row_dict(row, ("name",))
        if row["name"] == new_name:
            return
        try:
            db.execute("UPDATE authors SET name = ? WHERE id = ?", (new_name, author_id))
        except sqlite3.IntegrityError as exc:
            raise EditError(f"An author named {new_name!r} already exists") from exc
        _audit(db, entity_type="author", entity_id=author_id, action="rename", before=before, after={"name": new_name})


def edit_series(
    database_path: str | Path,
    series_id: str,
    *,
    title: str | object = _UNSET,
    author_id: str | object = _UNSET,
    complete: bool | None | object = _UNSET,
    parent_series_id: str | None | object = _UNSET,
) -> None:
    with _connect(database_path) as db:
        row = db.execute("SELECT id, author_id, title, complete, parent_series_id FROM series WHERE id = ?", (series_id,)).fetchone()
        if not row:
            raise EditError(f"Series not found: {series_id}")
        before = _row_dict(row, ("author_id", "title", "complete", "parent_series_id"))
        new_title = row["title"] if title is _UNSET else str(title).strip()
        new_author = row["author_id"] if author_id is _UNSET else str(author_id)
        if not new_title:
            raise EditError("Series title cannot be empty")
        if not db.execute("SELECT 1 FROM authors WHERE id = ?", (new_author,)).fetchone():
            raise EditError(f"Author not found: {new_author}")
        new_complete = row["complete"] if complete is _UNSET else (None if complete is None else int(complete))
        new_parent = row["parent_series_id"] if parent_series_id is _UNSET else parent_series_id
        if new_parent == "":
            new_parent = None
        if new_parent is not None:
            parent = db.execute("SELECT id, author_id, parent_series_id FROM series WHERE id = ?", (new_parent,)).fetchone()
            if not parent:
                raise EditError(f"Parent series not found: {new_parent}")
            if parent["author_id"] != new_author:
                raise EditError("Parent series must belong to the same author")
            if new_parent == series_id:
                raise EditError("A series cannot be its own parent")
            cursor = parent
            seen = {series_id}
            while cursor is not None:
                if cursor["id"] in seen:
                    raise EditError("Series nesting cannot contain a cycle")
                seen.add(cursor["id"])
                pid = cursor["parent_series_id"]
                cursor = db.execute("SELECT id, author_id, parent_series_id FROM series WHERE id = ?", (pid,)).fetchone() if pid else None
        after = {"author_id": new_author, "title": new_title, "complete": new_complete, "parent_series_id": new_parent}
        if before == after:
            return
        try:
            if new_author != row["author_id"]:
                descendants = db.execute(
                    """WITH RECURSIVE tree(id) AS (
                           SELECT id FROM series WHERE parent_series_id = ?
                           UNION ALL
                           SELECT s.id FROM series s JOIN tree t ON s.parent_series_id = t.id
                       ) SELECT id FROM tree""",
                    (series_id,),
                ).fetchall()
                for descendant in descendants:
                    db.execute("UPDATE series SET author_id = ? WHERE id = ?", (new_author, descendant["id"]))
            db.execute("UPDATE series SET author_id = ?, title = ?, complete = ?, parent_series_id = ? WHERE id = ?", (new_author, new_title, new_complete, new_parent, series_id))
        except sqlite3.IntegrityError as exc:
            raise EditError("That author already has a series with this title") from exc
        _audit(db, entity_type="series", entity_id=series_id, action="edit", before=before, after=after)



def reorder_issues(database_path: str | Path, series_id: str, issue_ids: list[str]) -> None:
    with _connect(database_path) as db:
        rows = db.execute(
            "SELECT id, sort_order FROM issues WHERE series_id = ? ORDER BY COALESCE(sort_order, 2147483647), id",
            (series_id,),
        ).fetchall()
        existing_ids = [row["id"] for row in rows]
        if set(existing_ids) != set(issue_ids) or len(existing_ids) != len(issue_ids):
            raise EditError("Issue order must include every issue in the series exactly once")
        before = {row["id"]: row["sort_order"] for row in rows}
        after = {issue_id: index for index, issue_id in enumerate(issue_ids, start=1)}
        if before == after:
            return
        for issue_id, sort_order in after.items():
            db.execute("UPDATE issues SET sort_order = ? WHERE id = ? AND series_id = ?", (sort_order, issue_id, series_id))
        _audit(db, entity_type="series", entity_id=series_id, action="reorder_issues", before=before, after=after)

def edit_issue(
    database_path: str | Path,
    issue_id: str,
    *,
    issue_number: str | None | object = _UNSET,
    title: str | None | object = _UNSET,
    complete: bool | None | object = _UNSET,
    series_id: str | object = _UNSET,
) -> None:
    with _connect(database_path) as db:
        row = db.execute(
            "SELECT id, series_id, issue_number, title, complete FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        if not row:
            raise EditError(f"Issue not found: {issue_id}")
        before = _row_dict(row, ("series_id", "issue_number", "title", "complete"))
        new_series = row["series_id"] if series_id is _UNSET else str(series_id)
        if not db.execute("SELECT 1 FROM series WHERE id = ?", (new_series,)).fetchone():
            raise EditError(f"Series not found: {new_series}")
        new_number = row["issue_number"] if issue_number is _UNSET else issue_number
        new_title = row["title"] if title is _UNSET else title
        new_complete = row["complete"] if complete is _UNSET else (None if complete is None else int(complete))
        after = {"series_id": new_series, "issue_number": new_number, "title": new_title, "complete": new_complete}
        if before == after:
            return
        db.execute(
            "UPDATE issues SET series_id = ?, issue_number = ?, title = ?, complete = ? WHERE id = ?",
            (new_series, new_number, new_title, new_complete, issue_id),
        )
        # Issue-owned groups must follow the issue to its new series.
        if new_series != row["series_id"]:
            db.execute("UPDATE content_groups SET series_id = ? WHERE issue_id = ?", (new_series, issue_id))
        _audit(db, entity_type="issue", entity_id=issue_id, action="edit", before=before, after=after)


def rename_group(database_path: str | Path, group_id: str, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise EditError("Group name cannot be empty")
    with _connect(database_path) as db:
        row = db.execute("SELECT id, name FROM content_groups WHERE id = ?", (group_id,)).fetchone()
        if not row:
            raise EditError(f"Content group not found: {group_id}")
        if row["name"] == new_name:
            return
        db.execute("UPDATE content_groups SET name = ? WHERE id = ?", (new_name, group_id))
        _audit(db, entity_type="group", entity_id=group_id, action="rename", before={"name": row["name"]}, after={"name": new_name})


def move_extra_group(
    database_path: str | Path,
    group_id: str,
    *,
    series_id: str,
    issue_id: str | None = None,
) -> None:
    with _connect(database_path) as db:
        group = db.execute(
            "SELECT id, series_id, issue_id, role FROM content_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if not group:
            raise EditError(f"Content group not found: {group_id}")
        if group["role"] == "primary":
            raise EditError("Primary comic content cannot be moved with move-extra")
        if not db.execute("SELECT 1 FROM series WHERE id = ?", (series_id,)).fetchone():
            raise EditError(f"Series not found: {series_id}")
        if issue_id is not None:
            issue = db.execute("SELECT series_id FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if not issue:
                raise EditError(f"Issue not found: {issue_id}")
            if issue["series_id"] != series_id:
                raise EditError("Target issue does not belong to target series")
        before = {"series_id": group["series_id"], "issue_id": group["issue_id"], "role": group["role"]}
        role = "issue-extra" if issue_id else "series-extra"
        after = {"series_id": series_id, "issue_id": issue_id, "role": role}
        if before == after:
            return
        db.execute(
            "UPDATE content_groups SET series_id = ?, issue_id = ?, role = ? WHERE id = ?",
            (series_id, issue_id, role, group_id),
        )
        _audit(db, entity_type="group", entity_id=group_id, action="move-extra", before=before, after=after)



def create_issue_extra_group(database_path: str | Path, issue_id: str, name: str) -> str:
    name = name.strip()
    if not name:
        raise EditError("Extra group name cannot be empty")
    with _connect(database_path) as db:
        issue = db.execute("SELECT id, series_id FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if not issue:
            raise EditError(f"Issue not found: {issue_id}")
        duplicate = db.execute(
            "SELECT 1 FROM content_groups WHERE issue_id = ? AND name = ? COLLATE NOCASE",
            (issue_id, name),
        ).fetchone()
        if duplicate:
            raise EditError(f"This issue already has a group named {name!r}")
        sort_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM content_groups WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()[0]
        group_id = str(uuid4())
        db.execute(
            """INSERT INTO content_groups
               (id, series_id, issue_id, name, role, relative_path, sort_order)
               VALUES (?, ?, ?, ?, 'issue-extra', ?, ?)""",
            (group_id, issue["series_id"], issue_id, name, f"virtual/{group_id}", sort_order),
        )
        _audit(
            db,
            entity_type="group",
            entity_id=group_id,
            action="create-extra",
            before=None,
            after={"series_id": issue["series_id"], "issue_id": issue_id, "name": name, "role": "issue-extra"},
        )
        return group_id


def move_media_to_group(
    database_path: str | Path,
    media_ids: list[str],
    target_group_id: str,
) -> None:
    if not media_ids or len(set(media_ids)) != len(media_ids):
        raise EditError("Choose at least one unique media item to move")
    with _connect(database_path) as db:
        target = db.execute(
            "SELECT id, issue_id, series_id, role FROM content_groups WHERE id = ?",
            (target_group_id,),
        ).fetchone()
        if not target:
            raise EditError(f"Target content group not found: {target_group_id}")
        if target["issue_id"] is None:
            raise EditError("Media can currently be moved only between groups belonging to the same issue")

        placeholders = ",".join("?" for _ in media_ids)
        rows = db.execute(
            f"""SELECT m.id, m.group_id, m.position, m.active, g.issue_id
                FROM media m
                JOIN content_groups g ON g.id = m.group_id
                WHERE m.id IN ({placeholders})""",
            media_ids,
        ).fetchall()
        if len(rows) != len(media_ids):
            found = {row["id"] for row in rows}
            missing = [media_id for media_id in media_ids if media_id not in found]
            raise EditError("Media not found: " + ", ".join(missing))
        if any(row["issue_id"] != target["issue_id"] for row in rows):
            raise EditError("Media can only be moved between groups in the same issue")

        moving = [row for row in rows if row["group_id"] != target_group_id]
        if not moving:
            return

        source_group_ids = {row["group_id"] for row in moving}
        before = [
            {"media_id": row["id"], "group_id": row["group_id"], "position": row["position"]}
            for row in moving
        ]

        # Temporarily move selected rows out of the positive position range so
        # UNIQUE(group_id, position) cannot collide while groups are compacted.
        for index, row in enumerate(moving, start=1):
            db.execute("UPDATE media SET position = ? WHERE id = ?", (-1000000 - index, row["id"]))

        # Compact every source group after removing the selected media.
        for group_id in source_group_ids:
            remaining = db.execute(
                "SELECT id FROM media WHERE group_id = ? AND id NOT IN ({}) ORDER BY position".format(placeholders),
                [group_id, *media_ids],
            ).fetchall()
            for index, row in enumerate(remaining, start=1):
                db.execute("UPDATE media SET position = ? WHERE id = ?", (-2000000 - index, row["id"]))
            for index, row in enumerate(remaining, start=1):
                db.execute("UPDATE media SET position = ? WHERE id = ?", (index, row["id"]))

        target_max = db.execute(
            "SELECT COALESCE(MAX(position), 0) FROM media WHERE group_id = ?",
            (target_group_id,),
        ).fetchone()[0]
        ordered_moving = sorted(moving, key=lambda row: media_ids.index(row["id"]))
        for offset, row in enumerate(ordered_moving, start=1):
            db.execute(
                "UPDATE media SET group_id = ?, position = ? WHERE id = ?",
                (target_group_id, target_max + offset, row["id"]),
            )

        after = [
            {"media_id": row["id"], "group_id": target_group_id, "position": target_max + index}
            for index, row in enumerate(ordered_moving, start=1)
        ]
        _audit(
            db,
            entity_type="group",
            entity_id=target_group_id,
            action="move-media",
            before=before,
            after=after,
        )
        _recompute_issue_fingerprint(db, target["issue_id"])

def reorder_media(database_path: str | Path, group_id: str, ordered_media_ids: list[str]) -> None:
    if not ordered_media_ids or len(set(ordered_media_ids)) != len(ordered_media_ids):
        raise EditError("Media IDs must be a non-empty unique list")
    with _connect(database_path) as db:
        group = db.execute("SELECT issue_id FROM content_groups WHERE id = ?", (group_id,)).fetchone()
        if not group:
            raise EditError(f"Content group not found: {group_id}")
        rows = db.execute(
            "SELECT id, position FROM media WHERE group_id = ? AND active = 1 ORDER BY position", (group_id,)
        ).fetchall()
        existing = [row["id"] for row in rows]
        if set(existing) != set(ordered_media_ids) or len(existing) != len(ordered_media_ids):
            raise EditError("Reorder must include every active media item in this group exactly once")
        before = existing
        if before == ordered_media_ids:
            return
        # Avoid the UNIQUE(group_id, position) constraint while positions are changing.
        for index, media_id in enumerate(ordered_media_ids, start=1):
            db.execute("UPDATE media SET position = ? WHERE id = ?", (-index, media_id))
        for index, media_id in enumerate(ordered_media_ids, start=1):
            db.execute("UPDATE media SET position = ? WHERE id = ?", (index, media_id))
        _audit(db, entity_type="group", entity_id=group_id, action="reorder-media", before=before, after=ordered_media_ids)
        if group["issue_id"]:
            _recompute_issue_fingerprint(db, group["issue_id"])


def set_media_active(database_path: str | Path, media_id: str, active: bool) -> None:
    with _connect(database_path) as db:
        row = db.execute(
            """SELECT m.id, m.active, m.group_id, g.issue_id
               FROM media m JOIN content_groups g ON g.id = m.group_id WHERE m.id = ?""",
            (media_id,),
        ).fetchone()
        if not row:
            raise EditError(f"Media not found: {media_id}")
        old = bool(row["active"])
        if old == active:
            return
        db.execute("UPDATE media SET active = ? WHERE id = ?", (int(active), media_id))
        _audit(
            db,
            entity_type="media",
            entity_id=media_id,
            action="restore" if active else "remove",
            before={"active": old},
            after={"active": active},
        )
        if row["issue_id"]:
            _recompute_issue_fingerprint(db, row["issue_id"])


def _entity_label(db: sqlite3.Connection, entity_type: str, entity_id: str) -> str:
    if entity_type == "author":
        row = db.execute("SELECT name FROM authors WHERE id = ?", (entity_id,)).fetchone()
        return row["name"] if row else f"Deleted author ({entity_id})"
    if entity_type == "series":
        row = db.execute(
            """SELECT a.name AS author_name, s.title
               FROM series s JOIN authors a ON a.id = s.author_id
               WHERE s.id = ?""",
            (entity_id,),
        ).fetchone()
        return f"{row['author_name']} / {row['title']}" if row else f"Deleted series ({entity_id})"
    if entity_type == "issue":
        row = db.execute(
            """SELECT a.name AS author_name, s.title AS series_title,
                      i.issue_number, i.title AS issue_title
               FROM issues i
               JOIN series s ON s.id = i.series_id
               JOIN authors a ON a.id = s.author_id
               WHERE i.id = ?""",
            (entity_id,),
        ).fetchone()
        if not row:
            return f"Deleted issue ({entity_id})"
        issue_label = row["issue_number"] or row["issue_title"] or "One-shot"
        return f"{row['author_name']} / {row['series_title']} / {issue_label}"
    if entity_type == "group":
        row = db.execute(
            """SELECT g.name, a.name AS author_name, s.title AS series_title,
                      i.issue_number, i.title AS issue_title
               FROM content_groups g
               JOIN series s ON s.id = g.series_id
               JOIN authors a ON a.id = s.author_id
               LEFT JOIN issues i ON i.id = g.issue_id
               WHERE g.id = ?""",
            (entity_id,),
        ).fetchone()
        if not row:
            return f"Deleted content group ({entity_id})"
        owner = f"{row['author_name']} / {row['series_title']}"
        if row["issue_number"] or row["issue_title"]:
            owner += f" / {row['issue_number'] or row['issue_title']}"
        return f"{owner} / {row['name']}"
    if entity_type == "media":
        row = db.execute(
            """SELECT m.original_relative_path, g.name AS group_name,
                      a.name AS author_name, s.title AS series_title,
                      i.issue_number, i.title AS issue_title
               FROM media m
               JOIN content_groups g ON g.id = m.group_id
               JOIN series s ON s.id = g.series_id
               JOIN authors a ON a.id = s.author_id
               LEFT JOIN issues i ON i.id = g.issue_id
               WHERE m.id = ?""",
            (entity_id,),
        ).fetchone()
        if not row:
            return f"Deleted media ({entity_id})"
        owner = f"{row['author_name']} / {row['series_title']}"
        if row["issue_number"] or row["issue_title"]:
            owner += f" / {row['issue_number'] or row['issue_title']}"
        return f"{owner} / {row['group_name']} / {row['original_relative_path']}"
    return f"{entity_type} {entity_id}"


def _resolve_value_labels(db: sqlite3.Connection, value: Any) -> Any:
    if isinstance(value, list):
        # Reorder history stores media IDs as a list.
        resolved = []
        for item in value:
            if isinstance(item, str):
                row = db.execute(
                    "SELECT original_relative_path FROM media WHERE id = ?", (item,)
                ).fetchone()
                resolved.append(row["original_relative_path"] if row else item)
            else:
                resolved.append(item)
        return resolved
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    for key, item in value.items():
        result[key] = item
        if key == "author_id" and isinstance(item, str):
            row = db.execute("SELECT name FROM authors WHERE id = ?", (item,)).fetchone()
            result["author"] = row["name"] if row else item
        elif key == "series_id" and isinstance(item, str):
            row = db.execute("SELECT title FROM series WHERE id = ?", (item,)).fetchone()
            result["series"] = row["title"] if row else item
        elif key == "issue_id" and isinstance(item, str):
            row = db.execute(
                "SELECT issue_number, title FROM issues WHERE id = ?", (item,)
            ).fetchone()
            result["issue"] = (row["issue_number"] or row["title"] or "One-shot") if row else item
    return result


def get_history(
    database_path: str | Path,
    *,
    entity_id: str | None = None,
    include_noops: bool = False,
) -> list[dict[str, Any]]:
    with _connect(database_path) as db:
        if entity_id:
            rows = db.execute(
                """SELECT id, entity_type, entity_id, action, before_json, after_json, created_at
                   FROM audit_log WHERE entity_id = ? ORDER BY id DESC""",
                (entity_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT id, entity_type, entity_id, action, before_json, after_json, created_at
                   FROM audit_log ORDER BY id DESC"""
            ).fetchall()

        history: list[dict[str, Any]] = []
        for row in rows:
            before = json.loads(row["before_json"])
            after = json.loads(row["after_json"])
            if not include_noops and before == after:
                continue
            history.append(
                {
                    "id": row["id"],
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "entity_label": _entity_label(db, row["entity_type"], row["entity_id"]),
                    "action": row["action"],
                    "before": before,
                    "after": after,
                    "before_display": _resolve_value_labels(db, before),
                    "after_display": _resolve_value_labels(db, after),
                    "created_at": row["created_at"],
                }
            )
        return history


def delete_series(database_path: str | Path, library_root: str | Path, series_id: str, *, confirmation: str) -> tuple[str, str | None]:
    """Delete a series tree and its managed files.

    Returns ``(author_id, parent_series_id)`` so the web UI can redirect to a
    useful surviving page. The confirmation must exactly match the series
    title. Database deletion happens transactionally; managed files are then
    removed from their ID-based series directories.
    """
    library = Path(library_root).expanduser().resolve()
    with _connect(database_path) as db:
        row = db.execute(
            "SELECT id, author_id, title, parent_series_id FROM series WHERE id = ?",
            (series_id,),
        ).fetchone()
        if not row:
            raise EditError(f"Series not found: {series_id}")
        if confirmation != row["title"]:
            raise EditError("Confirmation title does not match the series title")
        descendants = db.execute(
            """WITH RECURSIVE tree(id) AS (
                   SELECT id FROM series WHERE id = ?
                   UNION ALL
                   SELECT s.id FROM series s JOIN tree t ON s.parent_series_id = t.id
               ) SELECT id FROM tree""",
            (series_id,),
        ).fetchall()
        series_ids = [item["id"] for item in descendants]
        placeholders = ",".join("?" for _ in series_ids)
        issue_count = db.execute(
            f"SELECT COUNT(*) FROM issues WHERE series_id IN ({placeholders})", series_ids
        ).fetchone()[0]
        media_count = db.execute(
            f"""SELECT COUNT(*) FROM media m
                JOIN content_groups g ON g.id = m.group_id
                WHERE g.series_id IN ({placeholders})""", series_ids
        ).fetchone()[0]
        before = {
            "title": row["title"], "author_id": row["author_id"],
            "parent_series_id": row["parent_series_id"],
            "series_count": len(series_ids), "issue_count": issue_count, "media_count": media_count,
        }
        _audit(db, entity_type="series", entity_id=series_id, action="delete", before=before, after=None)
        # imports intentionally retain no orphan references after destructive
        # deletion; their historical summary is represented by the audit row.
        db.execute(f"DELETE FROM imports WHERE series_id IN ({placeholders})", series_ids)
        db.execute("DELETE FROM series WHERE id = ?", (series_id,))
        author_id, parent_id = row["author_id"], row["parent_series_id"]

    series_root = (library / "series").resolve()
    for item_id in series_ids:
        target = (series_root / item_id).resolve()
        try:
            target.relative_to(series_root)
        except ValueError:
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    return author_id, parent_id
