# PROJECT_STATUS.md — Comic Archive Current Handoff

## WEBP support (2026-09-17; not executed or tested)

- Added case-insensitive WEBP recognition as image/webp in the shared importer
  registry, covering folder scans, bulk discovery and workspace media detection.
  WEBP shares JPEG/PNG sorting priority. Original bytes, transparency and animation
  are preserved by the existing copy/serve paths; thumbnails use the existing
  first-frame JPEG generation and white transparency background.
- Added regression coverage for mixed-case names, ordering, extras, bulk discovery,
  browser upload/workspace/commit/reader serving, animated and transparent originals,
  and thumbnail generation/rebuild/on-demand recovery.
- No dependency, schema or configuration changes. No application, tests or validation
  commands were run. User-run checks: test_folder_scanner.py, test_thumbnails.py and
  test_web.py; manually check WEBP-only and mixed imports, extras, both reader modes,
  animation, transparency and thumbnail maintenance.

## Restricted comic catalog visibility (2026-09-17; not executed or tested)

- Supersedes the metadata-hiding behavior described below: library/author listings
  now include all comics and their full counts. Restricted series, child series and
  issues show titles/stats, a Restricted label, and a placeholder instead of comic
  artwork. Their cards have no link. Search includes restricted metadata with no
  link as well. Authors remain navigable to let readers browse these catalog cards.
- Preview selection excludes restricted issues and series even when selecting a
  cover for an unrestricted ancestor/author. Direct restricted detail, reader,
  group, media, thumbnail and progress routes remain protected. Continue reading
  still includes only accessible issues.
- Place the supplied 300x300 image at `comic_archive/no-permission.jpg`. The fixed,
  authenticated `/assets/no-permission` endpoint recognizes PNG bytes despite the
  .jpg filename (actual JPEG also works). A neutral Restricted SVG is returned if
  the file is absent. No image was created or replaced by this implementation.
- Modified library view helpers, library/media routes, home/author/series/search/
  base templates, and permission regression tests. No schema/dependency change.
- Application, tests and validation commands were not executed. Suggested user-run
  tests: `tests/test_permissions.py` and `tests/test_web.py`. Manually check denied,
  allowed and admin accounts, nested restrictions, counts, search, placeholder
  display, keyboard navigation and direct URL denial.

## Comic permissions implementation (2026-09-15; not executed or tested)

- Added default-open restrictions for individual series and issues. Administrators
  use the Permissions link on the series/issue detail page to select allowed users.
  No selected users means administrators only. Administrators always retain access.
- Every ancestor restriction applies to descendants; a child/issue allowlist can
  narrow access but cannot override a parent restriction. Extras follow their owner.
  New users receive access to unrestricted comics only. Disabled accounts still
  cannot sign in; their saved selections apply if the account is re-enabled.
- Browse counts/previews, search and Continue reading omit inaccessible content.
  Reader, group, media, thumbnail and progress routes enforce restrictions. Denied
  content returns 404. Progress is preserved when access is revoked.
- Approved additive schema initialization creates four permission tables at app
  startup, after main/auth initialization. Existing comics remain unrestricted.
  No dependency change, source-file changes or migration execution was performed.
- Permission replacements are transactional and audited with the administrator ID.
  Inheritance follows the current hierarchy, including after admin moves/reparenting.
- Authenticated responses now use private/no-store caching. This cannot recall
  files already downloaded, previously cached thumbnails from older builds, or
  content already displayed/in flight when a restriction changes.
- Missing-number reports are omitted for readers on series with hidden issues to
  avoid deriving details from inaccessible issues. Unrestricted series metadata
  remains visible when only an individual issue is restricted.
- Added tests/test_permissions.py for defaults, inheritance, direct URLs, search
  limits, progress, administrative protection, migration persistence and caching.
  No application, tests, discovery, compilation, benchmarks or validation commands
  were run, as requested. The implementation is unverified.

Suggested user-run checks: `python -B -m pytest -q -p no:cacheprovider tests/test_permissions.py`
then the existing web/library/editing/nested-series tests and full suite. Manually
check permission forms in separate administrator/allowed/denied browser sessions,
including inherited restrictions, revocation, search, thumbnails and MP4 playback.

## PDF fallback multiprocessing update (2026-09-15)

Authoritative baseline: the latest `comic-archive-main.zip` attached to the
PDF Performance conversation (archive entries dated 2026-09-14), including the
existing direct JPEG/PNG extraction fast path.

