# ARCHITECTURE.md — Comic Archive

## Overview

Comic Archive imports irregular creator/archive folders into a normalized private library. Flow:

```text
source filesystem or browser upload
 -> indexed scanner (+ PDF render cache)
 -> ReviewPlan/inferred classifications
 -> editable virtual workspace
 -> StagedImport validation model
 -> commit (duplicate checks, hashes, copies, thumbnails, SQLite)
 -> authenticated library/reader/maintenance UI
```

Source content is always read-only.

## Stack

Python >=3.10, FastAPI, Jinja2, SQLite, Starlette sessions, Argon2, Pillow, PyMuPDF, Uvicorn; Caddy intended for production HTTPS. UI is server-rendered with targeted JavaScript for importer/workspace/reader behavior.

## App assembly

`comic_archive/web.py::create_app(database_path, library_root, staging_root, secure_cookies=False, import_root=None, log_file=None, log_level="INFO")` initializes schemas, roots, FastAPI state, middleware, routes, and session middleware. OpenAPI/docs are disabled. `/health` returns `{"ok": true}` to authenticated callers; anonymous requests redirect to login.

Important app state: `log_file`, `database_path`, `library_root`, `staging_root`, `import_root`, `upload_sessions`, `import_sessions`, `bulk_import_sessions`, `login_attempts`, `import_progress`, `staging_progress`, `last_upload_sweep`.

## Authentication/security

Local database-backed accounts. Argon2 passwords. `users` plus `app_settings`. Session cookie `comic_archive_session`, SameSite=Lax, 30-day max age, HTTPS-only when configured. Persistent secret is stored in `app_settings`. No public signup. `session_version` invalidates old sessions after password/account changes. CSRF tokens live in the session. Import/admin/maintenance/history/edit paths and POSTs under media/groups/issues/series are administrator-only. All series POSTs, including deletion, are covered; GET series viewing remains available to readers. Account-password and per-user progress mutations are available to authenticated users with CSRF. Deletion additionally requires the exact confirmation title.

## Database model

SQLite is authoritative metadata storage. Ordinary connections come from `database.connect_database()` and enable foreign keys without migrations.

Main schema:

```text
authors(id PK, name UNIQUE NOCASE)

series(
  id PK, author_id FK, title,
  complete nullable,
  parent_series_id FK->series nullable,
  sort_order nullable,
  created_at, updated_at,
  UNIQUE(author_id,title)
)

issues(
  id PK, series_id FK, source_key,
  issue_number nullable TEXT, title nullable,
  complete nullable, sort_order nullable,
  content_fingerprint nullable,
  created_at, updated_at
)

content_groups(
  id PK, series_id FK,
  issue_id FK nullable,
  name, role, relative_path, sort_order
)

media(
  id PK, group_id FK, position,
  stored_path UNIQUE, source_path,
  original_relative_path,
  mime_type, media_kind, size_bytes,
  sha256 nullable, active default 1,
  UNIQUE(group_id,position)
)

imports(
  id PK, staging_id UNIQUE, source_path,
  author_id FK, series_id FK, committed_at
)

reading_progress(
  user_id, issue_id FK, page, completed, updated_at,
  PRIMARY KEY(user_id,issue_id)
)

intentional_missing_issues(
  series_id FK, issue_number INTEGER, note, created_at,
  PRIMARY KEY(series_id,issue_number)
)

audit_log(
  id integer PK, entity_type, entity_id, action,
  before_json, after_json, created_at
)
```

Schema triggers update series/issue `updated_at` when related entities change. Migration is currently ad hoc (`CREATE TABLE IF NOT EXISTS`, `PRAGMA table_info`, conditional `ALTER TABLE`, backfills/triggers), not a formal migration framework.

A series can have direct issues, direct extras, and child series simultaneously. `content_groups.issue_id IS NULL` represents a series-level group.

## Managed storage

```text
<library_root>/
  series/<series-id>/groups/<group-id>/
    000001.ext
    000002.ext
    ...
    _thumbs/<media-id>.jpg
```

Generated IDs intentionally decouple managed storage from source names/layout.

