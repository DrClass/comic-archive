import sqlite3
from pathlib import Path

import pytest

from comic_archive.editing import (
    EditError,
    edit_issue,
    edit_series,
    get_history,
    move_extra_group,
    rename_author,
    rename_group,
    reorder_media,
    set_media_active,
)
from comic_archive.importer.commit import commit_staged_import
from comic_archive.importer.review import build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import
from comic_archive.library import read_library


def touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def make_library(tmp_path: Path):
    source = tmp_path / "series"
    touch(source / "1" / "001.jpg", b"one")
    touch(source / "1" / "002.jpg", b"two")
    touch(source / "1" / "Textless" / "001.png", b"textless")
    touch(source / "Covers" / "cover.png", b"cover")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Artist",
        series="Comic",
        issue_metadata={"1": {"issue_number": "1", "title": "Old", "complete": False}},
    )
    db = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=db)
    return db, library, result


def test_metadata_edits_are_persisted_and_audited(tmp_path: Path) -> None:
    db, _, result = make_library(tmp_path)
    rename_author(db, result.author_id, "Renamed Artist")
    edit_series(db, result.series_id, title="Renamed Comic")
    edit_issue(db, result.issue_ids[0], issue_number="1.5", title="Fixed", complete=True)

    authors = read_library(db)
    assert authors[0].name == "Renamed Artist"
    assert authors[0].series[0].title == "Renamed Comic"
    issue = authors[0].series[0].issues[0]
    assert (issue.issue_number, issue.title, issue.complete) == ("1.5", "Fixed", True)
    assert len(get_history(db)) == 3


def test_extra_can_move_between_issue_and_series_and_be_renamed(tmp_path: Path) -> None:
    db, _, result = make_library(tmp_path)
    series = read_library(db)[0].series[0]
    textless = next(g for g in series.issues[0].groups if g.name == "Textless")
    rename_group(db, textless.id, "No Text")
    move_extra_group(db, textless.id, series_id=result.series_id, issue_id=None)

    series = read_library(db)[0].series[0]
    assert "No Text" in {g.name for g in series.extras}
    assert "No Text" not in {g.name for g in series.issues[0].groups}

    move_extra_group(db, textless.id, series_id=result.series_id, issue_id=result.issue_ids[0])
    series = read_library(db)[0].series[0]
    assert "No Text" in {g.name for g in series.issues[0].groups}


def test_primary_group_cannot_use_move_extra(tmp_path: Path) -> None:
    db, _, result = make_library(tmp_path)
    primary = next(g for g in read_library(db)[0].series[0].issues[0].groups if g.role == "primary")
    with pytest.raises(EditError, match="Primary"):
        move_extra_group(db, primary.id, series_id=result.series_id, issue_id=None)


def test_media_reorder_and_soft_remove_restore(tmp_path: Path) -> None:
    db, library, _ = make_library(tmp_path)
    issue = read_library(db)[0].series[0].issues[0]
    primary = next(g for g in issue.groups if g.role == "primary")
    ids = [m.id for m in primary.media]
    paths_before = [library / m.stored_path for m in primary.media]
    old_fingerprint = sqlite3.connect(db).execute("SELECT content_fingerprint FROM issues WHERE id = ?", (issue.id,)).fetchone()[0]

    reorder_media(db, primary.id, list(reversed(ids)))
    reordered = next(g for g in read_library(db)[0].series[0].issues[0].groups if g.id == primary.id)
    assert [m.id for m in reordered.media] == list(reversed(ids))

    set_media_active(db, ids[0], False)
    after_remove = next(g for g in read_library(db)[0].series[0].issues[0].groups if g.id == primary.id)
    assert ids[0] not in [m.id for m in after_remove.media]
    assert all(path.exists() for path in paths_before)

    set_media_active(db, ids[0], True)
    after_restore = next(g for g in read_library(db)[0].series[0].issues[0].groups if g.id == primary.id)
    assert ids[0] in [m.id for m in after_restore.media]
    with sqlite3.connect(db) as conn:
        new_fingerprint = conn.execute("SELECT content_fingerprint FROM issues WHERE id = ?", (issue.id,)).fetchone()[0]
    assert new_fingerprint != old_fingerprint  # reorder changes ordered-content fingerprint


def test_reorder_requires_every_active_item(tmp_path: Path) -> None:
    db, _, _ = make_library(tmp_path)
    primary = next(g for g in read_library(db)[0].series[0].issues[0].groups if g.role == "primary")
    with pytest.raises(EditError, match="every active"):
        reorder_media(db, primary.id, [primary.media[0].id])


def test_noop_edits_do_not_create_audit_records(tmp_path: Path) -> None:
    db, _, result = make_library(tmp_path)
    series = read_library(db)[0].series[0]
    issue = series.issues[0]
    textless = next(g for g in issue.groups if g.name == "Textless")
    primary = next(g for g in issue.groups if g.role == "primary")
    original_ids = [m.id for m in primary.media]

    rename_author(db, result.author_id, "Artist")
    edit_series(db, result.series_id, title="Comic", author_id=result.author_id)
    edit_issue(
        db,
        result.issue_ids[0],
        issue_number="1",
        title="Old",
        complete=False,
        series_id=result.series_id,
    )
    rename_group(db, textless.id, "Textless")
    move_extra_group(
        db,
        textless.id,
        series_id=result.series_id,
        issue_id=result.issue_ids[0],
    )
    reorder_media(db, primary.id, original_ids)

    assert get_history(db) == []


def test_history_resolves_human_readable_labels_and_hides_legacy_noops(tmp_path: Path) -> None:
    db, _, result = make_library(tmp_path)
    series = read_library(db)[0].series[0]
    issue = series.issues[0]
    primary = next(g for g in issue.groups if g.role == "primary")
    ids = [m.id for m in primary.media]

    reorder_media(db, primary.id, list(reversed(ids)))

    # Simulate one of the old bad records already present in a user's DB.
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO audit_log(entity_type, entity_id, action, before_json, after_json)
               VALUES (?, ?, ?, ?, ?)""",
            ("group", primary.id, "reorder-media", '["same"]', '["same"]'),
        )
        conn.commit()

    history = get_history(db)
    assert len(history) == 1
    row = history[0]
    assert "Artist / Comic / 1 / Primary" in row["entity_label"]
    assert row["before_display"] == ["001.jpg", "002.jpg"]
    assert row["after_display"] == ["002.jpg", "001.jpg"]


def test_edit_series_completeness(tmp_path: Path) -> None:
    from comic_archive.importer.scanner import scan_folder
    from comic_archive.importer.review import build_review_plan
    from comic_archive.importer.staging import build_staged_import
    from comic_archive.importer.commit import commit_staged_import
    from comic_archive.library import read_library

    source = tmp_path / "source"
    source.mkdir()
    (source / "001.jpg").write_bytes(b"one")
    staged = build_staged_import(build_review_plan(scan_folder(source)), author="Artist", series="Series")
    result = commit_staged_import(staged, library_root=tmp_path / "library", database_path=tmp_path / "db.sqlite3")

    edit_series(tmp_path / "db.sqlite3", result.series_id, complete=False)
    assert read_library(tmp_path / "db.sqlite3")[0].series[0].complete is False
    edit_series(tmp_path / "db.sqlite3", result.series_id, complete=None)
    assert read_library(tmp_path / "db.sqlite3")[0].series[0].complete is None
