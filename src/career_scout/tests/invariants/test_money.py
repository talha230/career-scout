"""Currency conversion — I-20.

A missing FX rate raises. There is no nearest-date fallback, no triangulation
through a third currency, and no "close enough" rate, because every one of
those puts a number the user cannot check into a savings projection that
decides whether they apply for a job.
"""

from __future__ import annotations

import sqlite3

import pytest

from career_scout.money import fx
from career_scout.store import settings as settings_module


def _rate(conn: sqlite3.Connection, base: str, quote: str, rate: float, as_of: str) -> str:
    return fx.record_rate(
        conn,
        base=base,
        quote=quote,
        rate=rate,
        as_of=as_of,
        source_url="https://example.test/rates",
    )


# ------------------------------------------------------------------ I-20


def test_missing_fx_rate_raises(db: sqlite3.Connection) -> None:
    """An unknown pair raises rather than returning the amount unconverted."""
    with pytest.raises(fx.MissingRate) as caught:
        fx.convert(db, 1000.0, base="PKR", quote="USD", as_of="2026-09-01")

    message = str(caught.value)
    assert "PKR" in message and "USD" in message
    assert "2026-09-01" in message


def test_no_nearest_date_fallback(db: sqlite3.Connection) -> None:
    """A rate one day off is not the rate asked for."""
    _rate(db, "EUR", "USD", 1.08, "2026-08-31")

    with pytest.raises(fx.MissingRate):
        fx.convert(db, 100.0, base="EUR", quote="USD", as_of="2026-09-01")


def test_no_triangulation_through_a_third_currency(db: sqlite3.Connection) -> None:
    """EUR->USD and USD->PKR do not silently become EUR->PKR."""
    _rate(db, "EUR", "USD", 1.08, "2026-09-01")
    _rate(db, "USD", "PKR", 278.0, "2026-09-01")

    with pytest.raises(fx.MissingRate):
        fx.convert(db, 100.0, base="EUR", quote="PKR", as_of="2026-09-01")


def test_conversion_is_reproducible_by_hand(db: sqlite3.Connection) -> None:
    rate_id = _rate(db, "EUR", "USD", 1.08, "2026-09-01")

    converted = fx.convert(db, 2500.0, base="EUR", quote="USD", as_of="2026-09-01")

    assert converted.amount == pytest.approx(2500.0 * 1.08)
    assert converted.rate == 1.08
    assert converted.fx_rate_id == rate_id
    assert converted.inverted is False
    assert "2500" in converted.arithmetic and "1.08" in converted.arithmetic


def test_inverse_rate_is_used_but_labelled(db: sqlite3.Connection) -> None:
    """One stored direction serves both, and says which row it read."""
    rate_id = _rate(db, "USD", "PKR", 278.0, "2026-09-01")

    converted = fx.convert(db, 278000.0, base="PKR", quote="USD", as_of="2026-09-01")

    assert converted.amount == pytest.approx(1000.0)
    assert converted.fx_rate_id == rate_id
    assert converted.inverted is True
    assert "1 / 278.0" in converted.arithmetic


def test_identity_conversion_needs_no_rate_and_says_so(db: sqlite3.Connection) -> None:
    converted = fx.convert(db, 1234.5, base="usd", quote="USD", as_of="2026-09-01")

    assert converted.amount == 1234.5
    assert converted.rate == 1.0
    assert converted.fx_rate_id is None
    assert converted.identity is True


def test_unknown_currency_is_refused_not_treated_as_identity(db: sqlite3.Connection) -> None:
    """A posting with no currency must not convert as if it were the user's own."""
    with pytest.raises(ValueError, match="currency"):
        fx.convert(db, 100.0, base=None, quote="USD", as_of="2026-09-01")  # type: ignore[arg-type]


# -------------------------------------------------------- staleness, settings


def test_current_rate_reports_its_own_date(db: sqlite3.Connection) -> None:
    _rate(db, "EUR", "USD", 1.05, "2026-08-01")
    newest = _rate(db, "EUR", "USD", 1.08, "2026-09-20")

    found = fx.current_rate(db, base="EUR", quote="USD", today="2026-09-23")

    assert found.fx_rate_id == newest
    assert found.as_of == "2026-09-20"


def test_stale_rate_raises_rather_than_quietly_ageing(db: sqlite3.Connection) -> None:
    _rate(db, "EUR", "USD", 1.08, "2026-01-01")

    with pytest.raises(fx.StaleRate, match="265 days"):
        fx.current_rate(db, base="EUR", quote="USD", today="2026-09-23")


def test_staleness_window_comes_from_settings(db: sqlite3.Connection) -> None:
    """I-23: the window is a setting, so no threshold hides in a code path."""
    assert "fx_rate_max_age_days" in settings_module.DEFAULTS

    _rate(db, "EUR", "USD", 1.08, "2026-01-01")
    settings_module.set_value(db, "fx_rate_max_age_days", 400)

    found = fx.current_rate(db, base="EUR", quote="USD", today="2026-09-23")
    assert found.rate == 1.08


def test_recording_the_same_pair_and_date_twice_supersedes_nothing_silently(
    db: sqlite3.Connection,
) -> None:
    """The UNIQUE index is the point: one pair, one date, one rate."""
    _rate(db, "EUR", "USD", 1.08, "2026-09-01")

    with pytest.raises(sqlite3.IntegrityError):
        _rate(db, "EUR", "USD", 1.09, "2026-09-01")
