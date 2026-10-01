"""Recording a human approval — T051, T070.

An approval binds five things, not two: the content, the rendered bytes, the
destination, the route, and the mailbox it will leave from. :mod:`jarvis.send`
checks all five again at send time and refuses on any difference.

It lives outside :mod:`jarvis.send` on purpose: the send module must not be able
to create what it checks (I-07, T050), and nothing in the MCP server calls this
(I-25). The web app is the only caller of :func:`approve`, and only for a
request from this machine. :func:`approve_by_rules` runs in the daily pipeline
on rules only the user can write, from this machine (I-30).

Three refusals worth naming:

**The caller must name the content hash it showed the person.** An approval of
"package X" would approve whatever package X holds at the moment of the click; an
approval of "package X with hash H" approves only what was on the screen.

**Only from this machine.** With no login, anyone on the Wi-Fi can reach the web
app. Browsing the queue from a phone is fine; approving a send is the one act
that must be the user's own (owner decision 2026-09-24).

**One package at a time.** There is no list parameter and no bulk path.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from jarvis import send
from jarvis.documents import package as package_module
from jarvis.store import settings as settings_module
from jarvis.store.db import immediate, utcnow


class ApprovalRefused(Exception):
    """The approval was not recorded, and why."""


def current_mailbox(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The connected Google account a send would leave from, if any."""
    return conn.execute(
        "SELECT * FROM credential WHERE provider = 'google' AND revoked_at IS NULL "
        "ORDER BY connected_at DESC LIMIT 1"
    ).fetchone()


def approve(
    conn: sqlite3.Connection,
    package_id: str,
    *,
    content_hash: str,
    from_loopback: bool,
) -> dict[str, Any]:
    """Approve one package for sending. Returns the approval summary."""
    if not from_loopback:
        raise ApprovalRefused(
            "approval is accepted only on this computer, not over the network — "
            "open Jarvis at http://localhost to approve"
        )
    with immediate(conn):
        return _record(conn, package_id, content_hash=content_hash)


def _record(
    conn: sqlite3.Connection,
    package_id: str,
    *,
    content_hash: str,
    rule_basis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every check an approval needs, then the row. Runs inside the caller's transaction."""
    package = conn.execute(
        "SELECT p.*, o.apply_target, o.title, o.employer FROM application_package p "
        "JOIN opportunity o ON o.id = p.opportunity_id WHERE p.id = ?",
        (package_id,),
    ).fetchone()
    if package is None:
        raise ApprovalRefused(f"no package {package_id}")
    if package["qc_verdict"] != "pass":
        raise ApprovalRefused(f"this package failed QC: {package['blocked_reason']}")
    if package["superseded_by"]:
        raise ApprovalRefused("a newer version of this package exists; review that one")
    if package["state"] != "awaiting_approval":
        raise ApprovalRefused(f"this package is {package['state']}, not awaiting approval")
    if package["content_sha256"] != content_hash:
        raise ApprovalRefused(
            "the package changed after you opened it; reload and review it again"
        )
    if package["route"] != "email":
        raise ApprovalRefused(
            "this is a portal application: Jarvis sends nothing for it. Submit the "
            "worksheet on the employer's site, then record that you did"
        )

    destination = package_module.destination_for(conn, package_id)
    if not destination:
        raise ApprovalRefused("there is no address to send this to")
    if package["kind"] == "outreach" and send.suppressed(conn, destination):
        raise ApprovalRefused("this contact has asked not to be contacted")
    mailbox = current_mailbox(conn)
    if mailbox is None:
        raise ApprovalRefused("connect your Gmail first (jarvis setup google)")
    if mailbox["needs_renewal"]:
        raise ApprovalRefused(
            "Google has disconnected your mailbox; reconnect it (jarvis setup google)"
        )

    approval_id = str(uuid.uuid4())
    now = utcnow()
    snapshot = {
        "destination": destination,
        "route": "email",
        "channel": "email",
        "kind": package["kind"],
        "title": package["title"],
        "employer": package["employer"],
        "mailbox": mailbox["account_label"],
        "captured_at": now,
    }
    conn.execute(
        "INSERT INTO approval (id, package_id, approved_by, approved_at, content_hash, "
        "rendered_hash, destination_hash, destination_snapshot, mailbox_credential_id, "
        "rule_basis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            approval_id, package_id, "human" if rule_basis is None else "rule", now,
            content_hash, package["rendered_sha256"],
            send.destination_hash(destination, "email", package["opportunity_id"]),
            json.dumps(snapshot), mailbox["id"],
            None if rule_basis is None else json.dumps(rule_basis, sort_keys=True),
        ),
    )
    conn.execute(
        "UPDATE application_package SET state = 'approved' WHERE id = ?", (package_id,)
    )
    return {"approval_id": approval_id, "package_id": package_id,
            "approved_by": "human" if rule_basis is None else "rule", **snapshot}


