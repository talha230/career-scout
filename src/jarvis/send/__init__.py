"""The single send path — T051, T052.

**Everything outbound goes through :func:`send_approved`.** An application, an
outreach message and an auto-reply are all sends, and no send skips this gate.
There is exactly one function that calls a provider, so there is exactly one
place to read to know what can leave the machine.

Nine conditions, counted once here and cited rather than re-listed elsewhere:

===  ======================================================================
 1   the approval exists
 2   the content hash still matches what was approved          (I-02)
 3   the rendered hash still matches the bytes approved        (I-02)
 4   the destination still matches what was approved           (I-03)
 5   the sending mailbox is the one bound at approval          (I-04)
 6   the channel is opted in                                   (T053)
 7   the deadline has not passed
 8   the per-country and global caps have room                 (I-14)
 9   the minimum interval since the last send has elapsed      (I-14)
===  ======================================================================

Three structural decisions, each of which is a bug that would otherwise exist:

**Verify before claiming.** A cap refusal must leave the approval valid and
queued (I-15), so nothing is written until every condition has passed. The whole
check runs inside ``BEGIN IMMEDIATE``, which takes the write lock up front —
a deferred transaction lets two callers both read "19 of 20 used" and both
proceed.

**Claim by conditional UPDATE.** ``SET consumed_at = ? WHERE id = ? AND
consumed_at IS NULL`` succeeds for exactly one caller (I-05). Checking and then
updating is a race two concurrent senders win together.

**Reserve before calling the provider.** The ``application`` row is written in
state ``sending`` *before* Gmail is called. Gmail offers no idempotency key, so
a send that succeeds while its follow-up write fails must be recoverable by
reconciliation, never by retrying — retrying sends the same application twice
(I-06).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jarvis.store import settings as settings_module
from jarvis.store.db import immediate, utcnow

APPLICATION = "application"
OUTREACH = "outreach"
REPLY = "reply"

#: Which setting opts each channel in. Every one defaults to False (T053).
_CHANNEL_SETTING = {
    "email": "channel_email_autosend",
    "published_api": "channel_published_api",
}


def destination_hash(destination: str, channel: str, opportunity_id: str) -> str:
    """Bind a recipient, a route and an opportunity into one value.

    The same function is used when an approval is recorded and when a send is
    attempted, so the comparison at send time is between the address the human
    saw and the address the posting carries *now*. An employer that changes its
    apply address after approval gets a refusal, not a message sent somewhere
    nobody agreed to.
    """
    return hashlib.sha256(f"{destination}|{channel}|{opportunity_id}".encode()).hexdigest()


class SendRefused(Exception):
    """A condition failed. The approval is untouched and stays queued.

    Carries the condition number so a refusal can be reported precisely rather
    than as "something went wrong".
    """

    def __init__(self, condition: int, reason: str) -> None:
        super().__init__(f"condition {condition}: {reason}")
        self.condition = condition
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SendResult:
    application_id: str
    approval_id: str
    provider_message_id: str | None
    destination: str
    reconciled: bool = False


@dataclass(frozen=True, slots=True)
class Envelope:
    """What the transport is asked to send. Built from the approved row, never
    re-rendered: the bytes were hashed at generation and that hash was approved."""

    destination: str
    mailbox_credential_id: str
    package_id: str
    rendered_sha256: str


#: A transport takes an envelope and returns the provider's message id.
Transport = Callable[[Envelope], str]


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _verify(
    conn: sqlite3.Connection,
    approval: sqlite3.Row,
    package: sqlite3.Row,
    *,
    expected_content_hash: str,
    current_destination: str,
    mailbox_credential_id: str,
    channel: str,
    destination_country: str | None,
    now: str,
) -> None:
    """The nine conditions. Raises on the first failure, writes nothing."""
    # 2, 3 — the content and the bytes are the ones a human read and approved.
    if approval["content_hash"] != expected_content_hash:
        raise SendRefused(2, "package content changed after approval")
    if approval["rendered_hash"] != package["rendered_sha256"]:
        raise SendRefused(3, "rendered document changed after approval")

    # 6 — every channel is off until switched on. Checked before the destination
    # because the channel is one of the three things the destination hash binds:
    # an unknown channel would otherwise surface as "destination changed", which
    # is true but useless to whoever has to act on it.
    setting = _CHANNEL_SETTING.get(channel)
    if setting is None:
        raise SendRefused(6, f"channel {channel!r} has no automated send path")
    if not settings_module.get(conn, setting):
        raise SendRefused(6, f"channel {channel!r} is not opted in")
    # Outreach is its own decision: switching on email for applications does not
    # switch on writing to people who never received one.
    if _kind_of(package) == OUTREACH and not settings_module.get(conn, "channel_outreach"):
        raise SendRefused(6, "outreach is not opted in")

    # 4 — approving a message to one recipient never authorises another. The
    # hash is recomputed from where the posting points NOW, so an employer that
    # changed its apply address after approval is refused rather than written to.
    recomputed = destination_hash(current_destination, channel, package["opportunity_id"])
    if approval["destination_hash"] != recomputed:
        raise SendRefused(4, "destination changed after approval")

    # 5 — reconnecting a different Google account between approval and send
    # changes the sender identity the recipient sees.
    if approval["mailbox_credential_id"] != mailbox_credential_id:
        raise SendRefused(5, "sending mailbox is not the one bound at approval")

    # 7 — a deadline that has passed makes the send pointless, not merely late.
    deadline = conn.execute(
        "SELECT o.deadline FROM opportunity o "
        "JOIN application_package p ON p.opportunity_id = o.id WHERE p.id = ?",
        (package["id"],),
    ).fetchone()
    if deadline and deadline["deadline"] and deadline["deadline"] < now:
        raise SendRefused(7, f"deadline passed ({deadline['deadline']})")

    per_country, total, min_interval = settings_module.cap_settings(conn)
    window_start = (_parse(now) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    # 8 — caps protect the user's own mailbox reputation, which is the real risk.
    used_total = conn.execute(
        "SELECT count(*) c FROM send_log WHERE sent_at >= ?", (window_start,)
    ).fetchone()["c"]
    if used_total >= total:
        raise SendRefused(8, f"global cap reached ({used_total}/{total} in 24h)")

    if destination_country:
        used_country = conn.execute(
            "SELECT count(*) c FROM send_log WHERE sent_at >= ? AND destination_country = ?",
            (window_start, destination_country),
        ).fetchone()["c"]
        if used_country >= per_country:
            raise SendRefused(
                8,
                f"cap reached for {destination_country} ({used_country}/{per_country} in 24h)",
            )

    # 9 — a burst looks like automation to a mail provider even under the cap.
    last = conn.execute("SELECT max(sent_at) last FROM send_log").fetchone()["last"]
    if last:
        elapsed = (_parse(now) - _parse(last)).total_seconds()
        if elapsed < min_interval:
            raise SendRefused(
                9, f"minimum interval not elapsed ({elapsed:.0f}s of {min_interval}s)"
            )


def send_approved(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    expected_content_hash: str,
    transport: Transport,
    mailbox_credential_id: str,
    channel: str = "email",
    kind: str = APPLICATION,
    now: str | None = None,
) -> SendResult:
    """Send one approved package. The only function that calls a provider.

    ``expected_content_hash`` is required and is the caller's statement of what
    it believes it is sending. An MCP tool takes it from the model, so a model
    must name the content rather than send whatever is queued (I-25).

    ``transport`` is injected so the gate is testable without a network and so
    this module never imports a provider.
    """
    moment = now or utcnow()

    with immediate(conn):
        approval = conn.execute(
            "SELECT * FROM approval WHERE id = ?", (approval_id,)
        ).fetchone()
        if approval is None:
            raise SendRefused(1, f"no approval {approval_id!r}")          # I-01

        package = conn.execute(
            "SELECT * FROM application_package WHERE id = ?", (approval["package_id"],)
        ).fetchone()
        if package is None:
            raise SendRefused(1, "approval points at no package")
        if package["qc_verdict"] != "pass":
            raise SendRefused(1, "package did not pass QC")

        destination = _destination_of(approval)
        country = _country_of(conn, package["opportunity_id"])
        # The package says what it is; a caller cannot relabel an outreach
        # message as an application to dodge the suppression check.
        kind = _kind_of(package) or kind

        _verify(
            conn,
            approval,
            package,
            expected_content_hash=expected_content_hash,
            current_destination=_current_destination(conn, package, destination),
            mailbox_credential_id=mailbox_credential_id,
            channel=channel,
            destination_country=country,
            now=moment,
        )

        # Claim. Exactly one caller wins this, whatever the concurrency (I-05).
        claimed = conn.execute(
            "UPDATE approval SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (moment, approval_id),
        )
        if claimed.rowcount != 1:
            raise SendRefused(1, "approval already consumed")

        # Reserve before the provider is called, so a send that succeeds and
        # then fails to record is reconcilable rather than repeatable (I-06).
        application_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO application "
            "(id, package_id, opportunity_id, channel, status, destination, created_at, kind) "
            "VALUES (?, ?, ?, ?, 'sending', ?, ?, ?)",
            (
                application_id,
                package["id"],
                package["opportunity_id"],
                channel,
                destination,
                moment,
                kind,
            ),
        )
        conn.execute(
            "INSERT INTO send_log (sent_at, destination_country, kind, reference_id) "
            "VALUES (?, ?, ?, ?)",
            (moment, country, kind, application_id),
        )
        conn.execute(
            "INSERT INTO application_status_history "
            "(application_id, from_status, to_status, actor, changed_at) "
            "VALUES (?, NULL, 'sending', ?, ?)",
            (application_id, "human", moment),
        )

    envelope = Envelope(
        destination=destination,
        mailbox_credential_id=mailbox_credential_id,
        package_id=package["id"],
        rendered_sha256=package["rendered_sha256"],
    )
    provider_message_id = transport(envelope)

    return reconcile(conn, application_id, provider_message_id, now=moment)


def reconcile(
    conn: sqlite3.Connection,
    application_id: str,
    provider_message_id: str,
    *,
    now: str | None = None,
) -> SendResult:
    """Record the provider's answer against a reserved application.

    Idempotent on ``provider_message_id``: called twice with the same id, the
    second call reports the same result rather than writing a second row. This is
    the recovery path for a send that reached the provider while the process
    died before recording it — the fix for that is always reconciliation, never
    another send.
    """
    moment = now or utcnow()
    with immediate(conn):
        row = conn.execute(
            "SELECT * FROM application WHERE id = ?", (application_id,)
        ).fetchone()
        if row is None:
            raise SendRefused(1, f"no application {application_id!r}")

        already = row["provider_message_id"]
        if already is not None:
            return SendResult(
                application_id=application_id,
                approval_id="",
                provider_message_id=already,
                destination=row["destination"],
                reconciled=True,
            )

        conn.execute(
            "UPDATE application SET status = 'submitted', sent_at = ?, provider_message_id = ? "
            "WHERE id = ?",
            (moment, provider_message_id, application_id),
        )
        conn.execute(
            "INSERT INTO application_status_history "
            "(application_id, from_status, to_status, actor, changed_at) "
            "VALUES (?, 'sending', 'submitted', ?, ?)",
            (application_id, "human", moment),
        )

    return SendResult(
        application_id=application_id,
        approval_id="",
        provider_message_id=provider_message_id,
        destination=row["destination"],
    )


def unreconciled(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Applications reserved but never recorded — the reconciliation queue.

    A row here means the provider may or may not have sent. It is resolved by
    looking the message up at the provider, never by sending again.
    """
    return conn.execute(
        "SELECT * FROM application WHERE status = 'sending' ORDER BY created_at ASC"
    ).fetchall()


def queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Approved, unconsumed packages in the order they should be released — I-16.

    Ascending deadline, **nulls last**, then approval time. A long backlog
    otherwise sends tomorrow's deadline next week.
    """
    return conn.execute(
        "SELECT a.*, o.deadline FROM approval a "
        "JOIN application_package p ON p.id = a.package_id "
        "JOIN opportunity o ON o.id = p.opportunity_id "
        "WHERE a.consumed_at IS NULL "
        "ORDER BY o.deadline IS NULL, o.deadline ASC, a.approved_at ASC"
    ).fetchall()


def _destination_of(approval: sqlite3.Row) -> str:
    snapshot: dict[str, Any] = json.loads(approval["destination_snapshot"])
    destination = snapshot.get("destination")
    if not destination:
        raise SendRefused(4, "approved destination snapshot names no destination")
    return str(destination)


def _kind_of(package: sqlite3.Row) -> str | None:
    return package["kind"] if "kind" in package.keys() else None  # noqa: SIM118 - sqlite3.Row


def suppressed(conn: sqlite3.Connection, address: str) -> bool:
    """T086 — has this address asked never to be contacted, or to be erased?

    Held as a hash, globally and permanently, so honouring it outlives the
    contact row and any reinstall that restores a backup.
    """
    digest = hashlib.sha256(address.strip().lower().encode()).hexdigest()
    return conn.execute(
        "SELECT 1 FROM contact_suppression WHERE email_sha256 = ?", (digest,)
    ).fetchone() is not None


def _current_destination(
    conn: sqlite3.Connection, package: sqlite3.Row, approved: str
) -> str:
    """Where this message would go *now*, read from its own source row.

    - an application: the posting's apply address, falling back to the approved
      one when the posting carries none (a target that was never there has not
      moved);
    - an auto-reply: the sender of the reply being answered;
    - outreach: the contact's published address — and a contact who has since
      objected or asked to be erased is refused here, whatever was approved.
    """
    kind = _kind_of(package) or APPLICATION
    if kind == REPLY:
        row = conn.execute(
            "SELECT from_address FROM reply WHERE id = ?", (package["reply_id"],)
        ).fetchone()
        return str(row["from_address"]) if row else ""
    if kind == OUTREACH:
        row = conn.execute(
            "SELECT email, do_not_contact, erased_at FROM contact WHERE id = ?",
            (package["contact_id"],),
        ).fetchone()
        blocked = row is None or row["erased_at"] or row["do_not_contact"]
        if blocked or suppressed(conn, row["email"]):
            raise SendRefused(4, "this contact has asked not to be contacted, or was erased")
        return str(row["email"])

    row = conn.execute(
        "SELECT apply_target FROM opportunity WHERE id = ?", (package["opportunity_id"],)
    ).fetchone()
    if row is None or not row["apply_target"]:
        return approved
    return str(row["apply_target"])


def _country_of(conn: sqlite3.Connection, opportunity_id: str) -> str | None:
    row = conn.execute(
        "SELECT country_iso2 FROM opportunity WHERE id = ?", (opportunity_id,)
    ).fetchone()
    return row["country_iso2"] if row else None