- Added a lazy two-process pool for fallback render plus JPEG-save work only,
  with explicit spawn and at most two page jobs in flight per PDF scan.
- Direct extraction eligibility and implementation, cache fingerprint/version,
  page order, 150 DPI, quality 98, progress messages, diagnostic fields, original
  source immutability, and non-PDF handling are preserved.
- Fallback workers publish JPEGs atomically; all exit paths wait for workers
  before returning to import cleanup. No migration or additional dependency.
- Added ten tests for scheduling, output equivalence, cache reuse, failures,
  cleanup, source preservation, non-PDF handling, and real spawned execution.

Validation on Windows/Python 3.10 in the Codex filesystem sandbox:

- Baseline PDF tests before editing: **9 passed**.
- Focused new tests excluding actual spawn: **9 passed, 1 deselected**.
- Full normal suite: **208 passed, 12 failed, 1 warning**, 29.90 seconds.
  All twelve failures originate from Windows denying Python multiprocessing's
  local named-pipe access (`WinError 5` in `multiprocessing.connection.Pipe`),
  including downstream web responses after that error. The permission tool
  could not represent the pipe namespace. Real process execution is unverified;
  this is not a clean full-suite pass.
- Supplemental full integration run substituting the controlled executor test
  double from `tests/test_pdf_workers.py`: **219 passed, 1 deselected, 1 warning**,
  32.30 seconds. This runs real rendering/saving sequentially behind a simulated
  out-of-order completion scheduler; it does not prove multiprocessing startup,
  concurrency, or speedup. Only the explicit spawned-worker test was deselected.
- All **48 Python files** passed in-memory compilation. Direct extraction and
  cache lookup helpers were checked against the uploaded baseline and unchanged.

Before production use, rerun `python -B -m pytest -q -p no:cacheprovider` outside
the restricted sandbox, then measure a real PDF import. No parallel performance
benchmark or production deployment was possible in this environment. The existing
Starlette/httpx deprecation warning remains. Earlier checkpoint sections below
are historical; their test counts predate this PDF work.

## Current checkpoint (2026-09-13)

**Milestone 45 plus completed post-handoff maintenance.** There is no new numbered
milestone or package version bump; pyproject.toml still reports 0.1.0. The original
Milestone 45 regression baseline was 182 tests. The current repository passes
**201 tests**. Use this working tree, not an older Milestone 45 archive.

Git base at handoff: branch `main`, HEAD `5cd7299` (Performance improvements for
thumbnail generation). Session code, tests and documentation are uncommitted;
some essential new files are untracked. No commit, deployment or ZIP creation was
performed by the handoff step. Include all working-tree files when packaging.

This document is self-contained; no prior ChatGPT/Codex conversation is required.
AGENTS.md provides permanent instructions; ARCHITECTURE.md maps components;
DECISIONS.md records constraints and tradeoffs; README.md provides setup/logging.
TODO.md mirrors the outstanding requests, which are also recorded below.

## Completed functionality

- Private FastAPI/Jinja2/SQLite library with Argon2 local accounts, no signup,
  CSRF, login throttling, session-version checks and optional HTTPS-only cookies.
- Authors, recursive series/sub-series, direct issues alongside child series,
  non-contiguous issue numbers, issue/series extras, search and aggregate counts.
- Image/MP4 reader with single-page/vertical modes, navigation, preferences,
  per-user progress, resume and issue-level Mark unread.
- Audited editing, reordering, soft media removal/restoration, recursive managed
  series and individual issue deletion with admin/CSRF/confirmation checks, and maintenance reports for gaps,
  missing files/thumbnails, empty groups, incomplete issues and duplicates.
- Server-folder import and browser folder upload, bulk artist import, JPG/JPEG/
  PNG/GIF/MP4/PDF input, direct PDF image extraction with JPEG-render fallback, editable virtual workspace, synthetic
  nodes, reparent/order/target/ignore operations, validation and staged commit.
- Raw streamed uploads with restart recovery, periodic checkpoints, 16 GiB/file,
  4 MiB write buffer, browser concurrency six, and an upload-session cancel route.
- Recoverable import/bulk/upload state with 12-hour inactivity cleanup; managed
  ID-based copies, duplicate fingerprints, staging-ID protection and web receipts.
- Progress/memory diagnostics and configurable rotating application/server logs.

