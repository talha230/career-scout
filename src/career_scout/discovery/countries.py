"""Country packs — T031.

A pack is the whole country-specific decision in one place: whether the country
is searched, which registered sources serve it, and what its default savings
floor is together with where that figure came from. They live in
``config/v1/country_packs.json`` and land in the ``country`` table through
:func:`install`.

Two separations are load-bearing.

**Enablement is about searching, not about reading.** Location resolution knows
all 45 countries in ``country_preferences.json`` whether their pack is on or off,
so a posting that mentions Italy resolves to Italy either way and is simply never
fetched. If enablement changed how a posting was normalised, a stored country
would depend on which packs happened to be on that day — and enabling a sixth
country would silently rewrite the other five (T028, I-24).

**A pack names its own sources.** Not a global default list that shifts when a
pack is added, because that is the other way one country's stored output changes
when a different country is switched on. Providers are shared — one Greenhouse
client serves every pack that names it — but the decision to use it is per pack.

Floors ship ``null``. None of the five enabled countries has a sourced monthly
savings floor yet, and a plausible number written here would decide shortlists.
Resolution then does two correct things: Pakistan reports ``UNSCORED — default
not yet sourced``, and the others fall through to the global fallback and say
``fallback`` as their source, so nothing claims to be local data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from career_scout import config as config_module


class UnknownCountry(KeyError):
    """No pack for this ISO code."""


class PackInvalid(ValueError):
    """A pack that cannot be installed, and why."""


@dataclass(frozen=True, slots=True)
class Pack:
    """One country's whole pack."""

    code: str
    name: str
    enabled: bool
    currency: str | None
    sources: tuple[str, ...]
    default_savings_floor: float | None
    floor_basis: str | None
    floor_source_url: str | None
    floor_as_of: str | None
    notes: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "enabled": self.enabled,
            "currency": self.currency,
            "sources": list(self.sources),
            "default_savings_floor": self.default_savings_floor,
            "floor_basis": self.floor_basis,
            "floor_source_url": self.floor_source_url,
            "floor_as_of": self.floor_as_of,
            **self.notes,
        }


def packs(version: str = config_module.CURRENT_VERSION) -> dict[str, Pack]:
    """Every registered pack, keyed by ISO code, in file order."""
    loaded: dict[str, Pack] = {}
    for entry in config_module.load("country_packs", version).get("packs", []):
        code = entry["code"].upper()
        loaded[code] = Pack(
            code=code,
            name=entry["name"],
            enabled=bool(entry.get("enabled")),
            currency=entry.get("currency"),
            sources=tuple(entry.get("sources") or ()),
            default_savings_floor=entry.get("default_savings_floor"),
            floor_basis=entry.get("floor_basis"),
            floor_source_url=entry.get("floor_source_url"),
            floor_as_of=entry.get("floor_as_of"),
            notes={
                key: entry[key]
                for key in ("floor_note", "source_note", "disabled_reason")
                if entry.get(key)
            },
        )
    return loaded


def pack(code: str, version: str = config_module.CURRENT_VERSION) -> Pack:
    found = packs(version).get(code.upper())
    if found is None:
        raise UnknownCountry(f"no country pack for {code!r}")
    return found


def validate(version: str = config_module.CURRENT_VERSION) -> list[str]:
    """Problems that would make a pack a lie. Empty list means sound.

    Run by ``career-scout doctor`` alongside the other config checks. Two of these have
    bitten already in this design's ancestors: a floor with no basis (a number
    nobody can argue with), and a pack naming a source that is not in the
    registry (a country that looks covered and is never fetched).
    """
    problems: list[str] = []
    registered = registered_sources(version)

    for code, entry in packs(version).items():
        if entry.default_savings_floor is not None and not (
            entry.floor_basis and entry.floor_source_url and entry.floor_as_of
        ):
            problems.append(
                f"country_packs: {code} sets a savings floor with no basis, source URL "
                f"or as-of date. A floor nobody can trace is a number nobody can argue "
                f"with, and it decides which opportunities are shown."
            )

        for name in entry.sources:
            if name not in registered:
                problems.append(
                    f"country_packs: {code} names source {name!r}, which is not in "
                    f"sources.json. The pack would report coverage the pipeline cannot "
                    f"fetch."
                )

        if entry.enabled and not entry.sources:
            problems.append(
                f"country_packs: {code} is enabled with no sources, so it would be "
                f"searched by nothing while appearing to be on."
            )

    return problems


def registered_sources(version: str = config_module.CURRENT_VERSION) -> set[str]:
    """Every source name a pack may legitimately reference.

    Read from ``sources.json`` rather than listed here, so adding a board cannot
    leave this check behind. ``manual_review_only`` entries are deliberately
    absent: they are names the pipeline must never fetch, so a pack naming one is
    an error rather than a permission.
    """
    config = config_module.sources(version)
    names: set[str] = {entry["name"] for entry in config.get("aggregators", [])}
    names |= set(config.get("employer_boards", {}))
    names |= {entry["name"] for entry in config.get("government_portals", [])}
    return names


