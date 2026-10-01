"""Net monthly savings, in USD, from sourced figures only — T035.

The question this answers is the one the whole shortlist turns on: **after tax
and after living there, how much of this salary is left?** A ranking by gross
pay puts a 95,000 USD role in San Francisco above a 60,000 USD role in Dubai,
and the second one saves more.

Every line of the arithmetic cites two ids — a ``reference_figure_id`` for the
figure and an ``fx_rate_id`` for the conversion — so the same explanation
regenerates to the same numbers years later, and a user who disbelieves a number
can follow it to the sentence it was read from.

Three rules decide what this module refuses to do.

**A net figure needs every line.** Pay, tax and each required cost must resolve,
or ``net_savings_usd_month`` is ``None`` with the reasons attached. This
asymmetry is deliberate: a missing cost line does not make the cost smaller, it
makes the *saving* look bigger, and that is the direction that moves somebody to
another country for a job that does not pay for itself.

**Where you are taxed is where you say you are taxed.** A remote role is
evaluated against the user's own confirmed tax residence. It is never inferred
from citizenship, never from a postal address, and an unconfirmed tax residence
leaves the projection UNSCORED rather than assuming the employer's country.

**An estimated salary is labelled and ranked below a disclosed one.** 86% of the
legacy corpus disclosed no pay. Where an official wage statistic exists, it is
used and marked ``pay_basis='estimate'``; :attr:`Projection.sort_key` puts every
estimate below every disclosed figure, so an estimate can never outrank a number
an employer actually published.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from career_scout import config as config_module
from career_scout.matching.posting import PostingView
from career_scout.matching.profile_view import CandidateView
from career_scout.money import fx
from career_scout.money import reference as reference_module
from career_scout.store import settings as settings_module
from career_scout.store.db import utcnow

TARGET_CURRENCY = "USD"


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the projection, with the two ids that make it checkable."""

    kind: str
    label: str
    direction: str  # 'income' | 'cost' | 'deduction'
    amount_usd_month: float
    arithmetic: str
    reference_figure_id: str | None
    fx_rate_id: str | None
    source_url: str
    as_of: str | None
    quote: str | None = None
    granularity: str | None = None
    #: Set when the number rests on an assumption the source did not state —
    #: a full-time schedule behind an hourly rate, a national average standing
    #: in for a city. Displayed with the line; never silently dropped.
    assumption: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "direction": self.direction,
            "amount_usd_month": self.amount_usd_month,
            "arithmetic": self.arithmetic,
            "reference_figure_id": self.reference_figure_id,
            "fx_rate_id": self.fx_rate_id,
            "source_url": self.source_url,
            "as_of": self.as_of,
            "quote": self.quote,
            "granularity": self.granularity,
            "assumption": self.assumption,
        }


@dataclass(frozen=True, slots=True)
class Unscored:
    """One thing that could not be computed, and what would fix it."""

    component: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"component": self.component, "reason": self.reason}