## Work completed during the Codex session

1. Independently reviewed documentation/source/tests. Initial test execution was
   blocked by missing pytest/httpx. After environment setup, all 182 tests ran:
   172 passed and ten failed due to Windows path formatting in test assertions.
2. Corrected scanner/review/nested-series test path comparisons without changing
   production path semantics. Restored the 182-pass baseline.
3. Fixed missing administrator enforcement for series deletion by restricting all
   `/series/` POSTs. Added regular-user viewing/deletion-denial coverage and
   strengthened admin deletion, CSRF and original-file preservation checks.
4. Centralized ordinary thumbnail exception handling with warning logs and False
   return; used atomic sibling temporary-file replacement and best-effort cleanup.
   Added nine test cases for commit survival, rebuild/on-demand recovery, existing
   derivative preservation and process-control exceptions. Kept JPEG downsampling.
5. Replaced additive upload reconciliation with an authoritative completed-disk
   set, assigned only after successful enumeration. Added three recovery tests
   covering missing files, empty sets, uncheckpointed files, `.part` exclusion,
   re-upload/finalization and directory-read errors.
6. Added `logging_config.py`: application/scanner/PDF/thumbnail and CLI-launched
   Uvicorn logs share a configurable rotating file. Added CLI/API options, UTC
   timestamps, error mirroring, resilient fallback, Git log exclusions and six
   logging tests. Configuration moved out of diagnostics import-time setup.
7. Updated permanent documentation and recorded five new user requests. This
   final handoff changed documentation only; it did not start any planned feature.

No schema migration, dependency constraint change or production deployment was
part of these code changes. Dependencies were installed in the local environment
by the user; the repository has minimum versions, not a reproducible lockfile.

## Verification at this handoff

Command run from the repository root on Windows:

```text
python -B -m pytest -q -p no:cacheprovider
```

**201 passed, 0 failed, 0 skipped, 0 collection errors; 1 warning; 32.69 seconds.**
Accounting: 84 non-web tests (including six logging cases and eleven thumbnail
cases) plus 117 web tests. The monolithic process exited normally. All 47 Python
source/test files also passed in-memory compilation. No bytecode or pytest cache
was needed for these checks. There are no test-failure details to report.

Observed runtime: Python 3.10.11, pytest 9.1.1, FastAPI 0.141.1, Starlette 1.6.0,
httpx 0.28.1, Uvicorn 0.52.4, Pillow 10.0.0, PyMuPDF 1.28.2.
The warning is StarletteDeprecationWarning: its TestClient httpx fallback is
deprecated in favor of httpx2. The existing dev extra declares httpx>=0.27;
no dependency change was made just to silence this warning.

These tests validate server behavior and selected template content, not full
browser JavaScript interaction. No fresh Linux/production run, live Caddy/systemd
verification or large-real-import benchmark was performed in this session.

## Recommended next development task

**Reproduce and fix the validation button remaining locked after an error.**
The user reports clicking "Validate and continue to confirmation" with an error
leaves it disabled until refresh. Edits reportedly survive. This is a known
reported UI bug, not a reproduced automated/browser diagnosis yet.

Inspect `templates/base.html` (generic processing-form submit/progress lock),
`templates/import_workspace.html` (async metadata flush/finalize handler), and
`routes/import_workspace.py` (validation/error rendering). The generic handler
sets the submitting flag and disables controls; the async workspace handler can
prevent submission and only alert on failure. That interaction is a plausible
lead, not a confirmed root cause. Reproduce in a real browser, preserve duplicate
submission protection and saved workspace data, and verify correction/retry
without refresh. Do not claim TestClient alone executes the JavaScript.

## Outstanding requests and partial coverage

No new requested feature below is partly implemented during this session. Some
older functionality overlaps the request and should be reused deliberately.

1. **Confirmed cancellation with immediate cleanup for uploads/imports.** An
   upload-session cancel endpoint already exists; full scan/workspace/staging/
   commit cancellation with coordinated worker shutdown does not. Stop workers
   before cleanup so they cannot recreate files. Preserve original sources and
   committed content. **Open product choice:** for bulk, cancel current series,
   all remaining series, or provide both. Do not decide by silently deleting a
   whole batch. Interrupted commit/receipt recovery needs explicit treatment.
