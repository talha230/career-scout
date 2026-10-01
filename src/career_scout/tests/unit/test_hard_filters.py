"""Hard filters — T038.

Every rejection here is a stored row naming a rule, and the posting stays. The
tests that matter most are the ones asserting a filter does **not** fire: on a
missing date, on unknown documented years, and on a body word that only appears
in the title of a different kind of role.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

import pytest

from career_scout import config as config_module
from career_scout import service as service_module
from career_scout.discovery import requirements as requirements_module
from career_scout.eligibility import filters
from career_scout.matching import screen
from career_scout.matching.posting import PostingView
from career_scout.profile import records
from career_scout.store import settings as settings_module


def _posting(**overrides: Any) -> PostingView:
    fields: dict[str, Any] = {
        "id": "opp-1",
        "kind": "job",
        "title": "Industrial Engineer",
        "employer": "Acme",
        "country_iso2": "US",
        "city": "Austin",
        "work_arrangement": "onsite",
        "role_family": "industrial_engineering",
        "requirements": {},
        "requirements_confidence": 0.9,
        "pay_disclosed": None,
        "funding_disclosed": None,
        "description_chars": 900,
        "employer_open_roles": 1,
        "deadline": None,
        "posted_at": None,
        "source_url": "https://example.test/jobs/1",
    }
    fields.update(overrides)
    return PostingView(**fields)


def _apply(posting: PostingView, **kwargs: Any) -> filters.Rejection | None:
    options: dict[str, Any] = {
        "staleness_days": 45,
        "documented_years": 6.62,
        "today": date(2026, 9, 23),
    }
    options.update(kwargs)
    return filters.apply(posting, **options)


# --------------------------------------------------------------- what fires


def test_a_posting_with_no_source_url_is_rejected() -> None:
    found = _apply(_posting(source_url=""))

    assert found is not None
    assert found.rule == "F10_missing_essential_data"
    assert "source_url" in found.evidence


def test_an_internship_title_is_rejected() -> None:
    found = _apply(_posting(title="Industrial Engineering Internship"))

    assert found is not None
    assert found.rule == "F08_non_professional_role"
    assert "Internship" in found.evidence


def test_a_stale_posting_is_rejected_with_its_age() -> None:
    found = _apply(_posting(posted_at="2026-06-01T00:00:00Z"))

    assert found is not None
    assert found.rule == "F07_posting_too_old"
    assert "114 days ago" in found.reason
    assert "45-day" in found.reason


def test_a_year_demand_far_above_documented_is_rejected() -> None:
    found = _apply(_posting(requirements={"required_years": 15}))

    assert found is not None
    assert found.rule == "F05_experience_far_above_documented"
    assert "15 years" in found.reason
    assert "6.62" in found.reason


def test_an_excluded_country_is_rejected() -> None:
    """The list is empty by default, so this drives it through the config."""
    rule = next(
        r for r in filters.enabled_filters() if r["id"] == "F09_excluded_countries"
    )
    rule = {**rule, "excluded_country_codes": ["US"]}

    found = filters._check(rule, _posting(), 45, 6.62, date(2026, 9, 23))

    assert found is not None
    assert "US is on your excluded list" in found.reason


# ----------------------------------------------------------- what must not fire


def test_a_posting_with_no_date_is_not_stale() -> None:
    """An unknown treated as a fact would reject every source without dates."""
    assert _apply(_posting(posted_at=None)) is None


def test_an_unreadable_date_is_not_stale() -> None:
    assert _apply(_posting(posted_at="sometime last spring")) is None


def test_unknown_documented_years_never_fires_the_experience_filter() -> None:
    """An unfinished profile must not reject the whole board."""
    assert _apply(_posting(requirements={"required_years": 15}), documented_years=None) is None


def test_a_modest_year_gap_is_left_to_the_score() -> None:
    assert _apply(_posting(requirements={"required_years": 10})) is None


def test_mentoring_interns_in_the_body_does_not_reject() -> None:
    """F08 matches the title only, which is why the rule says so."""
    assert _apply(_posting(title="Senior Industrial Engineer")) is None


def test_the_empty_excluded_list_rejects_nothing() -> None:
    assert _apply(_posting(country_iso2="US")) is None


# ----------------------------------------------------------------- the config


def test_the_profile_dependent_rules_are_disabled_and_say_where_they_went() -> None:
    """They became eligibility, which soft-hides instead of rejecting."""
    by_id = {r["id"]: r for r in config_module.hard_filters()["filters"]}
    moved = {
        "F01_explicit_no_sponsorship_abroad": "eligibility.right_to_work",
        "F02_security_clearance_required": "eligibility.security_clearance",
        "F03_citizenship_or_residency_required": "eligibility.right_to_work",
        "F04_degree_above_user": "eligibility.qualification",
        "F06_language_requirement_user_lacks": "eligibility.language",
    }
    for rule_id, destination in moved.items():
        assert by_id[rule_id]["enabled"] is False
        assert by_id[rule_id]["moved_to"] == destination

    assert {r["id"] for r in filters.enabled_filters()} == {
        "F10_missing_essential_data",
        "F08_non_professional_role",
        "F07_posting_too_old",
        "F05_experience_far_above_documented",
        "F09_excluded_countries",
    }


def test_a_body_matching_rule_is_refused_rather_than_matched_on_the_title() -> None:
    """The view carries no description; pretending otherwise would fake the rule."""
    rule = {
        "id": "F99_body_rule",
        "enabled": True,
        "label": "invented",
        "patterns": ["anything"],
        "applies_to": "title_and_body",
    }

    with pytest.raises(ValueError, match="can only see the title"):
        filters._check(rule, _posting(), 45, 6.62, date(2026, 9, 23))


# ------------------------------------------------------------ the stored row


def _seed(conn: sqlite3.Connection, **columns: Any) -> None:
    text = columns.pop(
        "description",
        "Industrial Engineer. Requirements: bachelor's degree, five years of "
        "manufacturing experience, lean manufacturing, Six Sigma, AutoCAD and Excel.",
    )
    parsed = requirements_module.parse(text, title=columns.get("title", "Industrial Engineer"))
    row: dict[str, Any] = {
        "id": "opp-1",
        "kind": "job",
        "country_iso2": None,
        "city": "Austin",
        "work_arrangement": "onsite",
        "title": "Industrial Engineer",
        "employer": "Acme",
        "role_family": "industrial_engineering",
        "requirements": json.dumps(parsed.as_dict()),
        "requirements_confidence": max(parsed.confidence, 0.9),
        "description": text,
        "posted_at": None,
        "source_url": "https://example.test/jobs/1",
        "fetched_at": "2026-09-20T00:00:00Z",
        "dedupe_key": "k1",
        "employer_norm": "acme",
        "title_norm": "industrial engineer",
        "first_seen_at": "2026-09-20T00:00:00Z",
        "last_seen_at": "2026-09-20T00:00:00Z",
    }
    row.update(columns)
    names = ", ".join(row)
    places = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO opportunity ({names}) VALUES ({places})", tuple(row.values()))


def _profile(conn: sqlite3.Connection) -> None:
    records.add(conn, "identity.citizenship", "PK", confirmed=True)
    records.add(conn, "basics.location.countryCode", "PK", confirmed=True)
    records.add(conn, "work[0].startDate", "2019-01-01", confirmed=True)
    records.add(conn, "work[0].endDate", "2024-01-01", confirmed=True)
    records.add(conn, "education[0].studyType", "B.Sc.", confirmed=True)
    records.add(conn, "skills[0].name", "Lean Manufacturing", confirmed=True)


def test_screening_a_rejected_posting_writes_the_row_and_skips_scoring(
    db: sqlite3.Connection,
) -> None:
    _profile(db)
    _seed(db, title="Manufacturing Internship")

    result = screen(db, "opp-1", as_of=date(2026, 9, 23), run_id="run-1")

    assert result.rejected
    assert result.assessment is None

    row = db.execute("SELECT * FROM rejection").fetchone()
    assert row["rule"] == "F08_non_professional_role"
    assert row["run_id"] == "run-1"
    assert "Internship" in row["evidence"]

    # Stored, not deleted: the posting is exactly where it was.
    assert db.execute("SELECT COUNT(*) AS n FROM opportunity").fetchone()["n"] == 1


def test_screening_a_good_posting_scores_it_and_writes_no_rejection(
    db: sqlite3.Connection,
) -> None:
    _profile(db)
    _seed(db)

    result = screen(db, "opp-1", as_of=date(2026, 9, 23))

    assert not result.rejected
    assert result.assessment is not None
    assert result.assessment.total > 0
    assert db.execute("SELECT COUNT(*) AS n FROM rejection").fetchone()["n"] == 0


def test_the_staleness_window_comes_from_settings(db: sqlite3.Connection) -> None:
    _profile(db)
    _seed(db, posted_at="2026-08-01T00:00:00Z")  # 53 days before the frozen today

    settings_module.set_value(db, "staleness_window_days", 90)
    assert not screen(db, "opp-1", as_of=date(2026, 9, 23)).rejected

    settings_module.set_value(db, "staleness_window_days", 30)
    again = screen(db, "opp-1", as_of=date(2026, 9, 23))
    assert again.rejected
    assert again.rejection is not None
    assert "30-day" in again.rejection.reason


def test_disabling_a_rule_brings_the_posting_back(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reversibility is the property that makes a hard filter safe to have."""
    _profile(db)
    _seed(db, title="Manufacturing Internship")

    assert screen(db, "opp-1", as_of=date(2026, 9, 23)).rejected

    original = config_module.hard_filters()
    disabled = {
        **original,
        "filters": [
            {**rule, "enabled": False} if rule["id"] == "F08_non_professional_role" else rule
            for rule in original["filters"]
        ],
    }
    monkeypatch.setattr(config_module, "hard_filters", lambda *_a, **_k: disabled)

    again = screen(db, "opp-1", as_of=date(2026, 9, 23))

    assert not again.rejected
    row = db.execute("SELECT * FROM rejection").fetchone()
    assert row["rule"] == "F08_non_professional_role"   # kept, not deleted
    assert row["lifted_at"] is not None
    assert "re-assessment" in row["lifted_reason"]
    assert service_module.get_opportunity(db, "opp-1")["rejected"] is False


