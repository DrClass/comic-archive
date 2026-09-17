from pathlib import Path
import pytest

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
    assert [item.relative_path.as_posix() for item in result.primary.media] == [
        "1.jpg",
        "2.png",
        "10.mp4",
    ]
    assert result.primary.media[-1].media_kind is MediaKind.VIDEO

    assert [group.name for group in result.extras] == ["Bonus", "Textless"]
    assert [item.relative_path.as_posix() for item in result.extras[0].media] == [
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


def test_webp_scanning_order_extras_and_bulk_discovery(tmp_path: Path) -> None:
    from comic_archive.importer.bulk import discover_artist_comics

    comic = tmp_path / "Comic"
    for name in ("10.webp", "2.WEBP", "2.gif", "2.mp4", "Covers/cover.WeBp"):
        touch(comic / name)
    result = scan_folder(comic)
    assert result.primary is not None
    assert [item.relative_path.name for item in result.primary.media] == [
        "2.WEBP", "2.gif", "2.mp4", "10.webp",
    ]
    pages = [result.primary.media[0], result.primary.media[-1], result.extras[0].media[0]]
    assert all(item.media_kind is MediaKind.IMAGE for item in pages)
    assert all(item.mime_type == "image/webp" for item in pages)
    assert result.extras[0].name == "Covers"
    assert not result.ignored_files
    _, candidates = discover_artist_comics(tmp_path)
    assert [(item.name, item.media_count) for item in candidates] == [("Comic", 5)]


def test_nested_extra_folders_stay_separate_groups(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Extras" / "Renders" / "1.png")
    touch(tmp_path / "Extras" / "Animations" / "2.mp4")

    result = scan_folder(tmp_path)

    assert [(group.name, group.relative_path.as_posix()) for group in result.extras] == [
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
    assert [item.relative_path.as_posix() for item in without_hint.primary.media] == [
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
    assert [item.relative_path.as_posix() for item in result.primary.media] == [
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

    assert [(group.name, group.relative_path.as_posix()) for group in result.extras] == [
        ("Extra Angles", "Extras/Extra Angles"),
        ("Textless", "Extras/Textless"),
    ]
    assert [len(group.media) for group in result.extras] == [1, 2]


def test_extra_folder_with_direct_and_nested_media_keeps_both_groups(tmp_path: Path) -> None:
    touch(tmp_path / "001.jpg")
    touch(tmp_path / "Extras" / "cover.png")
    touch(tmp_path / "Extras" / "Textless" / "001.png")

    result = scan_folder(tmp_path)

    assert [(group.name, group.relative_path.as_posix()) for group in result.extras] == [
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
    assert [item.relative_path.as_posix() for item in result.primary.media] == ["001.jpg", "002.jpg"]
    assert [(group.name, group.relative_path.as_posix()) for group in result.extras] == [
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
    assert [(group.name, group.relative_path.as_posix()) for group in first.extras] == [
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


def test_pdf_is_expanded_into_ordered_jpeg_pages(tmp_path):
    import fitz

    source = tmp_path / "PDF Comic"
    source.mkdir()
    pdf_path = source / "comic.pdf"
    doc = fitz.open()
    for label in ("Page one", "Page two", "Page three"):
        page = doc.new_page(width=300, height=400)
        page.insert_text((40, 80), label)
    doc.save(pdf_path)
    doc.close()

    cache = tmp_path / "pdf-cache"
    result = scan_folder(source, pdf_cache_root=cache)

    assert result.primary is not None
    assert len(result.primary.media) == 3
    assert [item.mime_type for item in result.primary.media] == ["image/jpeg"] * 3
    assert [item.relative_path.as_posix() for item in result.primary.media] == [
        "comic_pdf_pages/0001.jpg",
        "comic_pdf_pages/0002.jpg",
        "comic_pdf_pages/0003.jpg",
    ]
    assert all(item.path.suffix == ".jpg" and item.path.is_file() for item in result.primary.media)
    assert Path(pdf_path).read_bytes().startswith(b"%PDF")


def test_pdf_directly_extracts_single_full_page_jpeg_without_reencoding(tmp_path, caplog):
    fitz = pytest.importorskip("fitz")
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "Direct JPEG Comic"
    source.mkdir()
    original = source / "original.jpg"
    image_module.new("RGB", (900, 1200), (80, 120, 160)).save(original, format="JPEG", quality=91)
    original_bytes = original.read_bytes()

    pdf_path = source / "comic.pdf"
    doc = fitz.open()
    page = doc.new_page(width=900, height=1200)
    page.insert_image(page.rect, filename=str(original))
    doc.save(pdf_path)
    doc.close()
    original.unlink()

    caplog.set_level("INFO", logger="comic_archive.scanner")
    result = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")

    assert result.primary is not None
    assert len(result.primary.media) == 1
    media = result.primary.media[0]
    assert media.mime_type == "image/jpeg"
    assert media.relative_path.as_posix() == "comic_pdf_pages/0001.jpg"
    assert media.path.read_bytes() == original_bytes
    summary = next(record.getMessage() for record in caplog.records if "pdf render complete" in record.getMessage())
    assert "extracted=1" in summary
    assert "rendered=0" in summary


def test_pdf_direct_extraction_cache_reuses_original_extension(tmp_path, caplog):
    fitz = pytest.importorskip("fitz")
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "Direct Cache Comic"
    source.mkdir()
    original = source / "original.png"
    image_module.new("RGB", (400, 600), (25, 50, 75)).save(original, format="PNG")

    pdf_path = source / "comic.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=600)
    page.insert_image(page.rect, filename=str(original))
    doc.save(pdf_path)
    doc.close()
    original.unlink()
    cache = tmp_path / "pdf-cache"

    first = scan_folder(source, pdf_cache_root=cache)
    assert first.primary is not None
    assert first.primary.media[0].path.suffix == ".png"

    caplog.clear()
    caplog.set_level("INFO", logger="comic_archive.scanner")
    second = scan_folder(source, pdf_cache_root=cache)

    assert second.primary is not None
    assert second.primary.media[0].path.suffix == ".png"
    summary = next(record.getMessage() for record in caplog.records if "pdf render complete" in record.getMessage())
    assert "extracted=0" in summary
    assert "rendered=0" in summary
    assert "cached=1" in summary


def test_pdf_page_with_text_overlay_falls_back_to_rendered_jpeg(tmp_path, caplog):
    fitz = pytest.importorskip("fitz")
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "Overlay Comic"
    source.mkdir()
    original = source / "original.png"
    image_module.new("RGB", (600, 800), (40, 80, 120)).save(original, format="PNG")

    pdf_path = source / "overlay.pdf"
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_image(page.rect, filename=str(original))
    page.insert_text((30, 50), "PDF overlay must be preserved")
    doc.save(pdf_path)
    doc.close()
    original.unlink()

    caplog.set_level("INFO", logger="comic_archive.scanner")
    result = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")

    assert result.primary is not None
    media = result.primary.media[0]
    assert media.mime_type == "image/jpeg"
    assert media.relative_path.as_posix() == "overlay_pdf_pages/0001.jpg"
    assert media.path.read_bytes().startswith(b"\xff\xd8\xff")
    summary = next(record.getMessage() for record in caplog.records if "pdf render complete" in record.getMessage())
    assert "extracted=0" in summary
    assert "rendered=1" in summary


def test_pdf_directly_extracts_single_full_page_png_and_preserves_type(tmp_path, caplog):
    fitz = pytest.importorskip("fitz")
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "Direct PNG Comic"
    source.mkdir()
    original = source / "original.png"
    image_module.new("RGB", (500, 700), (10, 20, 30)).save(original, format="PNG")

    pdf_path = source / "comic.pdf"
    doc = fitz.open()
    page = doc.new_page(width=500, height=700)
    page.insert_image(page.rect, filename=str(original))
    doc.save(pdf_path)
    doc.close()
    original.unlink()

    caplog.set_level("INFO", logger="comic_archive.scanner")
    result = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")

    assert result.primary is not None
    media = result.primary.media[0]
    assert media.mime_type == "image/png"
    assert media.relative_path.as_posix() == "comic_pdf_pages/0001.png"
    assert media.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    summary = next(record.getMessage() for record in caplog.records if "pdf render complete" in record.getMessage())
    assert "extracted=1" in summary
    assert "rendered=0" in summary


def test_pdf_render_reports_page_progress_and_timing_summary(tmp_path, caplog):
    fitz = pytest.importorskip("fitz")
    source = tmp_path / "PDF Progress Comic"
    source.mkdir()
    pdf_path = source / "progress.pdf"
    doc = fitz.open()
    for _ in range(3):
        doc.new_page(width=240, height=320)
    doc.save(pdf_path)
    doc.close()

    events = []
    caplog.set_level("INFO", logger="comic_archive.scanner")
    scan_folder(
        source,
        pdf_cache_root=tmp_path / "pdf-cache",
        progress=lambda phase, current, total, message: events.append((phase, current, total, message)),
    )

    pdf_events = [event for event in events if event[0] == "rendering_pdf"]
    assert [(event[1], event[2]) for event in pdf_events] == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert all(event[3] == "Processing PDF: progress.pdf" for event in pdf_events)

    summary = next(record.getMessage() for record in caplog.records if "pdf render complete" in record.getMessage())
    assert "pages=3" in summary
    assert "extracted=0" in summary
    assert "rendered=3" in summary
    assert "cached=0" in summary
    assert "load_elapsed=" in summary
    assert "extract_elapsed=" in summary
    assert "raster_elapsed=" in summary
    assert "save_elapsed=" in summary
    assert "output_bytes=" in summary


def test_pdf_render_progress_reports_cache_reuse(tmp_path, caplog):
    fitz = pytest.importorskip("fitz")
    source = tmp_path / "PDF Cache Comic"
    source.mkdir()
    pdf_path = source / "cached.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=300)
    doc.new_page(width=200, height=300)
    doc.save(pdf_path)
    doc.close()
    cache = tmp_path / "pdf-cache"

    scan_folder(source, pdf_cache_root=cache)
    caplog.clear()
    caplog.set_level("INFO", logger="comic_archive.scanner")
    events = []
    scan_folder(
        source,
        pdf_cache_root=cache,
        progress=lambda phase, current, total, message: events.append((phase, current, total, message)),
    )

    assert [(current, total) for phase, current, total, _ in events if phase == "rendering_pdf"] == [
        (0, 2), (1, 2), (2, 2)
    ]
    summary = next(record.getMessage() for record in caplog.records if "pdf render complete" in record.getMessage())
    assert "rendered=0" in summary
    assert "cached=2" in summary


def test_pdf_inside_issue_with_extras_stays_primary_and_extras_stay_separate(tmp_path):
    import fitz

    series = tmp_path / "Comic"
    issue1 = series / "Issue 1"
    issue2 = series / "Issue 2"
    extras = issue1 / "Extras"
    extras.mkdir(parents=True)
    issue2.mkdir(parents=True)
    (extras / "bonus.png").write_bytes(b"bonus")
    (issue2 / "001.png").write_bytes(b"page")

    doc = fitz.open()
    doc.new_page(width=200, height=300)
    doc.new_page(width=200, height=300)
    doc.save(issue1 / "issue.pdf")
    doc.close()

    result = scan_folder(series, pdf_cache_root=tmp_path / "pdf-cache")
    first = next(issue for issue in result.issues if issue.name == "Issue 1")
    assert first.primary is not None
    assert len(first.primary.media) == 2
    assert all(item.mime_type == "image/jpeg" for item in first.primary.media)
    assert len(first.extras) == 1
    assert first.extras[0].name == "Extras"
    assert len(first.extras[0].media) == 1


def test_multiple_root_pdfs_seed_separate_issues(tmp_path: Path):
    fitz = pytest.importorskip("fitz")
    source = tmp_path / "Amazing Comic"
    source.mkdir()
    for name in ("Amazing Comic 1.pdf", "Amazing Comic 2.pdf"):
        doc = fitz.open()
        doc.new_page()
        doc.save(source / name)
        doc.close()
    scan = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")
    assert scan.is_series_candidate
    assert [issue.name for issue in scan.issues] == ["Amazing Comic 1", "Amazing Comic 2"]
    assert all(len(issue.primary.media) == 1 for issue in scan.issues)


def test_loose_cover_does_not_flatten_issue_folders(tmp_path: Path):
    source = tmp_path / "Amazing Comic"
    for chapter in ("Chapter 1", "Chapter 2", "Chapter 3"):
        target = source / chapter / "001.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(chapter.encode())
    (source / "cover.png").write_bytes(b"cover")
    scan = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")
    assert scan.is_series_candidate
    assert [issue.name for issue in scan.issues] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert len(scan.extras) == 1
    assert scan.extras[0].name == "Series extras"
    assert [item.relative_path.as_posix() for item in scan.extras[0].media] == ["cover.png"]


def test_scan_uses_single_pass_index_without_recursive_rglob(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Indexed Comic"
    for issue in range(1, 8):
        for page in range(1, 6):
            touch(source / f"Issue {issue}" / f"{page:03}.jpg")
    touch(source / "Issue 1" / "Extras" / "bonus.png")

    def fail_rglob(*args, **kwargs):
        raise AssertionError("normal scan must not recursively rglob subtrees")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    result = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")
    assert result.is_series_candidate
    assert len(result.issues) == 7


def test_scan_progress_reports_index_and_completion(tmp_path: Path) -> None:
    source = tmp_path / "Progress Comic"
    touch(source / "001.jpg")
    events = []

    scan_folder(
        source,
        pdf_cache_root=tmp_path / "pdf-cache",
        progress=lambda phase, current, total, message: events.append((phase, current, total, message)),
    )

    assert events[0][0] == "indexing"
    assert any(event[0] == "classifying" for event in events)
    assert events[-1][0] == "complete"


def test_indexed_scan_does_not_reenumerate_directories_after_index(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "No Rewalk Comic"
    for issue in range(1, 5):
        for page in range(1, 8):
            touch(source / f"Issue {issue}" / f"{page:03}.jpg")
    touch(source / "Issue 1" / "Extras" / "bonus.png")

    def fail_iterdir(*args, **kwargs):
        raise AssertionError("classification must use ScanIndex rather than Path.iterdir()")

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    result = scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")
    assert result.is_series_candidate
    assert len(result.issues) == 4


def test_scan_progress_reports_finalizing_phase(tmp_path: Path) -> None:
    source = tmp_path / "Progress Detail Comic"
    for issue in range(1, 3):
        for page in range(1, 4):
            touch(source / f"Issue {issue}" / f"{page:03}.jpg")
    phases = []

    scan_folder(
        source,
        pdf_cache_root=tmp_path / "pdf-cache",
        progress=lambda phase, current, total, message: phases.append(phase),
    )

    assert "indexing" in phases
    assert "classifying" in phases
    assert "finalizing" in phases
    assert phases[-1] == "complete"
