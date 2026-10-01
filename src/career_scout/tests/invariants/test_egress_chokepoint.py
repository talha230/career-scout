"""I-08 — all outbound network traffic passes through the audited chokepoint.

This is asserted at *runtime*, not by scanning imports, and the distinction is
the whole point of the test.

An import-graph check ("no module outside ``career_scout.net`` imports httpx") cannot
see three things this codebase actually contains: ``google-api-python-client``
carries its own HTTP transport, a PDF renderer is a program that resolves
hostnames on its own account, and any dependency may open a socket without
importing anything we thought to look for. So instead of asking what was
imported, we patch ``socket.socket.connect`` and ask, for every connection
attempted anywhere in the process, whether the call stack passes through
``career_scout.net``.

A connection with no ``career_scout/net/`` frame beneath it fails the test and names
the module that opened it.
"""

from __future__ import annotations

import socket
import traceback
from pathlib import Path

import pytest

from career_scout import net

NET_PACKAGE_DIR = str(Path(net.__file__).parent)

pytestmark = pytest.mark.invariant


class EgressViolation(AssertionError):
    """Raised at the moment an unaudited connection is attempted."""


@pytest.fixture
def chokepoint_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Allow connections that originate in ``career_scout.net``; record any that do not."""
    violations: list[str] = []
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> object:
        stack = traceback.extract_stack()
        through_net = any(NET_PACKAGE_DIR in frame.filename for frame in stack)
        if not through_net:
            caller = next(
                (f"{f.filename}:{f.lineno} in {f.name}" for f in reversed(stack[:-1])),
                "unknown",
            )
            violations.append(f"connect({address!r}) from {caller}")
            raise EgressViolation(
                f"outbound connection to {address!r} did not pass through career_scout.net; "
                f"opened from {caller}"
            )
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return violations


def test_a_direct_socket_outside_net_is_refused(chokepoint_guard: list[str]) -> None:
    """The guard itself works. Without this, a green suite proves nothing."""
    with pytest.raises(EgressViolation):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("example.com", 80))
        finally:
            sock.close()
    assert chokepoint_guard, "the violation should have been recorded"


def test_rendering_a_package_opens_no_connection(chokepoint_guard: list[str], db) -> None:
    """T012 — a document render resolves no remote host.

    The renderer is pure python-docx today. This keeps it honest if a PDF step,
    a font fetch or a browser is ever added to the path.
    """
    from career_scout.documents import package
    from career_scout.tests.invariants.test_documents import _opportunity, _profile

    _profile(db)
    pkg = package.generate_package(db, _opportunity(db))
    assert pkg["documents"]
    assert chokepoint_guard == []


def test_egress_chokepoint_runtime(chokepoint_guard: list[str], db, monkeypatch) -> None:
    """I-08 — a full pipeline pass opens no connection outside ``career_scout.net``.

    Discovery and Gmail are given mock transports so no real network is
    touched; every other step — screening, sufficiency, generation, rendering,
    polling, backup — runs for real. Anything in that path that reaches for a
    socket of its own (a Google client library's transport, a font fetch, a
    renderer) is caught by the guard.
    """
    import httpx

    from career_scout import pipeline
    from career_scout.profile import records
    from career_scout.tests.invariants.test_documents import _opportunity, _profile
    from career_scout.tests.unit.test_clients import FakeWeb

    _profile(db)
    records.add(db, "identity.citizenship", "PK", confirmed=True)
    records.add(db, "identity.tax_residence", "PK", confirmed=True)
    _opportunity(db)
    web = FakeWeb()
    from career_scout.discovery import countries

    countries.install(db)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def google(conn, credential_id):
        from career_scout.gmail.google import GoogleClient

        fetcher = net.Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(401)),
                              respect_robots=False, sleep=lambda _s: None)
        return GoogleClient(conn, credential_id, fetcher=fetcher)

    with web.fetcher() as fetcher:
        result = pipeline.run(db, fetcher=fetcher, google_client_factory=google)

    assert chokepoint_guard == [], chokepoint_guard
    assert result["counts"]["discover"].get("sources"), result["counts"]
    assert result["counts"]["generate"]["generated"] >= 1


def test_httpx_used_directly_is_refused(chokepoint_guard: list[str]) -> None:
    """Importing httpx elsewhere and calling it does not get a pass."""
    httpx = pytest.importorskip("httpx")
    with pytest.raises((EgressViolation, httpx.HTTPError, OSError)):
        httpx.get("http://example.com", timeout=1.0)


def test_net_module_is_the_only_socket_owner_in_the_package() -> None:
    """Cheap tripwire beside the runtime guard: no sibling imports an HTTP client.

    This cannot prove the guarantee — that is what the runtime guard is for —
    but it catches the common mistake early and names the file.
    """
    package_root = Path(net.__file__).parent.parent
    offenders: list[str] = []
    banned = (
        "import httpx",
        "import requests",
        "import urllib.request",
        "from httpx",
        "from requests",
    )

    for path in package_root.rglob("*.py"):
        if NET_PACKAGE_DIR in str(path) or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(stripped.startswith(b) for b in banned):
                offenders.append(f"{path.relative_to(package_root)}: {stripped}")

    assert not offenders, "HTTP client imported outside career_scout.net:\n" + "\n".join(offenders)


def test_manual_review_only_source_is_never_fetched() -> None:
    """A source whose terms forbid automated access is refused before the request.

    The refusal is in the fetcher, not in the caller, so no call site can
    forget it.
    """
    with pytest.raises(net.SourceNotFetchable, match="manual_review_only"):
        net.check_fetch_allowed(
            url="https://www.linkedin.com/jobs/view/1",
            access_mode="manual_review_only",
        )


def test_non_get_is_refused_outside_the_send_channels() -> None:
    """Discovery reads. Only the enumerated send channels may write."""
    with pytest.raises(net.MethodNotAllowed):
        net.check_method_allowed("POST", "https://boards-api.greenhouse.io/v1/boards/x/jobs")


def test_send_channels_may_post() -> None:
    for url in (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        "https://www.googleapis.com/upload/drive/v3/files",
    ):
        net.check_method_allowed("POST", url)  # must not raise


def test_no_credential_is_carried_to_a_non_send_host() -> None:
    """A token bound for Gmail must never ride along on a job-board request."""
    with pytest.raises(net.CredentialLeak):
        net.check_headers_allowed(
            "https://boards-api.greenhouse.io/v1/boards/x/jobs",
            {"Authorization": "Bearer ya29.a0-secret"},
        )


def test_credentials_are_permitted_on_send_hosts() -> None:
    net.check_headers_allowed(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        {"Authorization": "Bearer ya29.a0-secret"},
    )
