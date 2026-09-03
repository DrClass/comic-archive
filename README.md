# Comic Archive — Milestone 4: Commit layer

Current importer workflow:

1. `scan` — read-only folder scan.
2. `review` — correct scanner classifications.
3. `stage` — save reviewed metadata to a JSON staging record.
4. `commit` — validate the staging record, copy media into normalized storage, and create/update SQLite records.

Supported media currently: `.jpg`, `.jpeg`, `.png`, `.gif`, `.mp4`.

The commit step **copies** files. It does not move, rename, convert, delete, or otherwise modify source archive files.

## Scan

```bash
python -m comic_archive.cli scan "/path/to/comic/folder"
```

## Review

```bash
python -m comic_archive.cli review "/path/to/comic/folder"
```

## Stage

```bash
python -m comic_archive.cli stage "/path/to/comic/folder" --output ./staging/import.json
```

## Commit

```bash
python -m comic_archive.cli commit ./staging/import.json \
  --library ./library \
  --database ./comic_archive.sqlite3
```

Before copying, commit verifies that every staged source file still exists and has the same byte size it had during scanning. The same staging record cannot be committed twice.

Storage uses stable IDs rather than artist/series names:

```text
library/
└── series/
    └── <series-id>/
        └── groups/
            └── <group-id>/
                ├── 000001.jpg
                ├── 000002.png
                └── ...
```

Display names and relationships live in SQLite. Separate extras such as `Textless`, `Extra Angles`, and `Covers` remain separate content-group records.

## Test

```bash
python -m pytest
```

## Current milestone commands

Inspect committed data:

```bash
python -m comic_archive.cli library --database ./comic_archive.sqlite3
```

Commit duplicate protection checks both exact content hashes and matching author/series/issue metadata. To deliberately accept a flagged import:

```bash
python -m comic_archive.cli commit ./staging/import.json --library ./library --database ./comic_archive.sqlite3 --allow-duplicate
```

`--allow-duplicate` does not disable source validation or the guard against committing the exact same staging record twice.

## Editing committed imports

Show IDs needed for command-line edits:

```bash
python -m comic_archive.cli library --database ./comic_archive.sqlite3 --ids
```

Examples:

```bash
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 author AUTHOR_ID --name "Correct Artist"
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 series SERIES_ID --title "Correct Series"
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 issue ISSUE_ID --number "7.5" --title "Special" --complete yes
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 group GROUP_ID --name "Textless"
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 move-extra GROUP_ID --series-id SERIES_ID
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 move-extra GROUP_ID --series-id SERIES_ID --issue-id ISSUE_ID
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 reorder GROUP_ID MEDIA_ID_2 MEDIA_ID_1 MEDIA_ID_3
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 remove-media MEDIA_ID
python -m comic_archive.cli edit --database ./comic_archive.sqlite3 restore-media MEDIA_ID
```

Inspect edit history:

```bash
python -m comic_archive.cli history --database ./comic_archive.sqlite3
python -m comic_archive.cli history --database ./comic_archive.sqlite3 --entity-id ISSUE_ID
```

Media removal is soft: the media record becomes inactive and normal library readback hides it, but the copied file is not deleted. It can be restored later.

## Read-only web GUI

Install web dependencies:

```bash
pip install -e '.[web]'
```

Run the web library:

```bash
python -m comic_archive.cli serve --database ./comic_archive.sqlite3 --library ./library
```

Then open `http://127.0.0.1:8000/` in a browser.

This first GUI is read-only and provides Authors -> Series -> Issues/Extras navigation. Importing, editing, authentication, media serving, thumbnails, and the comic reader are intentionally not exposed through the browser yet.

## Milestone 8: first horizontal reader

Run the web server as before:

```powershell
python -m comic_archive.cli serve --database ./comic_archive.sqlite3 --library ./library
```

Open an issue and use **Read issue**. The first reader supports:

- one media item at a time
- JPEG/PNG/GIF images through the browser's native image support
- MP4 through the browser's native video player, including audio
- click left/right side to navigate
- Left/Right arrow keys
- Home/End for first/last media item
- page position in the URL (`?page=N`)
- media served by database ID rather than directly exposing stored paths

This milestone intentionally does not yet include thumbnails, preloading, vertical mode, reader preferences, or authentication.


## Authentication

There is no public account registration.

Before starting the authenticated web app for the first time, create the first administrator from the command line:

