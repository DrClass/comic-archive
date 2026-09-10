from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

from .database import connect_database


class AuthError(RuntimeError):
    pass


@dataclass(slots=True)
class User:
    id: str
    username: str
    is_admin: bool
    active: bool
    session_version: int = 0


_hasher = PasswordHasher()


def _connect(database_path: str | Path, *, ensure_schema: bool = False) -> sqlite3.Connection:
    db = connect_database(database_path, row_factory=True)
    if ensure_schema:
        _ensure_auth_schema(db)
    return db


def initialize_auth_database(database_path: str | Path) -> None:
    """Create/migrate authentication tables at an application boundary."""
    with _connect(database_path, ensure_schema=True):
        pass


def _ensure_auth_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL COLLATE NOCASE UNIQUE,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        session_version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "session_version" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    db.commit()


def get_or_create_session_secret(database_path: str | Path) -> str:
    with _connect(database_path, ensure_schema=True) as db:
        row = db.execute("SELECT value FROM app_settings WHERE key = 'session_secret'").fetchone()
        if row:
            return row["value"]
        secret = secrets.token_urlsafe(48)
        db.execute("INSERT INTO app_settings(key, value) VALUES ('session_secret', ?)", (secret,))
        db.commit()
        return secret


def create_user(database_path: str | Path, username: str, password: str, *, is_admin: bool = False) -> User:
    username = username.strip()
    if not username:
        raise AuthError("Username cannot be empty")
    _validate_password(password)
    user_id = str(uuid4())
    password_hash = _hasher.hash(password)
    try:
        with _connect(database_path, ensure_schema=True) as db:
            db.execute(
                """INSERT INTO users(id, username, password_hash, is_admin, active, session_version)
                   VALUES (?, ?, ?, ?, 1, 0)""",
                (user_id, username, password_hash, int(is_admin)),
            )
            db.commit()
    except sqlite3.IntegrityError as exc:
        raise AuthError(f"Username already exists: {username}") from exc
    return User(user_id, username, is_admin, True, 0)


def authenticate(database_path: str | Path, username: str, password: str) -> User | None:
    with _connect(database_path) as db:
        row = db.execute(
            """SELECT id, username, password_hash, is_admin, active, session_version
               FROM users WHERE username = ? COLLATE NOCASE""",
            (username.strip(),),
        ).fetchone()
        if not row or not row["active"]:
            return None
        try:
            _hasher.verify(row["password_hash"], password)
        except (VerifyMismatchError, VerificationError):
            return None
        if _hasher.check_needs_rehash(row["password_hash"]):
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (_hasher.hash(password), row["id"]))
            db.commit()
        return _user_from_row(row)


def get_user(database_path: str | Path, user_id: str) -> User | None:
    with _connect(database_path) as db:
        row = db.execute(
            """SELECT id, username, is_admin, active, session_version
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
        if not row or not row["active"]:
            return None
        return _user_from_row(row)


def list_users(database_path: str | Path) -> list[User]:
    with _connect(database_path) as db:
        rows = db.execute(
            """SELECT id, username, is_admin, active, session_version
               FROM users ORDER BY username COLLATE NOCASE"""
        ).fetchall()
        return [_user_from_row(r) for r in rows]


def change_password(database_path: str | Path, user_id: str, current_password: str, new_password: str) -> User:
    _validate_password(new_password)
    with _connect(database_path) as db:
        row = db.execute(
            """SELECT id, username, password_hash, is_admin, active, session_version
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
        if not row or not row["active"]:
            raise AuthError("Account not found or disabled")
        try:
            _hasher.verify(row["password_hash"], current_password)
        except (VerifyMismatchError, VerificationError) as exc:
            raise AuthError("Current password is incorrect") from exc
        new_version = int(row["session_version"]) + 1
        db.execute(
            "UPDATE users SET password_hash = ?, session_version = ? WHERE id = ?",
            (_hasher.hash(new_password), new_version, user_id),
        )
        db.commit()
        return User(row["id"], row["username"], bool(row["is_admin"]), True, new_version)


def reset_password(database_path: str | Path, user_id: str, new_password: str) -> None:
    _validate_password(new_password)
    with _connect(database_path) as db:
        if not db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise AuthError("Account not found")
        db.execute(
            """UPDATE users
               SET password_hash = ?, session_version = session_version + 1
               WHERE id = ?""",
            (_hasher.hash(new_password), user_id),
        )
        db.commit()


def set_user_active(database_path: str | Path, user_id: str, active: bool) -> None:
    with _connect(database_path) as db:
        row = db.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise AuthError("Account not found")
        db.execute(
            """UPDATE users
               SET active = ?, session_version = session_version + 1
               WHERE id = ?""",
            (int(active), user_id),
        )
        db.commit()


def set_user_admin(database_path: str | Path, user_id: str, is_admin: bool) -> None:
    with _connect(database_path) as db:
        row = db.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise AuthError("Account not found")
        db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
        db.commit()


def _validate_password(password: str) -> None:
    if len(password) < 10:
        raise AuthError("Password must be at least 10 characters")


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        row["id"],
        row["username"],
        bool(row["is_admin"]),
        bool(row["active"]),
        int(row["session_version"]),
    )
