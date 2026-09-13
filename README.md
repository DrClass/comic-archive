# Comic Archive

Comic Archive is a private, account-gated web application for importing,
organizing, browsing, and reading a personal digital comic collection.

The project is intentionally lightweight: Python, FastAPI, Jinja2,
SQLite, and a managed filesystem library. It is designed for collections
that arrive in inconsistent folder structures and may contain normal
image pages, PDFs, animations, videos, extras, nested sub-series,
missing issue numbers, and incomplete material.

## Current project state

The current development baseline is **Milestone 45 plus post-handoff maintenance
(2026-09-13)**. The working tree includes security, recovery, thumbnail reliability
and file-logging changes beyond the original Milestone 45 package.

Milestone 42 completed the major structural refactor. Feature routes and
importer services were moved out of the former monolithic `web.py`.
Milestones 43-45 then began substantive performance work.

Current automated regression count: **201 passed, 0 failed, 0 skipped** on
Windows/Python 3.10. The original Milestone 45 baseline was 182 tests.

The latest known large-comic improvements are:

-   workspace construction no longer recursively re-walks the filesystem
    after scanning;
-   automatic ownership resolution no longer performs media x
    workspace-node work;
-   immutable scanner folder/media membership is indexed once and
    reused;
-   automatic page ownership is cached separately from explicit page
    moves;
-   page-move destination validation avoids quadratic workspace
    rebuilding;
-   staged-import construction precomputes semantic ancestry;
-   JPEG thumbnail generation uses decoder-level downsampling before
    final resize.

A synthetic 400-issue / 1,200-page ownership-cache test improved from
roughly 3 seconds per build/rebuild to roughly 0.05-0.06 seconds. A
synthetic 300-page large-JPEG commit improved from roughly 11 seconds to
roughly 3.3 seconds. These are development benchmarks, not production
guarantees.

The recommended next development task is the reported validation-button lockup
after an error. The next performance-validation task remains a large real import
with captured logs and phase timings. See PROJECT_STATUS.md for unresolved work.

## Technology

-   Python 3.10+
-   FastAPI
-   Jinja2
-   SQLite
-   Pillow
-   PyMuPDF for PDF page rendering
-   Argon2 password hashing
-   Uvicorn
-   Caddy in production

The package metadata and optional dependencies are defined in
`pyproject.toml`.

For development/web dependencies:

``` bash
python -m pip install -e '.[web,dev]'
```

## Running locally

Create an administrator account:

``` bash
comic-import user-add admin --admin --database ./comic_archive.sqlite3
```

Start the application:

``` bash
comic-import serve \
  --database ./comic_archive.sqlite3 \
  --library ./library \
  --staging ./staging \
  --import-root /path/to/import/source \
  --host 127.0.0.1 \
  --port 8000
```

When running behind HTTPS/Caddy, add:

``` bash
--secure-cookies
```

There is no public signup flow.

## Production deployment

The established production URL is:

`https://comics.super-original.net`

The intended production arrangement is:

``` text
Internet
  -> Caddy / HTTPS
  -> one Uvicorn Comic Archive process
  -> SQLite + managed library filesystem
```

The application should normally bind to localhost behind Caddy.
Production should use `--secure-cookies`.

## Library model

The logical hierarchy is:

``` text
Author
  -> Series
       -> direct Issues
       -> child Sub-Series
            -> Issues
```

A series may contain **direct issues and child sub-series at the same
time**.

Sub-series are represented recursively through
`series.parent_series_id`.

Issue numbers do not need to be continuous. Missing numeric issues can
be marked intentional and optionally given a note.

Content groups can represent primary pages, issue extras, or series
extras.

## Supported importer media

The scanner accepts:

-   `.jpg`
-   `.jpeg`
-   `.png`
-   `.gif`
-   `.mp4`
-   `.pdf`

PDF is an importer input format only. PDF pages are rendered to high-quality JPEG for
the managed library; the source PDF is not modified.

The importer does not currently require CBZ support and does not convert
normal image originals.

## Managed storage

Imported source material is never moved or deleted by the normal import
process.

Committed media is copied into ID-based managed storage resembling:

``` text
library/
  series/
    <series-id>/
      groups/
        <group-id>/
          000001.jpg
          000002.jpg
          ...
          _thumbs/
            <media-id>.jpg
```

This deliberately separates logical metadata from original source folder
naming.

Temporary browser uploads and recoverable import state live under the
configured staging directory.

## Import workflow

The web importer is designed around this conceptual pipeline:

``` text
Source/upload
  -> scanner
  -> editable workspace
  -> staged import
  -> commit
  -> managed library + SQLite
```

Important characteristics:

-   raw browser uploads are streamed rather than held as multipart
    bodies in memory;
-   browser upload sessions are restart-safe and periodically
    checkpointed;
