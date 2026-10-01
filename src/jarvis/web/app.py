"""The local web app — T066.

A JSON API over :mod:`jarvis.service`, the same functions the MCP server calls,
plus the SPA from ``web/static``. Like MCP it implements no rule of its own:
every route is a one-line delegation, so the web app cannot widen what is
possible either.

There is no login — the machine and the Wi-Fi are the boundary (PLAN.md §2).
Two guards stop *other websites* rather than other people, and neither is an
auth surface:

- **Host check.** A page on ``evil.example`` that re-points its DNS at
  127.0.0.1 (DNS rebinding) arrives with ``Host: evil.example``. Only
  ``localhost`` and IP literals are served.
- **Writes need** ``X-Jarvis: 1``. A cross-site form or ``fetch`` cannot set a
  custom header without a CORS preflight, and this app answers none.
"""

from __future__ import annotations

import ipaddress
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from jarvis import __version__, service
from jarvis.store.db import session

STATIC_DIR = Path(__file__).parent / "static"

# Python's mimetypes reads the Windows registry, which can map .js to text/plain;
# a browser then refuses to register the service worker. Pin what the PWA needs.
for _type, _ext in (("text/javascript", ".js"), ("application/manifest+json", ".webmanifest")):
    mimetypes.add_type(_type, _ext)
