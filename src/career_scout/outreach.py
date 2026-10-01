"""Outreach to a published decision-maker — T083 to T086, I-28.

**A contact exists only because an employer published it.** :func:`add_contact`
fetches the page through ``career_scout.net``, stores a snapshot, and refuses unless the
address — and the name, when one is given — appears **verbatim** on that page.
It is the same check I-22 applies to a sourced figure, and it is what makes a
guessed ``firstname.lastname@`` pattern impossible to store: that address is not
on any page. ``published_source_url`` is ``NOT NULL`` in the schema, so the rule
does not depend on this function being the only door.

**A contact stays only until they object.** :func:`suppress` (do not contact)
and :func:`erase` (erase me) write a SHA-256 of the address to
``contact_suppression`` — global, permanent, holding less about the person than
the contact row did. The send path checks it (``career_scout.send``, condition 4), so
an approval made before the objection is refused after it. Erasure also blanks
the contact's name and address in place.

**The route to object works.** Every message carries a one-sentence notice
(T085): who holds the details, the page they came from, and that replying
"remove" ends it. :func:`handle_objection` is what the Gmail poller calls with a
reply from a contact, and it honours that reply.

A referee the user named in their own documents never enters outreach (T086):
the address is refused if it appears in any of the user's profile records.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from collections.abc import Callable
from typing import Any

from career_scout.documents import generate
from career_scout.documents import package as package_module
from career_scout.store import snapshots
from career_scout.store.db import utcnow

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NO_REPLY = re.compile(r"^(no-?reply|donotreply|do-not-reply|mailer-daemon|bounce)", re.I)
#: "Contact Sara Malik, Plant Engineering Manager, at sara@..." — a capitalised
#: name of two to four words just before the address, optionally with a title.
_NAME_BEFORE = re.compile(
    r"([A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){1,3})(?:\s*,\s*([^,@\n]{3,60}?))?"
    r"\s*(?:,|\(|at|via|on|:|-)?\s*(?:at\s*)?$"
)
#: Capitalised words that start a sentence before a name, not part of it.
_LEAD_WORDS = frozenset({"contact", "email", "reach", "write", "questions", "please", "ask",
                         "send", "message", "call", "apply", "to", "or"})
_OBJECTION = re.compile(
    r"\b(remove me|unsubscribe|do not contact|don'?t contact|stop contacting|erase|"
    r"delete my (data|details|information)|take me off|opt out)\b|^\s*remove\b",
    re.I | re.M,
)


class NotPublished(Exception):
    """The contact was not stored, and why."""


def _digest(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _suppressed(conn: sqlite3.Connection, email: str) -> bool:
    return conn.execute("SELECT 1 FROM contact_suppression WHERE email_sha256 = ?",
                        (_digest(email),)).fetchone() is not None


def _in_own_profile(conn: sqlite3.Connection, email: str) -> bool:
    needle = email.strip().lower()
    return any(needle in (r["value"] or "").lower() for r in conn.execute(
        "SELECT value FROM profile_record WHERE value LIKE ?", (f"%{needle}%",)))


def _default_fetch(url: str) -> str:
    from career_scout import net

    with net.Fetcher() as fetcher:
        response = fetcher.get(url)
        response.raise_for_status()
        return response.text


def add_contact(
    conn: sqlite3.Connection,
    *,
    email: str,
    published_source_url: str,
    name: str = "",
    role: str | None = None,
    institution: str | None = None,
    opportunity_id: str | None = None,
    fetch: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Store a contact read from a page the employer published — or refuse."""
    email = email.strip()
    if not _EMAIL.fullmatch(email):
        raise NotPublished(f"{email!r} is not an email address")
    if _suppressed(conn, email):
        raise NotPublished("this person asked not to be contacted; the request is permanent")
    if _in_own_profile(conn, email):
        raise NotPublished("this address is in your own documents (a referee?); people you "
                           "named are never cold-contacted")
    if not published_source_url.lower().startswith(("https://", "http://")):
        raise NotPublished("a contact needs the web page it was published on")

    page = (fetch or _default_fetch)(published_source_url)
    snapshot = snapshots.store(page, source_url=published_source_url)
    if not snapshots.contains(snapshot.relative_path, email):
        raise NotPublished(f"{email} does not appear on {published_source_url}; only an "
                           f"address the employer published can be stored")
    if name and not snapshots.contains(snapshot.relative_path, name):
        raise NotPublished(f"the name {name!r} does not appear on that page as written")

    contact_id = str(uuid.uuid4())
    now = utcnow()
    conn.execute(
        "INSERT INTO contact (id, name, role, institution, email, published_source_url, "
        "published_read_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (contact_id, name, role, institution, email, published_source_url, now, now),
    )
    return {"id": contact_id, "email": email, "name": name, "role": role,
            "published_source_url": published_source_url,
            "snapshot": snapshot.relative_path, "opportunity_id": opportunity_id}


