"""Permission regressions. These tests have not been executed by the implementing agent."""
import json
import re
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from comic_archive.auth import create_user
from comic_archive.database import connect_database
from comic_archive.library import read_library
from comic_archive.library_views import search_library
from comic_archive.permissions import AccessPolicy, initialize_permissions_database, save_restriction
from comic_archive.progress import get_continue_reading, get_progress, save_progress
from comic_archive.web import create_app


def token(client, path="/"):
    page = client.get(path)
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None, page.text
    return match.group(1)


def post(client, url, data=None):
    return client.post(url, data={"csrf_token": token(client), **(data or {})}, follow_redirects=False)


@pytest.fixture
def archive(tmp_path):
    database = tmp_path / "archive.sqlite3"
    library = tmp_path / "library"
    library.mkdir()
    app = create_app(database, library, tmp_path / "staging")
    users = {name: create_user(database, name, "password-12345", is_admin=name == "admin")
             for name in ("admin", "allowed", "denied")}
    with connect_database(database) as db:
        db.execute("INSERT INTO authors(id, name) VALUES ('artist', 'Secret Artist'), ('public-artist', 'Public Artist')")
        db.execute("""INSERT INTO series(id, author_id, title, parent_series_id) VALUES
            ('root', 'artist', 'Secret Root', NULL),
            ('child', 'artist', 'Secret Child', 'root'),
            ('public', 'public-artist', 'Public Series', NULL)""")
        for series_id in ("root", "child", "public"):
            issue_id = f"{series_id}-issue"
            db.execute("INSERT INTO issues(id, series_id, title, issue_number) VALUES (?, ?, ?, '1')",
                       (issue_id, series_id, f"{series_id} Issue"))
            for suffix, owner, role in (("pages", issue_id, "primary"), ("extra", issue_id, "extra"), ("series-extra", None, "extra")):
                group_id = f"{series_id}-{suffix}"
                db.execute("""INSERT INTO content_groups(id, series_id, issue_id, name, role, relative_path, sort_order)
                    VALUES (?, ?, ?, ?, ?, '.', 1)""", (group_id, series_id, owner, group_id, role))
                filename = f"{group_id}.jpg"
                Image.new("RGB", (8, 8), "red").save(library / filename)
                db.execute("""INSERT INTO media(id, group_id, position, stored_path, source_path,
                    original_relative_path, mime_type, media_kind, size_bytes)
                    VALUES (?, ?, 1, ?, 'source-is-not-touched', ?, 'image/jpeg', 'image', ?)""",
                    (group_id, group_id, filename, filename, (library / filename).stat().st_size))
    with ExitStack() as stack:
        clients = {}
        for name in users:
            client = stack.enter_context(TestClient(app))
            response = client.post("/login", data={"username": name, "password": "password-12345",
                "csrf_token": token(client, "/login")}, follow_redirects=False)
            assert response.status_code == 303
            clients[name] = client
        yield database, users, clients


def restrict(archive, kind="series", target="root", names=("allowed",)):
    database, users, _ = archive
    save_restriction(database, users["admin"], kind, target, True, {users[name].id for name in names})


def test_defaults_admin_ui_and_restriction_round_trip(archive):
    database, users, clients = archive
    for client in clients.values():
        assert client.get("/read/child-issue").status_code == 200
        assert client.get("/media/child-pages").status_code == 200
    admin = clients["admin"]
    assert '/admin/permissions/series/root' in admin.get("/series/root").text
    assert '/admin/permissions/issue/root-issue' in admin.get("/issues/root-issue").text
    url = "/admin/permissions/series/root"
    assert post(admin, url, {"access": "selected", f"user_{users['allowed'].id}": "yes"}).status_code == 303
    assert clients["allowed"].get("/series/root").status_code == 200
    assert clients["denied"].get("/series/root").status_code == 404
    assert post(admin, url, {"access": "selected"}).status_code == 303
    assert clients["allowed"].get("/series/root").status_code == 404
    assert admin.get("/series/root").status_code == 200
    assert post(admin, url, {"access": "all"}).status_code == 303
    assert clients["denied"].get("/series/root").status_code == 200
    with connect_database(database) as db:
        rows = db.execute("SELECT after_json FROM audit_log WHERE action = 'permissions'").fetchall()
    assert len(rows) == 3
    assert json.loads(rows[0][0])["actor_id"] == users["admin"].id