```powershell
python -m comic_archive.cli user-add your-admin-name --admin --database ./comic_archive.sqlite3
```

You will be prompted for the password without echoing it to the terminal.

Then start the server normally:

```powershell
python -m comic_archive.cli serve --database ./comic_archive.sqlite3 --library ./library --staging ./staging
```

When serving through HTTPS (for example through Caddy), add `--secure-cookies` so the session cookie is HTTPS-only:

```powershell
python -m comic_archive.cli serve --database ./comic_archive.sqlite3 --library ./library --staging ./staging --secure-cookies
```

After logging in as an administrator, use **Accounts** in the header to create additional admin or regular reader accounts. Regular accounts can browse and read but cannot import, edit, view history, or manage accounts.


## Security/account hardening

This milestone adds:

- CSRF protection for every state-changing browser form, including login/logout.
- User password changes with current-password verification.
- Administrator password resets.
- Account enable/disable controls.
- Administrator/reader role changes.
- Session-version invalidation so password resets/changes and disabling an account invalidate older sessions.
- Basic login throttling: five failed attempts for the same username/client combination within five minutes blocks further attempts temporarily.
- FastAPI docs/OpenAPI routes disabled.
- `itsdangerous` is now included in the `web` dependency set.

When serving through HTTPS/Caddy, continue to start Comic Archive with `--secure-cookies`.


## Thumbnail preview grids

Image thumbnails are derived cache files only; originals are never modified.

New imports generate thumbnails automatically. Existing libraries can be backfilled with:

```powershell
python -m comic_archive.cli thumbnails-build `
  --database ./comic_archive.sqlite3 `
  --library ./library
```

Still images and the first frame of animated GIFs receive a small JPEG thumbnail. MP4 files are not decoded for thumbnails in this milestone and appear as video placeholders in preview grids.

Issue and extra-content detail pages now show lazy-loaded thumbnail grids. Clicking a thumbnail opens the horizontal reader directly at that page/item.


## UI browsing redesign

This milestone changes the visual hierarchy of the library:

- Author pages show series as square, cropped preview cards.
- Series pages show issues as square, cropped preview cards.
- Series previews use the first usable image from the first issue.
- Issue previews use the first usable image from that issue's primary group.
- Issue pages have one main Read action instead of duplicate primary-group controls.
- Issue and series extras live in collapsed panels and link to dedicated extra-group pages.
- Extra-group pages have their own Read action and thumbnail grid.
- Comic page previews use a fixed width with natural image height/aspect ratio.
- Grid rows naturally take the height of the tallest preview in that row.
- Page/item numbers are small corner overlays; filenames are not shown in normal browsing.
- Admin edit links are visually secondary.
- Navigation cards use square `object-fit: cover` previews and neutral placeholders when no usable image exists.


## Issue editor media access

The issue editor now links directly to the primary comic page editor. From **Edit issue**, administrators can open **Edit comic pages** to reorder pages, remove pages, and restore removed pages. Primary-group edit screens use page-specific labels rather than generic content-group/media wording.


## Content regrouping

Administrators can correct grouping mistakes both before and after import.

### After import
- **Edit issue** can create a new issue-level extra group.
- **Edit comic pages** and extra-group editors show a Move checkbox for active files.
- Selected files can be moved between the main comic and any extra group in the same issue.
- Moving files changes database grouping/order only. Managed media files are not rewritten, deleted, or moved on disk.
- Source groups are compacted after a move and moved files are appended to the target group.
- Group creation and media moves are recorded in the audit log.

### During web import
After metadata, the importer now has an **Organize files** step before confirmation.
- Create new issue-level extra groups.
- Assign each individual scanned file to the main comic or any extra group.
- This works even when extras were mixed into the main issue folder and the scanner could not identify them automatically.
- Source files remain untouched.


## Library search

Authenticated users can search the archive from the header or `/search`.

Search currently covers:
- author names
- series titles
- issue numbers and issue titles
- extra-group names

Search is case-insensitive and supports partial matches. Results are grouped visually by type and link directly to the matching author, series, issue, or extra group. Original page/media filenames are deliberately not included in normal library search.


## Reading progress, status, and modified dates

Reading progress is stored per account and per issue.

- Opening/reading a primary issue records the current page.
- Issue and series pages show **Unread**, **In progress**, or **Finished**.
- In-progress issues resume at the saved page when **Continue reading** is used.
- Reaching the final page marks the issue finished.
- Finished issues can be read again from page 1.
- **Mark unread** clears the current user's progress for that issue.
- The home page shows a **Continue reading** section for the user's recently read unfinished issues.
- Extra groups do not affect the main issue's reading status.
- Progress is private to each account.