@dataclass(slots=True)
class Projection:
    """The whole projection: the lines, the total, and everything missing."""

    opportunity_id: str
    arrangement: str | None
    #: Where the money is earned and taxed, and where the living costs are read.
    tax_country: str | None
    cost_country: str | None
    cost_city: str | None
    pay_basis: str | None  # 'disclosed' | 'estimate' | None
    gross_usd_month: float | None
    tax_usd_month: float | None
    cost_usd_month: float | None
    net_savings_usd_month: float | None
    floor_usd_month: float | None
    floor_source: str
    passes_floor: bool | None
    confidence: str  # 'low' | 'medium' | 'high'
    lines: tuple[Line, ...]
    unscored: tuple[Unscored, ...]
    computed_at: str
    config_version: str
    currency: str = TARGET_CURRENCY
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def arithmetic(self) -> str:
        """The whole sum as one string, so it can be checked by hand."""
        if self.net_savings_usd_month is None:
            missing = ", ".join(item.component for item in self.unscored)
            return f"UNSCORED — {missing or 'nothing computed'}"
        parts = [f"gross {self.gross_usd_month:.2f}"]
        if self.tax_usd_month is not None:
            parts.append(f"- tax {self.tax_usd_month:.2f}")
        for line in self.lines:
            if line.direction == "cost":
                parts.append(f"- {line.kind} {line.amount_usd_month:.2f}")
        return f"{' '.join(parts)} = {self.net_savings_usd_month:.2f} USD/month"

    @property
    def sort_key(self) -> tuple[int, int, float]:
        """Rank: scored above unscored, disclosed above estimated, then by size.

        Returned as a key rather than applied here, because the shortlist that
        sorts is also the thing that displays, and a hidden sort is a number the
        user cannot see.
        """
        scored = 1 if self.net_savings_usd_month is not None else 0
        disclosed = 1 if self.pay_basis == "disclosed" else 0
        return (scored, disclosed, self.net_savings_usd_month or 0.0)

    def as_dict(self) -> dict[str, Any]:
        """The JSON stored in ``assessment.projection``."""
        return {
            "opportunity_id": self.opportunity_id,
            "arrangement": self.arrangement,
            "tax_country": self.tax_country,
            "cost_country": self.cost_country,
            "cost_city": self.cost_city,
            "currency": self.currency,
            "pay_basis": self.pay_basis,
            "gross_usd_month": self.gross_usd_month,
            "tax_usd_month": self.tax_usd_month,
            "cost_usd_month": self.cost_usd_month,
            "net_savings_usd_month": self.net_savings_usd_month,
            "floor_usd_month": self.floor_usd_month,
            "floor_source": self.floor_source,
            "passes_floor": self.passes_floor,
            "confidence": self.confidence,
            "arithmetic": self.arithmetic,
            "lines": [line.as_dict() for line in self.lines],
            "unscored": [item.as_dict() for item in self.unscored],
            "notes": list(self.notes),
            "computed_at": self.computed_at,
            "config_version": self.config_version,
            "reference_figure_ids": sorted(
                {line.reference_figure_id for line in self.lines if line.reference_figure_id}
            ),
            "fx_rate_ids": sorted({line.fx_rate_id for line in self.lines if line.fx_rate_id}),
        }


# --------------------------------------------------------------- the engine


