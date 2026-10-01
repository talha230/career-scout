"""The nine component scorers, and the end-to-end assessment they combine into.

Most of these tests are about the same thing said nine ways: when a component
cannot be computed it says ``UNSCORED`` and why, and it never quietly picks a
number. The one that matters most is at the bottom —
``test_an_unreadable_posting_scores_nothing_at_all``, which is the regression
test for a posting titled "Oops something happened" scoring 61.14.
"""

from __future__ import annotations

from datetime import date

import pytest

from career_scout import config as config_module
from career_scout.discovery import requirements as requirements_module
from career_scout.matching import components, score_posting
from career_scout.matching.posting import PostingView
from career_scout.matching.profile_view import CandidateView
from career_scout.matching.scoring import component_weights
from career_scout.matching.skills import build_skill_gap, hits_from_requirements

pytestmark = pytest.mark.usefixtures("career_scout_home")

FLOOR = 0.35
WEIGHTS = component_weights()


def candidate(**overrides: object) -> CandidateView:
    """A fully known candidate; each test blanks out only what it is about."""
    base = {
        "documented_years": 6.62,
        "years_basis": {"formula": "test", "ranges": []},
        "education_level": "bachelor",
        "education_evidence": "B.Sc.",
        "target_roles": ("Industrial Engineer", "Process Engineer"),
        "salary_minimum": 60000.0,
        "salary_currency": "USD",
        "citizenship": ("PK",),
        "residence_country": "PK",
        "residence_city": "Lahore",
        "tax_residence": "PK",
        "skills": frozenset({"Lean Manufacturing", "Six Sigma", "Python"}),
        "skill_evidence": {},
        "unknowns": {},
        "unconfirmed_counts": {},
        "as_of": "2026-08-08",
    }
    base.update(overrides)
    return CandidateView(**base)  # type: ignore[arg-type]


def unknown(attribute: str, **overrides: object) -> CandidateView:
    """A candidate missing one attribute, with the reason the view would give."""
    blanks: dict[str, object] = {
        "documented_years": None,
        "education_level": None,
        "target_roles": (),
        "salary_minimum": None,
        "skills": frozenset(),
        "citizenship": (),
    }
    return candidate(
        **{attribute: blanks[attribute]},
        unknowns={attribute: f"no confirmed {attribute} in your profile"},
        **overrides,
    )


def posting(description: str = "", *, title: str = "Industrial Engineer", **overrides: object):
    """A posting whose requirements are parsed exactly as ingest would parse them."""
    parsed = requirements_module.parse(description, title=title)
    fields: dict[str, object] = {
        "id": "opp-1",
        "kind": "job",
        "title": title,
        "employer": "Acme",
        "country_iso2": "AE",
        "city": "Dubai",
        "work_arrangement": "onsite",
        "role_family": "industrial_engineering",
        "requirements": parsed.as_dict(),
        "requirements_confidence": max(parsed.confidence, 0.9),
        "pay_disclosed": None,
        "funding_disclosed": None,
        "description_chars": len(description),
        "employer_open_roles": 1,
        "deadline": None,
        "posted_at": None,
        "source_url": "https://example.test/jobs/1",
    }
    fields.update(overrides)
    return PostingView(**fields)  # type: ignore[arg-type]


# -------------------------------------------------------------- skill match


def test_skill_match_is_a_weighted_ratio_over_what_the_posting_names() -> None:
    body = "Requirements: Lean Manufacturing, Six Sigma and Value Stream Mapping."
    component, _gap = components.score_skill_match(posting(body), candidate(), 0.25)
    assert component.scored
    assert component.inputs["arithmetic"].startswith("100 * ")
    assert {m["skill"] for m in component.inputs["matched_skills"]} == {
        "Lean Manufacturing", "Six Sigma"
    }
    assert [m["skill"] for m in component.inputs["missing_skills"]] == ["Value Stream Mapping"]


