"""One application package: generate, QC, render once, hash — T044 to T046, T054, T055.

A package is generated only for an opportunity whose sufficiency verdict is
``sufficient``. ``not_evaluated`` is not a soft yes (I-18): a posting nobody
could read does not get an application written for it.

Two hashes, both stored here and both bound by the approval:

``content_sha256``
    The documents' lines and their sources, canonicalised. It is what the
    person reads on the approval screen, and they approve *that* hash.
``rendered_sha256``
    The attached bytes. Rendering happens once, here, and nothing re-renders
    at send (T046) — so the file that leaves is byte-for-byte the one approved.

A newer package for the same opportunity supersedes an older one that was never
approved. The old row stays, marked, so what was shown to the user earlier can
still be read.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from jarvis import config as config_module
from jarvis.documents import generate, qc, render, style_variant
from jarvis.profile import functions
from jarvis.store import settings as settings_module
from jarvis.store.db import utcnow
from jarvis.store.paths import get_paths

EMAIL = "email"
PORTAL = "portal"

_EMAIL_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotSufficient(Exception):
    """The profile does not support an honest application to this posting."""


def route_for(posting: dict[str, Any]) -> tuple[str, str | None]:
    """``('email', address)`` when the posting names an address to apply to.

    Everything else — an ATS form, a portal, a missing target — is the portal
    route, which Jarvis never submits (T054; U4 expects no employer publishes a
    submission API).
    """
    target = (posting.get("apply_target") or "").strip()
    if posting.get("apply_route") == "email" and _EMAIL_ADDRESS.match(target):
        return EMAIL, target
    return PORTAL, None


def destination_for(conn: sqlite3.Connection, package_id: str) -> str | None:
    """Where a package would be sent, read from its kind's own source row.

    The same answer the send gate re-derives at send time, so what the person
    approves and what the gate checks cannot come from two different places.
    """
    row = conn.execute(
        "SELECT p.kind, p.route, p.reply_id, p.contact_id, o.apply_target "
        "FROM application_package p JOIN opportunity o ON o.id = p.opportunity_id "
        "WHERE p.id = ?", (package_id,),
    ).fetchone()
    if row is None or row["route"] != EMAIL:
        return None
    if row["kind"] == "reply":
        found = conn.execute("SELECT from_address FROM reply WHERE id = ?",
                             (row["reply_id"],)).fetchone()
        return found["from_address"] if found else None
    if row["kind"] == "outreach":
        found = conn.execute("SELECT email FROM contact WHERE id = ? AND erased_at IS NULL",
                             (row["contact_id"],)).fetchone()
        return found["email"] if found else None
    return (row["apply_target"] or "").strip() or None


def content_hash(drafts: list[dict[str, Any]], route: str, destination: str | None) -> str:
    canonical = json.dumps(
        {"documents": drafts, "route": route, "destination": destination},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rendered_hash(documents: list[dict[str, str]]) -> str:
    joined = "|".join(f"{d['type']}:{d['sha256']}" for d in documents)
    return hashlib.sha256(joined.encode()).hexdigest()


def duplicates(conn: sqlite3.Connection, posting: dict[str, Any]) -> list[str]:
    """T055 — earlier applications or approvals to the same employer and title.

    Matched on the normalised employer and title rather than the opportunity id,
    because the same vacancy reposted, or found on a second board and not
    merged, is still the same vacancy to the employer reading it.
    """
    warnings: list[str] = []
    for row in conn.execute(
        "SELECT o.title, o.employer, a.status, a.sent_at FROM application a "
        "JOIN opportunity o ON o.id = a.opportunity_id "
        "WHERE o.employer_norm = ? AND o.title_norm = ?",
        (posting.get("employer_norm"), posting.get("title_norm")),
    ):
        warnings.append(
            f"You already applied to {row['title']} at {row['employer']} "
            f"({row['status']}, sent {row['sent_at'] or 'not yet'})."
        )
    for row in conn.execute(
        "SELECT o.title, o.employer, p.id FROM application_package p "
        "JOIN approval ap ON ap.package_id = p.id "
        "JOIN opportunity o ON o.id = p.opportunity_id "
        "WHERE o.employer_norm = ? AND o.title_norm = ? AND ap.consumed_at IS NULL "
        "AND o.id != ?",
        (posting.get("employer_norm"), posting.get("title_norm"), posting["id"]),
    ):
        warnings.append(
            f"An approved, unsent application to {row['title']} at {row['employer']} is "
            f"already queued (package {row['id'][:8]})."
        )
    return warnings


def generate_package(
    conn: sqlite3.Connection,
    opportunity_id: str,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> dict[str, Any]:
    """Generate, check, render and store one package. Returns its summary."""
    opportunity = conn.execute(
        "SELECT * FROM opportunity WHERE id = ?", (opportunity_id,)
    ).fetchone()
    if opportunity is None:
        raise KeyError(f"no opportunity {opportunity_id}")

    verdict = functions.sufficiency(conn, dict(opportunity))
    functions.store_verdict(conn, opportunity_id, verdict)
    if not verdict.may_generate:
        raise NotSufficient(
            verdict.reason
            or f"missing: {', '.join(verdict.missing_field_paths) or 'unknown'}"
        )

    posting = generate.load_posting(conn, opportunity_id)
    route, destination = route_for(posting)
    variant = style_variant.resolve(conn, settings_module.install_id(conn), version=version)
    generated = generate.build(conn, opportunity_id, variant, portal=route == PORTAL,
                               version=version)
    report = qc.check(conn, generated, version=version)

    package_id = str(uuid.uuid4())
    folder = get_paths().generated_documents / package_id
    documents: list[dict[str, str]] = []
    for draft in generated.drafts:
        path = folder / f"{draft.doc_type}.docx"
        digest = render.render_docx(draft, variant, path)
        documents.append({
            "type": draft.doc_type,
            "path": str(path.relative_to(get_paths().root)).replace("\\", "/"),
            "sha256": digest,
        })

    drafts_payload = [d.as_dict() for d in generated.drafts]
    now = utcnow()
    state = "awaiting_approval" if report.passed else "blocked"

    conn.execute(
        "INSERT INTO application_package (id, opportunity_id, documents, claim_trace, "
        "rendered_sha256, qc_verdict, blocked_reason, omissions, state, generated_at, "
        "content_sha256, route, qc_findings, warnings) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package_id, opportunity_id,
            json.dumps({"documents": documents, "drafts": drafts_payload}),
            json.dumps(generated.claim_trace()),
            rendered_hash(documents),
            "pass" if report.passed else "blocked",
            report.blocked_reason,
            json.dumps(generated.omissions),
            state, now,
            content_hash(drafts_payload, route, destination),
            route,
            json.dumps([f.as_dict() for f in report.findings]),
            json.dumps(duplicates(conn, posting)),
        ),
    )
    # Only packages nobody approved are superseded; an approved one is the
    # user's decision and stays exactly as it is.
    conn.execute(
        "UPDATE application_package SET superseded_by = ?, state = 'blocked', "
        "blocked_reason = COALESCE(blocked_reason, 'superseded by a newer package') "
        "WHERE opportunity_id = ? AND id != ? AND superseded_by IS NULL "
        "AND state IN ('generated', 'awaiting_approval', 'blocked')",
        (package_id, opportunity_id, package_id),
    )
    return get_package(conn, package_id)


def store_message_package(
    conn: sqlite3.Connection,
    *,
    kind: str,
    opportunity_id: str,
    draft: generate.Draft,
    posting: dict[str, str],
    omissions: list[dict[str, Any]],
    reply_id: str | None = None,
    contact_id: str | None = None,
) -> dict[str, Any]:
    """An auto-reply or an outreach message: the same QC, hashes and gate (T065, T084).

    The message body is written once, to a text file, and that file's hash is the
    rendered hash — the send path attaches nothing and sends exactly these bytes'
    content, so both hashes still bind what leaves.
    """
    generated = generate.Generated(opportunity_id=opportunity_id, kind=kind, posting=posting,
                                   drafts=[draft], omissions=omissions)
    report = qc.check(conn, generated)
    package_id = str(uuid.uuid4())
    path = get_paths().generated_documents / package_id / f"{kind}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(line.text for line in draft.lines)
    path.write_text(body, encoding="utf-8")
    documents = [{
        "type": kind,
        "path": str(path.relative_to(get_paths().root)).replace("\\", "/"),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }]
    drafts_payload = [draft.as_dict()]
    conn.execute(
        "INSERT INTO application_package (id, opportunity_id, documents, claim_trace, "
        "rendered_sha256, qc_verdict, blocked_reason, omissions, state, generated_at, "
        "content_sha256, route, qc_findings, warnings, kind, reply_id, contact_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'email', ?, '[]', ?, ?, ?)",
        (
            package_id, opportunity_id,
            json.dumps({"documents": documents, "drafts": drafts_payload}),
            json.dumps(generated.claim_trace()), rendered_hash(documents),
            "pass" if report.passed else "blocked", report.blocked_reason,
            json.dumps(omissions), "awaiting_approval" if report.passed else "blocked",
            utcnow(), json.dumps([f.as_dict() for f in report.findings]),
            kind, reply_id, contact_id,
        ),
    )
    # The destination is part of the content hash, and is read the same way the
    # gate will read it — so it is filled in once the row exists.
    conn.execute(
        "UPDATE application_package SET content_sha256 = ? WHERE id = ?",
        (content_hash(drafts_payload, EMAIL, destination_for(conn, package_id)), package_id),
    )
    return get_package(conn, package_id)


def get_package(conn: sqlite3.Connection, package_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT p.*, o.title, o.employer, o.apply_target, o.source_url, o.deadline, "
        "o.country_iso2 FROM application_package p "
        "JOIN opportunity o ON o.id = p.opportunity_id WHERE p.id = ?",
        (package_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no package {package_id}")
    docs = json.loads(row["documents"])
    approval = conn.execute(
        "SELECT id, approved_at, consumed_at, destination_snapshot FROM approval "
        "WHERE package_id = ?", (package_id,),
    ).fetchone()
    route = row["route"]
    destination = destination_for(conn, package_id)
    trace = json.loads(row["claim_trace"])
    cited = sorted({s for sources in trace.values() for s in sources})
    records_index: dict[str, dict[str, Any]] = {}
    if cited:
        marks = ",".join("?" * len(cited))
        for record in conn.execute(
            f"SELECT id, field_path, value, confirmed, superseded_by, document_id "  # noqa: S608
            f"FROM profile_record WHERE id IN ({marks})", tuple(cited),
        ):
            records_index[record["id"]] = {
                "field_path": record["field_path"], "value": record["value"],
                "confirmed": bool(record["confirmed"]),
                "current": record["superseded_by"] is None,
                "source": "document" if record["document_id"] else "typed",
            }
    return {
        "id": row["id"],
        "kind": row["kind"],
        "reply_id": row["reply_id"],
        "contact_id": row["contact_id"],
        "opportunity_id": row["opportunity_id"],
        "title": row["title"],
        "employer": row["employer"],
        "deadline": row["deadline"],
        "country": row["country_iso2"],
        "state": row["state"],
        "route": route,
        # The destination in full, never abbreviated: the person approving must
        # see exactly where it goes.
        "destination": destination,
        "posting_url": row["source_url"],
        "qc_verdict": row["qc_verdict"],
        "blocked_reason": row["blocked_reason"],
        "qc_findings": json.loads(row["qc_findings"] or "[]"),
        "warnings": json.loads(row["warnings"] or "[]"),
        "omissions": json.loads(row["omissions"] or "[]"),
        "content_hash": row["content_sha256"],
        "rendered_hash": row["rendered_sha256"],
        "documents": docs["documents"],
        "drafts": docs["drafts"],
        "claim_trace": trace,
        # What each cited record says, so the approval screen can show the
        # evidence beside the line it backs.
        "records": records_index,
        "superseded_by": row["superseded_by"],
        "generated_at": row["generated_at"],
        "approval": dict(approval) if approval else None,
    }