def test_rescreening_a_still_rejected_posting_writes_no_repeat_row(
    db: sqlite3.Connection,
) -> None:
    _profile(db)
    _seed(db, title="Manufacturing Internship")

    for _ in range(3):
        assert screen(db, "opp-1", as_of=date(2026, 9, 23)).rejected

    assert db.execute("SELECT COUNT(*) AS n FROM rejection").fetchone()["n"] == 1


def test_widening_the_staleness_window_lifts_the_stale_rejection(
    db: sqlite3.Connection,
) -> None:
    _profile(db)
    _seed(db, posted_at="2026-08-01T00:00:00Z")

    settings_module.set_value(db, "staleness_window_days", 30)
    assert screen(db, "opp-1", as_of=date(2026, 9, 23)).rejected
    settings_module.set_value(db, "staleness_window_days", 90)
    assert not screen(db, "opp-1", as_of=date(2026, 9, 23)).rejected

    listed = service_module.list_opportunities(db)
    assert listed[0]["rejected"] is False
    payload = service_module.get_opportunity(db, "opp-1")
    assert [r["active"] for r in payload["rejections"]] == [False]


def test_the_service_shows_the_rejection_rather_than_an_absence(
    db: sqlite3.Connection,
) -> None:
    """"Why is this not in my shortlist?" has to have an answer in the payload."""
    from career_scout import service

    _profile(db)
    _seed(db, title="Manufacturing Internship")
    screen(db, "opp-1", as_of=date(2026, 9, 23))

    payload = service.get_opportunity(db, "opp-1")

    assert len(payload["rejections"]) == 1
    assert payload["rejections"][0]["rule"] == "F08_non_professional_role"
    assert "Internship" in payload["rejections"][0]["evidence"]

    # And the posting is still listed, because a rejection is not a deletion.
    listed = service.list_opportunities(db)
    assert [row["id"] for row in listed] == ["opp-1"]