def test_a_skill_in_the_title_counts_as_critical() -> None:
    body = "Requirements: Lean Manufacturing, Six Sigma, Value Stream Mapping."
    component, _gap = components.score_skill_match(
        posting(body, title="Six Sigma Engineer"), candidate(), 0.25
    )
    criticalities = {
        m["skill"]: m["criticality"]
        for m in component.inputs["matched_skills"] + component.inputs["missing_skills"]
    }
    assert criticalities["Six Sigma"] == "critical"


def test_too_few_recognised_skills_is_unscored_rather_than_a_confident_ratio() -> None:
    """A firmware role that says "Python" once must not score 100% on fit."""
    component, _gap = components.score_skill_match(posting("We use Python."), candidate(), 0.25)
    assert not component.scored
    assert "minimum 3 to score" in component.unscored_reason


def test_a_posting_naming_no_known_skill_is_unscored() -> None:
    component, _gap = components.score_skill_match(posting("A lovely place."), candidate(), 0.25)
    assert not component.scored
    assert "no skill in the vocabulary" in component.unscored_reason


def test_an_empty_confirmed_skill_set_is_unscored_not_a_zero_percent_match() -> None:
    """0% says "you have none of these". Unscored says "nobody has checked"."""
    body = "Requirements: Lean Manufacturing, Six Sigma, Value Stream Mapping."
    component, _gap = components.score_skill_match(posting(body), unknown("skills"), 0.25)
    assert not component.scored
    assert component.score != 0


# --------------------------------------------------------------- experience


def test_meeting_the_stated_years_scores_full_marks() -> None:
    component = components.score_experience(
        posting("Requirements: 5 years of experience."), candidate(), 0.2
    )
    assert component.score == 100.0


def test_falling_short_scores_the_ratio() -> None:
    component = components.score_experience(
        posting("Requirements: at least 10 years of experience."),
        candidate(documented_years=5.0),
        0.2,
    )
    assert component.score == pytest.approx(50.0)


def test_a_posting_stating_no_years_has_nothing_to_fall_short_of() -> None:
    component = components.score_experience(posting("Come and work here."), candidate(), 0.2)
    assert component.score == 100.0
    assert component.inputs["required_years"] is None
    assert "nothing to fall short of" in component.inputs["note"]


def test_being_far_over_the_requirement_costs_the_configured_penalty() -> None:
    config = config_module.weights()["components"]["experience"]["overqualification_penalty"]
    component = components.score_experience(
        posting("Requirements: minimum of 1 years."), candidate(documented_years=30.0), 0.2
    )
    assert component.inputs["overqualification_penalty_applied"] == config["penalty"]


def test_unknown_experience_is_unscored_and_repeats_the_profile_reason() -> None:
    view = unknown("documented_years")
    component = components.score_experience(posting("5 years of experience"), view, 0.2)
    assert not component.scored
    assert component.unscored_reason == view.unknowns["documented_years"]


# --------------------------------------------------------- career alignment


def test_a_target_role_contained_in_the_title_scores_highly() -> None:
    component = components.score_career_alignment(
        posting(title="Senior Process Engineer, EMEA"), candidate(), 0.15
    )
    assert component.inputs["target_role_fully_contained_in_title"] is True
    assert component.score >= 85


def test_token_sets_rather_than_character_similarity() -> None:
    """"Firmware Engineer" vs "Process Engineer" is one shared word, not 0.75."""
    component = components.score_career_alignment(
        posting(title="Firmware Engineer"), candidate(), 0.15
    )
    assert component.inputs["token_overlap_jaccard"] == pytest.approx(1 / 3)
    assert component.score < 40


def test_no_confirmed_target_roles_is_unscored() -> None:
    component = components.score_career_alignment(
        posting(title="Process Engineer"), unknown("target_roles"), 0.15
    )
    assert not component.scored


# ---------------------------------------------------------------- education


