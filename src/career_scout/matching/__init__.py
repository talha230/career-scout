"""Scoring one opportunity against one profile — T034, T037, T038.

    screen(conn, opportunity_id)      -> Screened   (filters, then score)
    score(conn, opportunity_id)       -> Assessment
    score_posting(posting, candidate)  -> Assessment

:func:`score_posting` is the whole engine and it touches nothing: two plain
objects in, one result out, no database, no clock, no network. :func:`score`
is the thin wrapper that loads those two objects. Keeping them apart is what
makes I-19 — every score reproducible by hand from its own stored row —
testable rather than aspirational.

The nine components live in :mod:`career_scout.matching.components`, the arithmetic
that combines them in :mod:`career_scout.matching.scoring`, and every number either
of them uses in ``career_scout/config/v1/``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from career_scout import config as config_module
from career_scout import eligibility as eligibility_module
from career_scout.eligibility import filters as filters_module
from career_scout.matching import components as component_module
from career_scout.matching import posting as posting_module
from career_scout.matching import profile_view
from career_scout.matching.posting import PostingView
from career_scout.matching.profile_view import CandidateView
from career_scout.matching.scoring import Component, Score, combine, component_weights, unscored
from career_scout.matching.skills import SkillGap
from career_scout.money import funding, savings
from career_scout.money.savings import Projection
from career_scout.store import settings as settings_module
from career_scout.store.db import utcnow

__all__ = ["Assessment", "Screened", "score", "score_posting", "screen"]


@dataclass(frozen=True, slots=True)
class Assessment:
    """A score, the gap behind it, and the two views it was computed from."""

    opportunity_id: str
    score: Score
    gap: SkillGap
    candidate: dict[str, Any]
    reasons_for: list[str]
    reasons_against: list[str]
    #: The savings projection (T035), when one could be attempted. ``None``
    #: means nobody asked for it — an unreadable posting is not projected —
    #: which is distinct from a projection that ran and could not resolve a
    #: figure. That second case is a Projection whose net is None with its
    #: reasons attached.
    projection: Projection | None = None
    #: The eligibility verdict (T037), when one could be attempted. It
    #: soft-hides and never deletes: an ineligible opportunity keeps its row,
    #: its score and its projection, and carries the reason.
    eligibility: eligibility_module.Verdict | None = None

    @property
    def total(self) -> float:
        return self.score.total

    def as_row(self) -> dict[str, Any]:
        """The ``assessment`` columns this task owns.

        The columns belonging to later tasks — ``eligibility_verdict``,
        ``sufficiency`` — are deliberately absent rather than filled with a
        placeholder. A row that says nothing about eligibility is honest; a row
        that says ``eligible`` because nobody has written that engine yet is not.

        ``projection``, ``passes_floor``, ``floor_applied``, ``floor_source``
        and ``pay_basis`` are filled from T035's projection when there is one,
        and absent when no projection was attempted.
        """
        row: dict[str, Any] = {
            "opportunity_id": self.opportunity_id,
            "config_version": self.score.config_version,
            # NULL, not 0.0. `combine` reports 0.0 alongside tier UNSCORED so
            # the dataclass has a float; storing that 0.0 would rank an
            # unreadable posting as though it had been measured and scored
            # badly, which is a different claim from not having been measured.
            "match_score": None if self.score.tier == "UNSCORED" else self.score.total,
            "confidence": self.score.confidence,
            "formula": self.score.formula,
            "weights": {
                "declared": {
                    name: body["weight"] for name, body in self.score.components.items()
                },
                "effective": self.score.effective_weights,
                "config_fingerprint": self.score.config_fingerprint,
            },
            "inputs": {
                "components": self.score.components,
                "arithmetic": self.score.arithmetic,
                "tier": self.score.tier,
                "skill_gap": self.gap.as_dict(),
                "candidate": self.candidate,
                "reasons_for": self.reasons_for,
                "reasons_against": self.reasons_against,
            },
            "unscored_components": [
                {
                    "component": name,
                    "reason": self.score.components[name]["unscored_reason"],
                    "declared_weight": self.score.components[name]["weight"],
                    "redistributed_to": [
                        other
                        for other, value in self.score.effective_weights.items()
                        if value > 0
                    ],
                }
                for name in self.score.unscored
            ],
        }
        if self.eligibility is not None:
            row["eligibility_verdict"] = self.eligibility.verdict
            row["eligibility_detail"] = self.eligibility.detail
            row["inputs"]["eligibility"] = self.eligibility.as_dict()
        if self.projection is not None:
            row.update(
                {
                    "projection": self.projection.as_dict(),
                    # None, not 0 and not False: a projection that could not
                    # resolve every line has no verdict about the floor, and
                    # "does not pass" is a different claim from "unknown".
                    "passes_floor": self.projection.passes_floor,
                    "floor_applied": self.projection.floor_usd_month,
                    "floor_source": self.projection.floor_source,
                    "pay_basis": self.projection.pay_basis,
                }
            )
        return row


@dataclass(frozen=True, slots=True)
class Screened:
    """What one pass over one opportunity produced.

    Exactly one of :attr:`rejection` and :attr:`assessment` is set. A hard filter
    rejects **before** scoring, because a score computed on a posting that was
    never a real vacancy is a number that will later be compared against real
    ones. The rejection row is already written when this returns, so the reason is
    queryable and the posting itself is untouched.
    """

    opportunity_id: str
    rejection: filters_module.Rejection | None = None
    assessment: Assessment | None = None

    @property
    def rejected(self) -> bool:
        return self.rejection is not None


_ASSESSMENT_COLUMNS = (
    "opportunity_id", "config_version", "match_score", "confidence", "formula", "weights",
    "inputs", "unscored_components", "projection", "passes_floor", "floor_applied",
    "floor_source", "pay_basis", "eligibility_verdict", "eligibility_detail",
)


def store_assessment(
    conn: sqlite3.Connection, assessment: Assessment, *, run_id: str | None = None
) -> None:
    """Write an assessment, replacing this config version's previous one.

    A column :meth:`Assessment.as_row` leaves out is written NULL, never a
    placeholder: no eligibility engine run means no eligibility verdict.
    """
    row = assessment.as_row()
    values = []
    for column in _ASSESSMENT_COLUMNS:
        value = row.get(column)
        if isinstance(value, dict | list):
            value = json.dumps(value, default=str)
        if isinstance(value, bool):
            value = int(value)
        values.append(value)
    placeholders = ", ".join("?" * (len(_ASSESSMENT_COLUMNS) + 2))
    conn.execute(
        f"INSERT OR REPLACE INTO assessment ({', '.join(_ASSESSMENT_COLUMNS)}, run_id, "  # noqa: S608
        f"computed_at) VALUES ({placeholders})",
        (*values, run_id, utcnow()),
    )


def screen(
    conn: sqlite3.Connection,
    opportunity_id: str,
    *,
    as_of: date | None = None,
    candidate: CandidateView | None = None,
    version: str = config_module.CURRENT_VERSION,
    run_id: str | None = None,
) -> Screened:
    """Hard filters first, then score whatever survives.

    The two thresholds the filters need come from settings here, not from inside
    the filter module, so neither is a constant in a code path (I-23).
    """
    view = candidate or profile_view.build(conn, as_of=as_of, version=version)
    posting = posting_module.load(conn, opportunity_id)

    rejection = filters_module.apply(
        posting,
        staleness_days=int(settings_module.get(conn, "staleness_window_days")),
        documented_years=view.documented_years,
        today=as_of or date.today(),
        version=version,
    )
    # Recorded either way: a pass lifts a hard-filter rejection from an earlier
    # run, which is what makes disabling a rule bring its postings back.
    filters_module.record(conn, opportunity_id, rejection, run_id=run_id)
    if rejection is not None:
        return Screened(opportunity_id=opportunity_id, rejection=rejection)

    return Screened(
        opportunity_id=opportunity_id,
        assessment=score(conn, opportunity_id, as_of=as_of, version=version, candidate=view),
    )


def score_posting(
    posting: PostingView,
    candidate: CandidateView,
    *,
    requirements_floor: float,
    version: str = config_module.CURRENT_VERSION,
) -> Assessment:
    """The engine. Pure: the same objects always give the same answer.

    ``requirements_floor`` has no default on purpose. It is the
    ``requirements_confidence_floor`` setting, and a default here would be the
    threshold-as-code-constant that I-23 exists to prevent.
    """
    weights = component_weights(version)

    if posting.requirements_confidence < requirements_floor:
        return _unread(posting, candidate, weights, requirements_floor, version)

    skill_match, gap = component_module.score_skill_match(
        posting, candidate, weights["skill_match"], version=version
    )
    parts: list[Component] = [
        skill_match,
        component_module.score_experience(
            posting, candidate, weights["experience"], version=version
        ),
        component_module.score_career_alignment(
            posting, candidate, weights["career_alignment"], version=version
        ),
        component_module.score_education(
            posting, candidate, weights["education"], version=version
        ),
        component_module.score_salary(
            posting, candidate, weights["salary"], version=version
        ),
        component_module.score_location_remote(
            posting, candidate, weights["location_remote"], version=version
        ),
        component_module.score_visa(posting, candidate, weights["visa"], version=version),
        component_module.score_cv_enhancement(
            posting, candidate, gap, weights["cv_enhancement"], version=version
        ),
        component_module.score_company_quality(
            posting, weights["company_quality"], version=version
        ),
    ]

    result = combine(parts, config_version=version)
    for_, against = build_reasons(posting, result, gap)
    return Assessment(
        opportunity_id=posting.id,
        score=result,
        gap=gap,
        candidate=candidate.as_dict(),
        reasons_for=for_,
        reasons_against=against,
    )


def _unread(
    posting: PostingView,
    candidate: CandidateView,
    weights: dict[str, float],
    floor: float,
    version: str,
) -> Assessment:
    """A posting nobody could read produces no score at all.

    The legacy engine scored a posting titled "Oops something happened" at
    **61.14** and put it in tier B. Nothing was wrong with its arithmetic: four
    components — experience, education, career alignment, company quality —
    score *well* when a posting is silent, because "states no year requirement"
    and "states no degree requirement" both mean "nothing to fall short of".
    Absence of evidence read as evidence of fit, four times over, summed.

    I-18 applies the same rule to the sufficiency verdict, where it stops an
    application being generated. This is that rule one step earlier: a total
    computed from a posting that was never read is not a measurement of
    anything, so it is not reported as one.
    """
    reason = (
        f"the posting could not be read: requirements confidence "
        f"{posting.requirements_confidence:.3f} is below the floor of {floor:.2f}. "
        f"Components that score well on silence — no stated years, no stated "
        f"degree — would otherwise turn an unreadable posting into a good match"
    )
    parts = [unscored(name, weight, reason) for name, weight in weights.items()]
    result = combine(parts, config_version=version)
    return Assessment(
        opportunity_id=posting.id,
        score=result,
        gap=SkillGap(),
        candidate=candidate.as_dict(),
        reasons_for=[],
        reasons_against=[reason],
    )


def score(
    conn: sqlite3.Connection,
    opportunity_id: str,
    *,
    as_of: date | None = None,
    version: str = config_module.CURRENT_VERSION,
    candidate: CandidateView | None = None,
) -> Assessment:
    """Load the posting, the profile and the floor, then score.

    ``candidate`` is accepted so a full pass builds the profile view once
    rather than once per posting; it is the same object either way.
    """
    view = candidate or profile_view.build(conn, as_of=as_of, version=version)
    floor = float(settings_module.get(conn, "requirements_confidence_floor"))
    posting = posting_module.load(conn, opportunity_id)
    assessment = score_posting(posting, view, requirements_floor=floor, version=version)

    # A posting that could not be read is not projected. Its own score is
    # UNSCORED for the same reason, and a savings figure attached to a posting
    # nobody could read would be the one number on the screen that looked solid.
    if assessment.score.tier == "UNSCORED":
        return assessment

    # A scholarship's money question is the same shape and a different sum:
    # T = stipend + allowances + visa-capped work income, against tuition and
    # the same living costs. Both engines return one Projection, so the row, the
    # API and the screen do not branch.
    engine = funding.project if posting.kind == "scholarship" else savings.project
    return replace(
        assessment,
        eligibility=eligibility_module.evaluate(posting, view, version=version),
        projection=engine(
            conn,
            posting,
            view,
            today=(as_of or date.today()).isoformat(),
            version=version,
        ),
    )


# ------------------------------------------------------------------ reasons


def build_reasons(
    posting: PostingView, result: Score, gap: SkillGap
) -> tuple[list[str], list[str]]:
    """Read the reasons off the stored breakdown; do not generate new ones.

    Every sentence here is a restatement of a number already in the row, so a
    reason cannot say anything the arithmetic does not. Nothing is written by a
    model, and nothing is written about data that was never scored.
    """
    worth: list[str] = []
    against: list[str] = []

    def body(name: str) -> dict[str, Any]:
        return result.components.get(name, {})

    def value(name: str) -> float | None:
        return body(name).get("score")

    skill = value("skill_match")
    if skill is not None:
        matched, missing = gap.matched, gap.missing
        if skill >= 70 and matched:
            worth.append(
                f"Skills line up: {len(matched)} of {len(matched) + len(missing)} "
                f"recognised skills matched, including {', '.join(matched[:3])}."
            )
        elif skill < 50:
            against.append(
                f"Skill gap: missing {len(missing)} of the skills this posting names"
                + (f", including {', '.join(missing[:3])}." if missing else ".")
            )
    if gap.critical_missing:
        against.append(
            "Missing from the title itself: " + ", ".join(gap.critical_missing[:3]) + "."
        )

    alignment = value("career_alignment")
    inputs = body("career_alignment").get("inputs", {})
    if alignment is not None and alignment >= 80:
        worth.append(
            f"Title is close to your target role "
            f"'{inputs.get('best_matching_target_role')}' ({alignment:.0f}% match)."
        )
    elif alignment is not None and alignment < 50:
        closest = inputs.get("best_matching_target_role")
        against.append(
            "This title shares no word with any of your target roles."
            if closest is None
            else f"Title is some distance from your target roles — closest was "
            f"'{closest}' at {alignment:.0f}%."
        )

    experience = value("experience")
    exp_inputs = body("experience").get("inputs", {})
    if experience is not None and exp_inputs.get("required_years"):
        if experience >= 90:
            worth.append(
                f"Your {exp_inputs['candidate_years_documented']} documented years meet "
                f"the {exp_inputs['required_years']} the posting asks for."
            )
        elif experience < 60:
            against.append(
                f"The posting asks for {exp_inputs['required_years']} years; "
                f"{exp_inputs['candidate_years_documented']} are documented."
            )

    education = value("education")
    edu_inputs = body("education").get("inputs", {})
    if education is not None and education < 60:
        against.append(
            f"The posting asks for a {edu_inputs.get('required_level')} "
            f"('{edu_inputs.get('required_evidence')}'); your highest confirmed "
            f"qualification is {edu_inputs.get('candidate_level')}."
        )
    if edu_inputs.get("accreditation_bonus_applied"):
        worth.append(
            "The posting names a professional-engineering accreditation, which your "
            "confirmed qualification is relevant to."
        )

    visa_inputs = body("visa").get("inputs", {})
    visa = value("visa")
    if visa is not None:
        if visa_inputs.get("sponsorship_signal") == "refused":
            against.append("The posting states it cannot sponsor a visa.")
        elif visa_inputs.get("sponsorship_signal") == "offered":
            worth.append("The posting offers visa sponsorship or relocation support.")
        elif visa >= 80:
            worth.append(
                f"{visa_inputs.get('country_name')} has a workable route on your "
                f"{visa_inputs.get('passport_used')} passport "
                f"({visa_inputs.get('visa_band')})."
            )
        elif visa <= 30:
            against.append(
                f"The route into {visa_inputs.get('country_name')} is difficult on your "
                f"{visa_inputs.get('passport_used')} passport "
                f"({visa_inputs.get('visa_band')})."
            )

    if posting.pay_is_disclosed:
        pay = posting.pay_disclosed or {}
        currency = pay.get("currency") or ""
        low = f"{pay['min']:,.0f}" if pay.get("min") else "?"
        high = f"{pay['max']:,.0f}" if pay.get("max") else "?"
        period = f" per {pay['period']}" if pay.get("period") else ""
        worth.append(f"Pay is disclosed: {currency} {low} – {high}{period}.")

    if gap.transferable:
        worth.append(
            "Gaps you could argue as transferable: "
            + ", ".join(t["skill"] for t in gap.transferable[:2])
            + "."
        )

    if result.unscored:
        against.append(
            f"{len(result.unscored)} of {len(result.components)} components could not be "
            f"scored ({', '.join(result.unscored)}), so this total rests on less evidence "
            f"than a fully scored one."
        )

    return worth, against
