from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .staging import SCHEMA_VERSION, StagedGroup, StagedImport, StagedIssue, StagedMedia, StagedSeries


class CommitError(RuntimeError):
    pass


@dataclass(slots=True)
class CommitResult:
    import_id: str
    author_id: str
    series_id: str
    issue_ids: list[str]
    copied_files: int
    copied_bytes: int
    database_path: Path
    library_root: Path
    duplicate_warnings: list[str]


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS authors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
);

CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    title TEXT NOT NULL COLLATE NOCASE,
    complete INTEGER,
    parent_series_id TEXT REFERENCES series(id) ON DELETE SET NULL,
    sort_order INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(author_id, title)
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    source_key TEXT,
    issue_number TEXT,
    title TEXT,
    complete INTEGER,
    sort_order INTEGER,
    content_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_groups (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    issue_id TEXT REFERENCES issues(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sort_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES content_groups(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    stored_path TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    original_relative_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(group_id, position)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS imports (
    id TEXT PRIMARY KEY,
    staging_id TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    author_id TEXT NOT NULL REFERENCES authors(id),
    series_id TEXT NOT NULL REFERENCES series(id),
    committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reading_progress (
    user_id TEXT NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, issue_id)
);

CREATE TABLE IF NOT EXISTS intentional_missing_issues (
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    issue_number INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(series_id, issue_number)
);
"""




def _ensure_schema_columns(db: sqlite3.Connection) -> None:
    issue_columns = {row[1] for row in db.execute("PRAGMA table_info(issues)")}
    media_columns = {row[1] for row in db.execute("PRAGMA table_info(media)")}
    if "content_fingerprint" not in issue_columns:
        db.execute("ALTER TABLE issues ADD COLUMN content_fingerprint TEXT")
    if "sort_order" not in issue_columns:
        db.execute("ALTER TABLE issues ADD COLUMN sort_order INTEGER")
        # Preserve the library's previous display order when introducing manual ordering.
        series_ids = [row[0] for row in db.execute("SELECT id FROM series").fetchall()]
        for series_id in series_ids:
            issue_ids = [row[0] for row in db.execute(
                """SELECT id FROM issues WHERE series_id = ?
                   ORDER BY COALESCE(issue_number, title, source_key) COLLATE NOCASE""",
                (series_id,),
            ).fetchall()]
            for index, issue_id in enumerate(issue_ids, start=1):
                db.execute("UPDATE issues SET sort_order = ? WHERE id = ?", (index, issue_id))
    if "sha256" not in media_columns:
        db.execute("ALTER TABLE media ADD COLUMN sha256 TEXT")
    if "active" not in media_columns:
        db.execute("ALTER TABLE media ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    series_columns = {row[1] for row in db.execute("PRAGMA table_info(series)")}
    if "complete" not in series_columns:
        db.execute("ALTER TABLE series ADD COLUMN complete INTEGER")
    if "parent_series_id" not in series_columns:
        db.execute("ALTER TABLE series ADD COLUMN parent_series_id TEXT REFERENCES series(id) ON DELETE SET NULL")
    if "sort_order" not in series_columns:
        db.execute("ALTER TABLE series ADD COLUMN sort_order INTEGER")
        rows = db.execute("SELECT id, author_id FROM series ORDER BY author_id, title COLLATE NOCASE").fetchall()
        counters: dict[str, int] = {}
        for series_id, author_id in rows:
            counters[author_id] = counters.get(author_id, 0) + 1
            db.execute("UPDATE series SET sort_order = ? WHERE id = ?", (counters[author_id], series_id))
    if "created_at" not in series_columns:
        db.execute("ALTER TABLE series ADD COLUMN created_at TEXT")
    if "updated_at" not in series_columns:
        db.execute("ALTER TABLE series ADD COLUMN updated_at TEXT")
    if "created_at" not in issue_columns:
        db.execute("ALTER TABLE issues ADD COLUMN created_at TEXT")
    if "updated_at" not in issue_columns:
        db.execute("ALTER TABLE issues ADD COLUMN updated_at TEXT")
    db.execute("""UPDATE series
                  SET created_at = COALESCE(created_at, (SELECT MIN(committed_at) FROM imports WHERE imports.series_id = series.id), CURRENT_TIMESTAMP),
                      updated_at = COALESCE(updated_at, (SELECT MAX(committed_at) FROM imports WHERE imports.series_id = series.id), CURRENT_TIMESTAMP)""")
    db.execute("""UPDATE issues
                  SET created_at = COALESCE(created_at, (SELECT MIN(committed_at) FROM imports WHERE imports.series_id = issues.series_id), CURRENT_TIMESTAMP),
                      updated_at = COALESCE(updated_at, (SELECT MAX(committed_at) FROM imports WHERE imports.series_id = issues.series_id), CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS reading_progress (
        user_id TEXT NOT NULL,
        issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        page INTEGER NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, issue_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS intentional_missing_issues (
        series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
        issue_number INTEGER NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(series_id, issue_number)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.executescript("""
    CREATE TRIGGER IF NOT EXISTS touch_series_after_series_update
    AFTER UPDATE OF title, author_id, complete, parent_series_id, sort_order ON series
    BEGIN
      UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
    END;
    CREATE TRIGGER IF NOT EXISTS touch_issue_after_issue_update
    AFTER UPDATE OF series_id, issue_number, title, complete, sort_order, content_fingerprint ON issues
    BEGIN
      UPDATE issues SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
      UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id IN (OLD.series_id, NEW.series_id);
    END;
    CREATE TRIGGER IF NOT EXISTS touch_issue_after_sort_order_update
    AFTER UPDATE OF sort_order ON issues
    BEGIN
      UPDATE issues SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
      UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id IN (OLD.series_id, NEW.series_id);
    END;
    CREATE TRIGGER IF NOT EXISTS touch_group_after_insert
    AFTER INSERT ON content_groups
    BEGIN
      UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.series_id;
      UPDATE issues SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.issue_id;
    END;
    CREATE TRIGGER IF NOT EXISTS touch_group_after_update
    AFTER UPDATE OF series_id, issue_id, name, role, sort_order ON content_groups
    BEGIN
      UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id IN (OLD.series_id, NEW.series_id);
      UPDATE issues SET updated_at = CURRENT_TIMESTAMP WHERE id IN (OLD.issue_id, NEW.issue_id);
    END;
    CREATE TRIGGER IF NOT EXISTS touch_media_after_insert
    AFTER INSERT ON media
    BEGIN
      UPDATE issues SET updated_at = CURRENT_TIMESTAMP WHERE id = (SELECT issue_id FROM content_groups WHERE id = NEW.group_id);
      UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id = (SELECT series_id FROM content_groups WHERE id = NEW.group_id);
    END;
    CREATE TRIGGER IF NOT EXISTS touch_media_after_update
    AFTER UPDATE OF group_id, position, active ON media
    BEGIN
      UPDATE issues SET updated_at = CURRENT_TIMESTAMP WHERE id IN (
        SELECT issue_id FROM content_groups WHERE id IN (OLD.group_id, NEW.group_id)
      );
      UPDATE series SET updated_at = CURRENT_TIMESTAMP WHERE id IN (
        SELECT series_id FROM content_groups WHERE id IN (OLD.group_id, NEW.group_id)
      );
    END;
    """)


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue_fingerprint(issue: StagedIssue) -> tuple[str, dict[str, str]]:
    """Hash issue structure and bytes while preserving group/media order."""
    digest = hashlib.sha256()
    hashes: dict[str, str] = {}
    for group in issue.groups:
        for item in sorted(group.media, key=lambda x: x.order):
            file_hash = _sha256_file(item.source_path)
            hashes[item.source_path] = file_hash
            digest.update(file_hash.encode("ascii"))
            digest.update(b"\0")
    return digest.hexdigest(), hashes


def _backfill_hashes_and_fingerprints(
    db: sqlite3.Connection, library_root: Path
) -> None:
    missing_media = db.execute(
        "SELECT id, stored_path FROM media WHERE sha256 IS NULL OR sha256 = ''"
    ).fetchall()
    for media_id, stored_path in missing_media:
        path = library_root / stored_path
        if path.is_file():
            db.execute("UPDATE media SET sha256 = ? WHERE id = ?", (_sha256_file(path), media_id))

    issue_rows = db.execute(
        "SELECT id FROM issues WHERE content_fingerprint IS NULL OR content_fingerprint = ''"
    ).fetchall()
    for (issue_id,) in issue_rows:
        hashes = db.execute(
            """SELECT m.sha256
               FROM content_groups g
               JOIN media m ON m.group_id = g.id
               WHERE g.issue_id = ? AND m.active = 1 AND m.sha256 IS NOT NULL
               ORDER BY g.sort_order, m.position""",
            (issue_id,),
        ).fetchall()
        if not hashes:
            continue
        digest = hashlib.sha256()
        for (file_hash,) in hashes:
            digest.update(file_hash.encode("ascii"))
            digest.update(b"\0")
        db.execute(
            "UPDATE issues SET content_fingerprint = ? WHERE id = ?",
            (digest.hexdigest(), issue_id),
        )


def find_duplicate_warnings(
    db: sqlite3.Connection, staged: StagedImport
) -> tuple[list[str], dict[str, tuple[str, dict[str, str]]]]:
    warnings: list[str] = []
    computed: dict[str, tuple[str, dict[str, str]]] = {}

    author = db.execute(
        "SELECT id FROM authors WHERE name = ? COLLATE NOCASE", (staged.author,)
    ).fetchone()
    series_id = None
    if author:
        series = db.execute(
            "SELECT id FROM series WHERE author_id = ? AND title = ? COLLATE NOCASE",
            (author[0], staged.series),
        ).fetchone()
        if series:
            series_id = series[0]

    for issue in staged.issues:
        fingerprint, hashes = _issue_fingerprint(issue)
        computed[issue.source_key] = (fingerprint, hashes)

        content_match = db.execute(
            """SELECT i.issue_number, i.title, s.title, a.name
               FROM issues i
               JOIN series s ON s.id = i.series_id
               JOIN authors a ON a.id = s.author_id
               WHERE i.content_fingerprint = ?
               LIMIT 1""",
            (fingerprint,),
        ).fetchone()
        if content_match:
            number, title, series_title, author_name = content_match
            label = number or title or "(unlabeled issue)"
            warnings.append(
                f"Exact content match: {author_name} / {series_title} / {label}"
            )

        if series_id is not None and (issue.issue_number or issue.title):
            clauses = []
            params: list[object] = [series_id]
            if issue.issue_number:
                clauses.append("issue_number = ? COLLATE NOCASE")
                params.append(issue.issue_number)
            if issue.title:
                clauses.append("title = ? COLLATE NOCASE")
                params.append(issue.title)
            row = db.execute(
                f"SELECT issue_number, title FROM issues WHERE series_id = ? AND ({' OR '.join(clauses)}) LIMIT 1",
                params,
            ).fetchone()
            if row:
                label = row[0] or row[1] or "(unlabeled issue)"
                warnings.append(
                    f"Metadata match in {staged.author} / {staged.series}: existing {label}"
                )

    # Keep messages stable and non-redundant.
    return list(dict.fromkeys(warnings)), computed


def load_staged_import(path: str | Path) -> StagedImport:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise CommitError(f"Staging file does not exist: {source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommitError(f"Could not read staging record: {exc}") from exc

    if data.get("schema_version") != SCHEMA_VERSION:
        raise CommitError(
            f"Unsupported staging schema {data.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )

    def media(item: dict) -> StagedMedia:
        return StagedMedia(**item)

    def group(item: dict) -> StagedGroup:
        payload = dict(item)
        payload["media"] = [media(x) for x in payload.get("media", [])]
        return StagedGroup(**payload)

    def issue(item: dict) -> StagedIssue:
        payload = dict(item)
        payload["groups"] = [group(x) for x in payload.get("groups", [])]
        return StagedIssue(**payload)

    def nested_series(item: dict) -> StagedSeries:
        return StagedSeries(**item)

    try:
        return StagedImport(
            schema_version=data["schema_version"],
            staging_id=data["staging_id"],
            created_at=data["created_at"],
            source_path=data["source_path"],
            content_root=data["content_root"],
            author=data["author"],
            series=data["series"],
            series_complete=data.get("series_complete"),
            import_kind=data["import_kind"],
            issues=[issue(x) for x in data.get("issues", [])],
            subseries=[nested_series(x) for x in data.get("subseries", [])],
            series_extras=[group(x) for x in data.get("series_extras", [])],
        )
    except (KeyError, TypeError) as exc:
        raise CommitError(f"Invalid staging record: {exc}") from exc


def initialize_database(database_path: str | Path) -> Path:
    destination = Path(database_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as db:
        db.executescript(_SCHEMA)
        _ensure_schema_columns(db)
    return destination


def validate_staged_sources(staged: StagedImport) -> list[str]:
    errors: list[str] = []
    all_groups = list(staged.series_extras)
    for issue in staged.issues:
        all_groups.extend(issue.groups)
    for group in all_groups:
        if not group.media:
            errors.append(f"Empty content group: {group.name}")
            continue
        for item in group.media:
            source = Path(item.source_path)
            if not source.is_file():
                errors.append(f"Missing source file: {source}")
                continue
            try:
                actual_size = source.stat().st_size
            except OSError as exc:
                errors.append(f"Cannot stat source file {source}: {exc}")
                continue
            if actual_size != item.size_bytes:
                errors.append(
                    f"Source file changed size since staging: {source} "
                    f"({item.size_bytes} -> {actual_size} bytes)"
                )
    return errors


def _find_or_create_author(db: sqlite3.Connection, name: str) -> str:
    row = db.execute("SELECT id FROM authors WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row[0]
    author_id = str(uuid4())
    db.execute("INSERT INTO authors(id, name) VALUES (?, ?)", (author_id, name))
    return author_id


def _find_or_create_series(
    db: sqlite3.Connection, author_id: str, title: str, complete: bool | None = None,
    *, parent_series_id: str | None = None, sort_order: int | None = None,
) -> str:
    row = db.execute(
        "SELECT id, complete, parent_series_id, sort_order FROM series WHERE author_id = ? AND title = ? COLLATE NOCASE",
        (author_id, title),
    ).fetchone()
    complete_value = None if complete is None else int(complete)
    if row:
        updates = []
        params: list[object] = []
        if complete is not None and row[1] != complete_value:
            updates.append("complete = ?"); params.append(complete_value)
        if row[2] != parent_series_id:
            updates.append("parent_series_id = ?"); params.append(parent_series_id)
        if sort_order is not None and row[3] != sort_order:
            updates.append("sort_order = ?"); params.append(sort_order)
        if updates:
            params.append(row[0])
            db.execute(f"UPDATE series SET {', '.join(updates)} WHERE id = ?", params)
        return row[0]
    series_id = str(uuid4())
    db.execute(
        "INSERT INTO series(id, author_id, title, complete, parent_series_id, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
        (series_id, author_id, title, complete_value, parent_series_id, sort_order),
    )
    return series_id


def _extension_for(item: StagedMedia) -> str:
    suffix = Path(item.source_path).suffix.lower()
    return suffix if suffix else ".bin"


def _copy_group(
    db: sqlite3.Connection,
    *,
    group: StagedGroup,
    series_id: str,
    issue_id: str | None,
    library_root: Path,
    sort_order: int,
    created_paths: list[Path],
    file_hashes: dict[str, str] | None = None,
) -> tuple[int, int]:
    group_id = str(uuid4())
    db.execute(
        """INSERT INTO content_groups
           (id, series_id, issue_id, name, role, relative_path, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (group_id, series_id, issue_id, group.name, group.role, group.relative_path, sort_order),
    )

    group_root = library_root / "series" / series_id / "groups" / group_id
    group_root.mkdir(parents=True, exist_ok=False)
    created_paths.append(group_root)

    copied_files = 0
    copied_bytes = 0
    for position, item in enumerate(sorted(group.media, key=lambda x: x.order), start=1):
        media_id = str(uuid4())
        filename = f"{position:06d}{_extension_for(item)}"
        destination = group_root / filename
        shutil.copy2(item.source_path, destination)
        stored_rel = destination.relative_to(library_root).as_posix()
        file_hash = (file_hashes or {}).get(item.source_path) or _sha256_file(item.source_path)
        db.execute(
            """INSERT INTO media
               (id, group_id, position, stored_path, source_path, original_relative_path,
                mime_type, media_kind, size_bytes, sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                media_id,
                group_id,
                position,
                stored_rel,
                item.source_path,
                item.relative_path,
                item.mime_type,
                item.media_kind,
                item.size_bytes,
                file_hash,
            ),
        )
        # Thumbnail generation is best-effort and never changes the original.
        # A failed derivative must not make an otherwise valid import fail.
        if item.mime_type.startswith("image/"):
            from ..thumbnails import create_thumbnail
            thumb_path = destination.parent / "_thumbs" / f"{media_id}.jpg"
            create_thumbnail(destination, thumb_path, mime_type=item.mime_type)
        copied_files += 1
        copied_bytes += item.size_bytes
    return copied_files, copied_bytes


def commit_staged_import(
    staged: StagedImport,
    *,
    library_root: str | Path,
    database_path: str | Path,
    allow_duplicate: bool = False,
) -> CommitResult:
    errors = validate_staged_sources(staged)
    if errors:
        raise CommitError("Staged import failed validation:\n- " + "\n- ".join(errors))

    library = Path(library_root).expanduser().resolve()
    library.mkdir(parents=True, exist_ok=True)
    database = initialize_database(database_path)

    created_paths: list[Path] = []
    copied_files = 0
    copied_bytes = 0
    issue_ids: list[str] = []
    import_id = str(uuid4())

    try:
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            _backfill_hashes_and_fingerprints(db, library)
            existing = db.execute(
                "SELECT id FROM imports WHERE staging_id = ?", (staged.staging_id,)
            ).fetchone()
            if existing:
                raise CommitError(f"Staging record has already been committed: {staged.staging_id}")

            duplicate_warnings, computed_hashes = find_duplicate_warnings(db, staged)
            if duplicate_warnings and not allow_duplicate:
                raise CommitError(
                    "Possible duplicate import detected:\n- "
                    + "\n- ".join(duplicate_warnings)
                    + "\nUse --allow-duplicate to import anyway."
                )

            author_id = _find_or_create_author(db, staged.author)
            series_id = _find_or_create_series(db, author_id, staged.series, staged.series_complete, parent_series_id=None)
            series_ids_by_key: dict[str, str] = {".": series_id}
            pending = list(staged.subseries)
            while pending:
                progressed = False
                for nested in list(pending):
                    if nested.parent_key not in series_ids_by_key:
                        continue
                    parent_id = series_ids_by_key[nested.parent_key]
                    nested_id = _find_or_create_series(
                        db, author_id, nested.title, nested.complete,
                        parent_series_id=parent_id, sort_order=nested.sort_order,
                    )
                    series_ids_by_key[nested.source_key] = nested_id
                    pending.remove(nested)
                    progressed = True
                if not progressed:
                    raise CommitError("Nested series hierarchy contains an unresolved parent")

            existing_orders = {
                key: db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM issues WHERE series_id = ?", (sid,)).fetchone()[0]
                for key, sid in series_ids_by_key.items()
            }
            imported_counts: dict[str, int] = {key: 0 for key in series_ids_by_key}
            ordered_staged_issues = sorted(
                enumerate(staged.issues),
                key=lambda pair: (
                    pair[1].sort_order if pair[1].sort_order is not None else pair[0] + 1,
                    pair[0],
                ),
            )
            for import_position, (_, issue) in enumerate(ordered_staged_issues, start=1):
                issue_id = str(uuid4())
                target_series_id = series_ids_by_key.get(issue.series_key)
                if target_series_id is None:
                    raise CommitError(f"Issue references unknown nested series: {issue.series_key}")
                imported_counts[issue.series_key] = imported_counts.get(issue.series_key, 0) + 1
                complete_value = None if issue.complete is None else int(issue.complete)
                fingerprint, issue_hashes = computed_hashes[issue.source_key]
                db.execute(
                    """INSERT INTO issues
                       (id, series_id, source_key, issue_number, title, complete, sort_order, content_fingerprint)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        issue_id,
                        target_series_id,
                        issue.source_key,
                        issue.issue_number,
                        issue.title,
                        complete_value,
                        existing_orders.get(issue.series_key, 0) + imported_counts[issue.series_key],
                        fingerprint,
                    ),
                )
                issue_ids.append(issue_id)
                for index, group in enumerate(issue.groups, start=1):
                    count, size = _copy_group(
                        db,
                        group=group,
                        series_id=target_series_id,
                        issue_id=issue_id,
                        library_root=library,
                        sort_order=index,
                        created_paths=created_paths,
                        file_hashes=issue_hashes,
                    )
                    copied_files += count
                    copied_bytes += size

            extra_orders: dict[str, int] = {}
            for index, group in enumerate(staged.series_extras, start=1):
                owner_key = group.owner_key or "."
                target_series_id = series_ids_by_key.get(owner_key)
                if target_series_id is None:
                    raise CommitError(f"Extra group references unknown nested series: {owner_key}")
                extra_orders[owner_key] = extra_orders.get(owner_key, 0) + 1
                count, size = _copy_group(
                    db,
                    group=group,
                    series_id=target_series_id,
                    issue_id=None,
                    library_root=library,
                    sort_order=extra_orders[owner_key],
                    created_paths=created_paths,
                )
                copied_files += count
                copied_bytes += size

            db.execute(
                """INSERT INTO imports(id, staging_id, source_path, author_id, series_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (import_id, staged.staging_id, staged.source_path, author_id, series_id),
            )
            db.commit()
    except Exception:
        # Database rollback is handled by sqlite context cleanup. Remove only
        # storage directories created by this commit; never touch source files.
        for path in reversed(created_paths):
            shutil.rmtree(path, ignore_errors=True)
        raise

    return CommitResult(
        import_id=import_id,
        author_id=author_id,
        series_id=series_id,
        issue_ids=issue_ids,
        copied_files=copied_files,
        copied_bytes=copied_bytes,
        database_path=database,
        library_root=library,
        duplicate_warnings=duplicate_warnings,
    )
