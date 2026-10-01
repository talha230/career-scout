"""The savings projection reaches the assessment row — T035.

Scoring and projecting are two different questions about one posting: how well
it fits, and what it leaves in the bank. This is about the join between them —
that the projection is computed where the database is available, that its
columns land on the row, and that an unreadable posting gets no projection at
all rather than a confident-looking number.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from career_scout.discovery import requirements as requirements_module
from career_scout.matching import score
from career_scout.money import fx
from career_scout.profile import records
from career_scout.tests import support

POSTING_TEXT = (
    "We are hiring an Industrial Engineer in Dubai. Requirements: a bachelor's "
    "degree in industrial or mechanical engineering, five years of manufacturing "
    "experience, lean manufacturing and Six Sigma, AutoCAD, and strong Excel. "
    "You will run time studies, improve line balancing and report OEE weekly. "
    "Send your CV and a cover letter."
)


def _profile(conn: sqlite3.Connection) -> None:
    records.add(conn, "identity.citizenship", "PK", confirmed=True)
    records.add(conn, "identity.tax_residence", "PK", confirmed=True)
    records.add(conn, "basics.location.countryCode", "PK", confirmed=True)
    records.add(conn, "basics.location.city", "Lahore", confirmed=True)
    records.add(conn, "skills[0].name", "Lean Manufacturing", confirmed=True)
    records.add(conn, "work[0].startDate", "2019-01-01", confirmed=True)
    records.add(conn, "work[0].endDate", "2024-01-01", confirmed=True)
    records.add(conn, "education[0].studyType", "B.Sc.", confirmed=True)


def _opportunity(
    conn: sqlite3.Connection,
    *,
    opportunity_id: str = "opp-1",
    text: str = POSTING_TEXT,
    confidence: float | None = None,
    pay: dict[str, object] | None = None,
) -> None:
    conn.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES ('AE', 'UAE', 1)")
    parsed = requirements_module.parse(text, title="Industrial Engineer")
    conn.execute(
        "INSERT INTO opportunity (id, kind, country_iso2, city, work_arrangement, title, "
        "employer, role_family, requirements, requirements_confidence, description, "
        "pay_disclosed, source_url, fetched_at, dedupe_key, employer_norm, title_norm, "
        "first_seen_at, last_seen_at) "
        "VALUES (?, 'job', 'AE', 'Dubai', 'onsite', 'Industrial Engineer', 'Acme', "
        "'industrial_engineering', ?, ?, ?, ?, 'https://example.test/1', "
        "'2026-09-20T00:00:00Z', ?, 'acme', 'industrial engineer', "
        "'2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')",
        (
            opportunity_id,
            json.dumps(parsed.as_dict()),
            confidence if confidence is not None else max(parsed.confidence, 0.9),
            text,
            json.dumps(pay) if pay else None,
            f"key-{opportunity_id}",
        ),
    )


def _sourced_world(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES ('AE', 'UAE', 1)")
    fx.record_rate(
        conn,
        base="AED",
        quote="USD",
        rate=0.2723,
        as_of="2026-09-20",
        source_url="https://example.test/rates",
    )
    support.sourced_figure(
        conn,
        kind="tax_rate",
        value=0.0,
        unit="fraction",
        country_iso2="AE",
        quote="there is no personal income tax",
    )
    for kind, value in {
        "rent": 6000.0,
        "utilities": 700.0,
        "food": 1500.0,
        "transport": 500.0,
        "health_insurance": 400.0,
    }.items():
        support.sourced_figure(
            conn,
            kind=kind,
            value=value,
            unit="per_month",
            currency="AED",
            country_iso2="AE",
            city="Dubai",
            quote=f"{kind} averages {value} AED a month",
        )


PAY = {
    "min": 22000.0,
    "max": 28000.0,
    "currency": "AED",
    "period": "month",
    "raw_text": "AED 22,000 - 28,000 per month",
}


def test_scoring_a_posting_attaches_its_projection(db: sqlite3.Connection) -> None:
    _profile(db)
    _sourced_world(db)
    _opportunity(db, pay=PAY)

    result = score(db, "opp-1", as_of=date(2026, 9, 23))

    assert result.projection is not None
    assert result.projection.net_savings_usd_month == pytest.approx(
        (22000.0 - 9100.0) * 0.2723
    )


def test_the_projection_columns_reach_the_row(db: sqlite3.Connection) -> None:
    _profile(db)
    _sourced_world(db)
    _opportunity(db, pay=PAY)

    row = score(db, "opp-1", as_of=date(2026, 9, 23)).as_row()

    assert row["pay_basis"] == "disclosed"
    # AE defines no default floor and is not on countries_requiring_own_floor,
    # so the global fallback applies and says so (T041).
    assert row["floor_source"] == "fallback"
    assert row["floor_applied"] == 2000.0
    assert row["passes_floor"] is True
    assert row["projection"]["lines"]
    assert row["projection"]["fx_rate_ids"]
    assert row["projection"]["reference_figure_ids"]
    # The row has to survive the trip to SQLite as JSON.
    assert json.loads(json.dumps(row["projection"]))["arithmetic"].endswith("USD/month")


def test_the_row_is_storable(db: sqlite3.Connection) -> None:
    """Every projection column is one the schema accepts."""
    _profile(db)
    _sourced_world(db)
    _opportunity(db, pay=PAY)

    row = score(db, "opp-1", as_of=date(2026, 9, 23)).as_row()
    db.execute(
        "INSERT INTO assessment (opportunity_id, config_version, match_score, confidence, "
        "formula, weights, inputs, unscored_components, projection, passes_floor, "
        "floor_applied, floor_source, pay_basis, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-09-23T00:00:00Z')",
        (
            row["opportunity_id"],
            row["config_version"],
            row["match_score"],
            row["confidence"],
            row["formula"],
            json.dumps(row["weights"]),
            json.dumps(row["inputs"]),
            json.dumps(row["unscored_components"]),
            json.dumps(row["projection"]),
            row["passes_floor"],
            row["floor_applied"],
            row["floor_source"],
            row["pay_basis"],
        ),
    )

    stored = db.execute("SELECT projection, pay_basis FROM assessment").fetchone()
    assert json.loads(stored["projection"])["lines"]
    assert stored["pay_basis"] == "disclosed"


def test_an_unreadable_posting_is_not_projected(db: sqlite3.Connection) -> None:
    """The one number on the screen that looked solid is the one to refuse."""
    _profile(db)
    _sourced_world(db)
    _opportunity(db, text="Oops something happened", confidence=0.25, pay=PAY)

    result = score(db, "opp-1", as_of=date(2026, 9, 23))

    assert result.score.tier == "UNSCORED"
    assert result.projection is None
    assert "projection" not in result.as_row()


def test_a_posting_with_nothing_sourced_projects_unscored_with_reasons(
    db: sqlite3.Connection,
) -> None:
    """Today's real state for most countries: readable posting, no figures on file."""
    _profile(db)
    _opportunity(db, pay=PAY)

    result = score(db, "opp-1", as_of=date(2026, 9, 23))

    assert result.projection is not None
    assert result.projection.net_savings_usd_month is None
    assert result.as_row()["passes_floor"] is None
    reasons = {item.component for item in result.projection.unscored}
    assert "pay" in reasons  # no AED->USD rate on file
    assert result.projection.confidence == "low"


