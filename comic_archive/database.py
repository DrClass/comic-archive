from __future__ import annotations

import sqlite3
from pathlib import Path


def database_path(database_path: str | Path) -> Path:
    """Return the canonical database path without running migrations."""
    return Path(database_path).expanduser().resolve()


def connect_database(database_path_value: str | Path, *, row_factory: bool = False) -> sqlite3.Connection:
    """Open an application database connection without schema work.

    Schema creation/migration belongs at application/CLI boundaries, not in
    ordinary request-time reads.
    """
    db = sqlite3.connect(database_path(database_path_value))
    if row_factory:
        db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db