Thumbnails: JPEG, max 320x480, quality 78; transparent images composite onto white; animated images use first frame. Current path does not create image thumbnails for MP4. JPEGs use decoder-level downsampling before final resize. `create_thumbnail` writes to a unique sibling temporary file, closes it, and atomically replaces the final JPEG. Ordinary generation exceptions log a warning and return False; cleanup is best-effort, and KeyboardInterrupt/SystemExit propagate. Existing final derivatives survive failed writes. `ensure_thumbnail` and batch rebuilding reuse this behavior; batch rebuilding counts failures and continues. Hard process termination can leave temporary files, and existing derivatives are not integrity-checked.

## Supported import media

Scanner extensions: `.jpg`, `.jpeg`, `.png`, `.gif`, `.mp4`, `.pdf`. Images/video map to media kinds. PDF is input-only: PyMuPDF renders pages to high-quality JPEG; the PDF itself is not committed as reader media. Source originals are untouched.

## Scanner

`importer/scanner.py` uses an indexed filesystem pass with `os.scandir`/cached metadata rather than repeated recursive discovery. Downstream code should consume the scan model instead of walking the filesystem again. Scanner emits progress and diagnostic logging.

Multiple loose PDFs at a series root can seed one logical issue per PDF. PDF cache identity uses source path/stat information.

## Review and workspace

`importer/review.py` produces a `ReviewPlan` with roles: Issue, Primary Pages, Issue-Extras, Series-Extras, Sub-Series. The virtual workspace (`services/workspace.py`, `routes/import_workspace.py`) adds logical roles including Series, Container, Unassigned, Ignore.

Workspace supports metadata, synthetic issues/sub-series/groups, drag/reparent, folder/media order, one/multi-page targeting, ignore/restore, issue/series extras, and root Series/Issue/Container/Ignore. Physical source files never move.

`ImportSession` runtime cache fields include:

```text
workspace_cache_signature
workspace_cached_tree
workspace_cached_folder_media
workspace_cached_media_index
workspace_cached_media_owner
workspace_cached_automatic_media_owner
workspace_seed_folder_media
workspace_cache_builds
workspace_ownership_dirty
```

Immutable scanner folder/media membership is separated from mutable semantic ownership. Explicit user targets override automatic ownership. Selected-folder media is paginated at 250 rows.

## Staging

`importer/staging.py`, current `SCHEMA_VERSION = 2`.

Models: `StagedImport`, `StagedSeries`, `StagedIssue`, `StagedGroup`, `StagedMedia`.

`StagedImport` stores staging ID/time/source/content root/author/top series/import kind/completion plus issues, subseries, and series extras. Workspace finalization resolves logical ownership and rejects invalid/unassigned structure before commit.

## Commit

`importer/commit.py::commit_staged_import()` validates staged sources, performs duplicate/hash checks, creates/fetches hierarchy rows, copies media, inserts groups/media, generates best-effort thumbnails, records the import, and returns `CommitResult` (`import_id`, author/series IDs, issue IDs, copied counts/bytes, paths, duplicate warnings).

SHA-256 hashes and issue fingerprints support duplicate detection. `imports.staging_id` uniqueness prevents duplicate commit. The domain function raises CommitError for an already committed staging ID; web orchestration retrieves the existing receipt/result to provide replay recovery. Validation checks source existence and size; it is not a snapshot guarantee against same-size external edits.

## Browser upload

`services/uploads.py` + `routes/import_uploads.py`. Modern resumable upload sends each file as raw `application/octet-stream`, not multipart. Per-file max 16 GiB; write buffer 4 MiB; browser concurrency 6. Incomplete files use `.part`, then atomically finalize. JSON state is periodically checkpointed. On restoration, reconciliation builds a local set of completed disk files and replaces saved `received_paths` only after successful enumeration, including an empty result. `.part` files are excluded, missing saved files are dropped, and uncheckpointed completed files are recovered. Directory enumeration errors raise; restoration returns None without caching a partial session or rewriting saved state. Finalization returns 409 when recovered count differs from expected count. No filesystem rescan is added to each ordinary status/file request, so external deletion during a live session is not continuously detected.

Legacy multipart upload routes remain for compatibility, but should not replace the resumable large-folder path.

## Session persistence/recovery

