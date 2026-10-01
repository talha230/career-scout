"""Country isolation — T028, invariant I-24.

**Enabling a country leaves every other country's stored output byte-identical.**

This is the property that makes a country pack a pack rather than a global tweak.
Without it, switching Italy on could move a German posting's score, and nobody
would notice until an application went out on numbers that had quietly changed.

Two details decide whether the test means anything:

**The projection is canonicalised, and the fields excluded are excluded by
name.** ``computed_at``, ``run_id`` and ``fetched_at`` differ between two runs of
identical work, so they are dropped — explicitly, one name at a time. A test that
dropped "anything that looks like a timestamp" would also drop ``as_of``, and
``as_of`` is exactly the sort of field a bug would change.

**The fixtures are frozen.** One posting per country, the same clock, the same
sourced figures, so the only variable between the two passes is which packs are
on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

import pytest

from jarvis.discovery import countries
from jarvis.discovery import requirements as requirements_module
from jarvis.matching import screen
from jarvis.money import fx
from jarvis.profile import records
from jarvis.tests import support

TODAY = date(2026, 9, 23)

#: Excluded from the comparison by name, and only these. Each one legitimately
#: differs between two runs of identical work.
VOLATILE_FIELDS = frozenset({"computed_at", "run_id", "fetched_at"})

#: One frozen posting per country: (code, city, currency, monthly pay, rate).
FIXTURES = (
    ("US", "Austin", "USD", 7000.0, 1.0),
    ("GB", "London", "GBP", 4200.0, 1.27),
    ("DE", "Berlin", "EUR", 4800.0, 1.08),
    ("AU", "Sydney", "AUD", 8000.0, 0.66),
    ("PK", "Lahore", "PKR", 450000.0, 0.00357),
    ("IT", "Milan", "EUR", 3000.0, 1.08),
)

COST_SHARE = {
    "rent": 0.30,
    "utilities": 0.05,
    "food": 0.10,
    "transport": 0.03,
    "health_insurance": 0.02,
}

POSTING_TEXT = (
    "Industrial Engineer. Requirements: a bachelor's degree in industrial or "
    "mechanical engineering, five years of manufacturing experience, lean "
    "manufacturing, Six Sigma, AutoCAD and strong Excel. You will run time "
    "studies, improve line balancing and report OEE weekly. Send your CV."
)


def _profile(conn: sqlite3.Connection) -> None:
    records.add(conn, "identity.citizenship", "PK", confirmed=True)
    records.add(conn, "identity.tax_residence", "PK", confirmed=True)
    records.add(conn, "basics.location.countryCode", "PK", confirmed=True)
    records.add(conn, "basics.location.city", "Lahore", confirmed=True)
    records.add(conn, "languages[0].language", "English", confirmed=True)
    records.add(conn, "education[0].studyType", "B.Sc.", confirmed=True)
    records.add(conn, "skills[0].name", "Lean Manufacturing", confirmed=True)
    records.add(conn, "skills[1].name", "Six Sigma", confirmed=True)
    records.add(conn, "work[0].startDate", "2019-01-01", confirmed=True)
    records.add(conn, "work[0].endDate", "2024-01-01", confirmed=True)


def _world(conn: sqlite3.Connection) -> None:
    """Sourced figures and rates for every fixture country, enabled or not."""
    recorded: set[str] = set()
    for code, city, currency, pay, rate in FIXTURES:
        # DE and IT share EUR. One pair, one date, one rate: the UNIQUE index
        # refuses a second, which is the behaviour, not an obstacle.
        if currency != "USD" and currency not in recorded:
            recorded.add(currency)
            fx.record_rate(
                conn,
                base=currency,
                quote="USD",
                rate=rate,
                as_of="2026-09-20",
                source_url=f"https://example.test/rates/{currency}",
            )
        support.sourced_figure(
            conn,
            kind="tax_rate",
            value=25.0,
            unit="percent",
            country_iso2=code,
            quote=f"The effective rate in {code} is 25 percent.",
        )
        for kind, share in COST_SHARE.items():
            support.sourced_figure(
                conn,
                kind=kind,
                value=round(pay * share, 2),
                unit="per_month",
                currency=currency,
                country_iso2=code,
                city=city,
                quote=f"Average {kind} in {city} is {currency} {pay * share:,.2f} per month.",
            )


def _postings(conn: sqlite3.Connection) -> None:
    parsed = requirements_module.parse(POSTING_TEXT, title="Industrial Engineer")
    for code, city, currency, pay, _rate in FIXTURES:
        conn.execute(
            "INSERT INTO opportunity (id, kind, country_iso2, city, work_arrangement, "
            "title, employer, role_family, requirements, requirements_confidence, "
            "description, pay_disclosed, source_url, fetched_at, posted_at, dedupe_key, "
            "employer_norm, title_norm, first_seen_at, last_seen_at) "
            "VALUES (?, 'job', ?, ?, 'onsite', 'Industrial Engineer', 'Acme', "
            "'industrial_engineering', ?, ?, ?, ?, ?, '2026-09-20T00:00:00Z', "
            "'2026-09-18T00:00:00Z', ?, 'acme', 'industrial engineer', "
            "'2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')",
            (
                f"opp-{code}",
                code,
                city,
                json.dumps(parsed.as_dict()),
                max(parsed.confidence, 0.9),
                POSTING_TEXT,
                json.dumps(
                    {
                        "min": pay,
                        "max": pay * 1.2,
                        "currency": currency,
                        "period": "month",
                        "raw_text": f"{currency} {pay:,.0f} per month",
                    }
                ),
                f"https://example.test/jobs/{code}",
                f"key-{code}",
            ),
        )


def _canonical(value: Any) -> Any:
    """Drop the named volatile fields, everywhere, at any depth."""
    if isinstance(value, dict):
        return {
            key: _canonical(inner)
            for key, inner in sorted(value.items())
            if key not in VOLATILE_FIELDS
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _projection(conn: sqlite3.Connection, code: str) -> str:
    """One country's whole assessment, canonicalised to comparable bytes."""
    result = screen(conn, f"opp-{code}", as_of=TODAY)
    if result.rejection is not None:
        return json.dumps({"rejected": result.rejection.as_dict()}, sort_keys=True)

    assert result.assessment is not None
    return json.dumps(_canonical(result.assessment.as_row()), sort_keys=True)