# ------------------------------------------------------------ rule approval


def approve_by_rules(conn: sqlite3.Connection) -> dict[str, Any]:
    """I-30 — approve application packages that satisfy the user's own written rules.

    Off until the user switches it on and writes the rules, and only they can, from
    this computer (``HUMAN_ONLY_KEYS``). Each package is judged on its own, through
    the same checks as a human approval, and the row records the rules and the
    inputs that satisfied them. Nothing is sent here: a rule approval waits in the
    queue for the same nine-condition send gate as any other.
    """
    get = settings_module.get
    if not get(conn, "auto_approve_enabled"):
        return {"approved": 0, "skipped": "rule approval is switched off"}
    min_score = get(conn, "auto_approve_min_score")
    countries = sorted({str(c).upper() for c in get(conn, "auto_approve_countries") or []})
    if min_score is None or not countries:
        return {"approved": 0, "skipped": "rule approval is on but its rules are not written: "
                                          "set auto_approve_min_score and auto_approve_countries"}
    rules = {
        "kind": "application", "min_score": float(min_score), "countries": countries,
        "confidence": ["high", "medium"], "eligibility": "eligible",
        "sufficiency": "sufficient", "warnings": "none",
        "max_per_day": int(get(conn, "auto_approve_max_per_day")),
    }
    candidates = conn.execute(
        "SELECT p.id FROM application_package p JOIN opportunity o ON o.id = p.opportunity_id "
        "WHERE p.state = 'awaiting_approval' AND p.superseded_by IS NULL "
        "ORDER BY o.deadline IS NULL, o.deadline, p.generated_at"
    ).fetchall()
    approved: list[str] = []
    held: list[dict[str, str]] = []
    for candidate in candidates:
        with immediate(conn):
            now = utcnow()
            used = conn.execute(
                "SELECT COUNT(*) FROM approval WHERE approved_by = 'rule' "
                "AND substr(approved_at, 1, 10) = ?", (now[:10],),
            ).fetchone()[0]
            if used >= rules["max_per_day"]:
                held.append({"package_id": candidate["id"],
                             "reason": "today's rule-approval limit is reached"})
                continue
            reason, inputs = _check_rules(conn, candidate["id"], rules, now)
            if reason is None:
                try:
                    _record(conn, candidate["id"], content_hash=inputs.pop("content_sha256"),
                            rule_basis={"rules": rules, "inputs": inputs})
                except ApprovalRefused as refused:
                    reason = str(refused)
            if reason is None:
                approved.append(candidate["id"])
            else:
                held.append({"package_id": candidate["id"], "reason": reason})
    return {"approved": len(approved), "approved_ids": approved, "held": held}