@pytest.mark.parametrize("path", [
    "/authors/artist", "/series/root", "/series/child", "/issues/root-issue", "/issues/child-issue",
    "/read/child-issue", "/groups/child-pages", "/groups/child-extra", "/groups/root-series-extra",
    "/read-group/child-extra", "/read-group/root-series-extra", "/read-group/child-pages",
    "/media/child-pages", "/media/child-extra", "/media/root-series-extra",
    "/thumbnail/child-pages", "/thumbnail/child-extra", "/thumbnail/root-series-extra",
])
def test_inherited_restrictions_protect_direct_urls(archive, path):
    _, _, clients = archive
    restrict(archive)
    assert clients["denied"].get(path).status_code == 404
    for name in ("allowed", "admin"):
        assert clients[name].get(path).status_code == 200
    assert clients["denied"].get("/read/public-issue").status_code == 200


def test_search_counts_previews_and_continue_reading_do_not_leak(archive):
    database, users, clients = archive
    save_progress(database, users["denied"].id, "child-issue", 1, 2)
    save_progress(database, users["denied"].id, "public-issue", 1, 2)
    restrict(archive)
    home = clients["denied"].get("/")
    assert "Secret Artist" not in home.text
    assert "Secret Child" not in home.text
    assert "/thumbnail/root-pages" not in home.text
    assert "1 authors" in home.text
    access = AccessPolicy(database, users["denied"])
    assert search_library(database, "Secret", access=access) == []
    assert search_library(database, "child", access=access) == []
    # Filtering must precede LIMIT, including when hidden matches sort first.
    results = search_library(database, "Issue", limit=1, access=access)
    assert [row["url"] for row in results] == ["/issues/public-issue"]
    assert [row["issue_id"] for row in get_continue_reading(database, users["denied"].id, limit=1, access=access)] == ["public-issue"]
    assert read_library(database, access=access)[0].total_series == 1
    search = clients["denied"].get("/search?q=child").text
    assert 'href="/issues/child-issue"' not in search
    assert 'href="/groups/child-extra"' not in search


def test_issue_can_narrow_but_cannot_override_parent_and_revocation_preserves_progress(archive):
    database, users, clients = archive
    restrict(archive)
    restrict(archive, "issue", "child-issue", ("denied",))
    assert clients["allowed"].get("/series/child").status_code == 200
    assert clients["allowed"].get("/issues/child-issue").status_code == 404
    assert clients["denied"].get("/issues/child-issue").status_code == 404
    assert clients["allowed"].get("/media/child-extra").status_code == 404
    assert clients["allowed"].get("/media/child-series-extra").status_code == 200
    assert "child-issue" not in clients["allowed"].get("/series/child").text
    save_progress(database, users["allowed"].id, "child-issue", 1, 2)
    client = clients["allowed"]
    assert post(client, "/progress/child-issue", {"page": "2", "total_pages": "2"}).status_code == 404
    assert post(client, "/progress/child-issue/reset").status_code == 404
    saved = get_progress(database, users["allowed"].id, "child-issue")
    assert saved.page == 1 and not saved.completed
    save_restriction(database, users["admin"], "issue", "child-issue", False, set())
    assert client.get("/issues/child-issue").status_code == 200
    restrict(archive, "series", "child", ("denied",))
    assert client.get("/series/child").status_code == 404
    assert clients["denied"].get("/series/child").status_code == 404


def test_admin_csrf_and_invalid_selections_do_not_change_access(archive):
    database, users, clients = archive
    url = "/admin/permissions/series/root"
    assert clients["denied"].get(url).status_code == 403
    assert post(clients["denied"], url, {"access": "selected"}).status_code == 403
    assert clients["admin"].post(url, data={"access": "selected"}).status_code == 403
    assert post(clients["admin"], url, {"access": "selected", "user_unknown": "yes"}).status_code == 400
    assert post(clients["admin"], url, {"access": "typo"}).status_code == 400
    assert clients["denied"].get("/series/root").status_code == 200
    with pytest.raises(PermissionError):
        save_restriction(database, users["denied"], "series", "root", True, set())


def test_migration_is_additive_idempotent_and_new_users_are_not_allowed(archive):
    database, users, clients = archive
    restrict(archive)
    initialize_permissions_database(database)
    initialize_permissions_database(database)
    newcomer = create_user(database, "newcomer", "password-12345")
    access = AccessPolicy(database, newcomer)
    assert not access.series("root") and not access.issue("child-issue")
    assert access.series("public")
    assert AccessPolicy(database, users["allowed"]).issue("child-issue")
    assert clients["denied"].get("/series/root").status_code == 404
    with connect_database(database) as db:
        assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 9


def test_media_range_and_cache_headers_recheck_after_revocation(archive):
    _, _, clients = archive
    client = clients["denied"]
    for path in ("/", "/issues/root-issue", "/media/root-pages", "/thumbnail/root-pages"):
        response = client.get(path)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
    restrict(archive)
    for path in ("/media/root-pages", "/thumbnail/root-pages"):
        assert client.get(path, headers={"Range": "bytes=0-9"}).status_code == 404
