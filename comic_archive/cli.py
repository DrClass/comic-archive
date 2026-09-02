from __future__ import annotations

import argparse
from pathlib import Path

from .importer.review import ReviewError, ReviewRole, ReviewPlan, build_review_plan
from .importer.staging import StagingError, build_staged_import
from .importer.scanner import FolderScanError, scan_folder
from .importer.commit import CommitError, commit_staged_import, load_staged_import
from .library import read_library
from .thumbnails import build_missing_thumbnails
from .auth import AuthError, create_user
from .editing import (
    EditError, edit_issue, edit_series, get_history, move_extra_group,
    rename_author, rename_group, reorder_media, set_media_active,
)


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _print_group(group, indent: str = "") -> None:
    for item in group.media:
        print(f"{indent}{item.order:>4}  {item.relative_path}  ({item.mime_type}, {_format_size(item.size_bytes)})")


def print_scan(
    path: str | Path,
    *,
    extra_folders: list[str] | None = None,
    primary_folders: list[str] | None = None,
) -> None:
    result = scan_folder(
        path,
        extra_folders=extra_folders,
        primary_folders=primary_folders,
    )
    print(f"Source:       {result.source}")
    print(f"Content root: {result.content_root}")
    print(f"Media files:  {result.media_count}")

    if result.is_series_candidate:
        print("\nSeries candidate:")
        for issue in result.issues:
            print(f"\n  Issue candidate [{issue.name}]")
            if issue.primary:
                _print_group(issue.primary, "    ")
            else:
                print("    Primary candidate: none")
            if issue.extras:
                print("    Issue extra groups:")
                for group in issue.extras:
                    print(f"      [{group.name}]")
                    _print_group(group, "        ")
    elif result.primary:
        print("\nPrimary candidate:")
        _print_group(result.primary, "  ")
    else:
        print("\nPrimary candidate: none")

    if result.extras:
        label = "Series extra groups" if result.is_series_candidate else "Candidate extra groups"
        print(f"\n{label}:")
        for group in result.extras:
            print(f"  [{group.name}]")
            _print_group(group, "    ")
    else:
        label = "Series extra groups" if result.is_series_candidate else "Candidate extra groups"
        print(f"\n{label}: none")

    if result.ignored_files:
        print("\nIgnored files:")
        for item in result.ignored_files:
            print(f"  {item}")


def _role_choices(item, *, is_series: bool) -> list[ReviewRole]:
    if item.source_kind == "issue":
        return [ReviewRole.ISSUE, ReviewRole.SERIES_EXTRA]
    if is_series and item.issue_path is None:
        # A root-level series group has no issue owner yet, so the CLI can
        # safely switch it only between series-extra and a new issue. The
        # future web review UI will support selecting an issue owner directly.
        return [ReviewRole.SERIES_EXTRA, ReviewRole.ISSUE]
    if is_series:
        return [ReviewRole.ISSUE_EXTRA, ReviewRole.PRIMARY, ReviewRole.SERIES_EXTRA]
    return [ReviewRole.ISSUE_EXTRA, ReviewRole.PRIMARY, ReviewRole.SERIES_EXTRA]


def _print_review(plan) -> None:
    print(f"Source:       {plan.scan.source}")
    print(f"Content root: {plan.scan.content_root}")
    print("\nClassification review:")
    for index, item in enumerate(plan.items, start=1):
        marker = " *" if item.changed else ""
        owner = f"; issue={item.issue_path}" if item.issue_path is not None and item.role is ReviewRole.ISSUE_EXTRA else ""
        display_name = f" [{item.name}]" if item.name and item.name != str(item.relative_path) else ""
        print(
            f"  {index:>2}. {item.relative_path}{display_name} -> {item.role.value} "
            f"({item.media_count} media{owner}){marker}"
        )
    print("\n* = changed from scanner suggestion")


