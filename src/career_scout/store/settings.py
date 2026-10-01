"""Settings: everything a user can change, and every number code must not hardcode.

Invariant I-23 asserts that no threshold, cap or interval is a constant in a
code path. They all resolve through :func:`get`, which falls back to
:data:`DEFAULTS` — a table that is itself data, readable in one place and
displayed to the user as a changeable default rather than a fixed rule.

The savings floor is resolved in three steps, and the third one matters:

1. the user's own override, if they set one
2. the enabled country's default, **with the basis it was derived from**
3. ``USD 2,000/month``

A country that has no sourced default gets ``None`` at step 2 and the resolver
reports ``UNSCORED — default not yet sourced`` rather than silently applying
step 3. Pakistan is the live case: prevailing wages differ enough that
inheriting 2,000 would produce a permanently empty shortlist, and inventing a
replacement would be a fabricated number.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from career_scout.store.db import utcnow

#: Defaults, applied when the user has not set a value. Data, not logic.
DEFAULTS: dict[str, Any] = {
    # --- financial thresholds
    "savings_floor_usd_month": None,  # None = fall through to the country default
    "scholarship_funding_floor_usd_month": None,
    "fallback_savings_floor_usd_month": 2000.0,
    # Countries where the global fallback must NOT be applied silently, because
    # prevailing wages differ enough that inheriting it produces a permanently
    # empty shortlist. These report UNSCORED until a sourced figure exists.
    "countries_requiring_own_floor": ["PK"],
    # How old a stored FX rate may be before a conversion refuses to use it.
    # A two-year-old rate is not wrong arithmetic; it is a right answer to a
    # question nobody asked, and it silently moves a savings projection across
    # the floor. Widening this is a deliberate act, not a default.
    "fx_rate_max_age_days": 30,
    # --- outbound volume, protecting the user's own mailbox reputation
    "cap_per_destination_per_day": 10,
    "cap_total_per_day": 20,
    "min_send_interval_seconds": 240,
    # --- channels, every one off until switched on
    "channel_email_autosend": False,
    "channel_published_api": False,
    "channel_outreach": False,
    # --- rule-based approval (I-30). Off, and empty, until the user writes the
    # rules. Applications only: replies and outreach always need a person.
    "auto_approve_enabled": False,
    "auto_approve_min_score": None,  # None = no rule approval at all
    "auto_approve_countries": [],    # explicit allow-list; empty = none
    "auto_approve_max_per_day": 3,
    # --- discovery
    "effort_split_jobs": 0.8,
    "effort_split_scholarships": 0.2,
    "staleness_window_days": 45,
    "discovery_schedule_hour": 7,
    # --- sufficiency
    # Below this, a posting's parsed requirements are not trusted enough to
    # authorise an application. The verdict becomes 'not_evaluated', which is
    # not 'sufficient'. Measured basis: 481 of 2,305 legacy assessments found
    # no vocabulary match in the posting at all.
    "requirements_confidence_floor": 0.35,
    # --- profile
    "completeness_definition_version": 1,
    "sufficiency_rule_version": 1,
    # --- documents
    # The account's CV presentation variant (T043b), derived once from the
    # account id and then stored, so a retuned default cannot change the look of
    # a CV between two applications to the same employer. None = not yet derived.
    # Presentation only: it carries no claim, figure or wording.
    "cv_style_variant": None,
    # A random id for this install, created on first use (see install_id()). It
    # seeds the style variant and is never rendered into a document.
    "install_id": None,
    # --- AI
    "ai_provider": None,  # None = MCP host only; deterministic when unattended
    # --- operations
    "backup_enabled": True,
    "backup_target": None,  # None = <CAREER_SCOUT_HOME>/backups
    "backup_keep_daily": 7,
    "backup_keep_weekly": 4,
    # A daily schedule with a couple of hours' slack: older than this is stale.
    "backup_stale_after_hours": 26,
    "run_staleness_alert_hours": 48,
    # How many of the best-ranked sufficient opportunities get a package per run.
    # Generating for every sufficient posting fills the queue with documents no
    # one will read; a person can approve about this many carefully in a day.
    "generate_top_n": 10,
    # How often `career-scout serve` polls Gmail for replies between daily runs.
    "gmail_poll_minutes": 60,
    "web_port": 8765,
    "web_bind": "0.0.0.0",  # noqa: S104 - LAN access is the point; see PLAN.md §7
}

#: Settings that must never be logged or returned by the settings API verbatim.
SECRET_KEYS: frozenset[str] = frozenset({"ai_api_key"})

#: Settings that grant approval. Only a person on this computer may change them —
#: never the MCP host, never a phone on the Wi-Fi. A model that could switch these
#: on could approve its own sends, which I-25 forbids (I-30).
HUMAN_ONLY_KEYS: frozenset[str] = frozenset(
    k for k in DEFAULTS if k.startswith("auto_approve_")
)


class UnknownSetting(KeyError):
    """A key with no default and no stored value."""


def get(conn: sqlite3.Connection, key: str) -> Any:
    """Read a setting, falling back to its default."""
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return json.loads(row["value"])
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise UnknownSetting(key)


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Write a setting. Unknown keys are refused rather than silently accepted."""
    if key not in DEFAULTS and key not in SECRET_KEYS:
        raise UnknownSetting(
            f"{key!r} is not a known setting; add it to DEFAULTS so it is visible "
            f"to the user rather than hidden in a row"
        )
    conn.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), utcnow()),
    )


