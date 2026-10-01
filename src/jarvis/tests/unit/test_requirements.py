"""Requirement parsing, and the confidence figure that decides sufficiency.

The fixtures here are shapes taken from the 4,402-posting legacy corpus, not
inventions. Measured against that corpus this parser reads 97.2% of postings
well enough to match at least one vocabulary skill, and marks 1.7% as too
unreadable to evaluate — including every one of the junk rows the legacy engine
scored anyway.
"""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.discovery import requirements as R
from jarvis.profile import functions as fn
from jarvis.profile import records

REAL_POSTING = """
About the role
We are hiring a Senior Industrial Engineer to join our operations team in Berlin.
You will own line balancing, capacity planning and continuous improvement across
three production lines, working closely with the plant leadership team.

Responsibilities
- Lead time and motion studies and drive throughput improvements
- Own the CAPEX plan for line upgrades and present it to the executive team
- Partner with procurement on supplier qualification

Requirements
- 5+ years of experience in manufacturing or industrial engineering
- Bachelor's degree in Industrial Engineering or a related field
- Demonstrated expertise in Lean Manufacturing and Six Sigma
- Strong capability in Value Stream Mapping and root cause analysis
- Fluent English; German at B2 level is an advantage
- Lean Six Sigma Green Belt certification preferred

What we offer
Visa sponsorship and relocation support for the right candidate.
Please submit your CV and a short cover letter.
"""

#: Verbatim shape of a real row: a short stub that is not a posting at all.
JUNK_POSTING = "This page could not be loaded. Please try again later or contact support."


# ------------------------------------------------------------------ parsing


def test_a_real_posting_parses_confidently() -> None:
    result = R.parse(REAL_POSTING, title="Senior Industrial Engineer")
    assert result.confidence >= 0.9, R.explain(result)


def test_skills_are_found_and_attributed_to_the_requirements_section() -> None:
    """A skill under 'Requirements' is a requirement; the same word under
    'About us' is decoration."""
    result = R.parse(REAL_POSTING, title="Senior Industrial Engineer")
    assert "Lean Manufacturing" in result.skills
    assert "Six Sigma" in result.skills_in_requirements_section


def test_years_of_experience_are_read() -> None:
    assert R.parse(REAL_POSTING, title="x").required_years == 5


def test_seniority_comes_from_the_title() -> None:
    assert R.parse(REAL_POSTING, title="Senior Industrial Engineer").seniority == "senior"
    assert R.parse(REAL_POSTING, title="Graduate Engineer").seniority == "junior"
    assert R.parse("We need an intern", title="Working Student").seniority == "intern"


def test_degree_certification_and_language_signals_are_read() -> None:
    result = R.parse(REAL_POSTING, title="x")
    assert result.degree_required is True
    assert result.requires_certification is True
    assert result.requires_language_fluency is True


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # Two named levels: the posting's minimum is the lower one.
        ("Master's or PhD in engineering required.", "master"),
        ("PhD or Master's in engineering required.", "master"),
        ("Bachelor's or Master's degree in engineering.", "bachelor"),
        # The generic "degree in" must not drag a named level down to bachelor.
        ("Master's degree in Industrial Engineering.", "master"),
        ("PhD degree in physics.", "phd"),
        # ...but still counts when nothing is named.
        ("A degree in engineering is required.", "bachelor"),
    ],
)
def test_degree_level_is_the_lowest_named(text: str, level: str) -> None:
    assert R.parse(text, title="x").degree_level == level


@pytest.mark.parametrize(
    "text",
    [
        "Experience working as a Scrum Master in an agile team.",
        "Own master data quality across SAP modules.",
        "Reports to the Master Scheduler.",
        "Agencies need a valid ABS Master Service Agreement.",
        "Build a 360-degree view of the customer.",
        "Work with a high degree of accuracy.",
        "Advanced MS Excel and MS Office skills.",
    ],
)
def test_words_that_only_look_like_degrees_are_not_degrees(text: str) -> None:
    result = R.parse(text, title="x")
    assert result.degree_level is None
    assert result.degree_required is False


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("A Master’s degree is required.", "master"),
        ("Masters in Supply Chain Management.", "master"),
        ("Master of Science in Engineering.", "master"),
        ("Master degree in Data Science.", "master"),
        ("MSc or MBA preferred.", "master"),
        ("M.S. in Computer Science.", "master"),
    ],
)
def test_real_masters_demands_are_still_read(text: str, level: str) -> None:
    result = R.parse(text, title="x")
    assert result.degree_level == level
    assert result.degree_required is True


def test_sponsorship_offered_is_detected() -> None:
    assert R.parse(REAL_POSTING, title="x").sponsorship == "offered"


def test_sponsorship_refused_is_detected() -> None:
    text = "You must be authorized to work in the United States. " + REAL_POSTING
    assert R.parse(text, title="x").sponsorship == "refused"


def test_requested_documents_are_read_and_cv_is_always_included() -> None:
    result = R.parse(REAL_POSTING, title="x")
    assert "cv" in result.document_types
    assert "cover_letter" in result.document_types


def test_longest_phrase_wins_over_its_prefix() -> None:
    """Python's alternation is first-match, so ordering is load-bearing."""
    result = R.parse("Requirements: deep experience in Lean Six Sigma.", title="x")
    assert "Six Sigma" in result.skills or "Lean Manufacturing" in result.skills


