"""Helpers shared by tests. Not imported by the package itself.

The one that matters is :func:`sourced_figure`. Writing a reference figure now
means writing the page it was read from too, because I-22 refuses a figure whose
quote is not in its snapshot. Every test that needs a rent or a tax rate needs a
snapshot as well, and doing that in one place keeps the invariant real in the
tests rather than worked around in each of them.
"""

from __future__ import annotations

import sqlite3

from career_scout.money import reference
from career_scout.store import snapshots


def sourced_figure(
    conn: sqlite3.Connection,
    *,
    kind: str,
    value: float,
    unit: str,
    currency: str | None = None,
    country_iso2: str | None = None,
    city: str | None = None,
    occupation: str | None = None,
    quote: str | None = None,
    source_url: str | None = None,
    as_of: str = "2026-09-01",
    supersedes: str | None = None,
) -> str:
    """Store a snapshot that really contains the quote, then the figure citing it."""
    quote = quote or f"{kind} is {value} {unit}"
    source_url = source_url or f"https://example.test/{kind}"

    snapshot = snapshots.store(
        f"<html><body><h1>{kind}</h1><p>{quote}</p></body></html>",
        source_url=source_url,
        fetched_at=f"{as_of}T00:00:00Z",
    )

    if country_iso2:
        conn.execute(
            "INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES (?, ?, 1)",
            (country_iso2, country_iso2),
        )

    return reference.add_figure(
        conn,
        kind=kind,
        value=value,
        unit=unit,
        currency=currency,
        country_iso2=country_iso2,
        city=city,
        occupation=occupation,
        source_url=source_url,
        as_of=as_of,
        quote=quote,
        snapshot_path=snapshot.relative_path,
        supersedes=supersedes,
    )
