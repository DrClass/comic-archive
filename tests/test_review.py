from pathlib import Path

import pytest

from comic_archive.importer.review import ReviewError, ReviewRole, build_review_plan
from comic_archive.importer.scanner import scan_folder


def touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_single_issue_review_contains_primary_and_issue_extras(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Covers" / "front.png")

    plan = build_review_plan(scan_folder(tmp_path))

    assert [(str(item.relative_path), item.role) for item in plan.items] == [
        (".", ReviewRole.PRIMARY),
        ("Covers", ReviewRole.ISSUE_EXTRA),
    ]
    assert plan.is_valid


def test_review_can_correct_extra_to_primary(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Covers" / "002.png")

    plan = build_review_plan(scan_folder(tmp_path))
    changed = plan.set_role("Covers", ReviewRole.PRIMARY)

    assert changed.changed
    assert changed.role is ReviewRole.PRIMARY
    plan.reset_role("Covers")
    assert plan.find("Covers").role is ReviewRole.ISSUE_EXTRA


def test_series_review_separates_issue_and_series_extras(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "1" / "Extra Angles" / "angle.png")
    touch(tmp_path / "2" / "001.jpg")
    touch(tmp_path / "Extras" / "cover.png")

    plan = build_review_plan(scan_folder(tmp_path))

    roles = {str(item.relative_path): item.role for item in plan.items}
    assert roles == {
        "1": ReviewRole.ISSUE,
        "1/Extra Angles": ReviewRole.ISSUE_EXTRA,
        "2": ReviewRole.ISSUE,
        "Extras": ReviewRole.SERIES_EXTRA,
    }
    assert plan.find("1/Extra Angles").issue_path == Path("1")


def test_series_issue_can_be_reclassified_as_series_extra(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "2" / "001.jpg")

    plan = build_review_plan(scan_folder(tmp_path))
    plan.set_role("2", "series-extra")

    assert plan.find("2").role is ReviewRole.SERIES_EXTRA
    assert plan.is_valid


def test_series_extra_can_be_reclassified_as_issue(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "2" / "001.jpg")
    touch(tmp_path / "Covers" / "cover.png")

    plan = build_review_plan(scan_folder(tmp_path))
    plan.set_role("Covers", "issue")

    assert plan.find("Covers").role is ReviewRole.ISSUE
    assert plan.is_valid


def test_issue_container_rejects_primary_role(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "2" / "001.jpg")

    plan = build_review_plan(scan_folder(tmp_path))

    with pytest.raises(ReviewError):
        plan.set_role("1", "primary")

def test_review_keeps_named_extra_groups_separate(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Textless" / "001.png")
    touch(tmp_path / "Extra Angles" / "angle.png")

    plan = build_review_plan(scan_folder(tmp_path))

    extras = [item for item in plan.items if item.role is ReviewRole.ISSUE_EXTRA]
    assert [(item.name, str(item.relative_path)) for item in extras] == [
        ("Extra Angles", "Extra Angles"),
        ("Textless", "Textless"),
    ]


def test_review_can_rename_extra_without_touching_source(tmp_path: Path) -> None:
    source = tmp_path / "Issue"
    touch(source / "001.jpg")
    touch(source / "Series - Part XX - Extras" / "bonus.png")
    plan = build_review_plan(scan_folder(source))
    item = plan.find("Series - Part XX - Extras")
    plan.set_name(item.relative_path, "Extras")
    assert item.name == "Extras"
    assert item.changed
    assert (source / "Series - Part XX - Extras").exists()


def test_review_exposes_unrecognized_flattened_folder_before_staging(tmp_path: Path) -> None:
    from comic_archive.importer.review import flattened_folder_candidates

    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Alternate Artwork" / "a.png")
    touch(tmp_path / "Alternate Artwork" / "b.png")

    scan = scan_folder(tmp_path)
    assert scan.primary is not None
    assert "Alternate Artwork/a.png" in [str(media.relative_path) for media in scan.primary.media]

    candidates = flattened_folder_candidates(scan)
    assert [(str(item.display_path), item.media_count) for item in candidates] == [
        ("Alternate Artwork", 2),
    ]

    rescanned = scan_folder(tmp_path, extra_folders=[str(candidates[0].relative_path)])
    plan = build_review_plan(rescanned)
    assert plan.find("Alternate Artwork").role is ReviewRole.ISSUE_EXTRA
    assert rescanned.primary is not None
    assert [str(media.relative_path) for media in rescanned.primary.media] == ["001.jpg"]


def test_series_review_exposes_unrecognized_issue_subfolder_before_staging(tmp_path: Path) -> None:
    from comic_archive.importer.review import flattened_folder_candidates

    touch(tmp_path / "Issue A" / "Pages" / "001.jpg")
    touch(tmp_path / "Issue A" / "Gallery" / "bonus.png")
    touch(tmp_path / "Issue B" / "001.jpg")

    scan = scan_folder(tmp_path)
    candidates = flattened_folder_candidates(scan)
    assert any(str(item.display_path) == "Issue A/Gallery" for item in candidates)

    chosen = next(item for item in candidates if str(item.display_path) == "Issue A/Gallery")
    rescanned = scan_folder(tmp_path, extra_folders=[str(chosen.relative_path)])
    plan = build_review_plan(rescanned)
    assert plan.find("Issue A/Gallery").role is ReviewRole.ISSUE_EXTRA
    assert plan.find("Issue A/Gallery").issue_path == Path("Issue A")
