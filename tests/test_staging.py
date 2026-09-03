import json
from pathlib import Path

from comic_archive.importer.review import ReviewRole, build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import


def touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_stage_single_issue_preserves_named_extras(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Textless" / "001.png")
    touch(tmp_path / "Extra Angles" / "angle.png")

    plan = build_review_plan(scan_folder(tmp_path))
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Comic",
        issue_metadata={".": {"issue_number": "7", "title": "Seven", "complete": False}},
    )

    assert staged.author == "Artist"
    assert staged.series == "Comic"
    assert staged.issues[0].issue_number == "7"
    assert staged.issues[0].complete is False
    assert [g.name for g in staged.issues[0].groups] == ["Primary", "Extra Angles", "Textless"]
    assert [g.role for g in staged.issues[0].groups] == ["primary", "issue-extra", "issue-extra"]


def test_stage_series_keeps_issue_and_series_extras_separate(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "1" / "Textless" / "001.png")
    touch(tmp_path / "3" / "001.jpg")
    touch(tmp_path / "Covers" / "cover.png")

    plan = build_review_plan(scan_folder(tmp_path))
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Series",
        issue_metadata={
            "1": {"issue_number": "1", "complete": True},
            "3": {"issue_number": "3", "complete": False},
        },
    )

    assert [issue.issue_number for issue in staged.issues] == ["1", "3"]
    assert [g.name for g in staged.issues[0].groups] == ["Primary", "Textless"]
    assert [g.name for g in staged.series_extras] == ["Covers"]


def test_stage_can_move_issue_extra_to_series_extra(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "1" / "Covers" / "cover.png")
    touch(tmp_path / "2" / "001.jpg")

    plan = build_review_plan(scan_folder(tmp_path))
    plan.set_role("1/Covers", ReviewRole.SERIES_EXTRA)
    staged = build_staged_import(plan, author="A", series="S")

    assert [g.name for g in staged.series_extras] == ["Covers"]
    assert [g.name for g in staged.issues[0].groups] == ["Primary"]


def test_staged_import_serializes_to_json(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    plan = build_review_plan(scan_folder(tmp_path))
    staged = build_staged_import(plan, author="A", series="S")

    destination = tmp_path / "stage.json"
    staged.save(destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["author"] == "A"
    assert payload["series"] == "S"
    assert payload["issues"][0]["groups"][0]["media"][0]["relative_path"] == "001.jpg"


def test_staging_preserves_reviewed_extra_name(tmp_path: Path) -> None:
    source = tmp_path / "Issue"
    touch(source / "001.jpg")
    touch(source / "Series - Part XX - Extras" / "bonus.png")
    plan = build_review_plan(scan_folder(source))
    plan.set_name("Series - Part XX - Extras", "Extras")
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Comic",
        issue_metadata={".": {"issue_number": "1"}},
    )
    extra_names = [g.name for g in staged.issues[0].groups if g.role == "issue-extra"]
    assert extra_names == ["Extras"]


def test_stage_series_keeps_nested_extras_under_each_issue_separate(tmp_path: Path) -> None:
    for issue_number in (1, 2, 3):
        issue = tmp_path / f"issue {issue_number}"
        touch(issue / "001.jpg")
        touch(issue / "002.jpg")
        touch(issue / "extras" / "bonus.png")

    plan = build_review_plan(scan_folder(tmp_path))
    staged = build_staged_import(plan, author="Artist", series="Comic")

    assert [issue.source_key for issue in staged.issues] == ["issue 1", "issue 2", "issue 3"]
    for issue in staged.issues:
        assert [(group.role, group.name, len(group.media)) for group in issue.groups] == [
            ("primary", "Primary", 2),
            ("issue-extra", "extras", 1),
        ]
