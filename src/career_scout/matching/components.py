"""The nine component scorers — T034.

Each function takes a :class:`~career_scout.matching.posting.PostingView`, a
:class:`~career_scout.matching.profile_view.CandidateView` and a weight, and returns
one :class:`~career_scout.matching.scoring.Component`. They are pure: same inputs,
same output, no database, no clock, no network, no model.

Three rules decide the shape of every one of them.

**Every number comes from config.** Not one threshold, bonus, cap or ladder
rung is written in this file (I-23). If a figure is needed and the config does
not define it, that is a config bug and it surfaces as such.

**A component that cannot be computed is ``UNSCORED`` with a reason** — never
0, never 50, never a neutral default. Where the reason is about the *user*
rather than the posting, it is taken verbatim from
:attr:`CandidateView.unknowns`, so the message names the record they would have
to confirm to turn it into a number.

**Nothing re-reads the posting.** The requirements were parsed at ingest and
the view carries no description. A component physically cannot reach the text.
"""

from __future__ import annotations

import re
from typing import Any

from career_scout import config as config_module
from career_scout.matching.posting import PostingView
from career_scout.matching.profile_view import CandidateView, education_ladder
from career_scout.matching.scoring import Component, unscored
from career_scout.matching.skills import SkillGap, SkillHit, build_skill_gap, hits_from_requirements

_TITLE_STOPWORDS = frozenset({"a", "an", "the", "and", "or", "of", "for", "to", "in", "at", "with"})


def _component_config(name: str, version: str) -> dict[str, Any]:
    return config_module.weights(version).get("components", {}).get(name, {})


def _formula(name: str, version: str) -> str:
    return _component_config(name, version).get("formula", f"{name}: no formula in config")


# ------------------------------------------------------------- skill match