def _check_rules(
    conn: sqlite3.Connection, package_id: str, rules: dict[str, Any], now: str
) -> tuple[str | None, dict[str, Any]]:
    """The first rule this package breaks, or None, and the inputs that were judged."""
    row = conn.execute(
        "SELECT p.kind, p.warnings, p.content_sha256, o.id opportunity_id, o.country_iso2, "
        "o.deadline, v.verdict sufficiency FROM application_package p "
        "JOIN opportunity o ON o.id = p.opportunity_id "
        "LEFT JOIN sufficiency_verdict v ON v.opportunity_id = o.id WHERE p.id = ?",
        (package_id,),
    ).fetchone()
    assessment = conn.execute(
        "SELECT match_score, confidence, eligibility_verdict, config_version FROM assessment "
        "WHERE opportunity_id = ? ORDER BY computed_at DESC LIMIT 1", (row["opportunity_id"],),
    ).fetchone()
    inputs: dict[str, Any] = {
        "content_sha256": row["content_sha256"], "country": row["country_iso2"],
        "deadline": row["deadline"], "sufficiency": row["sufficiency"],
        "score": assessment["match_score"] if assessment else None,
        "confidence": assessment["confidence"] if assessment else None,
        "eligibility": assessment["eligibility_verdict"] if assessment else None,
        "assessment_config_version": assessment["config_version"] if assessment else None,
    }
    if row["kind"] != rules["kind"]:
        return "replies and outreach always need a person", inputs
    if json.loads(row["warnings"] or "[]"):
        return "it carries warnings a person must read", inputs
    if (row["country_iso2"] or "").upper() not in rules["countries"]:
        return f"{row['country_iso2'] or 'no country'} is not in the allowed countries", inputs
    if row["deadline"] and row["deadline"] < now:
        return "the deadline has passed", inputs
    if inputs["sufficiency"] != rules["sufficiency"]:
        return f"sufficiency is {inputs['sufficiency'] or 'not evaluated'}", inputs
    if inputs["score"] is None:
        return "the match is UNSCORED", inputs
    if inputs["score"] < rules["min_score"]:
        return f"score {inputs['score']:.1f} is below {rules['min_score']:g}", inputs
    if inputs["confidence"] not in rules["confidence"]:
        return f"score confidence is {inputs['confidence'] or 'unknown'}", inputs
    if inputs["eligibility"] != rules["eligibility"]:
        return f"eligibility is {inputs['eligibility'] or 'unknown'}", inputs
    return None, inputs


def record_manual_submission(
    conn: sqlite3.Connection, package_id: str, *, note: str | None = None
) -> dict[str, Any]:
    """The user submitted a portal application themselves. Nothing was sent by Jarvis.

    Recorded so it appears in /applications and the duplicate check, with the
    posting URL as its destination and the user as the actor.
    """
    with immediate(conn):
        package = conn.execute(
            "SELECT p.*, o.source_url FROM application_package p "
            "JOIN opportunity o ON o.id = p.opportunity_id WHERE p.id = ?",
            (package_id,),
        ).fetchone()
        if package is None:
            raise ApprovalRefused(f"no package {package_id}")
        if package["route"] != "portal":
            raise ApprovalRefused("only a portal application is submitted by hand")
        if package["state"] == "submitted":
            raise ApprovalRefused("already recorded as submitted")

        application_id = str(uuid.uuid4())
        now = utcnow()
        conn.execute(
            "INSERT INTO application (id, package_id, opportunity_id, channel, status, "
            "sent_at, destination, submission_evidence, created_at) "
            "VALUES (?, ?, ?, 'manual', 'submitted', ?, ?, ?, ?)",
            (application_id, package_id, package["opportunity_id"], now,
             package["source_url"] or "employer portal", note, now),
        )
        conn.execute(
            "INSERT INTO application_status_history "
            "(application_id, from_status, to_status, actor, note, changed_at) "
            "VALUES (?, NULL, 'submitted', 'human', ?, ?)",
            (application_id, note or "submitted by hand on the employer's site", now),
        )
        conn.execute(
            "UPDATE application_package SET state = 'submitted' WHERE id = ?", (package_id,)
        )
    return {"application_id": application_id, "package_id": package_id, "status": "submitted"}
