"""Eligibility — T037.

Two properties dominate these tests. A blocked opportunity is **hidden, not
deleted**, and it carries the sentence from the posting and the fact from the
profile that produced the block. And **missing profile data never blocks**: a
user who has not confirmed their citizenship sees everything, with a reason, not
nothing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

import pytest

from jarvis import eligibility
from jarvis.discovery import requirements as requirements_module
from jarvis.matching import score
from jarvis.matching.posting import PostingView
from jarvis.matching.profile_view import CandidateView, build
from jarvis.profile import records


def _posting(
    text: str = "", *, title: str = "Industrial Engineer", **overrides: Any
) -> PostingView:
    parsed = requirements_module.parse(text, title=title)
    fields: dict[str, Any] = {
        "id": "opp-1",
        "kind": "job",
        "title": title,
        "employer": "Acme",
        "country_iso2": "US",
        "city": "Austin",
        "work_arrangement": "onsite",
        "role_family": "industrial_engineering",
        "requirements": parsed.as_dict(),
        "requirements_confidence": max(parsed.confidence, 0.9),
        "pay_disclosed": None,
        "funding_disclosed": None,
        "description_chars": len(text),
        "employer_open_roles": 1,
        "deadline": None,
        "posted_at": None,
        "source_url": "https://example.test/jobs/1",
    }
    fields.update(overrides)
    return PostingView(**fields)


def _candidate(**overrides: Any) -> CandidateView:
    fields: dict[str, Any] = {
        "documented_years": 6.6,
        "years_basis": {},
        "education_level": "bachelor",
        "education_evidence": "B.Sc. Industrial Engineering",
        "target_roles": ("Industrial Engineer",),
        "salary_minimum": None,
        "salary_currency": None,
        "citizenship": ("PK",),
        "residence_country": "PK",
        "residence_city": "Lahore",
        "tax_residence": "PK",
        "skills": frozenset({"Lean Manufacturing"}),
        "skill_evidence": {},
        "languages": frozenset({"english", "urdu"}),
        "unknowns": {},
        "unconfirmed_counts": {},
        "as_of": "2026-09-23",
    }
    fields.update(overrides)
    return CandidateView(**fields)


def _finding(verdict: eligibility.Verdict, dimension: str) -> eligibility.Finding:
    return next(f for f in verdict.findings if f.dimension == dimension)


# ---------------------------------------------------------------- the ordinary


def test_a_plain_posting_is_eligible(db: sqlite3.Connection) -> None:
    verdict = eligibility.evaluate(
        _posting("We are hiring an industrial engineer. Five years of experience."),
        _candidate(),
    )

    assert verdict.verdict == "eligible"
    assert verdict.hidden is False
    assert {f.state for f in verdict.findings} == {"ok"}


def test_every_dimension_is_answered(db: sqlite3.Connection) -> None:
    verdict = eligibility.evaluate(_posting("Ordinary posting."), _candidate())

    assert tuple(f.dimension for f in verdict.findings) == eligibility.DIMENSIONS


# ------------------------------------------------------------ right to work


def test_a_refusal_to_sponsor_abroad_blocks_with_both_sides_shown() -> None:
    verdict = eligibility.evaluate(
        _posting("You must be authorized to work in the United States; we do not sponsor."),
        _candidate(),
    )

    assert verdict.verdict == "ineligible"
    assert verdict.hidden is True
    finding = _finding(verdict, "right_to_work")
    assert finding.state == "blocked"
    assert finding.posting_evidence is not None
    assert "PK" in (finding.profile_evidence or "")


def test_a_citizenship_demand_blocks_and_is_quoted() -> None:
    verdict = eligibility.evaluate(_posting("Must be a US Citizen."), _candidate())

    finding = _finding(verdict, "right_to_work")
    assert finding.state == "blocked"
    assert "citizen" in (finding.posting_evidence or "").lower()
    assert "citizenship" in finding.reason


def test_the_same_posting_at_home_is_not_blocked() -> None:
    """A refusal to sponsor is no obstacle to somebody who lives there."""
    verdict = eligibility.evaluate(
        _posting(
            "You must be authorized to work in Pakistan; we do not sponsor.",
            country_iso2="PK",
        ),
        _candidate(),
    )

    assert verdict.verdict == "eligible"
    assert _finding(verdict, "right_to_work").state == "ok"


def test_residence_counts_and_says_it_assumed_so() -> None:
    verdict = eligibility.evaluate(
        _posting("We cannot offer visa sponsorship.", country_iso2="AE"),
        _candidate(citizenship=("PK",), residence_country="AE", residence_city="Dubai"),
    )

    finding = _finding(verdict, "right_to_work")
    assert finding.state == "ok"
    assert "assumed to carry the right to work" in finding.reason


def test_offered_sponsorship_clears_the_dimension() -> None:
    verdict = eligibility.evaluate(
        _posting("Visa sponsorship is available for the right candidate."), _candidate()
    )

    assert _finding(verdict, "right_to_work").state == "ok"


def test_unknown_citizenship_is_unscored_and_hides_nothing() -> None:
    verdict = eligibility.evaluate(
        _posting("Must be a US Citizen."),
        _candidate(
            citizenship=(),
            unknowns={"citizenship": "no confirmed citizenship, which is what decides"},
        ),
    )

    assert verdict.verdict == "unscored"
    assert verdict.hidden is False
    assert _finding(verdict, "right_to_work").state == "unscored"
    assert "citizenship" in verdict.detail


# ------------------------------------------------------------------ clearance


def test_a_clearance_requirement_blocks_a_non_citizen() -> None:
    verdict = eligibility.evaluate(
        _posting("An active security clearance is required for this role."), _candidate()
    )

    finding = _finding(verdict, "security_clearance")
    assert finding.state == "blocked"
    assert "issued only to its citizens" in finding.reason


def test_a_clearance_requirement_does_not_block_a_citizen() -> None:
    verdict = eligibility.evaluate(
        _posting("An active security clearance is required for this role."),
        _candidate(citizenship=("US",), residence_country="US"),
    )

    finding = _finding(verdict, "security_clearance")
    assert finding.state == "ok"
    assert "separate question" in finding.reason


def test_a_clearance_with_no_country_is_unscored() -> None:
    verdict = eligibility.evaluate(
        _posting("Security clearance required.", country_iso2=None), _candidate()
    )

    assert _finding(verdict, "security_clearance").state == "unscored"
    assert verdict.hidden is False


# -------------------------------------------------------------- qualification


def test_a_phd_requirement_blocks_a_bachelor() -> None:
    verdict = eligibility.evaluate(
        _posting("A PhD is required. Requirements: PhD in engineering."), _candidate()
    )

    finding = _finding(verdict, "qualification")
    assert finding.state == "blocked"
    assert "bachelor" in finding.reason


def test_a_masters_requirement_does_not_block_a_masters() -> None:
    verdict = eligibility.evaluate(
        _posting("A Master's degree is required."), _candidate(education_level="master")
    )

    assert _finding(verdict, "qualification").state == "ok"


def test_an_unconfirmed_qualification_is_unscored() -> None:
    verdict = eligibility.evaluate(
        _posting("A PhD is required."),
        _candidate(
            education_level=None,
            education_evidence=None,
            unknowns={"education_level": "no confirmed qualification on record"},
        ),
    )

    assert _finding(verdict, "qualification").state == "unscored"
    assert verdict.verdict == "unscored"
    assert verdict.hidden is False


# ------------------------------------------------------------------- language


def test_a_language_the_user_has_not_confirmed_blocks() -> None:
    verdict = eligibility.evaluate(
        _posting("Fluent German is required.", country_iso2="DE"), _candidate()
    )

    finding = _finding(verdict, "language")
    assert finding.state == "blocked"
    assert "german" in finding.reason
    assert "German" in (finding.posting_evidence or "")


def test_a_language_the_user_speaks_does_not_block() -> None:
    verdict = eligibility.evaluate(
        _posting("Fluent German is required.", country_iso2="DE"),
        _candidate(languages=frozenset({"german", "english"})),
    )

    assert _finding(verdict, "language").state == "ok"


def test_a_fluency_demand_with_no_readable_language_is_unscored() -> None:
    """The posting demands something; guessing which language would hide a role."""
    posting = _posting("Fluency in the local language is essential.")
    requirements = dict(posting.requirements)
    requirements["requires_language_fluency"] = True
    requirements["languages_required"] = []

    verdict = eligibility.evaluate(
        _posting(country_iso2="DE", requirements=requirements), _candidate()
    )

    finding = _finding(verdict, "language")
    assert finding.state == "unscored"
    assert "not treated as satisfied" in finding.reason


def test_a_language_demand_with_no_confirmed_languages_is_unscored() -> None:
    verdict = eligibility.evaluate(
        _posting("Fluent German is required.", country_iso2="DE"),
        _candidate(languages=frozenset()),
    )

    assert _finding(verdict, "language").state == "unscored"
    assert verdict.hidden is False


# ---------------------------------------------------------- through the score


def test_the_verdict_reaches_the_assessment_row(db: sqlite3.Connection) -> None:
    records.add(db, "identity.citizenship", "PK", confirmed=True)
    records.add(db, "basics.location.countryCode", "PK", confirmed=True)
    records.add(db, "basics.location.city", "Lahore", confirmed=True)
    records.add(db, "languages[0].language", "English", confirmed=True)
    records.add(db, "education[0].studyType", "B.Sc.", confirmed=True)
    records.add(db, "skills[0].name", "Lean Manufacturing", confirmed=True)

    text = (
        "Industrial Engineer. Requirements: bachelor's degree, five years of "
        "manufacturing experience, lean manufacturing, Six Sigma, AutoCAD, Excel. "
        "An active security clearance is required. You must be a US Citizen."
    )
    parsed = requirements_module.parse(text, title="Industrial Engineer")
    db.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES ('US', 'USA', 1)")
    db.execute(
        "INSERT INTO opportunity (id, kind, country_iso2, city, work_arrangement, title, "
        "employer, role_family, requirements, requirements_confidence, description, "
        "source_url, fetched_at, dedupe_key, employer_norm, title_norm, first_seen_at, "
        "last_seen_at) VALUES ('opp-1', 'job', 'US', 'Austin', 'onsite', "
        "'Industrial Engineer', 'Acme', 'industrial_engineering', ?, ?, ?, "
        "'https://example.test/1', '2026-09-20T00:00:00Z', 'k1', 'acme', "
        "'industrial engineer', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')",
        (json.dumps(parsed.as_dict()), max(parsed.confidence, 0.9), text),
    )

    result = score(db, "opp-1", as_of=date(2026, 9, 23))
    row = result.as_row()

    assert result.eligibility is not None
    assert row["eligibility_verdict"] == "ineligible"
    assert "clearance" in row["eligibility_detail"]
    assert row["inputs"]["eligibility"]["hidden"] is True

    # Soft-hide, not delete: the row, the score and the projection all survive.
    assert row["match_score"] is not None
    assert result.projection is not None
    assert db.execute("SELECT COUNT(*) AS n FROM opportunity").fetchone()["n"] == 1


def test_the_verdict_is_storable(db: sqlite3.Connection) -> None:
    """``eligibility_verdict`` has a CHECK constraint; every value must satisfy it."""
    db.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES ('US', 'USA', 1)")
    db.execute(
        "INSERT INTO opportunity (id, kind, title, employer, source_url, fetched_at, "
        "dedupe_key, employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('opp-2', 'job', 'T', 'A', 'https://example.test/2', "
        "'2026-09-20T00:00:00Z', 'k2', 'a', 't', '2026-09-20T00:00:00Z', "
        "'2026-09-20T00:00:00Z')"
    )
    for verdict in ("eligible", "ineligible", "unscored"):
        db.execute(
            "INSERT INTO assessment (opportunity_id, config_version, formula, weights, "
            "inputs, eligibility_verdict, eligibility_detail, computed_at) "
            "VALUES ('opp-2', ?, 'f', '{}', '{}', ?, 'because', '2026-09-23T00:00:00Z')",
            (f"v1-{verdict}", verdict),
        )

    stored = {row["eligibility_verdict"] for row in db.execute(
        "SELECT eligibility_verdict FROM assessment"
    )}
    assert stored == {"eligible", "ineligible", "unscored"}


def test_confirmed_languages_reach_the_view(db: sqlite3.Connection) -> None:
    records.add(db, "languages[0].language", "Deutsch", confirmed=True)
    records.add(db, "languages[1].language", "English, Urdu", confirmed=True)

    view = build(db, as_of=date(2026, 9, 23))

    # "Deutsch" and "German" are one language, normalised through the same table
    # the posting parser uses.
    assert view.languages == frozenset({"german", "english", "urdu"})


def test_an_unconfirmed_language_does_not_count(db: sqlite3.Connection) -> None:
    records.add(db, "languages[0].language", "German", confirmed=False)

    view = build(db, as_of=date(2026, 9, 23))

    assert view.languages == frozenset()
    assert "languages" in view.unknowns


@pytest.mark.parametrize(
    ("verdict", "hidden"),
    [("eligible", False), ("unscored", False), ("ineligible", True)],
)
def test_only_ineligible_hides(verdict: str, hidden: bool) -> None:
    assert eligibility.Verdict(verdict=verdict, detail="").hidden is hidden


def test_a_masters_demand_does_not_exclude_a_bachelor() -> None:
    """Measured: treating it as exclusion hides 396 of 4,402 corpus postings.

    Employers routinely accept equivalent experience against a stated Master's,
    so it is a shortfall the education component scores rather than a wall. Only
    the levels listed in eligibility.json exclude, and by default that is a
    doctorate alone.
    """
    verdict = eligibility.evaluate(
        _posting("A Master's degree is required for this role."), _candidate()
    )

    finding = _finding(verdict, "qualification")
    assert finding.state == "ok"
    assert "scored as a shortfall" in finding.reason
    assert verdict.verdict == "eligible"


def test_the_blocking_levels_come_from_config() -> None:
    from jarvis import config as config_module

    rules = config_module.eligibility()["qualification"]
    assert rules["blocking_levels"] == ["phd"]
    assert any("396" in line for line in rules["basis"])


def test_the_service_carries_the_verdict_without_dropping_the_row(
    db: sqlite3.Connection,
) -> None:
    """A soft hide is the caller's decision; the API never omits the row."""
    import json as json_module

    from jarvis import service

    records.add(db, "identity.citizenship", "PK", confirmed=True)
    records.add(db, "basics.location.countryCode", "PK", confirmed=True)
    records.add(db, "languages[0].language", "English", confirmed=True)
    records.add(db, "education[0].studyType", "B.Sc.", confirmed=True)
    records.add(db, "skills[0].name", "Lean Manufacturing", confirmed=True)

    text = (
        "Industrial Engineer. Requirements: bachelor's degree, five years of "
        "manufacturing experience, lean manufacturing and Six Sigma. An active "
        "security clearance is required."
    )
    parsed = requirements_module.parse(text, title="Industrial Engineer")
    db.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES ('US', 'USA', 1)")
    db.execute(
        "INSERT INTO opportunity (id, kind, country_iso2, title, employer, role_family, "
        "requirements, requirements_confidence, description, source_url, fetched_at, "
        "dedupe_key, employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('opp-9', 'job', 'US', 'Industrial Engineer', 'Acme', "
        "'industrial_engineering', ?, ?, ?, 'https://example.test/9', "
        "'2026-09-20T00:00:00Z', 'k9', 'acme', 'industrial engineer', "
        "'2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')",
        (json_module.dumps(parsed.as_dict()), max(parsed.confidence, 0.9), text),
    )
    row = score(db, "opp-9", as_of=date(2026, 9, 23)).as_row()
    db.execute(
        "INSERT INTO assessment (opportunity_id, config_version, match_score, confidence, "
        "formula, weights, inputs, unscored_components, eligibility_verdict, "
        "eligibility_detail, pay_basis, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-09-23T00:00:00Z')",
        (
            row["opportunity_id"],
            row["config_version"],
            row["match_score"],
            row["confidence"],
            row["formula"],
            json_module.dumps(row["weights"]),
            json_module.dumps(row["inputs"]),
            json_module.dumps(row["unscored_components"]),
            row["eligibility_verdict"],
            row["eligibility_detail"],
            row.get("pay_basis"),
        ),
    )

    listed = service.list_opportunities(db)
    assert [item["id"] for item in listed] == ["opp-9"]
    assert listed[0]["eligibility_verdict"] == "ineligible"
    assert listed[0]["hidden"] is True
    assert "clearance" in listed[0]["eligibility_detail"]

    payload = service.get_opportunity(db, "opp-9")
    assert payload["eligibility"]["verdict"] == "ineligible"
    assert payload["eligibility"]["hidden"] is True
    dimensions = {f["dimension"] for f in payload["eligibility"]["findings"]}
    assert dimensions == set(eligibility.DIMENSIONS)
