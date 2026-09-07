from pathlib import Path
import sqlite3
import time
import re
import json

from fastapi.testclient import TestClient
from PIL import Image

from comic_archive.importer.commit import commit_staged_import
from comic_archive.importer.review import build_review_plan
from comic_archive.importer.scanner import scan_folder
from comic_archive.importer.staging import build_staged_import
from comic_archive.web import create_app
from comic_archive.library import read_library
from comic_archive.auth import create_user, list_users




def _csrf_token(client: TestClient, *, login: bool = False) -> str:
    page = client.get("/login" if login else "/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, page.text
    return match.group(1)


def _post(client: TestClient, url: str, *, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client, login=(url == "/login")))
    return client.post(url, data=payload, **kwargs)


def _admin_client(database: Path, library: Path, staging: Path | None = None) -> TestClient:
    try:
        create_user(database, "test-admin", "test-password-123", is_admin=True)
    except Exception:
        pass
    app = create_app(database, library, staging or (database.parent / "staging"))
    client = TestClient(app)
    response = _post(client, 
        "/login",
        data={"username": "test-admin", "password": "test-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _make_library(tmp_path: Path):
    source = tmp_path / "source" / "Issue 1"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page-one")
    extras = source / "Covers"
    extras.mkdir()
    (extras / "cover.png").write_bytes(b"cover")

    scan = scan_folder(source)
    plan = build_review_plan(scan)
    staged = build_staged_import(
        plan,
        author="Example Artist",
        series="Example Comic",
        issue_metadata={".": {"issue_number": "1", "title": "Opening", "complete": True}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    return database, library, result


def test_home_author_series_issue_navigation(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    response = client.get("/")
    assert response.status_code == 200
    assert "Example Artist" in response.text
    assert "1 authors" in response.text

    response = client.get(f"/authors/{result.author_id}")
    assert response.status_code == 200
    assert "Example Comic" in response.text

    response = client.get(f"/series/{result.series_id}")
    assert response.status_code == 200
    assert ">1</h3>" in response.text

    response = client.get(f"/issues/{result.issue_ids[0]}")
    assert response.status_code == 200
    assert "Opening" in response.text
    assert "Covers" in response.text
    assert 'class="page-number">1<' in response.text
    assert "001.jpg" not in response.text
    assert response.text.count('href="/read/' + result.issue_ids[0] + '"') == 1


def test_unknown_pages_return_404(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    client = _admin_client(database, library)
    assert client.get("/authors/not-real").status_code == 404
    assert client.get("/series/not-real").status_code == 404
    assert client.get("/issues/not-real").status_code == 404


def test_health(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    client = _admin_client(database, library)
    assert client.get("/health").json() == {"ok": True}


def test_reader_and_media_serving(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)
    issue_id = result.issue_ids[0]

    issue = client.get(f"/issues/{issue_id}")
    assert issue.status_code == 200
    assert f'/read/{issue_id}' in issue.text

    reader = client.get(f"/read/{issue_id}")
    assert reader.status_code == 200
    assert "1 / 1" in reader.text
    assert 'id="reader-next"' in reader.text

    from comic_archive.library import read_library
    media_id = read_library(database)[0].series[0].issues[0].groups[0].media[0].id
    response = client.get(f"/media/{media_id}")
    assert response.status_code == 200
    assert response.content == b"page-one"
    assert response.headers["content-type"].startswith("image/jpeg")


def test_reader_clamps_page_and_unknown_media_404(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)
    issue_id = result.issue_ids[0]
    response = client.get(f"/read/{issue_id}?page=999")
    assert response.status_code == 200
    assert "1 / 1" in response.text
    assert client.get("/media/not-real").status_code == 404


def test_issue_extra_can_be_read(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    from comic_archive.library import read_library
    issue = read_library(database)[0].series[0].issues[0]
    extra = next(group for group in issue.groups if group.role != "primary")

    details = client.get(f"/issues/{issue.id}")
    assert f'/groups/{extra.id}' in details.text
    assert f'/read-group/{extra.id}' not in details.text

    extra_details = client.get(f"/groups/{extra.id}")
    assert extra_details.status_code == 200
    assert f'/read-group/{extra.id}' in extra_details.text

    reader = client.get(f"/read-group/{extra.id}")
    assert reader.status_code == 200
    assert extra.name in reader.text
    assert "1 / 1" in reader.text
    assert f'/groups/{extra.id}' in reader.text


def test_series_extra_can_be_read(tmp_path: Path):
    source = tmp_path / "source" / "Example Comic"
    issue_dir = source / "Issue 1"
    issue_dir.mkdir(parents=True)
    (issue_dir / "001.jpg").write_bytes(b"page-one")
    covers = source / "Covers"
    covers.mkdir()
    (covers / "cover.png").write_bytes(b"series-cover")

    scan = scan_folder(source)
    plan = build_review_plan(scan)
    staged = build_staged_import(
        plan,
        author="Example Artist",
        series="Example Comic",
        issue_metadata={"Issue 1": {"issue_number": "1", "title": "Opening", "complete": True}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    from comic_archive.library import read_library
    series = read_library(database)[0].series[0]
    assert series.extras
    extra = series.extras[0]

    details = client.get(f"/series/{result.series_id}")
    assert f'/groups/{extra.id}' in details.text
    assert f'/read-group/{extra.id}' not in details.text

    extra_details = client.get(f"/groups/{extra.id}")
    assert extra_details.status_code == 200
    assert f'/read-group/{extra.id}' in extra_details.text

    reader = client.get(f"/read-group/{extra.id}")
    assert reader.status_code == 200
    assert extra.name in reader.text
    assert "1 / 1" in reader.text
    assert f'/groups/{extra.id}' in reader.text


def test_web_import_folder_flow(tmp_path: Path):
    source = tmp_path / "incoming" / "Issue 2"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page-two")
    extra = source / "Textless"
    extra.mkdir()
    (extra / "001.png").write_bytes(b"textless")

    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = client.get("/import")
    assert response.status_code == 200
    assert "Import folder" in response.text

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    assert response.status_code == 303
    review_url = response.headers["location"]
    assert review_url.endswith("/review")

    review = client.get(review_url)
    assert review.status_code == 200
    assert "Textless" in review.text
    session_id = review_url.split("/")[2]

    response = _post(client, 
        review_url,
        data={
            "name_0": "Primary content",
            "role_0": "primary",
            "name_1": "Textless",
            "role_1": "issue-extra",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/metadata")

    metadata_url = response.headers["location"]
    response = _post(client, 
        metadata_url,
        data={
            "author": "Example Artist",
            "series": "Example Comic",
            "issue_number_0": "2",
            "title_0": "Second",
            "complete_0": "yes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    organize_url = response.headers["location"]
    assert organize_url.endswith("/organize")
    assert list(staging.glob("*.json"))

    organize = client.get(organize_url)
    assert organize.status_code == 200
    assert "Organize files" in organize.text
    response = _post(client, organize_url, data={"action": "continue"}, follow_redirects=False)
    assert response.status_code == 303
    confirm_url = response.headers["location"]
    assert confirm_url.endswith("/confirm")

    confirm = client.get(confirm_url)
    assert confirm.status_code == 200
    assert "Example Artist" in confirm.text
    assert "Example Comic" in confirm.text

    commit_url = f"/import/{session_id}/commit"
    done = _post(client, commit_url, data={})
    assert done.status_code == 200
    assert "Import complete" in done.text

    authors = read_library(database)
    issue = authors[0].series[0].issues[0]
    assert issue.issue_number == "2"
    assert any(group.name == "Textless" for group in issue.groups)


def test_web_duplicate_warning_can_be_overridden(tmp_path: Path):
    source = tmp_path / "source" / "Issue 1"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"same-page")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"

    scan = scan_folder(source)
    plan = build_review_plan(scan)
    first = build_staged_import(plan, author="Artist", series="Comic", issue_metadata={".": {"issue_number": "1"}})
    commit_staged_import(first, library_root=library, database_path=database)

    client = _admin_client(database, library, staging)
    scan_response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    review_url = scan_response.headers["location"]
    session_id = review_url.split("/")[2]
    response = _post(client, review_url, data={"name_0": "Primary content", "role_0": "primary"}, follow_redirects=False)
    metadata_url = response.headers["location"]
    response = _post(client, metadata_url, data={"author": "Artist", "series": "Comic", "issue_number_0": "1", "title_0": "", "complete_0": ""}, follow_redirects=False)
    assert response.status_code == 303

    commit_url = f"/import/{session_id}/commit"
    warning = _post(client, commit_url, data={})
    assert warning.status_code == 409
    assert "Possible duplicate import detected" in warning.text
    assert "Import anyway despite duplicate warning" in warning.text

    override = _post(client, commit_url, data={"allow_duplicate": "yes"})
    assert override.status_code == 200
    assert "Import complete" in override.text


def test_web_unlabeled_single_issue_is_shown_as_one_shot(tmp_path: Path):
    source = tmp_path / "source" / "Standalone"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"

    scan = scan_folder(source)
    plan = build_review_plan(scan)
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Standalone Comic",
        issue_metadata={".": {"issue_number": "", "title": "", "complete": True}},
    )
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    series_page = client.get(f"/series/{result.series_id}")
    assert series_page.status_code == 200
    assert "One-shot" in series_page.text
    assert "Unlabeled issue" not in series_page.text

    issue_page = client.get(f"/issues/{result.issue_ids[0]}")
    assert issue_page.status_code == 200
    assert "<h1>One-shot</h1>" in issue_page.text


def test_web_edit_author_series_and_issue(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    response = _post(client, 
        f"/authors/{result.author_id}/edit",
        data={"name": "Renamed Artist"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = _post(client, 
        f"/series/{result.series_id}/edit",
        data={"title": "Renamed Comic", "author_id": result.author_id},
        follow_redirects=False,
    )
    assert response.status_code == 303

    issue_id = result.issue_ids[0]
    response = _post(client, 
        f"/issues/{issue_id}/edit",
        data={
            "issue_number": "1.5",
            "title": "Corrected Opening",
            "complete": "no",
            "series_id": result.series_id,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    authors = read_library(database)
    assert authors[0].name == "Renamed Artist"
    assert authors[0].series[0].title == "Renamed Comic"
    issue = authors[0].series[0].issues[0]
    assert issue.issue_number == "1.5"
    assert issue.title == "Corrected Opening"
    assert issue.complete is False

    history = client.get("/history")
    assert history.status_code == 200
    assert "Renamed Artist" in history.text
    assert "Corrected Opening" in history.text


def test_web_edit_extra_name_and_owner(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    authors = read_library(database)
    issue = authors[0].series[0].issues[0]
    extra = next(group for group in issue.groups if group.role != "primary")

    edit_page = client.get(f"/groups/{extra.id}/edit")
    assert edit_page.status_code == 200
    assert "Covers" in edit_page.text

    response = _post(client, 
        f"/groups/{extra.id}/edit",
        data={"name": "Alternate Covers", "owner": "series"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    refreshed = read_library(database)[0].series[0]
    assert any(group.name == "Alternate Covers" for group in refreshed.extras)
    assert all(group.name != "Alternate Covers" for group in refreshed.issues[0].groups)


def test_web_media_remove_restore_and_reorder(tmp_path: Path):
    source = tmp_path / "source" / "Issue 1"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"one")
    (source / "002.jpg").write_bytes(b"two")
    scan = scan_folder(source)
    plan = build_review_plan(scan)
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Comic",
        issue_metadata={".": {"issue_number": "1"}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    issue = read_library(database)[0].series[0].issues[0]
    group = next(group for group in issue.groups if group.role == "primary")
    first, second = group.media

    response = _post(client, 
        f"/groups/{group.id}/edit",
        data={
            f"position_{first.id}": "2",
            f"position_{second.id}": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    reordered = read_library(database)[0].series[0].issues[0].groups[0].media
    assert [m.id for m in reordered] == [second.id, first.id]

    response = _post(client, 
        f"/media/{second.id}/remove",
        params={"group_id": group.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    active = read_library(database)[0].series[0].issues[0].groups[0].media
    assert [m.id for m in active] == [first.id]

    page = client.get(f"/groups/{group.id}/edit")
    assert "removed" in page.text

    response = _post(client, 
        f"/media/{second.id}/restore",
        params={"group_id": group.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    restored = read_library(database)[0].series[0].issues[0].groups[0].media
    assert {m.id for m in restored} == {first.id, second.id}


def test_anonymous_users_only_see_login(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = TestClient(create_app(database, library))

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == "/login"

    issue = client.get(f"/issues/{result.issue_ids[0]}", follow_redirects=False)
    assert issue.status_code == 303
    assert issue.headers["location"] == "/login"

    media_id = read_library(database)[0].series[0].issues[0].groups[0].media[0].id
    media = client.get(f"/media/{media_id}", follow_redirects=False)
    assert media.status_code == 303
    assert media.headers["location"] == "/login"

    health = client.get("/health", follow_redirects=False)
    assert health.status_code == 303
    assert health.headers["location"] == "/login"

    assert client.get("/docs", follow_redirects=False).status_code == 303
    assert client.get("/openapi.json", follow_redirects=False).status_code == 303

    login = client.get("/login")
    assert login.status_code == 200
    assert "Comic Archive" in login.text
    assert "Example Artist" not in login.text




def test_anonymous_protected_request_preserves_login_csrf(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    client = TestClient(create_app(database, library))

    first_token = _csrf_token(client, login=True)
    protected = client.get("/health", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"

    second_token = _csrf_token(client, login=True)
    assert second_token == first_token


def test_favicon_does_not_rotate_login_csrf_and_secure_login_succeeds(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    create_user(database, "reader", "reader-password-123", is_admin=False)
    client = TestClient(
        create_app(database, library, secure_cookies=True),
        base_url="https://comics.example.test",
    )

    login_page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, login_page.text
    token = match.group(1)

    favicon = client.get("/favicon.ico", follow_redirects=False)
    assert favicon.status_code == 204

    after_favicon = client.get("/login")
    after_match = re.search(r'name="csrf_token" value="([^"]+)"', after_favicon.text)
    assert after_match, after_favicon.text
    assert after_match.group(1) == token

    login = client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "reader",
            "password": "reader-password-123",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/"


def test_regular_user_can_read_but_not_administer(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    create_user(database, "reader", "reader-password-123", is_admin=False)
    client = TestClient(create_app(database, library))
    login = _post(client, 
        "/login",
        data={"username": "reader", "password": "reader-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    home = client.get("/")
    assert home.status_code == 200
    assert "Example Artist" in home.text
    assert "Import" not in home.text
    assert "Accounts" not in home.text

    issue = client.get(f"/issues/{result.issue_ids[0]}")
    assert issue.status_code == 200
    assert "Edit issue" not in issue.text

    assert client.get("/import", follow_redirects=False).status_code == 403
    assert client.get("/history", follow_redirects=False).status_code == 403
    assert client.get("/admin/users", follow_redirects=False).status_code == 403


def test_admin_can_create_account_without_public_signup(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    client = _admin_client(database, library)

    assert client.get("/register", follow_redirects=False).status_code == 404

    page = client.get("/admin/users")
    assert page.status_code == 200
    assert "test-admin" in page.text

    created = _post(client, 
        "/admin/users",
        data={"username": "friend", "password": "friend-password-123"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin/users"

    # New account can log in.
    other = TestClient(create_app(database, library))
    login = _post(other, 
        "/login",
        data={"username": "friend", "password": "friend-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert other.get("/").status_code == 200


def test_csrf_is_required_for_state_changes(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    create_user(database, "reader", "reader-password-123", is_admin=False)
    client = TestClient(create_app(database, library))

    bad_login = client.post(
        "/login",
        data={"username": "reader", "password": "reader-password-123"},
        follow_redirects=False,
    )
    assert bad_login.status_code == 403

    assert _post(
        client, "/login",
        data={"username": "reader", "password": "reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    bad_logout = client.post("/logout", data={}, follow_redirects=False)
    assert bad_logout.status_code == 403
    assert client.get("/").status_code == 200


def test_user_can_change_password_and_old_session_is_invalidated(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    create_user(database, "reader", "reader-password-123", is_admin=False)

    first = TestClient(create_app(database, library))
    second = TestClient(create_app(database, library))
    for client in (first, second):
        assert _post(
            client, "/login",
            data={"username": "reader", "password": "reader-password-123"},
            follow_redirects=False,
        ).status_code == 303

    changed = _post(
        first, "/account/password",
        data={
            "current_password": "reader-password-123",
            "new_password": "reader-new-password-456",
            "confirm_password": "reader-new-password-456",
        },
    )
    assert changed.status_code == 200
    assert "Password changed" in changed.text

    assert first.get("/").status_code == 200
    stale = second.get("/", follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/login"

    fresh = TestClient(create_app(database, library))
    assert _post(
        fresh, "/login",
        data={"username": "reader", "password": "reader-new-password-456"},
        follow_redirects=False,
    ).status_code == 303


def test_admin_can_reset_and_disable_account(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    create_user(database, "reader", "reader-password-123", is_admin=False)
    admin = _admin_client(database, library)
    reader_user = next(user for user in list_users(database) if user.username == "reader")

    reader = TestClient(create_app(database, library))
    assert _post(
        reader, "/login",
        data={"username": "reader", "password": "reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    reset = _post(
        admin, f"/admin/users/{reader_user.id}/reset-password",
        data={"password": "admin-reset-password-456"},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert reader.get("/", follow_redirects=False).status_code == 303

    disabled = _post(
        admin, f"/admin/users/{reader_user.id}/toggle-active",
        data={}, follow_redirects=False,
    )
    assert disabled.status_code == 303
    attempt = TestClient(create_app(database, library))
    failed = _post(
        attempt, "/login",
        data={"username": "reader", "password": "admin-reset-password-456"},
        follow_redirects=False,
    )
    assert failed.status_code == 401


def test_login_throttles_repeated_failures(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    create_user(database, "reader", "reader-password-123", is_admin=False)
    client = TestClient(create_app(database, library))

    for _ in range(5):
        response = _post(
            client, "/login",
            data={"username": "reader", "password": "wrong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 401

    blocked = _post(
        client, "/login",
        data={"username": "reader", "password": "reader-password-123"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429


def test_thumbnail_grid_uses_small_authenticated_derivatives(tmp_path: Path):
    source = tmp_path / "source" / "Issue 1"
    source.mkdir(parents=True)
    Image.new("RGB", (1600, 2400), "white").save(source / "001.jpg", quality=95)
    (source / "002.mp4").write_bytes(b"fake-video")

    scan = scan_folder(source)
    plan = build_review_plan(scan)
    staged = build_staged_import(
        plan,
        author="Artist",
        series="Comic",
        issue_metadata={".": {"issue_number": "1"}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    issue = read_library(database)[0].series[0].issues[0]
    image_media = next(m for g in issue.groups for m in g.media if m.mime_type.startswith("image/"))
    video_media = next(m for g in issue.groups for m in g.media if m.mime_type == "video/mp4")

    page = client.get(f"/issues/{result.issue_ids[0]}")
    assert page.status_code == 200
    assert f'/thumbnail/{image_media.id}' in page.text
    assert 'loading="lazy"' in page.text
    assert "VIDEO" in page.text
    assert f'/thumbnail/{video_media.id}' not in page.text

    thumb = client.get(f"/thumbnail/{image_media.id}")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/jpeg")
    assert len(thumb.content) < (library / image_media.stored_path).stat().st_size

    anonymous = TestClient(create_app(database, library))
    protected = anonymous.get(f"/thumbnail/{image_media.id}", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"

    unavailable = client.get(f"/thumbnail/{video_media.id}")
    assert unavailable.status_code == 404


def test_browse_pages_use_square_cover_cards_and_issue_pages_use_natural_previews(tmp_path: Path):
    source = tmp_path / "source" / "Example Comic" / "Issue 1"
    source.mkdir(parents=True)
    Image.new("RGB", (900, 1400), "white").save(source / "001.jpg")
    Image.new("RGB", (1600, 800), "white").save(source / "002.jpg")

    staged = build_staged_import(
        build_review_plan(scan_folder(source.parent)),
        author="Example Artist",
        series="Example Comic",
        issue_metadata={"Issue 1": {"issue_number": "1", "complete": True}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    author_page = client.get(f"/authors/{result.author_id}")
    assert 'class="media-card"' in author_page.text
    assert 'class="media-card-image"' in author_page.text
    assert "/thumbnail/" in author_page.text

    series_page = client.get(f"/series/{result.series_id}")
    assert 'class="media-card"' in series_page.text
    assert "/thumbnail/" in series_page.text

    issue_page = client.get(f"/issues/{result.issue_ids[0]}")
    assert issue_page.text.count('class="page-preview"') == 2
    assert 'class="page-number">1<' in issue_page.text
    assert 'class="page-number">2<' in issue_page.text
    assert "001.jpg" not in issue_page.text
    assert "002.jpg" not in issue_page.text

    base_css = (Path(__file__).parents[1] / "comic_archive" / "templates" / "base.html").read_text()
    assert ".media-card-image { width: 100%; aspect-ratio: 1 / 1;" in base_css
    assert "object-fit: cover;" in base_css
    assert ".page-preview img { display: block; width: 150px; height: auto;" in base_css


def test_issue_editor_links_to_primary_page_editor(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    issue = read_library(database)[0].series[0].issues[0]
    primary = next(group for group in issue.groups if group.role == "primary")

    response = client.get(f"/issues/{issue.id}/edit")
    assert response.status_code == 200
    assert "Comic pages" in response.text
    assert f'/groups/{primary.id}/edit' in response.text

    page_editor = client.get(f"/groups/{primary.id}/edit")
    assert page_editor.status_code == 200
    assert "Edit comic pages" in page_editor.text
    assert "Page order" in page_editor.text
    for media in primary.media:
        assert media.original_relative_path in page_editor.text


def test_issue_can_create_extra_group_and_move_primary_page(tmp_path: Path):
    source = tmp_path / "source" / "Issue 1"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page-one")
    (source / "002.jpg").write_bytes(b"page-two")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Artist",
        series="Comic",
        issue_metadata={".": {"issue_number": "1"}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    issue = read_library(database)[0].series[0].issues[0]
    primary = next(group for group in issue.groups if group.role == "primary")
    moved_media = primary.media[1]

    response = _post(
        client,
        f"/issues/{issue.id}/groups",
        data={"name": "Bonus"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    refreshed = read_library(database)[0].series[0].issues[0]
    bonus = next(group for group in refreshed.groups if group.name == "Bonus")
    assert bonus.role == "issue-extra"

    response = _post(
        client,
        f"/groups/{primary.id}/move-media",
        data={f"move_{moved_media.id}": "yes", "target_group_id": bonus.id},
        follow_redirects=False,
    )
    assert response.status_code == 303

    refreshed = read_library(database)[0].series[0].issues[0]
    primary_after = next(group for group in refreshed.groups if group.role == "primary")
    bonus_after = next(group for group in refreshed.groups if group.name == "Bonus")
    assert [m.original_relative_path for m in primary_after.media] == ["001.jpg"]
    assert [m.original_relative_path for m in bonus_after.media] == ["002.jpg"]


def test_web_import_organizer_can_create_group_and_move_file(tmp_path: Path):
    source = tmp_path / "incoming" / "Issue 7"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page-one")
    (source / "002.jpg").write_bytes(b"page-two")
    (source / "bonus.jpg").write_bytes(b"bonus")

    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    review_url = response.headers["location"]
    session_id = review_url.split("/")[2]

    response = _post(
        client,
        review_url,
        data={"name_0": "Primary content", "role_0": "primary"},
        follow_redirects=False,
    )
    metadata_url = response.headers["location"]
    response = _post(
        client,
        metadata_url,
        data={
            "author": "Artist",
            "series": "Comic",
            "issue_number_0": "7",
            "title_0": "",
            "complete_0": "yes",
        },
        follow_redirects=False,
    )
    organize_url = response.headers["location"]
    assert organize_url.endswith("/organize")

    response = _post(
        client,
        organize_url,
        data={"new_group_0": "Bonus", "action": "create:0"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    session = client.app.state.import_sessions[session_id]
    issue = session.staged.issues[0]
    primary = next(group for group in issue.groups if group.role == "primary")
    bonus = next(group for group in issue.groups if group.name == "Bonus")
    assert len(primary.media) == 3
    assert bonus.media == []

    response = _post(
        client,
        organize_url,
        data={
            "action": "continue",
            "target_0_0": primary.relative_path,
            "target_0_1": primary.relative_path,
            "target_0_2": bonus.relative_path,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/confirm")

    issue = session.staged.issues[0]
    primary = next(group for group in issue.groups if group.role == "primary")
    bonus = next(group for group in issue.groups if group.name == "Bonus")
    assert [m.relative_path for m in primary.media] == ["001.jpg", "002.jpg"]
    assert [m.relative_path for m in bonus.media] == ["bonus.jpg"]

    done = _post(client, f"/import/{session_id}/commit", data={})
    assert done.status_code == 200
    imported = read_library(database)[0].series[0].issues[0]
    imported_bonus = next(group for group in imported.groups if group.name == "Bonus")
    assert [m.original_relative_path for m in imported_bonus.media] == ["bonus.jpg"]


def test_search_finds_authors_series_issues_and_extras(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    author_search = client.get("/search", params={"q": "Example Artist"})
    assert author_search.status_code == 200
    assert "Author" in author_search.text
    assert f'/authors/{result.author_id}' in author_search.text

    series_search = client.get("/search", params={"q": "Example Comic"})
    assert series_search.status_code == 200
    assert "Series" in series_search.text
    assert f'/series/{result.series_id}' in series_search.text

    issue = read_library(database)[0].series[0].issues[0]
    issue_search = client.get("/search", params={"q": "Opening"})
    assert issue_search.status_code == 200
    assert "Issue" in issue_search.text
    assert f'/issues/{issue.id}' in issue_search.text

    extra = next(group for group in issue.groups if group.role != "primary")
    extra_search = client.get("/search", params={"q": extra.name})
    assert extra_search.status_code == 200
    assert "Extra" in extra_search.text
    assert f'/groups/{extra.id}' in extra_search.text


def test_search_is_case_insensitive_partial_and_available_in_header(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    response = client.get("/search", params={"q": "comic"})
    assert response.status_code == 200
    assert "Example Comic" in response.text
    assert 'class="header-search"' in response.text
    assert 'name="q"' in response.text


def test_search_empty_and_no_results_states(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    response = client.get("/search")
    assert response.status_code == 200
    assert "Search author names, series titles" in response.text

    response = client.get("/search", params={"q": "definitely-not-in-library"})
    assert response.status_code == 200
    assert "No results" in response.text


def test_search_does_not_expose_page_filenames(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    response = client.get("/search", params={"q": "001.jpg"})
    assert response.status_code == 200
    assert "No results" in response.text


def test_reading_progress_status_continue_and_finish(tmp_path: Path):
    source = tmp_path / "source" / "Issue 3"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page-one")
    (source / "002.jpg").write_bytes(b"page-two")
    (source / "003.jpg").write_bytes(b"page-three")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Progress Artist",
        series="Progress Comic",
        issue_metadata={".": {"issue_number": "3", "complete": True}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)
    issue_id = result.issue_ids[0]

    issue_page = client.get(f"/issues/{issue_id}")
    assert "Unread" in issue_page.text
    assert "Read issue" in issue_page.text

    reader = client.get(f"/read/{issue_id}?page=2")
    assert reader.status_code == 200
    issue_page = client.get(f"/issues/{issue_id}")
    assert "In progress · page 2" in issue_page.text
    assert "Continue reading · page 2" in issue_page.text

    home = client.get("/")
    assert "Continue reading" in home.text
    assert "Progress Comic" in home.text
    assert "page 2" in home.text

    response = _post(
        client,
        f"/progress/{issue_id}",
        data={"page": "3", "total_pages": "3"},
    )
    assert response.status_code == 200
    assert response.json()["completed"] is True

    issue_page = client.get(f"/issues/{issue_id}")
    assert "Finished" in issue_page.text
    assert "Read again" in issue_page.text
    home = client.get("/")
    assert "Progress Comic" not in home.text

    response = _post(client, f"/progress/{issue_id}/reset", data={}, follow_redirects=False)
    assert response.status_code == 303
    issue_page = client.get(f"/issues/{issue_id}")
    assert "Unread" in issue_page.text


def test_reading_progress_is_per_user(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    first = _admin_client(database, library)
    issue_id = result.issue_ids[0]
    first.get(f"/read/{issue_id}")

    from comic_archive.auth import create_user
    create_user(database, "reader-two", "reader-password-123", is_admin=False)
    second = TestClient(create_app(database, library, database.parent / "staging"))
    login = _post(
        second,
        "/login",
        data={"username": "reader-two", "password": "reader-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = second.get(f"/issues/{issue_id}")
    assert "Unread" in page.text


def test_issue_and_series_show_last_modified_dates(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)
    authors = read_library(database)
    series = authors[0].series[0]
    issue = series.issues[0]
    assert series.updated_at
    assert issue.updated_at

    series_page = client.get(f"/series/{series.id}")
    assert f"Modified {series.updated_at[:10]}" in series_page.text
    assert f"Modified {issue.updated_at[:10]}" in series_page.text

    issue_page = client.get(f"/issues/{issue.id}")
    assert f"Modified {issue.updated_at[:10]}" in issue_page.text

    author_page = client.get(f"/authors/{authors[0].id}")
    assert f"Modified {series.updated_at[:10]}" in author_page.text


def test_content_edit_touches_issue_and_series_modified_timestamp(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    issue_id = result.issue_ids[0]
    series_id = result.series_id

    import sqlite3
    with sqlite3.connect(database) as db:
        db.execute("UPDATE issues SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (issue_id,))
        db.execute("UPDATE series SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (series_id,))
        group_id = db.execute(
            "SELECT id FROM content_groups WHERE issue_id = ? AND role = 'primary'", (issue_id,)
        ).fetchone()[0]
        media_id = db.execute(
            "SELECT id FROM media WHERE group_id = ? ORDER BY position LIMIT 1", (group_id,)
        ).fetchone()[0]
        db.execute("UPDATE media SET active = 0 WHERE id = ?", (media_id,))

    refreshed = read_library(database)[0].series[0]
    issue = refreshed.issues[0]
    assert issue.updated_at > "2000-01-01 00:00:00"
    assert refreshed.updated_at > "2000-01-01 00:00:00"


def test_reader_has_modes_fit_fullscreen_swipe_and_page_navigator(tmp_path: Path):
    source = tmp_path / "source" / "Issue 9"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page-one")
    (source / "002.jpg").write_bytes(b"page-two")
    (source / "003.jpg").write_bytes(b"page-three")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Reader Artist",
        series="Reader Comic",
        issue_metadata={".": {"issue_number": "9", "complete": True}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    response = client.get(f"/read/{result.issue_ids[0]}?page=2")
    assert response.status_code == 200
    assert 'id="reader-mode"' in response.text
    assert "Vertical scroll" in response.text
    assert 'id="reader-fit"' in response.text
    assert "Fit width" in response.text
    assert "Fit height" in response.text
    assert "Original size" in response.text
    assert 'id="reader-fullscreen"' in response.text
    assert 'id="reader-pages-button"' in response.text
    assert 'class="reader-thumbnail' in response.text
    assert "touchstart" in response.text
    assert "touchend" in response.text
    assert "IntersectionObserver" in response.text


def test_single_page_reader_initially_loads_only_current_full_image(tmp_path: Path):
    source = tmp_path / "source" / "Issue 10"
    source.mkdir(parents=True)
    for name in ("001.jpg", "002.jpg", "003.jpg"):
        (source / name).write_bytes(name.encode())
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Lazy Artist",
        series="Lazy Comic",
        issue_metadata={".": {"issue_number": "10"}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    issue = read_library(database)[0].series[0].issues[0]
    primary = next(group for group in issue.groups if group.role == "primary")
    response = client.get(f"/read/{result.issue_ids[0]}?page=2")
    html = response.text

    # All pages retain data-src for JS preloading, but only the current page has
    # an initial full-media src in server-rendered HTML.
    for media in primary.media:
        assert f'data-src="/media/{media.id}"' in html
    import re
    full_src_ids = re.findall(r'<img\s+src="/media/([^"]+)"', html)
    assert full_src_ids == [primary.media[1].id]
    assert "[index - 1, index, index + 1].forEach(loadMedia)" in html


def test_reader_preferences_are_saved_locally(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)
    response = client.get(f"/read/{result.issue_ids[0]}")
    assert response.status_code == 200
    assert "comicArchiveReaderMode" in response.text
    assert "comicArchiveReaderFit" in response.text


def test_series_detects_missing_issue_and_admin_can_mark_intentional(tmp_path: Path):
    source1 = tmp_path / "issue1"
    source1.mkdir()
    (source1 / "001.jpg").write_bytes(b"one")
    staged1 = build_staged_import(
        build_review_plan(scan_folder(source1)),
        author="Gap Artist",
        series="Gap Comic",
        issue_metadata={".": {"issue_number": "1", "complete": True}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result1 = commit_staged_import(staged1, library_root=library, database_path=database)

    source3 = tmp_path / "issue3"
    source3.mkdir()
    (source3 / "001.jpg").write_bytes(b"three")
    staged3 = build_staged_import(
        build_review_plan(scan_folder(source3)),
        author="Gap Artist",
        series="Gap Comic",
        issue_metadata={".": {"issue_number": "3", "complete": True}},
    )
    commit_staged_import(staged3, library_root=library, database_path=database)

    client = _admin_client(database, library)
    page = client.get(f"/series/{result1.series_id}")
    assert "Missing issue numbers" in page.text
    assert "Issue 2" in page.text
    assert "Missing" in page.text

    response = _post(
        client,
        f"/series/{result1.series_id}/missing/2",
        data={"intentional": "yes", "note": "Never released", "return_to": f"/series/{result1.series_id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/series/{result1.series_id}")
    assert "Intentionally unavailable" in page.text
    assert "Never released" in page.text

    response = _post(
        client,
        f"/series/{result1.series_id}/missing/2",
        data={"intentional": "no", "return_to": f"/series/{result1.series_id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/series/{result1.series_id}")
    assert "Intentionally unavailable" not in page.text


def test_regular_user_cannot_change_missing_issue_state(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    from comic_archive.auth import create_user
    create_user(database, "plain-reader", "reader-password-123", is_admin=False)
    client = TestClient(create_app(database, library, database.parent / "staging"))
    login = _post(
        client, "/login",
        data={"username": "plain-reader", "password": "reader-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    response = _post(
        client,
        f"/series/{result.series_id}/missing/2",
        data={"intentional": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_maintenance_dashboard_reports_health_problems(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "001.jpg").write_bytes(b"page")
    staged = build_staged_import(
        build_review_plan(scan_folder(source)),
        author="Maintenance Artist",
        series="Maintenance Comic",
        issue_metadata={".": {"issue_number": "1", "complete": False}},
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    import sqlite3
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT id, stored_path FROM media LIMIT 1").fetchone()
        media_id, stored_path = row
    managed = library / stored_path
    managed.unlink()

    page = client.get("/maintenance")
    assert page.status_code == 200
    assert "Library maintenance" in page.text
    assert "Maintenance Artist / Maintenance Comic" in page.text
    assert "Missing managed files" in page.text
    assert stored_path in page.text
    assert "Incomplete or unknown issues" in page.text


def test_maintenance_dashboard_is_admin_only(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    from comic_archive.auth import create_user
    create_user(database, "maintenance-reader", "reader-password-123", is_admin=False)
    client = TestClient(create_app(database, library, database.parent / "staging"))
    login = _post(
        client, "/login",
        data={"username": "maintenance-reader", "password": "reader-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    response = client.get("/maintenance")
    assert response.status_code == 403


def test_maintenance_report_does_not_reinitialize_database_for_each_series(tmp_path: Path, monkeypatch):
    database, library, result = _make_library(tmp_path)

    # Regression for Windows SQLite "database is locked": build_maintenance_report
    # must compute gaps from its existing open connection rather than calling
    # the public series_gaps() helper, which initializes/opens the DB again.
    import comic_archive.maintenance as maintenance

    def fail_if_nested(*args, **kwargs):
        raise AssertionError("maintenance report must not call series_gaps() while scanning")

    monkeypatch.setattr(maintenance, "series_gaps", fail_if_nested)
    report = maintenance.build_maintenance_report(database, library)
    assert report is not None


def _complete_bulk_single_issue(client: TestClient, review_url: str, *, author: str, series: str):
    response = _post(
        client,
        review_url,
        data={"name_0": "Primary content", "role_0": "primary"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    metadata_url = response.headers["location"]

    metadata_page = client.get(metadata_url)
    assert metadata_page.status_code == 200
    assert f'value="{author}"' in metadata_page.text
    assert f'value="{series}"' in metadata_page.text

    response = _post(
        client,
        metadata_url,
        data={
            "author": author,
            "series": series,
            "issue_number_0": "",
            "title_0": "",
            "complete_0": "yes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    organize_url = response.headers["location"]

    response = _post(client, organize_url, data={"action": "continue"}, follow_redirects=False)
    assert response.status_code == 303
    confirm_url = response.headers["location"]
    session_id = confirm_url.split("/")[2]
    return session_id


def test_bulk_artist_import_discovers_and_processes_comics_sequentially(tmp_path: Path):
    artist_root = tmp_path / "Bulk Artist"
    comic_a = artist_root / "Comic Alpha"
    comic_b = artist_root / "Comic Beta"
    ignored = artist_root / "Notes"
    comic_a.mkdir(parents=True)
    comic_b.mkdir(parents=True)
    ignored.mkdir(parents=True)
    (comic_a / "001.jpg").write_bytes(b"alpha")
    (comic_b / "001.png").write_bytes(b"beta")
    (ignored / "readme.txt").write_text("not a comic")

    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    start = client.get("/import/bulk")
    assert start.status_code == 200
    assert "Bulk artist import" in start.text

    response = _post(
        client,
        "/import/bulk/scan",
        data={"source_path": str(artist_root), "author": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    choose_url = response.headers["location"]
    choose = client.get(choose_url)
    assert "Comic Alpha" in choose.text
    assert "Comic Beta" in choose.text
    assert "Notes" not in choose.text
    assert 'value="Bulk Artist"' in choose.text

    response = _post(
        client,
        f"{choose_url}/start",
        data={"author": "Bulk Artist", "comic_0": "yes", "comic_1": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    first_review = response.headers["location"]

    first_session = _complete_bulk_single_issue(
        client, first_review, author="Bulk Artist", series="Comic Alpha"
    )
    first_done = _post(client, f"/import/{first_session}/commit", data={})
    assert first_done.status_code == 200
    assert "Bulk progress: 1 of 2" in first_done.text
    import re
    match = re.search(r'href="(/import/[^"]+/review)"[^>]*>Continue with next comic', first_done.text)
    assert match
    second_review = match.group(1)

    second_session = _complete_bulk_single_issue(
        client, second_review, author="Bulk Artist", series="Comic Beta"
    )
    final = _post(
        client,
        f"/import/{second_session}/commit",
        data={},
        follow_redirects=False,
    )
    assert final.status_code == 303
    assert final.headers["location"].endswith("/done")

    done = client.get(final.headers["location"])
    assert done.status_code == 200
    assert "Bulk import complete" in done.text
    assert "2 comics imported" in done.text
    assert "Comic Alpha" in done.text
    assert "Comic Beta" in done.text

    authors = read_library(database)
    assert authors[0].name == "Bulk Artist"
    assert {series.title for series in authors[0].series} == {"Comic Alpha", "Comic Beta"}


def test_bulk_artist_import_can_select_subset(tmp_path: Path):
    artist_root = tmp_path / "Subset Artist"
    for name in ("Keep", "Skip"):
        folder = artist_root / name
        folder.mkdir(parents=True)
        (folder / "001.jpg").write_bytes(name.encode())

    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    scan = _post(
        client, "/import/bulk/scan",
        data={"source_path": str(artist_root), "author": "Subset Artist"},
        follow_redirects=False,
    )
    choose_url = scan.headers["location"]
    response = _post(
        client, f"{choose_url}/start",
        data={"author": "Subset Artist", "comic_0": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/review")


def test_authenticated_header_uses_hamburger_menu(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)
    page = client.get("/")
    assert page.status_code == 200
    assert '<details class="header-menu">' in page.text
    assert 'aria-label="Open menu"' in page.text
    assert '<a href="/import">Import</a>' in page.text
    assert '<a href="/maintenance">Maintenance</a>' in page.text
    assert '<a href="/history">History</a>' in page.text
    assert '<a href="/admin/users">Accounts</a>' in page.text
    assert "<nav>" not in page.text


def test_single_comic_review_defaults_group_name_to_folder_name(tmp_path: Path):
    source = tmp_path / "Named Comic"
    source.mkdir()
    (source / "001.jpg").write_bytes(b"page")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    scan = _post(
        client,
        "/import/scan",
        data={"source_path": str(source)},
        follow_redirects=False,
    )
    assert scan.status_code == 303
    review = client.get(scan.headers["location"])
    assert review.status_code == 200
    assert 'value="Named Comic"' in review.text
    assert 'value="Primary content"' not in review.text


def test_import_folder_browser_is_rooted_and_selects_relative_folder(tmp_path: Path):
    import_root = tmp_path / "incoming"
    comic = import_root / "Artist" / "Comic"
    comic.mkdir(parents=True)
    (comic / "001.jpg").write_bytes(b"page")

    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    try:
        create_user(database, "browser-admin", "test-password-123", is_admin=True)
    except Exception:
        pass
    app = create_app(
        database,
        library,
        tmp_path / "staging",
        import_root=import_root,
    )
    client = TestClient(app)
    login = _post(
        client,
        "/login",
        data={"username": "browser-admin", "password": "test-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    root = client.get("/import/browse")
    assert root.status_code == 200
    assert "Choose folder" in root.text
    assert "Artist" in root.text
    assert str(import_root) not in root.text

    artist = client.get("/import/browse?path=Artist")
    assert artist.status_code == 200
    assert "Comic" in artist.text

    selected = client.get("/import?folder=Artist/Comic")
    assert selected.status_code == 200
    assert "Artist/Comic" in selected.text
    assert 'name="selected_path" value="Artist/Comic"' in selected.text

    escaped = client.get("/import/browse?path=../")
    assert escaped.status_code == 400


def _multipart_csrf(client: TestClient) -> str:
    return _csrf_token(client)


def test_client_folder_upload_preserves_tree_and_starts_review(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = client.post(
        "/import/upload",
        data={"csrf_token": _multipart_csrf(client)},
        files=[
            ("files", ("Uploaded Comic/001.jpg", b"page-one", "image/jpeg")),
            ("files", ("Uploaded Comic/Textless/001.png", b"textless", "image/png")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/review")

    review = client.get(response.headers["location"])
    assert review.status_code == 200
    assert 'value="Uploaded Comic"' in review.text
    assert "Textless" in review.text

    upload_content = list((staging / "uploads").glob("*/content/Uploaded Comic/001.jpg"))
    assert len(upload_content) == 1
    assert upload_content[0].read_bytes() == b"page-one"


def test_client_uploaded_single_comic_is_cleaned_after_commit(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload = client.post(
        "/import/upload",
        data={"csrf_token": _multipart_csrf(client)},
        files=[("files", ("Clean Comic/001.jpg", b"page", "image/jpeg"))],
        follow_redirects=False,
    )
    review_url = upload.headers["location"]
    session_id = review_url.split("/")[2]

    response = _post(
        client,
        review_url,
        data={"name_0": "Clean Comic", "role_0": "primary"},
        follow_redirects=False,
    )
    metadata_url = response.headers["location"]
    response = _post(
        client,
        metadata_url,
        data={
            "author": "Upload Artist",
            "series": "Clean Comic",
            "issue_number_0": "",
            "title_0": "",
            "complete_0": "yes",
        },
        follow_redirects=False,
    )
    organize_url = response.headers["location"]
    response = _post(client, organize_url, data={"action": "continue"}, follow_redirects=False)
    assert response.status_code == 303

    done = _post(client, f"/import/{session_id}/commit", data={})
    assert done.status_code == 200
    assert "Import complete" in done.text
    assert not list((staging / "uploads").glob("*"))


def test_client_bulk_artist_upload_discovers_comic_folders(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = client.post(
        "/import/bulk/upload",
        data={"csrf_token": _multipart_csrf(client)},
        files=[
            ("files", ("Remote Artist/Comic A/001.jpg", b"a", "image/jpeg")),
            ("files", ("Remote Artist/Comic B/001.png", b"b", "image/png")),
            ("files", ("Remote Artist/Notes/readme.txt", b"ignore", "text/plain")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    choose = client.get(response.headers["location"])
    assert choose.status_code == 200
    assert "Remote Artist" in choose.text
    assert "Comic A" in choose.text
    assert "Comic B" in choose.text
    assert "Notes" not in choose.text


def test_import_page_prefers_client_folder_upload(tmp_path: Path):
    database, library, _ = _make_library(tmp_path)
    client = _admin_client(database, library)
    page = client.get("/import")
    assert page.status_code == 200
    assert 'action="/import/upload-session"' in page.text
    assert "webkitdirectory" in page.text
    assert "Upload from this device" in page.text
    assert "Import a folder already on the server" in page.text


def test_stale_uploaded_import_is_cleaned_after_12_hours(tmp_path: Path, monkeypatch):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload = client.post(
        "/import/upload",
        data={"csrf_token": _multipart_csrf(client)},
        files=[("files", ("Idle Comic/001.jpg", b"page", "image/jpeg"))],
        follow_redirects=False,
    )
    assert upload.status_code == 303
    review_url = upload.headers["location"]
    session_id = review_url.split("/")[2]

    app = client.app
    session = app.state.import_sessions[session_id]
    upload_root = session.upload_root
    assert upload_root is not None and upload_root.exists()

    old = time.time() - (12 * 60 * 60) - 5
    session.last_activity = old
    import os
    os.utime(upload_root, (old, old))

    expired = client.get(review_url)
    assert expired.status_code == 404
    assert session_id not in app.state.import_sessions
    assert not upload_root.exists()


def test_orphaned_upload_directory_is_swept_after_12_hours(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    orphan = staging / "uploads" / "orphan-upload"
    orphan.mkdir(parents=True)
    (orphan / "content").mkdir()
    old = time.time() - (12 * 60 * 60) - 5
    import os
    os.utime(orphan, (old, old))

    client.app.state.last_upload_sweep = 0
    response = client.get("/")
    assert response.status_code == 200
    assert not orphan.exists()


def test_bulk_duplicate_can_be_skipped_and_continue_to_next_comic(tmp_path: Path):
    # Seed Comic A so the first bulk item will trigger duplicate detection.
    existing_source = tmp_path / "existing" / "Comic A"
    existing_source.mkdir(parents=True)
    (existing_source / "001.jpg").write_bytes(b"same-a")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"

    scan = scan_folder(existing_source)
    plan = build_review_plan(scan)
    staged = build_staged_import(
        plan,
        author="Bulk Artist",
        series="Comic A",
        issue_metadata={".": {"issue_number": "", "title": "", "complete": True}},
    )
    commit_staged_import(staged, library_root=library, database_path=database)

    client = _admin_client(database, library, staging)

    uploaded = client.post(
        "/import/bulk/upload",
        data={"csrf_token": _multipart_csrf(client)},
        files=[
            ("files", ("Bulk Artist/Comic A/001.jpg", b"same-a", "image/jpeg")),
            ("files", ("Bulk Artist/Comic B/001.jpg", b"new-b", "image/jpeg")),
        ],
        follow_redirects=False,
    )
    choose_url = uploaded.headers["location"]

    started = _post(
        client,
        f"{choose_url}/start",
        data={"author": "Bulk Artist", "comic_0": "yes", "comic_1": "yes"},
        follow_redirects=False,
    )
    first_review = started.headers["location"]
    first_session = _complete_bulk_single_issue(
        client, first_review, author="Bulk Artist", series="Comic A"
    )

    duplicate = _post(client, f"/import/{first_session}/commit", data={})
    assert duplicate.status_code == 409
    assert "Possible duplicate import detected" in duplicate.text
    assert "Skip this comic and continue" in duplicate.text

    skipped = _post(
        client,
        f"/import/{first_session}/skip",
        data={},
        follow_redirects=False,
    )
    assert skipped.status_code == 303
    assert skipped.headers["location"].endswith("/review")
    second_review = skipped.headers["location"]

    second_session = _complete_bulk_single_issue(
        client, second_review, author="Bulk Artist", series="Comic B"
    )
    final = _post(
        client,
        f"/import/{second_session}/commit",
        data={},
        follow_redirects=False,
    )
    assert final.status_code == 303
    done = client.get(final.headers["location"])
    assert done.status_code == 200
    assert "Comic A" in done.text
    assert "Skipped" in done.text

    authors = read_library(database)
    series_titles = {
        series.title
        for author in authors
        if author.name == "Bulk Artist"
        for series in author.series
    }
    assert series_titles == {"Comic A", "Comic B"}


def test_bulk_skip_is_not_available_for_single_import(tmp_path: Path):
    source = tmp_path / "Single"
    source.mkdir()
    (source / "001.jpg").write_bytes(b"page")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    scan_response = _post(
        client,
        "/import/scan",
        data={"source_path": str(source)},
        follow_redirects=False,
    )
    review_url = scan_response.headers["location"]
    session_id = review_url.split("/")[2]
    skipped = _post(client, f"/import/{session_id}/skip", data={})
    assert skipped.status_code == 400


def test_orphaned_staging_json_is_swept_after_12_hours(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    staging.mkdir(parents=True, exist_ok=True)
    orphan = staging / "orphan.json"
    orphan.write_text('{"stale": true}')
    old = time.time() - (12 * 60 * 60) - 5
    import os
    os.utime(orphan, (old, old))

    client.app.state.last_upload_sweep = 0
    response = client.get("/")
    assert response.status_code == 200
    assert not orphan.exists()


def test_active_staging_json_is_preserved_even_if_old(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    source = tmp_path / "source" / "Active Comic"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"page")

    scan_response = _post(
        client,
        "/import/scan",
        data={"source_path": str(source)},
        follow_redirects=False,
    )
    review_url = scan_response.headers["location"]
    session_id = review_url.split("/")[2]

    review = _post(
        client,
        review_url,
        data={"name_0": "Active Comic", "role_0": "primary"},
        follow_redirects=False,
    )
    metadata_url = review.headers["location"]
    metadata = _post(
        client,
        metadata_url,
        data={
            "author": "Active Artist",
            "series": "Active Comic",
            "issue_number_0": "",
            "title_0": "",
            "complete_0": "yes",
        },
        follow_redirects=False,
    )
    assert metadata.status_code == 303

    session = client.app.state.import_sessions[session_id]
    assert session.staging_path is not None
    old = time.time() - (12 * 60 * 60) - 5
    import os
    os.utime(session.staging_path, (old, old))

    # Keep the import session itself active, then force a global sweep.
    session.last_activity = time.time()
    client.app.state.last_upload_sweep = 0
    home = client.get("/")
    assert home.status_code == 200
    assert session.staging_path.exists()


def _create_browser_upload(client: TestClient, *, mode: str, expected_files: int) -> str:
    response = _post(
        client,
        "/import/upload-session",
        data={"mode": mode, "expected_files": str(expected_files)},
    )
    assert response.status_code == 200, response.text
    return response.json()["upload_id"]


def _send_browser_upload_file(client: TestClient, upload_id: str, relative_path: str, content: bytes):
    return client.post(
        f"/import/upload-session/{upload_id}/file",
        data={
            "csrf_token": _multipart_csrf(client),
            "relative_path": relative_path,
        },
        files={"file": (Path(relative_path).name, content, "application/octet-stream")},
    )


def test_resumable_upload_session_accepts_files_individually_and_finalizes(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload_id = _create_browser_upload(client, mode="single", expected_files=2)
    first = _send_browser_upload_file(client, upload_id, "Session Comic/001.jpg", b"one")
    second = _send_browser_upload_file(client, upload_id, "Session Comic/Covers/cover.png", b"cover")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["received_files"] == 2

    root = client.app.state.upload_sessions[upload_id].upload_root
    assert (root / "content" / "Session Comic" / "001.jpg").read_bytes() == b"one"
    assert (root / "content" / "Session Comic" / "Covers" / "cover.png").read_bytes() == b"cover"

    finalized = _post(client, f"/import/upload-session/{upload_id}/finalize")
    assert finalized.status_code == 200, finalized.text
    redirect = finalized.json()["redirect"]
    assert redirect.endswith("/review")
    assert upload_id not in client.app.state.upload_sessions
    assert root.exists()  # ownership transferred to the import session


def test_resumable_upload_repeated_file_is_idempotent(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload_id = _create_browser_upload(client, mode="single", expected_files=1)
    first = _send_browser_upload_file(client, upload_id, "Retry Comic/001.jpg", b"page")
    retry = _send_browser_upload_file(client, upload_id, "Retry Comic/001.jpg", b"page")
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["received_files"] == 1

    finalized = _post(client, f"/import/upload-session/{upload_id}/finalize")
    assert finalized.status_code == 200


def test_resumable_upload_cannot_finalize_until_every_file_arrives(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload_id = _create_browser_upload(client, mode="single", expected_files=2)
    sent = _send_browser_upload_file(client, upload_id, "Incomplete Comic/001.jpg", b"one")
    assert sent.status_code == 200

    finalized = _post(client, f"/import/upload-session/{upload_id}/finalize")
    assert finalized.status_code == 409
    assert "received 1 of 2 files" in finalized.json()["error"]
    assert upload_id in client.app.state.upload_sessions


def test_resumable_bulk_upload_finalizes_to_artist_selection(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload_id = _create_browser_upload(client, mode="bulk", expected_files=2)
    assert _send_browser_upload_file(
        client, upload_id, "Artist Upload/Comic A/001.jpg", b"a"
    ).status_code == 200
    assert _send_browser_upload_file(
        client, upload_id, "Artist Upload/Comic B/001.jpg", b"b"
    ).status_code == 200

    finalized = _post(client, f"/import/upload-session/{upload_id}/finalize")
    assert finalized.status_code == 200
    redirect = finalized.json()["redirect"]
    assert redirect.startswith("/import/bulk/")
    choose = client.get(redirect)
    assert choose.status_code == 200
    assert "Comic A" in choose.text
    assert "Comic B" in choose.text


def test_stale_unfinished_browser_upload_session_is_cleaned(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload_id = _create_browser_upload(client, mode="single", expected_files=2)
    assert _send_browser_upload_file(
        client, upload_id, "Idle Session/001.jpg", b"one"
    ).status_code == 200

    upload = client.app.state.upload_sessions[upload_id]
    root = upload.upload_root
    old = time.time() - (12 * 60 * 60) - 5
    upload.last_activity = old
    import os
    os.utime(root, (old, old))

    client.app.state.last_upload_sweep = 0
    home = client.get("/")
    assert home.status_code == 200
    assert upload_id not in client.app.state.upload_sessions
    assert not root.exists()


def test_resumable_upload_can_be_cancelled(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    upload_id = _create_browser_upload(client, mode="single", expected_files=2)
    root = client.app.state.upload_sessions[upload_id].upload_root
    cancelled = _post(client, f"/import/upload-session/{upload_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True
    assert upload_id not in client.app.state.upload_sessions
    assert not root.exists()


def test_import_pages_use_resumable_file_by_file_upload_ui(tmp_path: Path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    single = client.get("/import")
    bulk = client.get("/import/bulk")
    for response in (single, bulk):
        assert response.status_code == 200
        assert "/import/upload-session" in response.text
        assert "concurrency = 3" in response.text
        assert "maxAttempts = 3" in response.text
        assert "Resume upload" in response.text
        assert "/file" in response.text
        assert "/finalize" in response.text


def test_author_page_reports_series_summary_totals(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    response = client.get(f"/authors/{result.author_id}")
    assert response.status_code == 200
    assert "1 issue" in response.text
    assert "Completeness unknown" not in response.text
    assert "1 page + 1 extra" in response.text
    assert "Unread" in response.text
    assert "Issue 1: 1 page" not in response.text


def test_series_completeness_can_be_edited_in_web_ui(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    response = _post(
        client,
        f"/series/{result.series_id}/edit",
        data={"title": "Example Comic", "author_id": result.author_id, "complete": "no"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert read_library(database)[0].series[0].complete is False
    page = client.get(f"/series/{result.series_id}")
    assert ">Incomplete</span>" in page.text


def test_import_keepalive_marks_active_bulk_work_as_recent(tmp_path: Path):
    artist_root = tmp_path / "Artist"
    comic = artist_root / "Comic"
    comic.mkdir(parents=True)
    (comic / "001.jpg").write_bytes(b"page")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/bulk/scan", data={"source_path": str(artist_root)}, follow_redirects=False)
    assert response.status_code == 303
    bulk_id = response.headers["location"].split("/")[3]
    bulk = client.app.state.bulk_import_sessions[bulk_id]
    bulk.last_activity = time.time() - 3600

    before = bulk.last_activity
    response = _post(client, f"/import/bulk/{bulk_id}/keepalive")
    assert response.status_code == 204
    assert bulk.last_activity > before


def test_organizer_group_actions_preserve_moves_and_block_empty_groups(tmp_path: Path):
    source = tmp_path / "incoming" / "Issue"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"one")
    (source / "002.jpg").write_bytes(b"two")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    review_url = response.headers["location"]
    session_id = review_url.split("/")[2]
    response = _post(client, review_url, data={"name_0": "Issue", "role_0": "primary"}, follow_redirects=False)
    response = _post(
        client,
        response.headers["location"],
        data={"author": "Artist", "series": "Comic", "issue_number_0": "1", "complete_0": "yes"},
        follow_redirects=False,
    )
    organize_url = response.headers["location"]

    response = _post(client, organize_url, data={"new_group_0": "Bonus", "action": "create:0"}, follow_redirects=False)
    assert response.status_code == 303
    staged_issue = client.app.state.import_sessions[session_id].staged.issues[0]
    primary = next(group for group in staged_issue.groups if group.role == "primary")
    bonus = next(group for group in staged_issue.groups if group.name == "Bonus")

    # Create a second group while also moving a page. The move must be saved
    # before the page reload caused by group creation.
    response = _post(
        client,
        organize_url,
        data={
            "new_group_0": "Textless",
            "action": "create:0",
            "target_0_0": primary.relative_path,
            "target_0_1": bonus.relative_path,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    staged_issue = client.app.state.import_sessions[session_id].staged.issues[0]
    bonus = next(group for group in staged_issue.groups if group.name == "Bonus")
    textless = next(group for group in staged_issue.groups if group.name == "Textless")
    assert [media.relative_path for media in bonus.media] == ["002.jpg"]
    assert textless.media == []

    # Continuing with an empty group stays in the organizer and explains it.
    response = _post(client, organize_url, data={"action": "continue"}, follow_redirects=False)
    assert response.status_code == 400
    assert "Empty content group" in response.text
    assert "Remove empty group" in response.text

    staged_issue = client.app.state.import_sessions[session_id].staged.issues[0]
    empty_index = next(i for i, group in enumerate(staged_issue.groups) if group.name == "Textless")
    response = _post(
        client,
        organize_url,
        data={"action": f"remove-empty:0:{empty_index}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert all(group.name != "Textless" for group in client.app.state.import_sessions[session_id].staged.issues[0].groups)


def test_import_review_can_promote_issue_folder_to_subseries_before_staging(tmp_path: Path):
    source = tmp_path / "incoming" / "Nested Comic"
    for path in (
        "Arc One/Chapter 1/001.jpg",
        "Arc One/Chapter 2/001.jpg",
        "Arc Two/Part A/001.jpg",
    ):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.encode())

    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    review_url = response.headers["location"]
    session_id = review_url.split("/")[2]

    review = client.get(review_url)
    assert 'value="Arc One">Make sub-series' in review.text

    promoted = _post(
        client,
        f"/import/{session_id}/review/mark-subseries",
        data={"folder_path": "Arc One"},
        follow_redirects=False,
    )
    assert promoted.status_code == 303

    review = client.get(review_url)
    assert "sub-series" in review.text
    assert "Arc One/Chapter 1" in review.text
    assert "Arc One/Chapter 2" in review.text


def test_nested_series_browsing_and_parent_edit_controls(tmp_path: Path):
    source = tmp_path / "nested-ui"
    for path in ("Arc One/Chapter 1/001.jpg", "Arc Two/Chapter 2/001.jpg"):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.encode())
    staged = build_staged_import(
        build_review_plan(scan_folder(source, subseries_folders=["Arc One", "Arc Two"])),
        author="Nested Artist",
        series="Parent Comic",
    )
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    result = commit_staged_import(staged, library_root=library, database_path=database)
    client = _admin_client(database, library)

    author_page = client.get(f"/authors/{result.author_id}")
    assert "Parent Comic" in author_page.text
    assert "2 issues" in author_page.text

    root_page = client.get(f"/series/{result.series_id}")
    assert "Sub-series" in root_page.text
    assert "Arc One" in root_page.text and "Arc Two" in root_page.text

    root = read_library(database)[0].series[0]
    child = root.children[0]
    child_page = client.get(f"/series/{child.id}")
    assert f'href="/series/{result.series_id}">Parent Comic</a>' in child_page.text

    edit_page = client.get(f"/series/{child.id}/edit")
    assert 'name="parent_series_id"' in edit_page.text
    assert f'value="{result.series_id}" selected' in edit_page.text


def test_import_workspace_shows_full_tree_and_can_reclassify_flattened_folder(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    for path in (
        "Issue 1/Pages/001.jpg",
        "Issue 1/Gallery/bonus.png",
        "Issue 2/001.jpg",
    ):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.encode())
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    review_url = response.headers["location"]
    session_id = review_url.split("/")[2]
    workspace = client.get(review_url)
    assert workspace.status_code == 200
    assert "Import workspace" in workspace.text
    assert "Issue 1/" in workspace.text
    assert "Pages/" in workspace.text
    assert "Gallery/" in workspace.text
    assert "Issue 2/" in workspace.text

    changed = _post(
        client,
        f"/import/{session_id}/workspace/role",
        data={"folder_path": "Issue 1/Gallery", "role": "issue-extras"},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    updated = client.get(changed.headers["location"])
    assert "Gallery/" in updated.text
    gallery_section = updated.text[updated.text.index("Gallery/"):]
    assert "Issue-Extras" in gallery_section[:1200]


def test_import_workspace_folder_drag_order_feeds_metadata_order(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    for issue in ("Chapter C", "Chapter A", "Chapter B"):
        target = source / issue / "001.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(issue.encode())
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    ordered = ["Chapter C", "Chapter A", "Chapter B"]
    saved = _post(
        client,
        f"/import/{session_id}/workspace/folder-order",
        data={"parent": ".", "ordered_paths": __import__("json").dumps(ordered)},
    )
    assert saved.status_code == 204

    continued = _post(client, f"/import/{session_id}/review", follow_redirects=False)
    assert continued.status_code == 303
    metadata = client.get(continued.headers["location"])
    positions = [metadata.text.index(f"<legend>{name}</legend>") for name in ordered]
    assert positions == sorted(positions)


def test_import_workspace_page_reorder_reaches_staging(tmp_path: Path):
    source = tmp_path / "incoming" / "One Shot"
    source.mkdir(parents=True)
    for name in ("001.jpg", "002.jpg", "003.jpg"):
        (source / name).write_bytes(name.encode())
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    workspace = client.get(response.headers["location"])
    assert "Import workspace" in workspace.text
    paths = re.findall(r'class="workspace-media-row" draggable="true" data-source="([^"]+)"', workspace.text)
    assert len(paths) == 3
    reversed_paths = list(reversed(paths))
    saved = _post(
        client,
        f"/import/{session_id}/workspace/media-order",
        data={"folder_path": ".", "ordered_paths": __import__("json").dumps(reversed_paths)},
    )
    assert saved.status_code == 204

    _post(client, f"/import/{session_id}/review", follow_redirects=False)
    staged_response = _post(
        client,
        f"/import/{session_id}/metadata",
        data={
            "author": "Artist",
            "series": "One Shot",
            "series_complete": "",
            "sort_order_0": "1",
            "issue_number_0": "",
            "title_0": "",
            "complete_0": "",
        },
        follow_redirects=False,
    )
    assert staged_response.status_code == 303
    staged = client.app.state.import_sessions[session_id].staged
    assert staged is not None
    media = staged.issues[0].groups[0].media
    assert [item.source_path for item in media] == reversed_paths


def test_import_workspace_uses_natural_folder_order(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    for number in (1, 2, 3, 10, 11, 12):
        target = source / f"Issue {number}" / "001.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(str(number).encode())
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    workspace = client.get(response.headers["location"])
    positions = [workspace.text.index(f"Issue {number}/") for number in (1, 2, 3, 10, 11, 12)]
    assert positions == sorted(positions)


def test_import_workspace_can_finalize_metadata_and_created_extra_without_organizer(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    issue = source / "Issue 1"
    issue.mkdir(parents=True)
    (issue / "001.jpg").write_bytes(b"one")
    (issue / "002.jpg").write_bytes(b"two")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    workspace = client.get(response.headers["location"])
    assert "Comic metadata" in workspace.text
    assert "Validate and continue to confirmation" in workspace.text

    saved_root = _post(
        client,
        f"/import/{session_id}/workspace/metadata",
        data={"folder_path": "Issue 1", "author": "Artist", "series": "Comic", "series_complete": "yes", "issue_number": "Special", "title": "Opening", "issue_complete": "yes"},
    )
    assert saved_root.status_code == 204
    saved_issue = saved_root
    assert saved_issue.status_code == 204

    created = _post(
        client,
        f"/import/{session_id}/workspace/create-group",
        data={"owner": "Issue 1", "name": "Textless"},
    )
    assert created.status_code == 200
    group_path = created.json()["group_path"]
    session = client.app.state.import_sessions[session_id]
    source_path = str(session.plan.scan.primary.media[1].path)
    moved = _post(
        client,
        f"/import/{session_id}/workspace/media-target",
        data={"source_path": source_path, "target": group_path},
    )
    assert moved.status_code == 204

    finalized = _post(
        client,
        f"/import/{session_id}/workspace/finalize",
        data={"author": "Artist", "series": "Comic", "series_complete": "yes", "selected": "Issue 1"},
        follow_redirects=False,
    )
    assert finalized.status_code == 303
    assert finalized.headers["location"] == f"/import/{session_id}/confirm"
    staged = client.app.state.import_sessions[session_id].staged
    assert staged is not None
    assert staged.author == "Artist"
    assert staged.series == "Comic"
    assert staged.series_complete is True
    staged_issue = staged.issues[0]
    assert staged_issue.issue_number == "Special"
    assert staged_issue.title == "Opening"
    assert staged_issue.complete is True
    primary = next(group for group in staged_issue.groups if group.role == "primary")
    extra = next(group for group in staged_issue.groups if group.name == "Textless")
    assert len(primary.media) == 1
    assert len(extra.media) == 1
    assert extra.media[0].source_path == source_path


def test_import_workspace_metadata_is_autosave_and_page_target_dropdown_is_removed(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    issue = source / "Issue 1"
    extras = issue / "Extras"
    extras.mkdir(parents=True)
    (issue / "001.jpg").write_bytes(b"one")
    (extras / "bonus.jpg").write_bytes(b"bonus")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    workspace = client.get(response.headers["location"])
    assert workspace.status_code == 200
    assert "Changes save automatically" in workspace.text
    assert 'class="workspace-media-target"' not in workspace.text
    assert 'data-media-target="true"' in workspace.text
    assert "Ctrl/Cmd-click selects multiple pages" in workspace.text


def test_import_workspace_can_move_multiple_selected_pages_to_extra_group(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    issue = source / "Issue 1"
    issue.mkdir(parents=True)
    for number in range(1, 4):
        (issue / f"{number:03}.jpg").write_bytes(str(number).encode())
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    created = _post(client, f"/import/{session_id}/workspace/create-group", data={"owner": "Issue 1", "name": "Textless"})
    assert created.status_code == 200
    group_path = created.json()["group_path"]
    session = client.app.state.import_sessions[session_id]
    source_paths = [str(item.path) for item in session.plan.scan.primary.media[:2]]

    moved = _post(
        client,
        f"/import/{session_id}/workspace/media-targets",
        data={"source_paths": json.dumps(source_paths), "target": group_path},
    )
    assert moved.status_code == 204
    assert all(session.workspace_media_targets[path] == group_path for path in source_paths)

    extra_workspace = client.get(f"/import/{session_id}/review?folder={group_path}")
    assert extra_workspace.status_code == 200
    for source_path in source_paths:
        assert source_path in extra_workspace.text


def test_import_workspace_recovers_from_disk_after_in_memory_session_loss(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    issue = source / "Issue 1"
    issue.mkdir(parents=True)
    for number in range(1, 4):
        (issue / f"{number:03}.jpg").write_bytes(str(number).encode())
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    _post(
        client,
        f"/import/{session_id}/workspace/metadata",
        data={
            "folder_path": "Issue 1", "author": "Recovered Artist", "series": "Recovered Comic",
            "series_complete": "no", "issue_number": "A", "title": "Recovered Issue", "issue_complete": "yes",
        },
    )
    created = _post(client, f"/import/{session_id}/workspace/create-group", data={"owner": "Issue 1", "name": "Textless"})
    group_path = created.json()["group_path"]
    active = client.app.state.import_sessions[session_id]
    page = str(active.plan.scan.primary.media[1].path)
    _post(client, f"/import/{session_id}/workspace/media-target", data={"source_path": page, "target": group_path})

    state_file = staging / "session_state" / f"import_{session_id}.json"
    assert state_file.exists()
    client.app.state.import_sessions.clear()

    recovered_page = client.get(f"/import/{session_id}/review?folder=Issue%201")
    assert recovered_page.status_code == 200
    recovered = client.app.state.import_sessions[session_id]
    assert recovered.workspace_author == "Recovered Artist"
    assert recovered.workspace_series == "Recovered Comic"
    assert recovered.workspace_series_complete == "no"
    assert recovered.workspace_metadata["Issue 1"]["title"] == "Recovered Issue"
    assert recovered.workspace_virtual_groups[group_path.split(":", 1)[1]]["name"] == "Textless"
    assert recovered.workspace_media_targets[page] == group_path


def test_bulk_import_session_recovers_from_disk_after_in_memory_session_loss(tmp_path: Path):
    artist = tmp_path / "incoming" / "Artist"
    for name in ("Comic A", "Comic B"):
        folder = artist / name
        folder.mkdir(parents=True)
        (folder / "001.jpg").write_bytes(name.encode())
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/bulk/scan", data={"selected_path": str(artist), "author": "Artist"}, follow_redirects=False)
    bulk_id = response.headers["location"].split("/")[-1]
    assert (staging / "session_state" / f"bulk_{bulk_id}.json").exists()
    client.app.state.bulk_import_sessions.clear()

    recovered_page = client.get(f"/import/bulk/{bulk_id}")
    assert recovered_page.status_code == 200
    recovered = client.app.state.bulk_import_sessions[bulk_id]
    assert recovered.author == "Artist"
    assert [candidate.name for candidate in recovered.candidates] == ["Comic A", "Comic B"]


def test_card_cleanup_hides_unknown_and_zero_extras_and_authors_have_preview(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)

    home = client.get("/")
    assert home.status_code == 200
    assert f'/thumbnail/' in home.text
    assert f'/authors/{result.author_id}' in home.text

    # Remove the single extra so the series summary should not advertise + 0 extras.
    with sqlite3.connect(database) as db:
        group = db.execute("SELECT id FROM content_groups WHERE series_id = ? AND role != 'primary' LIMIT 1", (result.series_id,)).fetchone()
        if group:
            db.execute("UPDATE media SET active = 0 WHERE group_id = ?", (group[0],))
    author = client.get(f"/authors/{result.author_id}")
    assert author.status_code == 200
    assert "+ 0 extra" not in author.text
    assert "Completeness unknown" not in author.text
    assert "Unread" in author.text


def test_series_without_direct_issues_hides_issues_section(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)
    # Create an empty parent and make the existing series its child.
    parent_id = "parent-series"
    with sqlite3.connect(database) as db:
        author_id = db.execute("SELECT author_id FROM series WHERE id = ?", (result.series_id,)).fetchone()[0]
        db.execute("INSERT INTO series(id, author_id, title, parent_series_id, sort_order) VALUES (?, ?, ?, NULL, 1)", (parent_id, author_id, "Parent"))
        db.execute("UPDATE series SET parent_series_id = ?, sort_order = 1 WHERE id = ?", (parent_id, result.series_id))
    page = client.get(f"/series/{parent_id}")
    assert page.status_code == 200
    assert "Sub-series" in page.text
    assert "<h2>Issues</h2>" not in page.text
    assert "No issues." not in page.text


def test_import_commit_is_idempotent_and_completed_pages_recover(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    issue = source / "Issue 1"
    issue.mkdir(parents=True)
    (issue / "001.jpg").write_bytes(b"one")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    _post(client, f"/import/{session_id}/workspace/metadata", data={
        "folder_path": "Issue 1", "author": "Artist", "series": "Comic",
        "series_complete": "", "issue_number": "1", "title": "", "issue_complete": "",
    })
    finalized = _post(client, f"/import/{session_id}/workspace/finalize", data={
        "author": "Artist", "series": "Comic", "series_complete": "", "selected": "Issue 1",
    }, follow_redirects=False)
    assert finalized.status_code == 303

    first = _post(client, f"/import/{session_id}/commit", data={}, follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == f"/import/{session_id}/done"

    # A replay of the same POST must not create another import or fail.
    second = _post(client, f"/import/{session_id}/commit", data={}, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == f"/import/{session_id}/done"

    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1

    done = client.get(f"/import/{session_id}/done")
    assert done.status_code == 200
    assert "Import complete" in done.text

    # Browser back to confirmation/workspace after completion should recover to done.
    confirm = client.get(f"/import/{session_id}/confirm", follow_redirects=False)
    assert confirm.status_code == 303
    assert confirm.headers["location"] == f"/import/{session_id}/done"
    workspace = client.get(f"/import/{session_id}/review", follow_redirects=False)
    assert workspace.status_code == 303
    assert workspace.headers["location"] == f"/import/{session_id}/done"


def test_confirm_page_has_duplicate_submit_guard_and_processing_indicator(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    source.mkdir(parents=True)
    (source / "001.jpg").write_bytes(b"one")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    client = _admin_client(database, library)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    _post(client, f"/import/{session_id}/workspace/metadata", data={
        "folder_path": ".", "author": "Artist", "series": "Comic", "series_complete": "",
        "issue_number": "", "title": "", "issue_complete": "",
    })
    _post(client, f"/import/{session_id}/workspace/finalize", data={
        "author": "Artist", "series": "Comic", "series_complete": "", "selected": ".",
    }, follow_redirects=False)
    confirm = client.get(f"/import/{session_id}/confirm")
    assert 'class="import-processing-form"' in confirm.text
    assert "Importing comic" in confirm.text
    assert "<progress" in confirm.text


def test_series_can_be_deleted_with_managed_files(tmp_path: Path):
    database, library, result = _make_library(tmp_path)
    client = _admin_client(database, library)
    managed_dir = library / "series" / result.series_id
    assert managed_dir.exists()

    wrong = _post(client, f"/series/{result.series_id}/delete", data={"confirmation": "wrong"}, follow_redirects=False)
    assert wrong.status_code == 400
    assert managed_dir.exists()

    response = _post(client, f"/series/{result.series_id}/delete", data={"confirmation": "Example Comic"}, follow_redirects=False)
    assert response.status_code == 303
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT 1 FROM series WHERE id = ?", (result.series_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM issues WHERE series_id = ?", (result.series_id,)).fetchone() is None
    assert not managed_dir.exists()


def test_workspace_can_create_synthetic_issue_and_move_pages_into_it(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    issue = source / "Issue 1"
    issue.mkdir(parents=True)
    (issue / "001.jpg").write_bytes(b"one")
    (issue / "002.jpg").write_bytes(b"two")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    session = client.app.state.import_sessions[session_id]
    root_key = str(session.plan.scan.content_root.relative_to(session.plan.scan.source)).replace("\\", "/")
    if root_key == ".":
        root_key = "."
    created = _post(
        client, f"/import/{session_id}/workspace/create-node",
        data={"parent": root_key, "kind": "issue", "name": "Recovered Chapter"},
    )
    assert created.status_code == 200
    synthetic = created.json()["node_path"]
    session = client.app.state.import_sessions[session_id]
    page = str((session.plan.scan.primary or session.plan.scan.issues[0].primary).media[1].path)
    moved = _post(
        client, f"/import/{session_id}/workspace/media-targets",
        data={"source_paths": json.dumps([page]), "target": synthetic},
    )
    assert moved.status_code == 204

    finalized = _post(
        client, f"/import/{session_id}/workspace/finalize",
        data={"selected": synthetic, "author": "Artist", "series": "Comic", "series_complete": ""},
        follow_redirects=False,
    )
    assert finalized.status_code == 303
    staged = client.app.state.import_sessions[session_id].staged
    synthetic_issue = next(item for item in staged.issues if item.source_key == synthetic)
    assert synthetic_issue.issue_number == "Recovered Chapter"
    assert len(synthetic_issue.groups[0].media) == 1
    original = next(item for item in staged.issues if item.source_key != synthetic)
    assert len(next(group for group in original.groups if group.role == "primary").media) == 1


def test_bulk_artist_import_discovers_root_level_pdf_as_comic(tmp_path: Path):
    import fitz

    artist = tmp_path / "incoming" / "PDF Artist"
    artist.mkdir(parents=True)
    pdf = artist / "Series A.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "page one")
    document.save(pdf)
    document.close()

    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)
    response = _post(
        client, "/import/bulk/scan",
        data={"selected_path": str(artist), "author": "PDF Artist"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    choose_url = response.headers["location"]
    choose = client.get(choose_url)
    assert "Series A" in choose.text

    started = _post(
        client, f"{choose_url}/start",
        data={"author": "PDF Artist", "comic_0": "yes"},
        follow_redirects=False,
    )
    assert started.status_code == 303
    workspace = client.get(started.headers["location"])
    assert workspace.status_code == 200
    assert "Series A" in workspace.text


def test_workspace_can_create_synthetic_subseries_with_issue(tmp_path: Path):
    source = tmp_path / "incoming" / "Comic"
    for name, payload in (("Issue 1", b"one"), ("Issue 2", b"two")):
        folder = source / name
        folder.mkdir(parents=True)
        (folder / "001.jpg").write_bytes(payload)
        if name == "Issue 1":
            (folder / "002.jpg").write_bytes(b"one-more")
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    client = _admin_client(database, library, staging)

    response = _post(client, "/import/scan", data={"source_path": str(source)}, follow_redirects=False)
    session_id = response.headers["location"].split("/")[2]
    root = client.app.state.import_sessions[session_id].plan.scan.content_root
    scan_source = client.app.state.import_sessions[session_id].plan.scan.source
    root_key = str(root.relative_to(scan_source)).replace("\\", "/")
    series_node = _post(
        client, f"/import/{session_id}/workspace/create-node",
        data={"parent": root_key, "kind": "sub-series", "name": "Arc A"},
    ).json()["node_path"]
    issue_node = _post(
        client, f"/import/{session_id}/workspace/create-node",
        data={"parent": series_node, "kind": "issue", "name": "Recovered"},
    ).json()["node_path"]
    session = client.app.state.import_sessions[session_id]
    page = str(session.plan.scan.issues[0].primary.media[0].path)
    moved = _post(
        client, f"/import/{session_id}/workspace/media-targets",
        data={"source_paths": json.dumps([page]), "target": issue_node},
    )
    assert moved.status_code == 204
    finalized = _post(
        client, f"/import/{session_id}/workspace/finalize",
        data={"selected": issue_node, "author": "Artist", "series": "Comic", "series_complete": ""},
        follow_redirects=False,
    )
    assert finalized.status_code == 303
    staged = client.app.state.import_sessions[session_id].staged
    assert any(item.source_key == series_node and item.parent_key == "." for item in staged.subseries)
    created_issue = next(item for item in staged.issues if item.source_key == issue_node)
    assert created_issue.series_key == series_node
    assert len(created_issue.groups[0].media) == 1
