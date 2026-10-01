"""The savings engine — T035.

The behaviour under test is mostly about refusal: what the engine does when a
figure is missing, and whether the number it reports can be followed back to the
sentence it came from.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from jarvis.matching.posting import PostingView
from jarvis.matching.profile_view import CandidateView
from jarvis.money import fx, reference, savings
from jarvis.tests import support

# A complete, sourced world: Dubai on-site, a user who lives and pays tax in PK.
DUBAI_COSTS = {
    "rent": 6000.0,
    "utilities": 700.0,
    "food": 1500.0,
    "transport": 500.0,
    "health_insurance": 400.0,
}


def _posting(**overrides: Any) -> PostingView:
    fields: dict[str, Any] = {
        "id": "opp-1",
        "kind": "job",
        "title": "Industrial Engineer",
        "employer": "Acme",
        "country_iso2": "AE",
        "city": "Dubai",
        "work_arrangement": "onsite",
        "role_family": "industrial_engineering",
        "requirements": {},
        "requirements_confidence": 0.9,
        "pay_disclosed": {
            "min": 22000.0,
            "max": 28000.0,
            "currency": "AED",
            "period": "month",
            "raw_text": "AED 22,000 - 28,000 per month",
        },
        "funding_disclosed": None,
        "description_chars": 900,
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
    """A reference figure is keyed to a configured country, so configure it."""
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
    country: str | None = "AE",
    city: str | None = "Dubai",
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


def _rates(conn: sqlite3.Connection) -> None:
    fx.record_rate(
        conn,
        base="AED",
        quote="USD",
        rate=0.2723,
        as_of="2026-09-20",
        source_url="https://example.test/rates",
    )
    fx.record_rate(
        conn,
        base="PKR",
        quote="USD",
        rate=0.00357,
        as_of="2026-09-20",
        source_url="https://example.test/rates",
    )


def _sourced_world(conn: sqlite3.Connection, *, country: str = "AE", city: str = "Dubai") -> None:
    _rates(conn)
    _figure(conn, "tax_rate", 0.0, "fraction", country=country, city=None)
    for kind, value in DUBAI_COSTS.items():
        _figure(conn, kind, value, "per_month", currency="AED", country=country, city=city)


def _floor(conn: sqlite3.Connection, country: str, value: float) -> None:
    _country(conn, country)
    conn.execute(
        "UPDATE country SET default_savings_floor = ?, floor_basis = ?, "
        "floor_source_url = ?, floor_as_of = ? WHERE iso2 = ?",
        (value, "sourced for the test", "https://example.test/floor", "2026-09-01", country),
    )


# ------------------------------------------------------------- the happy path


def test_a_fully_sourced_projection_is_reproducible_by_hand(db: sqlite3.Connection) -> None:
    _sourced_world(db)
    _floor(db, "AE", 2000.0)

    result = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    # 22,000 AED/month (the low end of the range) at 0.2723, tax 0%,
    # costs 9,100 AED/month at the same rate.
    gross = 22000.0 * 0.2723
    costs = sum(DUBAI_COSTS.values()) * 0.2723
    assert result.gross_usd_month == pytest.approx(gross)
    assert result.tax_usd_month == pytest.approx(0.0)
    assert result.cost_usd_month == pytest.approx(costs)
    assert result.net_savings_usd_month == pytest.approx(gross - costs)
    assert result.passes_floor is True
    assert result.pay_basis == "disclosed"
    assert not result.unscored


def test_every_line_cites_a_figure_and_a_rate(db: sqlite3.Connection) -> None:
    """The point of the whole module: no line is a number on its own."""
    _sourced_world(db)
    _floor(db, "AE", 2000.0)

    stored = savings.project(db, _posting(), _candidate(), today="2026-09-23").as_dict()

    for line in stored["lines"]:
        if line["kind"] == "pay":
            # A disclosed salary is the employer's own figure, so it cites the
            # posting rather than a reference figure — but it still cites a rate.
            assert line["reference_figure_id"] is None
            assert line["fx_rate_id"]
            assert line["source_url"].startswith("https://")
            continue
        if line["kind"] == "tax":
            # A rate is dimensionless: there is nothing to convert.
            assert line["reference_figure_id"]
            assert line["fx_rate_id"] is None
            continue
        assert line["reference_figure_id"], f"{line['kind']} cites no figure"
        assert line["fx_rate_id"], f"{line['kind']} cites no FX rate"
        assert line["quote"], f"{line['kind']} carries no quote"

    assert stored["reference_figure_ids"]
    assert stored["fx_rate_ids"]
    assert "USD/month" in stored["arithmetic"]


def test_the_lower_end_of_a_published_range_is_used(db: sqlite3.Connection) -> None:
    """The top of a range is a salary nobody was offered."""
    _sourced_world(db)

    result = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    assert result.gross_usd_month == pytest.approx(22000.0 * 0.2723)


# ------------------------------------------------------------------ refusals


def test_a_missing_cost_line_makes_the_net_unscored_not_smaller(db: sqlite3.Connection) -> None:
    _rates(db)
    _figure(db, "tax_rate", 0.0, "fraction", city=None)
    _figure(db, "rent", 6000.0, "per_month", currency="AED")
    # utilities, food, transport and health insurance are not on file.

    result = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    assert result.net_savings_usd_month is None
    assert result.passes_floor is None
    missing = {item.component for item in result.unscored}
    assert missing == {"cost_utilities", "cost_food", "cost_transport", "cost_health_insurance"}
    assert "makes the saving look bigger" in result.unscored[0].reason
    assert result.arithmetic.startswith("UNSCORED")


def test_a_missing_tax_rate_does_not_become_zero_tax(db: sqlite3.Connection) -> None:
    _rates(db)
    for kind, value in DUBAI_COSTS.items():
        _figure(db, kind, value, "per_month", currency="AED")

    result = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    assert result.tax_usd_month is None
    assert result.net_savings_usd_month is None
    assert any(item.component == "tax" for item in result.unscored)


def test_undisclosed_pay_with_no_wage_statistic_is_unscored(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = savings.project(
        db, _posting(pay_disclosed=None), _candidate(), today="2026-09-23"
    )

    assert result.gross_usd_month is None
    assert result.net_savings_usd_month is None
    assert result.pay_basis is None
    assert any("invented salary" in item.reason for item in result.unscored)


def test_pay_with_no_currency_is_unscored(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = savings.project(
        db,
        _posting(pay_disclosed={"min": 22000.0, "max": None, "currency": None, "period": "month"}),
        _candidate(),
        today="2026-09-23",
    )

    assert result.gross_usd_month is None
    assert any("different offer in every currency" in item.reason for item in result.unscored)


def test_pay_with_an_unreadable_period_is_unscored(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = savings.project(
        db,
        _posting(
            pay_disclosed={
                "min": 22000.0,
                "max": None,
                "currency": "AED",
                "period": "per fortnight",
            }
        ),
        _candidate(),
        today="2026-09-23",
    )

    assert result.gross_usd_month is None
    assert any("twelvefold" in item.reason for item in result.unscored)


def test_a_missing_fx_rate_is_unscored_not_unconverted(db: sqlite3.Connection) -> None:
    """The conversion failing must not leave an AED figure labelled USD."""
    _figure(db, "tax_rate", 0.0, "fraction", city=None)
    for kind, value in DUBAI_COSTS.items():
        _figure(db, kind, value, "per_month", currency="AED")

    result = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    assert result.gross_usd_month is None
    assert any("no stored FX rate" in item.reason for item in result.unscored)


def test_an_unreadable_work_arrangement_projects_nothing(db: sqlite3.Connection) -> None:
    """11.6% of the corpus. It is not assumed to be on-site."""
    _sourced_world(db)

    result = savings.project(
        db, _posting(work_arrangement=None), _candidate(), today="2026-09-23"
    )

    assert result.net_savings_usd_month is None
    assert any(item.component == "work_arrangement" for item in result.unscored)


# -------------------------------------------------------------------- remote


def test_a_remote_role_is_taxed_and_costed_where_the_user_lives(db: sqlite3.Connection) -> None:
    _rates(db)
    _figure(db, "tax_rate", 20.0, "percent", country="PK", city=None)
    for kind, value in {
        "rent": 60000.0,
        "utilities": 12000.0,
        "food": 30000.0,
        "transport": 8000.0,
        "health_insurance": 5000.0,
    }.items():
        _figure(db, kind, value, "per_month", currency="PKR", country="PK", city="Lahore")
    # The employer's own country has figures too; they must not be the ones used.
    _figure(db, "tax_rate", 0.0, "fraction", country="AE", city=None)

    result = savings.project(
        db, _posting(work_arrangement="remote"), _candidate(), today="2026-09-23"
    )

    assert result.tax_country == "PK"
    assert result.cost_country == "PK"
    assert result.cost_city == "Lahore"
    gross = 22000.0 * 0.2723
    assert result.tax_usd_month == pytest.approx(gross * 0.20)
    assert result.net_savings_usd_month is not None


def test_remote_with_no_confirmed_tax_residence_is_unscored(db: sqlite3.Connection) -> None:
    """Never inferred from citizenship, never from an address."""
    _sourced_world(db)

    result = savings.project(
        db,
        _posting(work_arrangement="remote"),
        _candidate(
            tax_residence=None,
            unknowns={"tax_residence": "no confirmed tax residence, which is what decides"},
        ),
        today="2026-09-23",
    )

    assert result.tax_country is None
    assert result.net_savings_usd_month is None
    assert any(item.component == "tax_residence" for item in result.unscored)


# ---------------------------------------------------------------- estimates


def test_an_estimated_salary_is_labelled_and_ranks_below_a_disclosed_one(
    db: sqlite3.Connection,
) -> None:
    _sourced_world(db)
    _figure(
        db,
        "wage_stat",
        360000.0,
        "per_year",
        currency="AED",
        city=None,
        occupation="industrial_engineering",
    )

    estimated = savings.project(
        db, _posting(pay_disclosed=None), _candidate(), today="2026-09-23"
    )
    disclosed = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    assert estimated.pay_basis == "estimate"
    assert estimated.gross_usd_month == pytest.approx(360000.0 / 12 * 0.2723)
    assert any("ranks below" in note for note in estimated.notes)

    # The estimate is the larger number and still sorts below the disclosed one.
    assert estimated.gross_usd_month > disclosed.gross_usd_month
    assert sorted([estimated, disclosed], key=lambda p: p.sort_key)[-1] is disclosed


def test_an_hourly_rate_carries_its_full_time_assumption(db: sqlite3.Connection) -> None:
    _sourced_world(db)

    result = savings.project(
        db,
        _posting(
            pay_disclosed={"min": 150.0, "max": None, "currency": "AED", "period": "hour"}
        ),
        _candidate(),
        today="2026-09-23",
    )

    pay_line = next(line for line in result.lines if line.kind == "pay")
    assert pay_line.assumption is not None
    assert "full-time" in pay_line.assumption
    assert result.confidence == "low"


# ------------------------------------------------------- specificity, floors


def test_a_national_figure_standing_in_for_a_city_says_so(db: sqlite3.Connection) -> None:
    """A country is not a rent — but a labelled national average is not a lie either."""
    _rates(db)
    _figure(db, "tax_rate", 0.0, "fraction", city=None)
    for kind, value in DUBAI_COSTS.items():
        _figure(db, kind, value, "per_month", currency="AED", city=None)

    result = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    rent = next(line for line in result.lines if line.kind == "rent")
    assert rent.granularity == "country"
    assert "a country is not a rent" in (rent.assumption or "")
    assert result.net_savings_usd_month is not None
    assert result.confidence == "low"


def test_a_city_figure_is_preferred_over_the_national_one(db: sqlite3.Connection) -> None:
    _sourced_world(db)
    national = _figure(db, "rent", 3000.0, "per_month", currency="AED", city=None)

    result = savings.project(db, _posting(), _candidate(), today="2026-09-23")

    rent = next(line for line in result.lines if line.kind == "rent")
    assert rent.reference_figure_id != national
    assert rent.granularity == "city"


def test_an_unsourced_country_floor_reports_rather_than_inheriting_2000(
    db: sqlite3.Connection,
) -> None:
    """Pakistan's case: the projection is a number, the verdict is not."""
    _rates(db)
    _figure(db, "tax_rate", 20.0, "percent", country="PK", city=None)
    for kind, value in DUBAI_COSTS.items():
        _figure(db, kind, value * 10, "per_month", currency="PKR", country="PK", city="Lahore")
    _country(db, "PK")

    result = savings.project(
        db,
        _posting(country_iso2="PK", city="Lahore", pay_disclosed=None),
        _candidate(),
        today="2026-09-23",
    )

    assert result.floor_usd_month is None
    assert result.passes_floor is None


def test_a_superseded_figure_is_not_read(db: sqlite3.Connection) -> None:
    _rates(db)
    old = _figure(db, "rent", 6000.0, "per_month", currency="AED")
    support.sourced_figure(
        db,
        kind="rent",
        value=6500.0,
        unit="per_month",
        currency="AED",
        country_iso2="AE",
        city="Dubai",
        as_of="2026-09-15",
        quote="rent is now 6500 AED",
        supersedes=old,
    )

    current = reference.current(db, "rent", country_iso2="AE", city="Dubai")

    assert current is not None
    assert current.id != old
    assert current.value == 6500.0