def test_meeting_the_required_degree_scores_full_marks() -> None:
    component = components.score_education(
        posting("Requirements: a Bachelor's degree in engineering."), candidate(), 0.1
    )
    assert component.score == 100.0
    assert component.inputs["required_level"] == "bachelor"


def test_being_a_level_short_scores_the_configured_value() -> None:
    scoring = config_module.weights()["components"]["education"]["scoring"]
    component = components.score_education(
        posting("Requirements: a Master's degree is required."), candidate(), 0.1
    )
    assert component.score == scoring["candidate_one_level_below"]


def test_an_accreditation_mention_adds_the_configured_bonus() -> None:
    bonus = config_module.weights()["components"]["education"]["accreditation_bonus"]
    component = components.score_education(
        posting("Requirements: a Master's degree; Chartered Engineer preferred."),
        candidate(),
        0.1,
    )
    assert component.inputs["accreditation_bonus_applied"] == bonus["points"]


def test_unknown_education_is_unscored() -> None:
    component = components.score_education(
        posting("Requirements: a Bachelor's degree."), unknown("education_level"), 0.1
    )
    assert not component.scored


# ------------------------------------------------------------------- salary


def test_disclosed_pay_above_the_minimum_scores_full_marks() -> None:
    pay = {"min": 70000, "max": 90000, "currency": "USD", "period": "year"}
    component = components.score_salary(posting(pay_disclosed=pay), candidate(), 0.1)
    assert component.score == 100.0


def test_undisclosed_pay_is_unscored_and_no_estimate_is_substituted() -> None:
    """86% of the legacy corpus disclosed nothing; an estimate never scores."""
    component = components.score_salary(posting(), candidate(), 0.1)
    assert not component.scored
    assert "only a figure the employer actually published" in component.unscored_reason


def test_a_currency_mismatch_is_unscored_rather_than_converted_at_a_guess() -> None:
    pay = {"min": 70000, "max": 90000, "currency": "EUR", "period": "year"}
    component = components.score_salary(posting(pay_disclosed=pay), candidate(), 0.1)
    assert not component.scored
    assert "currency_mismatch_no_fx_source" in component.unscored_reason


def test_no_stated_minimum_is_unscored() -> None:
    pay = {"min": 70000, "currency": "USD"}
    component = components.score_salary(
        posting(pay_disclosed=pay), unknown("salary_minimum"), 0.1
    )
    assert not component.scored


# ------------------------------------------------------------------ country


def test_location_uses_the_configured_bands_and_sub_weights() -> None:
    component = components.score_location_remote(posting(country_iso2="AE"), candidate(), 0.05)
    assert component.scored
    assert component.inputs["country_code"] == "AE"
    assert sum(component.inputs["effective_sub_weights"].values()) == pytest.approx(1.0)


def test_an_unresolvable_country_is_unscored() -> None:
    component = components.score_location_remote(posting(country_iso2="XX"), candidate(), 0.05)
    assert not component.scored
    assert "country_unresolved" in component.unscored_reason


def test_a_citizenship_with_no_pathway_table_renormalises_the_sub_weights() -> None:
    """The shipped table covers Pakistani citizenship. Others are not guessed at."""
    component = components.score_location_remote(
        posting(country_iso2="AE"), candidate(citizenship=("FR",)), 0.05
    )
    assert component.scored
    assert component.inputs["visa_score"] is None
    assert "no visa pathway data" in component.inputs["visa_unavailable_reason"]
    assert "visa" not in component.inputs["effective_sub_weights"]
    assert sum(component.inputs["effective_sub_weights"].values()) == pytest.approx(1.0)


def test_a_remote_posting_says_how_it_was_treated() -> None:
    component = components.score_location_remote(
        posting(work_arrangement="remote"), candidate(), 0.05
    )
    assert "no remote scope" in component.inputs["remote_treatment"]