Issues and series now also have created/updated timestamps. The UI shows the last-modified date on issue pages, series pages, series cards, and issue cards. Changes to issue metadata, content groups, page grouping/order, or active media update the issue and its parent series. Series-level metadata/extras update the series timestamp. Existing databases are migrated automatically; where possible, older timestamps are initialized from import history.


## Reader improvements

The reader now supports:
- **Single page** mode (default).
- **Vertical scroll** mode for continuous reading.
- Fit controls: **Fit screen**, **Fit width**, **Fit height**, and **Original size**.
- Fullscreen mode using the browser Fullscreen API.
- Touch/swipe page navigation in single-page mode.
- A collapsible **Pages** thumbnail navigator for jumping directly to a page.
- Home/End and arrow-key navigation remain available.
- Reader mode and fit preference are stored locally in the browser and reused next time.
- In single-page mode, only the current full-resolution image is initially requested. The reader then loads/preloads the current, previous, and next pages as needed rather than eagerly requesting every full-resolution page.
- Vertical mode loads the issue content and updates reading progress based on the page currently visible.
- These controls also work when reading extras, except extras continue not to affect the issue's main reading-progress status.


## Missing issue awareness and maintenance

Numeric issue gaps are detected automatically within a series. For example, issues 1, 2, 4, and 5 produce a detected gap for issue 3. Non-numeric issue labels and one-shots are ignored by gap detection.

Admins can mark a detected gap as intentionally unavailable and optionally save a note. Intentional gaps remain visible on the series page but are distinguished from unresolved missing issues. They can be reopened later.

The admin-only **Maintenance** page performs read-only library health checks and reports:
- unresolved and intentional issue-number gaps
- incomplete issues and issues with unknown completeness
- active database media whose managed files are missing
- active image media whose thumbnails are missing
- empty content groups
- sets of issues with identical content fingerprints

The dashboard does not delete, move, repair, or rewrite managed library files automatically. Existing thumbnail repair continues to use the `thumbnails-build` command.


## Maintenance lock fix

Milestone 19.1 fixes a Windows SQLite `database is locked` error when opening the Maintenance page. The maintenance report now computes per-series issue gaps using the report's existing database connection instead of reopening/reinitializing SQLite once for every series during the scan.


## Bulk artist importer

The web importer now has a **Bulk artist import** workflow for an artist folder containing many comics.

- Choose an artist folder on the server machine.
- The importer discovers immediate child folders that contain supported media.
- Non-comic child folders with no supported media are ignored.
- Review the discovered list and select all, none, or any subset.
- The author defaults to the artist folder name but can be corrected before starting.
- Each selected comic then goes through the existing review → metadata → organize → confirm workflow independently.
- The comic folder name is used as the default series title.
- After a comic commits successfully, the completion page links directly to the next selected comic.
- The final batch page lists every imported series.
- Source files remain untouched throughout.
- Bulk import sessions are kept in memory just like the existing web import sessions, so restarting the server ends an unfinished batch.

The bulk importer intentionally does not auto-commit every discovered folder. This preserves the existing classification, duplicate-warning, organization, and confirmation safeguards for each comic.

## Header menu

Authenticated account/admin actions now live in a compact hamburger menu in the top-right of the header. Search remains directly accessible. Admins see Import, Maintenance, History, and Accounts in the menu; all users see Change password and Log out.


## Server import folder browser

The web importer now uses a server-side folder browser instead of asking administrators to type arbitrary filesystem paths.

Run the server with an explicit import root:

```powershell
python -m comic_archive.cli serve `
  --database ./comic_archive.sqlite3 `
  --library ./library `
  --staging ./staging `
  --import-root "G:\Comics\Incoming"
```

When deployed to a server, `--import-root` should point at the directory (or mounted volume) containing material available for import, for example `/srv/comic-imports`. The browser is restricted to that root and rejects attempts to navigate outside it.

Both normal and bulk artist importers use the same browser. The web UI stores and submits paths relative to the configured import root rather than exposing arbitrary server paths.

For direct Python `create_app(...)` use, if `import_root` is omitted it defaults to the database file's parent directory for backward compatibility. The CLI defaults `--import-root` to the current directory, but an explicit import root is recommended.