-   scan work runs off the main request path and reports progress;
-   workspace classification is editable without modifying source files;
-   virtual issues/sub-series/groups can be created;
-   media can be reassigned, ignored, restored, and reordered;
-   validation builds a staged import before commit;
-   commit is designed to be idempotent/recoverable;
-   duplicate content is detected using ordered media SHA-256
    fingerprints;
-   commit progress includes copying and thumbnail generation;
-   ordinary thumbnail failures are logged without aborting valid imports;
    derivatives are published atomically and can be retried;
-   upload recovery drops missing completed files and rejects directory-read
    errors rather than accepting an incomplete filesystem scan.

## Workspace roles

The workspace supports roles including:

-   Series
-   Sub-Series
-   Issue
-   Issue-Extras
-   Series-Extras
-   Primary Pages
-   Container
-   Unassigned
-   Ignore

Root-level and nested structures are supported. Missing issue numbers
are allowed.

Bonus content may exist at both issue and series level.

## Reading

The web reader supports image pages and MP4 media.

Implemented reader/library behavior includes:

-   single-page and vertical reading modes;
-   fit/fullscreen controls;
-   keyboard and swipe navigation;
-   thumbnail navigation;
-   lazy adjacent loading;
-   per-user issue reading progress;
-   Unread / In progress / Finished state;
-   resume unfinished issues;
-   finishing on the final page;
-   Read again for completed issues;
-   Mark unread on issue pages with existing progress;
-   recursive series progress aggregation.

Extras do not affect primary issue completion.

## Authentication and security

-   local accounts only;
-   Argon2 password hashing;
-   no public signup;
-   administrator-only import, maintenance, editing, and account
    management;
-   CSRF protection on POST actions;
-   login throttling;
-   persistent session secret;
-   session invalidation through `session_version`;
-   SameSite=Lax cookies;
-   optional HTTPS-only cookies for production.

## Editing and maintenance

The application supports:

-   author rename;
-   series and issue editing;
-   nested series movement/ordering;
-   extra-group rename/move;
-   media reorder;
-   soft media remove/restore;
-   issue-extra creation;
-   audit history;
-   recursive series deletion and individual issue deletion of managed data;
-   missing-file/thumbnail checks;
-   empty-group checks;
-   incomplete-issue checks;
-   duplicate-fingerprint checks;
-   numeric issue-gap reporting and intentional-gap tracking.

Source import folders are not deleted by library editing/deletion
operations.

## Code layout

The major structural refactor is complete. Important modules are now
organized roughly as:

``` text
comic_archive/
  web.py
  database.py
  auth.py
  library.py
  library_views.py
  editing.py
  maintenance.py
  progress.py
  thumbnails.py
  web_forms.py
  logging_config.py

  routes/
    auth.py
    library.py
    editing.py
    maintenance.py
    media.py
    import_uploads.py
    import_review.py
    import_workspace.py
    import_finalize.py

  services/
    diagnostics.py
    uploads.py
    import_sessions.py
    import_orchestration.py
    workspace.py

  importer/
    scanner.py
    models.py
    review.py
    staging.py
    commit.py
    bulk.py
    sorting.py
```

`web.py` is now primarily application construction, middleware, state
initialization, and dependency wiring rather than the implementation
home for every feature.

## Diagnostics

Application diagnostics and Uvicorn logs (when launched with `comic-import serve`)
are written to `logs/comic-archive.log` beside the configured database. Commands
without a database option use `./logs/comic-archive.log` in the working directory.

Override the destination and verbosity with:

``` bash
comic-import serve --database ./comic_archive.sqlite3 --log-file /var/log/comic-archive/comic-archive.log --log-level INFO
```

The service account needs write permission to the log directory. Direct Python
callers can pass `log_file=` and `log_level=` to `create_app()`. CLI log options
follow the command name; for `edit`, put them before its nested subcommand.
Use DEBUG temporarily for investigation; INFO is the default.

Records include UTC timestamps, severity, logger name, and `CA_DIAG`, plus
session identifiers where supplied by the operation. Scanner/PDF, uploads,
workspace, staging, commit and thumbnail messages share the same file.
CLI command results still print normally to the terminal.

Files rotate at 10 MiB with five backups (`comic-archive.log.1` through `.5`).
Copy the current file and any backups covering the import when sharing a run.
Logs can contain source paths and comic metadata. Errors also go to stderr for
systemd/journald. If opening, writing or rotating the file fails, logging falls
back to stderr without aborting application work. A startup open failure uses
stderr until restart/reconfiguration; runtime write failures are retried on
subsequent records.

Logging is process-wide: the most recently configured application selects the
destination. This follows the supported single-process deployment. External
Uvicorn launchers must configure their own server logging; the application
file configuration still captures Comic Archive messages.

Memory checkpoints include RSS, anonymous/file-backed memory, virtual size and
thread count where `/proc/self/status` is available. Windows reports these as
unknown. No logs are served through the web UI.

## Testing

Run the normal test suite with:

``` bash
python -B -m pytest -q -p no:cacheprovider
```

