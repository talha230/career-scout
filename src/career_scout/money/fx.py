"""Currency conversion against sourced rates — T015, invariant I-20.

Every rate this module returns comes from a row in ``fx_rate`` that names where
it was fetched from and the date it applies to. There are four things it
deliberately refuses to do, and each refusal exists because the alternative
writes a number into somebody's savings projection that they cannot check:

**No nearest-date fallback.** Asking for 2026-09-01 and getting 2026-08-31's
rate is a different number wearing the requested date's label. A lookup with no
row raises :class:`MissingRate`, which names the pair and the date, so the fix
is to fetch the rate rather than to guess it.

**No triangulation.** EUR->USD times USD->PKR is two rates, possibly sourced
from two places, presented as one. If EUR->PKR is wanted, EUR->PKR gets
recorded.

**No implicit identity.** A posting whose currency could not be read has
``None``, not "probably the user's own". Converting ``None`` raises, because
treating an unknown currency as the target currency is how a PKR salary becomes
a USD salary 278 times too large.

**No silently ageing rate.** :func:`current_rate` returns the newest rate that
exists together with its date, and raises :class:`StaleRate` past the window in
``fx_rate_max_age_days``. A conversion at a two-year-old rate is not wrong
arithmetic; it is a right answer to a question nobody asked.

One thing it does do: read a stored pair backwards. A rate table holds
USD->PKR, and a PKR salary still has to reach USD. The inverse is ``1 / rate``
over the same row, and :attr:`Converted.inverted` plus :attr:`Converted.arithmetic`
say so, so a hand recomputation follows the same path the engine took.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date

from career_scout.store import settings as settings_module
from career_scout.store.db import utcnow


class MissingRate(LookupError):
    """No stored rate for this pair on this date. Deliberately not recoverable here."""


class StaleRate(LookupError):
    """A rate exists, but it is older than the window the user allows."""


@dataclass(frozen=True, slots=True)
class Rate:
    """One stored rate, identified so a projection line can cite it."""

    fx_rate_id: str | None
    base: str
    quote: str
    rate: float
    as_of: str | None
    inverted: bool = False
    identity: bool = False

    @property
    def basis(self) -> str:
        if self.identity:
            return f"{self.base} is already {self.quote}; no rate is involved"
        direction = f"{self.quote}/{self.base}" if self.inverted else f"{self.base}/{self.quote}"
        suffix = " (read backwards from the stored row)" if self.inverted else ""
        return f"{direction} as of {self.as_of}{suffix}"


@dataclass(frozen=True, slots=True)
class Converted:
    """A converted amount, carrying the row it used and the arithmetic it did."""

    amount: float
    rate: float
    fx_rate_id: str | None
    base: str
    quote: str
    as_of: str | None
    inverted: bool
    identity: bool
    arithmetic: str


def record_rate(
    conn: sqlite3.Connection,
    *,
    base: str,
    quote: str,
    rate: float,
    as_of: str,
    source_url: str,
    fetched_at: str | None = None,
) -> str:
    """Store one sourced rate and return its id.

    A second rate for the same pair and date is refused by the UNIQUE index
    rather than overwriting the first. Two different answers to one question
    mean the source changed shape, and that is worth an error.
    """
    if rate <= 0:
        raise ValueError(f"rate {rate!r} for {base}->{quote} is not positive")
    rate_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO fx_rate (id, base, quote, rate, as_of, source_url, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            rate_id,
            _currency(base, "base"),
            _currency(quote, "quote"),
            float(rate),
            as_of,
            source_url,
            fetched_at or utcnow(),
        ),
    )
    return rate_id


def lookup(conn: sqlite3.Connection, *, base: str, quote: str, as_of: str) -> Rate:
    """The rate for this pair on exactly this date, or :class:`MissingRate`."""
    base = _currency(base, "base")
    quote = _currency(quote, "quote")

    if base == quote:
        return Rate(fx_rate_id=None, base=base, quote=quote, rate=1.0, as_of=as_of, identity=True)

    row = conn.execute(
        "SELECT id, rate FROM fx_rate WHERE base = ? AND quote = ? AND as_of = ?",
        (base, quote, as_of),
    ).fetchone()
    if row is not None:
        return Rate(
            fx_rate_id=row["id"], base=base, quote=quote, rate=float(row["rate"]), as_of=as_of
        )

    back = conn.execute(
        "SELECT id, rate FROM fx_rate WHERE base = ? AND quote = ? AND as_of = ?",
        (quote, base, as_of),
    ).fetchone()
    if back is not None:
        return Rate(
            fx_rate_id=back["id"],
            base=base,
            quote=quote,
            rate=1.0 / float(back["rate"]),
            as_of=as_of,
            inverted=True,
        )

    raise MissingRate(
        f"no stored FX rate for {base}->{quote} on {as_of}. "
        f"Fetch and record that rate; there is no nearest-date fallback and no "
        f"triangulation through a third currency, because either would put an "
        f"unsourced number into a savings projection."
    )


def current_rate(
    conn: sqlite3.Connection, *, base: str, quote: str, today: str | None = None
) -> Rate:
    """The newest stored rate for this pair, refused once it is too old.

    The window is ``fx_rate_max_age_days`` in settings, never a constant here
    (I-23). The returned :class:`Rate` carries its own ``as_of``, so a caller
    can stamp the age onto whatever it displays.
    """
    base = _currency(base, "base")
    quote = _currency(quote, "quote")
    today = today or utcnow()[:10]

    if base == quote:
        return Rate(fx_rate_id=None, base=base, quote=quote, rate=1.0, as_of=today, identity=True)

    row = conn.execute(
        "SELECT id, base, quote, rate, as_of FROM fx_rate "
        "WHERE (base = ? AND quote = ?) OR (base = ? AND quote = ?) "
        "ORDER BY as_of DESC LIMIT 1",
        (base, quote, quote, base),
    ).fetchone()
    if row is None:
        raise MissingRate(
            f"no stored FX rate for {base}->{quote} at any date. Record one before "
            f"converting; an unconverted amount reported as converted is a wrong number."
        )

    age = _days_between(row["as_of"], today)
    window = int(settings_module.get(conn, "fx_rate_max_age_days"))
    if age > window:
        raise StaleRate(
            f"the newest {base}->{quote} rate is from {row['as_of']}, {age} days old, "
            f"past the {window}-day window in fx_rate_max_age_days. Refresh the rate "
            f"or widen the window deliberately."
        )

    inverted = row["base"] != base
    rate = 1.0 / float(row["rate"]) if inverted else float(row["rate"])
    return Rate(
        fx_rate_id=row["id"],
        base=base,
        quote=quote,
        rate=rate,
        as_of=row["as_of"],
        inverted=inverted,
    )


def convert(
    conn: sqlite3.Connection,
    amount: float,
    *,
    base: str,
    quote: str,
    as_of: str,
) -> Converted:
    """Convert ``amount`` from ``base`` to ``quote`` at the rate for ``as_of``."""
    return apply_rate(amount, lookup(conn, base=base, quote=quote, as_of=as_of))


def convert_current(
    conn: sqlite3.Connection,
    amount: float,
    *,
    base: str,
    quote: str,
    today: str | None = None,
) -> Converted:
    """Convert at the newest rate on file, refusing a stale one."""
    return apply_rate(amount, current_rate(conn, base=base, quote=quote, today=today))


def apply_rate(amount: float, found: Rate) -> Converted:
    """The arithmetic, separated so one looked-up rate can serve several lines."""
    amount = float(amount)
    if found.identity:
        arithmetic = f"{amount:g} {found.base} (no conversion)"
    elif found.inverted:
        stored = 1.0 / found.rate
        arithmetic = (
            f"{amount:g} {found.base} x (1 / {stored!r}) = {amount * found.rate:g} {found.quote}"
        )
    else:
        arithmetic = (
            f"{amount:g} {found.base} x {found.rate!r} = {amount * found.rate:g} {found.quote}"
        )

    return Converted(
        amount=amount * found.rate,
        rate=found.rate,
        fx_rate_id=found.fx_rate_id,
        base=found.base,
        quote=found.quote,
        as_of=found.as_of,
        inverted=found.inverted,
        identity=found.identity,
        arithmetic=arithmetic,
    )


def _currency(code: object, role: str) -> str:
    """Normalise an ISO 4217 code, refusing anything that is not one."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError(
            f"{role} currency is {code!r}. An unknown currency is not the target "
            f"currency: converting it as if it were inflates or deflates the amount "
            f"by the whole exchange rate."
        )
    normalised = code.strip().upper()
    if len(normalised) != 3 or not normalised.isalpha():
        raise ValueError(f"{role} currency {code!r} is not a three-letter ISO 4217 code")
    return normalised


def _days_between(earlier: str, later: str) -> int:
    return (date.fromisoformat(later[:10]) - date.fromisoformat(earlier[:10])).days