def test_visa_uses_the_band_for_the_users_own_passport() -> None:
    component = components.score_visa(posting(country_iso2="AE"), candidate(), 0.05)
    assert component.scored
    assert component.inputs["passport_used"] == "PK"


def test_an_explicit_refusal_to_sponsor_scores_zero_but_is_not_hidden() -> None:
    body = "Requirements: you must be authorized to work in the country. No sponsorship."
    component = components.score_visa(posting(body), candidate(), 0.05)
    assert component.score == 0.0
    assert component.inputs["sponsorship_signal"] == "refused"


def test_an_offer_of_sponsorship_adds_the_configured_bonus() -> None:
    config = config_module.weights()["components"]["visa"]
    body = "We provide visa sponsorship and relocation support."
    plain = components.score_visa(posting(), candidate(), 0.05)
    offered = components.score_visa(posting(body), candidate(), 0.05)
    assert offered.score == min(
        config["score_cap"], plain.score + config["sponsorship_offered_bonus"]
    )


def test_visa_without_a_confirmed_citizenship_is_unscored() -> None:
    component = components.score_visa(posting(), unknown("citizenship"), 0.05)
    assert not component.scored


def test_visa_is_never_scored_from_the_users_address() -> None:
    """Where somebody lives is not which passport they hold."""
    component = components.score_visa(
        posting(country_iso2="AE"), unknown("citizenship", residence_country="PK"), 0.05
    )
    assert not component.scored


# ------------------------------------------------- cv enhancement + quality


def test_cv_enhancement_counts_only_in_demand_skills_the_user_lacks() -> None:
    body = "Requirements: Lean Manufacturing, Six Sigma, Value Stream Mapping, Minitab."
    view = candidate()
    hits = hits_from_requirements(posting(body).requirements, view)
    gap = build_skill_gap(hits)
    component = components.score_cv_enhancement(posting(body), view, gap, 0.05)
    assert component.scored
    assert "Lean Manufacturing" not in component.inputs["in_demand_skills_gained"]


def test_cv_enhancement_is_unscored_when_the_user_has_confirmed_no_skills() -> None:
    """Otherwise every posting looks like it would teach the user everything."""
    body = "Requirements: Lean Manufacturing, Six Sigma, Minitab."
    view = unknown("skills")
    gap = build_skill_gap(hits_from_requirements(posting(body).requirements, view))
    component = components.score_cv_enhancement(posting(body), view, gap, 0.05)
    assert not component.scored


def test_company_quality_is_three_observable_signals_and_says_what_it_is_not() -> None:
    component = components.score_company_quality(posting("x" * 3000), 0.05)
    assert component.score == pytest.approx(
        (100.0 + 0.0 + component.inputs["hiring_scale"]) / 3
    )
    assert "NOT a company reputation" in component.inputs["HONESTY_NOTE"]


# ------------------------------------------------------------- end to end


def _real_posting_text() -> str:
    return (
        "About the role\n"
        "You will lead process improvement across three plants.\n"
        "Requirements:\n"
        "- 5 years of experience in manufacturing\n"
        "- A Bachelor's degree in Industrial Engineering\n"
        "- Lean Manufacturing, Six Sigma and Value Stream Mapping\n"
        "- Experience with Minitab and Python\n"
    ) + ("Further detail about the team and the plant. " * 40)


