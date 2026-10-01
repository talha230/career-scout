"""Scholarship funding — T036.

``T = stipend + allowances + visa-capped work income``, minus tax, tuition and
living costs. Most of these tests are about the three asymmetries: a waiver on
the cost side, an unverified work income at zero, and a silent fee charged in
full.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from jarvis.matching.posting import PostingView
from jarvis.matching.profile_view import CandidateView
from jarvis.money import funding, fx
from jarvis.store import settings as settings_module
from jarvis.tests import support

BERLIN_COSTS = {
    "rent": 900.0,
    "utilities": 150.0,
    "food": 300.0,
    "transport": 60.0,
    "health_insurance": 120.0,
}

FUNDING = {
    "stipend": {"amount": 1200.0, "currency": "EUR", "period": "month"},
    "allowances": [{"label": "travel", "amount": 1200.0, "currency": "EUR", "period": "year"}],
    "tuition": {"amount": 3000.0, "currency": "EUR", "period": "year"},
    "tuition_waived": True,
    "work_allowed": False,
    "tax_exempt": True,
    "quote": "The scholarship pays EUR 1,200 per month and waives tuition in full.",
    "source_url": "https://example.test/scholarship",
}


def _posting(**overrides: Any) -> PostingView:
    fields: dict[str, Any] = {
        "id": "sch-1",
        "kind": "scholarship",
        "title": "MSc Industrial Engineering Scholarship",
        "employer": "TU Berlin",
        "country_iso2": "DE",
        "city": "Berlin",
        "work_arrangement": None,
        "role_family": "industrial_engineering",
        "requirements": {},
        "requirements_confidence": 0.9,
        "pay_disclosed": None,
        "funding_disclosed": dict(FUNDING),
        "description_chars": 1200,
        "employer_open_roles": 1,
        "deadline": "2026-12-01",
        "posted_at": None,
        "source_url": "https://example.test/scholarship",
    }
    fields.update(overrides)
    return PostingView(**fields)


def _candidate(**overrides: Any) -> CandidateView:
    fields: dict[str, Any] = {
        "documented_years": 6.6,
        "years_basis": {},
        "education_level": "bachelor",
        "education_evidence": "B.Sc.",
        "target_roles": ("Industrial Engineer",),
        "salary_minimum": None,
        "salary_currency": None,
        "citizenship": ("PK",),
        "residence_country": "PK",
        "residence_city": "Lahore",
        "tax_residence": "PK",
        "skills": frozenset(),
        "skill_evidence": {},
        "unknowns": {},
        "unconfirmed_counts": {},
        "as_of": "2026-09-23",
    }
    fields.update(overrides)
    return CandidateView(**fields)


def _country(conn: sqlite3.Connection, iso2: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES (?, ?, 1)", (iso2, iso2)
    )


def _figure(
    conn: sqlite3.Connection,
    kind: str,
    value: float,
    unit: str,
    *,
    currency: str | None = None,
    country: str | None = "DE",
    city: str | None = "Berlin",
    occupation: str | None = None,
) -> str:
    if country:
        _country(conn, country)
    # Through the shared helper, which writes the snapshot the quote has to
    # appear in. I-22 refuses a figure whose quote is not in its page.
    return support.sourced_figure(
        conn,
        kind=kind,
        value=value,
        unit=unit,
        currency=currency,
        country_iso2=country,
        city=city,
        occupation=occupation,
    )


def _sourced_world(conn: sqlite3.Connection) -> None:
    _country(conn, "DE")
    fx.record_rate(
        conn,
        base="EUR",
        quote="USD",
        rate=1.08,
        as_of="2026-09-20",
        source_url="https://example.test/rates",
    )
    _figure(conn, "tax_rate", 30.0, "percent", city=None)
    for kind, value in BERLIN_COSTS.items():
        _figure(conn, kind, value, "per_month", currency="EUR")


def _net(result: funding.Projection) -> float:
    assert result.net_savings_usd_month is not None
    return result.net_savings_usd_month


# ------------------------------------------------------------- the happy path


def test_the_full_package_adds_up_by_hand(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = funding.project(db, _posting(), _candidate(), today="2026-09-23")

    stipend = 1200.0 * 1.08
    travel = 1200.0 / 12 * 1.08
    costs = sum(BERLIN_COSTS.values()) * 1.08
    assert result.gross_usd_month == pytest.approx(stipend + travel)
    assert result.tax_usd_month == pytest.approx(0.0)  # the scheme states exempt
    assert result.cost_usd_month == pytest.approx(costs)  # tuition waived
    assert _net(result) == pytest.approx(stipend + travel - costs)
    assert not result.unscored


def test_every_funded_line_is_cited(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    stored = funding.project(db, _posting(), _candidate(), today="2026-09-23").as_dict()

    kinds = [line["kind"] for line in stored["lines"]]
    assert kinds.count("stipend") == 1
    assert kinds.count("allowance") == 1
    assert "work_income" in kinds
    assert "tuition" in kinds
    for line in stored["lines"]:
        if line["kind"] in {"stipend", "allowance"}:
            assert line["fx_rate_id"], f"{line['kind']} cites no FX rate"
        assert line["source_url"] is not None
    assert stored["fx_rate_ids"]


# ------------------------------------------------------------ the asymmetries


def test_a_waiver_reduces_the_cost_and_never_the_income(db: sqlite3.Connection) -> None:
    """A 3,000 EUR waiver must not appear as 3,000 EUR of income."""
    _sourced_world(db)

    waived = funding.project(db, _posting(), _candidate(), today="2026-09-23")
    charged = funding.project(
        db,
        _posting(funding_disclosed={**FUNDING, "tuition_waived": False}),
        _candidate(),
        today="2026-09-23",
    )

    assert waived.gross_usd_month == charged.gross_usd_month
    assert _net(waived) > _net(charged)
    assert _net(waived) - _net(charged) == pytest.approx(3000.0 / 12 * 1.08)

    tuition = next(line for line in waived.lines if line.kind == "tuition")
    assert tuition.direction == "cost"
    assert tuition.amount_usd_month == 0.0
    assert "never added to income" in tuition.arithmetic


def test_tuition_the_scheme_is_silent_about_is_charged_in_full(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = funding.project(
        db,
        _posting(funding_disclosed={**FUNDING, "tuition_waived": None}),
        _candidate(),
        today="2026-09-23",
    )

    tuition = next(line for line in result.lines if line.kind == "tuition")
    assert tuition.amount_usd_month == pytest.approx(3000.0 / 12 * 1.08)
    assert any("has not waived them" in note for note in result.notes)


def test_prohibited_work_is_an_explicit_zero_with_its_reason(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = funding.project(db, _posting(), _candidate(), today="2026-09-23")

    work = next(line for line in result.lines if line.kind == "work_income")
    assert work.amount_usd_month == 0.0
    assert "prohibits paid work" in work.arithmetic
    assert any("counted as zero" in note for note in result.notes)


def test_unverified_work_is_zero_rather_than_an_invented_job(db: sqlite3.Connection) -> None:
    """Work is allowed, but no published hour limit and no wage figure exist."""
    _sourced_world(db)

    result = funding.project(
        db,
        _posting(funding_disclosed={**FUNDING, "work_allowed": True, "work_hours_per_week": 20}),
        _candidate(),
        today="2026-09-23",
    )

    work = next(line for line in result.lines if line.kind == "work_income")
    assert work.amount_usd_month == 0.0
    assert "no published limit on student working hours" in work.arithmetic
    assert result.net_savings_usd_month is not None  # zero income is not UNSCORED


def test_permitted_work_is_capped_by_the_published_visa_limit(db: sqlite3.Connection) -> None:
    _sourced_world(db)
    _figure(db, "visa_rule", 20.0, "hours_per_week", city=None)
    _figure(db, "wage_stat", 13.0, "per_hour", currency="EUR", city=None)

    result = funding.project(
        db,
        # The scheme allows 30 hours; the visa permits 20. The lower wins.
        _posting(funding_disclosed={**FUNDING, "work_allowed": True, "work_hours_per_week": 30}),
        _candidate(),
        today="2026-09-23",
    )

    work = next(line for line in result.lines if line.kind == "work_income")
    assert work.amount_usd_month == pytest.approx(20.0 * (52 / 12) * 13.0 * 1.08)
    assert "20 h/week" in work.label
    assert work.assumption is not None and "actually worked" in work.assumption


def test_a_scheme_that_publishes_no_funding_is_unscored(db: sqlite3.Connection) -> None:
    """No published stipend is not a zero stipend."""
    _sourced_world(db)

    result = funding.project(
        db, _posting(funding_disclosed=None), _candidate(), today="2026-09-23"
    )

    assert result.gross_usd_month is None
    assert result.net_savings_usd_month is None
    assert any(item.component == "funding" for item in result.unscored)


def test_allowances_without_amounts_do_not_silently_vanish(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = funding.project(
        db,
        _posting(
            funding_disclosed={
                **FUNDING,
                "allowances": [{"label": "relocation", "amount": None, "period": "year"}],
            }
        ),
        _candidate(),
        today="2026-09-23",
    )

    assert result.net_savings_usd_month is None
    assert any(item.component == "allowance_0" for item in result.unscored)


# ------------------------------------------------------------------- the tax


def test_an_award_silent_about_tax_is_taxed_and_says_so(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = funding.project(
        db,
        _posting(funding_disclosed={**FUNDING, "tax_exempt": None}),
        _candidate(),
        today="2026-09-23",
    )

    assert result.tax_usd_month == pytest.approx(result.gross_usd_month * 0.30)
    assert any("does not say whether the award is taxable" in note for note in result.notes)


def test_a_taxable_award_with_no_rate_on_file_is_unscored(db: sqlite3.Connection) -> None:
    _country(db, "DE")
    fx.record_rate(
        db,
        base="EUR",
        quote="USD",
        rate=1.08,
        as_of="2026-09-20",
        source_url="https://example.test/rates",
    )
    for kind, value in BERLIN_COSTS.items():
        _figure(db, kind, value, "per_month", currency="EUR")

    result = funding.project(
        db,
        _posting(funding_disclosed={**FUNDING, "tax_exempt": False}),
        _candidate(),
        today="2026-09-23",
    )

    assert result.tax_usd_month is None
    assert result.net_savings_usd_month is None
    assert any(item.component == "tax" for item in result.unscored)


# ------------------------------------------------------------------- the floor


def test_the_funding_floor_can_be_set_apart_from_the_salary_floor(
    db: sqlite3.Connection,
) -> None:
    _sourced_world(db)
    settings_module.set_value(db, "savings_floor_usd_month", 2000.0)
    settings_module.set_value(db, "scholarship_funding_floor_usd_month", 100.0)

    result = funding.project(db, _posting(), _candidate(), today="2026-09-23")

    assert result.floor_usd_month == 100.0
    assert result.floor_source == "user_override"
    # This award does not clear even 100 USD: 1,404 of funding against 1,652 of
    # Berlin living costs. The verdict is False, which is a measurement — and a
    # different statement from the None an unresolved projection returns.
    assert _net(result) < 0
    assert result.passes_floor is False

    # A floor the award does clear, to show the comparison is live and not
    # always negative.
    settings_module.set_value(db, "scholarship_funding_floor_usd_month", -500.0)
    assert funding.project(db, _posting(), _candidate(), today="2026-09-23").passes_floor is True


def test_the_arrangement_assumption_is_stated(db: sqlite3.Connection) -> None:
    """A scholarship with no stated arrangement is costed at the institution."""
    _sourced_world(db)

    result = funding.project(db, _posting(), _candidate(), today="2026-09-23")

    assert result.arrangement == "onsite"
    assert result.cost_city == "Berlin"
    assert any("Arrangement assumed onsite" in note for note in result.notes)
