from pathlib import Path

from comic_archive.importer.commit import commit_staged_import
from comic_archive.importer.review import build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import
from comic_archive.library import read_library


def touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_library_readback_preserves_issue_and_extra_structure(tmp_path: Path) -> None:
    source = tmp_path / "series"
    touch(source / "1" / "001.jpg", b"page")
    touch(source / "1" / "Textless" / "001.png", b"textless")
    touch(source / "Covers" / "cover.png", b"cover")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Artist", series="Comic", issue_metadata={"1": {"issue_number": "1", "complete": True}},
    )
    db = tmp_path / "archive.sqlite3"
    commit_staged_import(staged, library_root=tmp_path / "library", database_path=db)

    authors = read_library(db)
    assert len(authors) == 1
    series = authors[0].series[0]
    assert series.title == "Comic"
    assert series.issues[0].issue_number == "1"
    assert series.issues[0].complete is True
    assert {group.name for group in series.issues[0].groups} == {"Primary", "Textless"}
    assert [group.name for group in series.extras] == ["Covers"]
    assert series.issues[0].groups[0].media[0].sha256


def test_series_completeness_round_trips(tmp_path: Path) -> None:
    from comic_archive.importer.scanner import scan_folder
    from comic_archive.importer.review import build_review_plan
    from comic_archive.importer.staging import build_staged_import
    from comic_archive.importer.commit import commit_staged_import

    source = tmp_path / "source"
    source.mkdir()
    (source / "001.jpg").write_bytes(b"one")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Artist",
        series="Complete Series",
        series_complete=True,
        issue_metadata={".": {"issue_number": "1", "complete": True}},
    )
    commit_staged_import(staged, library_root=tmp_path / "library", database_path=tmp_path / "db.sqlite3")

    series = read_library(tmp_path / "db.sqlite3")[0].series[0]
    assert series.complete is True