`services/import_sessions.py`. Inactivity timeout: 12 hours. JSON state lives under `<staging_root>/session_state/` for import/bulk/completed/upload state. Persisted workspace edits include metadata, virtual nodes/groups, role/parent/order overrides, media targets, ignored media, upload/PDF cache references, and staged result. Runtime caches are not authoritative/persisted. Restore rescans the source and reapplies saved edits.

An upload-session cancellation endpoint already exists and cleans its temporary root. Full workflow cancellation, bulk-cancellation scope and forced-admin expiration remain planned work; the daemon-thread bridge has no general cancellation protocol.

Bulk sessions persist source, author, candidates, selected indices, current position, imported/skipped series, upload root, and activity.

## Library/read model

`library.py` builds `AuthorView -> SeriesView -> IssueView -> GroupView -> MediaView`, with recursive child series and computed totals. `read_library()` currently constructs the whole hierarchy and uses nested queries; this is a known future scalability target (whole-library/N+1 behavior).

Reader routes: `/read/{issue_id}`, `/read-group/{group_id}`, `/media/{media_id}`, `/thumbnail/{media_id}`. Images and MP4 supported. Reader includes single-page/vertical modes, fit/fullscreen, keyboard/swipe, thumbnails, and client preference storage. Reading progress is per-user; final primary page completes an issue; extras do not affect completion. Issue pages already provide Mark unread through `/progress/{issue_id}/reset`; a direct Mark read UI and whole-series status controls are not implemented.

## Editing/maintenance

Editing: author rename; series edit/move/reorder/delete; issue edit; extra group create/rename/move; media move/reorder; soft remove/restore; audit history. Recursive series deletion removes managed data/files only after confirmation and must never touch original source.

Maintenance covers missing managed files/thumbnails, empty groups, incomplete issues, duplicate fingerprints, and numeric gaps. Gaps can be marked intentionally missing with notes.

## Route groups

Auth/account: `/login`, `/logout`, `/admin/users`, `/account/password`.

Library: `/`, `/search`, `/authors/{id}`, `/series/{id}`, `/issues/{id}`, `/groups/{id}`, `/read/{id}`, `/read-group/{id}`, `/progress/{id}`.

Editing/maintenance/media: `/authors/{id}/edit`, `/series/{id}/edit`, `/series/{id}/delete`, `/issues/{id}/edit`, `/groups/{id}/edit`, `/media/{id}/remove|restore`, `/history`, `/maintenance`, `/series/{id}/missing/{n}`, `/media/{id}`, `/thumbnail/{id}`.

Import families: `/import`, `/import/browse`, `/import/scan`, `/import/upload`, `/import/bulk...`, `/import/upload-session...`, `/import/{session}/review`, `/import/{session}/workspace/...`, `/metadata`, `/organize`, `/confirm`, `/staging-progress`, `/commit-progress`, `/commit`, `/done`.

## Concurrency/diagnostics

Long scan/staging work uses a custom daemon-thread/future bridge in `services/diagnostics.py`; it was chosen because long threadpool work caused pytest TestClient teardown hangs. Commit uses `asyncio.to_thread`; not all background work uses the custom bridge. SQLite remains synchronous. `logging_config.py` configures process-wide rotating file logging for application messages and CLI-launched Uvicorn. The default is `logs/comic-archive.log` beside the database, with 10 MiB rotation and five backups. `create_app(log_file=..., log_level=...)` and CLI `--log-file`/`--log-level` override it. UTC timestamps, severity, logger names, `CA_DIAG`, and available memory checkpoints are retained. Errors also reach stderr; file failures fall back to stderr without aborting work.

## Deployment boundary

Intended: Browser -> Caddy HTTPS -> one Uvicorn/FastAPI process -> SQLite + local managed/staging filesystems. Multi-worker concurrent import coordination is not designed/tested.

## Frontend validation failure investigation

A user reports that "Validate and continue to confirmation" remains disabled after
an error until refresh, with edits preserved. Relevant code is the generic
processing-form submit/progress handling in `templates/base.html`, the async
metadata flush/finalize handler in `templates/import_workspace.html`, and
`routes/import_workspace.py` finalization/error response. The workspace handler
prevents default submission, awaits metadata saves, and only alerts on failure;
the generic handler disables submit controls. This interaction is an investigation
lead, not a browser-reproduced diagnosis. Tests using TestClient do not execute
JavaScript. See PROJECT_STATUS.md for next steps and all outstanding requests.
