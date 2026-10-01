"""Reading replies from the user's mailbox — T061, T062, T063.

Incremental: ``history.list`` from the stored history id, then ``messages.get``
for each new message. With no history id yet — the first poll, or one Google
has expired — it reads the threads of the user's own applications instead,
then records the mailbox's current history id to continue from.

**What is kept, and what is not looked at.** A message is stored only when it
belongs to the thread of an application Jarvis sent, or comes from the domain an
application was sent to. Everything else in the mailbox is skipped without its
body being stored: this is the user's personal email, not a dataset.

A message from an application's domain that matches **several** applications is
kept *unlinked*, never guessed onto one (T063): "which of the three roles at this
company is this about?" is a question for the person.

A disconnected mailbox is reported as exactly that (T059). The poll returns
``state: 'disconnected'`` with the reason, and the run records it as an
obstacle — never as "0 new replies".
"""

from __future__ import annotations

import base64
import html
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse

from jarvis import tracking
from jarvis.gmail import classify
from jarvis.gmail.google import (
    GMAIL,
    GoogleClient,
    GoogleError,
    MailboxDisconnected,
    label_id,
)
from jarvis.store.db import utcnow

_LABEL_FOR = {
    "interview_invite": "Jarvis/Interview",
    "offer": "Jarvis/Offer",
    "rejection": "Jarvis/Rejected",
}
_TAG = re.compile(r"<[^>]+>")


def _domain(address: str) -> str:
    address = address.strip().lower()
    if "@" in address:
        return address.rsplit("@", 1)[1]
    return (urlparse(address).hostname or "").lower()


def _same_org(sender_domain: str, target_domain: str) -> bool:
    return bool(target_domain) and (
        sender_domain == target_domain or sender_domain.endswith("." + target_domain)
    )


def _header(message: dict[str, Any], name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def body_text(payload: dict[str, Any]) -> str:
    """Plain text of a message: text/plain preferred, else text/html stripped."""
    plain: list[str] = []
    rich: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            rich.append(html.unescape(_TAG.sub(" ", _decode(data))))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain) if plain else "\n".join(rich)
    return " ".join(text.split())[:20000]


# --------------------------------------------------------------- matching


def _match(conn: sqlite3.Connection, thread_id: str, sender: str) -> tuple[str | None, str | None]:
    """``(application_id, link_basis)``; ``(None, None)`` means not ours — skip it."""
    row = conn.execute(
        "SELECT id, kind FROM application WHERE gmail_thread_id = ? "
        "ORDER BY created_at DESC LIMIT 1", (thread_id,),
    ).fetchone()
    if row:
        return row["id"], "thread" if row["kind"] == "application" else f"{row['kind']}_thread"

    sender_domain = _domain(sender)
    candidates = [
        r["id"] for r in conn.execute(
            "SELECT id, destination FROM application WHERE kind = 'application'"
        ) if _same_org(sender_domain, _domain(r["destination"]))
    ]
    if len(candidates) == 1:
        return candidates[0], "sender_domain"
    if len(candidates) > 1:
        return None, "sender_domain_ambiguous"     # kept, unlinked, for the person
    return None, None


def _store(conn: sqlite3.Connection, client: GoogleClient, message: dict[str, Any],
           own_address: str) -> str | None:
    """Classify and store one message if it is ours. Returns the stored class."""
    if "SENT" in message.get("labelIds", []) or "DRAFT" in message.get("labelIds", []):
        return None
    sender = parseaddr(_header(message, "From"))[1]
    if not sender or sender.lower() == own_address.lower():
        return None
    if conn.execute("SELECT 1 FROM reply WHERE gmail_message_id = ?",
                    (message["id"],)).fetchone():
        return None

    application_id, basis = _match(conn, message.get("threadId", ""), sender)
    if basis is None:
        return None                                   # not about an application

    subject = _header(message, "Subject")
    text = body_text(message.get("payload", {}))
    if basis == "outreach_thread":
        # The notice promised that replying "remove" ends it (T085). Honour it
        # before anything else is done with the message.
        from jarvis import outreach

        outreach.handle_objection(conn, sender, text)
    result = classify.classify(subject, text, sender)
    received = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, UTC)

    conn.execute(
        "INSERT INTO reply (id, application_id, gmail_message_id, gmail_thread_id, from_address, "
        "subject, received_at, classification, classification_confidence, body_text, "
        "link_basis, classification_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), application_id, message["id"], message.get("threadId", ""), sender,
         subject, received.isoformat().replace("+00:00", "Z"), result.stored, result.confidence,
         text, basis, json.dumps(result.detail())),
    )
    # A reply to outreach or to an auto-reply does not move an application's status.
    moves_status = basis in ("thread", "sender_domain")
    if application_id and moves_status and result.stored in classify.STATUS_FOR:
        tracking.transition(conn, application_id, classify.STATUS_FOR[result.stored],
                            actor="gmail_poll", note=f"reply classified {result.label}")
    if application_id:
        _label(client, message["id"], result.stored)
    return result.stored


