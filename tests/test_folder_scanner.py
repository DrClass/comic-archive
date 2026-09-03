from pathlib import Path

from comic_archive.importer.models import MediaKind, SuggestedRole
from comic_archive.importer.scanner import scan_folder


def touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_scans_primary_and_extra_groups(tmp_path: Path) -> None:
    touch(tmp_path / "1.jpg")
    touch(tmp_path / "2.png")
    touch(tmp_path / "10.mp4")
    touch(tmp_path / "Bonus" / "render2.png")
    touch(tmp_path / "Bonus" / "render10.png")
    touch(tmp_path / "Textless" / "1.jpg")
    touch(tmp_path / "Thumbs.db")
    touch(tmp_path / "notes.txt")

    result = scan_folder(tmp_path)

    assert result.primary is not None
    assert result.primary.suggested_role is SuggestedRole.PRIMARY
    assert [str(item.relative_path) for item in result.primary.media] == [
        "1.jpg",
        "2.png",
        "10.mp4",
    ]
    assert result.primary.media[-1].media_kind is MediaKind.VIDEO

    assert [group.name for group in result.extras] == ["Bonus", "Textless"]
    assert [str(item.relative_path) for item in result.extras[0].media] == [
        "render2.png",
        "render10.png",
    ]
    assert [str(path) for path in result.ignored_files] == ["notes.txt", "Thumbs.db"]


def test_collapses_single_wrapper_directory(tmp_path: Path) -> None:
    wrapper = tmp_path / "Downloaded Comic Name"
    touch(wrapper / "001.jpg")
    touch(wrapper / "002.jpg")

    result = scan_folder(tmp_path)

    assert result.content_root == wrapper
    assert result.primary is not None
    assert [item.relative_path.name for item in result.primary.media] == ["001.jpg", "002.jpg"]


