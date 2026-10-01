"""The opportunity, as the scorer sees it — T034.

A deliberately narrow view, and the narrowness is the point: **it carries no
description.** The schema says requirements are parsed once, at ingest, and
that no later step reads the description; making that structurally true rather
than merely intended means a component scorer *cannot* re-read a posting even
by accident, because the text is not in the object it is handed.

What the description is still needed for — its length, as a posting-quality
signal — is measured here, at the boundary, and travels as a number.

``pay_disclosed`` is the JSON the employer actually published. Its shape is
defined here because :mod:`jarvis.matching` is its first consumer::

    {"min": 65000, "max": 85000, "currency": "USD",
     "period": "year", "source_url": "https://…"}

Every key may be absent. Absent is not zero: a posting with no pay disclosed
leaves the salary component ``UNSCORED``, which is what 86% of the legacy
corpus did.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PostingView:
    """One opportunity, reduced to what a component is allowed to know."""

    id: str
    kind: str
    title: str
    employer: str
    country_iso2: str | None
    #: The office city, when the source resolved one. NULL stays NULL: the
    #: savings engine costs a city's rent, and a country is not a rent.
    city: str | None
    work_arrangement: str | None
    role_family: str
    requirements: dict[str, Any]
    requirements_confidence: float
    pay_disclosed: dict[str, Any] | None
    #: The scholarship side of the same question: what the scheme published
    #: about stipend, allowances, fees and permitted work. Its shape is
    #: documented in :mod:`jarvis.money.funding`, which is its first consumer.
    funding_disclosed: dict[str, Any] | None
    description_chars: int
    employer_open_roles: int
    deadline: str | None
    #: When the source says the posting went up. NULL when it published no date,
    #: which the staleness filter treats as unknown rather than as old.
    posted_at: str | None
    source_url: str

    @property
    def pay_is_disclosed(self) -> bool:
        """True only when the employer published a figure we can compare."""
        pay = self.pay_disclosed or {}
        return pay.get("min") is not None or pay.get("max") is not None

    @property
    def is_remote(self) -> bool:
        return self.work_arrangement == "remote"


def from_row(
    row: sqlite3.Row, *, employer_open_roles: int = 1
) -> PostingView:
    """Build the view from an ``opportunity`` row.

    This is the only place the description is touched, and only to measure it.
    """
    description = row["description"] or ""
    return PostingView(
        id=row["id"],
        kind=row["kind"],
        title=row["title"] or "",
        employer=row["employer"] or "",
        country_iso2=row["country_iso2"],
        city=row["city"],
        work_arrangement=row["work_arrangement"],
        role_family=row["role_family"] or "unclassified",
        requirements=json.loads(row["requirements"] or "{}"),
        requirements_confidence=float(row["requirements_confidence"] or 0.0),
        pay_disclosed=json.loads(row["pay_disclosed"] or "null"),
        funding_disclosed=json.loads(row["funding_disclosed"] or "null"),
        description_chars=len(description),
        employer_open_roles=employer_open_roles,
        deadline=row["deadline"],
        posted_at=row["posted_at"],
        source_url=row["source_url"],
    )


def load(conn: sqlite3.Connection, opportunity_id: str) -> PostingView:
    """One posting, with its employer's concurrently open role count."""
    row = conn.execute("SELECT * FROM opportunity WHERE id = ?", (opportunity_id,)).fetchone()
    if row is None:
        raise KeyError(f"no opportunity {opportunity_id}")

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM opportunity "
        "WHERE employer_norm = ? AND expired_at IS NULL",
        (row["employer_norm"],),
    ).fetchone()["n"]
    return from_row(row, employer_open_roles=int(count or 1))
