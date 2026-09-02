from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .importer.commit import initialize_database


@dataclass(slots=True)
class ReadingProgress:
    issue_id: str
    page: int
    completed: bool
    updated_at: str


def get_progress(database_path: str | Path, user_id: str, issue_id: str) -> ReadingProgress | None:
    database = initialize_database(database_path)
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT issue_id, page, completed, updated_at FROM reading_progress WHERE user_id = ? AND issue_id = ?",
            (user_id, issue_id),
        ).fetchone()
    if row is None:
        return None
    return ReadingProgress(issue_id=row[0], page=row[1], completed=bool(row[2]), updated_at=row[3])


def get_progress_map(database_path: str | Path, user_id: str, issue_ids: list[str]) -> dict[str, ReadingProgress]:
    if not issue_ids:
        return {}
    database = initialize_database(database_path)
    placeholders = ",".join("?" for _ in issue_ids)
    with sqlite3.connect(database) as db:
        rows = db.execute(
            f"""SELECT issue_id, page, completed, updated_at
                FROM reading_progress
                WHERE user_id = ? AND issue_id IN ({placeholders})""",
            [user_id, *issue_ids],
        ).fetchall()
    return {
        row[0]: ReadingProgress(issue_id=row[0], page=row[1], completed=bool(row[2]), updated_at=row[3])
        for row in rows
    }


def save_progress(
    database_path: str | Path,
    user_id: str,
    issue_id: str,
    page: int,
    total_pages: int,
) -> ReadingProgress:
    if total_pages < 1:
        raise ValueError("total_pages must be positive")
    page = max(1, min(int(page), total_pages))
    completed = page >= total_pages
    database = initialize_database(database_path)
    with sqlite3.connect(database) as db:
        exists = db.execute("SELECT 1 FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if not exists:
            raise ValueError("Issue not found")
        db.execute(
            """INSERT INTO reading_progress(user_id, issue_id, page, completed, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id, issue_id) DO UPDATE SET
                 page = excluded.page,
                 completed = excluded.completed,
                 updated_at = CURRENT_TIMESTAMP""",
            (user_id, issue_id, page, int(completed)),
        )
        row = db.execute(
            "SELECT issue_id, page, completed, updated_at FROM reading_progress WHERE user_id = ? AND issue_id = ?",
            (user_id, issue_id),
        ).fetchone()
    return ReadingProgress(issue_id=row[0], page=row[1], completed=bool(row[2]), updated_at=row[3])


def reset_progress(database_path: str | Path, user_id: str, issue_id: str) -> None:
    database = initialize_database(database_path)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM reading_progress WHERE user_id = ? AND issue_id = ?", (user_id, issue_id))


def get_continue_reading(database_path: str | Path, user_id: str, *, limit: int = 8) -> list[dict[str, object]]:
    database = initialize_database(database_path)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT rp.issue_id, rp.page, rp.updated_at,
                      i.issue_number, i.title AS issue_title,
                      s.title AS series_title, a.name AS author_name
               FROM reading_progress rp
               JOIN issues i ON i.id = rp.issue_id
               JOIN series s ON s.id = i.series_id
               JOIN authors a ON a.id = s.author_id
               WHERE rp.user_id = ? AND rp.completed = 0
               ORDER BY rp.updated_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]
