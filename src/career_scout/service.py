"""The application layer — one implementation, three callers.

The CLI, the web app and the MCP server all call these functions. None of them
holds a rule of its own, and none can reach anything the others cannot. That is
what makes "the MCP server never widens what is possible" a structural fact
rather than a promise: there is no second code path for it to widen.

Every function here takes an open connection and returns plain dictionaries, so
the same result serialises to JSON for the web app, to a tool result for MCP,
and to a table for the terminal.

**Nothing in this module sends anything.** Outbound actions live in
:mod:`career_scout.send` behind the approval gate, and are reached from here only
through that module.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from career_scout import __version__
from career_scout import config as config_module
from career_scout.discovery import role_family
from career_scout.profile import functions as profile_functions
from career_scout.profile import records
from career_scout.store import settings as settings_module
from career_scout.store.db import utcnow

# --------------------------------------------------------------------- profile


def profile_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Progress, the checklist, and what to fill in next.

    The percentage is reported as progress and nothing branches on it. The
    checklist is reported as named items, because "your profile is 78%
    complete" tells somebody nothing about what to do.
    """
    progress = profile_functions.completeness(conn)
    checklist = profile_functions.minimum_viable_profile(conn)

    return {
        "completeness": {
            "percentage": progress.percentage,
            "earned": progress.earned,
            "total": progress.total,
            "version": progress.version,
            "note": "progress indicator only; no capability depends on it",
            "missing": [
                {"field_path": m.path, "label": m.label, "weight": m.weight}
                for m in progress.missing
            ],
        },
        "minimum_viable_profile": {
            "satisfied": checklist.satisfied,
            "unlocks": "scheduled discovery, scoring and shortlisting",
            "items": [
                {
                    "key": i.key,
                    "label": i.label,
                    "why": i.rationale,
                    "satisfied": i.satisfied,
                    "still_needed": list(i.missing),
                }
                for i in checklist.items
            ],
        },
        "next_best_actions": [
            {"field_path": u.field_path, "label": u.label, "unlocks_opportunities": u.unlocks}
            for u in profile_functions.ranked_unlocks(conn)
        ],
        "record_counts": {
            "confirmed": len(records.current(conn, confirmed_only=True)),
            "awaiting_your_check": len(
                [r for r in records.current(conn) if not r.confirmed]
            ),
        },
    }


def list_profile_records(
    conn: sqlite3.Connection, *, unconfirmed_only: bool = False
) -> list[dict[str, Any]]:
    """Profile records with their provenance.

    ``source`` distinguishes a value read from a document from one the user
    typed. Restricted paths are listed but their values are withheld.
    """
    out: list[dict[str, Any]] = []
    for record in records.current(conn):
        if unconfirmed_only and record.confirmed:
            continue
        out.append(
            {
                "id": record.id,
                "field_path": record.field_path,
                "value": "[restricted]" if record.restricted else record.value,
                "confirmed": record.confirmed,
                "source": "document" if record.from_document else "typed",
                "document_id": record.document_id,
                "locator": record.locator,
            }
        )
    return out


def confirm_field(conn: sqlite3.Connection, record_id: str) -> dict[str, Any]:
    """Accept an extracted proposal as correct."""
    records.confirm(conn, record_id)
    record = next((r for r in records.current(conn) if r.id == record_id), None)
    return {
        "confirmed": True,
        "field_path": record.field_path if record else None,
        "value": record.value if record and not record.restricted else None,
    }


def correct_field(
    conn: sqlite3.Connection, record_id: str, value: str
) -> dict[str, Any]:
    """Replace a value, keeping the original readable.

    The correction is recorded as **typed**, with no document and no locator,
    because the user supplied it rather than a document stating it.
    """
    new_id = records.supersede(conn, record_id, value)
    return {"superseded": record_id, "new_record_id": new_id, "value": value,
            "source": "typed"}


def set_profile_field(
    conn: sqlite3.Connection, field_path: str, value: str
) -> dict[str, Any]:
    """Add a value the user states directly, such as tax residence."""
    existing = records.get(conn, field_path)
    if existing is not None:
        return correct_field(conn, existing.id, value)
    record_id = records.add(conn, field_path, value, confirmed=True)
    return {"new_record_id": record_id, "field_path": field_path, "value": value,
            "source": "typed"}


# ------------------------------------------------------------- opportunities


