"""Settings, and the floor resolver that must never invent a number."""

from __future__ import annotations

import sqlite3

import pytest

from career_scout.gmail.setup import SCOPE_DRIVE, SCOPE_READ, SCOPE_SEND, required_scopes
from career_scout.store import settings as s


def _country(
    conn: sqlite3.Connection,
    iso2: str,
    *,
    floor: float | None = None,
    enabled: int = 1,
) -> None:
    if floor is None:
        conn.execute(
            "INSERT INTO country (iso2, name, enabled) VALUES (?, ?, ?)",
            (iso2, iso2, enabled),
        )
    else:
        conn.execute(
            "INSERT INTO country (iso2, name, enabled, default_savings_floor, floor_basis, "
            "floor_source_url, floor_as_of) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso2, iso2, enabled, floor, "median wage, source X", "https://x/stats", "2026-01-01"),
        )


# ------------------------------------------------------------------ basics


def test_defaults_are_readable_without_a_stored_row(db: sqlite3.Connection) -> None:
    assert s.get(db, "cap_total_per_day") == 20
    assert s.get(db, "channel_email_autosend") is False


def test_unknown_key_raises_rather_than_returning_none(db: sqlite3.Connection) -> None:
    with pytest.raises(s.UnknownSetting):
        s.get(db, "no_such_setting")


def test_unknown_key_cannot_be_written(db: sqlite3.Connection) -> None:
    """A setting that is not in DEFAULTS is invisible to the settings screen."""
    with pytest.raises(s.UnknownSetting):
        s.set_value(db, "sneaky_limit", 999)


def test_set_then_get_round_trips(db: sqlite3.Connection) -> None:
    s.set_value(db, "cap_total_per_day", 3)
    assert s.get(db, "cap_total_per_day") == 3


def test_every_channel_is_off_by_default(db: sqlite3.Connection) -> None:
    """Opt-in, never opt-out. Nothing sends until the user switches it on."""
    for key in ("channel_email_autosend", "channel_published_api", "channel_outreach"):
        assert s.get(db, key) is False, key


def test_caps_come_from_settings(db: sqlite3.Connection) -> None:
    s.set_value(db, "cap_per_destination_per_day", 2)
    s.set_value(db, "cap_total_per_day", 5)
    s.set_value(db, "min_send_interval_seconds", 60)
    assert s.cap_settings(db) == (2, 5, 60)


# ------------------------------------------------------- floor resolution


def test_user_override_wins(db: sqlite3.Connection) -> None:
    _country(db, "DE", floor=1800.0)
    s.set_value(db, "savings_floor_usd_month", 3000.0)
    resolved = s.resolve_savings_floor(db, "DE")
    assert resolved.value == 3000.0
    assert resolved.source == "user_override"


def test_country_default_carries_its_basis(db: sqlite3.Connection) -> None:
    """A sourced number arrives with its source, or it does not arrive."""
    _country(db, "DE", floor=1800.0)
    resolved = s.resolve_savings_floor(db, "DE")
    assert resolved.value == 1800.0
    assert resolved.source == "country_default"
    assert resolved.basis and resolved.source_url and resolved.as_of


def test_country_without_a_default_falls_back(db: sqlite3.Connection) -> None:
    _country(db, "US")
    resolved = s.resolve_savings_floor(db, "US")
    assert resolved.value == 2000.0
    assert resolved.source == "fallback"


def test_pakistan_reports_unscored_rather_than_inheriting_2000(db: sqlite3.Connection) -> None:
    """The floor stays unresolved until a figure is sourced.

    Applying the global 2,000 to Pakistan produces a permanently empty
    shortlist, and inventing a replacement would be a fabricated number. So the
    resolver returns no value and says why, and the opportunity is reported
    UNSCORED rather than silently filtered out.
    """
    _country(db, "PK")
    resolved = s.resolve_savings_floor(db, "PK")
    assert resolved.value is None
    assert resolved.source == "unsourced"
    assert "not yet sourced" in (resolved.reason or "")


def test_pakistan_uses_its_default_once_one_is_sourced(db: sqlite3.Connection) -> None:
    _country(db, "PK", floor=650.0)
    resolved = s.resolve_savings_floor(db, "PK")
    assert resolved.value == 650.0
    assert resolved.source == "country_default"


def test_countries_requiring_their_own_floor_are_configurable(db: sqlite3.Connection) -> None:
    """Enabling a sixth country is configuration, never a code change."""
    _country(db, "IN")
    assert s.resolve_savings_floor(db, "IN").source == "fallback"
    s.set_value(db, "countries_requiring_own_floor", ["PK", "IN"])
    assert s.resolve_savings_floor(db, "IN").source == "unsourced"


def test_unknown_country_is_unsourced_not_zero(db: sqlite3.Connection) -> None:
    resolved = s.resolve_savings_floor(db, "ZZ")
    assert resolved.value is None
    assert "not configured" in (resolved.reason or "")


# ------------------------------------------------------------- AI is optional


def test_ai_provider_defaults_to_none(db: sqlite3.Connection) -> None:
    """Absent AI is a state, not a fault: the MCP host supplies it when attached."""
    assert s.get(db, "ai_provider") is None


def test_secret_settings_are_masked_in_the_listing(db: sqlite3.Connection) -> None:
    s.set_value(db, "ai_api_key", "sk-do-not-print-me")
    listing = s.all_settings(db)
    assert listing["ai_api_key"] == "***set***"
    assert "sk-do-not-print-me" not in str(listing)


# ------------------------------------------------------------ google scopes


def test_send_scope_is_not_requested_until_autosend_is_on(db: sqlite3.Connection) -> None:
    """A user who never switches auto-send on never grants the ability to send as them."""
    scopes = required_scopes(db)
    assert SCOPE_READ in scopes
    assert SCOPE_DRIVE in scopes
    assert SCOPE_SEND not in scopes


def test_send_scope_appears_once_autosend_is_on(db: sqlite3.Connection) -> None:
    s.set_value(db, "channel_email_autosend", True)
    assert SCOPE_SEND in required_scopes(db)


def test_no_broad_drive_or_mail_scope_is_ever_requested(db: sqlite3.Connection) -> None:
    """drive.file only. The broad scopes are restricted and unnecessary here."""
    s.set_value(db, "channel_email_autosend", True)
    scopes = set(required_scopes(db))
    forbidden = {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
    }
    assert not (scopes & forbidden)


def test_run_staleness_alert_exists(db: sqlite3.Connection) -> None:
    """A run that never happens produces no error of its own, so it needs a threshold."""
    assert s.get(db, "run_staleness_alert_hours") > 0
