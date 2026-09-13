import json
import sqlite3
from pathlib import Path

import pytest

from comic_archive.importer.commit import CommitError, commit_staged_import, load_staged_import
from comic_archive.importer.review import build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import


def touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def make_stage(source: Path, stage_path: Path):
    plan = build_review_plan(scan_folder(source))
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Comic",
        issue_metadata={".": {"issue_number": "7", "complete": False}},
    )
    staged.save(stage_path)
    return staged


def test_commit_copies_files_and_creates_database_records(tmp_path: Path) -> None:
    source = tmp_path / "source"
    touch(source / "001.jpg", b"one")
    touch(source / "002.png", b"two")
    touch(source / "Textless" / "001.png", b"textless")

    staged = make_stage(source, tmp_path / "stage.json")
    result = commit_staged_import(
        staged,
        library_root=tmp_path / "library",
        database_path=tmp_path / "archive.sqlite3",
    )

    assert result.copied_files == 3
    assert (source / "001.jpg").read_bytes() == b"one"

    with sqlite3.connect(result.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM content_groups").fetchone()[0] == 2
        rows = db.execute("SELECT stored_path FROM media ORDER BY stored_path").fetchall()

    assert len(rows) == 3
    assert all((result.library_root / row[0]).is_file() for row in rows)


def test_commit_preserves_separate_series_and_issue_extra_groups(tmp_path: Path) -> None:
    source = tmp_path / "series"
    touch(source / "1" / "001.jpg")
    touch(source / "1" / "Textless" / "001.png")
    touch(source / "Covers" / "cover.png")
    plan = build_review_plan(scan_folder(source))
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Series",
        issue_metadata={"1": {"issue_number": "1"}},
    )

    result = commit_staged_import(
        staged,
        library_root=tmp_path / "library",
        database_path=tmp_path / "archive.sqlite3",
    )
    with sqlite3.connect(result.database_path) as db:
        groups = db.execute(
            "SELECT name, role, issue_id IS NULL FROM content_groups ORDER BY name"
        ).fetchall()

    assert ("Covers", "series-extra", 1) in groups
    assert ("Textless", "issue-extra", 0) in groups


def test_commit_rejects_changed_source_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    touch(source / "001.jpg", b"original")
    staged = make_stage(source, tmp_path / "stage.json")
    touch(source / "001.jpg", b"changed and longer")

    with pytest.raises(CommitError, match="changed size"):
        commit_staged_import(
            staged,
            library_root=tmp_path / "library",
            database_path=tmp_path / "archive.sqlite3",
        )


def test_same_staging_record_cannot_be_committed_twice(tmp_path: Path) -> None:
    source = tmp_path / "source"
    touch(source / "001.jpg")
    staged = make_stage(source, tmp_path / "stage.json")
    kwargs = {
        "library_root": tmp_path / "library",
        "database_path": tmp_path / "archive.sqlite3",
    }
    commit_staged_import(staged, **kwargs)
    with pytest.raises(CommitError, match="already been committed"):
        commit_staged_import(staged, **kwargs)


def test_load_staged_import_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source"
    touch(source / "001.jpg")
    staged = make_stage(source, tmp_path / "stage.json")
    loaded = load_staged_import(tmp_path / "stage.json")
    assert loaded.staging_id == staged.staging_id
    assert loaded.issues[0].groups[0].media[0].relative_path == "001.jpg"


def test_separate_single_issue_imports_can_join_same_series(tmp_path: Path) -> None:
    source1 = tmp_path / "issue1"
    source2 = tmp_path / "issue2"
    touch(source1 / "001.jpg", b"one")
    touch(source2 / "001.jpg", b"two")

    plan1 = build_review_plan(scan_folder(source1))
    staged1 = build_staged_import(
        plan1, author="Artist", series="Comic", issue_metadata={".": {"issue_number": "1"}}
    )
    plan2 = build_review_plan(scan_folder(source2))
    staged2 = build_staged_import(
        plan2, author="Artist", series="Comic", issue_metadata={".": {"issue_number": "2"}}
    )

    kwargs = {
        "library_root": tmp_path / "library",
        "database_path": tmp_path / "archive.sqlite3",
    }
    first = commit_staged_import(staged1, **kwargs)
    second = commit_staged_import(staged2, **kwargs)

    assert first.author_id == second.author_id
    assert first.series_id == second.series_id
    with sqlite3.connect(first.database_path) as db:
        issues = db.execute(
            "SELECT issue_number FROM issues ORDER BY issue_number"
        ).fetchall()
    assert issues == [("1",), ("2",)]


