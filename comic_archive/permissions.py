"""Default-open comic permissions. Schema work runs only at app startup."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from .database import connect_database

if TYPE_CHECKING:
    from .auth import User


def initialize_permissions_database(database: Path) -> None:
    with connect_database(database) as db:
        for kind, table in (("series", "series"), ("issue", "issues")):
            db.execute(f"""CREATE TABLE IF NOT EXISTS {kind}_restrictions (
                target_id TEXT PRIMARY KEY REFERENCES {table}(id) ON DELETE CASCADE
            )""")
            db.execute(f"""CREATE TABLE IF NOT EXISTS {kind}_access (
                target_id TEXT NOT NULL REFERENCES {kind}_restrictions(target_id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY(target_id, user_id)
            )""")


class AccessPolicy:
    """A request-local snapshot; never cached across requests or account changes."""

    def __init__(self, database: Path, user: User):
        self.admin = user.is_admin
        self.denied_series: set[str] = set()
        self.denied_issues: set[str] = set()
        self.hidden_authors: set[str] = set()
        self.partially_hidden_series: set[str] = set()
        if self.admin:
            return
        with connect_database(database) as db:
            # UNION prevents malformed cycles from looping. Descendants cannot
            # reopen access denied by an ancestor.
            self.denied_series = {row[0] for row in db.execute("""
                WITH RECURSIVE denied(id) AS (
                    SELECT r.target_id FROM series_restrictions r
                    WHERE NOT EXISTS (SELECT 1 FROM series_access a
                        WHERE a.target_id = r.target_id AND a.user_id = ?)
                    UNION
                    SELECT s.id FROM series s JOIN denied d ON s.parent_series_id = d.id
                ) SELECT id FROM denied
            """, (user.id,))}
            self.denied_issues = {row[0] for row in db.execute("""
                SELECT r.target_id FROM issue_restrictions r
                WHERE NOT EXISTS (SELECT 1 FROM issue_access a
                    WHERE a.target_id = r.target_id AND a.user_id = ?)
            """, (user.id,))}
            for issue_id, series_id in db.execute("SELECT id, series_id FROM issues"):
                if series_id in self.denied_series:
                    self.denied_issues.add(issue_id)
                if issue_id in self.denied_issues:
                    self.partially_hidden_series.add(series_id)
            authors: dict[str, bool] = {}
            for series_id, author_id in db.execute("SELECT id, author_id FROM series"):
                authors[author_id] = authors.get(author_id, False) or self.series(series_id)
            self.hidden_authors = {key for key, visible in authors.items() if not visible}

    def series(self, target_id: str) -> bool:
        return target_id not in self.denied_series

    def issue(self, target_id: str) -> bool:
        return target_id not in self.denied_issues

    def author(self, target_id: str) -> bool:
        return target_id not in self.hidden_authors

    def group(self, series_id: str, issue_id: str | None) -> bool:
        return self.series(series_id) and (issue_id is None or self.issue(issue_id))

    def register_sql(self, db: sqlite3.Connection) -> None:
        db.create_function("can_view_author", 1, self.author)
        db.create_function("can_view_series", 1, self.series)
        db.create_function("can_view_issue", 1, self.issue)
        db.create_function("can_view_group", 2, self.group)


def can_view_media(db: sqlite3.Connection, user: User, media_id: str) -> bool:
    """Check just this media's owners/ancestors, without loading the library."""
    if user.is_admin:
        return True
    row = db.execute("""
        WITH RECURSIVE owners(series_id, issue_id) AS (
            SELECT g.series_id, g.issue_id FROM media m
            JOIN content_groups g ON g.id = m.group_id WHERE m.id = ?
        ), ancestors(id) AS (
            SELECT series_id FROM owners
            UNION
            SELECT i.series_id FROM issues i JOIN owners o ON i.id = o.issue_id
            UNION
            SELECT s.parent_series_id FROM series s JOIN ancestors a ON s.id = a.id
            WHERE s.parent_series_id IS NOT NULL
        )
        SELECT EXISTS(SELECT 1 FROM owners)
          AND NOT EXISTS (
            SELECT 1 FROM ancestors a JOIN series_restrictions r ON r.target_id = a.id
            WHERE NOT EXISTS (SELECT 1 FROM series_access p
                WHERE p.target_id = r.target_id AND p.user_id = ?)
          ) AND NOT EXISTS (
            SELECT 1 FROM owners o JOIN issue_restrictions r ON r.target_id = o.issue_id
            WHERE NOT EXISTS (SELECT 1 FROM issue_access p
                WHERE p.target_id = r.target_id AND p.user_id = ?)
          )
    """, (media_id, user.id, user.id)).fetchone()
    return bool(row[0])


def _tables(kind: str) -> tuple[str, str, str]:
    if kind not in {"series", "issue"}:
        raise ValueError("Unknown permission type")
    return ("series" if kind == "series" else "issues", f"{kind}_restrictions", f"{kind}_access")


def get_restriction(db: sqlite3.Connection, kind: str, target_id: str) -> dict:
    _, restrictions, access = _tables(kind)
    return {
        "restricted": db.execute(f"SELECT 1 FROM {restrictions} WHERE target_id = ?", (target_id,)).fetchone() is not None,
        "users": sorted(row[0] for row in db.execute(f"SELECT user_id FROM {access} WHERE target_id = ?", (target_id,))),
    }


def save_restriction(database: Path, actor: User, kind: str, target_id: str,
                     restricted: bool, user_ids: set[str]) -> None:
    if not actor.is_admin:
        raise PermissionError("Administrator access required")
    table, restrictions, access = _tables(kind)
    with connect_database(database) as db:
        # Keep existence checks, replacement and audit in the same transaction.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (target_id,)).fetchone():
            raise LookupError("Comic not found")
        known = {row[0] for row in db.execute("SELECT id FROM users")}
        if not user_ids <= known:
            raise ValueError("Unknown user selected; reload the permissions page")
        before = get_restriction(db, kind, target_id)
        db.execute(f"DELETE FROM {restrictions} WHERE target_id = ?", (target_id,))
        if restricted:
            db.execute(f"INSERT INTO {restrictions}(target_id) VALUES (?)", (target_id,))
            db.executemany(f"INSERT INTO {access}(target_id, user_id) VALUES (?, ?)",
                           [(target_id, user_id) for user_id in sorted(user_ids)])
        after = get_restriction(db, kind, target_id)
        if before != after:
            db.execute("""INSERT INTO audit_log(entity_type, entity_id, action, before_json, after_json)
                VALUES (?, ?, 'permissions', ?, ?)""", (
                    kind, target_id, json.dumps(before), json.dumps({**after, "actor_id": actor.id}),
                ))
