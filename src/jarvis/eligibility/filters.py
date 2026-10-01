"""Hard filters — T038.

A hard filter asks a question about the **posting**: is this a real, current,
professional vacancy somewhere the user will consider. Eligibility
(:mod:`jarvis.eligibility`) asks about the **reader** and soft-hides. The legacy
config mixed the two and encoded one candidate's citizenship, degree and three
languages as literals in the filter table; those rules moved and this module
keeps only the reader-independent ones.

Two properties make a rejection safe to act on.

**A rejected posting is stored, with a row naming the rule and quoting the
evidence.** Disable the rule, re-run, and the posting comes back. Nothing is
deleted and nothing is silently dropped, so a filter that is wrong is visible in
a query rather than in an absence nobody can see.

**An unknown never fires a filter.** A posting with no date is not stale; a user
whose documented years cannot be established is not compared against a year
requirement. Both are the same mistake in different clothes — treating a missing
value as a fact — and in this direction the mistake costs a whole board.

:func:`apply` is pure: the staleness window and the documented years arrive as
arguments, resolved from settings by the caller, so no threshold is a constant
here (I-23) and the rules are testable without a database.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

from jarvis import config as config_module
from jarvis.matching.posting import PostingView
from jarvis.store import rejections


@dataclass(frozen=True, slots=True)
class Rejection:
    """One rule that fired, with what it matched."""

    rule: str
    label: str
    reason: str
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "label": self.label,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def enabled_filters(version: str = config_module.CURRENT_VERSION) -> list[dict[str, Any]]:
    """The filters in force, in the order the config lists them."""
    return [
        rule
        for rule in config_module.hard_filters(version).get("filters", [])
        if rule.get("enabled")
    ]


@lru_cache(maxsize=32)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def apply(
    posting: PostingView,
    *,
    staleness_days: int,
    documented_years: float | None,
    today: date | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> Rejection | None:
    """The first rule that fires, or ``None`` to carry on to scoring."""
    today = today or date.today()

    for rule in enabled_filters(version):
        found = _check(rule, posting, staleness_days, documented_years, today)
        if found is not None:
            return found
    return None


def _check(  # noqa: PLR0911 - one return per rule reads better than a lookup table
    rule: dict[str, Any],
    posting: PostingView,
    staleness_days: int,
    documented_years: float | None,
    today: date,
) -> Rejection | None:
    rule_id = rule["id"]

    if rule_id == "F10_missing_essential_data":
        missing = [
            name
            for name, value in (
                ("title", posting.title),
                ("employer", posting.employer),
                ("source_url", posting.source_url),
            )
            if not (value or "").strip()
        ]
        if missing:
            return Rejection(
                rule_id,
                rule["label"],
                f"the posting has no {', no '.join(missing)}, so it cannot be traced or "
                f"applied to",
                evidence=f"missing: {', '.join(missing)}",
            )
        return None

    if rule_id == "F07_posting_too_old":
        if not posting.posted_at:
            return None  # an unknown date is not evidence of staleness
        try:
            posted = date.fromisoformat(posting.posted_at[:10])
        except ValueError:
            return None  # an unreadable date is also not evidence
        age = (today - posted).days
        if age > staleness_days:
            return Rejection(
                rule_id,
                rule["label"],
                f"posted {posted.isoformat()}, {age} days ago, past the "
                f"{staleness_days}-day staleness window",
                evidence=f"posted_at={posting.posted_at}",
            )
        return None

    if rule_id == "F05_experience_far_above_documented":
        required = (posting.requirements or {}).get("required_years")
        if required is None or documented_years is None:
            return None
        threshold = float(rule["threshold_years_above_documented"])
        if float(required) > documented_years + threshold:
            return Rejection(
                rule_id,
                rule["label"],
                f"it asks for {required} years and you have documented "
                f"{documented_years:.2f}, a gap of more than {threshold:g}",
                evidence=f"required_years={required}",
            )
        return None

    if rule_id == "F09_excluded_countries":
        excluded = rule.get("excluded_country_codes") or []
        if posting.country_iso2 and posting.country_iso2 in excluded:
            return Rejection(
                rule_id,
                rule["label"],
                f"{posting.country_iso2} is on your excluded list",
                evidence=f"country={posting.country_iso2}",
            )
        return None

    patterns = rule.get("patterns")
    if not patterns:
        return None

    # Only the title is available here, and that is structural rather than an
    # omission: the posting view carries no description, because requirements are
    # parsed once at ingest and no later stage re-reads the text. A rule that
    # wants to match the body says so and gets an error, because quietly matching
    # the title instead would look like the rule worked.
    if rule.get("applies_to", "title_only") != "title_only":
        raise ValueError(
            f"{rule_id} matches {rule['applies_to']!r}, but a hard filter can only see the "
            f"title: the description is deliberately absent from the posting view. Detect "
            f"it at ingest, in jarvis.discovery.requirements, and filter on the parsed "
            f"signal instead."
        )

    title = posting.title or ""
    for pattern in _compiled(tuple(patterns)):
        match = pattern.search(title)
        if match:
            return Rejection(
                rule_id,
                rule["label"],
                f"the title matched {match.group(0)!r}",
                evidence=_snippet(title, match),
            )
    return None


def _snippet(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 40)
    return "..." + text[start : match.end() + 40].replace("\n", " ").strip() + "..."


def record(
    conn: sqlite3.Connection,
    opportunity_id: str,
    rejection: Rejection | None,
    *,
    run_id: str | None = None,
) -> None:
    """Reconcile the stored hard-filter rejections with this verdict.

    ``None`` means the posting passed, which lifts any earlier hard-filter
    rejection. The posting itself stays exactly where it is either way.
    """
    rejections.reconcile(
        conn,
        opportunity_id,
        rejections.HARD_FILTER,
        None if rejection is None
        else rejections.Verdict(rejection.rule, rejection.reason, rejection.evidence),
        run_id=run_id,
    )