def list_opportunities(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    country: str | None = None,
    kind: str | None = None,
    passes_floor_only: bool = False,
) -> list[dict[str, Any]]:
    """The shortlist, ranked, with the age of the data that produced it."""
    clauses = ["o.expired_at IS NULL"]
    params: list[Any] = []
    if country:
        clauses.append("o.country_iso2 = ?")
        params.append(country.upper())
    if kind:
        clauses.append("o.kind = ?")
        params.append(kind)
    if passes_floor_only:
        clauses.append("a.passes_floor = 1")

    params.append(min(limit, 200))

    # `clauses` holds only the literal fragments written above; every value the
    # caller supplied is bound through `params`. Nothing from outside this
    # function reaches the SQL text.
    where = " AND ".join(clauses)
    query = (
        "SELECT o.id, o.kind, o.title, o.employer, o.country_iso2, o.role_family, "  # noqa: S608 - see above
        "       o.deadline, o.apply_route, o.source_url, o.fetched_at, "
        "       a.match_score, a.confidence, a.passes_floor, a.sufficiency, a.computed_at, "
        "       a.eligibility_verdict, a.eligibility_detail, a.pay_basis, "
        "       v.verdict, v.missing_field_paths, "
        "       EXISTS (SELECT 1 FROM rejection j WHERE j.opportunity_id = o.id "
        "               AND j.lifted_at IS NULL) AS rejected "
        "FROM opportunity o "
        "LEFT JOIN assessment a ON a.opportunity_id = o.id "
        "LEFT JOIN sufficiency_verdict v ON v.opportunity_id = o.id "
        "WHERE " + where + " "
        "ORDER BY a.match_score DESC NULLS LAST, o.first_seen_at DESC LIMIT ?"
    )
    rows = conn.execute(query, params).fetchall()

    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "title": r["title"],
            "employer": r["employer"],
            "country": r["country_iso2"],
            "role_family": r["role_family"],
            "role_family_label": role_family.label_for(r["role_family"] or "unclassified"),
            "deadline": r["deadline"],
            "apply_route": r["apply_route"],
            "source_url": r["source_url"],
            "fetched_at": r["fetched_at"],
            # Soft-hide is the caller's decision, not this function's: an
            # ineligible opportunity is returned with its verdict and its reason,
            # so a screen can collapse it and the user can still open it. A list
            # that silently omitted rows would be a deletion nobody could see.
            "eligibility_verdict": r["eligibility_verdict"],
            "eligibility_detail": r["eligibility_detail"],
            "hidden": r["eligibility_verdict"] == "ineligible",
            # Same rule for a hard rejection: returned and flagged, never dropped.
            "rejected": bool(r["rejected"]),
            "pay_basis": r["pay_basis"],
            "match_score": r["match_score"],
            "confidence": r["confidence"],
            "passes_floor": None if r["passes_floor"] is None else bool(r["passes_floor"]),
            "sufficiency": r["verdict"] or r["sufficiency"] or "not_evaluated",
            "missing_fields": json.loads(r["missing_field_paths"] or "[]"),
            "scored_at": r["computed_at"],
        }
        for r in rows
    ]


