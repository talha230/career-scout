"""The candidate view: what the scorer is allowed to know, and what it refuses.

Two properties carry the weight here. Unconfirmed records do not move a score,
and an unknown is an unknown rather than a zero — a candidate with no confirmed
work history must not look like one with no experience.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from jarvis.matching import profile_view
from jarvis.profile import records

pytestmark = pytest.mark.usefixtures("jarvis_home")

AS_OF = date(2026, 8, 8)


def _put(conn: sqlite3.Connection, path: str, value: object, *, confirmed: bool = True) -> str:
    return records.add(conn, path, value, confirmed=confirmed)


def _employed(conn: sqlite3.Connection, index: int, employer: str, start: str, end: str | None,
              *, confirmed: bool = True) -> None:
    _put(conn, f"work[{index}].employer", employer, confirmed=confirmed)
    _put(conn, f"work[{index}].startDate", start, confirmed=confirmed)
    if end is not None:
        _put(conn, f"work[{index}].endDate", end, confirmed=confirmed)


# ------------------------------------------------------------------- dates


def test_a_full_date_is_used_exactly_as_given() -> None:
    assert profile_view.parse_profile_date("2019-12-24", as_of=AS_OF) == (
        date(2019, 12, 24), "day"
    )


def test_a_month_precision_date_is_placed_at_the_midpoint_of_the_month() -> None:
    """An edge would bias every range; the midpoint is the smallest error."""
    assert profile_view.parse_profile_date("2019-03", as_of=AS_OF) == (date(2019, 3, 15), "month")


def test_a_year_precision_date_is_placed_at_the_midpoint_of_the_year() -> None:
    assert profile_view.parse_profile_date("2019", as_of=AS_OF) == (date(2019, 7, 1), "year")


def test_present_resolves_to_the_date_the_view_was_built_for() -> None:
    assert profile_view.parse_profile_date("Present", as_of=AS_OF) == (AS_OF, "open")


def test_an_unreadable_date_is_none_rather_than_a_guess() -> None:
    assert profile_view.parse_profile_date("sometime in the nineties", as_of=AS_OF) is None
    assert profile_view.parse_profile_date("2019-13-45", as_of=AS_OF) is None


# ------------------------------------------------------------- experience


def test_documented_years_matches_the_figure_the_master_cv_computed_by_hand(
    db: sqlite3.Connection,
) -> None:
    """The legacy profile recorded 6.62 years for exactly these two ranges.

    Reproducing that number from ``profile_record`` rather than from a typed
    field is the whole point of this module: it now moves when the record does.
    """
    _employed(db, 0, "Interloop Limited", "2021-04-01", None)
    _employed(db, 1, "Comet Sports (Pvt) Limited", "2019-12-24", "2021-03-31")

    view = profile_view.build(db, as_of=AS_OF)
    assert view.documented_years == 6.62
    assert view.years_basis["merged_days"] == 2418


def test_overlapping_roles_are_counted_once(db: sqlite3.Connection) -> None:
    """Two concurrent jobs are not twice the experience."""
    _employed(db, 0, "A", "2020-01-01", "2022-01-01")
    _employed(db, 1, "B", "2021-01-01", "2023-01-01")

    view = profile_view.build(db, as_of=AS_OF)
    assert view.documented_years == pytest.approx(3.0, abs=0.01)


def test_an_unconfirmed_role_does_not_add_experience(db: sqlite3.Connection) -> None:
    """A proposal the user has not looked at must not move a number they act on."""
    _employed(db, 0, "Confirmed Ltd", "2020-01-01", "2021-01-01")
    _employed(db, 1, "Unchecked Ltd", "2010-01-01", "2019-01-01", confirmed=False)

    view = profile_view.build(db, as_of=AS_OF)
    assert view.documented_years == pytest.approx(1.0, abs=0.01)
    assert view.unconfirmed_counts["work"] == 3


def test_no_confirmed_work_is_unknown_not_zero(db: sqlite3.Connection) -> None:
    """Zero years and unknown years are different claims, and only one is true."""
    view = profile_view.build(db, as_of=AS_OF)
    assert view.documented_years is None
    assert "documented_years" in view.unknowns


def test_the_unknown_reason_names_what_would_fix_it(db: sqlite3.Connection) -> None:
    _employed(db, 0, "Unchecked Ltd", "2020-01-01", None, confirmed=False)
    view = profile_view.build(db, as_of=AS_OF)
    assert "waiting for you to confirm" in view.unknowns["documented_years"]


def test_a_role_with_no_readable_start_date_is_reported_not_dropped_silently(
    db: sqlite3.Connection,
) -> None:
    _employed(db, 0, "Good Ltd", "2020-01-01", "2021-01-01")
    _put(db, "work[1].employer", "No Dates Ltd")

    view = profile_view.build(db, as_of=AS_OF)
    assert view.years_basis["skipped"] == [
        {"entry": "No Dates Ltd", "reason": "no confirmed, readable start date"}
    ]


def test_the_arithmetic_is_reproducible_by_hand(db: sqlite3.Connection) -> None:
    _employed(db, 0, "A", "2020-01-01", "2021-01-01")
    view = profile_view.build(db, as_of=AS_OF)
    days = view.years_basis["merged_days"]
    assert view.documented_years == pytest.approx(round(days / 365.25, 2))
    assert str(days) in view.years_basis["arithmetic"]


# -------------------------------------------------------------- education


def test_the_highest_confirmed_qualification_wins(db: sqlite3.Connection) -> None:
    _put(db, "education[0].studyType", "Bachelor of Science (B.Sc.)")
    _put(db, "education[1].studyType", "Master of Engineering")

    view = profile_view.build(db, as_of=AS_OF)
    assert view.education_level == "master"


def test_an_unconfirmed_qualification_does_not_raise_the_level(db: sqlite3.Connection) -> None:
    _put(db, "education[0].studyType", "Bachelor of Science")
    _put(db, "education[1].studyType", "PhD in Engineering", confirmed=False)

    view = profile_view.build(db, as_of=AS_OF)
    assert view.education_level == "bachelor"


def test_no_confirmed_qualification_is_unknown(db: sqlite3.Connection) -> None:
    view = profile_view.build(db, as_of=AS_OF)
    assert view.education_level is None
    assert "education_level" in view.unknowns


# ----------------------------------------------------------------- skills


def test_skills_come_from_the_users_own_records_not_from_the_packaged_config(
    db: sqlite3.Connection,
) -> None:
    """``candidate_has`` in skill_synonyms.json is one person's skill set.

    Reading it would hand every new installation the skills of whoever the
    config was tuned for, and every application would then claim them.
    """
    view = profile_view.build(db, as_of=AS_OF)
    assert view.skills == frozenset()

    _put(db, "skills[0].name", "Lean Manufacturing")
    assert "Lean Manufacturing" in profile_view.build(db, as_of=AS_OF).skills


def test_a_skill_is_matched_by_its_synonyms(db: sqlite3.Connection) -> None:
    _put(db, "skills[0].keywords", "toyota production system, kaizen")
    view = profile_view.build(db, as_of=AS_OF)
    assert "Lean Manufacturing" in view.skills
    assert view.skill_evidence["Lean Manufacturing"] == "toyota production system"


def test_an_unconfirmed_skill_is_not_claimed(db: sqlite3.Connection) -> None:
    _put(db, "skills[0].name", "Six Sigma", confirmed=False)
    view = profile_view.build(db, as_of=AS_OF)
    assert "Six Sigma" not in view.skills
    assert "skills" in view.unknowns


# ------------------------------------------------------------ preferences


def test_target_roles_are_read_in_index_order(db: sqlite3.Connection) -> None:
    _put(db, "preferences.target_roles[1]", "Process Engineer")
    _put(db, "preferences.target_roles[0]", "Industrial Engineer")

    view = profile_view.build(db, as_of=AS_OF)
    assert view.target_roles == ("Industrial Engineer", "Process Engineer")


def test_an_absent_salary_minimum_is_unknown_not_zero(db: sqlite3.Connection) -> None:
    view = profile_view.build(db, as_of=AS_OF)
    assert view.salary_minimum is None
    assert "invent" in view.unknowns["salary_minimum"]


# -------------------------------------------------------------- identity


def test_citizenship_is_split_and_uppercased(db: sqlite3.Connection) -> None:
    _put(db, "identity.citizenship", "pk,gb")
    view = profile_view.build(db, as_of=AS_OF)
    assert view.citizenship == ("PK", "GB")


def test_tax_residence_is_never_filled_in_from_somewhere_else(
    db: sqlite3.Connection,
) -> None:
    """It decides how a remote salary is taxed; a guess is a fabricated number."""
    _put(db, "identity.citizenship", "PK")
    _put(db, "basics.location.countryCode", "PK")

    view = profile_view.build(db, as_of=AS_OF)
    assert view.tax_residence is None
