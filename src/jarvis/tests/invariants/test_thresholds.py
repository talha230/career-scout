"""Thresholds live in settings, never in a code path — T016, invariant I-23.

Two halves, because either one alone passes while the property is broken.

The **structural** half walks every declared threshold and asserts the code
reads it by name somewhere. A threshold that is declared in ``DEFAULTS`` and
then hardcoded at its call site still shows up on the settings screen, still
accepts a new value, and still changes nothing — which is worse than having no
setting at all, because the user is told they are in control and they are not.

The **behavioural** half changes a threshold and asserts the answer moves. It
is the only half that can tell a read from an effective read.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import jarvis
from jarvis.money import fx
from jarvis.profile import functions
from jarvis.store import settings as settings_module

#: Every setting that gates, caps or times something. Not the whole table:
#: ``web_port`` and ``backup_target`` are configuration, not thresholds.
THRESHOLD_KEYS: frozenset[str] = frozenset(
    {
        "savings_floor_usd_month",
        "scholarship_funding_floor_usd_month",
        "fallback_savings_floor_usd_month",
        "countries_requiring_own_floor",
        "fx_rate_max_age_days",
        "cap_per_destination_per_day",
        "cap_total_per_day",
        "min_send_interval_seconds",
        "requirements_confidence_floor",
        "staleness_window_days",
        "run_staleness_alert_hours",
        "backup_keep_daily",
        "backup_keep_weekly",
        "generate_top_n",
        "gmail_poll_minutes",
        "backup_stale_after_hours",
    }
)

#: Thresholds whose consumer is not built yet. Each one names the task that
#: will read it, so this list shrinks and is never a place to hide a miss.
NOT_YET_CONSUMED: dict[str, str] = {}


def _source_files() -> list[Path]:
    root = Path(jarvis.__file__).parent
    return [path for path in root.rglob("*.py") if "tests" not in path.parts]


def _source_text_outside_defaults() -> str:
    """Every source line except the ``DEFAULTS`` table that declares the keys.

    ``settings.py`` is included on purpose: a key read by
    :func:`resolve_savings_floor` or :func:`cap_settings` is read by the code,
    and those resolvers are the documented single place each threshold resolves
    through. What must not count as a read is the declaration itself.
    """
    chunks: list[str] = []
    for path in _source_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        if path.name == "settings.py":
            start = next(i for i, line in enumerate(lines) if line.startswith("DEFAULTS"))
            end = next(i for i, line in enumerate(lines[start:], start=start) if line == "}")
            lines = lines[:start] + lines[end + 1 :]
        chunks.append("\n".join(lines))
    return "\n".join(chunks)


def test_every_declared_threshold_is_actually_read() -> None:
    """A declared threshold nobody reads is a promise the code does not keep."""
    text = _source_text_outside_defaults()

    unread = {
        key
        for key in THRESHOLD_KEYS
        if key not in NOT_YET_CONSUMED and f'"{key}"' not in text and f"'{key}'" not in text
    }
    assert not unread, (
        f"these thresholds are declared in settings and read nowhere: {sorted(unread)}. "
        f"Either the call site hardcoded the number, or the setting is decoration."
    )


def test_every_threshold_key_is_declared() -> None:
    """The list above cannot drift away from the settings table."""
    missing = THRESHOLD_KEYS - set(settings_module.DEFAULTS)
    assert not missing, f"{sorted(missing)} is checked here but absent from DEFAULTS"


def test_not_yet_consumed_shrinks_rather_than_grows() -> None:
    """A threshold cannot be excused twice: once here and once at its call site."""
    text = _source_text_outside_defaults()
    now_consumed = {key for key in NOT_YET_CONSUMED if f'"{key}"' in text}
    assert not now_consumed, (
        f"{sorted(now_consumed)} is read by the code now; remove it from "
        f"NOT_YET_CONSUMED so the structural check covers it"
    )


def test_no_bare_numeric_comparison_against_a_confidence_floor() -> None:
    """The specific shape that keeps reappearing: ``confidence < 0.35``.

    A tripwire, documented as one. It cannot see every hardcoded threshold, and
    the behavioural tests below are the real guarantee — but this catches the
    copy-paste that reintroduces the literal next to a correct settings read.
    """
    pattern = re.compile(r"confidence\s*[<>]=?\s*0\.\d+")
    offenders = [
        f"{path.name}:{line_no}"
        for path in _source_files()
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not offenders, f"confidence compared against a literal at {offenders}"


# ------------------------------------------------------------- behavioural


def test_requirements_floor_change_moves_the_verdict(db: sqlite3.Connection) -> None:
    """The same posting, two floors, two verdicts — so the setting is effective."""
    posting = {
        "kind": "job",
        "role_family": "industrial_engineering",
        "requirements": {"skills": ["python"], "documents": ["cv"]},
        "requirements_confidence": 0.50,
    }

    settings_module.set_value(db, "requirements_confidence_floor", 0.40)
    assert functions.sufficiency(db, posting).verdict != "not_evaluated"

    settings_module.set_value(db, "requirements_confidence_floor", 0.80)
    blocked = functions.sufficiency(db, posting)
    assert blocked.verdict == "not_evaluated"
    assert "0.80" in blocked.reason


def test_savings_floor_override_beats_the_country_default(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO country (iso2, name, enabled, default_savings_floor, floor_basis, "
        "                     floor_source_url, floor_as_of) "
        "VALUES ('US', 'United States', 1, 1800.0, 'sourced for the test', "
        "        'https://example.test/basis', '2026-09-01')"
    )

    from_country = settings_module.resolve_savings_floor(db, "US")
    assert from_country.value == 1800.0
    assert from_country.source == "country_default"

    settings_module.set_value(db, "savings_floor_usd_month", 2600.0)
    overridden = settings_module.resolve_savings_floor(db, "US")
    assert overridden.value == 2600.0
    assert overridden.source == "user_override"


def test_fx_window_change_moves_the_refusal(db: sqlite3.Connection) -> None:
    fx.record_rate(
        db,
        base="EUR",
        quote="USD",
        rate=1.08,
        as_of="2026-08-01",
        source_url="https://example.test/rates",
    )

    settings_module.set_value(db, "fx_rate_max_age_days", 10)
    with pytest.raises(fx.StaleRate):
        fx.current_rate(db, base="EUR", quote="USD", today="2026-09-23")

    settings_module.set_value(db, "fx_rate_max_age_days", 90)
    assert fx.current_rate(db, base="EUR", quote="USD", today="2026-09-23").rate == 1.08


def test_send_caps_come_from_settings(db: sqlite3.Connection) -> None:
    settings_module.set_value(db, "cap_per_destination_per_day", 3)
    settings_module.set_value(db, "cap_total_per_day", 7)
    settings_module.set_value(db, "min_send_interval_seconds", 99)

    assert settings_module.cap_settings(db) == (3, 7, 99)
