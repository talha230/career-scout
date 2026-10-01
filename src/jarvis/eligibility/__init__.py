"""Eligibility — T037.

Four questions, each asked of the user's **own confirmed profile** rather than of
a table of assumptions about them:

* may they work there at all — the posting refuses sponsorship, or demands a
  citizenship or settled status they do not hold
* does it need a security clearance, which requires citizenship of the issuing
  country
* does it demand a qualification above the one they have documented
* does it demand a language they have not confirmed

The verdict **soft-hides; it never deletes**. An ineligible opportunity keeps its
row, its score and its projection, and carries a reason the user can read and
disagree with — because every input here is a profile fact that may be missing,
stale or simply not yet confirmed, and the fix for a wrong hide is to correct the
profile, not to re-crawl a board.

Three rules make that safe:

**Missing profile data never blocks.** No confirmed citizenship means the
clearance and right-to-work dimensions are ``unscored``, not ``ineligible``.
Blocking on absence would hide the whole world from a user who has not finished
their profile, and the reason would read like a judgement about them.

**A demand this could not read is unscored, not absent.** A posting demanding
fluency in a language the parser could not name says so, rather than passing the
language dimension as satisfied.

**Residence counts toward the right to work, and says that it assumed so.** A
user living in the posting's country is not blocked by a refusal to sponsor. If
that assumption is wrong for them, the consequence is a role they see and skip —
the opposite error hides a job they are entitled to hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jarvis import config as config_module
from jarvis.matching.posting import PostingView
from jarvis.matching.profile_view import CandidateView, education_ladder

#: The dimensions, in the order a person would ask them.
DIMENSIONS = ("right_to_work", "security_clearance", "qualification", "language")


@dataclass(frozen=True, slots=True)
class Finding:
    """One dimension's answer, with the evidence on both sides."""

    dimension: str
    state: str  # 'ok' | 'blocked' | 'unscored'
    reason: str
    posting_evidence: str | None = None
    profile_evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "state": self.state,
            "reason": self.reason,
            "posting_evidence": self.posting_evidence,
            "profile_evidence": self.profile_evidence,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """The whole answer: one word, one sentence, and the four dimensions."""

    verdict: str  # 'eligible' | 'ineligible' | 'unscored'
    detail: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def hidden(self) -> bool:
        """Whether the opportunity is soft-hidden. Nothing is ever deleted."""
        return self.verdict == "ineligible"

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.state == "blocked")

    @property
    def unknowns(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.state == "unscored")

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "detail": self.detail,
            "hidden": self.hidden,
            "findings": [f.as_dict() for f in self.findings],
        }