WRITE_HEADER = "x-jarvis"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_allowed(host_header: str) -> bool:
    host = host_header.strip()
    if host.startswith("["):                       # [::1]:8765
        host = host[1 : host.find("]")]
    elif host.count(":") == 1:                      # 192.168.1.5:8765
        host = host.split(":", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class Value(BaseModel):
    value: Any


class FieldValue(BaseModel):
    field_path: str
    value: Any


class Enabled(BaseModel):
    enabled: bool


class ContentHash(BaseModel):
    content_hash: str


class Note(BaseModel):
    note: str | None = None


class StatusChange(BaseModel):
    status: str
    note: str | None = None


class Classification(BaseModel):
    classification: str


class Link(BaseModel):
    application_id: str


class NewContact(BaseModel):
    email: str
    published_source_url: str
    name: str = ""
    role: str | None = None


class ForOpportunity(BaseModel):
    opportunity_id: str


class StarAnswer(BaseModel):
    situation: str = ""
    task: str = ""
    result: str = ""


class Answer(BaseModel):
    answer: str


_LOOPBACK = {"127.0.0.1", "::1"}


def _is_loopback(request: Request) -> bool:
    """Is the connecting socket on this machine? (Owner decision 2026-09-24.)

    Read from the transport, never from X-Forwarded-For or any header: a phone
    on the Wi-Fi can set a header, but it cannot make its packets come from
    127.0.0.1.
    """
    host = request.client.host if request.client else ""
    return host in _LOOPBACK or host.startswith("127.")


def create_app() -> FastAPI:
    app = FastAPI(title="Jarvis", version=__version__, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _host_allowed(request.headers.get("host", "")):
            return JSONResponse({"detail": "unrecognised Host header"}, status_code=421)
        if request.method not in _SAFE_METHODS and request.headers.get(WRITE_HEADER) != "1":
            return JSONResponse({"detail": f"writes need the {WRITE_HEADER} header"}, 403)
        return await call_next(request)

    # KeyError means "no such thing"; ValueError means "refused, with a reason".
    # Both carry a message written for the user, so it is passed through.
    @app.exception_handler(KeyError)
    async def not_found(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc.args[0]) if exc.args else "not found"}, 404)

    @app.exception_handler(ValueError)
    async def refused(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, 422)

    # Domain refusals: the action was understood and declined, with the reason.
    from jarvis.approval import ApprovalRefused
    from jarvis.documents.package import NotSufficient
    from jarvis.documents.reply import NoReplyDrafted
    from jarvis.gmail.google import MailboxDisconnected
    from jarvis.outreach import NotPublished
    from jarvis.send import SendRefused

    for declined in (ApprovalRefused, SendRefused, MailboxDisconnected, NotSufficient,
                     NoReplyDrafted, NotPublished):
        app.add_exception_handler(
            declined,
            lambda _r, exc: JSONResponse({"detail": str(exc), "refused": True}, 409),
        )
    app.add_exception_handler(
        PermissionError, lambda _r, exc: JSONResponse({"detail": str(exc)}, 403))

    # ----------------------------------------------------------------- read

    @app.get("/api/profile/status")
    def profile_status() -> dict[str, Any]:
        with session() as conn:
            return service.profile_status(conn)

    @app.get("/api/profile/records")
    def profile_records(unconfirmed_only: bool = False) -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_profile_records(conn, unconfirmed_only=unconfirmed_only)

    @app.get("/api/opportunities")
    def opportunities(
        limit: int = 25,
        country: str | None = None,
        kind: str | None = None,
        passes_floor_only: bool = False,
    ) -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_opportunities(
                conn, limit=limit, country=country, kind=kind,
                passes_floor_only=passes_floor_only,
            )

    @app.get("/api/opportunities/{opportunity_id}")
    def opportunity(opportunity_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.get_opportunity(conn, opportunity_id)

    @app.get("/api/allowance")
    def allowance() -> dict[str, Any]:
        with session() as conn:
            return service.get_allowance(conn)

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        with session() as conn:
            return service.capability_status(conn)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with session() as conn:
            return service.run_health(conn)

    @app.get("/api/documents")
    def documents() -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_documents(conn)

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        with session() as conn:
            return service.get_settings(conn)

    # --------------------------------------------------- write, not outbound

    @app.post("/api/profile/records/{record_id}/confirm")
    def confirm(record_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.confirm_field(conn, record_id)

    @app.post("/api/profile/records/{record_id}/correct")
    def correct(record_id: str, body: Value) -> dict[str, Any]:
        with session() as conn:
            return service.correct_field(conn, record_id, body.value)

    @app.post("/api/profile/fields")
    def set_field(body: FieldValue) -> dict[str, Any]:
        with session() as conn:
            return service.set_profile_field(conn, body.field_path, body.value)

    @app.put("/api/settings/{key}")
    def set_setting(key: str, body: Value, request: Request) -> dict[str, Any]:
        with session() as conn:
            return service.set_setting(conn, key, body.value,
                                       from_loopback=_is_loopback(request))

    @app.put("/api/countries/{country_iso2}")
    def set_country(country_iso2: str, body: Enabled) -> dict[str, Any]:
        with session() as conn:
            return service.set_country_enabled(conn, country_iso2, body.enabled)

    # ---------------------------------------------------------- screens

    @app.get("/api/progress")
    def progress() -> dict[str, Any]:
        with session() as conn:
            return service.progress(conn)

    @app.get("/api/system/health")
    def system_health() -> dict[str, Any]:
        with session() as conn:
            return service.health(conn)

    @app.get("/api/onboarding")
    def onboarding() -> dict[str, Any]:
        with session() as conn:
            return service.onboarding(conn)

    @app.get("/api/countries")
    def countries() -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_countries(conn)

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict[str, Any]:
        """Lets the UI say up front whether approval is possible from this device."""
        return {"loopback": _is_loopback(request)}

    # ------------------------------------------------ packages and approval

    @app.get("/api/queue")
    def queue() -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_queue(conn)

    @app.post("/api/opportunities/{opportunity_id}/package")
    def generate(opportunity_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.generate_package(conn, opportunity_id)

    @app.get("/api/packages/{package_id}")
    def package(package_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.get_package(conn, package_id)

    @app.get("/api/packages/{package_id}/documents/{doc_type}")
    def package_document(package_id: str, doc_type: str) -> FileResponse:
        from jarvis.store.paths import get_paths

        with session() as conn:
            pkg = service.get_package(conn, package_id)
        doc = next((d for d in pkg["documents"] if d["type"] == doc_type), None)
        if doc is None:
            raise HTTPException(404, f"no {doc_type} in this package")
        path = (get_paths().root / doc["path"]).resolve()
        if get_paths().generated_documents.resolve() not in path.parents:
            raise HTTPException(404, "not a generated document")
        return FileResponse(path, filename=path.name)

    @app.post("/api/packages/{package_id}/approve")
    def approve(package_id: str, body: ContentHash, request: Request) -> dict[str, Any]:
        # The client address the socket reports, not a header a client can set.
        with session() as conn:
            return service.approve_package(conn, package_id, body.content_hash,
                                           from_loopback=_is_loopback(request))

    @app.post("/api/packages/{package_id}/send")
    def send_now(package_id: str, body: ContentHash) -> dict[str, Any]:
        with session() as conn:
            return service.send_package(conn, package_id, body.content_hash)

    @app.post("/api/packages/{package_id}/submitted")
    def submitted(package_id: str, body: Note) -> dict[str, Any]:
        with session() as conn:
            return service.record_manual_submission(conn, package_id, body.note)

    # ---------------------------------------------- applications, replies

    @app.get("/api/applications")
    def applications() -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_applications(conn)

    @app.get("/api/applications/{application_id}")
    def application(application_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.get_application(conn, application_id)

    @app.post("/api/applications/{application_id}/status")
    def application_status(application_id: str, body: StatusChange) -> dict[str, Any]:
        with session() as conn:
            return service.set_application_status(conn, application_id, body.status, body.note)

    @app.get("/api/replies")
    def replies(unlinked_only: bool = False) -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_replies(conn, unlinked_only=unlinked_only)

    @app.post("/api/replies/{reply_id}/correct")
    def correct_reply(reply_id: str, body: Classification) -> dict[str, Any]:
        with session() as conn:
            return service.correct_reply(conn, reply_id, body.classification)

    @app.post("/api/replies/{reply_id}/link")
    def link_reply(reply_id: str, body: Link) -> dict[str, Any]:
        with session() as conn:
            return service.link_reply(conn, reply_id, body.application_id)

    @app.post("/api/replies/{reply_id}/draft")
    def draft_reply(reply_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.draft_reply(conn, reply_id)

    @app.get("/api/mailbox")
    def mailbox() -> dict[str, Any]:
        with session() as conn:
            return service.mailbox(conn)

    @app.post("/api/mailbox/poll")
    def poll() -> dict[str, Any]:
        with session() as conn:
            return service.poll_mailbox(conn)

    # -------------------------------------------------------------- interview

    @app.get("/api/star")
    def star() -> list[dict[str, Any]]:
        with session() as conn:
            return service.star_bank(conn)

    @app.post("/api/star/{record_id}")
    def save_star(record_id: str, body: StarAnswer) -> dict[str, Any]:
        with session() as conn:
            return service.save_star_answer(conn, record_id, body.situation, body.task,
                                            body.result)

    @app.get("/api/opportunities/{opportunity_id}/interview")
    def interview_questions(opportunity_id: str) -> list[dict[str, Any]]:
        with session() as conn:
            return service.interview_questions(conn, opportunity_id)

    @app.post("/api/opportunities/{opportunity_id}/interview/score")
    def score_answer(opportunity_id: str, body: Answer) -> dict[str, Any]:
        with session() as conn:
            return service.score_interview_answer(conn, opportunity_id, body.answer)

    # --------------------------------------------------------------- outreach

    @app.get("/api/contacts")
    def contacts() -> list[dict[str, Any]]:
        with session() as conn:
            return service.list_contacts(conn)

    @app.get("/api/opportunities/{opportunity_id}/contacts")
    def posting_contacts(opportunity_id: str) -> list[dict[str, Any]]:
        with session() as conn:
            return service.discover_contacts(conn, opportunity_id)

    @app.post("/api/contacts")
    def add_contact(body: NewContact) -> dict[str, Any]:
        with session() as conn:
            return service.add_contact(conn, body.email, body.published_source_url,
                                       body.name, body.role)

    @app.post("/api/contacts/{contact_id}/draft")
    def draft_outreach(contact_id: str, body: ForOpportunity) -> dict[str, Any]:
        with session() as conn:
            return service.draft_outreach(conn, contact_id, body.opportunity_id)

    @app.post("/api/contacts/{contact_id}/suppress")
    def suppress_contact(contact_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.suppress_contact(conn, contact_id)

    @app.post("/api/contacts/{contact_id}/erase")
    def erase_contact(contact_id: str) -> dict[str, Any]:
        with session() as conn:
            return service.suppress_contact(conn, contact_id, erase=True)

    @app.get("/api/documents/{document_id}/file")
    def document_file(document_id: str, request: Request) -> Response:
        """A restricted document opens only on this computer, and every open is audited."""
        with session() as conn:
            row = conn.execute("SELECT filename, path, restricted FROM document WHERE id = ?",
                               (document_id,)).fetchone()
            if row is None or not row["path"]:
                raise HTTPException(404, "no such document")
            if row["restricted"]:
                data = service.open_restricted_document(conn, document_id,
                                                        from_loopback=_is_loopback(request))
                return Response(data, media_type="application/octet-stream", headers={
                    "Content-Disposition": f'attachment; filename="{row["filename"]}"'})
        return FileResponse(row["path"], filename=row["filename"])

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def unknown_api(rest: str) -> None:
        raise HTTPException(404, f"no such endpoint: /api/{rest}")

    # ------------------------------------------------------------------ SPA

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # Client-side routes (/opportunities/abc) all load the one index.html;
        # a real file in static/ (manifest, icons) is served as itself.
        candidate = (STATIC_DIR / path).resolve()
        if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(503, "the web UI is not built yet — the JSON API is under /api")
        return FileResponse(index)

    return app
