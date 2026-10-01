"""The three functions that replaced a single gate — T026, T040.

    completeness(conn)                 -> progress indicator; gates nothing
    minimum_viable_profile(conn)       -> unlocks scheduled discovery
    sufficiency(conn, opportunity)     -> decides one application

A single 95% threshold answered badly in both directions: it locked out people
whose profile was already adequate for the roles they wanted, and it admitted
people at 96% whose profile said nothing about the job in front of them. The
narrower question — *does this person's confirmed record support an honest
application to this specific opportunity?* — is more lenient at the start,
stricter at the moment that matters, and cannot be gamed by filling unrelated
fields.

All three are pure functions of stored data and are reproducible by hand.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from jarvis.profile import records
from jarvis.profile.schema import (
    COMPLETENESS,
    COMPLETENESS_VERSION,
    DEFAULT_DOCUMENT_TYPES,
    MINIMUM_VIABLE_PROFILE,
    MVP_VERSION,
    SUFFICIENCY_RULES,
    SUFFICIENCY_VERSION,
    CompletenessField,
    SufficiencyRule,
    normalise,
)
from jarvis.store import settings as settings_module
from jarvis.store.db import utcnow

# --------------------------------------------------------------- completeness


@dataclass(frozen=True, slots=True)
class MissingField:
    path: str
    label: str
    weight: float


@dataclass(frozen=True, slots=True)
class Completeness:
    """A percentage that can be recomputed by hand from ``present`` and ``missing``."""

    percentage: float
    earned: float
    total: float
    version: int
    present: tuple[str, ...]
    missing: tuple[MissingField, ...]

    def explain(self) -> str:
        lines = [f"{self.earned:g} of {self.total:g} = {self.percentage:.1f}%"]
        lines += [f"  missing {m.path} ({m.label}) worth {m.weight:g}" for m in self.missing]
        return "\n".join(lines)


def completeness(conn: sqlite3.Connection) -> Completeness:
    """Weighted progress. **Nothing branches on this** (I-23's sibling, T027).

    A repeated group scores its weight once at least one entry is present, so
    somebody with four jobs is not ranked above somebody with two.
    """
    have = records.confirmed_paths(conn)

    earned = 0.0
    present: list[str] = []
    missing: list[MissingField] = []

    for definition in COMPLETENESS:
        if _satisfied(definition, have):
            earned += definition.weight
            present.append(definition.path)
        else:
            missing.append(MissingField(definition.path, definition.label, definition.weight))

    total = sum(f.weight for f in COMPLETENESS)
    return Completeness(
        percentage=round(100.0 * earned / total, 1) if total else 0.0,
        earned=earned,
        total=total,
        version=COMPLETENESS_VERSION,
        present=tuple(present),
        missing=tuple(missing),
    )


def _satisfied(definition: CompletenessField, have: set[str]) -> bool:
    return normalise(definition.path) in have


# ------------------------------------------------------- minimum viable profile


@dataclass(frozen=True, slots=True)
class ChecklistResult:
    key: str
    label: str
    rationale: str
    satisfied: bool
    missing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MinimumViableProfile:
    satisfied: bool
    version: int
    items: tuple[ChecklistResult, ...]

    @property
    def outstanding(self) -> tuple[ChecklistResult, ...]:
        return tuple(i for i in self.items if not i.satisfied)


def minimum_viable_profile(conn: sqlite3.Connection) -> MinimumViableProfile:
    """The only thing that gates discovery, and it is a list of items, not a score."""
    have = records.confirmed_paths(conn)
    results: list[ChecklistResult] = []

    for item in MINIMUM_VIABLE_PROFILE:
        best_missing: tuple[str, ...] | None = None
        satisfied = False
        for group in item.any_of:
            absent = tuple(p for p in group if normalise(p) not in have)
            if not absent:
                satisfied = True
                best_missing = ()
                break
            # Report the alternative the user is closest to finishing.
            if best_missing is None or len(absent) < len(best_missing):
                best_missing = absent
        results.append(
            ChecklistResult(
                key=item.key,
                label=item.label,
                rationale=item.rationale,
                satisfied=satisfied,
                missing=best_missing or (),
            )
        )

    return MinimumViableProfile(
        satisfied=all(r.satisfied for r in results),
        version=MVP_VERSION,
        items=tuple(results),
    )


# ---------------------------------------------------------------- sufficiency


@dataclass(frozen=True, slots=True)
class Sufficiency:
    """One opportunity's verdict, with everything needed to check it by hand."""

    verdict: str  # 'sufficient' | 'insufficient' | 'not_evaluated'
    reason: str | None
    rules_applied: tuple[str, ...]
    fields_examined: tuple[str, ...]
    missing_field_paths: tuple[str, ...]
    rule_version: int = SUFFICIENCY_VERSION

    @property
    def may_generate(self) -> bool:
        """Only ``sufficient`` authorises generating a package.

        ``not_evaluated`` is not a soft yes. This is the fail-closed direction
        and the reason I-18 exists.
        """
        return self.verdict == "sufficient"


def sufficiency(
    conn: sqlite3.Connection,
    opportunity: dict[str, Any],
    *,
    confidence_floor: float | None = None,
) -> Sufficiency:
    """Decide whether this profile supports an honest application to this posting.

    A set comparison between the rules an opportunity matches and the user's
    confirmed field paths. No text processing, no AI, no model.

    The confidence gate comes first and is the important part. Requirements are
    parsed from free text at ingest, and parsing sometimes produces nothing —
    in the legacy corpus, 481 of 2,305 assessments recorded that the posting
    named no skill in the vocabulary at all, and one posting was scored on the
    title ``"Oops something happened"``. A posting we could not read yields an
    empty conditional rule set, and an empty rule set would otherwise make
    every profile trivially sufficient. So it returns ``not_evaluated``, which
    does not authorise generation, and says why.
    """
    if confidence_floor is None:
        confidence_floor = float(settings_module.get(conn, "requirements_confidence_floor"))

    requirements = _requirements_of(opportunity)
    confidence = float(opportunity.get("requirements_confidence") or 0.0)

    if confidence < confidence_floor:
        return Sufficiency(
            verdict="not_evaluated",
            reason=(
                f"this posting's requirements could not be read confidently "
                f"({confidence:.2f} < {confidence_floor:.2f}), so whether your profile "
                f"covers them is unknown; it is not treated as covered"
            ),
            rules_applied=(),
            fields_examined=(),
            missing_field_paths=(),
        )

    kind = opportunity.get("kind", "job")
    role_family = opportunity.get("role_family", "unclassified")
    document_types = _document_types(requirements, kind)

    applied: list[SufficiencyRule] = []
    for rule in SUFFICIENCY_RULES:
        if rule.document_type not in document_types:
            continue
        if rule.kind is not None and rule.kind != kind:
            continue
        if rule.role_family is not None and rule.role_family != role_family:
            continue
        if rule.conditional_on and not _predicate_holds(rule.conditional_on, requirements):
            continue
        applied.append(rule)

    if not applied:
        return Sufficiency(
            verdict="not_evaluated",
            reason=(
                f"no sufficiency rule matches a {kind} needing {sorted(document_types)}; "
                f"without a rule there is nothing to check against"
            ),
            rules_applied=(),
            fields_examined=(),
            missing_field_paths=(),
        )

    have = records.confirmed_paths(conn)
    examined: list[str] = []
    missing: list[str] = []
    for rule in applied:
        for path in rule.required_field_paths:
            normalised = normalise(path)
            if normalised not in examined:
                examined.append(normalised)
            if normalised not in have and normalised not in missing:
                missing.append(normalised)

    return Sufficiency(
        verdict="insufficient" if missing else "sufficient",
        reason=None,
        rules_applied=tuple(r.label for r in applied),
        fields_examined=tuple(examined),
        missing_field_paths=tuple(missing),
    )


def _requirements_of(opportunity: dict[str, Any]) -> dict[str, Any]:
    raw = opportunity.get("requirements")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _document_types(requirements: dict[str, Any], kind: str) -> set[str]:
    stated = requirements.get("document_types")
    if isinstance(stated, list) and stated:
        return {str(t) for t in stated}
    return set(DEFAULT_DOCUMENT_TYPES.get(kind, ("cv",)))


def _predicate_holds(predicate: dict[str, object], requirements: dict[str, Any]) -> bool:
    """Every key in the predicate must equal the parsed requirement."""
    return all(requirements.get(key) == expected for key, expected in predicate.items())


# ------------------------------------------------------------- persistence


def store_verdict(
    conn: sqlite3.Connection, opportunity_id: str, result: Sufficiency
) -> None:
    """Record a verdict so the ranked "what unlocks most" list is one query."""
    conn.execute(
        "INSERT INTO sufficiency_verdict (opportunity_id, verdict, reason, rules_applied, "
        "fields_examined, missing_field_paths, rule_version, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(opportunity_id) DO UPDATE SET "
        "  verdict = excluded.verdict, reason = excluded.reason, "
        "  rules_applied = excluded.rules_applied, fields_examined = excluded.fields_examined, "
        "  missing_field_paths = excluded.missing_field_paths, "
        "  rule_version = excluded.rule_version, computed_at = excluded.computed_at",
        (
            opportunity_id,
            result.verdict,
            result.reason,
            json.dumps(list(result.rules_applied)),
            json.dumps(list(result.fields_examined)),
            json.dumps(list(result.missing_field_paths)),
            result.rule_version,
            utcnow(),
        ),
    )


@dataclass(frozen=True, slots=True)
class Unlock:
    """One missing field, and how much filling it in would buy."""

    field_path: str
    label: str
    unlocks: int


def ranked_unlocks(conn: sqlite3.Connection, limit: int = 10) -> list[Unlock]:
    """What to fill in next, ranked by how many opportunities it would unlock.

    "Add your IELTS score — unlocks 14 shortlisted opportunities" is the
    required shape. "Your profile is 78% complete" is not: it tells the user
    nothing about what to do.
    """
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT missing_field_paths FROM sufficiency_verdict WHERE verdict = 'insufficient'"
    ):
        for path in json.loads(row["missing_field_paths"]):
            counts[path] = counts.get(path, 0) + 1

    labels = {normalise(f.path): f.label for f in COMPLETENESS}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        Unlock(field_path=path, label=labels.get(path, path), unlocks=count)
        for path, count in ranked[:limit]
    ]


# ------------------------------------------------------------ seeding


def seed_definitions(conn: sqlite3.Connection) -> None:
    """Write the versioned definition tables from :mod:`jarvis.profile.schema`.

    The tables exist so a verdict can name the exact rule version it used, and
    so an old verdict stays explicable after the rules change.
    """
    conn.execute("DELETE FROM sufficiency_rule WHERE version = ?", (SUFFICIENCY_VERSION,))
    for rule in SUFFICIENCY_RULES:
        conn.execute(
            "INSERT INTO sufficiency_rule (version, document_type, role_family, kind, "
            "required_field_paths, conditional_on, label, rationale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                SUFFICIENCY_VERSION,
                rule.document_type,
                rule.role_family,
                rule.kind,
                json.dumps(list(rule.required_field_paths)),
                json.dumps(rule.conditional_on) if rule.conditional_on else None,
                rule.label,
                rule.rationale,
            ),
        )


def new_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "Completeness",
    "MinimumViableProfile",
    "Sufficiency",
    "Unlock",
    "completeness",
    "minimum_viable_profile",
    "ranked_unlocks",
    "seed_definitions",
    "store_verdict",
    "sufficiency",
]