def install(conn: sqlite3.Connection, version: str = config_module.CURRENT_VERSION) -> list[str]:
    """Write every pack into the ``country`` table. Returns the codes installed.

    Idempotent, and it preserves what the user changed: a country's ``enabled``
    flag is written only when the row is new. Reinstalling packs after an upgrade
    must not switch a country back on that somebody deliberately switched off.
    """
    problems = validate(version)
    if problems:
        raise PackInvalid("; ".join(problems))

    installed: list[str] = []
    for code, entry in packs(version).items():
        existing = conn.execute(
            "SELECT iso2, enabled FROM country WHERE iso2 = ?", (code,)
        ).fetchone()

        if existing is None:
            conn.execute(
                "INSERT INTO country (iso2, name, enabled, default_savings_floor, "
                "floor_basis, floor_source_url, floor_as_of, currency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code,
                    entry.name,
                    int(entry.enabled),
                    entry.default_savings_floor,
                    entry.floor_basis,
                    entry.floor_source_url,
                    entry.floor_as_of,
                    entry.currency,
                ),
            )
        else:
            # The pack's reference data is refreshed; the user's own enablement is
            # not touched. A sourced floor arriving in a later pack version is the
            # point of this branch.
            conn.execute(
                "UPDATE country SET name = ?, default_savings_floor = ?, floor_basis = ?, "
                "floor_source_url = ?, floor_as_of = ?, currency = ? WHERE iso2 = ?",
                (
                    entry.name,
                    entry.default_savings_floor,
                    entry.floor_basis,
                    entry.floor_source_url,
                    entry.floor_as_of,
                    entry.currency,
                    code,
                ),
            )
        installed.append(code)

    installed.extend(_install_reference_rows(conn, set(installed), version))
    return installed


def _install_reference_rows(
    conn: sqlite3.Connection, packed: set[str], version: str
) -> list[str]:
    """A ``country`` row for every country location resolution can return.

    ``opportunity.country_iso2`` is a foreign key into ``country``, and a fetched
    board posts wherever its employer hires: the first Careem posting resolves to
    AE, which has no pack. Without a row it fails the insert, and the whole
    source's batch with it. The ingest tests never saw this because they seed the
    two countries they use.

    These rows are **reference data, not packs**: ``enabled = 0``, no sources, no
    floor. ``enabled = 0`` here means "no pack searches for it", which is true —
    an AE posting still arrives when a board some pack fetched happens to post
    one, and it is stored like any other. Only a country with a pack that is
    switched off is a decision to leave it out.
    """
    added: list[str] = []
    preferences = config_module.load("country_preferences", version)
    for entry in preferences.get("countries", []):
        code = entry["code"].upper()
        if code in packed:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES (?, ?, 0)",
            (code, entry["name"]),
        )
        if cursor.rowcount:
            added.append(code)
    return added


def switched_off(
    conn: sqlite3.Connection, version: str = config_module.CURRENT_VERSION
) -> frozenset[str]:
    """Countries with a pack that is currently off — an explicit decision.

    A pack country whose row is ``enabled = 0``: either shipped off (IT, DK, SE)
    or switched off by the user. Not every ``enabled = 0`` row: an unpacked
    country's reference row is also 0, and treating that as "switched off" would
    silently drop every posting from ~37 countries nobody made a decision about.
    """
    registered = set(packs(version))
    return frozenset(
        row["iso2"]
        for row in conn.execute("SELECT iso2 FROM country WHERE enabled = 0")
        if row["iso2"] in registered
    )


def set_enabled(conn: sqlite3.Connection, code: str, *, enabled: bool) -> None:
    """Switch one country on or off. A country with no pack is refused."""
    code = code.upper()
    pack(code)  # raises UnknownCountry
    updated = conn.execute(
        "UPDATE country SET enabled = ? WHERE iso2 = ?", (int(enabled), code)
    ).rowcount
    if not updated:
        raise UnknownCountry(
            f"{code} has a pack but no row; run install() before enabling it"
        )


def enabled_codes(conn: sqlite3.Connection) -> list[str]:
    """The countries currently searched, alphabetically."""
    return [
        row["iso2"]
        for row in conn.execute("SELECT iso2 FROM country WHERE enabled = 1 ORDER BY iso2")
    ]


def sources_for(
    conn: sqlite3.Connection, version: str = config_module.CURRENT_VERSION
) -> dict[str, tuple[str, ...]]:
    """``{source name: the enabled countries that asked for it}``.

    The shape discovery needs: one client per source, told which countries are
    asking. A source no enabled pack names is absent rather than present with an
    empty list, so nothing is fetched "just in case".
    """
    asked: dict[str, list[str]] = {}
    registered = packs(version)
    for code in enabled_codes(conn):
        entry = registered.get(code)
        if entry is None:
            continue
        for name in entry.sources:
            asked.setdefault(name, []).append(code)
    return {name: tuple(codes) for name, codes in sorted(asked.items())}