def discover_from_posting(conn: sqlite3.Connection, opportunity_id: str) -> list[dict[str, Any]]:
    """T083a — addresses the posting itself publishes, with the name beside each.

    Candidates only; nothing is stored until :func:`add_contact` verifies the
    address against the fetched page. The apply address and no-reply senders
    are not contacts. A role with no published contact stays without one.
    """
    row = conn.execute("SELECT description, apply_target, source_url FROM opportunity "
                       "WHERE id = ?", (opportunity_id,)).fetchone()
    if row is None:
        raise KeyError(f"no opportunity {opportunity_id}")
    text = row["description"] or ""
    apply_target = (row["apply_target"] or "").lower()
    found: list[dict[str, Any]] = []
    for match in _EMAIL.finditer(text):
        email = match.group(0).rstrip(".")
        if email.lower() == apply_target or _NO_REPLY.match(email):
            continue
        if any(c["email"].lower() == email.lower() for c in found):
            continue
        before = text[max(0, match.start() - 120):match.start()]
        named = _NAME_BEFORE.search(before.rstrip())
        name = ""
        if named:
            words = named.group(1).split()
            while words and words[0].lower() in _LEAD_WORDS:
                words.pop(0)
            name = " ".join(words) if len(words) >= 2 else ""
        found.append({
            "email": email,
            "name": name,
            "role": (named.group(2) or "").strip() or None if named else None,
            "published_source_url": row["source_url"],
            "suppressed": _suppressed(conn, email),
        })
    return found


def list_contacts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, role, institution, email, published_source_url, published_read_at, "
        "do_not_contact, notice_sent_at, erased_at, created_at FROM contact "
        "ORDER BY created_at DESC")]


def suppress(conn: sqlite3.Connection, contact_id: str, *, kind: str = "do_not_contact") -> None:
    row = conn.execute("SELECT email FROM contact WHERE id = ?", (contact_id,)).fetchone()
    if row is None:
        raise KeyError(f"no contact {contact_id}")
    if row["email"]:
        conn.execute("INSERT OR IGNORE INTO contact_suppression (email_sha256, kind, requested_at) "
                     "VALUES (?, ?, ?)", (_digest(row["email"]), kind, utcnow()))
    conn.execute("UPDATE contact SET do_not_contact = 1 WHERE id = ?", (contact_id,))
    # Anything drafted for them and not yet sent stops here too.
    conn.execute("UPDATE application_package SET state = 'blocked', "
                 "blocked_reason = 'the contact asked not to be contacted' "
                 "WHERE contact_id = ? AND state IN ('generated', 'awaiting_approval', 'approved')",
                 (contact_id,))


def erase(conn: sqlite3.Connection, contact_id: str) -> None:
    """I-28 — suppress permanently, then remove the person's details in place."""
    suppress(conn, contact_id, kind="erasure")
    conn.execute("UPDATE contact SET name = '', email = '', role = NULL, institution = NULL, "
                 "erased_at = ? WHERE id = ?", (utcnow(), contact_id))


def handle_objection(conn: sqlite3.Connection, sender: str, body: str) -> bool:
    """A reply from a contact asking to be removed. Returns whether it was honoured."""
    if not _OBJECTION.search(body or ""):
        return False
    rows = conn.execute("SELECT id FROM contact WHERE lower(email) = ? AND erased_at IS NULL",
                        (sender.strip().lower(),)).fetchall()
    for row in rows:
        erase(conn, row["id"])
    if not rows:
        conn.execute("INSERT OR IGNORE INTO contact_suppression (email_sha256, kind, requested_at) "
                     "VALUES (?, 'erasure', ?)", (_digest(sender), utcnow()))
    return True


def draft(conn: sqlite3.Connection, contact_id: str, opportunity_id: str) -> dict[str, Any]:
    """T084 — one outreach message, through the same approval and send gate."""
    contact = conn.execute("SELECT * FROM contact WHERE id = ?", (contact_id,)).fetchone()
    if contact is None:
        raise KeyError(f"no contact {contact_id}")
    if contact["erased_at"] or contact["do_not_contact"] or _suppressed(conn, contact["email"]):
        raise NotPublished("this person asked not to be contacted")

    profile = generate.Profile(conn)
    posting = generate.load_posting(conn, opportunity_id)
    message = generate.Draft("outreach")
    message.add(f"I am writing about the {posting['title']} position at {posting['employer']}.",
                generate.PARAGRAPH, generate.POSTING, "body")
    label = profile.get("basics.label")
    if label:
        message.add(f"I am {generate.article(label[0])} {label[0]}.", generate.PARAGRAPH,
                    generate.PROFILE, "body", (label[1],))
    summary = profile.get("basics.summary")
    if summary:
        message.add(summary[0], generate.PARAGRAPH, generate.PROFILE, "body", (summary[1],))
    message.add("I would welcome the chance to discuss the role.", generate.PARAGRAPH,
                generate.TEMPLATE, "body")
    contact_line = [p for p in ("basics.name", "basics.email") if profile.get(p)]
    if contact_line:
        message.add(" · ".join(profile.text(p) or "" for p in contact_line), generate.CONTENT,
                    generate.PROFILE, "signature", profile.ids(*contact_line))
    # T085 — one sentence and a working route to object. The page URL and the
    # sender's own address come from the contact row and the profile.
    holder = profile.get("basics.email")
    message.add(
        f"Notice: {holder[0] if holder else 'the sender'} holds your work address, found on "
        f"{contact['published_source_url']}; reply remove and it will be deleted and you will "
        f"not be contacted again.",
        generate.PARAGRAPH, generate.NOTICE, "notice", (holder[1],) if holder else (),
    )
    return package_module.store_message_package(
        conn, kind="outreach", opportunity_id=opportunity_id, draft=message,
        posting={"title": posting["title"], "employer": posting["employer"],
                 "source_url": contact["published_source_url"]},
        omissions=[], contact_id=contact_id,
    )