def test_the_projection_capability_reports_what_is_missing(db: sqlite3.Connection) -> None:
    """An empty figure table is reported, not papered over with an estimate."""
    from career_scout import service

    before = service.capability_status(db)["financial_projection"]
    assert before["available"] is False
    assert "exchange rates" in before["reason"]
    assert "cost-of-living figures" in before["reason"]
    assert "effective tax rates" in before["reason"]

    _sourced_world(db)

    after = service.capability_status(db)["financial_projection"]
    assert after["available"] is True
    assert after["reason"] is None


def test_a_scholarship_is_projected_by_the_funding_engine(db: sqlite3.Connection) -> None:
    """One Projection shape, two engines: the row and the screen do not branch."""
    _profile(db)
    _sourced_world(db)
    _opportunity(db)
    db.execute(
        "UPDATE opportunity SET kind = 'scholarship', funding_disclosed = ? WHERE id = 'opp-1'",
        (
            json.dumps(
                {
                    "stipend": {"amount": 4000.0, "currency": "AED", "period": "month"},
                    "tuition_waived": True,
                    "work_allowed": False,
                    "tax_exempt": True,
                    "source_url": "https://example.test/award",
                    "quote": "AED 4,000 a month, tuition waived",
                }
            ),
        ),
    )

    result = score(db, "opp-1", as_of=date(2026, 9, 23))

    assert result.projection is not None
    kinds = {line.kind for line in result.projection.lines}
    assert "stipend" in kinds
    assert "pay" not in kinds
    assert result.projection.gross_usd_month == pytest.approx(4000.0 * 0.2723)
