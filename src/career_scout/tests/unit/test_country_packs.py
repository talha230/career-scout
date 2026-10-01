"""Country packs — T031."""

from __future__ import annotations

import sqlite3

import pytest

from career_scout import config as config_module
from career_scout.discovery import countries
from career_scout.store import settings as settings_module

ENABLED = {"PK", "US", "GB", "DE", "AU"}
DISABLED = {"IT", "DK", "SE"}


def test_the_five_named_countries_are_on_and_the_three_are_off() -> None:
    packs = countries.packs()

    assert {code for code, pack in packs.items() if pack.enabled} == ENABLED
    assert {code for code, pack in packs.items() if not pack.enabled} == DISABLED


def test_every_pack_is_valid() -> None:
    assert countries.validate() == []


def test_a_pack_may_only_name_a_registered_source() -> None:
    """A pack naming an unknown source reports coverage nothing can fetch."""
    for pack in countries.packs().values():
        for name in pack.sources:
            assert name in countries.registered_sources(), f"{pack.code} names {name}"


def test_no_pack_claims_a_floor_it_cannot_source() -> None:
    """All five ship null. A plausible number here decides shortlists."""
    for pack in countries.packs().values():
        assert pack.default_savings_floor is None
        assert pack.floor_basis is None


def test_installing_packs_fills_the_country_table(db: sqlite3.Connection) -> None:
    installed = countries.install(db)

    assert set(installed) >= ENABLED | DISABLED
    assert set(countries.enabled_codes(db)) == ENABLED

    row = db.execute("SELECT * FROM country WHERE iso2 = 'DE'").fetchone()
    assert row["name"] == "Germany"
    assert row["currency"] == "EUR"
    assert row["default_savings_floor"] is None


def test_installing_twice_changes_nothing(db: sqlite3.Connection) -> None:
    countries.install(db)
    before = db.execute("SELECT * FROM country ORDER BY iso2").fetchall()

    countries.install(db)
    after = db.execute("SELECT * FROM country ORDER BY iso2").fetchall()

    assert [tuple(row) for row in before] == [tuple(row) for row in after]


def test_reinstalling_does_not_re_enable_what_the_user_switched_off(
    db: sqlite3.Connection,
) -> None:
    """An upgrade must not undo a decision somebody made deliberately."""
    countries.install(db)
    countries.set_enabled(db, "AU", enabled=False)

    countries.install(db)

    assert "AU" not in countries.enabled_codes(db)


def test_enabling_a_country_without_a_pack_is_refused(db: sqlite3.Connection) -> None:
    countries.install(db)

    with pytest.raises(countries.UnknownCountry):
        countries.set_enabled(db, "FR", enabled=True)


def test_a_floor_without_a_basis_is_refused(db: sqlite3.Connection) -> None:
    """The check has to be able to fail. The schema refuses it too."""
    broken = {
        "packs": [
            {
                "code": "XX",
                "name": "Nowhere",
                "enabled": True,
                "sources": ["remoteok"],
                "default_savings_floor": 2500.0,
            }
        ]
    }
    import json

    # Written into the user's own config copy under the temp CAREER_SCOUT_HOME, which
    # takes precedence over the packaged file. Editing the packaged one would
    # leave broken config behind for every later test in the process.
    user_copy = config_module.install_user_copy() / "country_packs.json"
    user_copy.write_text(json.dumps(broken), encoding="utf-8")
    config_module._load_cached.cache_clear()

    problems = countries.validate()
    assert any("no basis" in problem for problem in problems)
    with pytest.raises(countries.PackInvalid):
        countries.install(db)


def test_sources_are_asked_for_per_country(db: sqlite3.Connection) -> None:
    countries.install(db)

    asked = countries.sources_for(db)

    # greenhouse serves four of the five; arbeitnow only Germany.
    assert asked["arbeitnow"] == ("DE",)
    assert set(asked["greenhouse"]) == {"US", "GB", "DE", "AU"}
    assert set(asked["remoteok"]) == ENABLED
    # SE names lever but is off, so lever is asked for by the US, GB and AU packs.
    assert "SE" not in asked["lever"]


def test_a_source_no_enabled_pack_wants_is_absent(db: sqlite3.Connection) -> None:
    countries.install(db)
    for code in ENABLED:
        countries.set_enabled(db, code, enabled=False)
    countries.set_enabled(db, "DE", enabled=True)

    asked = countries.sources_for(db)

    # T031b added the two worldwide-remote boards to every enabled pack.
    assert set(asked) == {"arbeitnow", "greenhouse", "remoteok", "himalayas", "jobicy"}
    assert "lever" not in asked


def test_the_pakistan_floor_still_reports_unsourced(db: sqlite3.Connection) -> None:
    """The pack does not quietly fix U1 by inheriting the global fallback."""
    countries.install(db)

    resolved = settings_module.resolve_savings_floor(db, "PK")

    assert resolved.value is None
    assert resolved.source == "unsourced"
    assert "not yet sourced" in resolved.reason


def test_another_country_falls_through_to_the_labelled_fallback(
    db: sqlite3.Connection,
) -> None:
    countries.install(db)

    resolved = settings_module.resolve_savings_floor(db, "DE")

    assert resolved.value == 2000.0
    assert resolved.source == "fallback"
    assert "global fallback" in resolved.basis


def test_every_resolvable_country_gets_a_reference_row(db: sqlite3.Connection) -> None:
    """``opportunity.country_iso2`` is a foreign key into ``country``.

    The first Careem posting resolves to AE, which has no pack. Without a row the
    insert fails, and the whole source's batch with it.
    """
    countries.install(db)

    known = {c["code"] for c in config_module.load("country_preferences")["countries"]}
    rows = {row["iso2"]: row for row in db.execute("SELECT iso2, enabled FROM country")}

    assert known <= set(rows)
    assert rows["AE"]["enabled"] == 0
    # A reference row is not a pack and carries no floor.
    ae = db.execute("SELECT default_savings_floor FROM country WHERE iso2 = 'AE'").fetchone()
    assert ae["default_savings_floor"] is None


def test_only_a_pack_that_is_off_counts_as_switched_off(db: sqlite3.Connection) -> None:
    """An unpacked country's row is also 0, and that is not a decision."""
    countries.install(db)

    assert countries.switched_off(db) == DISABLED
    assert "AE" not in countries.switched_off(db)

    countries.set_enabled(db, "AU", enabled=False)
    assert "AU" in countries.switched_off(db)