def get_opportunity(conn: sqlite3.Connection, opportunity_id: str) -> dict[str, Any]:
    """One opportunity with the full arithmetic behind its score."""
    row = conn.execute(
        "SELECT * FROM opportunity WHERE id = ?", (opportunity_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no opportunity {opportunity_id}")

    assessment = conn.execute(
        "SELECT * FROM assessment WHERE opportunity_id = ? ORDER BY computed_at DESC LIMIT 1",
        (opportunity_id,),
    ).fetchone()
    verdict = conn.execute(
        "SELECT * FROM sufficiency_verdict WHERE opportunity_id = ?", (opportunity_id,)
    ).fetchone()

    payload: dict[str, Any] = {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "employer": row["employer"],
        "country": row["country_iso2"],
        "role_family": row["role_family"],
        "work_arrangement": row["work_arrangement"],
        "deadline": row["deadline"],
        "apply_route": row["apply_route"],
        "source_url": row["source_url"],
        "fetched_at": row["fetched_at"],
        "requirements": json.loads(row["requirements"] or "{}"),
        "requirements_confidence": row["requirements_confidence"],
        "pay_disclosed": json.loads(row["pay_disclosed"] or "null"),
        "pay_estimate": json.loads(row["pay_estimate"] or "null"),
    }

    if assessment is not None:
        payload["assessment"] = {
            "match_score": assessment["match_score"],
            "confidence": assessment["confidence"],
            "formula": assessment["formula"],
            "weights": json.loads(assessment["weights"] or "{}"),
            "inputs": json.loads(assessment["inputs"] or "{}"),
            "unscored_components": json.loads(assessment["unscored_components"] or "[]"),
            "projection": json.loads(assessment["projection"] or "null"),
            "passes_floor": None if assessment["passes_floor"] is None
            else bool(assessment["passes_floor"]),
            "floor_applied": assessment["floor_applied"],
            "floor_source": assessment["floor_source"],
            "pay_basis": assessment["pay_basis"],
            "config_version": assessment["config_version"],
            "computed_at": assessment["computed_at"],
        }
        payload["eligibility"] = {
            "verdict": assessment["eligibility_verdict"],
            "detail": assessment["eligibility_detail"],
            "hidden": assessment["eligibility_verdict"] == "ineligible",
            # The four dimensions, each with the posting sentence and the profile
            # fact behind it, are stored inside `inputs` by the matching layer.
            "findings": json.loads(assessment["inputs"] or "{}")
            .get("eligibility", {})
            .get("findings", []),
        }

    # Why this is not in the shortlist, if it is not: every hard filter that
    # fired, naming its rule and quoting what it matched. The posting was kept,
    # so the answer is available rather than inferred from an absence. A lifted
    # rejection is kept and shown as lifted: the rule no longer holds.
    payload["rejections"] = [
        {
            "stage": row["stage"],
            "rule": row["rule"],
            "reason": row["reason"],
            "evidence": row["evidence"],
            "rejected_at": row["rejected_at"],
            "active": row["lifted_at"] is None,
            "lifted_at": row["lifted_at"],
            "lifted_reason": row["lifted_reason"],
        }
        for row in conn.execute(
            "SELECT stage, rule, reason, evidence, rejected_at, lifted_at, lifted_reason "
            "FROM rejection WHERE opportunity_id = ? ORDER BY rejected_at, id",
            (opportunity_id,),
        )
    ]
    payload["rejected"] = any(r["active"] for r in payload["rejections"])

    if verdict is not None:
        payload["sufficiency"] = {
            "verdict": verdict["verdict"],
            "reason": verdict["reason"],
            "rules_applied": json.loads(verdict["rules_applied"] or "[]"),
            "fields_examined": json.loads(verdict["fields_examined"] or "[]"),
            "missing_field_paths": json.loads(verdict["missing_field_paths"] or "[]"),
            "rule_version": verdict["rule_version"],
        }

    return payload


# ---------------------------------------------------------------- settings


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every setting with its effective value. Secrets are masked."""
    return settings_module.all_settings(conn)


class SettingRefused(PermissionError):
    """A setting only a person on this computer may change."""


def set_setting(
    conn: sqlite3.Connection, key: str, value: Any, *, from_loopback: bool = False
) -> dict[str, Any]:
    """Change one setting. Approval-granting keys need a person on this computer (I-30)."""
    if key in settings_module.HUMAN_ONLY_KEYS and not from_loopback:
        raise SettingRefused(
            f"{key} decides what is approved without you, so it can be changed only in "
            "the web app on this computer (http://localhost), not by an assistant or "
            "over the network"
        )
    settings_module.set_value(conn, key, value)
    return {"key": key, "value": settings_module.get(conn, key)}


def set_country_enabled(
    conn: sqlite3.Connection, country_iso2: str, enabled: bool
) -> dict[str, Any]:
    """Enable or disable a country. Configuration, never a code change."""
    code = country_iso2.upper()
    updated = conn.execute(
        "UPDATE country SET enabled = ? WHERE iso2 = ?", (int(enabled), code)
    ).rowcount
    if not updated:
        raise KeyError(f"country {code} is not configured")
    return {"country": code, "enabled": enabled}


def _hours_ago(hours: float) -> str:
    moment = datetime.now(UTC) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_allowance(conn: sqlite3.Connection) -> dict[str, Any]:
    """How many sends remain today, and when the next slot frees.

    Counted over a rolling 24 hours rather than a calendar day, so a queue
    cannot release twice its limit across midnight.
    """
    per_country, total_limit, interval = settings_module.cap_settings(conn)
    # Stored timestamps are ISO 'YYYY-MM-DDTHH:MM:SSZ'. SQLite's datetime('now')
    # renders 'YYYY-MM-DD HH:MM:SS', and 'T' sorts after ' ', so comparing the two
    # counted every earlier send on the same date as inside the window.
    window_start = _hours_ago(24)

    used_total = conn.execute(
        "SELECT COUNT(*) FROM send_log WHERE sent_at > ?", (window_start,)
    ).fetchone()[0]
    by_country = conn.execute(
        "SELECT destination_country, COUNT(*) n FROM send_log "
        "WHERE sent_at > ? GROUP BY destination_country", (window_start,)
    ).fetchall()
    last = conn.execute("SELECT MAX(sent_at) FROM send_log").fetchone()[0]

    return {
        "window": "rolling 24 hours",
        "total": {"used": used_total, "limit": total_limit,
                  "remaining": max(0, total_limit - used_total)},
        "per_destination": [
            {
                "country": r["destination_country"],
                "used": r["n"],
                "limit": per_country,
                "remaining": max(0, per_country - r["n"]),
            }
            for r in by_country
        ],
        "per_destination_limit": per_country,
        "minimum_interval_seconds": interval,
        "last_send_at": last,
    }


# ------------------------------------------------------------- capabilities


def capability_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """What works right now, and why anything unavailable is unavailable.

    An absent capability is reported as a state with a reason, never as an
    error and never as an empty result.
    """
    credential = conn.execute(
        "SELECT account_label, revoked_at, needs_renewal FROM credential "
        "WHERE provider = 'google' ORDER BY connected_at DESC LIMIT 1"
    ).fetchone()
    mailbox_ok = bool(credential) and not credential["revoked_at"] \
        and not credential["needs_renewal"]

    ai_provider = settings_module.get(conn, "ai_provider")
    checklist = profile_functions.minimum_viable_profile(conn)

    def state(available: bool, reason: str) -> dict[str, Any]:
        return {"available": available, "reason": None if available else reason}

    return {
        "discovery": state(
            checklist.satisfied,
            "the Minimum Viable Profile is not yet satisfied: "
            + ", ".join(i.label for i in checklist.outstanding),
        ),
        "scoring": state(True, ""),
        # A projection needs figures. With none on file the engine runs and
        # reports UNSCORED for every line, which is correct and useless — so the
        # capability says what is missing rather than claiming to be available.
        "financial_projection": state(*_projection_readiness(conn)),
        "document_assembly": state(True, ""),
        "tailored_prose": state(
            True,
            "",
        ) if ai_provider else {
            "available": True,
            "reason": None,
            "note": "supplied by the connected MCP host when one is attached; "
                    "unattended runs fall back to template assembly",
        },
        "reply_tracking": state(
            mailbox_ok,
            "reconnect your mailbox" if credential else "no mailbox connected yet",
        ),
        "sending": state(
            mailbox_ok and settings_module.get(conn, "channel_email_autosend"),
            "auto-send is switched off" if mailbox_ok else "no mailbox connected yet",
        ),
    }


def _projection_readiness(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Whether a savings projection can produce a number yet, and what is missing.

    Reported from what is actually on file: an exchange rate to convert pay into
    the comparison currency, and living-cost figures for somewhere. Claiming the
    capability with neither would make every shortlist read ``UNSCORED`` with no
    explanation of why.
    """
    rates = conn.execute("SELECT COUNT(*) AS n FROM fx_rate").fetchone()["n"]
    costs = conn.execute(
        "SELECT COUNT(*) AS n FROM reference_figure "
        "WHERE superseded_by IS NULL AND kind IN "
        "('rent', 'utilities', 'food', 'transport', 'health_insurance')"
    ).fetchone()["n"]
    taxes = conn.execute(
        "SELECT COUNT(*) AS n FROM reference_figure "
        "WHERE superseded_by IS NULL AND kind = 'tax_rate'"
    ).fetchone()["n"]

    missing = [
        label
        for label, present in (
            ("exchange rates", rates),
            ("cost-of-living figures", costs),
            ("effective tax rates", taxes),
        )
        if not present
    ]
    if missing:
        return False, (
            f"no {', no '.join(missing)} on file yet, so net savings would be reported "
            f"UNSCORED for every opportunity. Nothing is estimated in their place."
        )
    return True, ""


def run_health(conn: sqlite3.Connection) -> dict[str, Any]:
    """When things last ran, and whether that is recent enough to trust.

    A scheduled run that never happens produces no error of its own, so its age
    is surfaced rather than waited for.
    """
    alert_after = int(settings_module.get(conn, "run_staleness_alert_hours"))
    runs = conn.execute(
        "SELECT kind, MAX(started_at) last_ok FROM run WHERE status = 'ok' GROUP BY kind"
    ).fetchall()

    stale = conn.execute(
        "SELECT kind FROM run WHERE status = 'ok' "
        "GROUP BY kind HAVING MAX(started_at) < ?",
        (_hours_ago(alert_after),),
    ).fetchall()

    backup = conn.execute(
        "SELECT taken_at, status, restored_at FROM backup_record "
        "ORDER BY taken_at DESC LIMIT 1"
    ).fetchone()

    from career_scout.gmail.google import mailbox_status

    return {
        "mailbox": mailbox_status(conn),
        "now": utcnow(),
        "version": __version__,
        "config_version": config_module.CURRENT_VERSION,
        "config_fingerprint": config_module.fingerprint(),
        "last_successful_runs": {r["kind"]: r["last_ok"] for r in runs},
        "stale_kinds": [r["kind"] for r in stale],
        "alert_after_hours": alert_after,
        "never_run": not runs,
        "backup": None if backup is None else {
            "taken_at": backup["taken_at"],
            "status": backup["status"],
            "restored_at": backup["restored_at"],
            "note": None if backup["restored_at"]
            else "never restore-tested; a backup that has never been restored is not one",
        },
    }


def list_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Document metadata. A restricted document's contents are never returned."""
    from career_scout.documents import store as document_store

    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "filename": r["filename"],
            "sha256": r["sha256"],
            "bytes": r["bytes"],
            "restricted": bool(r["restricted"]),
            "extracted": bool(r["extracted_ok"]),
            "note": r["extraction_note"],
            "pages": r["page_count"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in document_store.listing(conn)
    ]


def open_restricted_document(
    conn: sqlite3.Connection, document_id: str, *, from_loopback: bool
) -> bytes:
    """The user opening their own restricted file. On this machine only; audited.

    Not an MCP tool: a model never needs a passport's bytes.
    """
    from career_scout.documents import vault

    if not from_loopback:
        raise PermissionError("restricted documents open only on this computer")
    return vault.open_restricted(conn, document_id, purpose="owner opened their own file")


# ------------------------------------------------------------------ packages
#
# Generating, approving and sending are separate calls on purpose. Generation
# runs unattended; approval is a human act from this machine only (web app,
# never MCP); sending needs an approval that already exists and the content hash
# the caller believes it is sending.


def generate_package(conn: sqlite3.Connection, opportunity_id: str) -> dict[str, Any]:
    from career_scout.documents import package

    return package.generate_package(conn, opportunity_id)


def get_package(conn: sqlite3.Connection, package_id: str) -> dict[str, Any]:
    from career_scout.documents import package

    return package.get_package(conn, package_id)


def list_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Awaiting approval, then approved-and-unsent in release order (I-16).

    One row per package, and nothing here acts on a row: the queue is read,
    and each package is opened, checked and approved on its own.
    """
    rows = conn.execute(
        "SELECT p.id, p.kind, p.state, p.route, p.generated_at, p.qc_verdict, p.warnings, "
        "o.title, o.employer, o.deadline, o.country_iso2, ap.approved_at, ap.approved_by, "
        "ap.consumed_at "
        "FROM application_package p JOIN opportunity o ON o.id = p.opportunity_id "
        "LEFT JOIN approval ap ON ap.package_id = p.id "
        "WHERE p.superseded_by IS NULL AND "
        "(p.state = 'awaiting_approval' OR (p.state = 'approved' AND ap.consumed_at IS NULL)) "
        "ORDER BY p.state = 'approved' DESC, o.deadline IS NULL, o.deadline ASC, "
        "ap.approved_at ASC, p.generated_at ASC"
    ).fetchall()
    return [
        {
            "package_id": r["id"], "kind": r["kind"], "state": r["state"], "route": r["route"],
            "title": r["title"], "employer": r["employer"], "deadline": r["deadline"],
            "country": r["country_iso2"], "generated_at": r["generated_at"],
            "approved_at": r["approved_at"], "approved_by": r["approved_by"],
            "warnings": json.loads(r["warnings"] or "[]"),
        }
        for r in rows
    ]


def approve_package(
    conn: sqlite3.Connection, package_id: str, content_hash: str, *, from_loopback: bool
) -> dict[str, Any]:
    """Record a human approval. Called by the web app only — there is no MCP tool."""
    from career_scout import approval

    return approval.approve(conn, package_id, content_hash=content_hash,
                            from_loopback=from_loopback)


def record_manual_submission(
    conn: sqlite3.Connection, package_id: str, note: str | None = None
) -> dict[str, Any]:
    from career_scout import approval

    return approval.record_manual_submission(conn, package_id, note=note)


def send_package(
    conn: sqlite3.Connection,
    package_id: str,
    content_hash: str,
    *,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Send one package the user already approved. The only way anything leaves.

    Reaches the provider only through :func:`career_scout.send.send_approved`, which
    re-checks the nine conditions. ``content_hash`` is the caller's statement of
    what it believes it is sending; a mismatch is refused (I-02, I-25).
    """
    from career_scout import send
    from career_scout.documents import package as package_module
    from career_scout.gmail import send as gmail_send
    from career_scout.gmail.google import GMAIL, GoogleClient, mailbox_status

    approval_row = conn.execute(
        "SELECT id FROM approval WHERE package_id = ? AND consumed_at IS NULL", (package_id,)
    ).fetchone()
    if approval_row is None:
        raise send.SendRefused(
            1, "this package has no unsent approval — approve it in the web app on this computer"
        )
    status = mailbox_status(conn)
    if not status["connected"]:
        raise send.SendRefused(5, status["message"])

    # Refusals the user fixes in Settings come first, before anything talks to
    # Google. The gate re-checks all of this; this only orders the answers.
    kind = conn.execute("SELECT kind FROM application_package WHERE id = ?",
                        (package_id,)).fetchone()["kind"]
    if not settings_module.get(conn, "channel_email_autosend"):
        raise send.SendRefused(6, "the email channel is switched off — turn it on in Settings "
                                  "to let approved messages leave your Gmail")
    if kind == "outreach" and not settings_module.get(conn, "channel_outreach"):
        raise send.SendRefused(6, "outreach is switched off in Settings")

    client = (client_factory or GoogleClient)(conn, status["credential_id"])
    # A disconnection found now refuses before anything is reserved; found after
    # reservation it would leave a 'sending' row for a message that never left.
    client.ensure_token()

    pkg = package_module.get_package(conn, package_id)
    in_reply_to = None
    if pkg["kind"] == "reply":
        reply = conn.execute("SELECT gmail_message_id, gmail_thread_id, subject FROM reply "
                             "WHERE id = ?", (pkg["reply_id"],)).fetchone()
        original = client.get(f"{GMAIL}/messages/{reply['gmail_message_id']}",
                              format="metadata", metadataHeaders="Message-ID")
        header = next((h["value"] for h in original.get("payload", {}).get("headers", [])
                       if h.get("name", "").lower() == "message-id"), "")
        subject = reply["subject"] or ""
        in_reply_to = {
            "thread_id": reply["gmail_thread_id"], "message_id_header": header,
            "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
        }

    result = send.send_approved(
        conn, approval_row["id"], expected_content_hash=content_hash,
        transport=gmail_send.transport(conn, client, status["account"], in_reply_to),
        mailbox_credential_id=status["credential_id"],
    )
    errors = gmail_send.after_send(conn, client, result.application_id)
    conn.execute("UPDATE application_package SET state = 'submitted' WHERE id = ?", (package_id,))
    return {"sent": True, "application_id": result.application_id,
            "provider_message_id": result.provider_message_id,
            "destination": result.destination, "followup_errors": errors}


# -------------------------------------------------------------- applications


def list_applications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.id, a.status, a.channel, a.sent_at, a.destination, a.gmail_thread_id, "
        "a.gmail_label, a.drive_record_url, a.followup_errors, o.title, o.employer, "
        "(SELECT COUNT(*) FROM reply r WHERE r.application_id = a.id) replies "
        "FROM application a JOIN opportunity o ON o.id = a.opportunity_id "
        "WHERE a.kind = 'application' ORDER BY COALESCE(a.sent_at, a.created_at) DESC"
    ).fetchall()
    return [
        {**dict(r), "followup_errors": json.loads(r["followup_errors"] or "[]"),
         "gmail_link": _gmail_link(r["gmail_thread_id"])}
        for r in rows
    ]


def _gmail_link(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None


def get_application(conn: sqlite3.Connection, application_id: str) -> dict[str, Any]:
    from career_scout import tracking

    row = conn.execute(
        "SELECT a.*, o.title, o.employer FROM application a "
        "JOIN opportunity o ON o.id = a.opportunity_id WHERE a.id = ?", (application_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no application {application_id}")
    return {
        **dict(row),
        "followup_errors": json.loads(row["followup_errors"] or "[]"),
        "gmail_link": _gmail_link(row["gmail_thread_id"]),
        "history": tracking.history(conn, application_id),
        "replies": tracking.replies(conn, application_id=application_id),
    }


def list_replies(conn: sqlite3.Connection, *, unlinked_only: bool = False) -> list[dict[str, Any]]:
    from career_scout import tracking

    return tracking.replies(conn, unlinked_only=unlinked_only)


def correct_reply(conn: sqlite3.Connection, reply_id: str, classification: str) -> dict[str, Any]:
    from career_scout import tracking

    return tracking.correct_reply(conn, reply_id, classification)


def link_reply(conn: sqlite3.Connection, reply_id: str, application_id: str) -> dict[str, Any]:
    from career_scout import tracking

    return tracking.link_reply(conn, reply_id, application_id)


def set_application_status(
    conn: sqlite3.Connection, application_id: str, status: str, note: str | None = None
) -> dict[str, Any]:
    """A person moving an application by hand — withdrawn, or an interview by phone."""
    from career_scout import tracking

    changed = tracking.transition(conn, application_id, status, actor="human", note=note)
    return {"application_id": application_id, "status": status, "changed": changed}


def draft_reply(conn: sqlite3.Connection, reply_id: str) -> dict[str, Any]:
    from career_scout.documents import reply

    return reply.draft_reply_package(conn, reply_id)


def poll_mailbox(conn: sqlite3.Connection, *, client_factory: Any = None) -> dict[str, Any]:
    """One Gmail polling pass, recorded as a ``run`` row whatever happens."""
    import uuid

    from career_scout.gmail import poll
    from career_scout.gmail.google import GoogleClient, mailbox_status

    status = mailbox_status(conn)
    run_id, started = str(uuid.uuid4()), utcnow()
    if not status["connected"]:
        result: dict[str, Any] = {"state": status["state"], "message": status["message"],
                                  "counts": {}}
    else:
        client = (client_factory or GoogleClient)(conn, status["credential_id"])
        result = poll.poll(conn, client, own_address=status["account"])
    run_status = "ok" if result["state"] == "ok" else "failed"
    conn.execute(
        "INSERT INTO run (id, kind, started_at, finished_at, status, counts, obstacles, error) "
        "VALUES (?, 'gmail', ?, ?, ?, ?, ?, ?)",
        (run_id, started, utcnow(), run_status, json.dumps(result.get("counts", {})),
         json.dumps([] if run_status == "ok" else [result.get("message")]),
         None if run_status == "ok" else result.get("message")),
    )
    return {"run_id": run_id, **result}


def mailbox(conn: sqlite3.Connection) -> dict[str, Any]:
    from career_scout.gmail.google import mailbox_status

    return mailbox_status(conn)


# ------------------------------------------------------------------ outreach


def list_contacts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from career_scout import outreach

    return outreach.list_contacts(conn)


def discover_contacts(conn: sqlite3.Connection, opportunity_id: str) -> list[dict[str, Any]]:
    """Addresses the posting itself publishes. Candidates only — nothing is stored."""
    from career_scout import outreach

    return outreach.discover_from_posting(conn, opportunity_id)


def add_contact(
    conn: sqlite3.Connection, email: str, published_source_url: str, name: str = "",
    role: str | None = None,
) -> dict[str, Any]:
    """Store a contact only if the address appears on the page it was published on."""
    from career_scout import outreach

    return outreach.add_contact(conn, email=email, published_source_url=published_source_url,
                                name=name, role=role)


def draft_outreach(
    conn: sqlite3.Connection, contact_id: str, opportunity_id: str
) -> dict[str, Any]:
    from career_scout import outreach

    return outreach.draft(conn, contact_id, opportunity_id)


def suppress_contact(conn: sqlite3.Connection, contact_id: str, *, erase: bool = False
                     ) -> dict[str, Any]:
    """Do-not-contact, or erasure. Global and permanent either way (I-28)."""
    from career_scout import outreach

    if erase:
        outreach.erase(conn, contact_id)
    else:
        outreach.suppress(conn, contact_id)
    return {"contact_id": contact_id, "erased": erase, "suppressed": True}


# ----------------------------------------------------------------- proposals


def document_text(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    """A non-restricted document's text, for the MCP host's model to read."""
    from career_scout.documents.extract import proposals

    return proposals.document_text(conn, document_id)


def propose_profile_field(conn: sqlite3.Connection, document_id: str, field_path: str,
                          value: str, quote: str) -> dict[str, Any]:
    """Store a model's proposal only if its quote and value are really in the document."""
    from career_scout.documents.extract import proposals

    return proposals.propose(conn, document_id, field_path, value, quote)


# ----------------------------------------------------------------- interview


def star_bank(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from career_scout import interview

    return interview.star_bank(conn)


def save_star_answer(conn: sqlite3.Connection, record_id: str, situation: str = "",
                     task: str = "", result: str = "") -> dict[str, Any]:
    from career_scout import interview

    return interview.save_star_answer(conn, record_id, situation=situation, task=task,
                                      result=result)


def interview_questions(conn: sqlite3.Connection, opportunity_id: str) -> list[dict[str, Any]]:
    from career_scout import interview

    return interview.questions(conn, opportunity_id)


def score_interview_answer(conn: sqlite3.Connection, opportunity_id: str, answer: str
                           ) -> dict[str, Any]:
    """Score the user's own typed answer against the posting's skills."""
    from career_scout import interview

    row = conn.execute("SELECT inputs FROM assessment WHERE opportunity_id = ?",
                       (opportunity_id,)).fetchone()
    gap = json.loads(row["inputs"]).get("skill_gap", {}) if row else {}
    return interview.score(answer, list(gap.get("matched", [])) + list(gap.get("missing", [])))


# ------------------------------------------------------------- screens


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def progress(conn: sqlite3.Connection) -> dict[str, Any]:
    """The home screen: profile progress, the checklist, and counts by stage (T069)."""
    last = conn.execute(
        "SELECT kind, status, started_at, finished_at FROM run ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return {
        "profile": profile_status(conn),
        "stages": {
            "opportunities": _count(conn, "SELECT count(*) FROM opportunity "
                                          "WHERE is_canonical = 1"),
            "rejected": _count(conn, "SELECT count(DISTINCT opportunity_id) FROM rejection "
                                     "WHERE lifted_at IS NULL"),
            "scored": _count(conn, "SELECT count(*) FROM assessment"),
            "sufficient": _count(conn, "SELECT count(*) FROM sufficiency_verdict "
                                       "WHERE verdict = 'sufficient'"),
            "awaiting_approval": _count(conn, "SELECT count(*) FROM application_package "
                                              "WHERE state = 'awaiting_approval' "
                                              "AND superseded_by IS NULL"),
            "applications": _count(conn, "SELECT count(*) FROM application "
                                         "WHERE kind = 'application'"),
            "unlinked_replies": _count(conn, "SELECT count(*) FROM reply "
                                             "WHERE application_id IS NULL"),
        },
        "last_run": dict(last) if last else None,
        "mailbox": mailbox(conn),
        "now": utcnow(),
    }


def list_countries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every configured country, whether it is searched, and how its floor resolves."""
    from dataclasses import asdict

    return [
        {**dict(r), "enabled": bool(r["enabled"]),
         "floor": asdict(settings_module.resolve_savings_floor(conn, r["iso2"]))}
        for r in conn.execute(
            "SELECT iso2, name, enabled, default_savings_floor, floor_basis FROM country "
            "ORDER BY enabled DESC, name"
        )
    ]


def _dir_bytes(path: Any) -> int:
    from pathlib import Path

    root = Path(path)
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) if root.exists() else 0


def health(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything that can quietly stop working, and how old each thing is (T073)."""
    from career_scout import backup, send
    from career_scout.store.paths import get_paths

    paths = get_paths()
    runs = [dict(r) for r in conn.execute(
        "SELECT kind, status, MAX(started_at) started_at, finished_at, error FROM run "
        "GROUP BY kind ORDER BY kind"
    )]
    return {
        **run_health(conn),
        "latest_runs": runs,
        "backups": backup.status(conn),
        "unreconciled_sends": [dict(r) for r in send.unreconciled(conn)],
        "disk": {
            "database_bytes": paths.db.stat().st_size if paths.db.exists() else 0,
            "documents_bytes": _dir_bytes(paths.documents),
            "snapshots_bytes": _dir_bytes(paths.snapshots),
            "backups_bytes": _dir_bytes(paths.backups),
            "data_directory": str(paths.root),
        },
        "onboarding": onboarding(conn),
    }


def onboarding(conn: sqlite3.Connection) -> dict[str, Any]:
    """The first-run steps, each with what breaks if it is skipped (T072a).

    Every step stays skippable. Nothing here blocks; it says what will not work.
    """
    from career_scout.store.paths import get_paths

    checklist = profile_functions.minimum_viable_profile(conn)
    status = mailbox(conn)
    steps = [
        {"key": "data_directory", "label": "Where your data lives", "done": True,
         "detail": str(get_paths().root),
         "if_skipped": "Nothing — this is created for you. Change it with CAREER_SCOUT_HOME."},
        {"key": "profile", "label": "Import or confirm your profile", "done": checklist.satisfied,
         "detail": ", ".join(i.label for i in checklist.outstanding) or "complete",
         "if_skipped": "No scheduled search runs, and no application can be written: every "
                       "claim in a CV must trace to a confirmed record."},
        {"key": "google", "label": "Connect your own Gmail", "done": status["connected"],
         "detail": status["message"] if not status["connected"] else status["account"],
         "if_skipped": "Nothing can be sent and no replies are tracked. Portal applications "
                       "still work: you submit them yourself from a prefilled worksheet."},
        {"key": "channels", "label": "Choose which channels may send",
         "done": bool(settings_module.get(conn, "channel_email_autosend")),
         "detail": "email " + ("on" if settings_module.get(conn, "channel_email_autosend")
                               else "off"),
         "if_skipped": "Every channel stays off, so even an approved application waits. "
                       "Approval and sending stay two separate decisions."},
        {"key": "backup_key", "label": "Copy your backup key off this computer",
         "done": _count(conn, "SELECT count(*) FROM backup_record WHERE status = 'ok'") > 0,
         "detail": str(get_paths().backup_key),
         "if_skipped": "Backups are taken, but cannot be restored on another machine."},
    ]
    return {"steps": steps, "outstanding": [s["key"] for s in steps if not s["done"]]}
