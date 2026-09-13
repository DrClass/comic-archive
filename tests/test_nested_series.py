import sqlite3
from pathlib import Path

import pytest

from comic_archive.editing import EditError, edit_series
from comic_archive.importer.commit import commit_staged_import
from comic_archive.importer.review import ReviewRole, build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import
from comic_archive.library import read_library


def touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_recursive_subseries_scan_and_stage(tmp_path: Path) -> None:
    source = tmp_path / "Comic"
    touch(source / "Volume 1" / "Arc A" / "Chapter 1" / "001.jpg")
    touch(source / "Volume 1" / "Arc A" / "Chapter 2" / "001.jpg")
    touch(source / "Volume 1" / "Arc B" / "Chapter 3" / "001.jpg")

    scan = scan_folder(source, subseries_folders=["Volume 1", "Volume 1/Arc A", "Volume 1/Arc B"])
    assert [(item.relative_path.as_posix(), item.parent_path.as_posix()) for item in scan.subseries] == [
        ("Volume 1", "."),
        ("Volume 1/Arc A", "Volume 1"),
        ("Volume 1/Arc B", "Volume 1"),
    ]
    assert {issue.series_path.as_posix() for issue in scan.issues} == {"Volume 1/Arc A", "Volume 1/Arc B"}

    plan = build_review_plan(scan)
    assert [item.role for item in plan.items if item.source_kind == "subseries"] == [
        ReviewRole.SUBSERIES,
        ReviewRole.SUBSERIES,
        ReviewRole.SUBSERIES,
    ]
    staged = build_staged_import(plan, author="Artist", series="Comic")
    assert [(item.source_key, item.parent_key) for item in staged.subseries] == [
        ("Volume 1", "."),
        (str(Path("Volume 1") / "Arc A"), "Volume 1"),
        (str(Path("Volume 1") / "Arc B"), "Volume 1"),
    ]
    assert {issue.series_key for issue in staged.issues} == {str(Path("Volume 1") / "Arc A"), str(Path("Volume 1") / "Arc B")}


def test_nested_series_commit_and_recursive_totals(tmp_path: Path) -> None:
    source = tmp_path / "Comic"
    touch(source / "Arc One" / "Chapter 1" / "001.jpg", b"1")
    touch(source / "Arc One" / "Chapter 1" / "Extras" / "bonus.png", b"b")
    touch(source / "Arc One" / "Chapter 2" / "001.jpg", b"2")
    touch(source / "Arc Two" / "Part A" / "001.jpg", b"3")

    staged = build_staged_import(
        build_review_plan(scan_folder(source, subseries_folders=["Arc One", "Arc Two"])),
        author="Artist",
        series="Comic",
    )
    db = tmp_path / "archive.sqlite3"
    result = commit_staged_import(staged, library_root=tmp_path / "library", database_path=db)

    author = read_library(db)[0]
    root = author.series[0]
    assert root.id == result.series_id
    assert [child.title for child in root.children] == ["Arc One", "Arc Two"]
    assert root.issues == []
    assert root.total_issues == 3
    assert root.total_pages == 3
    assert root.total_extras == 1
    assert [issue.issue_number for issue in root.children[0].issues] == ["Chapter 1", "Chapter 2"]

    with sqlite3.connect(db) as conn:
        parents = conn.execute(
            "SELECT child.title, parent.title FROM series child JOIN series parent ON parent.id = child.parent_series_id ORDER BY child.title"
        ).fetchall()
    assert parents == [("Arc One", "Comic"), ("Arc Two", "Comic")]


def test_existing_series_can_be_nested_and_cycles_are_rejected(tmp_path: Path) -> None:
    def import_one(title: str, filename: str):
        source = tmp_path / filename
        touch(source / "001.jpg", title.encode())
        staged = build_staged_import(build_review_plan(scan_folder(source)), author="Artist", series=title)
        return commit_staged_import(staged, library_root=tmp_path / "library", database_path=tmp_path / "archive.sqlite3")

    parent = import_one("Parent", "parent")
    child = import_one("Child", "child")
    edit_series(tmp_path / "archive.sqlite3", child.series_id, parent_series_id=parent.series_id)

    root = read_library(tmp_path / "archive.sqlite3")[0].series[0]
    assert root.title == "Parent"
    assert [item.title for item in root.children] == ["Child"]

    with pytest.raises(EditError, match="cycle"):
        edit_series(tmp_path / "archive.sqlite3", parent.series_id, parent_series_id=child.series_id)
