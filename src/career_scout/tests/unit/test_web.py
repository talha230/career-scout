"""The local web app — T066. Routes delegate to the service; two browser guards."""

from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from career_scout.discovery import ingest
from career_scout.web import app as web_app

WRITE = {"X-Career-Scout": "1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's guard, narrowed to allow loopback only.

    On Windows ``socket.socketpair`` is emulated with a loopback ``connect``, and
    the asyncio loop behind ``TestClient`` needs one for its self-pipe.
    """
    real = socket.socket.connect

    def loopback_only(self: socket.socket, address: object) -> None:
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            raise RuntimeError(f"this test tried to reach {address!r}")
        real(self, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)


@pytest.fixture
def client(db: sqlite3.Connection) -> TestClient:
    return TestClient(web_app.create_app(), base_url="http://localhost:8765")


def _seed(db: sqlite3.Connection, title: str) -> str:
    db.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES ('US', 'US', 1)")
    db.execute(
        "INSERT OR IGNORE INTO source (id, name, provider, base_url, access_mode, legal_basis) "
        "VALUES ('b', 'B', 'greenhouse', 'https://b.example', 'api', 'public API')"
    )
    outcome = ingest.ingest_one(
        db,
        ingest.RawPosting(
            source_id="b", source_url=f"https://b.example/{title}", title=title,
            employer="Acme", location_raw="Austin, United States",
            description="Improve throughput. Requirements: 3 years of process improvement.",
        ),
    )
    assert outcome.opportunity_id
    return outcome.opportunity_id


def test_opportunities_are_listed_with_their_rejection_flag(client, db) -> None:
    good = _seed(db, "Industrial Engineer")
    junk = _seed(db, "CHECK BACK SOON")

    rows = {r["id"]: r for r in client.get("/api/opportunities").json()}

    assert rows[good]["rejected"] is False
    assert rows[junk]["rejected"] is True   # returned and flagged, never dropped


def test_one_opportunity_carries_its_rejections(client, db) -> None:
    junk = _seed(db, "CHECK BACK SOON")
    body = client.get(f"/api/opportunities/{junk}").json()
    assert body["rejected"] is True
    assert body["rejections"][0]["rule"].startswith("J")


def test_an_unknown_opportunity_is_404(client) -> None:
    assert client.get("/api/opportunities/nope").status_code == 404


def test_an_unknown_api_path_is_404_not_the_spa(client) -> None:
    assert client.get("/api/nothing-here").status_code == 404


def test_a_write_without_the_header_is_refused(client) -> None:
    # A cross-site form can POST here; it cannot set a custom header.
    response = client.put("/api/settings/staleness_window_days", json={"value": 30})
    assert response.status_code == 403


def test_a_write_with_the_header_goes_through_the_service(client) -> None:
    response = client.put("/api/settings/staleness_window_days", json={"value": 30}, headers=WRITE)
    assert response.status_code == 200
    assert response.json() == {"key": "staleness_window_days", "value": 30}
    assert client.get("/api/settings").status_code == 200


def test_an_unknown_setting_is_refused_not_stored(client) -> None:
    response = client.put("/api/settings/made_up", json={"value": 1}, headers=WRITE)
    assert response.status_code == 404


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "attacker.localhost.com"])
def test_a_foreign_host_header_is_refused(client, host) -> None:
    # DNS rebinding: a hostile page re-points its own name at this machine.
    assert client.get("/api/settings", headers={"Host": host}).status_code == 421


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:8765", "192.168.1.20:8765",
                                  "[::1]:8765"])
def test_local_and_lan_hosts_are_served(client, host) -> None:
    assert client.get("/api/settings", headers={"Host": host}).status_code == 200


def test_client_routes_fall_back_to_the_spa(client, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text("<div id=app></div>", encoding="utf-8")
    (tmp_path / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(web_app, "STATIC_DIR", tmp_path)

    assert "id=app" in client.get("/opportunities/abc").text
    assert client.get("/manifest.webmanifest").text == "{}"
    assert "id=app" in client.get("/../../etc/passwd").text   # no escape from static/


def test_the_shipped_ui_is_built_with_absolute_asset_paths() -> None:
    # The wheel ships web/static, and pip users have no node. Assets must be
    # "/assets/..." — a relative "./assets" breaks every nested route
    # (/opportunities/abc would ask for /opportunities/assets/...).
    index = web_app.STATIC_DIR / "index.html"
    assert index.is_file(), "run `npm run build:app --prefix dashboard`"
    html = index.read_text(encoding="utf-8")
    assert 'src="/assets/' in html
    assert "./assets" not in html


def test_the_pwa_files_are_served_with_types_a_browser_accepts(client) -> None:
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "javascript" in worker.headers["content-type"]
    manifest = client.get("/manifest.webmanifest")
    assert "manifest+json" in manifest.headers["content-type"]
    assert {i["sizes"] for i in manifest.json()["icons"]} >= {"192x192", "512x512"}
    assert client.get("/icon-512.png").headers["content-type"] == "image/png"


def _client_from(address: str) -> TestClient:
    return TestClient(web_app.create_app(), base_url="http://localhost:8765",
                      client=(address, 50000))


def _package(db: sqlite3.Connection) -> dict:
    from career_scout import service
    from career_scout.tests.invariants.test_documents import _opportunity, _profile

    _profile(db)
    db.execute("INSERT INTO credential (id, provider, account_label, scopes, connected_at) "
               "VALUES ('c1', 'google', 'me@example.org', '[]', '2026-09-01T00:00:00Z')")
    return service.generate_package(db, _opportunity(db))


def test_approval_from_this_computer_is_accepted(db) -> None:
    pkg = _package(db)
    response = _client_from("127.0.0.1").post(
        f"/api/packages/{pkg['id']}/approve", json={"content_hash": pkg["content_hash"]},
        headers=WRITE)
    assert response.status_code == 200, response.text
    assert response.json()["destination"] == "careers@acme.example"


def test_approval_from_the_network_is_refused(db) -> None:
    """Owner decision 2026-09-24: a phone on the Wi-Fi can browse, not approve."""
    pkg = _package(db)
    phone = _client_from("192.168.1.23")
    response = phone.post(f"/api/packages/{pkg['id']}/approve",
                          json={"content_hash": pkg["content_hash"]}, headers=WRITE)
    assert response.status_code == 409
    assert "this computer" in response.json()["detail"]
    assert phone.get(f"/api/packages/{pkg['id']}").status_code == 200   # browsing is fine
    assert phone.get("/api/whoami").json() == {"loopback": False}


def test_a_forwarded_for_header_does_not_make_a_phone_local(db) -> None:
    pkg = _package(db)
    response = _client_from("192.168.1.23").post(
        f"/api/packages/{pkg['id']}/approve", json={"content_hash": pkg["content_hash"]},
        headers={**WRITE, "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
    assert response.status_code == 409


def test_the_web_api_has_no_bulk_control() -> None:
    """T050 against the web app: one package per request, and no list of ids anywhere."""
    from fastapi.routing import APIRoute

    routes = [r for r in web_app.create_app().routes if isinstance(r, APIRoute)]
    approving = [r.path for r in routes if "approve" in r.path]
    assert approving == ["/api/packages/{package_id}/approve"]
    for route in routes:
        assert "all" not in route.path.split("/"), route.path
        assert not route.path.startswith("/api/bulk"), route.path
        body = route.body_field
        if body is not None:
            model = body.field_info.annotation
            for name, field in model.model_fields.items():
                assert "list" not in str(field.annotation).lower(), (route.path, name)


def test_a_restricted_document_opens_only_on_this_computer(db, tmp_path) -> None:
    from career_scout.documents import store

    passport = tmp_path / "passport.txt"
    passport.write_bytes(b"PASSPORT AB1234567")
    document_id = store.ingest(db, passport, kind="passport").document_id

    assert _client_from("192.168.1.23").get(f"/api/documents/{document_id}/file").status_code == 403
    local = _client_from("127.0.0.1").get(f"/api/documents/{document_id}/file")
    assert local.status_code == 200 and local.content == b"PASSPORT AB1234567"


def test_the_screens_have_their_data(db) -> None:
    client = _client_from("127.0.0.1")
    for path in ("/api/progress", "/api/system/health", "/api/onboarding", "/api/countries",
                 "/api/queue", "/api/applications", "/api/replies", "/api/mailbox"):
        assert client.get(path).status_code == 200, path
    health = client.get("/api/system/health").json()
    assert "alert" in health["backups"] and health["disk"]["data_directory"]
    steps = client.get("/api/onboarding").json()["steps"]
    assert all(step["if_skipped"] for step in steps)


def test_an_unbuilt_ui_says_so(client, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "STATIC_DIR", tmp_path)
    assert client.get("/").status_code == 503