2. **Manual read status.** Add a direct Mark read action for comics read elsewhere.
   Issue-level Mark unread already exists in `templates/issue.html` and
   `/progress/{issue_id}/reset` for read/in-progress issues. Confirm desired
   issue/whole-series controls; preserve per-user state and aggregate semantics.
3. **Hide or remove server-filesystem import from the web UI.** It is still visible
   and implemented. Decide UI-only hiding versus removing web routes; CLI removal
   is not approved. Preserve existing paths until implementation scope is agreed.
4. **Admin panel for in-progress imports and forced expiration/cleanup regardless
   of status.** Not implemented. Requires visibility into durable/runtime state,
   admin+CSRF protections, coordinated worker stopping and safe temporary-file
   cleanup. Combine shared lifecycle work with cancellation where appropriate.
5. **Validation-button lockup.** Recommended first task, as described above.

## Remaining technical limitations and performance work

- Large real import validation remains the next **performance** task. Capture the
  configured log and rotated backups for upload, scan, workspace, staging,
  hashing, copying, thumbnailing and finalizing. Optimize measured bottlenecks.
- `library.read_library()` builds the whole hierarchy with nested queries (N+1).
  Targeted queries and measured indexes are future work, after real-scale evidence.
- Per-file HTTP overhead remains for tiny-file-heavy uploads. Legacy multipart
  paths remain; do not route large uploads back through multipart parsing.
- Restoring imports rescans sources; persisted scan indexes are not implemented.
  Bulk discovery still recursively scans candidate folders independently.
- Upload reconciliation is restoration-time only. External deletion during an
  active session is not continuously detected. An unreadable recovery scan uses
  the existing failed-restoration/404 path; no special recovery-error UI exists.
- Single-process deployment is assumed. Sessions/progress and file-log selection
  are process-wide; concurrent multi-worker mutation/rotation is unsupported.
- Logs rotate at 10 MiB with five backups, so a long run can span files or exceed
  retained history. Startup log-open failure uses stderr until reconfiguration;
  runtime write failures retry on later records. Memory metrics require Linux
  `/proc/self/status`; Windows reports unknown. External Uvicorn launchers need
  their own server-log configuration. CLI output and arbitrary third-party root
  loggers are not redirected wholesale.
- Atomic thumbnail writes do not validate existing JPEGs or guarantee cleanup
  after hard process termination. Cleanup failure is logged; ordinary decode/
  write failure is best-effort. Disk-full source-copy/DB errors still fail import.
- Historical TestClient teardown stalls were documented on older environments;
  current full Windows runs finish. Use complete disjoint shards only if needed,
  and investigate isolated failures before blaming that history.
- Schema migration remains ad hoc SQL/backfills/triggers. No formal migration
  framework. No new migration was required by this session.

## Historical performance work to preserve

Milestone 42 completed the route/service refactor (web.py roughly 4,189 to 350
lines). M43 removed workspace recursive rewalking and media-times-node ownership:
synthetic 5,000 pages/50 folders scanned in about 0.22s and built workspace in
about 1.53s. M44 cached immutable scan membership/automatic ownership and staged
ancestry: synthetic 400 issues/1,200 pages cache rebuild about 3.06s to 0.053s.
M45 JPEG decoder downsampling improved a synthetic 300-page 1200x1800 JPEG commit
about 11.0s to 3.3s. These historical synthetic figures were not rerun here and
are not production guarantees. Parallel thumbnails were deliberately deferred
because of peak-memory risk. Richer tagging/downloads are later work; CBZ is not
currently required and general conversion of non-PDF originals is not desired.

## Deployment and packaging

Known intended URL: https://comics.super-original.net. Intended topology:
Caddy HTTPS -> one Uvicorn process -> SQLite plus local library/staging storage.
Use secure cookies behind HTTPS. Current systemd/Caddy files are not in this
repository, and the live service version/configuration has not been verified.

README Diagnostics documents new `--log-file`/`--log-level` options and the default
log beside the database. Ensure that directory is writable by the service account.
No migration or additional dependency is required for logging.

For a standalone ChatGPT handoff ZIP, include all five permanent docs, TODO.md,
source/templates, tests, pyproject.toml, .gitignore and all untracked new source
files. Do not use `git archive HEAD` as the deliverable: it excludes this session's
uncommitted work. Exclude private runtime databases, uploads/library/staging,
logs, environment files, virtual environments and caches. The prior convention
of omitting Markdown from update ZIPs is explicitly inapplicable to this handoff.