def install_id(conn: sqlite3.Connection) -> str:
    """This install's stable random id, created on first call."""
    current = get(conn, "install_id")
    if current:
        return str(current)
    created = str(uuid.uuid4())
    set_value(conn, "install_id", created)
    return created


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every setting with its effective value, for the settings screen."""
    effective = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM setting"):
        if row["key"] in SECRET_KEYS:
            effective[row["key"]] = "***set***"
            continue
        effective[row["key"]] = json.loads(row["value"])
    return effective


# ------------------------------------------------------------- resolution


@dataclass(frozen=True, slots=True)
class ResolvedFloor:
    """A savings floor together with where it came from.

    ``value is None`` means the floor could not be resolved and the opportunity
    is reported ``UNSCORED`` with :attr:`reason`. It never means zero.
    """

    value: float | None
    source: str  # 'user_override' | 'country_default' | 'fallback' | 'unsourced'
    basis: str | None = None
    source_url: str | None = None
    as_of: str | None = None
    reason: str | None = None


def resolve_savings_floor(conn: sqlite3.Connection, country_iso2: str) -> ResolvedFloor:
    """User override, then the country's sourced default, then the flat fallback."""
    override = get(conn, "savings_floor_usd_month")
    if override is not None:
        return ResolvedFloor(value=float(override), source="user_override")

    row = conn.execute(
        "SELECT default_savings_floor, floor_basis, floor_source_url, floor_as_of, "
        "       enabled FROM country WHERE iso2 = ?",
        (country_iso2,),
    ).fetchone()

    if row is None:
        return ResolvedFloor(
            value=None,
            source="unsourced",
            reason=f"country {country_iso2} is not configured",
        )

    if row["default_savings_floor"] is not None:
        return ResolvedFloor(
            value=float(row["default_savings_floor"]),
            source="country_default",
            basis=row["floor_basis"],
            source_url=row["floor_source_url"],
            as_of=row["floor_as_of"],
        )

    # The country is configured but its default has not been sourced. Applying
    # the global fallback here is what produces a permanently empty shortlist
    # for a country whose prevailing wages differ materially, so it is refused
    # and reported instead.
    if _country_requires_own_default(conn, country_iso2):
        return ResolvedFloor(
            value=None,
            source="unsourced",
            reason=(
                f"{country_iso2} has no sourced default savings floor. "
                f"UNSCORED — default not yet sourced."
            ),
        )

    return ResolvedFloor(
        value=float(get(conn, "fallback_savings_floor_usd_month")),
        source="fallback",
        basis="global fallback applied because this country defines no default",
    )


def resolve_funding_floor(conn: sqlite3.Connection, country_iso2: str) -> ResolvedFloor:
    """The floor a scholarship's funding must clear, for T036.

    Its own override first, because a student's threshold is genuinely a
    different number from a salaried worker's. With no override it falls through
    to :func:`resolve_savings_floor`, on the reasoning that what you need left
    over each month does not depend on whether the money arrived as a salary or
    as a stipend — and an unsourced country floor stays unsourced either way.
    """
    override = get(conn, "scholarship_funding_floor_usd_month")
    if override is not None:
        return ResolvedFloor(
            value=float(override),
            source="user_override",
            basis="your own scholarship funding floor",
        )
    return resolve_savings_floor(conn, country_iso2)


def _country_requires_own_default(conn: sqlite3.Connection, country_iso2: str) -> bool:
    """Countries flagged as needing their own basis before any floor applies.

    Held in settings rather than hardcoded, so enabling a sixth country is a
    configuration change and never a code change.
    """
    return country_iso2 in get(conn, "countries_requiring_own_floor")


def cap_settings(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """``(per_destination, total, min_interval_seconds)`` — read by the send path."""
    return (
        int(get(conn, "cap_per_destination_per_day")),
        int(get(conn, "cap_total_per_day")),
        int(get(conn, "min_send_interval_seconds")),
    )
