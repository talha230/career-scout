"""Application status and reply corrections — T063, T064.

Every status change writes ``application_status_history`` with the previous
value, the new value, the time and the actor (``human``, ``gmail_poll``,
``run:<id>``). Nothing is overwritten without that row.

An **automatic** transition only moves forward. Replies arrive out of order —
an ATS acknowledgement can land after the interview invitation — and a late
"we received your application" must not move an interview back to
"acknowledged". A **human** transition may set any status: the person knows
things the mailbox does not.

A correction to a reply's classification keeps the original (T063): the rule
engine's answer stays readable beside the person's, so a rule that keeps being
corrected is visible as a rule worth fixing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from career_scout.gmail.classify import STATUS_FOR, STORED_CLASSES
from career_scout.store.db import utcnow

#: Forward order for automatic transitions. Terminal states sit apart.
_RANK = {"sending": 0, "submitted": 1, "acknowledged": 2, "info_requested": 3,
         "interview": 4, "offer": 5}
_TERMINAL = {"rejected", "withdrawn"}
STATUSES = frozenset(_RANK) | _TERMINAL


def transition(
    conn: sqlite3.Connection,
    application_id: str,
    to_status: str,
    *,
    actor: str,
    note: str | None = None,
) -> bool:
    """Move an application to ``to_status``. Returns whether anything changed."""
    if to_status not in STATUSES:
        raise ValueError(f"unknown status {to_status!r}")
    row = conn.execute("SELECT status FROM application WHERE id = ?",
                       (application_id,)).fetchone()
    if row is None:
        raise KeyError(f"no application {application_id}")
    current = row["status"]
    if current == to_status:
        return False
    if actor != "human":
        if current in _TERMINAL:
            return False
        if to_status not in _TERMINAL and _RANK[to_status] <= _RANK.get(current, 0):
            return False
    now = utcnow()
    conn.execute("UPDATE application SET status = ? WHERE id = ?", (to_status, application_id))
    conn.execute(
        "INSERT INTO application_status_history "
        "(application_id, from_status, to_status, actor, note, changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (application_id, current, to_status, actor, note, now),
    )
    return True


def correct_reply(conn: sqlite3.Connection, reply_id: str, classification: str) -> dict[str, Any]:
    """The person's reading of a reply, kept beside the rule engine's."""
    if classification not in STORED_CLASSES:
        raise ValueError(f"{classification!r} is not one of {sorted(STORED_CLASSES)}")
    row = conn.execute("SELECT * FROM reply WHERE id = ?", (reply_id,)).fetchone()
    if row is None:
        raise KeyError(f"no reply {reply_id}")
    now = utcnow()
    conn.execute(
        "UPDATE reply SET classification_corrected_to = ?, corrected_at = ? WHERE id = ?",
        (classification, now, reply_id),
    )
    moved = False
    if row["application_id"] and classification in STATUS_FOR:
        moved = transition(
            conn, row["application_id"], STATUS_FOR[classification], actor="human",
            note=f"reply reclassified from {row['classification']} to {classification}",
        )
    return {"reply_id": reply_id, "original": row["classification"],
            "corrected_to": classification, "status_changed": moved}


def link_reply(conn: sqlite3.Connection, reply_id: str, application_id: str) -> dict[str, Any]:
    """Attach an unlinked reply to an application by hand. It was retained for this."""
    reply = conn.execute("SELECT * FROM reply WHERE id = ?", (reply_id,)).fetchone()
    if reply is None:
        raise KeyError(f"no reply {reply_id}")
    if conn.execute("SELECT 1 FROM application WHERE id = ?", (application_id,)).fetchone() is None:
        raise KeyError(f"no application {application_id}")
    conn.execute("UPDATE reply SET application_id = ?, link_basis = 'human' WHERE id = ?",
                 (application_id, reply_id))
    effective = reply["classification_corrected_to"] or reply["classification"]
    if effective in STATUS_FOR:
        transition(conn, application_id, STATUS_FOR[effective], actor="human",
                   note="reply linked by hand")
    return {"reply_id": reply_id, "application_id": application_id}


def history(conn: sqlite3.Connection, application_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT from_status, to_status, actor, note, changed_at FROM application_status_history "
        "WHERE application_id = ? ORDER BY id", (application_id,),
    )]


def replies(conn: sqlite3.Connection, *, application_id: str | None = None,
            unlinked_only: bool = False) -> list[dict[str, Any]]:
    sql = ("SELECT id, application_id, gmail_thread_id, from_address, subject, received_at, "
           "classification, classification_confidence, classification_corrected_to, "
           "corrected_at, link_basis, classification_detail FROM reply")
    clauses, params = [], []
    if application_id:
        clauses.append("application_id = ?")
        params.append(application_id)
    if unlinked_only:
        clauses.append("application_id IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(sql + " ORDER BY received_at DESC", params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["classification_detail"] = json.loads(r["classification_detail"] or "null")
        item["effective_classification"] = (r["classification_corrected_to"]
                                            or r["classification"])
        out.append(item)
    return out
