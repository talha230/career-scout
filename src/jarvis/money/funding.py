"""Scholarship funding — T036.

The job side asks what a salary leaves after tax and rent. The scholarship side
asks the same question of a package that arrives in pieces::

    T = stipend + allowances + visa-capped work income

and then subtracts the same living costs, plus whatever tuition the award does
not cover. The result is stored in the same :class:`~jarvis.money.savings.Projection`
shape as a salaried role, so a shortlist can hold both and one screen can
explain either.

Three rules are specific to this side, and each one exists because the opposite
choice flatters an award.

**A waiver reduces the cost, never the income.** A waived tuition fee is money
not spent, not money received. Adding a 30,000 USD waiver to the income side
reports a scholarship as paying a salary it does not pay, and the arithmetic then
says a student can live on it.

**Unverified work income is zero.** Counting hours a visa may not permit, at a
wage nobody published, invents a job. Zero understates the funding, which is the
direction that leaves the user better informed rather than worse. It is written
as an explicit line with its reason, not left out silently — a reader has to be
able to see that work was considered and excluded, and why.

**Tuition the scheme is silent about is counted in full.** An award that says
nothing about fees has not waived them.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from jarvis import config as config_module
from jarvis.matching.posting import PostingView
from jarvis.matching.profile_view import CandidateView
from jarvis.money import fx, savings
from jarvis.money import reference as reference_module
from jarvis.money.savings import Line, Place, Projection, Unscored
from jarvis.store import settings as settings_module
from jarvis.store.db import utcnow

#: The shape of ``opportunity.funding_disclosed``. Defined here because this is
#: its first consumer, and every key may be absent — absent is not zero, except
#: where this module says in writing that it treats it as zero::
#:
#:     {"stipend":   {"amount": 1200, "currency": "EUR", "period": "month"},
#:      "allowances": [{"label": "travel", "amount": 1000,
#:                      "currency": "EUR", "period": "year"}],
#:      "tuition":   {"amount": 1500, "currency": "EUR", "period": "year"},
#:      "tuition_waived": true,
#:      "work_allowed": true,
#:      "work_hours_per_week": 20,
#:      "tax_exempt": true,
#:      "quote": "…", "source_url": "…"}
FUNDING_KEYS = (
    "stipend",
    "allowances",
    "tuition",
    "tuition_waived",
    "work_allowed",
    "work_hours_per_week",
    "tax_exempt",
)


def project(
    conn: sqlite3.Connection,
    posting: PostingView,
    candidate: CandidateView,
    *,
    today: str | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> Projection:
    """Project what a scholarship leaves per month, in USD."""
    money = config_module.money(version)
    rules = money["scholarship"]
    today = today or utcnow()[:10]

    lines: list[Line] = []
    unscored: list[Unscored] = []
    notes: list[str] = []

    place = _where_the_study_is(posting, rules, notes)
    funding = _funding_disclosed(posting)

    if funding is None:
        unscored.append(
            Unscored(
                "funding",
                "this scheme published no funding detail, and a scholarship with no "
                "published stipend is not a scholarship with a zero stipend. Read the "
                "award page before applying",
            )
        )

    total = _total_funding(
        conn, posting, place, funding, money, rules, today, lines, unscored, notes
    )
    tax = _tax(conn, total, place, funding, money, lines, unscored, notes)
    tuition = _tuition(conn, funding, place, money, today, lines, unscored, notes)
    costs = savings.cost_of_living_usd_month(
        conn, place, money, today, lines, unscored, notes, kinds=list(rules["cost_kinds"])
    )

    net: float | None = None
    if None not in (total, tax, tuition, costs):
        net = total - tax - tuition - costs  # type: ignore[operator]

    floor = settings_module.resolve_funding_floor(conn, posting.country_iso2 or "")
    passes: bool | None = None
    if net is not None and floor.value is not None:
        passes = net >= floor.value
    elif net is not None and floor.reason:
        unscored.append(Unscored("funding_floor", floor.reason))

    return Projection(
        opportunity_id=posting.id,
        arrangement=place.arrangement,
        tax_country=place.tax_country,
        cost_country=place.cost_country,
        cost_city=place.cost_city,
        pay_basis=place.pay_basis,
        gross_usd_month=total,
        tax_usd_month=tax,
        # The cost side of a scholarship is tuition plus living costs; they are
        # summed here so the stored total is the number the lines add up to.
        cost_usd_month=None if None in (tuition, costs) else (tuition or 0.0) + (costs or 0.0),
        net_savings_usd_month=net,
        floor_usd_month=floor.value,
        floor_source=floor.source,
        passes_floor=passes,
        confidence=savings.confidence_of(lines, unscored, net),
        lines=tuple(lines),
        unscored=tuple(unscored),
        notes=tuple(notes),
        computed_at=utcnow(),
        config_version=version,
    )


# ------------------------------------------------------------------ the parts


def _funding_disclosed(posting: PostingView) -> dict[str, Any] | None:
    """The scheme's own published funding block, if it published one."""
    raw = posting.funding_disclosed
    if not isinstance(raw, dict) or not raw:
        return None
    return raw


