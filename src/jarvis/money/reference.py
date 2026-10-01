"""Sourced figures — the only numbers the savings engine is allowed to use.

Every row carries where it came from, the date it applies to, a verbatim quote
and the path to the snapshot that quote must appear in. Nothing else may enter a
projection: an estimate typed in by hand, a remembered rent, a "roughly 30% tax"
are all numbers a user cannot check, and a projection made of them decides
whether somebody moves country.

Two properties are load-bearing.

**Figures are superseded, never updated.** An explanation shown a year from now
has to resolve to the same arithmetic it did on the day, so this month's rent
figure gets a new row and last month's keeps its id. :func:`current` reads only
rows with ``superseded_by IS NULL``.

**Specificity is reported, not assumed.** A country is not a rent. When a city
was asked for and only a national figure exists, the figure comes back with
``granularity='country'`` so the line can say so and the projection can drop its
confidence, rather than presenting a national average as a local cost.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from jarvis.store import snapshots
from jarvis.store.db import utcnow

#: The kinds ``reference_figure.kind`` accepts, mirrored here so a typo is an
#: error at the call site rather than a CHECK constraint failure deep in a run.
KINDS: frozenset[str] = frozenset(
    {
        "tax_rate",
        "rent",
        "utilities",
        "food",
        "transport",
        "health_insurance",
        "flight_home",
        "wage_stat",
        "visa_rule",
    }
)


class UnknownKind(ValueError):
    """A figure kind the schema does not accept."""


class QuoteNotInSnapshot(ValueError):
    """The quote stored with a figure is not in the page it names (I-22)."""


@dataclass(frozen=True, slots=True)
class Figure:
    """One sourced figure, with everything a projection line has to cite."""

    id: str
    kind: str
    value: float
    unit: str
    currency: str | None
    country_iso2: str | None
    city: str | None
    occupation: str | None
    source_url: str
    as_of: str
    quote: str
    snapshot_path: str
    #: ``'city'`` when this row is for the city that was asked for, ``'country'``
    #: when it is the national figure standing in for one, ``'global'`` when it
    #: is keyed to no country at all.
    granularity: str = "country"

    def as_citation(self) -> dict[str, Any]:
        return {
            "reference_figure_id": self.id,
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "source_url": self.source_url,
            "as_of": self.as_of,
            "quote": self.quote,
            "granularity": self.granularity,
            "country": self.country_iso2,
            "city": self.city,
            "occupation": self.occupation,
        }


def add_figure(
    conn: sqlite3.Connection,
    *,
    kind: str,
    value: float,
    unit: str,
    source_url: str,
    as_of: str,
    quote: str,
    snapshot_path: str,
    currency: str | None = None,
    country_iso2: str | None = None,
    city: str | None = None,
    occupation: str | None = None,
    fetched_at: str | None = None,
    supersedes: str | None = None,
) -> str:
    """Store one figure and return its id, superseding an earlier row if given."""
    if kind not in KINDS:
        raise UnknownKind(f"{kind!r} is not a reference figure kind; one of {sorted(KINDS)}")
    if not quote.strip():
        raise ValueError(
            f"a {kind} figure with no quote cannot be checked against its snapshot; "
            f"quote the sentence the number came from"
        )
    if not snapshots.contains(snapshot_path, quote):
        # Invariant I-22. A quote that is not in the snapshot is either a
        # paraphrase or a number from somewhere else, and both make the figure
        # uncheckable at exactly the moment somebody doubts it — which is the only
        # moment the quote was ever for.
        raise QuoteNotInSnapshot(
            f"the quote stored with this {kind} figure does not appear in "
            f"{snapshot_path}. Store the page the number was read from, and quote it "
            f"as it is written there rather than summarising it.\n"
            f"  quote: {quote[:160]!r}"
        )

    figure_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO reference_figure "
        "(id, kind, country_iso2, city, occupation, value, unit, currency, "
        " source_url, fetched_at, as_of, quote, snapshot_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            figure_id,
            kind,
            country_iso2,
            city,
            occupation,
            float(value),
            unit,
            currency,
            source_url,
            fetched_at or utcnow(),
            as_of,
            quote,
            snapshot_path,
        ),
    )
    if supersedes:
        conn.execute(
            "UPDATE reference_figure SET superseded_by = ? WHERE id = ?",
            (figure_id, supersedes),
        )
    return figure_id


def current(
    conn: sqlite3.Connection,
    kind: str,
    *,
    country_iso2: str | None = None,
    city: str | None = None,
    occupation: str | None = None,
) -> Figure | None:
    """The most specific current figure of this kind, or ``None``.

    Most specific first: the city asked for, then the country, then a figure
    keyed to no country. Each result says which of those it was, because a
    national average presented as a city's rent is a wrong number with a right
    provenance.
    """
    if kind not in KINDS:
        raise UnknownKind(f"{kind!r} is not a reference figure kind; one of {sorted(KINDS)}")

    attempts: list[tuple[str, str | None, str | None]] = []
    if city and country_iso2:
        attempts.append(("city", country_iso2, city))
    if country_iso2:
        attempts.append(("country", country_iso2, None))
    attempts.append(("global", None, None))

    for granularity, country, city_name in attempts:
        row = _one(conn, kind, country, city_name, occupation)
        if row is not None:
            return _to_figure(row, granularity)
    return None


def _one(
    conn: sqlite3.Connection,
    kind: str,
    country: str | None,
    city: str | None,
    occupation: str | None,
) -> sqlite3.Row | None:
    clauses = ["kind = ?", "superseded_by IS NULL"]
    params: list[Any] = [kind]

    clauses.append("country_iso2 = ?" if country else "country_iso2 IS NULL")
    if country:
        params.append(country)

    # A city-level lookup must not match a national row, and a national lookup
    # must not match one city's figure and report it as the country's.
    clauses.append("city = ?" if city else "city IS NULL")
    if city:
        params.append(city)

    if occupation:
        clauses.append("occupation = ?")
        params.append(occupation)
    else:
        clauses.append("occupation IS NULL")

    return conn.execute(
        f"SELECT * FROM reference_figure WHERE {' AND '.join(clauses)} "  # noqa: S608 - fixed clauses
        f"ORDER BY as_of DESC LIMIT 1",
        params,
    ).fetchone()


def _to_figure(row: sqlite3.Row, granularity: str) -> Figure:
    return Figure(
        id=row["id"],
        kind=row["kind"],
        value=float(row["value"]),
        unit=row["unit"],
        currency=row["currency"],
        country_iso2=row["country_iso2"],
        city=row["city"],
        occupation=row["occupation"],
        source_url=row["source_url"],
        as_of=row["as_of"],
        quote=row["quote"],
        snapshot_path=row["snapshot_path"],
        granularity=granularity,
    )
