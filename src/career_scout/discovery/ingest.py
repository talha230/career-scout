"""Turning a fetched posting into a stored opportunity — T030d (store half).

This is pipeline step 3, "normalise, dedupe, parse". It is deliberately split
from fetching: everything here is a pure function of a payload plus the database,
so the whole stage is testable without a socket, and the fetch half can be added
behind ``career_scout/net`` without touching any of these rules.

The order is not arbitrary:

1. **Normalise** — resolve employer, title, place, pay and the dedupe key.
2. **Parse requirements** once, here. No later step reads ``description``.
3. **Classify** role family and work arrangement.
4. **Authenticity** — is this a real, applyable vacancy?
5. **Dedupe** — has this vacancy already arrived, from here or elsewhere?
6. **Store**, then write the rejection or the merge evidence.

**A rejected posting is still stored.** The ``rejection`` table points at an
opportunity, and the rule is that rejections are recorded rather than dropped —
disable a rule and re-run, and those postings come back. Nothing that reaches
this function disappears without a row saying why. A later sighting re-judges
the stored text and lifts a rejection the current rules no longer support.

**Dedupe runs before the insert**, not after, because an identical dedupe key is
refused by a UNIQUE index. The same vacancy arriving from a second board is not a
new row at all: it is another sighting of one, so it lands in
``opportunity_source`` and moves ``last_seen_at``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from career_scout import config as config_module
from career_scout.discovery import (
    arrangement,
    authenticity,
    dedupe,
    normalize,
    requirements,
    role_family,
)
from career_scout.store import rejections
from career_scout.store.db import utcnow

STORED = "stored"
SIGHTING = "additional_sighting"
MERGED = "merged"
REJECTED = "rejected"


@dataclass(slots=True)
class RawPosting:
    """One posting as a source client yields it, before any interpretation.

    Source-agnostic on purpose: every client maps its own payload onto this, so
    the rules below never learn the shape of a particular board.
    """

    source_id: str
    source_url: str
    title: str
    employer: str
    kind: str = "job"
    location_raw: str | None = None
    description: str | None = None
    description_format: str | None = None
    employment_types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    posted_at: str | None = None
    deadline: str | None = None
    apply_route: str = "manual"
    apply_target: str | None = None
    pay_min: Any = None
    pay_max: Any = None
    pay_currency: str | None = None
    pay_period: str | None = None
    raw_payload: dict[str, Any] | None = None
    fetched_at: str | None = None
    #: The stored page this posting was read from, relative to ``CAREER_SCOUT_HOME``.
    #: The posting's own URL is not evidence: postings are taken down, and the
    #: page as it was fetched is what every parsed requirement was read from.
    snapshot_path: str | None = None


@dataclass(slots=True)
class IngestOutcome:
    """What happened to one posting, and why."""

    outcome: str
    opportunity_id: str | None = None
    canonical_id: str | None = None
    rule: str | None = None
    reason: str | None = None

    @property
    def is_rejected(self) -> bool:
        return self.outcome == REJECTED


@dataclass(slots=True)
class IngestReport:
    """Counts for the run row, plus every rejection so a human can scan them."""

    stored: int = 0
    sightings: int = 0
    merged: int = 0
    rejected: int = 0
    rejections: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def seen(self) -> int:
        return self.stored + self.sightings + self.merged + self.rejected

    def record(self, outcome: IngestOutcome, title: str) -> None:
        if outcome.outcome == STORED:
            self.stored += 1
        elif outcome.outcome == SIGHTING:
            self.sightings += 1
        elif outcome.outcome == MERGED:
            self.merged += 1
        elif outcome.outcome == REJECTED:
            self.rejected += 1
            self.rejections.append((outcome.rule or "?", title, outcome.reason or ""))


def _normalised(raw: RawPosting, version: str) -> dict[str, Any]:
    """Everything derived from the payload, before anything is stored."""
    description = normalize.strip_html(raw.description) if raw.description else None
    location = normalize.resolve_location(
        raw.location_raw, extra_signals=" ".join(raw.tags), version=version
    )

    pay = normalize.pay_from_structured(
        raw.pay_min, raw.pay_max, raw.pay_currency, raw.pay_period
    )
    if not pay.disclosed:
        # Only fall back to prose when the source published no usable figure.
        # A structured field is a statement; a parsed one is a reading.
        pay = normalize.pay_from_text(description)

    place = None
    if location.country_code:
        place = ", ".join(filter(None, [location.city, location.country_code]))

    work_arrangement = arrangement.classify(
        raw.title,
        description=description,
        employment_types=raw.employment_types,
        tags=raw.tags,
        location_raw=raw.location_raw,
        resolved_place=place,
        version=version,
    )
    parsed = requirements.parse(
        description or "", title=raw.title, kind=raw.kind, version=version
    )
    family = role_family.classify(raw.title, version=version)

    return {
        "kind": raw.kind,
        "title": raw.title,
        "employer": raw.employer,
        "country_iso2": location.country_code,
        "city": location.city,
        "work_arrangement": work_arrangement.kind,
        "role_family": family.key,
        "description": description,
        "description_format": raw.description_format,
        "requirements": json.dumps(parsed.as_dict()),
        "requirements_confidence": parsed.confidence,
        "raw_payload": json.dumps(raw.raw_payload) if raw.raw_payload else None,
        "snapshot_path": raw.snapshot_path,
        "apply_route": raw.apply_route,
        "apply_target": raw.apply_target,
        "deadline": raw.deadline,
        "pay_disclosed": json.dumps(pay.as_payload()) if pay.disclosed else None,
        "source_id": raw.source_id,
        "source_url": raw.source_url,
        "url_canonical": normalize.canonical_url(raw.source_url),
        "posted_at": raw.posted_at,
        "employer_norm": normalize.normalize_company(raw.employer),
        "title_norm": normalize.normalize_title(raw.title),
        "dedupe_key": normalize.dedupe_key(
            raw.employer, raw.title, location.country_code, location.city
        ),
    }


# The columns ingest writes, fixed and in order. The INSERT is built from this
# tuple once rather than from whatever keys a dict happens to carry: the
# statement is then a constant, and a mistyped key in _normalised raises here
# instead of quietly becoming a column nobody declared.
_COLUMNS: tuple[str, ...] = (
    "id", "kind", "title", "employer", "country_iso2", "city", "work_arrangement",
    "role_family", "description", "description_format", "requirements",
    "requirements_confidence", "raw_payload", "snapshot_path", "apply_route",
    "apply_target", "deadline",
    "pay_disclosed", "source_id", "source_url", "url_canonical", "posted_at",
    "employer_norm", "title_norm", "dedupe_key", "fetched_at", "first_seen_at",
    "last_seen_at",
)

_INSERT_SQL = (
    f"INSERT INTO opportunity ({', '.join(_COLUMNS)}) "  # noqa: S608 - fixed tuple above
    f"VALUES ({', '.join('?' * len(_COLUMNS))})"
)


def _insert(conn: sqlite3.Connection, row: dict[str, Any], seen_at: str) -> str:
    opportunity_id = str(uuid.uuid4())
    stored = dict(row)
    stored.update(
        id=opportunity_id,
        fetched_at=seen_at,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )

    unexpected = set(stored) - set(_COLUMNS)
    if unexpected:
        raise KeyError(f"ingest produced columns the table does not take: {sorted(unexpected)}")

    conn.execute(_INSERT_SQL, tuple(stored.get(column) for column in _COLUMNS))
    return opportunity_id


def _judge(
    conn: sqlite3.Connection,
    opportunity_id: str,
    title: str | None,
    description: str | None,
    *,
    run_id: str | None,
    seen_at: str,
    version: str,
) -> authenticity.Verdict:
    """Run the authenticity check and reconcile the stored rejections with it."""
    verdict = authenticity.check(title, description, version=version)
    stored = None
    if verdict.rejected:
        if not verdict.rule:
            raise ValueError("an authenticity rejection must name its rule")
        stored = rejections.Verdict(verdict.rule, verdict.reason, verdict.evidence)
    rejections.reconcile(
        conn,
        opportunity_id,
        rejections.AUTHENTICITY,
        stored,
        run_id=run_id,
        at=seen_at,
    )
    return verdict


def ingest_one(
    conn: sqlite3.Connection,
    raw: RawPosting,
    *,
    run_id: str | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> IngestOutcome:
    """Normalise, judge and store one posting. The caller owns the transaction."""
    seen_at = raw.fetched_at or utcnow()
    row = _normalised(raw, version)

    # An identical vacancy already stored is a sighting, not a row. Checked before
    # the insert, because opportunity_dedupe is a UNIQUE index.
    match = dedupe.find_duplicate(conn, {**row, "id": None}, version=version)
    if match is not None and match.rule == dedupe.IDENTICAL_KEY:
        dedupe.record_additional_source(
            conn,
            match.canonical_id,
            source_id=raw.source_id,
            source_url=raw.source_url,
            fetched_at=seen_at,
        )
        # A sighting is the re-assessment path: the stored text is judged again
        # under the current rules, so a rule disabled since the first sighting
        # lifts its rejection instead of standing for ever.
        stored = conn.execute(
            "SELECT title, description FROM opportunity WHERE id = ?", (match.canonical_id,)
        ).fetchone()
        _judge(
            conn, match.canonical_id, stored["title"], stored["description"],
            run_id=run_id, seen_at=seen_at, version=version,
        )
        return IngestOutcome(
            SIGHTING, opportunity_id=match.canonical_id, rule=match.rule, reason=match.evidence
        )

    opportunity_id = _insert(conn, row, seen_at)
    conn.execute(
        "INSERT OR IGNORE INTO opportunity_source "
        "(opportunity_id, source_id, source_url, fetched_at) VALUES (?, ?, ?, ?)",
        (opportunity_id, raw.source_id, raw.source_url, seen_at),
    )

    verdict = _judge(
        conn, opportunity_id, raw.title, row["description"],
        run_id=run_id, seen_at=seen_at, version=version,
    )
    if verdict.rejected:
        return IngestOutcome(
            REJECTED, opportunity_id=opportunity_id, rule=verdict.rule, reason=verdict.reason
        )

    if match is not None:
        merge = dedupe.DuplicateMatch(
            canonical_id=match.canonical_id,
            duplicate_id=opportunity_id,
            rule=match.rule,
            similarity=match.similarity,
            evidence=match.evidence,
        )
        dedupe.record_duplicate(conn, merge, run_id=run_id, version=version)
        return IngestOutcome(
            MERGED,
            opportunity_id=opportunity_id,
            canonical_id=match.canonical_id,
            rule=match.rule,
            reason=match.evidence,
        )

    return IngestOutcome(STORED, opportunity_id=opportunity_id)


def ingest_many(
    conn: sqlite3.Connection,
    postings: list[RawPosting],
    *,
    run_id: str | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> IngestReport:
    """Ingest a batch in arrival order.

    Order matters and is preserved: the canonical record is the first one seen,
    so re-ordering the input changes which row the others point at. Sorting the
    batch would quietly rewrite that choice.
    """
    report = IngestReport()
    for raw in postings:
        outcome = ingest_one(conn, raw, run_id=run_id, version=version)
        report.record(outcome, raw.title)
    return report