def project(
    conn: sqlite3.Connection,
    posting: PostingView,
    candidate: CandidateView,
    *,
    today: str | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> Projection:
    """Project net monthly savings in USD for one opportunity."""
    money = config_module.money(version)
    today = today or utcnow()[:10]

    lines: list[Line] = []
    unscored: list[Unscored] = []
    notes: list[str] = []

    place = where_the_money_lands(posting, candidate, unscored)

    gross = _gross_monthly_usd(conn, posting, place, money, today, lines, unscored, notes)
    tax = tax_monthly_usd(conn, gross, place, money, lines, unscored)
    costs = cost_of_living_usd_month(conn, place, money, today, lines, unscored, notes)

    net: float | None = None
    if gross is not None and tax is not None and costs is not None:
        net = gross - tax - costs

    floor = settings_module.resolve_savings_floor(conn, posting.country_iso2 or "")
    passes: bool | None = None
    if net is not None and floor.value is not None:
        passes = net >= floor.value
    elif net is not None and floor.reason:
        unscored.append(Unscored("savings_floor", floor.reason))

    return Projection(
        opportunity_id=posting.id,
        arrangement=place.arrangement,
        tax_country=place.tax_country,
        cost_country=place.cost_country,
        cost_city=place.cost_city,
        pay_basis=place.pay_basis,
        gross_usd_month=gross,
        tax_usd_month=tax,
        cost_usd_month=costs,
        net_savings_usd_month=net,
        floor_usd_month=floor.value,
        floor_source=floor.source,
        passes_floor=passes,
        confidence=confidence_of(lines, unscored, net),
        lines=tuple(lines),
        unscored=tuple(unscored),
        notes=tuple(notes),
        computed_at=utcnow(),
        config_version=version,
    )


@dataclass(slots=True)
class Place:
    """Which country taxes the pay and which place's costs apply."""

    arrangement: str | None
    tax_country: str | None
    cost_country: str | None
    cost_city: str | None
    pay_basis: str | None = None


def where_the_money_lands(
    posting: PostingView, candidate: CandidateView, unscored: list[Unscored]
) -> Place:
    """Resolve the two places the projection depends on, or say why it cannot.

    On-site: the money is earned, taxed and spent where the job is. Remote: it
    is earned from the employer's country but taxed and spent where the user
    lives, which is why ``tax_residence`` is a confirmed profile field and never
    a guess.
    """
    arrangement = posting.work_arrangement

    if arrangement is None:
        unscored.append(
            Unscored(
                "work_arrangement",
                "the posting did not say whether the role is on-site or remote, and that "
                "decides which country taxes the pay and whose living costs apply. It is "
                "not assumed to be on-site",
            )
        )
        return Place(None, None, None, None)

    if arrangement == "remote":
        tax_country = candidate.tax_residence
        if not tax_country:
            unscored.append(
                Unscored(
                    "tax_residence",
                    candidate.unknowns.get("tax_residence")
                    or "confirm where you pay tax; a remote salary's tax is never inferred "
                    "from your citizenship or your address",
                )
            )
        cost_country = candidate.residence_country
        if not cost_country:
            unscored.append(
                Unscored(
                    "residence_country",
                    candidate.unknowns.get("residence_country")
                    or "confirm the country you live in; a remote role is costed where you live",
                )
            )
        return Place(arrangement, tax_country, cost_country, candidate.residence_city)

    country = posting.country_iso2
    if not country:
        unscored.append(
            Unscored(
                "location",
                "this posting resolved to no country, so neither its tax nor its living "
                "costs can be looked up",
            )
        )
    return Place(arrangement, country, country, posting.city)


def _gross_monthly_usd(
    conn: sqlite3.Connection,
    posting: PostingView,
    place: Place,
    money: dict[str, Any],
    today: str,
    lines: list[Line],
    unscored: list[Unscored],
    notes: list[str],
) -> float | None:
    """The salary, monthly and in USD — disclosed if published, else estimated."""
    pay = posting.pay_disclosed or {}
    end = money["pay_range_end"]["use"]
    figure = pay.get(end) if pay.get(end) is not None else pay.get("max")

    if figure is not None:
        place.pay_basis = "disclosed"
        return money_line(
            conn,
            amount=float(figure),
            currency=pay.get("currency"),
            period=pay.get("period"),
            money=money,
            today=today,
            label=f"salary published by the employer ({end} of the range)",
            source_url=pay.get("source_url") or posting.source_url,
            as_of=None,
            reference_figure_id=None,
            quote=pay.get("raw_text"),
            granularity=None,
            lines=lines,
            unscored=unscored,
            notes=notes,
        )

    if not place.tax_country and not place.cost_country:
        unscored.append(
            Unscored(
                "pay",
                "this employer published no pay figure and there is nowhere to look up a "
                "wage statistic, because the posting resolved to no country",
            )
        )
        return None

    stat = reference_module.current(
        conn,
        "wage_stat",
        country_iso2=posting.country_iso2,
        city=posting.city,
        occupation=posting.role_family,
    )
    if stat is None:
        unscored.append(
            Unscored(
                "pay",
                f"this employer published no pay figure and no official wage statistic is "
                f"on file for {posting.role_family} in "
                f"{posting.country_iso2 or 'an unresolved country'}. An invented salary "
                f"would decide this shortlist",
            )
        )
        return None

    place.pay_basis = "estimate"
    notes.append(
        "Pay is an official wage statistic, not this employer's figure. It ranks below "
        "every posting that published one."
    )
    return money_line(
        conn,
        amount=stat.value,
        currency=stat.currency,
        period=period_of_unit(stat.unit),
        money=money,
        today=today,
        label=f"estimated from official wage statistics for {stat.occupation or 'this role'}",
        source_url=stat.source_url,
        as_of=stat.as_of,
        reference_figure_id=stat.id,
        quote=stat.quote,
        granularity=stat.granularity,
        lines=lines,
        unscored=unscored,
        notes=notes,
    )


def money_line(  # noqa: PLR0913 - every argument is a piece of the citation
    conn: sqlite3.Connection,
    *,
    amount: float,
    currency: str | None,
    period: str | None,
    money: dict[str, Any],
    today: str,
    label: str,
    source_url: str,
    as_of: str | None,
    reference_figure_id: str | None,
    quote: str | None,
    granularity: str | None,
    lines: list[Line],
    unscored: list[Unscored],
    notes: list[str],
    kind: str = "pay",
    component: str | None = None,
    direction: str = "income",
) -> float | None:
    """Normalise one income figure to USD per month, or record why it cannot be.

    Shared with the scholarship engine (T036), which runs a stipend, each
    allowance and any permitted work income through exactly this path — one
    place where a currency or a period becomes a monthly USD figure, so a
    stipend cannot be normalised by slightly different arithmetic from a salary.
    """
    component = component or kind
    if not currency:
        unscored.append(
            Unscored(
                component,
                "a pay figure was published with no currency, so it cannot be compared "
                "with anything. 60,000 is a different offer in every currency",
            )
        )
        return None

    conversion = money["periods_per_month"].get(period or "")
    if conversion is None:
        unscored.append(
            Unscored(
                component,
                f"the {kind} period is {period!r}, which is not one of "
                f"{sorted(money['periods_per_month'])}. Whether 60,000 is a year or a "
                f"month changes the answer twelvefold",
            )
        )
        return None

    monthly_own_currency = amount * conversion["factor"]

    try:
        converted = fx.convert_current(
            conn, monthly_own_currency, base=currency, quote=TARGET_CURRENCY, today=today
        )
    except (fx.MissingRate, fx.StaleRate) as exc:
        unscored.append(Unscored(component, str(exc)))
        return None

    assumption = conversion["basis"] if conversion.get("assumed") else None
    if assumption:
        notes.append(f"{kind.replace('_', ' ').capitalize()} converted from {period}: {assumption}")

    lines.append(
        Line(
            kind=kind,
            label=label,
            direction=direction,
            amount_usd_month=converted.amount,
            arithmetic=(
                f"{amount:g} {currency}/{period} x {conversion['factor']!r} = "
                f"{monthly_own_currency:g} {currency}/month; {converted.arithmetic}"
            ),
            reference_figure_id=reference_figure_id,
            fx_rate_id=converted.fx_rate_id,
            source_url=source_url,
            as_of=as_of or converted.as_of,
            quote=quote,
            granularity=granularity,
            assumption=assumption,
        )
    )
    return converted.amount


def tax_monthly_usd(
    conn: sqlite3.Connection,
    gross: float | None,
    place: Place,
    money: dict[str, Any],
    lines: list[Line],
    unscored: list[Unscored],
) -> float | None:
    """Tax on the gross, at the effective rate on file for the taxing country."""
    if gross is None:
        return None
    if not place.tax_country:
        return None  # the reason is already recorded by where_the_money_lands

    figure = reference_module.current(
        conn, "tax_rate", country_iso2=place.tax_country, city=place.cost_city
    )
    if figure is None:
        unscored.append(
            Unscored(
                "tax",
                f"no effective tax rate is on file for {place.tax_country}. Reporting the "
                f"gross as take-home overstates savings by the whole tax bill",
            )
        )
        return None

    unit = money["rate_units"].get(figure.unit)
    if unit is None:
        unscored.append(
            Unscored(
                "tax",
                f"the tax figure for {place.tax_country} is in {figure.unit!r}, which is "
                f"not one of {sorted(money['rate_units'])}. 32 and 0.32 differ by a "
                f"factor of a hundred",
            )
        )
        return None

    rate = figure.value * unit["factor"]
    if not 0 <= rate < 1:
        unscored.append(
            Unscored("tax", f"the tax rate for {place.tax_country} reads as {rate}, outside 0-1")
        )
        return None

    amount = gross * rate
    lines.append(
        Line(
            kind="tax",
            label=f"effective tax in {place.tax_country}",
            direction="deduction",
            amount_usd_month=amount,
            arithmetic=f"{gross:.2f} USD x {rate!r} = {amount:.2f} USD/month",
            reference_figure_id=figure.id,
            fx_rate_id=None,  # a rate is dimensionless; there is nothing to convert
            source_url=figure.source_url,
            as_of=figure.as_of,
            quote=figure.quote,
            granularity=figure.granularity,
        )
    )
    return amount


def cost_of_living_usd_month(  # noqa: PLR0913 - the accumulators travel together
    conn: sqlite3.Connection,
    place: Place,
    money: dict[str, Any],
    today: str,
    lines: list[Line],
    unscored: list[Unscored],
    notes: list[str],
    kinds: list[str] | None = None,
) -> float | None:
    """Every required living cost, monthly and in USD. One missing line is fatal.

    ``kinds`` overrides the per-arrangement list, which is how the scholarship
    engine costs a student's month from its own configured list.
    """
    if not place.arrangement or not place.cost_country:
        return None

    required = kinds if kinds is not None else money["required_cost_kinds"].get(place.arrangement)
    if required is None:
        unscored.append(
            Unscored(
                "cost_of_living",
                f"no cost list is configured for a {place.arrangement!r} arrangement",
            )
        )
        return None

    total = 0.0
    complete = True

    for kind in [*required, *money["optional_cost_kinds"]]:
        optional = kind in money["optional_cost_kinds"]
        figure = reference_module.current(
            conn, kind, country_iso2=place.cost_country, city=place.cost_city
        )
        if figure is None:
            if not optional:
                complete = False
                unscored.append(
                    Unscored(
                        f"cost_{kind}",
                        f"no {kind} figure on file for "
                        f"{place.cost_city or place.cost_country}. A missing cost does not "
                        f"make the cost smaller; it makes the saving look bigger",
                    )
                )
            continue

        amount = cost_line(conn, kind, figure, money, today, place, lines, unscored, notes)
        if amount is None:
            if not optional:
                complete = False
            continue
        total += amount

    return total if complete else None


def cost_line(  # noqa: PLR0913 - one line, and every argument is part of it
    conn: sqlite3.Connection,
    kind: str,
    figure: reference_module.Figure,
    money: dict[str, Any],
    today: str,
    place: Place,
    lines: list[Line],
    unscored: list[Unscored],
    notes: list[str],
) -> float | None:
    unit = money["cost_units_per_month"].get(figure.unit)
    if unit is None:
        unscored.append(
            Unscored(
                f"cost_{kind}",
                f"the {kind} figure for {place.cost_city or place.cost_country} is in "
                f"{figure.unit!r}, which is not one of "
                f"{sorted(money['cost_units_per_month'])}",
            )
        )
        return None

    if not figure.currency:
        unscored.append(
            Unscored(f"cost_{kind}", f"the {kind} figure carries no currency, so it cannot be USD")
        )
        return None

    monthly_own_currency = figure.value * unit["factor"]
    try:
        converted = fx.convert_current(
            conn, monthly_own_currency, base=figure.currency, quote=TARGET_CURRENCY, today=today
        )
    except (fx.MissingRate, fx.StaleRate) as exc:
        unscored.append(Unscored(f"cost_{kind}", str(exc)))
        return None

    assumption = None
    if place.cost_city and figure.granularity == "country":
        assumption = (
            f"national figure for {figure.country_iso2} standing in for "
            f"{place.cost_city}; a country is not a rent"
        )
        notes.append(f"{kind}: {assumption}")

    lines.append(
        Line(
            kind=kind,
            label=f"{kind} in {figure.city or figure.country_iso2 or 'the target location'}",
            direction="cost",
            amount_usd_month=converted.amount,
            arithmetic=(
                f"{figure.value:g} {figure.currency}/{figure.unit} x {unit['factor']!r} = "
                f"{monthly_own_currency:g} {figure.currency}/month; {converted.arithmetic}"
            ),
            reference_figure_id=figure.id,
            fx_rate_id=converted.fx_rate_id,
            source_url=figure.source_url,
            as_of=figure.as_of,
            quote=figure.quote,
            granularity=figure.granularity,
            assumption=assumption,
        )
    )
    return converted.amount


def period_of_unit(unit: str) -> str | None:
    """Read a wage statistic's period out of its unit, e.g. ``per_year`` -> ``year``.

    A unit this cannot read returns ``None``, which the pay path reports as
    UNSCORED rather than assuming a period.
    """
    if unit.startswith("per_"):
        return unit[4:]
    return unit if unit in {"year", "month", "week", "day", "hour"} else None


def confidence_of(lines: list[Line], unscored: list[Unscored], net: float | None) -> str:
    """How much the number deserves to be trusted, from what went into it."""
    if net is None:
        return "low"
    if any(line.assumption for line in lines) or unscored:
        return "low"
    if any(line.granularity == "country" for line in lines):
        return "medium"
    return "high"