def score_skill_match(
    posting: PostingView,
    candidate: CandidateView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> tuple[Component, SkillGap]:
    """Weighted overlap between what the posting names and what the user has.

    Returns the gap alongside the component because two other components and
    the reasons both read it, and computing it twice would let them disagree.
    """
    config = _component_config("skill_match", version)
    formula = _formula("skill_match", version)
    skill_weights = config["skill_weights"]
    minimum = config["minimum_recognised_skills"]

    hits = hits_from_requirements(posting.requirements, candidate, version=version)
    gap = build_skill_gap(hits, version=version)

    if not candidate.skills:
        # Scoring a posting's skill list against an empty confirmed set would
        # report 0% — "this person has none of these" — when the truth is that
        # nobody has confirmed anything yet. Those are different claims.
        return unscored("skill_match", weight, candidate.unknowns["skills"],
                        skills_the_posting_names=len(hits)), gap

    if not hits:
        return unscored(
            "skill_match", weight,
            "the posting names no skill in the vocabulary, so there is nothing "
            "to match against",
            skills_found_in_posting=0,
            vocabulary_version=version,
        ), gap

    if len(hits) < minimum:
        # A ratio over one or two recognised skills is not a measurement of
        # fit: a firmware role that happens to say "Python" would score 100%
        # while requiring nothing this person can do.
        return unscored(
            "skill_match", weight,
            f"only {len(hits)} skill(s) from the vocabulary appear in this posting "
            f"(minimum {minimum} to score). Too little overlap to measure fit",
            skills_found_in_posting=len(hits),
            skills_found=[h.canonical for h in hits],
            minimum_required_to_score=minimum,
        ), gap

    required_total = sum(skill_weights[h.criticality] for h in hits)
    matched_total = sum(skill_weights[h.criticality] for h in hits if h.candidate_has)
    score = 100.0 * matched_total / required_total

    return Component(
        name="skill_match",
        score=score,
        weight=weight,
        formula=formula,
        inputs={
            "required_weight_sum": required_total,
            "matched_weight_sum": matched_total,
            "arithmetic": f"100 * {matched_total} / {required_total} = {score:.2f}",
            "matched_skills": [
                h.as_dict(skill_weights[h.criticality]) for h in hits if h.candidate_has
            ],
            "missing_skills": [
                h.as_dict(skill_weights[h.criticality]) for h in hits if not h.candidate_has
            ],
            "candidate_confirmed_skills": sorted(candidate.skills),
        },
    ), gap


# --------------------------------------------------------------- experience


def score_experience(
    posting: PostingView,
    candidate: CandidateView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """Documented years against the years the posting asks for."""
    config = _component_config("experience", version)
    formula = _formula("experience", version)
    required = posting.requirements.get("required_years")

    if candidate.documented_years is None:
        return unscored(
            "experience", weight, candidate.unknowns["documented_years"],
            required_years=required,
        )

    years = candidate.documented_years
    if required is None:
        return Component(
            name="experience", score=100.0, weight=weight, formula=formula,
            inputs={
                "required_years": None,
                "candidate_years_documented": years,
                "note": "the posting states no year requirement, so there is "
                        "nothing to fall short of",
                "years_basis": candidate.years_basis,
            },
        )

    score = 100.0 if years >= required else 100.0 * years / required
    arithmetic = (
        f"{years} >= {required} so 100"
        if years >= required
        else f"100 * {years} / {required} = {score:.2f}"
    )

    penalty_config = config["overqualification_penalty"]
    penalty = 0.0
    if penalty_config["enabled"] and years > required + penalty_config["threshold_years_above"]:
        penalty = float(penalty_config["penalty"])
        score = max(0.0, score - penalty)
        arithmetic += f"; minus {penalty} overqualification penalty = {score:.2f}"

    return Component(
        name="experience", score=score, weight=weight, formula=formula,
        inputs={
            "required_years": required,
            "candidate_years_documented": years,
            "overqualification_penalty_applied": penalty,
            "arithmetic": arithmetic,
            "years_basis": candidate.years_basis,
        },
    )


# --------------------------------------------------------- career alignment


def _title_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in tokens if t not in _TITLE_STOPWORDS and len(t) > 1}


def score_career_alignment(
    posting: PostingView,
    candidate: CandidateView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """Token-set overlap between the posting's title and the user's target roles.

    Character-level similarity was tried first and was badly wrong for this:
    "Firmware Engineer" against "Process Engineer" scored 0.75 purely because
    both end in "Engineer", which pushed unrelated engineering roles to the top
    of the ranking. Comparing token *sets* makes the shared word count 1 of 3,
    which is what a person would say the overlap is.
    """
    config = _component_config("career_alignment", version)
    formula = _formula("career_alignment", version)

    if not candidate.target_roles:
        return unscored(
            "career_alignment", weight, candidate.unknowns["target_roles"],
            job_title=posting.title,
        )

    title_tokens = _title_tokens(posting.title)
    if not title_tokens:
        return unscored(
            "career_alignment", weight,
            "the posting has no readable title, so there is nothing to compare",
            job_title=posting.title,
        )

    bonus_points = float(config["containment_bonus"])
    containment_floor = float(config["containment_floor"])

    best_role, best_ratio, containment = None, 0.0, False
    for role in candidate.target_roles:
        role_tokens = _title_tokens(role)
        if not role_tokens:
            continue
        jaccard = len(title_tokens & role_tokens) / len(title_tokens | role_tokens)
        # Every word of the target role present in the title is a strong signal
        # even when the title carries extras ("Senior Process Engineer, EMEA").
        if role_tokens <= title_tokens:
            containment = True
            jaccard = max(jaccard, containment_floor)
        if jaccard > best_ratio:
            best_ratio, best_role = jaccard, role

    bonus = bonus_points if containment else 0.0
    score = min(100.0, best_ratio * 100.0 + bonus)

    return Component(
        name="career_alignment", score=score, weight=weight, formula=formula,
        inputs={
            "job_title": posting.title,
            "best_matching_target_role": best_role,
            "title_tokens": sorted(title_tokens),
            "token_overlap_jaccard": best_ratio,
            "target_role_fully_contained_in_title": containment,
            "bonus_applied": bonus,
            "arithmetic": f"{best_ratio:.4f} * 100 + {bonus} = {score:.2f}",
        },
    )


# ---------------------------------------------------------------- education


def score_education(
    posting: PostingView,
    candidate: CandidateView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """The qualification the posting asks for against the user's highest."""
    config = _component_config("education", version)
    formula = _formula("education", version)
    ladder = education_ladder(version)
    scoring = config["scoring"]

    if candidate.education_level is None:
        return unscored(
            "education", weight, candidate.unknowns["education_level"],
            required_level=posting.requirements.get("degree_level"),
        )

    required_level = posting.requirements.get("degree_level")
    candidate_rank = ladder[candidate.education_level]

    if required_level is None:
        score = float(scoring["posting_states_no_requirement"])
        gap = None
    else:
        gap = ladder[required_level] - candidate_rank
        if gap <= 0:
            score = float(scoring["candidate_meets_or_exceeds"])
        elif gap == 1:
            score = float(scoring["candidate_one_level_below"])
        else:
            score = float(scoring["candidate_two_or_more_below"])

    bonus_config = config["accreditation_bonus"]
    accreditation = posting.requirements.get("accreditation_evidence")
    bonus = 0.0
    if accreditation:
        bonus = float(bonus_config["points"])
        score = min(float(bonus_config["cap"]), score + bonus)

    return Component(
        name="education", score=score, weight=weight, formula=formula,
        inputs={
            "required_level": required_level,
            "required_evidence": posting.requirements.get("degree_evidence"),
            "candidate_level": candidate.education_level,
            "candidate_evidence": candidate.education_evidence,
            "levels_short": gap,
            "ladder": ladder,
            "accreditation_bonus_applied": bonus,
            "accreditation_evidence": accreditation,
        },
    )


# ------------------------------------------------------------------- salary


def score_salary(
    posting: PostingView,
    candidate: CandidateView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """Published pay against the user's stated minimum. Estimates never score.

    In the legacy corpus this was ``UNSCORED`` on all 2,305 assessments, for
    two reasons that are both still here: 86% of postings disclose no pay, and
    the user had stated no minimum. Reporting a number anyway would have been
    an invention in every one of those rows.
    """
    formula = _formula("salary", version)

    if candidate.salary_minimum is None:
        return unscored("salary", weight, candidate.unknowns["salary_minimum"],
                        posting_pay_disclosed=posting.pay_is_disclosed)

    if not posting.pay_is_disclosed:
        return unscored(
            "salary", weight,
            "the posting discloses no pay. A market estimate is not used here — "
            "only a figure the employer actually published can score",
            posting_pay_disclosed=False,
        )

    pay = posting.pay_disclosed or {}
    posting_currency = (pay.get("currency") or "").upper()
    candidate_currency = (candidate.salary_currency or "").upper()
    if posting_currency and candidate_currency and posting_currency != candidate_currency:
        return unscored(
            "salary", weight,
            "currency_mismatch_no_fx_source: converting at an assumed rate would "
            "invent a number",
            posting_currency=posting_currency,
            candidate_currency=candidate_currency,
        )

    posting_top = pay.get("max") or pay.get("min")
    minimum = candidate.salary_minimum
    score = 100.0 if posting_top >= minimum else 100.0 * posting_top / minimum

    return Component(
        name="salary", score=score, weight=weight, formula=formula,
        inputs={
            "posting_min": pay.get("min"),
            "posting_max": pay.get("max"),
            "posting_currency": pay.get("currency"),
            "posting_period": pay.get("period"),
            "candidate_minimum": minimum,
            "candidate_currency": candidate.salary_currency,
            "pay_source_url": pay.get("source_url"),
            "arithmetic": (
                f"{posting_top} >= {minimum} so 100"
                if posting_top >= minimum
                else f"100 * {posting_top} / {minimum} = {score:.2f}"
            ),
        },
    )


# ----------------------------------------------------------------- country


def _country_entry(iso2: str | None, version: str) -> dict[str, Any] | None:
    if not iso2:
        return None
    return config_module.countries(version)["_by_code"].get(iso2.upper())


def _visa_band(
    country: dict[str, Any], citizenship: tuple[str, ...], version: str
) -> tuple[str | None, float | None, str | None, str | None]:
    """``(band, score, passport used, reason it could not be read)``.

    The pathway table is keyed by citizenship — ``visa_pathway_pk`` is the only
    one the shipped config has. A user of any other nationality gets no band
    and the reason says so, rather than being scored against somebody else's
    passport. A dual national is scored on whichever of their passports scores
    best, and the answer records which one.
    """
    bands = config_module.countries(version)["scoring_model"]["bands"]
    best: tuple[str, float, str] | None = None
    tried: list[str] = []

    for passport in citizenship:
        key = f"visa_pathway_{passport.lower()}"
        tried.append(key)
        entry = country.get(key)
        if not entry:
            continue
        band = entry["band"]
        table = bands.get(key) or {}
        if band not in table:
            continue
        score = float(table[band]["score"])
        if best is None or score > best[1]:
            best = (band, score, passport)

    if best is None:
        return None, None, None, (
            f"no visa pathway data for citizenship {', '.join(citizenship) or 'unknown'} "
            f"in country_preferences (looked for {', '.join(tried) or 'nothing'}). "
            f"The shipped table covers Pakistani citizenship only"
        )
    return best[0], best[1], best[2], None


def score_location_remote(
    posting: PostingView,
    candidate: CandidateView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """How liveable the posting's country is on the user's stated preferences.

    The sub-weights renormalise exactly as the top-level score does: if the
    visa pathway cannot be read for this user's citizenship, its share is
    redistributed across the bands that *can* be read and the inputs say so,
    rather than the whole component going dark or a band being guessed.
    """
    countries = config_module.countries(version)
    model = countries["scoring_model"]
    formula = model["formula"]
    bands = model["bands"]

    country = _country_entry(posting.country_iso2, version)
    if country is None:
        return unscored(
            "location_remote", weight,
            "country_unresolved — the posting's location could not be matched to "
            "a country in country_preferences",
            country_iso2=posting.country_iso2,
        )

    english = float(bands["english_usability"][country["english_usability"]["band"]]["score"])
    muslim = float(bands["muslim_life"][country["muslim_life"]["band"]]["score"])
    visa_band, visa_score, passport, visa_reason = _visa_band(
        country, candidate.citizenship, version
    )

    declared = model["weights"]
    parts: dict[str, tuple[float, float]] = {
        "english": (english, float(declared["w_english"])),
        "muslim": (muslim, float(declared["w_muslim"])),
    }
    if visa_score is not None:
        parts["visa"] = (visa_score, float(declared["w_visa"]))

    total_weight = sum(w for _score, w in parts.values())
    effective = {name: w / total_weight for name, (_score, w) in parts.items()}
    score = sum(value * effective[name] for name, (value, _w) in parts.items())

    # The remote treatment the config describes needs a remote *scope*, which
    # the schema does not record. Without it, a remote role is scored as if it
    # were in the employer's country — the conservative reading, stated here
    # rather than assumed silently.
    remote_note = None
    if posting.is_remote:
        remote_note = (
            "scored as onsite in the employer's country: the posting records "
            "'remote' but no remote scope, and the worldwide-remote treatment in "
            "country_preferences applies only to a role confirmed to be worldwide"
        )

    return Component(
        name="location_remote", score=score, weight=weight, formula=formula,
        inputs={
            "country_code": country["code"],
            "country_name": country["name"],
            "english_band": country["english_usability"]["band"],
            "english_score": english,
            "muslim_band": country["muslim_life"]["band"],
            "muslim_score": muslim,
            "visa_band": visa_band,
            "visa_score": visa_score,
            "visa_passport_used": passport,
            "visa_unavailable_reason": visa_reason,
            "declared_sub_weights": declared,
            "effective_sub_weights": effective,
            "work_arrangement": posting.work_arrangement,
            "remote_treatment": remote_note,
            "arithmetic": " + ".join(
                f"({value:.2f} x {effective[name]:.4f})" for name, (value, _w) in parts.items()
            )
            + f" = {score:.2f}",
        },
    )


def score_visa(
    posting: PostingView,
    candidate: CandidateView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """The immigration route from the user's citizenship into the employer's country."""
    config = _component_config("visa", version)
    formula = _formula("visa", version)

    if not candidate.citizenship:
        return unscored("visa", weight, candidate.unknowns["citizenship"],
                        country_iso2=posting.country_iso2)

    country = _country_entry(posting.country_iso2, version)
    if country is None:
        return unscored(
            "visa", weight,
            "country_unresolved — cannot determine which immigration system applies",
            country_iso2=posting.country_iso2,
        )

    band, base, passport, reason = _visa_band(country, candidate.citizenship, version)
    if base is None:
        return unscored("visa", weight, reason or "no visa pathway data",
                        country_iso2=posting.country_iso2,
                        citizenship=list(candidate.citizenship))

    sponsorship = posting.requirements.get("sponsorship")
    score, adjustment = base, None
    if sponsorship == "refused":
        # A posting that says it cannot sponsor is unavailable to this person
        # whatever the country's general pathway looks like. It is scored zero
        # rather than filtered, because a refusal read from text can be wrong
        # and the evidence has to stay visible.
        score = float(config["explicit_refusal_score"])
        adjustment = "set to 0: the posting explicitly refuses sponsorship"
    elif sponsorship == "offered":
        bonus = float(config["sponsorship_offered_bonus"])
        score = min(float(config["score_cap"]), base + bonus)
        adjustment = f"+{bonus:g}: the posting offers sponsorship or relocation support"

    return Component(
        name="visa", score=score, weight=weight, formula=formula,
        inputs={
            "country_code": country["code"],
            "country_name": country["name"],
            "visa_band": band,
            "band_score": base,
            "passport_used": passport,
            "citizenship": list(candidate.citizenship),
            "sponsorship_signal": sponsorship,
            "adjustment": adjustment,
            "arithmetic": f"{base}{' -> ' + str(score) if adjustment else ''}",
        },
    )


# ----------------------------------------------------------- cv enhancement


def score_cv_enhancement(
    posting: PostingView,
    candidate: CandidateView,
    gap: SkillGap,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """How much genuinely marketable skill the role would add.

    Deliberately a small weight: a job that would teach a lot but that the user
    cannot get is worth little.
    """
    config = _component_config("cv_enhancement", version)
    formula = _formula("cv_enhancement", version)
    target = config["cv_enhancement_target"]

    if not candidate.skills:
        # With nothing confirmed, every skill the posting names looks like a
        # gain, so this would score 100 on any posting — an artefact of empty
        # evidence rather than a property of the role.
        return unscored("cv_enhancement", weight, candidate.unknowns["skills"])

    if not gap.matched and not gap.missing:
        return unscored(
            "cv_enhancement", weight,
            "the posting names no recognisable skill, so no skill gain can be measured",
        )

    vocabulary = config_module.skill_vocabulary(version).get("skills", [])
    in_demand = {s["canonical"] for s in vocabulary if s.get("in_demand")}
    gained = sorted(set(gap.missing) & in_demand)
    score = min(100.0, 100.0 * len(gained) / target)

    return Component(
        name="cv_enhancement", score=score, weight=weight, formula=formula,
        inputs={
            "in_demand_skills_gained": gained,
            "count": len(gained),
            "target": target,
            "arithmetic": f"100 * {len(gained)} / {target} = {score:.2f}",
        },
    )


# ---------------------------------------------------------- company quality


def score_company_quality(
    posting: PostingView,
    weight: float,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Component:
    """Observable signals from the posting itself. Not a reputation score.

    Nothing here is a judgement about what the company is like to work for:
    review sites are outside what this system may read, and no licensed
    employer-rating dataset is configured. Every sub-signal is measured from
    data already ingested, and the honesty note travels with the result so the
    label cannot be quietly dropped by a display layer.
    """
    config = _component_config("company_quality", version)
    formula = _formula("company_quality", version)
    signals = config["sub_signals"]

    chars_target = float(signals["posting_completeness"]["full_marks_chars"])
    roles_target = float(signals["hiring_scale"]["full_marks_open_roles"])

    completeness = min(100.0, 100.0 * posting.description_chars / chars_target)
    transparency = 100.0 if posting.pay_is_disclosed else 0.0
    scale = 100.0 * min(1.0, posting.employer_open_roles / roles_target)
    score = (completeness + transparency + scale) / 3

    return Component(
        name="company_quality", score=score, weight=weight, formula=formula,
        inputs={
            "posting_completeness": completeness,
            "description_chars": posting.description_chars,
            "full_marks_chars": chars_target,
            "pay_transparency": transparency,
            "hiring_scale": scale,
            "employer_open_roles": posting.employer_open_roles,
            "full_marks_open_roles": roles_target,
            "arithmetic": (
                f"({completeness:.2f} + {transparency:.2f} + {scale:.2f}) / 3 = {score:.2f}"
            ),
            "HONESTY_NOTE": config["HONESTY_NOTE"],
        },
    )


__all__ = [
    "SkillHit",
    "score_career_alignment",
    "score_company_quality",
    "score_cv_enhancement",
    "score_education",
    "score_experience",
    "score_location_remote",
    "score_salary",
    "score_skill_match",
    "score_visa",
]