def _where_the_study_is(
    posting: PostingView, rules: dict[str, Any], notes: list[str]
) -> Place:
    """A scholarship is held where the institution is, unless it says otherwise."""
    arrangement = posting.work_arrangement or rules["default_arrangement"]
    if posting.work_arrangement is None:
        notes.append(f"Arrangement assumed {arrangement}: {rules['default_arrangement_basis']}")

    return Place(
        arrangement=arrangement,
        tax_country=posting.country_iso2,
        cost_country=posting.country_iso2,
        cost_city=posting.city,
    )


def _total_funding(  # noqa: PLR0913 - T is a sum of separately cited parts
    conn: sqlite3.Connection,
    posting: PostingView,
    place: Place,
    funding: dict[str, Any] | None,
    money: dict[str, Any],
    rules: dict[str, Any],
    today: str,
    lines: list[Line],
    unscored: list[Unscored],
    notes: list[str],
) -> float | None:
    """``T = stipend + allowances + visa-capped work income``, each line cited."""
    if funding is None:
        return None

    source_url = funding.get("source_url") or posting.source_url
    quote = funding.get("quote")

    stipend = funding.get("stipend") or {}
    if stipend.get("amount") is None:
        unscored.append(
            Unscored(
                "stipend",
                "this scheme published allowances or fees but no stipend figure. A "
                "missing stipend is unknown, not zero: whether it covers a month of "
                "rent is the whole question",
            )
        )
        return None

    place.pay_basis = "disclosed"
    total = savings.money_line(
        conn,
        amount=float(stipend["amount"]),
        currency=stipend.get("currency"),
        period=stipend.get("period"),
        money=money,
        today=today,
        label="stipend published by the scheme",
        source_url=stipend.get("source_url") or source_url,
        as_of=None,
        reference_figure_id=None,
        quote=stipend.get("quote") or quote,
        granularity=None,
        lines=lines,
        unscored=unscored,
        notes=notes,
        kind="stipend",
    )
    if total is None:
        return None

    for index, allowance in enumerate(funding.get("allowances") or []):
        if allowance.get("amount") is None:
            unscored.append(
                Unscored(
                    f"allowance_{index}",
                    f"the {allowance.get('label') or 'unnamed'} allowance names no amount, "
                    f"so it is not counted and the total is reported as incomplete",
                )
            )
            return None
        amount = savings.money_line(
            conn,
            amount=float(allowance["amount"]),
            currency=allowance.get("currency") or stipend.get("currency"),
            period=allowance.get("period"),
            money=money,
            today=today,
            label=f"{allowance.get('label') or 'allowance'} published by the scheme",
            source_url=allowance.get("source_url") or source_url,
            as_of=None,
            reference_figure_id=None,
            quote=allowance.get("quote") or quote,
            granularity=None,
            lines=lines,
            unscored=unscored,
            notes=notes,
            kind="allowance",
            component=f"allowance_{index}",
        )
        if amount is None:
            return None
        total += amount

    total += _work_income(conn, place, funding, rules, today, lines, notes)
    return total


def _work_income(  # noqa: PLR0913 - the cap, the wage and the accumulators
    conn: sqlite3.Connection,
    place: Place,
    funding: dict[str, Any],
    rules: dict[str, Any],
    today: str,
    lines: list[Line],
    notes: list[str],
) -> float:
    """Permitted work, at a published wage, or an explicit zero saying why not.

    This is the one figure in either engine that is allowed to be zero without
    being UNSCORED, and the asymmetry is deliberate: an unverified *income*
    counted as zero understates the package, while an unverified *cost* counted
    as zero overstates it. The zero is still written as a line with its reason,
    because a reader has to see that work was considered.
    """
    settings = rules["work_income"]

    def refuse(reason: str) -> float:
        lines.append(
            Line(
                kind="work_income",
                label="income from work alongside the award",
                direction="income",
                amount_usd_month=0.0,
                arithmetic=f"0.00 USD/month — {reason}",
                reference_figure_id=None,
                fx_rate_id=None,
                source_url=funding.get("source_url") or "",
                as_of=None,
                assumption=settings["zero_basis"],
            )
        )
        notes.append(f"Work income counted as zero: {reason}")
        return 0.0

    if funding.get("work_allowed") is False:
        return refuse("the scheme prohibits paid work alongside the award")
    if funding.get("work_allowed") is None:
        return refuse("the scheme does not say whether paid work is allowed")

    rule = reference_module.current(conn, "visa_rule", country_iso2=place.cost_country)
    if rule is None or rule.unit != "hours_per_week":
        return refuse(
            f"no published limit on student working hours is on file for "
            f"{place.cost_country or 'this country'}"
        )

    permitted = float(rule.value)
    stated = funding.get("work_hours_per_week")
    hours = min(permitted, float(stated)) if stated is not None else permitted

    wage = reference_module.current(
        conn, "wage_stat", country_iso2=place.cost_country, city=place.cost_city
    )
    if wage is None or savings.period_of_unit(wage.unit) != "hour" or not wage.currency:
        return refuse(
            f"no hourly wage figure is on file for {place.cost_city or place.cost_country}, "
            f"so hours the visa permits cannot be turned into money"
        )

    monthly_local = hours * float(settings["weeks_per_month"]) * wage.value
    try:
        converted = fx.convert_current(
            conn, monthly_local, base=wage.currency, quote=savings.TARGET_CURRENCY, today=today
        )
    except (fx.MissingRate, fx.StaleRate) as exc:
        return refuse(str(exc))

    lines.append(
        Line(
            kind="work_income",
            label=f"work permitted alongside the award, {hours:g} h/week",
            direction="income",
            amount_usd_month=converted.amount,
            arithmetic=(
                f"{hours:g} h/week x {settings['weeks_per_month']!r} weeks/month x "
                f"{wage.value:g} {wage.currency}/hour = {monthly_local:g} "
                f"{wage.currency}/month; {converted.arithmetic}"
            ),
            reference_figure_id=wage.id,
            fx_rate_id=converted.fx_rate_id,
            source_url=wage.source_url,
            as_of=wage.as_of,
            quote=wage.quote,
            granularity=wage.granularity,
            assumption=(
                f"assumes the full {hours:g} hours a week the visa permits "
                f"({rule.source_url}) are actually worked at the published average wage"
            ),
        )
    )
    return converted.amount