def test_duplicate_metadata_is_blocked_unless_overridden(tmp_path: Path) -> None:
    source1 = tmp_path / "issue_a"
    source2 = tmp_path / "issue_b"
    touch(source1 / "001.jpg", b"first-version")
    touch(source2 / "001.jpg", b"different-version")

    def staged_for(source: Path):
        return build_staged_import(
            build_review_plan(scan_folder(source)),
            author="Artist",
            series="Comic",
            issue_metadata={".": {"issue_number": "7"}},
        )

    kwargs = {"library_root": tmp_path / "library", "database_path": tmp_path / "archive.sqlite3"}
    commit_staged_import(staged_for(source1), **kwargs)
    with pytest.raises(CommitError, match="Metadata match"):
        commit_staged_import(staged_for(source2), **kwargs)

    result = commit_staged_import(staged_for(source2), allow_duplicate=True, **kwargs)
    assert any("Metadata match" in warning for warning in result.duplicate_warnings)


def test_exact_content_duplicate_is_detected_across_different_metadata(tmp_path: Path) -> None:
    source1 = tmp_path / "one"
    source2 = tmp_path / "two"
    touch(source1 / "001.jpg", b"same")
    touch(source2 / "001.jpg", b"same")

    first = build_staged_import(
        build_review_plan(scan_folder(source1)),
        author="Artist A", series="Comic A", issue_metadata={".": {"issue_number": "1"}},
    )
    second = build_staged_import(
        build_review_plan(scan_folder(source2)),
        author="Artist B", series="Comic B", issue_metadata={".": {"issue_number": "99"}},
    )
    kwargs = {"library_root": tmp_path / "library", "database_path": tmp_path / "archive.sqlite3"}
    commit_staged_import(first, **kwargs)
    with pytest.raises(CommitError, match="Exact content match"):
        commit_staged_import(second, **kwargs)


def test_duplicate_detection_backfills_previous_milestone_rows(tmp_path: Path) -> None:
    source1 = tmp_path / "old"
    source2 = tmp_path / "new"
    touch(source1 / "001.jpg", b"same-old-content")
    touch(source2 / "001.jpg", b"same-old-content")

    old_stage = build_staged_import(
        build_review_plan(scan_folder(source1)),
        author="Old Artist", series="Old Comic", issue_metadata={".": {"issue_number": "1"}},
    )
    kwargs = {"library_root": tmp_path / "library", "database_path": tmp_path / "archive.sqlite3"}
    first = commit_staged_import(old_stage, **kwargs)

    # Simulate rows created before hash/fingerprint columns were populated.
    with sqlite3.connect(first.database_path) as db:
        db.execute("UPDATE media SET sha256 = NULL")
        db.execute("UPDATE issues SET content_fingerprint = NULL")

    new_stage = build_staged_import(
        build_review_plan(scan_folder(source2)),
        author="Different Artist", series="Different Comic", issue_metadata={".": {"issue_number": "5"}},
    )
    with pytest.raises(CommitError, match="Exact content match"):
        commit_staged_import(new_stage, **kwargs)


def test_pdf_pages_commit_as_jpeg_files(tmp_path):
    import fitz

    source = tmp_path / "source"
    source.mkdir()
    doc = fitz.open()
    doc.new_page(width=200, height=300)
    doc.new_page(width=200, height=300)
    doc.save(source / "comic.pdf")
    doc.close()

    scan = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")
    staged = build_staged_import(
        build_review_plan(scan),
        author="PDF Artist",
        series="PDF Comic",
    )
    library = tmp_path / "library"
    result = commit_staged_import(
        staged,
        library_root=library,
        database_path=tmp_path / "archive.sqlite3",
    )

    stored = sorted(
        path for path in (library / "series" / result.series_id / "groups").rglob("*.jpg")
        if path.parent.name != "_thumbs"
    )
    assert len(stored) == 2
    assert [path.name for path in stored] == ["000001.jpg", "000002.jpg"]
    assert all(path.read_bytes().startswith(b"\xff\xd8") for path in stored)