def _review_loop(plan: ReviewPlan) -> ReviewPlan:
    while True:
        _print_review(plan)
        errors = plan.validation_errors()
        if errors:
            print("\nValidation:")
            for error in errors:
                print(f"  - {error}")

        answer = input("\nItem number to change role, 'n' to rename, 'r' to reset one, or Enter to finish: ").strip()
        if not answer:
            if errors:
                print("Cannot finish while validation errors remain.")
                continue
            return plan

        action = answer.casefold()
        reset = action == "r"
        rename = action == "n"
        if reset:
            target = input("Item number to reset: ").strip()
        elif rename:
            target = input("Item number to rename: ").strip()
        else:
            target = answer

        try:
            index = int(target)
            item = plan.items[index - 1]
        except (ValueError, IndexError):
            print("Invalid item number.")
            continue

        if reset:
            plan.reset(item.relative_path)
            continue
        if rename:
            new_name = input(f"New name [{item.name}]: ").strip()
            if new_name:
                try:
                    plan.set_name(item.relative_path, new_name)
                except ReviewError as exc:
                    print(f"Invalid name: {exc}")
            continue

        choices = _role_choices(item, is_series=plan.scan.is_series_candidate)
        print("Roles:")
        for choice_index, role in enumerate(choices, start=1):
            print(f"  {choice_index}. {role.value}")
        selected = input("Choose role: ").strip()
        try:
            selected_role = choices[int(selected) - 1]
            plan.set_role(item.relative_path, selected_role)
        except (ValueError, IndexError, ReviewError) as exc:
            print(f"Invalid role: {exc}")



def interactive_review(
    path: str | Path,
    *,
    extra_folders: list[str] | None = None,
    primary_folders: list[str] | None = None,
) -> ReviewPlan:
    scan = scan_folder(path, extra_folders=extra_folders, primary_folders=primary_folders)
    plan = _review_loop(build_review_plan(scan))
    print("\nReview complete. Nothing has been imported or modified.")
    return plan


def _prompt_optional(prompt: str, default: str | None = None) -> str | None:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    if value:
        return value
    return default


def _prompt_complete(prompt: str = "Complete? (y/n, blank=unknown)") -> bool | None:
    while True:
        value = input(f"{prompt}: ").strip().casefold()
        if not value:
            return None
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter y, n, or leave blank.")


def interactive_stage(
    path: str | Path,
    *,
    output: str | Path | None = None,
    extra_folders: list[str] | None = None,
    primary_folders: list[str] | None = None,
) -> Path:
    scan = scan_folder(path, extra_folders=extra_folders, primary_folders=primary_folders)
    plan = _review_loop(build_review_plan(scan))

    print("\nImport metadata:")
    author = input("Author: ").strip()
    while not author:
        print("Author is required.")
        author = input("Author: ").strip()
    series_default = scan.content_root.name
    series = _prompt_optional("Series", series_default) or series_default

    issue_metadata: dict[str, dict[str, object]] = {}
    if scan.is_series_candidate:
        issue_items = [item for item in plan.items if item.role is ReviewRole.ISSUE]
        for item in issue_items:
            key = str(item.relative_path)
            print(f"\nIssue metadata for {key}:")
            issue_metadata[key] = {
                "issue_number": _prompt_optional("Issue number/label", item.name),
                "title": _prompt_optional("Issue title (optional)"),
                "complete": _prompt_complete(),
            }
    else:
        print("\nIssue metadata:")
        issue_metadata["."] = {
            "issue_number": _prompt_optional("Issue number/label (optional)"),
            "title": _prompt_optional("Issue title (optional)"),
            "complete": _prompt_complete(),
        }

    staged = build_staged_import(
        plan,
        author=author,
        series=series,
        issue_metadata=issue_metadata,
    )
    if output is None:
        destination = Path.cwd() / "staging" / f"{staged.staging_id}.json"
    else:
        destination = Path(output)
    saved = staged.save(destination)
    print(f"\nStaging record saved: {saved}")
    print("No comic files were copied, moved, renamed, converted, or deleted.")
    return saved