def evaluate(
    posting: PostingView,
    candidate: CandidateView,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Verdict:
    """Judge one posting against one profile. Pure: no database, no clock."""
    requirements = posting.requirements or {}
    rules = config_module.eligibility(version)
    findings = (
        _right_to_work(posting, candidate, requirements, rules["right_to_work"]),
        _clearance(posting, candidate, requirements),
        _qualification(candidate, requirements, rules["qualification"], version),
        _language(candidate, requirements),
    )

    blocked = [f for f in findings if f.state == "blocked"]
    if blocked:
        return Verdict(
            verdict="ineligible",
            detail="; ".join(f.reason for f in blocked),
            findings=findings,
        )

    unknown = [f for f in findings if f.state == "unscored"]
    if unknown:
        return Verdict(
            # Not 'eligible': saying so while citizenship is unknown claims
            # something nobody checked. Unscored hides nothing, which is the
            # difference that matters to the user.
            verdict="unscored",
            detail="; ".join(f.reason for f in unknown),
            findings=findings,
        )

    return Verdict(
        verdict="eligible",
        detail="nothing in this posting excludes you on citizenship, clearance, "
        "qualification or language",
        findings=findings,
    )


# --------------------------------------------------------------- dimensions


def _right_to_work(
    posting: PostingView,
    candidate: CandidateView,
    requirements: dict[str, Any],
    rules: dict[str, Any],
) -> Finding:
    """Whether the user may hold this role at all."""
    country = posting.country_iso2
    refuses = requirements.get("sponsorship") == "refused"
    demands_citizenship = bool(requirements.get("requires_citizenship"))

    if not refuses and not demands_citizenship:
        return Finding(
            "right_to_work",
            "ok",
            "this posting neither refuses sponsorship nor demands a citizenship",
        )

    evidence = requirements.get("citizenship_evidence") or requirements.get(
        "sponsorship_evidence"
    )

    if not candidate.citizenship:
        return Finding(
            "right_to_work",
            "unscored",
            candidate.unknowns.get("citizenship")
            or "confirm your citizenship; without it, whether this posting's work "
            "authorisation demand excludes you is unknown",
            posting_evidence=evidence,
        )

    if country is None:
        return Finding(
            "right_to_work",
            "unscored",
            "this posting demands work authorisation but resolved to no country, so "
            "which authorisation it means cannot be established",
            posting_evidence=evidence,
        )

    if country in candidate.citizenship:
        return Finding(
            "right_to_work",
            "ok",
            f"you hold {country} citizenship, so this posting's work authorisation "
            f"demand is satisfied",
            posting_evidence=evidence,
            profile_evidence=f"citizenship: {', '.join(candidate.citizenship)}",
        )

    if rules.get("residence_counts") and candidate.residence_country == country:
        return Finding(
            "right_to_work",
            "ok",
            f"you live in {country}, which is assumed to carry the right to work "
            f"there. If it does not, this role is one to skip rather than one you "
            f"were never shown",
            posting_evidence=evidence,
            profile_evidence=f"residence: {country}",
        )

    if requirements.get("sponsorship") == "offered":
        return Finding(
            "right_to_work",
            "ok",
            "this posting offers sponsorship",
            posting_evidence=evidence,
        )

    demand = (
        "demands a citizenship or settled status you do not hold"
        if demands_citizenship
        else "states it will not sponsor a work visa"
    )
    return Finding(
        "right_to_work",
        "blocked",
        f"this {country} posting {demand}, and you hold "
        f"{', '.join(candidate.citizenship)} citizenship and live in "
        f"{candidate.residence_country or 'a country you have not confirmed'}",
        posting_evidence=evidence,
        profile_evidence=f"citizenship: {', '.join(candidate.citizenship)}",
    )


def _clearance(
    posting: PostingView, candidate: CandidateView, requirements: dict[str, Any]
) -> Finding:
    """A national security clearance requires citizenship of the issuing country."""
    if not requirements.get("requires_clearance"):
        return Finding("security_clearance", "ok", "no security clearance is required")

    evidence = requirements.get("clearance_evidence")

    if not candidate.citizenship:
        return Finding(
            "security_clearance",
            "unscored",
            "this posting requires a security clearance, which is issued only to "
            "citizens. Confirm your citizenship to judge it",
            posting_evidence=evidence,
        )

    if posting.country_iso2 is None:
        return Finding(
            "security_clearance",
            "unscored",
            "this posting requires a clearance but resolved to no country, so which "
            "government would issue it is unknown",
            posting_evidence=evidence,
        )

    if posting.country_iso2 in candidate.citizenship:
        return Finding(
            "security_clearance",
            "ok",
            f"a clearance is required and you hold {posting.country_iso2} "
            f"citizenship, which is the eligibility it rests on. Holding the "
            f"clearance itself is still a separate question",
            posting_evidence=evidence,
            profile_evidence=f"citizenship: {', '.join(candidate.citizenship)}",
        )

    return Finding(
        "security_clearance",
        "blocked",
        f"this role requires a {posting.country_iso2} security clearance, which is "
        f"issued only to its citizens; you hold "
        f"{', '.join(candidate.citizenship)} citizenship",
        posting_evidence=evidence,
        profile_evidence=f"citizenship: {', '.join(candidate.citizenship)}",
    )


def _qualification(
    candidate: CandidateView,
    requirements: dict[str, Any],
    rules: dict[str, Any],
    version: str,
) -> Finding:
    """A stated degree level above the one the user has documented.

    Only the levels listed as blocking in ``eligibility.json`` exclude anybody.
    By default that is a doctorate alone: a Master's demand is a shortfall the
    education component scores, because employers routinely accept equivalent
    experience against it, and treating it as an exclusion would soft-hide 396 of
    the 4,402 corpus postings from a bachelor holder.
    """
    demanded = requirements.get("degree_level")
    if not requirements.get("degree_required") or not demanded:
        return Finding("qualification", "ok", "no specific qualification level is demanded")

    blocking = set(rules.get("blocking_levels") or ())
    if demanded not in blocking:
        return Finding(
            "qualification",
            "ok",
            f"it asks for a {demanded}, which is scored as a shortfall rather than "
            f"treated as an exclusion; only {', '.join(sorted(blocking)) or 'nothing'} "
            f"excludes outright",
            posting_evidence=requirements.get("degree_evidence"),
            profile_evidence=candidate.education_evidence,
        )

    ladder = education_ladder(version)
    if demanded not in ladder:
        return Finding(
            "qualification",
            "unscored",
            f"this posting demands a {demanded!r}, which is not a level on the "
            f"configured qualification ladder",
            posting_evidence=requirements.get("degree_evidence"),
        )

    if not candidate.education_level:
        return Finding(
            "qualification",
            "unscored",
            candidate.unknowns.get("education_level")
            or "confirm your highest qualification to judge this posting's degree "
            "requirement",
            posting_evidence=requirements.get("degree_evidence"),
        )

    held = ladder.get(candidate.education_level, 0)
    if held >= ladder[demanded]:
        return Finding(
            "qualification",
            "ok",
            f"it asks for a {demanded} and you have documented a "
            f"{candidate.education_level}",
            posting_evidence=requirements.get("degree_evidence"),
            profile_evidence=candidate.education_evidence,
        )

    return Finding(
        "qualification",
        "blocked",
        f"this posting requires a {demanded} and your documented highest "
        f"qualification is a {candidate.education_level}",
        posting_evidence=requirements.get("degree_evidence"),
        profile_evidence=candidate.education_evidence,
    )


def _language(candidate: CandidateView, requirements: dict[str, Any]) -> Finding:
    """A language demand, compared against the languages the user confirmed."""
    demanded = list(requirements.get("languages_required") or [])
    evidence_by_language = requirements.get("language_evidence") or {}

    if not demanded:
        if requirements.get("requires_language_fluency"):
            return Finding(
                "language",
                "unscored",
                "this posting demands fluency in a language, and which language was "
                "not readable from its text. It is not treated as satisfied",
            )
        return Finding("language", "ok", "no language fluency is demanded")

    if not candidate.languages:
        return Finding(
            "language",
            "unscored",
            candidate.unknowns.get("languages")
            or f"this posting demands {', '.join(demanded)}, and your profile "
            f"confirms no languages to compare against",
            posting_evidence=next(iter(evidence_by_language.values()), None),
        )

    missing = [language for language in demanded if language not in candidate.languages]
    if not missing:
        return Finding(
            "language",
            "ok",
            f"it demands {', '.join(demanded)}, which your profile confirms",
            profile_evidence=f"languages: {', '.join(sorted(candidate.languages))}",
        )

    return Finding(
        "language",
        "blocked",
        f"this posting requires {', '.join(missing)} and your profile confirms "
        f"{', '.join(sorted(candidate.languages))}",
        posting_evidence=evidence_by_language.get(missing[0]),
        profile_evidence=f"languages: {', '.join(sorted(candidate.languages))}",
    )
