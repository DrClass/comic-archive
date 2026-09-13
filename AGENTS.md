# AGENTS.md — Comic Archive Coding Agent Guide

## Baseline and purpose

Comic Archive is a private, account-gated FastAPI/SQLite/Jinja2 application for importing, correcting, organizing, reading, and maintaining a personal digital comic archive. The current baseline is **Milestone 45 plus post-handoff maintenance (2026-09-13)**, not the original Milestone 45 package. The structural refactor is complete. Read PROJECT_STATUS.md for the current development priority and unresolved product decisions. Repository implementation is authoritative when documentation disagrees.

## Before editing

1. Read `pyproject.toml`, `comic_archive/web.py`, the relevant route module, the relevant service/domain module, and its tests.
2. For importer work, inspect `services/workspace.py`, `services/import_sessions.py`, `services/uploads.py`, `importer/scanner.py`, `importer/staging.py`, and `importer/commit.py` before changing behavior.
3. Search all callers before moving/changing helpers. `web.py` wires route dependency dataclasses to services.
4. Preserve behavior unless a behavior change is explicitly requested. Add regression tests before/with risky changes.
5. Never modify original source comic files. Workspace changes are logical; commit copies into managed storage.
6. For performance changes, measure first. Prefer algorithmic/operation-count regressions over fragile wall-clock tests.

## Runtime/dependencies

Python **>=3.10**. `pyproject.toml` web extras: FastAPI >=0.110, Uvicorn >=0.29, Jinja2 >=3.1, argon2-cffi >=23.1, itsdangerous >=2.1, Pillow >=10, python-multipart >=0.0.9, PyMuPDF >=1.24. Dev extras: pytest >=8, httpx >=0.27. CLI entry point: `comic-import = comic_archive.cli:main`.

Typical development install is equivalent to `pip install -e '.[web,dev]'`.

## Production assumptions

Intended production topology: Caddy HTTPS reverse proxy -> **one** Uvicorn process -> FastAPI -> SQLite/local library+staging filesystems. Known production URL: `https://comics.super-original.net`. Use `--secure-cookies` behind HTTPS. The CLI defaults to host `127.0.0.1`, port `8000`; an older deployment used another loopback port, so do not hard-code it.

## Repository structure

```text
comic_archive/
  auth.py cli.py database.py editing.py library.py library_views.py
  logging_config.py maintenance.py progress.py thumbnails.py web.py web_forms.py
  importer/
    bulk.py commit.py models.py review.py scanner.py sorting.py staging.py
  routes/
    auth.py editing.py import_finalize.py import_review.py import_uploads.py
    import_workspace.py library.py maintenance.py media.py
  services/
    diagnostics.py import_orchestration.py import_sessions.py uploads.py workspace.py
  templates/
tests/
  test_commit.py test_editing.py test_folder_scanner.py test_library.py
  test_logging.py test_nested_series.py test_review.py test_staging.py test_thumbnails.py test_web.py
```

`web.py` was reduced from ~4,189 lines to ~350 during Milestone 42. Keep it an application assembly/middleware/dependency-wiring layer; do not put feature implementations back into it.

## Module conventions

HTTP endpoints belong under `comic_archive/routes/`. Long-lived importer/web implementation logic belongs under `comic_archive/services/`. Scanner/review/staging/commit domain logic belongs under `comic_archive/importer/` and should remain usable by the CLI where practical.

Route responsibilities: `auth.py` authentication/users; `library.py` browse/search/read/progress; `editing.py` edits/history; `maintenance.py`; `media.py`; `import_uploads.py` resumable uploads; `import_review.py` start/browse/scan/bulk/review; `import_workspace.py` workspace mutations/finalize; `import_finalize.py` metadata/organize/confirm/staging/commit/done.

Service responsibilities: `workspace.py` tree/ownership/cache/media/staged-build; `import_sessions.py` durable sessions/recovery/cleanup; `uploads.py` streaming uploads/checkpoints/reconciliation; `diagnostics.py` CA_DIAG/memory/background I/O; `import_orchestration.py` import/bulk glue.

## Invariants that must not regress

- Source files are read-only. Never move/rename/rewrite/delete them during import.
- Managed library content is copied under ID-based storage.
- JPG/JPEG/PNG/GIF/MP4 originals are not converted. PDF input is rendered to high-quality JPEG pages; source PDF remains untouched.
- Series can contain **both direct issues and child sub-series**.
- Missing/non-contiguous issue numbers are valid.
- Extras exist at both series and issue level.
- Duplicate protection and staging-ID commit idempotency must remain intact.
- Restart-safe uploads and recoverable import/bulk sessions must remain intact.
- Admin-only import/edit/maintenance protections and CSRF checks must remain intact; no public signup.
- Reading progress is per-user; extras do not determine issue completion.
- Soft-removed media (`active=0`) is restorable.
- Managed series deletion may delete managed copies/DB rows but never original sources.
- Thumbnail generation catches ordinary `Exception`, logs failures and returns False. It writes a unique sibling temporary file and atomically replaces the final derivative. Preserve existing derivatives on failed writes; clean temporary files best-effort. Do not catch `BaseException`: KeyboardInterrupt/SystemExit must propagate. Copy/hash/database errors remain fatal.
- All POST requests under `/series/` require administrator access; normal series GET viewing remains available to authenticated readers. Keep CSRF and confirmation-title checks for deletion.

## Performance invariants