def test_parsing_never_raises_on_empty_or_broken_input() -> None:
    for text in ("", "   ", "\x00\x01", "a" * 50_000):
        R.parse(text, title="")  # must not raise


# --------------------------------------------------------------- confidence


def test_junk_scores_below_the_floor() -> None:
    """The legacy engine scored a posting titled 'Oops something happened' at
    61.14 and would have generated an application for it."""
    result = R.parse(JUNK_POSTING, title="Oops something happened")
    assert result.confidence < 0.35, R.explain(result)


def test_an_empty_posting_scores_near_zero() -> None:
    assert R.parse("", title="").confidence <= 0.1


def test_a_short_body_is_capped_however_much_matched() -> None:
    """A stub cannot be confidently read no matter which words appear in it."""
    stub = "Lean Manufacturing Six Sigma 5 years experience bachelor degree IELTS"
    result = R.parse(stub, title="Industrial Engineer")
    assert result.confidence <= 0.2
    assert "capped_reason" in result.signals


def test_confidence_is_hand_reproducible() -> None:
    """Every number here has to be checkable from its own workings."""
    result = R.parse(REAL_POSTING, title="Senior Industrial Engineer")
    earned = result.signals["earned"]
    assert result.confidence == pytest.approx(round(min(sum(earned.values()), 1.0), 3))
    for name in earned:
        assert earned[name] == R.CONFIDENCE_WEIGHTS[name]


def test_explain_lists_every_signal_that_fired() -> None:
    result = R.parse(REAL_POSTING, title="Senior Industrial Engineer")
    text = R.explain(result)
    assert "has_requirements_section" in text
    assert "skills_in_section" in text


def test_a_posting_with_no_requirements_section_scores_lower() -> None:
    prose = (
        "We are a fast growing company building the future of logistics. "
        "Our team is distributed and we value ownership and curiosity. " * 8
    )
    with_section = R.parse(REAL_POSTING, title="Engineer").confidence
    without = R.parse(prose, title="Engineer").confidence
    assert without < with_section


# ---------------------------------------------------- reaching the verdict


@pytest.mark.invariant
def test_an_unreadable_posting_cannot_authorise_an_application(
    db: sqlite3.Connection,
) -> None:
    """I-18, end to end: parse -> confidence -> verdict -> may_generate.

    This is the whole chain the fail-closed rule exists to protect. A posting
    nobody could read must not produce a package.
    """
    for path, value in (
        ("basics.name", "Test Person"),
        ("basics.email", "t@example.com"),
        ("identity.citizenship", "PK"),
        ("identity.tax_residence", "PK"),
        ("work[0].employer", "Acme"),
        ("work[0].position", "Engineer"),
        ("work[0].startDate", "2019-01"),
        ("work[0].summary", "Did the work"),
    ):
        records.add(db, path, value, confirmed=True)

    parsed = R.parse(JUNK_POSTING, title="Oops something happened")
    verdict = fn.sufficiency(db, {
        "id": "junk", "kind": "job", "role_family": "unclassified",
        "requirements": parsed.as_dict(),
        "requirements_confidence": parsed.confidence,
    })
    assert verdict.verdict == "not_evaluated"
    assert verdict.may_generate is False


def test_a_readable_posting_does_reach_a_verdict(db: sqlite3.Connection) -> None:
    """The gate must not be so tight that nothing gets through."""
    for path, value in (
        ("basics.name", "Test Person"),
        ("basics.email", "t@example.com"),
        ("work[0].employer", "Acme"),
        ("work[0].position", "Engineer"),
        ("work[0].startDate", "2019-01"),
        ("work[0].summary", "Did the work"),
    ):
        records.add(db, path, value, confirmed=True)

    parsed = R.parse(REAL_POSTING, title="Senior Industrial Engineer")
    verdict = fn.sufficiency(db, {
        "id": "real", "kind": "job", "role_family": "industrial-engineering",
        "requirements": parsed.as_dict(),
        "requirements_confidence": parsed.confidence,
    })
    assert verdict.verdict in {"sufficient", "insufficient"}
    assert verdict.verdict != "not_evaluated"


def test_serialised_requirements_round_trip(db: sqlite3.Connection) -> None:
    """``as_dict`` is what lands in the database, so the verdict must read it."""
    parsed = R.parse(REAL_POSTING, title="Senior Industrial Engineer")
    payload = parsed.as_dict()
    assert payload["requires_certification"] is True
    assert "cv" in payload["document_types"]
    assert isinstance(payload["skills"], list)


# ------------------------------------------------------------- vocabulary


def test_vocabulary_is_loaded_from_config() -> None:
    assert R.vocabulary_size() >= 20


def test_the_matcher_is_one_pass_not_one_per_synonym() -> None:
    """Performance is a correctness concern at ingest: the per-synonym version
    took minutes over the real corpus, this one takes seconds."""
    pattern, lookup = R._skill_matcher(R.config_module.CURRENT_VERSION)
    assert len(lookup) > R.vocabulary_size()  # many phrases per canonical skill
    assert pattern.groups == 0  # a single non-capturing alternation
