"""Sending from the user's own mailbox — T056.

:func:`transport` is what :func:`career_scout.send.send_approved` calls once every
condition has passed and the send is reserved. It attaches the files rendered at
generation — re-reading them from disk and **refusing if a byte differs from the
hash that was approved** — and sends one message. It does nothing else, so a
failure after the provider accepted the message can never cause a second send.

:func:`after_send` runs afterwards: it labels the thread
``Career Scout/Applied/<company>`` and writes the Drive application record. Its
failures are stored on the application row and reported; they are never a
reason to send again.

The Drive record is what makes Gmail and Drive a second system of record: for
each document it lists every claim, the profile record behind it, and how the
document differs from the full profile — what was left out and why.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from email.message import EmailMessage
from typing import Any

from career_scout import send as send_module
from career_scout.documents import package as package_module
from career_scout.gmail.google import DRIVE_UPLOAD, GMAIL, GoogleClient, label_id
from career_scout.store.paths import get_paths

_DOCX = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
_LETTERS = ("cover_letter", "motivation_letter", "reply", "outreach")
_ATTACHED = frozenset({"cv", "cover_letter", "motivation_letter"})


def _letter_text(pkg: dict[str, Any]) -> str:
    """The message body: the letter's own lines, as generated and approved."""
    for draft in pkg["drafts"]:
        if draft["doc_type"] in _LETTERS:
            return "\n\n".join(line["text"] for line in draft["lines"])
    return ""


