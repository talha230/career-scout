"""The send gate — I-01 to I-06, I-14 to I-16.

Each test here corresponds to a numbered invariant in PLAN.md §3. They are never
skipped. Each one exists because breaking it either sends something nobody
approved, or sends something twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid

import pytest

from jarvis import send
from jarvis.store import settings as settings_module
from jarvis.store.db import connect, utcnow

CONTENT_HASH = hashlib.sha256(b"the package a human read").hexdigest()
RENDERED_HASH = hashlib.sha256(b"the bytes that will be attached").hexdigest()
MAILBOX = "credential-1"
DESTINATION = "careers@acme.example"


def _destination_hash(destination: str, opportunity_id: str) -> str:
    # Uses the module's own function on purpose: a test that reimplements the
    # hash passes while the two sides drift apart.
    return send.destination_hash(destination, "email", opportunity_id)


def _seed(
    conn: sqlite3.Connection,
    *,
    destination: str = DESTINATION,
    content_hash: str = CONTENT_HASH,
    rendered_hash: str = RENDERED_HASH,
    mailbox: str = MAILBOX,
    deadline: str | None = None,
    country: str | None = "US",
    qc: str = "pass",
) -> tuple[str, str]:
    """Create one opportunity, package and approval. Returns (approval_id, package_id)."""
    if country:
        conn.execute(
            "INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES (?, ?, 1)",
            (country, country),
        )
    opportunity_id = str(uuid.uuid4())
    now = utcnow()
    conn.execute(
        "INSERT INTO opportunity (id, kind, title, employer, country_iso2, deadline, "
        " source_url, fetched_at, dedupe_key, employer_norm, title_norm, "
        " first_seen_at, last_seen_at) "
        "VALUES (?, 'job', 'Industrial Engineer', 'Acme', ?, ?, "
        " 'https://acme.example/1', ?, ?, 'acme', 'industrial engineer', ?, ?)",
        (opportunity_id, country, deadline, now, str(uuid.uuid4()), now, now),
    )

    package_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO application_package (id, opportunity_id, documents, claim_trace, "
        " rendered_sha256, qc_verdict, blocked_reason, state, generated_at) "
        "VALUES (?, ?, '[]', '{}', ?, ?, ?, 'approved', ?)",
        (
            package_id,
            opportunity_id,
            rendered_hash,
            qc,
            None if qc == "pass" else "blocked for a reason",
            now,
        ),
    )

    approval_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO approval (id, package_id, approved_by, approved_at, content_hash, "
        " rendered_hash, destination_hash, destination_snapshot, mailbox_credential_id) "
        "VALUES (?, ?, 'human', ?, ?, ?, ?, ?, ?)",
        (
            approval_id,
            package_id,
            now,
            content_hash,
            rendered_hash,
            _destination_hash(destination, opportunity_id),
            json.dumps({"destination": destination, "route": "email"}),
            mailbox,
        ),
    )
    return approval_id, package_id


@pytest.fixture
def ready(db: sqlite3.Connection) -> sqlite3.Connection:
    """A database with the email channel opted in, so the gate can be exercised."""
    settings_module.set_value(db, "channel_email_autosend", True)
    return db


def _transport(message_id: str | None = None):
    """A transport that records what it was asked to send.

    Each call returns a distinct provider id unless one is pinned:
    ``application.provider_message_id`` is UNIQUE, which is what stops one
    provider message being recorded as two applications.
    """
    sent: list[send.Envelope] = []

    def transport(envelope: send.Envelope) -> str:
        sent.append(envelope)
        return message_id or f"provider-{uuid.uuid4()}"

    transport.sent = sent            # type: ignore[attr-defined]
    return transport


def _send(conn, approval_id, **overrides):
    kwargs = {
        "expected_content_hash": CONTENT_HASH,
        "transport": _transport(),
        "mailbox_credential_id": MAILBOX,
    }
    kwargs.update(overrides)
    return send.send_approved(conn, approval_id, **kwargs)


class TestApprovalRequired:
    """I-01 — no send without a matching approval record."""

    def test_an_unknown_approval_is_refused(self, ready):
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, "no-such-approval")
        assert raised.value.condition == 1

    def test_nothing_is_sent_when_the_approval_is_missing(self, ready):
        transport = _transport()
        with pytest.raises(send.SendRefused):
            _send(ready, "no-such-approval", transport=transport)
        assert transport.sent == []

    def test_a_blocked_package_is_refused(self, ready):
        approval_id, _ = _seed(ready, qc="blocked")
        with pytest.raises(send.SendRefused):
            _send(ready, approval_id)

    def test_an_approved_package_sends(self, ready):
        approval_id, _ = _seed(ready)
        result = _send(ready, approval_id, transport=_transport("provider-1"))
        assert result.provider_message_id == "provider-1"
        assert result.destination == DESTINATION


class TestContentBinding:
    """I-02 — content changed after approval is refused."""

    def test_a_changed_content_hash_is_refused(self, ready):
        approval_id, _ = _seed(ready)
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id, expected_content_hash="something-else")
        assert raised.value.condition == 2

    def test_a_rerendered_document_is_refused(self, ready):
        # The bytes were hashed at generation and that hash was approved.
        # Re-rendering produces different bytes nobody agreed to.
        approval_id, package_id = _seed(ready)
        ready.execute(
            "UPDATE application_package SET rendered_sha256 = 'different' WHERE id = ?",
            (package_id,),
        )
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id)
        assert raised.value.condition == 3

    def test_a_refused_send_calls_no_provider(self, ready):
        approval_id, _ = _seed(ready)
        transport = _transport()
        with pytest.raises(send.SendRefused):
            _send(ready, approval_id, expected_content_hash="x", transport=transport)
        assert transport.sent == []


class TestDestinationBinding:
    """I-03, I-04 — destination and mailbox are bound at approval."""

    def test_a_changed_destination_is_refused(self, ready):
        approval_id, _ = _seed(ready)
        ready.execute(
            "UPDATE approval SET destination_hash = 'moved' WHERE id = ?", (approval_id,)
        )
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id)
        assert raised.value.condition == 4

    def test_a_different_mailbox_is_refused(self, ready):
        # Reconnecting a different Google account between approval and send
        # changes the sender identity the recipient sees.
        approval_id, _ = _seed(ready)
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id, mailbox_credential_id="another-credential")
        assert raised.value.condition == 5

    def test_the_transport_gets_the_approved_destination(self, ready):
        approval_id, _ = _seed(ready, destination="jobs@other.example")
        transport = _transport()
        _send(ready, approval_id, transport=transport)
        assert transport.sent[0].destination == "jobs@other.example"


class TestChannelOptIn:
    """T053 — every channel is off until switched on."""

    def test_a_channel_that_is_not_opted_in_refuses(self, db):
        approval_id, _ = _seed(db)
        with pytest.raises(send.SendRefused) as raised:
            _send(db, approval_id)
        assert raised.value.condition == 6

    def test_an_unknown_channel_refuses(self, ready):
        approval_id, _ = _seed(ready)
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id, channel="portal")
        assert raised.value.condition == 6


class TestDeadline:
    def test_a_passed_deadline_refuses(self, ready):
        approval_id, _ = _seed(ready, deadline="2020-01-01T00:00:00Z")
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id)
        assert raised.value.condition == 7

    def test_no_deadline_is_not_a_passed_deadline(self, ready):
        approval_id, _ = _seed(ready, deadline=None)
        assert _send(ready, approval_id).provider_message_id


class TestSingleUse:
    """I-05 — an approval is consumed exactly once."""

    def test_a_second_send_is_refused(self, ready):
        approval_id, _ = _seed(ready)
        _send(ready, approval_id)
        with pytest.raises(send.SendRefused):
            _send(ready, approval_id, now=utcnow())

    def test_concurrent_senders_send_once(self, ready, paths):
        # Checking consumed_at and then updating it is a race two senders win
        # together. The claim is a conditional UPDATE for exactly this reason.
        approval_id, _ = _seed(ready)
        settings_module.set_value(ready, "min_send_interval_seconds", 0)

        barrier = threading.Barrier(6)
        results: list[str] = []
        failures: list[Exception] = []
        lock = threading.Lock()

        def attempt() -> None:
            conn = connect(paths)
            try:
                barrier.wait(timeout=10)
                result = send.send_approved(
                    conn,
                    approval_id,
                    expected_content_hash=CONTENT_HASH,
                    transport=_transport(),
                    mailbox_credential_id=MAILBOX,
                )
                with lock:
                    results.append(result.application_id)
            except send.SendRefused as exc:
                with lock:
                    failures.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == 1, f"sent {len(results)} times"
        assert len(failures) == 5
        assert ready.execute("SELECT count(*) c FROM application").fetchone()["c"] == 1


class TestReconciliation:
    """I-06 — a send that succeeded but failed to record is reconciled, never re-sent."""

    def test_the_application_is_reserved_before_the_provider_is_called(self, ready):
        approval_id, _ = _seed(ready)
        seen: list[int] = []

        def transport(envelope: send.Envelope) -> str:
            # At this moment the row must already exist, in state 'sending'.
            row = ready.execute(
                "SELECT status FROM application ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            seen.append(row is not None and row["status"] == "sending")
            return "provider-9"

        _send(ready, approval_id, transport=transport)
        assert seen == [True]

    def test_a_crashed_send_leaves_a_reconcilable_row(self, ready):
        approval_id, _ = _seed(ready)

        def exploding(envelope: send.Envelope) -> str:
            raise RuntimeError("process died after the provider accepted it")

        with pytest.raises(RuntimeError):
            _send(ready, approval_id, transport=exploding)

        pending = send.unreconciled(ready)
        assert len(pending) == 1
        assert pending[0]["status"] == "sending"

    def test_reconciling_records_without_sending_again(self, ready):
        approval_id, _ = _seed(ready)

        def exploding(envelope: send.Envelope) -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            _send(ready, approval_id, transport=exploding)

        pending = send.unreconciled(ready)[0]
        send.reconcile(ready, pending["id"], "provider-found-at-gmail")

        row = ready.execute(
            "SELECT * FROM application WHERE id = ?", (pending["id"],)
        ).fetchone()
        assert row["status"] == "submitted"
        assert row["provider_message_id"] == "provider-found-at-gmail"
        assert send.unreconciled(ready) == []

    def test_reconciling_twice_is_harmless(self, ready):
        approval_id, _ = _seed(ready)
        result = _send(ready, approval_id, transport=_transport("provider-1"))
        again = send.reconcile(ready, result.application_id, "provider-1")
        assert again.reconciled is True
        assert ready.execute("SELECT count(*) c FROM application").fetchone()["c"] == 1

    def test_a_consumed_approval_is_not_released_by_a_failed_send(self, ready):
        # The approval was spent. Releasing it on failure would let a send that
        # may already have reached the provider be retried.
        approval_id, _ = _seed(ready)

        def exploding(envelope: send.Envelope) -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            _send(ready, approval_id, transport=exploding)

        consumed = ready.execute(
            "SELECT consumed_at FROM approval WHERE id = ?", (approval_id,)
        ).fetchone()["consumed_at"]
        assert consumed is not None


class TestCaps:
    """I-14, I-15 — caps hold, and a cap refusal leaves the approval queued."""

    def test_the_global_cap_refuses(self, ready):
        settings_module.set_value(ready, "cap_total_per_day", 2)
        settings_module.set_value(ready, "min_send_interval_seconds", 0)
        for _ in range(2):
            approval_id, _ = _seed(ready)
            _send(ready, approval_id)

        approval_id, _ = _seed(ready)
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id)
        assert raised.value.condition == 8

    def test_the_per_country_cap_refuses(self, ready):
        settings_module.set_value(ready, "cap_per_destination_per_day", 1)
        settings_module.set_value(ready, "cap_total_per_day", 50)
        settings_module.set_value(ready, "min_send_interval_seconds", 0)

        approval_id, _ = _seed(ready, country="US")
        _send(ready, approval_id)

        approval_id, _ = _seed(ready, country="US")
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id)
        assert raised.value.condition == 8

    def test_another_country_still_has_room(self, ready):
        settings_module.set_value(ready, "cap_per_destination_per_day", 1)
        settings_module.set_value(ready, "min_send_interval_seconds", 0)

        approval_id, _ = _seed(ready, country="US")
        _send(ready, approval_id)

        approval_id, _ = _seed(ready, country="DE")
        assert _send(ready, approval_id).provider_message_id

    def test_a_cap_refusal_leaves_the_approval_valid(self, ready):
        # I-15. The item stays queued, so raising the cap tomorrow sends it.
        settings_module.set_value(ready, "cap_total_per_day", 0)
        approval_id, _ = _seed(ready)

        with pytest.raises(send.SendRefused):
            _send(ready, approval_id)

        row = ready.execute(
            "SELECT consumed_at FROM approval WHERE id = ?", (approval_id,)
        ).fetchone()
        assert row["consumed_at"] is None
        assert [r["id"] for r in send.queue(ready)] == [approval_id]

    def test_a_cap_refusal_writes_no_application(self, ready):
        settings_module.set_value(ready, "cap_total_per_day", 0)
        approval_id, _ = _seed(ready)
        with pytest.raises(send.SendRefused):
            _send(ready, approval_id)
        assert ready.execute("SELECT count(*) c FROM application").fetchone()["c"] == 0

    def test_the_minimum_interval_refuses_a_burst(self, ready):
        settings_module.set_value(ready, "min_send_interval_seconds", 240)
        approval_id, _ = _seed(ready)
        _send(ready, approval_id, now="2026-09-01T10:00:00Z")

        approval_id, _ = _seed(ready)
        with pytest.raises(send.SendRefused) as raised:
            _send(ready, approval_id, now="2026-09-01T10:01:00Z")
        assert raised.value.condition == 9

    def test_the_interval_passes_once_elapsed(self, ready):
        settings_module.set_value(ready, "min_send_interval_seconds", 240)
        approval_id, _ = _seed(ready)
        _send(ready, approval_id, now="2026-09-01T10:00:00Z")

        approval_id, _ = _seed(ready)
        assert _send(ready, approval_id, now="2026-09-01T10:05:00Z").provider_message_id

    def test_caps_are_settings_not_constants(self, ready):
        # I-23. Raising the cap is a settings change, never a code change.
        settings_module.set_value(ready, "cap_total_per_day", 1)
        settings_module.set_value(ready, "min_send_interval_seconds", 0)
        approval_id, _ = _seed(ready)
        _send(ready, approval_id)

        approval_id, _ = _seed(ready)
        with pytest.raises(send.SendRefused):
            _send(ready, approval_id)

        settings_module.set_value(ready, "cap_total_per_day", 5)
        assert _send(ready, approval_id).provider_message_id


class TestQueueOrder:
    """I-16 — the queue releases in deadline order, nulls last."""

    def test_the_nearest_deadline_comes_first(self, ready):
        late, _ = _seed(ready, deadline="2099-12-01T00:00:00Z")
        soon, _ = _seed(ready, deadline="2099-10-01T00:00:00Z")
        undated, _ = _seed(ready, deadline=None)

        assert [row["id"] for row in send.queue(ready)] == [soon, late, undated]

    def test_a_consumed_approval_leaves_the_queue(self, ready):
        approval_id, _ = _seed(ready, deadline="2099-10-01T00:00:00Z")
        _send(ready, approval_id)
        assert send.queue(ready) == []


class TestNoBulkPath:
    """I-07 — there is no bulk approve, and no bulk send."""

    def test_send_takes_one_approval_at_a_time(self):
        import inspect

        signature = inspect.signature(send.send_approved)
        assert "approval_id" in signature.parameters
        for name in signature.parameters:
            assert not name.endswith("_all")
            assert "ids" not in name

    def test_no_function_here_creates_an_approval(self):
        # Approval is a human act in the web app. Nothing in the send path may
        # manufacture one.
        for name in dir(send):
            assert "approve" not in name.lower() or name == "send_approved"