Recent fixes removed severe large-comic scaling failures. Preserve them.

**Milestone 43:** workspace construction must not recursively re-walk the source after scanning; ownership resolution must not be media×nodes. Synthetic 5,000-page/50-folder: scan ~0.22s, workspace ~1.53s; old path failed to finish a comparison within two minutes.

**Milestone 44:** immutable scanner-folder->media index is reused across semantic cache invalidation; automatic media ownership is cached separately from explicit user targets; automatic origin lookup is direct; destination validation uses one tree pass; staged ancestry is precomputed. Synthetic 400 issues/1,200 pages: M43 cache rebuild ~3.06s vs M44 ~0.053s.

**Milestone 45:** JPEG thumbnails use Pillow decoder-level `draft()` downsampling and in-place EXIF transpose before final 320x480 resize. Synthetic 300 1200x1800 JPEG pages: ~11.0s -> ~3.3s. Do not casually remove this path. Parallel thumbnail workers were deliberately avoided to protect peak memory.

Workspace selected-folder media is paginated at **250 rows** to prevent huge HTML responses. Classification-only edits defer expensive authoritative ownership rebuilding until needed; validation/staging must still force correct ownership.

## Upload invariants

Modern browser folder upload uses raw streamed request bodies, not multipart parsing per file. Maximum file size: **16 GiB**. Disk write buffer: **4 MiB**. Browser concurrency: **6**. Files use `.part` while incomplete, then atomically finalize. Persisted state is checkpointed periodically; completed files on disk are authoritative during restart reconciliation. Do not revert the resumable endpoint to `request.form()`/`UploadFile`: the old multipart approach caused multi-GB memory growth on large imports.

## Database convention

`database.connect_database()` opens ordinary connections and enables foreign keys; it intentionally does not run migrations. Schema creation/migration belongs at application/CLI boundaries. `create_app()` initializes main/auth schemas. Do not reintroduce `initialize_database()` into ordinary browse/read requests; a regression test exists because request-time schema/backfill work previously caused overhead/database-lock collisions.

## Diagnostics

Application diagnostics use the shared rotating file configured by `logging_config.py`, defaulting to `logs/comic-archive.log` beside the database. INFO is the default; `--log-file` and `--log-level` configure the destination and verbosity. Records retain `CA_DIAG`; errors also reach stderr/journald, and file failures fall back to stderr. Memory checkpoints include RSS/anonymous/file-backed/virtual memory/thread counts where `/proc/self/status` supports them. Preserve these when changing import phases.

## Testing

Current verified baseline: **201 passed, 0 failed, 0 skipped** (Windows/Python 3.10; 84 non-web + 117 web). Run from the repository root:

```text
python -B -m pytest -q -p no:cacheprovider
```

Run focused tests first, then the full suite and an in-memory Python compile check (or compileall when cache files are acceptable). TestClient teardown has historically stalled in some environments; stable non-overlapping shards are acceptable if necessary, but first reproduce isolated failures. Recent full Windows runs finish normally. Account for every collected test. Do not weaken tests to preserve implementation.

Scanner/review models contain native `Path` objects. Use `.as_posix()` for comparisons with slash-formatted display strings. Staged string identifiers currently use native `str(Path)` in the legacy staging path; do not change that serialization contract casually. Preserve exact hierarchy, ordering and ownership assertions. The Windows fix changed tests, not production path semantics.

Dependencies are lower-bound constraints, not a lockfile. The current Starlette/httpx deprecation warning is documented in PROJECT_STATUS.md; it is not a test failure.

## Next performance work

Before optimizing again, capture a large real import on the current build and use phase timing/CA_DIAG evidence. Relevant phases: upload, scan, workspace, validation/staging, hashing, copying, thumbnailing, finalizing. Previously fixed: multipart memory growth, raw upload throughput, scanner rewalking, duplicate staging ownership, workspace click freeze, workspace cache quadratic behavior, JPEG thumbnail decode cost.

## Reporting changes

Report: what changed; why; behavior changes; exact tests/pass counts; benchmark setup/results if applicable; migration/config requirements; known limitations/follow-up. Distinguish synthetic benchmarks from production results.

## Recovery, logging and packaging safeguards

- Upload reconciliation occurs at restoration, not on every file/status request. Replace saved completion entries only after a successful full disk enumeration, even if empty; exclude `.part`, recover uncheckpointed completed files, and reject restoration on scan errors. Do not overwrite saved state from a partial scan. Live-session external file deletion is not continuously monitored.
- `logging_config.py` owns process-wide file configuration; `services/diagnostics.py` owns memory reporting and background I/O. Keep module loggers under `comic_archive`, configure at CLI/app boundaries, and preserve rotation, stderr fallback and error mirroring. Avoid import-time file creation. External Uvicorn launchers may configure server logging separately. Multi-process rotation is not supported.
- Cancellation/forced expiration must coordinate active workers before deleting temporary files; never delete original source folders or previously committed library content. Full import cancellation and forced-admin cleanup are planned, not currently implemented.
- For complete handoff ZIPs include all permanent Markdown documents, TODO.md, source/templates, tests, pyproject.toml, .gitignore and new/untracked source files. Do not use an old milestone ZIP rule that excludes Markdown. Exclude runtime databases (including account/session secrets), library/staging content, logs, caches, virtual environments and private environment files unless explicitly requested. A source ZIP should be built from the working tree when changes are uncommitted; `git archive HEAD` omits them.