A long-lived TestClient teardown stall has occasionally occurred in the
development container even against an unchanged baseline. Recent
milestones therefore also validated the complete suite in independent
shards rather than treating that environmental stall as an application
failure.

The current complete suite has **201 passing tests** (84 non-web and 117 web).
One Starlette/httpx deprecation warning remains; no tests are skipped. The original
Milestone 45 run had 182 tests. Current Windows runs finish without teardown stalls.

For future changes, preserve the pattern of:

1.  targeted tests for the changed subsystem;
2.  full regression coverage;
3.  compile check;
4.  clean package without Python cache artifacts.

Performance changes should add operation-count or behavioral regression
guards when possible instead of relying only on wall-clock timing.

## Current performance history

Several large-import bottlenecks have already been fixed:

1.  Multipart upload memory growth was replaced with raw request
    streaming.
2.  Raw-upload throughput was improved with bounded off-event-loop disk
    writes, higher browser concurrency, and less-frequent state
    checkpoints.
3.  Scanner recursive filesystem/path work was replaced with an indexed
    single-pass scan.
4.  Duplicate ownership work during staging was removed.
5.  Large workspace folder selection was paginated and classification
    edits stopped forcing immediate global ownership rebuilds.
6.  Workspace tree construction stopped recursively rewalking the
    filesystem.
7.  Ownership resolution was changed from media x nodes to
    indexed/node-oriented resolution.
8.  Folder/media membership and automatic ownership are cached for cheap
    rebuilds and page moves.
9.  Staging ancestry lookups are precomputed.
10. JPEG thumbnail generation now performs decoder-level downsampling.

Do not assume the import path is fully optimized. The next large
real-world import should determine what remains slow.

## Known issues / next work

### Current known UI issue

"Validate and continue to confirmation" can remain disabled after an error until
refresh. The user reports edits are preserved. Reproducing/fixing this is the
recommended next development task. Full import cancellation, manual Mark read,
server-folder UI removal/hiding and an admin import-cleanup panel remain planned;
see PROJECT_STATUS.md. Browser upload cancellation and issue Mark unread already
exist, but do not implement all of those broader requests.

### Next performance-validation task

Run a large import using the current build and record where time is spent:

``` text
upload
-> scan
-> workspace
-> validation/staging
-> hashing
-> copying
-> thumbnailing
-> finalizing
```

If possible, capture `CA_DIAG` output during the run.

Do not optimize a phase merely because it looks theoretically expensive;
use the production run to identify the next dominant cost.

### Known architectural follow-ups

Potential future work includes:

-   targeted database/library queries instead of reading large portions
    of the hierarchy for some pages;
-   SQLite indexing/query review after measuring real library scale;
-   reducing the large dependency-wiring section in `create_app()` if it
    becomes cumbersome;
-   removing or formally deprecating old CLI/legacy multipart paths when
    no longer needed;
-   reviewing restored-session rescanning behavior for very large
    sources;
-   reviewing bulk discovery separately from normal single-folder
    scanning;
-   considering carefully bounded thumbnail parallelism only if commit
    CPU remains dominant and memory measurements show adequate headroom.

Avoid aggressive thumbnail parallelism until memory behavior is measured
on a large real import.

## Important invariants for future development

Please preserve these unless a deliberate product decision changes them:

-   Source files are not modified, moved, or deleted by import.
-   PDF input is rendered to high-quality JPEG; normal image originals are preserved.
-   Issues and sub-series may coexist under the same series.
-   Missing issue numbers are valid.
-   Series-level and issue-level bonus content are both valid.
-   Explicit workspace media assignments override automatic ownership.
-   Import/recovery actions should remain idempotent where possible.
-   Browser uploads should remain restart-safe.
-   The app is private/default-deny; import/edit/maintenance actions are
    admin-only.
-   Reading progress is per user.
-   Extras do not determine issue completion.
-   Performance fixes should not silently change
    classification/ownership semantics.

## Packaging and standalone handoff

Include AGENTS.md, ARCHITECTURE.md, PROJECT_STATUS.md, DECISIONS.md, README.md,
TODO.md, source/templates, tests, pyproject.toml and .gitignore in a complete source
ZIP. Include new/untracked files, especially logging_config.py and test_logging.py.
The old convention of excluding Markdown from milestone update ZIPs must not be
used for a complete handoff. Exclude databases/accounts/session secrets, runtime
library/staging/uploads, logs, caches, virtual environments and private .env files.

This checkpoint has uncommitted work. Package the working tree, not only HEAD;
`git archive HEAD` would omit current changes. No ZIP is generated automatically.

To continue in a new conversation, read the five permanent documents, inspect the
relevant source/tests, install the declared web/dev extras in your environment,
and reproduce the regression baseline. PROJECT_STATUS.md contains the complete
outstanding requests and open choices, including those also listed in TODO.md.
Do not assume a source handoff implies a production deployment or authorize
large real imports or destructive cleanup without the user's task scope.
