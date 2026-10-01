"""Outreach — I-28 and the rules around it. T083 to T086.

A contact exists only because an employer published their address on a page,
and it stays only until they object. Every test here is about one of those two.
"""

from __future__ import annotations

import sqlite3

import pytest

from career_scout import outreach, send, service
from career_scout.profile import records
from career_scout.store import settings as settings_module
from career_scout.tests.invariants.test_documents import _opportunity, _profile

pytestmark = pytest.mark.invariant

PAGE = """<html><body><h1>Process Engineer</h1>
<p>Questions about this role? Contact Sara Malik, Plant Engineering Manager,
at sara.malik@acme.example.</p></body></html>"""
URL = "https://acme.example/jobs/1"


def _fetch(page: str = PAGE):
    return lambda url: page


def _mailbox(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO credential (id, provider, account_label, scopes, connected_at) "
                 "VALUES ('c1', 'google', 'me@example.org', '[]', '2026-09-01T00:00:00Z')")


def test_no_generated_contacts(db) -> None:
    """A guessed or pattern-built address cannot be stored: it is not on the page."""
    with pytest.raises(outreach.NotPublished, match="does not appear"):
        outreach.add_contact(db, email="s.malik@acme.example", name="Sara Malik",
                             published_source_url=URL, fetch=_fetch())
    with pytest.raises(sqlite3.IntegrityError):          # and the schema refuses no source
        db.execute("INSERT INTO contact (id, name, email, published_source_url, "
                   "published_read_at, created_at) VALUES ('x', 'n', 'e@x.y', NULL, 't', 't')")
    assert db.execute("SELECT count(*) c FROM contact").fetchone()["c"] == 0


def test_a_published_contact_is_stored_with_its_snapshot(db) -> None:
    contact = outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                                   published_source_url=URL, fetch=_fetch())
    row = db.execute("SELECT * FROM contact WHERE id = ?", (contact["id"],)).fetchone()
    assert row["published_source_url"] == URL and row["published_read_at"]
    assert contact["snapshot"]


def test_a_name_not_on_the_page_is_not_stored(db) -> None:
    with pytest.raises(outreach.NotPublished, match="name"):
        outreach.add_contact(db, email="sara.malik@acme.example", name="Sarah Mallik",
                             published_source_url=URL, fetch=_fetch())


def test_contacts_are_discovered_only_from_the_posting_page(db) -> None:
    """T083a — the posting itself, read for addresses and the name beside them."""
    _profile(db)
    opportunity_id = _opportunity(db, description=(
        "Questions? Contact Sara Malik, Plant Engineering Manager, at sara.malik@acme.example. "
        "Do not reply to noreply@acme.example."))
    found = outreach.discover_from_posting(db, opportunity_id)
    emails = {c["email"] for c in found}
    assert emails == {"sara.malik@acme.example"}          # no-reply and apply address excluded
    assert found[0]["name"] == "Sara Malik"


def test_a_referee_in_the_users_own_documents_never_enters_outreach(db) -> None:
    """T086 — someone the user named as a referee is not a cold-contact prospect."""
    records.add(db, "references[0].email", "sara.malik@acme.example", confirmed=True)
    with pytest.raises(outreach.NotPublished, match="your own"):
        outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                             published_source_url=URL, fetch=_fetch())


def test_contact_erasure_honoured(db) -> None:
    """I-28 — erasure removes the person, and the suppression outlives the row."""
    contact = outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                                   published_source_url=URL, fetch=_fetch())
    outreach.erase(db, contact["id"])

    row = db.execute("SELECT * FROM contact WHERE id = ?", (contact["id"],)).fetchone()
    assert row["erased_at"] and "sara" not in (row["name"] + row["email"]).lower()
    assert send.suppressed(db, "Sara.Malik@acme.example")      # case-insensitive, by hash
    stored = db.execute("SELECT email_sha256 FROM contact_suppression").fetchone()
    assert "sara" not in stored["email_sha256"]                  # a hash, not an address

    # Global and permanent: the same address cannot come back, from any page.
    with pytest.raises(outreach.NotPublished, match="asked not to be contacted"):
        outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                             published_source_url=URL, fetch=_fetch())


def test_an_approved_outreach_is_refused_after_the_contact_objects(db) -> None:
    """Suppression is checked in the send path, not only at drafting (T086)."""
    _profile(db)
    _mailbox(db)
    opportunity_id = _opportunity(db)
    contact = outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                                   published_source_url=URL, fetch=_fetch(),
                                   opportunity_id=opportunity_id)
    pkg = outreach.draft(db, contact["id"], opportunity_id)
    service.approve_package(db, pkg["id"], pkg["content_hash"], from_loopback=True)
    settings_module.set_value(db, "channel_email_autosend", True)
    settings_module.set_value(db, "channel_outreach", True)

    outreach.suppress(db, contact["id"])
    approval_id = db.execute("SELECT id FROM approval").fetchone()["id"]
    sent: list = []
    with pytest.raises(send.SendRefused) as refused:
        send.send_approved(db, approval_id, expected_content_hash=pkg["content_hash"],
                           transport=lambda e: sent.append(e) or "x", mailbox_credential_id="c1")
    assert refused.value.condition == 4 and sent == []


def test_outreach_needs_its_own_opt_in(db) -> None:
    _profile(db)
    _mailbox(db)
    opportunity_id = _opportunity(db)
    contact = outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                                   published_source_url=URL, fetch=_fetch())
    pkg = outreach.draft(db, contact["id"], opportunity_id)
    service.approve_package(db, pkg["id"], pkg["content_hash"], from_loopback=True)
    settings_module.set_value(db, "channel_email_autosend", True)     # outreach still off

    approval_id = db.execute("SELECT id FROM approval").fetchone()["id"]
    with pytest.raises(send.SendRefused) as refused:
        send.send_approved(db, approval_id, expected_content_hash=pkg["content_hash"],
                           transport=lambda e: "x", mailbox_credential_id="c1")
    assert refused.value.condition == 6


def test_every_outreach_message_carries_the_notice(db) -> None:
    """T085 — who holds the details, the page they came from, and how to object."""
    _profile(db)
    opportunity_id = _opportunity(db)
    contact = outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                                   published_source_url=URL, fetch=_fetch())
    pkg = outreach.draft(db, contact["id"], opportunity_id)
    text = "\n".join(line["text"] for line in pkg["drafts"][0]["lines"])
    assert URL in text
    assert "ayesha@example.org" in text                   # who holds the details
    assert "reply" in text.lower() and "remove" in text.lower()
    assert pkg["qc_verdict"] == "pass", pkg["qc_findings"]
    assert pkg["destination"] == "sara.malik@acme.example"


def test_a_removal_reply_suppresses_the_contact(db) -> None:
    """The route to object works: a reply asking to be removed is honoured by the poller."""
    contact = outreach.add_contact(db, email="sara.malik@acme.example", name="Sara Malik",
                                   published_source_url=URL, fetch=_fetch())
    handled = outreach.handle_objection(db, "sara.malik@acme.example",
                                        "Please remove me from your list.")
    assert handled
    assert send.suppressed(db, "sara.malik@acme.example")
    row = db.execute("SELECT erased_at FROM contact WHERE id = ?", (contact["id"],)).fetchone()
    assert row["erased_at"]