def _label(client: GoogleClient, message_id: str, stored: str) -> None:
    names = ["Jarvis/Reply"] + ([_LABEL_FOR[stored]] if stored in _LABEL_FOR else [])
    try:
        ids = [label_id(client, name) for name in names]
        client.post(f"{GMAIL}/messages/{message_id}/modify", {"addLabelIds": ids})
    except GoogleError:
        pass        # a missing label is cosmetic; the reply is stored either way


# ------------------------------------------------------------------- poll


def _new_message_ids(client: GoogleClient, history_id: str) -> list[str]:
    ids: list[str] = []
    page: str | None = None
    while True:
        params: dict[str, Any] = {"startHistoryId": history_id, "historyTypes": "messageAdded"}
        if page:
            params["pageToken"] = page
        result = client.get(f"{GMAIL}/history", **params)
        for record in result.get("history", []):
            for added in record.get("messagesAdded", []):
                ids.append(added["message"]["id"])
        page = result.get("nextPageToken")
        if not page:
            return ids


def _thread_message_ids(conn: sqlite3.Connection, client: GoogleClient) -> list[str]:
    """The resync set: every application thread, plus mail from application domains.

    The domain search catches a reply that started a new thread before there was
    a history id to follow. It asks Gmail only for senders at the domains the
    user applied to, so nothing else in the mailbox is listed.
    """
    ids: list[str] = []
    for row in conn.execute(
        "SELECT DISTINCT gmail_thread_id FROM application WHERE gmail_thread_id IS NOT NULL"
    ):
        thread = client.get(f"{GMAIL}/threads/{row['gmail_thread_id']}", format="minimal")
        ids.extend(m["id"] for m in thread.get("messages", []))

    domains = sorted({
        _domain(r["destination"]) for r in conn.execute(
            "SELECT destination FROM application WHERE kind = 'application' AND channel = 'email'"
        ) if "@" in (r["destination"] or "")
    })
    if domains:
        query = "from:(" + " OR ".join(domains) + ") newer_than:90d"
        found = client.get(f"{GMAIL}/messages", q=query, maxResults=100)
        ids.extend(m["id"] for m in found.get("messages", []))
    return ids


def poll(conn: sqlite3.Connection, client: GoogleClient, *, own_address: str) -> dict[str, Any]:
    """One polling pass. Always returns a state; never silently empty."""
    state = conn.execute("SELECT * FROM mailbox_state WHERE credential_id = ?",
                         (client.credential_id,)).fetchone()
    counts: dict[str, int] = {"examined": 0, "stored": 0}
    try:
        profile = client.get(f"{GMAIL}/profile")
        mode = "incremental"
        if state and state["history_id"]:
            try:
                ids = _new_message_ids(client, state["history_id"])
            except GoogleError as exc:
                if "404" not in str(exc):
                    raise
                mode, ids = "resync", _thread_message_ids(conn, client)
        else:
            mode, ids = "resync", _thread_message_ids(conn, client)

        for message_id in dict.fromkeys(ids):
            counts["examined"] += 1
            message = client.get(f"{GMAIL}/messages/{message_id}", format="full")
            stored = _store(conn, client, message, own_address)
            if stored:
                counts["stored"] += 1
                counts[stored] = counts.get(stored, 0) + 1
        _save(conn, client.credential_id, profile.get("historyId"), "ok", None)
        return {"state": "ok", "mode": mode, "counts": counts}
    except MailboxDisconnected as exc:
        _save(conn, client.credential_id, state["history_id"] if state else None,
              "disconnected", str(exc))
        return {"state": "disconnected", "message": str(exc), "counts": counts}
    except GoogleError as exc:
        _save(conn, client.credential_id, state["history_id"] if state else None, "error", str(exc))
        return {"state": "error", "message": str(exc), "counts": counts}


def _save(conn: sqlite3.Connection, credential_id: str, history_id: str | None,
          status: str, error: str | None) -> None:
    conn.execute(
        "INSERT INTO mailbox_state (credential_id, history_id, last_polled_at, last_status, "
        "last_error) VALUES (?, ?, ?, ?, ?) ON CONFLICT(credential_id) DO UPDATE SET "
        "history_id = COALESCE(excluded.history_id, history_id), "
        "last_polled_at = excluded.last_polled_at, last_status = excluded.last_status, "
        "last_error = excluded.last_error",
        (credential_id, history_id, utcnow(), status, error),
    )