Single-comic imports now name their primary content group after the comic folder instead of `Primary content`. This makes the default review name match the source comic name while remaining editable during review.


## Browser folder uploads

The primary web-import workflow is now designed for a remote Comic Archive server that does **not** already have access to the user's source files.

### Normal import

Open **Import** and choose a comic folder from the computer running the web browser. The browser uploads the selected folder tree to the server, preserving relative paths. Comic Archive writes that upload into an isolated temporary directory under:

```text
<staging>/uploads/<upload-id>/content/
```

The existing scanner, review, metadata, file-organization, duplicate-check, and commit pipeline then runs against that temporary copy.

A successful single-comic commit removes its temporary upload directory. The original files on the user's computer are never modified, moved, renamed, or deleted.

### Bulk artist import

Open **Bulk artist import** and choose the artist folder from the browser's computer. The entire selected directory tree is uploaded once. Comic Archive discovers comic folders beneath the uploaded artist folder and then processes selected comics sequentially through the existing per-comic review workflow.

The shared temporary artist upload remains available while the batch is being processed and is removed after the final selected comic is successfully committed.

### Upload implementation

The folder picker uses the browser directory-selection capability and JavaScript submits each file using its relative path so the server can reconstruct the folder hierarchy.

Uploaded files are copied to staging in 1 MiB chunks. Starlette's multipart parser is configured to accept up to 100,000 file parts for large artist collections. `python-multipart` is now part of the `web` dependency set.

After updating, reinstall the web dependencies:

```powershell
pip install -e '.[web]'
```

### Optional server-side import

The previous server-folder browser has not been removed. It is now a secondary admin option under **Import a folder already on the server**. `--import-root` controls the filesystem area exposed to that optional browser.

It is not needed for normal remote ingestion. A deployed server can have an empty source filesystem and still ingest folders chosen from an administrator's local computer through the web interface.

### Deployment note

Large browser uploads can take significant time and bandwidth. When Comic Archive is later placed behind Caddy or another reverse proxy, proxy request/body/time-out settings should be checked so they do not prematurely terminate large collection uploads.


## Import upload cleanup and bulk skip

Temporary browser uploads now expire after **12 hours of import inactivity**.

Meaningful import activity refreshes the inactivity clock, including upload creation, bulk selection/progression, review changes, metadata submission, organization changes, duplicate-confirmation attempts, and skipping to the next bulk comic. Merely viewing an import page does not keep an abandoned upload alive.

Comic Archive performs an opportunistic stale-upload sweep during web requests, no more than once per hour. Active in-memory imports are removed when expired, their temporary upload directories are deleted, and orphaned directories under `staging/uploads` are also deleted when their modification time is older than 12 hours. This orphan sweep means old upload data is still cleaned after a server restart even though in-memory import sessions do not survive the restart.

Accessing a specific import session after its 12-hour inactivity limit also triggers immediate expiration/cleanup rather than extending it.

Bulk imports now support **Skip this comic and continue** on the confirmation page, including when duplicate detection blocks the current comic. Skipping:

- does not commit the current comic,
- removes its staging JSON if one exists,
- keeps the shared bulk upload available,
- advances directly to the next selected comic,
- records the skipped comic in the final bulk summary,
- and removes the shared upload when the last selected comic is either committed or skipped.

The skip action is available only inside a bulk import; normal single-comic imports cannot use it.


### Orphaned staging JSON cleanup

The 12-hour importer cleanup also removes stale top-level `staging/*.json` records that are no longer referenced by any active import session. Active staging JSON files are explicitly protected from the orphan sweep even if their file modification time is old.

This complements the existing cleanup of `staging/uploads/*`, so both abandoned uploaded media and abandoned staging metadata are reclaimed after the inactivity window.


## Resumable per-file browser uploads

The primary browser importer now uses an upload-session protocol instead of sending an entire selected folder in one multipart request.

Flow:

1. The browser creates an upload session with the selected file count and import mode (`single` or `bulk`).
2. Files are uploaded individually while preserving each `webkitRelativePath`.
3. The browser runs up to **3 file uploads concurrently**.
4. A failed file is retried automatically up to **3 attempts** with short exponential backoff.
5. Successfully received relative paths are idempotent within the server upload session, so retrying the same completed file does not increase the received count or create a duplicate.
6. If some files still fail after automatic retries, the UI changes to **Resume upload**. Retrying sends only the files that have not completed in that browser session.
7. Finalization is rejected until the server has received exactly the expected number of files.
8. Finalization hands the completed temporary directory to the existing scan/review/metadata/organize/confirm/commit pipeline.
9. A **Cancel upload** action removes the unfinished upload session and its temporary directory.

