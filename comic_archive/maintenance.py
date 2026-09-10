from __future__ import annotations
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from .database import connect_database
from .thumbnails import thumbnail_path_for_media

@dataclass(slots=True)
class Gap:
    series_id: str
    author_name: str
    series_title: str
    issue_number: int
    intentional: bool = False
    note: str | None = None

@dataclass(slots=True)
class MaintenanceReport:
    missing_files: list[dict] = field(default_factory=list)
    missing_thumbnails: list[dict] = field(default_factory=list)
    empty_groups: list[dict] = field(default_factory=list)
    incomplete_issues: list[dict] = field(default_factory=list)
    duplicate_issues: list[dict] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

def _numeric_issue_numbers(db, series_id: str) -> list[int]:
    nums=[]
    for (value,) in db.execute("SELECT issue_number FROM issues WHERE series_id=? AND issue_number IS NOT NULL",(series_id,)):
        try:
            text=str(value).strip()
            if text.isdigit():
                nums.append(int(text))
        except Exception:
            pass
    return sorted(set(nums))

def _series_gaps_from_connection(db: sqlite3.Connection, series_id: str) -> list[Gap]:
    series=db.execute(
        """SELECT s.id,s.title,a.name author_name
           FROM series s JOIN authors a ON a.id=s.author_id WHERE s.id=?""",
        (series_id,),
    ).fetchone()
    if not series:
        return []
    nums=_numeric_issue_numbers(db,series_id)
    if len(nums)<2:
        return []
    intentional={
        r["issue_number"]:r["note"]
        for r in db.execute(
            "SELECT issue_number,note FROM intentional_missing_issues WHERE series_id=?",
            (series_id,),
        )
    }
    gaps=[]
    for n in range(min(nums), max(nums)+1):
        if n not in nums:
            gaps.append(
                Gap(
                    series_id,
                    series["author_name"],
                    series["title"],
                    n,
                    n in intentional,
                    intentional.get(n),
                )
            )
    return gaps


def series_gaps(database_path, series_id: str) -> list[Gap]:
    database=Path(database_path).expanduser().resolve()
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        return _series_gaps_from_connection(db, series_id)

def set_intentional_gap(database_path, series_id: str, issue_number: int, intentional: bool, note: str|None=None) -> None:
    if issue_number < 0: raise ValueError("Issue number must be non-negative")
    database=Path(database_path).expanduser().resolve()
    with sqlite3.connect(database) as db:
        if not db.execute("SELECT 1 FROM series WHERE id=?",(series_id,)).fetchone():
            raise ValueError("Series not found")
        if intentional:
            db.execute("""INSERT INTO intentional_missing_issues(series_id,issue_number,note) VALUES(?,?,?)
                          ON CONFLICT(series_id,issue_number) DO UPDATE SET note=excluded.note""",
                       (series_id,issue_number,(note or "").strip() or None))
        else:
            db.execute("DELETE FROM intentional_missing_issues WHERE series_id=? AND issue_number=?",(series_id,issue_number))

def build_maintenance_report(database_path, library_root) -> MaintenanceReport:
    database=Path(database_path).expanduser().resolve()
    library=Path(library_root).expanduser().resolve()
    report=MaintenanceReport()
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute("""SELECT m.id,m.stored_path,m.mime_type,m.original_relative_path,g.name group_name,
                                        i.id issue_id,i.issue_number,i.title issue_title,s.id series_id,s.title series_title,a.name author_name
                                 FROM media m JOIN content_groups g ON g.id=m.group_id
                                 JOIN series s ON s.id=g.series_id JOIN authors a ON a.id=s.author_id
                                 LEFT JOIN issues i ON i.id=g.issue_id WHERE m.active=1"""):
            item=dict(row)
            path=library/row["stored_path"]
            if not path.is_file():
                report.missing_files.append(item)
            elif str(row["mime_type"]).startswith("image/"):
                thumb=thumbnail_path_for_media(library,row["id"],row["stored_path"])
                if not thumb.is_file():
                    report.missing_thumbnails.append(item)
        for row in db.execute("""SELECT g.id,g.name,g.role,g.issue_id,s.id series_id,s.title series_title,a.name author_name,
                                        i.issue_number,i.title issue_title
                                 FROM content_groups g JOIN series s ON s.id=g.series_id JOIN authors a ON a.id=s.author_id
                                 LEFT JOIN issues i ON i.id=g.issue_id
                                 LEFT JOIN media m ON m.group_id=g.id AND m.active=1
                                 GROUP BY g.id HAVING COUNT(m.id)=0"""):
            report.empty_groups.append(dict(row))
        for row in db.execute("""SELECT i.id issue_id,i.issue_number,i.title issue_title,i.complete,s.id series_id,s.title series_title,a.name author_name
                                 FROM issues i JOIN series s ON s.id=i.series_id JOIN authors a ON a.id=s.author_id
                                 WHERE i.complete IS NULL OR i.complete=0 ORDER BY a.name,s.title"""):
            report.incomplete_issues.append(dict(row))
        for row in db.execute("""SELECT content_fingerprint,COUNT(*) count FROM issues
                                 WHERE content_fingerprint IS NOT NULL AND content_fingerprint!=''
                                 GROUP BY content_fingerprint HAVING COUNT(*)>1"""):
            members=[dict(r) for r in db.execute("""SELECT i.id issue_id,i.issue_number,i.title issue_title,s.id series_id,s.title series_title,a.name author_name
                                                   FROM issues i JOIN series s ON s.id=i.series_id JOIN authors a ON a.id=s.author_id
                                                   WHERE i.content_fingerprint=? ORDER BY a.name,s.title""",(row["content_fingerprint"],))]
            report.duplicate_issues.append({"fingerprint":row["content_fingerprint"],"issues":members})
        series_ids=[row[0] for row in db.execute("SELECT id FROM series")]
        for series_id in series_ids:
            report.gaps.extend(_series_gaps_from_connection(db,series_id))
    return report