def _tax(  # noqa: PLR0913 - the exemption, the rate and the accumulators
    conn: sqlite3.Connection,
    total: float | None,
    place: Place,
    funding: dict[str, Any] | None,
    money: dict[str, Any],
    lines: list[Line],
    unscored: list[Unscored],
    notes: list[str],
) -> float | None:
    """Tax on the award, zero only where the scheme says the award is exempt."""
    if total is None:
        return None

    if funding and funding.get("tax_exempt") is True:
        lines.append(
            Line(
                kind="tax",
                label="the scheme states the award is tax exempt",
                direction="deduction",
                amount_usd_month=0.0,
                arithmetic="0.00 USD/month — stated exempt by the awarding body",
                reference_figure_id=None,
                fx_rate_id=None,
                source_url=funding.get("source_url") or "",
                as_of=None,
                quote=funding.get("quote"),
            )
        )
        return 0.0

    if funding is not None and funding.get("tax_exempt") is None:
        notes.append(
            "The scheme does not say whether the award is taxable, so it is taxed at the "
            "country's effective rate. That understates the award if it is in fact exempt."
        )
    return savings.tax_monthly_usd(conn, total, place, money, lines, unscored)


def _tuition(  # noqa: PLR0913 - the waiver, the fee and the accumulators
    conn: sqlite3.Connection,
    funding: dict[str, Any] | None,
    place: Place,
    money: dict[str, Any],
    today: str,
    lines: list[Line],
    unscored: list[Unscored],
    notes: list[str],
) -> float | None:
    """Tuition the award does not cover, as a cost. A waiver reduces it to zero."""
    if funding is None:
        return None

    fee = funding.get("tuition") or {}
    if fee.get("amount") is None:
        # No fee published. That is not the same as no fee, so it is noted, but
        # it is not counted either: inventing a tuition figure would hide awards
        # that genuinely charge nothing.
        notes.append(
            "No tuition figure was published. Nothing is charged against the award for "
            "fees, and nothing is claimed about whether fees exist."
        )
        return 0.0

    waived = funding.get("tuition_waived")
    if waived is True:
        lines.append(
            Line(
                kind="tuition",
                label="tuition waived by the award",
                direction="cost",
                amount_usd_month=0.0,
                arithmetic=(
                    f"0.00 USD/month — {fee['amount']:g} "
                    f"{fee.get('currency') or ''}/{fee.get('period') or 'period'} waived. "
                    f"A waiver reduces the cost; it is never added to income"
                ),
                reference_figure_id=None,
                fx_rate_id=None,
                source_url=fee.get("source_url") or funding.get("source_url") or "",
                as_of=None,
                quote=fee.get("quote") or funding.get("quote"),
            )
        )
        return 0.0

    if waived is None:
        notes.append(
            "The award does not say whether tuition is waived, so the published fee is "
            "charged in full. An award that is silent about fees has not waived them."
        )

    amount = savings.money_line(
        conn,
        amount=float(fee["amount"]),
        currency=fee.get("currency"),
        period=fee.get("period"),
        money=money,
        today=today,
        label="tuition the award does not cover",
        source_url=fee.get("source_url") or funding.get("source_url") or "",
        as_of=None,
        reference_figure_id=None,
        quote=fee.get("quote") or funding.get("quote"),
        granularity=None,
        lines=lines,
        unscored=unscored,
        notes=notes,
        kind="tuition",
        direction="cost",
    )
    if amount is None:
        return None
    return amount