Upload-session activity participates in the existing 12-hour inactivity cleanup. Unfinished upload sessions that go idle expire and are removed; the orphan directory sweep remains the fallback after a server restart.

This is file-level resumability, not byte-range resumability inside one file. If a single file fails halfway through, that file is retransmitted from the beginning, but already completed files in the folder do not need to be resent.

The previous whole-folder `/import/upload` and `/import/bulk/upload` endpoints remain for backward compatibility, but the normal web interface no longer uses them.

For reverse-proxy deployment, request-body limits now generally need to accommodate the **largest individual file** being imported rather than an entire artist folder in one request.

## Milestone 22.1 — production login CSRF fix

Fixed a production-only login failure exposed by browser background requests such as `/favicon.ico`. Anonymous requests to protected paths now preserve the existing session CSRF token instead of clearing the session and generating a new token while the login form is open. Invalid/stale authenticated sessions still have authentication state cleared, but their existing CSRF token is preserved when possible. `/favicon.ico` now returns HTTP 204 without entering the authentication redirect flow. Regression coverage includes secure cookies over an HTTPS TestClient, favicon access between login GET and POST, and anonymous protected requests preserving the login CSRF token.


## Milestone 23 improvements

- Author series cards now list the primary page count for every issue.
- Series have independent completeness metadata: Unknown, Complete, or Incomplete. It can be set during web import, in the series editor, or with `edit series --complete`.
- Active import/review pages send a CSRF-protected keepalive every two minutes while visible, and normal importer page navigation also refreshes activity. The existing 12-hour cleanup now applies to genuinely inactive sessions. Server process restarts still clear in-memory import sessions.
- Extra-like folders nested beneath neutral page/container directories are preserved as extras instead of being flattened into primary comic pages.
- Organizer Create/Remove group actions first save all current file-to-group selections, so their page reload no longer discards pending moves.
- The organizer blocks continuation when any content group is empty, identifies the empty group, and allows empty extra groups to be removed. Commit validation also rejects empty groups as a final safeguard.

## Milestone 23.1 — Series card summaries

Author-page series cards now show a compact five-line summary: series title, issue count, series completeness, modified date, and aggregate page/extra counts. Page totals sum active media in primary groups across all issues. Extra totals sum active media in issue-level extra groups plus series-level extra groups.

## Milestone 23.2 — nested issue extras detection
- Series issue folders now preserve distinct child content folders instead of silently flattening them into the issue's primary pages when the issue already has direct page files.
- Extra-like folders such as `issue 1/extras/` are always staged as issue-level extra groups.
- Conventional primary page containers such as `Pages/`, `Images/`, `Main/`, and `Primary/` still fold into the main comic.
- Existing one-shot import behavior remains unchanged for arbitrary nested page folders.
- Added regression coverage for multi-issue series with nested extras and end-to-end staging preservation.

### PDF import

PDF files are accepted as importer input. Each PDF page is rendered at 150 DPI to an ordered PNG (`0001.png`, `0002.png`, ...), then follows the normal image import path. The source PDF is never modified. Rendered temporary pages are cleaned with their web import session, while the managed library stores ordinary PNG files. PDF rendering uses PyMuPDF, included in the `web` optional dependencies.

## Milestone 25 — Manual issue ordering

- Issues now have a persistent per-series `sort_order`.
- Existing databases automatically preserve their previous displayed issue order during migration.
- Import metadata includes an Order field for every issue; the chosen relative order is preserved on commit.
- Existing series can be reordered from the Edit series page using numeric order fields. Values are normalized on save.
- Series pages and issue-selection helpers consistently use the stored manual order before legacy label/title fallback.
- Reordering is audited and updates modified timestamps.

## Milestone 26: pre-flatten folder classification

The web import review now exposes media-bearing folders that are currently included in primary content but were not recognized as extras. Admins can click **Mark folder as extras** before staging. The importer rescans the untouched source with an explicit extra-folder override, preserving that folder as a real extra group instead of requiring page-by-page regrouping later.

This works for one-shot comics and for folders nested inside detected series issues, including structures such as `Issue A/Pages/...` plus `Issue A/Gallery/...` where `Gallery` is bonus material but has no recognized extra keyword.