def test_nested_extra_folders_stay_separate_groups(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Extras" / "Renders" / "1.png")
    touch(tmp_path / "Extras" / "Animations" / "2.mp4")

    result = scan_folder(tmp_path)

    assert [(group.name, str(group.relative_path)) for group in result.extras] == [
        ("Animations", "Extras/Animations"),
        ("Renders", "Extras/Renders"),
    ]


def test_missing_issue_numbers_are_irrelevant_to_folder_scan(tmp_path: Path) -> None:
    # The scanner only establishes ordered media within the selected import.
    # It deliberately makes no assumptions about series issue continuity.
    touch(tmp_path / "page1.jpg")
    touch(tmp_path / "page3.jpg")

    result = scan_folder(tmp_path)

    assert result.primary is not None
    assert [item.relative_path.name for item in result.primary.media] == ["page1.jpg", "page3.jpg"]


def test_same_stem_static_image_sorts_before_gif(tmp_path: Path) -> None:
    touch(tmp_path / "1.png")
    touch(tmp_path / "2.gif")
    touch(tmp_path / "2.png")
    touch(tmp_path / "3.png")

    result = scan_folder(tmp_path)

    assert result.primary is not None
    assert [item.relative_path.name for item in result.primary.media] == [
        "1.png",
        "2.png",
        "2.gif",
        "3.png",
    ]


def test_extra_angles_is_detected_as_extra(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "002.jpg")
    touch(tmp_path / "Extras" / "bonus.png")
    touch(tmp_path / "Extra Angles" / "angle1.png")

    result = scan_folder(tmp_path)

    assert result.primary is not None
    assert [item.relative_path.name for item in result.primary.media] == ["001.jpg", "002.jpg"]
    assert [group.name for group in result.extras] == ["Extra Angles", "Extras"]


def test_series_scan_preserves_issue_and_series_extras(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "1" / "002.jpg")
    touch(tmp_path / "1" / "Extra Angles" / "angle.png")
    touch(tmp_path / "2" / "001.jpg")
    touch(tmp_path / "2" / "002.jpg")
    touch(tmp_path / "Extras" / "series-bonus.png")

    result = scan_folder(tmp_path)

    assert result.is_series_candidate
    assert [issue.name for issue in result.issues] == ["1", "2"]
    assert result.issues[0].primary is not None
    assert [item.relative_path.name for item in result.issues[0].primary.media] == ["001.jpg", "002.jpg"]
    assert [group.name for group in result.issues[0].extras] == ["Extra Angles"]
    assert [group.name for group in result.extras] == ["Extras"]


def test_covers_is_detected_as_extra(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "002.jpg")
    touch(tmp_path / "covers" / "front.png")
    touch(tmp_path / "covers" / "back.png")

    result = scan_folder(tmp_path)

    assert result.primary is not None
    assert [item.relative_path.name for item in result.primary.media] == ["001.jpg", "002.jpg"]
    assert [group.name for group in result.extras] == ["covers"]


def test_explicit_extra_folder_override(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "002.jpg")
    touch(tmp_path / "Alternate Views" / "angle1.png")

    without_hint = scan_folder(tmp_path)
    assert without_hint.primary is not None
    assert [str(item.relative_path) for item in without_hint.primary.media] == [
        "001.jpg",
        "002.jpg",
        "Alternate Views/angle1.png",
    ]

    with_hint = scan_folder(tmp_path, extra_folders=["Alternate Views"])
    assert with_hint.primary is not None
    assert [item.relative_path.name for item in with_hint.primary.media] == ["001.jpg", "002.jpg"]
    assert [group.name for group in with_hint.extras] == ["Alternate Views"]


def test_explicit_primary_folder_override_beats_extra_name(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Bonus" / "002.jpg")
    touch(tmp_path / "Bonus" / "003.jpg")

    result = scan_folder(tmp_path, primary_folders=["Bonus"])

    assert result.primary is not None
    assert [str(item.relative_path) for item in result.primary.media] == [
        "001.jpg",
        "Bonus/002.jpg",
        "Bonus/003.jpg",
    ]
    assert result.extras == []


def test_nested_extra_container_preserves_separate_groups(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Extras" / "Textless" / "001.png")
    touch(tmp_path / "Extras" / "Textless" / "002.png")
    touch(tmp_path / "Extras" / "Extra Angles" / "angle1.png")

    result = scan_folder(tmp_path)

    assert [(group.name, str(group.relative_path)) for group in result.extras] == [
        ("Extra Angles", "Extras/Extra Angles"),
        ("Textless", "Extras/Textless"),
    ]
    assert [len(group.media) for group in result.extras] == [1, 2]


def test_extra_folder_with_direct_and_nested_media_keeps_both_groups(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Extras" / "cover.png")
    touch(tmp_path / "Extras" / "Textless" / "001.png")

    result = scan_folder(tmp_path)

    assert [(group.name, str(group.relative_path)) for group in result.extras] == [
        ("Extras", "Extras"),
        ("Textless", "Extras/Textless"),
    ]



def test_one_issue_plus_series_extra_is_not_unwrapped(tmp_path: Path) -> None:
    touch(tmp_path / "1" / "001.jpg")
    touch(tmp_path / "1" / "Textless" / "001.png")
    touch(tmp_path / "Covers" / "cover.png")

    result = scan_folder(tmp_path)

    assert result.is_series_candidate
    assert [issue.name for issue in result.issues] == ["1"]
    assert [group.name for group in result.issues[0].extras] == ["Textless"]
    assert [group.name for group in result.extras] == ["Covers"]


def test_nested_extra_below_page_container_is_not_flattened_into_primary(tmp_path: Path) -> None:
    touch(tmp_path / "Pages" / "001.jpg")
    touch(tmp_path / "Pages" / "002.jpg")
    touch(tmp_path / "Pages" / "Extras" / "bonus.png")

    result = scan_folder(tmp_path)

    assert result.primary is not None
    assert [str(item.relative_path) for item in result.primary.media] == ["001.jpg", "002.jpg"]
    assert [(group.name, str(group.relative_path)) for group in result.extras] == [
        ("Extras", "Extras")
    ]
    assert [item.relative_path.name for item in result.extras[0].media] == ["bonus.png"]


def test_series_with_nested_extras_under_every_issue_keeps_them_separate(tmp_path: Path) -> None:
    for issue_number in (1, 2, 3):
        issue = tmp_path / f"issue {issue_number}"
        touch(issue / "001.jpg")
        touch(issue / "002.jpg")
        touch(issue / "extras" / "bonus-1.png")
        touch(issue / "extras" / "bonus-2.png")

    result = scan_folder(tmp_path)

    assert result.is_series_candidate
    assert [issue.name for issue in result.issues] == ["issue 1", "issue 2", "issue 3"]
    for issue in result.issues:
        assert issue.primary is not None
        assert [item.relative_path.name for item in issue.primary.media] == ["001.jpg", "002.jpg"]
        assert [(group.name, len(group.media)) for group in issue.extras] == [("extras", 2)]
        assert all("extras" not in item.relative_path.parts for item in issue.primary.media)



def test_series_issue_sibling_folder_defaults_to_extra_when_direct_pages_exist(tmp_path: Path) -> None:
    for number in (1, 2):
        issue = tmp_path / f"issue {number}"
        touch(issue / "001.jpg")
        touch(issue / "002.jpg")
    touch(tmp_path / "issue 1" / "Alternate Material" / "alt.png")

    result = scan_folder(tmp_path)

    first = result.issues[0]
    assert first.primary is not None
    assert [item.relative_path.name for item in first.primary.media] == ["001.jpg", "002.jpg"]
    assert [(group.name, str(group.relative_path)) for group in first.extras] == [
        ("Alternate Material", "issue 1/Alternate Material")
    ]

def test_pages_container_still_folds_into_primary(tmp_path: Path) -> None:
    touch(tmp_path / "Pages" / "001.jpg")
    touch(tmp_path / "Pages" / "002.jpg")
    touch(tmp_path / "Pages" / "Extras" / "bonus.png")

    result = scan_folder(tmp_path)

    assert result.primary is not None
    assert [item.relative_path.name for item in result.primary.media] == ["001.jpg", "002.jpg"]
    assert [(group.name, len(group.media)) for group in result.extras] == [("Extras", 1)]