def _add_folder_hint_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--extra-folder",
        action="append",
        default=[],
        metavar="PATH",
        help="Treat this folder as extra content (relative to the scanned folder). Repeatable.",
    )
    parser.add_argument(
        "--primary-folder",
        action="append",
        default=[],
        metavar="PATH",
        help="Treat this folder as primary/issue content (relative to the scanned folder). Repeatable.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="comic-import")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scan a folder without modifying it")
    scan.add_argument("folder")
    _add_folder_hint_args(scan)

    review = subparsers.add_parser("review", help="Interactively review scanner classifications")
    review.add_argument("folder")
    _add_folder_hint_args(review)

    stage = subparsers.add_parser("stage", help="Review and save a persistent import staging record")
    stage.add_argument("folder")
    stage.add_argument("--output", help="Path for the staging JSON file")
    _add_folder_hint_args(stage)

    commit = subparsers.add_parser("commit", help="Copy a staged import into the library and SQLite database")
    commit.add_argument("staging_file")
    commit.add_argument("--library", default="./library", help="Library storage root (default: ./library)")
    commit.add_argument("--database", default="./comic_archive.sqlite3", help="SQLite database path")
    commit.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="Commit even when duplicate content or metadata is detected",
    )

    library = subparsers.add_parser("library", help="Inspect the committed library")
    library.add_argument("--database", default="./comic_archive.sqlite3", help="SQLite database path")
    library.add_argument("--ids", action="store_true", help="Show database IDs for editing")

    edit = subparsers.add_parser("edit", help="Correct committed library metadata and structure")
    edit.add_argument("--database", default="./comic_archive.sqlite3", help="SQLite database path")
    edit_sub = edit.add_subparsers(dest="edit_command", required=True)

    e_author = edit_sub.add_parser("author", help="Rename an author")
    e_author.add_argument("author_id")
    e_author.add_argument("--name", required=True)

    e_series = edit_sub.add_parser("series", help="Edit a series")
    e_series.add_argument("series_id")
    e_series.add_argument("--title")
    e_series.add_argument("--author-id")

    e_issue = edit_sub.add_parser("issue", help="Edit an issue")
    e_issue.add_argument("issue_id")
    e_issue.add_argument("--number")
    e_issue.add_argument("--title")
    e_issue.add_argument("--complete", choices=["yes", "no", "unknown"] )
    e_issue.add_argument("--series-id")

    e_group = edit_sub.add_parser("group", help="Rename a content group")
    e_group.add_argument("group_id")
    e_group.add_argument("--name", required=True)

    e_rename_extra = edit_sub.add_parser("rename-extra", help="Rename an extra content group")
    e_rename_extra.add_argument("group_id")
    e_rename_extra.add_argument("--name", required=True)

    e_move = edit_sub.add_parser("move-extra", help="Move an extra between series/issue ownership")
    e_move.add_argument("group_id")
    e_move.add_argument("--series-id", required=True)
    e_move.add_argument("--issue-id", help="Omit to make it a series-level extra")

    e_reorder = edit_sub.add_parser("reorder", help="Set the order of active media in a group")
    e_reorder.add_argument("group_id")
    e_reorder.add_argument("media_ids", nargs="+")

    e_remove = edit_sub.add_parser("remove-media", help="Hide a media item without deleting its file")
    e_remove.add_argument("media_id")
    e_restore = edit_sub.add_parser("restore-media", help="Restore a previously hidden media item")
    e_restore.add_argument("media_id")

    serve = subparsers.add_parser("serve", help="Run the read-only web library")
    serve.add_argument("--database", default="./comic_archive.sqlite3", help="SQLite database path")
    serve.add_argument("--library", default="./library", help="Library storage root")
    serve.add_argument("--staging", default="./staging", help="Import staging-record directory")
    serve.add_argument("--import-root", default=".", help="Root folder the web importer is allowed to browse")
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    serve.add_argument("--secure-cookies", action="store_true", help="Mark login cookies HTTPS-only (use behind Caddy/HTTPS)")

    user_add = subparsers.add_parser("user-add", help="Create a local account (no public signup exists)")
    user_add.add_argument("username")
    user_add.add_argument("--database", default="./comic_archive.sqlite3", help="SQLite database path")
    user_add.add_argument("--admin", action="store_true", help="Create an administrator account")
    user_add.add_argument("--password", help="Initial password; omit to be prompted securely")

    thumbs = subparsers.add_parser("thumbnails-build", help="Generate missing image thumbnails for the existing library")
    thumbs.add_argument("--database", default="./comic_archive.sqlite3", help="SQLite database path")
    thumbs.add_argument("--library", default="./library", help="Library storage root")

    history = subparsers.add_parser("history", help="Show edit history")
    history.add_argument("--database", default="./comic_archive.sqlite3", help="SQLite database path")
    history.add_argument("--entity-id")

    args = parser.parse_args()
    try:
        if args.command == "scan":
            print_scan(args.folder, extra_folders=args.extra_folder, primary_folders=args.primary_folder)
            return 0
        if args.command == "review":
            interactive_review(args.folder, extra_folders=args.extra_folder, primary_folders=args.primary_folder)
            return 0
        if args.command == "stage":
            interactive_stage(
                args.folder,
                output=args.output,
                extra_folders=args.extra_folder,
                primary_folders=args.primary_folder,
            )
            return 0
        if args.command == "commit":
            staged = load_staged_import(args.staging_file)
            result = commit_staged_import(
                staged,
                library_root=args.library,
                database_path=args.database,
                allow_duplicate=args.allow_duplicate,
            )
            print(f"Committed import: {result.import_id}")
            print(f"Copied files:     {result.copied_files}")
            print(f"Copied bytes:     {_format_size(result.copied_bytes)}")
            print(f"Library:          {result.library_root}")
            print(f"Database:         {result.database_path}")
            if result.duplicate_warnings:
                print("Duplicate warnings overridden:")
                for warning in result.duplicate_warnings:
                    print(f"  - {warning}")
            return 0
        if args.command == "edit":
            if args.edit_command == "author":
                rename_author(args.database, args.author_id, args.name)
            elif args.edit_command == "series":
                kwargs = {}
                if args.title is not None: kwargs["title"] = args.title
                if args.author_id is not None: kwargs["author_id"] = args.author_id
                edit_series(args.database, args.series_id, **kwargs)
            elif args.edit_command == "issue":
                kwargs = {}
                if args.number is not None: kwargs["issue_number"] = args.number
                if args.title is not None: kwargs["title"] = args.title
                if args.complete is not None:
                    kwargs["complete"] = None if args.complete == "unknown" else args.complete == "yes"
                if args.series_id is not None: kwargs["series_id"] = args.series_id
                edit_issue(args.database, args.issue_id, **kwargs)
            elif args.edit_command in {"group", "rename-extra"}:
                rename_group(args.database, args.group_id, args.name)
            elif args.edit_command == "move-extra":
                move_extra_group(args.database, args.group_id, series_id=args.series_id, issue_id=args.issue_id)
            elif args.edit_command == "reorder":
                reorder_media(args.database, args.group_id, args.media_ids)
            elif args.edit_command == "remove-media":
                set_media_active(args.database, args.media_id, False)
            elif args.edit_command == "restore-media":
                set_media_active(args.database, args.media_id, True)
            print("Edit saved.")
            return 0
        if args.command == "user-add":
            import getpass
            password = args.password
            if password is None:
                password = getpass.getpass("Password: ")
                confirm = getpass.getpass("Confirm password: ")
                if password != confirm:
                    parser.error("Passwords do not match")
            user = create_user(args.database, args.username, password, is_admin=args.admin)
            print(f"Created {'admin' if user.is_admin else 'user'} account: {user.username}")
            return 0
        if args.command == "serve":
            try:
                import uvicorn
            except ImportError as exc:
                parser.error("Web dependencies are not installed. Install with: pip install -e '.[web]'")
            from .web import create_app
            uvicorn.run(
                create_app(
                    args.database,
                    args.library,
                    args.staging,
                    secure_cookies=args.secure_cookies,
                    import_root=args.import_root,
                ),
                host=args.host,
                port=args.port,
            )
            return 0
        if args.command == "thumbnails-build":
            result = build_missing_thumbnails(args.database, args.library)
            print(f"Created:  {result.created}")
            print(f"Existing: {result.existing}")
            print(f"Skipped:  {result.skipped}")
            print(f"Failed:   {result.failed}")
            return 0
        if args.command == "history":
            rows = get_history(args.database, entity_id=args.entity_id)
            if not rows:
                print("No edit history.")
            for row in rows:
                print(f"{row['created_at']}  {row['entity_type']} {row['entity_id']}  {row['action']}")
                print(f"  before: {row['before']}")
                print(f"  after:  {row['after']}")
            return 0
        if args.command == "library":
            authors = read_library(args.database)
            if not authors:
                print("Library is empty.")
                return 0
            for author in authors:
                print(f"{author.name}" + (f"  [author:{author.id}]" if args.ids else ""))
                for series in author.series:
                    print(f"  {series.title}" + (f"  [series:{series.id}]" if args.ids else ""))
                    for issue in series.issues:
                        label = issue.issue_number or issue.title or "(unlabeled issue)"
                        status = "complete" if issue.complete is True else "incomplete" if issue.complete is False else "unknown"
                        print(f"    Issue {label} [{status}]" + (f"  [issue:{issue.id}]" if args.ids else ""))
                        for group in issue.groups:
                            print(f"      {group.name} ({group.role}) - {len(group.media)} media" + (f"  [group:{group.id}]" if args.ids else ""))
                            if args.ids:
                                for media in group.media:
                                    print(f"        {media.position}: {media.original_relative_path}  [media:{media.id}]")
                    if series.extras:
                        print("    Series extras:")
                        for group in series.extras:
                            print(f"      {group.name} ({group.role}) - {len(group.media)} media" + (f"  [group:{group.id}]" if args.ids else ""))
                            if args.ids:
                                for media in group.media:
                                    print(f"        {media.position}: {media.original_relative_path}  [media:{media.id}]")
            return 0
            print("Source files were not modified or deleted.")
            return 0
    except (FolderScanError, ReviewError, StagingError, CommitError, EditError, AuthError) as exc:
        parser.error(str(exc))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
