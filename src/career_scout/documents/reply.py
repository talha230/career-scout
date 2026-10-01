"""Drafting a response to a classified reply — T065.

**An auto-reply is a send, and no send skips the gate.** The draft becomes a
package of kind ``reply``; it passes the same QC, is approved by a person
against its content hash, and leaves through :func:`career_scout.send.send_approved`
under the same caps. "Auto" means the drafting is automatic, never the sending.

The drafts are short and deliberately commit to nothing the profile does not
hold: no availability, no salary, no start date, no answer to a question the
person has not answered. An **offer** is never answered here — the category
config says to respond yourself, and a model-shaped reply to an offer is the
last place to be clever.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from career_scout.documents import generate
from career_scout.documents import package as package_module

#: What a reply of each class may say. Template text only — no claims.
_OPENINGS = {
    "acknowledgement": "Thank you for confirming that my application was received.",
    "interview_invite": (
        "Thank you for the invitation. I would be glad to speak with you, and I will "
        "reply shortly with the times that suit me."
    ),
    "information_request": (
        "Thank you for your message. I will reply with the information you asked for."
    ),
    "rejection": "Thank you for letting me know, and for considering my application.",
}

class NoReplyDrafted(Exception):
    """This reply is not one Career Scout drafts a response to."""


def draft_reply_package(conn: sqlite3.Connection, reply_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT r.*, a.opportunity_id FROM reply r "
        "LEFT JOIN application a ON a.id = r.application_id WHERE r.id = ?", (reply_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no reply {reply_id}")
    if row["application_id"] is None:
        raise NoReplyDrafted("link this reply to its application first")
    effective = row["classification_corrected_to"] or row["classification"]
    if effective == "offer":
        raise NoReplyDrafted("an offer is answered by you, not drafted by Career Scout")
    if effective not in _OPENINGS:
        raise NoReplyDrafted(f"no draft for a reply classified {effective!r}")

    profile = generate.Profile(conn)
    posting = generate.load_posting(conn, row["opportunity_id"])
    draft = generate.Draft("reply")
    draft.add(_OPENINGS[effective], generate.PARAGRAPH, generate.TEMPLATE, "body")
    paths = [p for p in ("basics.name", "basics.email", "basics.phone") if profile.get(p)]
    if paths:
        draft.add(" · ".join(profile.text(p) or "" for p in paths), generate.CONTENT,
                  generate.PROFILE, "signature", profile.ids(*paths))

    return package_module.store_message_package(
        conn, kind="reply", opportunity_id=row["opportunity_id"], draft=draft,
        posting={"title": posting["title"], "employer": posting["employer"],
                 "source_url": posting.get("source_url") or ""},
        omissions=[], reply_id=reply_id,
    )