def build_message(pkg: dict[str, Any], *, sender: str, destination: str,
                  in_reply_to: dict[str, str] | None = None) -> EmailMessage:
    """Assemble the MIME message, verifying every attachment against its hash."""
    root = get_paths().root
    message = EmailMessage()
    message["From"] = sender
    message["To"] = destination
    if in_reply_to:
        message["Subject"] = in_reply_to["subject"]
        message["In-Reply-To"] = in_reply_to["message_id_header"]
        message["References"] = in_reply_to["message_id_header"]
    else:
        message["Subject"] = f"Application: {pkg['title']}"
    message.set_content(_letter_text(pkg))

    attached = []
    for doc in pkg["documents"]:
        data = (root / doc["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != doc["sha256"]:
            # Condition 3 compares hashes in the database; this compares the file
            # that is actually about to leave. A file edited on disk after
            # approval is not what the person approved.
            raise send_module.SendRefused(
                3, f"{doc['type']} on disk differs from the approved bytes")
        attached.append(doc)
        if doc["type"] not in _ATTACHED:
            continue   # a reply or outreach message is its body, not an attachment
        message.add_attachment(data, maintype=_DOCX[0], subtype=_DOCX[1],
                               filename=f"{doc['type']}.docx")
    if package_module.rendered_hash(attached) != pkg["rendered_hash"]:
        raise send_module.SendRefused(3, "attachments do not match the approved rendered hash")
    return message


def transport(conn: sqlite3.Connection, client: GoogleClient, sender: str,
              in_reply_to: dict[str, str] | None = None) -> send_module.Transport:
    """A transport bound to one mailbox. Returns the Gmail message id."""

    def _send(envelope: send_module.Envelope) -> str:
        pkg = package_module.get_package(conn, envelope.package_id)
        if pkg["rendered_hash"] != envelope.rendered_sha256:
            raise send_module.SendRefused(3, "package bytes changed since the send was reserved")
        message = build_message(pkg, sender=sender, destination=envelope.destination,
                                in_reply_to=in_reply_to)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        body: dict[str, Any] = {"raw": raw}
        if in_reply_to and in_reply_to.get("thread_id"):
            body["threadId"] = in_reply_to["thread_id"]
        result = client.post(f"{GMAIL}/messages/send", body)
        return str(result["id"])

    return _send


# ---------------------------------------------------------------- follow-up


def label_for(employer: str, kind: str = "application") -> str:
    base = {"application": "Career Scout/Applied", "reply": "Career Scout/Reply",
            "outreach": "Career Scout/Outreach"}[kind]
    # Gmail treats "/" as nesting; an employer name must not create extra levels.
    company = " ".join(employer.replace("/", "-").split())[:80] or "Unknown"
    return f"{base}/{company}" if kind == "application" else base


def application_record(conn: sqlite3.Connection, pkg: dict[str, Any],
                       application: sqlite3.Row) -> dict[str, Any]:
    """What the Drive record holds: every claim, its record, and the differences."""
    cited = {s for sources in pkg["claim_trace"].values() for s in sources}
    values = {}
    if cited:
        marks = ",".join("?" * len(cited))
        values = {
            r["id"]: {"field_path": r["field_path"], "value": r["value"]}
            for r in conn.execute(
                f"SELECT id, field_path, value FROM profile_record WHERE id IN ({marks})",  # noqa: S608
                tuple(cited),
            )
        }
    unused = [
        r["field_path"] for r in conn.execute(
            "SELECT field_path, id FROM profile_record WHERE superseded_by IS NULL "
            "AND confirmed = 1 ORDER BY field_path"
        ) if r["id"] not in cited and not r["field_path"].startswith(("identity.", "x_preferences"))
    ]
    return {
        "application": {
            "title": pkg["title"], "employer": pkg["employer"],
            "destination": application["destination"], "sent_at": application["sent_at"],
            "gmail_message_id": application["provider_message_id"],
            "posting_url": pkg["posting_url"],
        },
        "documents": [
            {"type": doc["type"], "sha256": doc["sha256"]} for doc in pkg["documents"]
        ],
        "claims": [
            {"line": line, "backed_by": [values.get(s, {"record_id": s}) for s in sources]}
            for line, sources in pkg["claim_trace"].items()
        ],
        "differs_from_full_profile": {
            "left_out_unconfirmed": [o["field_path"] for o in pkg["omissions"]],
            "confirmed_but_not_used": unused,
        },
        "content_hash": pkg["content_hash"],
        "rendered_hash": pkg["rendered_hash"],
    }


def _upload_record(client: GoogleClient, name: str, record: dict[str, Any]) -> str:
    boundary = "career-scout-record-boundary"
    metadata = json.dumps({"name": name, "mimeType": "application/json"})
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
        f"--{boundary}\r\nContent-Type: application/json\r\n\r\n"
        f"{json.dumps(record, indent=2, ensure_ascii=False)}\r\n--{boundary}--"
    ).encode()
    result = client.call(
        "POST", f"{DRIVE_UPLOAD}?uploadType=multipart", content=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    return f"https://drive.google.com/file/d/{result['id']}/view"


def after_send(conn: sqlite3.Connection, client: GoogleClient, application_id: str) -> list[str]:
    """Label the thread and write the Drive record. Returns the errors, if any."""
    application = conn.execute(
        "SELECT * FROM application WHERE id = ?", (application_id,)
    ).fetchone()
    pkg = package_module.get_package(conn, application["package_id"])
    errors: list[str] = []
    thread_id = label = drive_url = None

    try:
        message = client.get(f"{GMAIL}/messages/{application['provider_message_id']}",
                             format="minimal")
        thread_id = message.get("threadId")
        label = label_for(pkg["employer"], application["kind"])
        client.post(f"{GMAIL}/messages/{application['provider_message_id']}/modify",
                    {"addLabelIds": [label_id(client, label)]})
    except Exception as exc:  # noqa: BLE001 - recorded, never retried as a send
        errors.append(f"label: {exc}")

    if application["kind"] == "application":
        try:
            record = application_record(conn, pkg, application)
            drive_url = _upload_record(
                client,
                f"Career Scout application — {pkg['employer']} — {pkg['title']}.json",
                record,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"drive record: {exc}")

    conn.execute(
        "UPDATE application SET gmail_thread_id = COALESCE(?, gmail_thread_id), "
        "gmail_label = COALESCE(?, gmail_label), drive_record_url = COALESCE(?, drive_record_url), "
        "followup_errors = ? WHERE id = ?",
        (thread_id, label, drive_url, json.dumps(errors) if errors else None, application_id),
    )
    return errors