@pytest.fixture
def world(db: sqlite3.Connection) -> sqlite3.Connection:
    countries.install(db)
    _profile(db)
    _world(db)
    _postings(db)
    return db


# ------------------------------------------------------------------- I-24


def test_country_isolation(world: sqlite3.Connection) -> None:
    """Switching Italy on changes nothing about the other five."""
    others = [code for code, *_ in FIXTURES if code != "IT"]
    before = {code: _projection(world, code) for code in others}

    countries.set_enabled(world, "IT", enabled=True)

    after = {code: _projection(world, code) for code in others}

    for code in others:
        assert after[code] == before[code], (
            f"enabling IT changed {code}'s stored assessment. A country pack has to be "
            f"a pack: if one country's numbers move when another is switched on, no "
            f"stored score means what it said it meant."
        )


def test_disabling_a_country_changes_nothing_about_the_others(
    world: sqlite3.Connection,
) -> None:
    """The same property in the other direction, which is the one upgrades hit."""
    others = [code for code, *_ in FIXTURES if code not in {"IT", "AU"}]
    before = {code: _projection(world, code) for code in others}

    countries.set_enabled(world, "AU", enabled=False)

    assert {code: _projection(world, code) for code in others} == before


def test_a_disabled_country_still_resolves_and_still_scores(
    world: sqlite3.Connection,
) -> None:
    """Enablement decides what is searched, never how a posting is read.

    The Italy posting is in the database — a disabled pack does not make a stored
    row unreadable — and it scores and projects like any other. What being off
    means is that discovery never fetches it in the first place.
    """
    assert "IT" not in countries.enabled_codes(world)

    result = screen(world, "opp-IT", as_of=TODAY)

    assert result.assessment is not None
    assert result.assessment.projection is not None
    assert result.assessment.projection.cost_country == "IT"
    assert result.assessment.projection.net_savings_usd_month is not None


def test_the_comparison_would_notice_a_real_change(world: sqlite3.Connection) -> None:
    """A test that cannot fail proves nothing: change a figure, see it move."""
    before = _projection(world, "DE")

    support.sourced_figure(
        world,
        kind="rent",
        value=9999.0,
        unit="per_month",
        currency="EUR",
        country_iso2="DE",
        city="Berlin",
        as_of="2026-09-22",
        quote="Average rent in Berlin is EUR 9,999.00 per month.",
    )

    assert _projection(world, "DE") != before


def test_volatile_fields_are_excluded_by_name_not_by_shape() -> None:
    """``as_of`` must survive canonicalisation: a bug that moved it has to show."""
    sample = {
        "computed_at": "2026-09-23T00:00:00Z",
        "run_id": "run-1",
        "lines": [{"as_of": "2026-09-01", "fetched_at": "2026-09-20T00:00:00Z"}],
    }

    canonical = _canonical(sample)

    assert canonical == {"lines": [{"as_of": "2026-09-01"}]}
    assert {"computed_at", "run_id", "fetched_at"} == VOLATILE_FIELDS