def test_a_full_assessment_is_reproducible_from_its_own_components() -> None:
    result = score_posting(
        posting(_real_posting_text()), candidate(), requirements_floor=FLOOR
    )
    assert result.score.recompute() == pytest.approx(result.total)
    assert sum(result.score.effective_weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_every_declared_component_appears_in_the_result() -> None:
    result = score_posting(
        posting(_real_posting_text()), candidate(), requirements_floor=FLOOR
    )
    assert set(result.score.components) == set(WEIGHTS)


def test_an_unscored_component_names_where_its_weight_went() -> None:
    result = score_posting(
        posting(_real_posting_text()), candidate(), requirements_floor=FLOOR
    )
    row = result.as_row()
    assert row["unscored_components"], "salary is undisclosed here, so something must be unscored"
    for entry in row["unscored_components"]:
        assert entry["reason"]
        assert entry["redistributed_to"]


def test_reasons_only_restate_numbers_that_were_actually_scored() -> None:
    result = score_posting(
        posting(_real_posting_text()), candidate(), requirements_floor=FLOOR
    )
    assert any("could not be scored" in line for line in result.reasons_against)
    assert any("documented years meet" in line for line in result.reasons_for)

    # Nothing may be said about a component that was not scored. Salary is
    # undisclosed in this posting, so no reason may mention pay.
    assert "salary" in result.score.unscored
    assert not any("Pay is disclosed" in line for line in result.reasons_for)


def test_a_strong_skill_overlap_is_reported_as_one() -> None:
    body = (
        "Requirements:\n- Lean Manufacturing\n- Six Sigma\n- Python\n"
        "- Value Stream Mapping\n" + ("Detail about the plant. " * 40)
    )
    result = score_posting(posting(body), candidate(), requirements_floor=FLOOR)
    assert result.score.components["skill_match"]["score"] == pytest.approx(75.0)
    assert any("Skills line up" in line for line in result.reasons_for)


def test_an_unreadable_posting_scores_nothing_at_all() -> None:
    """The regression test for "Oops something happened", scored 61.14 by the legacy engine.

    Four components score *well* on a silent posting — no stated years and no
    stated degree both mean "nothing to fall short of" — so an empty page
    summed to a tier-B match. Below the requirements-confidence floor there is
    no score to report, and ``match_score`` is NULL rather than zero.
    """
    junk = posting("", title="Oops something happened", requirements_confidence=0.25)
    result = score_posting(junk, candidate(), requirements_floor=FLOOR)

    assert result.score.tier == "UNSCORED"
    assert result.as_row()["match_score"] is None
    assert set(result.score.unscored) == set(WEIGHTS)
    assert "could not be read" in result.reasons_against[0]


def test_the_floor_is_a_setting_and_not_a_constant(db) -> None:
    """I-23: moving the setting moves the behaviour, with no code change."""
    from career_scout.store import settings as settings_module

    junk = posting("", title="Oops something happened", requirements_confidence=0.25)
    settings_module.set_value(db, "requirements_confidence_floor", 0.10)
    lowered = settings_module.get(db, "requirements_confidence_floor")

    assert score_posting(junk, candidate(), requirements_floor=lowered).score.tier != "UNSCORED"


def test_score_reads_both_views_from_the_database(db) -> None:
    from career_scout.matching import score as score_from_db
    from career_scout.profile import records

    records.add(db, "identity.citizenship", "PK", confirmed=True)
    records.add(db, "skills[0].name", "Lean Manufacturing", confirmed=True)
    records.add(db, "work[0].startDate", "2019-01-01", confirmed=True)
    records.add(db, "work[0].endDate", "2024-01-01", confirmed=True)
    records.add(db, "education[0].studyType", "B.Sc.", confirmed=True)

    parsed = requirements_module.parse(_real_posting_text(), title="Industrial Engineer")
    db.execute(
        "INSERT INTO opportunity (id, kind, country_iso2, title, employer, requirements, "
        "requirements_confidence, description, source_url, fetched_at, dedupe_key, "
        "employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('opp-db', 'job', NULL, 'Industrial Engineer', 'Acme', ?, ?, ?, "
        "'https://example.test/1', '2026-08-08T00:00:00Z', 'k1', 'acme', "
        "'industrial engineer', '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z')",
        (__import__("json").dumps(parsed.as_dict()), parsed.confidence, _real_posting_text()),
    )

    result = score_from_db(db, "opp-db", as_of=date(2026, 8, 8))
    assert result.opportunity_id == "opp-db"
    assert result.score.recompute() == pytest.approx(result.total)
