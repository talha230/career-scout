"""The candidate, as the scorer sees them — T034.

The legacy engine read ``master_cv.json``: one file, one person, every figure
precomputed by hand. ``conservative_years_documented: 6.62`` was a number
somebody worked out and typed. This module computes the same quantities from
``profile_record``, so they move when the record moves and every one of them
traces to the rows it came from.

**Only confirmed, current records are read.** An extraction the user has not
looked at is a proposal (T017, I-12), and a proposal must not quietly move a
score the user then acts on. The consequence is deliberate and is the product
loop: a freshly imported profile scores mostly ``UNSCORED``, each unknown says
which records would change that, and confirming them is visibly what unlocks a
real number.

**An unknown is an unknown, never a zero.** Every attribute the scorer needs
either has a value or has a reason in :attr:`CandidateView.unknowns`, which the
component scorers hand straight to ``UNSCORED``. Nothing here defaults: a
candidate with no confirmed work history has ``documented_years is None``, not
``0.0``, because those are different claims and only one of them is true.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from career_scout import config as config_module
from career_scout.discovery import requirements as requirements_module
from career_scout.discovery.requirements import DEGREE_LEVELS, find_skill_matches

#: Used only if the packaged config somehow omits the ladder; the real one is
#: data, read by :func:`education_ladder`.
EDUCATION_LADDER_FALLBACK = {"none": 0, "diploma": 1, "bachelor": 2, "master": 3, "phd": 4}

_DATE = re.compile(r"^(?P<year>\d{4})(?:-(?P<month>\d{2}))?(?:-(?P<day>\d{2}))?$")
_PRESENT = re.compile(r"^(?:present|current|now|ongoing|to date)$", re.IGNORECASE)

#: How a date of reduced precision is placed on the calendar. The midpoint of
#: the stated precision, in both directions, because it is the choice with the
#: smallest possible error and no directional bias — a start placed at 1
#: January and an end at 31 December would inflate every range.
PRECISION_CONVENTION = (
    "a date given to the day is used as given; a date given as YYYY-MM is placed "
    "at the 15th of that month; a date given as YYYY alone is placed at 1 July. "
    "The midpoint of the stated precision, in both directions."
)


def education_ladder(version: str = config_module.CURRENT_VERSION) -> dict[str, int]:
    """The qualification ranking, from config rather than from this file."""
    education = config_module.weights(version).get("components", {}).get("education", {})
    return education.get("education_ladder") or dict(EDUCATION_LADDER_FALLBACK)


@dataclass(frozen=True, slots=True)
class DateRange:
    """One employment range, with the precision it was actually stated to."""

    start: date
    end: date
    start_precision: str  # 'day' | 'month' | 'year' | 'open'
    end_precision: str
    open_ended: bool
    label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_precision": self.start_precision,
            "end_precision": self.end_precision,
            "open_ended": self.open_ended,
            "days": (self.end - self.start).days,
        }


@dataclass(frozen=True, slots=True)
class CandidateView:
    """Everything the component scorers are allowed to know about the user."""

    documented_years: float | None
    years_basis: dict[str, Any]
    education_level: str | None
    education_evidence: str | None
    target_roles: tuple[str, ...]
    salary_minimum: float | None
    salary_currency: str | None
    citizenship: tuple[str, ...]
    residence_country: str | None
    residence_city: str | None
    tax_residence: str | None
    skills: frozenset[str]
    skill_evidence: dict[str, str]
    #: Languages the user has confirmed, normalised to the same names the
    #: requirement parser uses, so "this posting demands German and you speak
    #: German" is one comparison. An empty set means nothing is confirmed, which
    #: leaves the language dimension UNSCORED rather than blocking a role.
    languages: frozenset[str] = frozenset()
    #: attribute name -> why it could not be established. Handed verbatim to
    #: ``UNSCORED`` so a missing component names the record that would fix it.
    unknowns: dict[str, str] = field(default_factory=dict)
    #: Current records in each area still awaiting confirmation: what the user
    #: would have to confirm for an UNSCORED component to become a number.
    unconfirmed_counts: dict[str, int] = field(default_factory=dict)
    as_of: str = ""

    def has(self, skill: str) -> bool:
        return skill in self.skills

    def as_dict(self) -> dict[str, Any]:
        """The provenance block stored beside an assessment."""
        return {
            "documented_years": self.documented_years,
            "years_basis": self.years_basis,
            "education_level": self.education_level,
            "education_evidence": self.education_evidence,
            "target_roles": list(self.target_roles),
            "salary_minimum": self.salary_minimum,
            "salary_currency": self.salary_currency,
            "citizenship": list(self.citizenship),
            "residence_country": self.residence_country,
            "residence_city": self.residence_city,
            "tax_residence": self.tax_residence,
            "confirmed_skills": sorted(self.skills),
            "skill_evidence": self.skill_evidence,
            "languages": sorted(self.languages),
            "unknowns": self.unknowns,
            "unconfirmed_counts": self.unconfirmed_counts,
            "as_of": self.as_of,
        }


# ----------------------------------------------------------------- reading


def _confirmed(conn: sqlite3.Connection) -> dict[str, str]:
    """Current, confirmed, non-empty values, keyed by exact field path."""
    return {
        row["field_path"]: row["value"]
        for row in conn.execute(
            "SELECT field_path, value FROM profile_record "
            "WHERE superseded_by IS NULL AND confirmed = 1 "
            "AND value IS NOT NULL AND TRIM(value) != '' "
            "ORDER BY created_at"
        )
    }


def _unconfirmed_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Current records still awaiting confirmation, grouped by their area."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT field_path FROM profile_record "
        "WHERE superseded_by IS NULL AND confirmed = 0 "
        "AND value IS NOT NULL AND TRIM(value) != ''"
    ):
        area = re.split(r"[.\[]", row["field_path"])[0]
        counts[area] = counts.get(area, 0) + 1
    return counts


def _group(values: dict[str, str], prefix: str) -> dict[int, dict[str, str]]:
    """``work[2].employer`` -> ``{2: {'employer': ...}}``."""
    pattern = re.compile(rf"^{re.escape(prefix)}\[(\d+)\]\.(.+)$")
    grouped: dict[int, dict[str, str]] = {}
    for path, value in values.items():
        found = pattern.match(path)
        if found:
            grouped.setdefault(int(found.group(1)), {})[found.group(2)] = value
    return grouped


def _indexed(values: dict[str, str], prefix: str) -> list[str]:
    """``preferences.target_roles[0]`` … in index order."""
    pattern = re.compile(rf"^{re.escape(prefix)}\[(\d+)\]$")
    found: list[tuple[int, str]] = []
    for path, value in values.items():
        match = pattern.match(path)
        if match:
            found.append((int(match.group(1)), value))
    return [value for _index, value in sorted(found)]


# -------------------------------------------------------------------- dates


def parse_profile_date(raw: str | None, *, as_of: date) -> tuple[date, str] | None:
    """``(date, precision)`` for a stored profile date, or None if unreadable.

    Reduced precision is placed at the midpoint of what was stated
    (:data:`PRECISION_CONVENTION`) rather than at an edge, and the precision
    travels with the value so the arithmetic can say what it assumed.
    """
    if not raw:
        return None
    text = raw.strip()
    if _PRESENT.match(text):
        return as_of, "open"

    found = _DATE.match(text)
    if not found:
        return None

    year = int(found.group("year"))
    month, day = found.group("month"), found.group("day")
    try:
        if month and day:
            return date(year, int(month), int(day)), "day"
        if month:
            return date(year, int(month), 15), "month"
    except ValueError:
        return None
    return date(year, 7, 1), "year"


def merge(ranges: list[DateRange]) -> list[tuple[date, date]]:
    """Non-overlapping union, so two concurrent roles are not counted twice."""
    if not ranges:
        return []
    ordered = sorted(((r.start, r.end) for r in ranges), key=lambda pair: pair[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def documented_experience(
    values: dict[str, str], *, as_of: date
) -> tuple[float | None, dict[str, Any]]:
    """Years of confirmed employment, and the workings behind the figure.

    The formula is the one the legacy profile recorded for the same quantity —
    ``(end - start).days / 365.25``, summed over non-overlapping roles — so the
    two can be compared directly. What changed is where the inputs come from.
    """
    ranges: list[DateRange] = []
    skipped: list[dict[str, str]] = []

    for index, entry in sorted(_group(values, "work").items()):
        label = entry.get("employer") or entry.get("position") or f"work[{index}]"
        start = parse_profile_date(entry.get("startDate"), as_of=as_of)
        if start is None:
            skipped.append({"entry": label, "reason": "no confirmed, readable start date"})
            continue

        end = parse_profile_date(entry.get("endDate"), as_of=as_of) or (as_of, "open")
        if end[0] < start[0]:
            skipped.append({"entry": label, "reason": "end date precedes start date"})
            continue

        ranges.append(
            DateRange(
                start=start[0],
                end=end[0],
                start_precision=start[1],
                end_precision=end[1],
                open_ended=end[1] == "open",
                label=label,
            )
        )

    basis: dict[str, Any] = {
        "formula": "(end - start).days / 365.25, summed over non-overlapping confirmed roles",
        "precision_convention": PRECISION_CONVENTION,
        "as_of": as_of.isoformat(),
        "ranges": [r.as_dict() for r in ranges],
        "skipped": skipped,
    }

    if not ranges:
        basis["reason"] = (
            "no confirmed work history with a readable start date, so years of "
            "experience cannot be computed. Zero would be a different claim"
        )
        return None, basis

    days = sum((end - start).days for start, end in merge(ranges))
    basis["merged_days"] = days
    basis["arithmetic"] = f"{days} / 365.25 = {days / 365.25:.2f}"
    return round(days / 365.25, 2), basis


# ------------------------------------------------------------------- build


def build(
    conn: sqlite3.Connection,
    *,
    as_of: date | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> CandidateView:
    """Read the profile once, into the shape the component scorers need."""
    today = as_of or datetime.now(UTC).date()
    values = _confirmed(conn)
    pending = _unconfirmed_counts(conn)
    unknowns: dict[str, str] = {}

    years, years_basis = documented_experience(values, as_of=today)
    if years is None:
        unknowns["documented_years"] = _with_pending(years_basis["reason"], pending, "work")

    level, level_evidence = _highest_qualification(values, version)
    if level is None:
        unknowns["education_level"] = _with_pending(
            "no confirmed qualification in your profile, so there is nothing to "
            "compare the posting's requirement against",
            pending,
            "education",
        )

    roles = tuple(_indexed(values, "preferences.target_roles"))
    if not roles:
        unknowns["target_roles"] = _with_pending(
            "no confirmed target roles, so there is nothing to measure this "
            "title's distance from",
            pending,
            "preferences",
        )

    minimum = _number(values.get("preferences.salary_minimum"))
    if minimum is None:
        unknowns["salary_minimum"] = (
            "you have stated no minimum salary, and one cannot be inferred from "
            "your profile without inventing it"
        )

    skills, evidence = _claimed_skills(values, version)
    if not skills:
        unknowns["skills"] = _with_pending(
            "no confirmed skills in your profile. Scoring a posting's skill list "
            "against an empty set would report a gap that is really an absence "
            "of evidence",
            pending,
            "skills",
        )

    languages = _claimed_languages(values)
    if not languages:
        unknowns["languages"] = _with_pending(
            "no confirmed languages in your profile, so a posting that demands one "
            "cannot be judged either way",
            pending,
            "languages",
        )

    residence = values.get("basics.location.countryCode")
    if not residence:
        unknowns["residence_country"] = "no confirmed country of residence"

    # The city, not just the country, because a country is not a rent: the
    # savings engine costs a remote role where the user actually lives.
    residence_city = values.get("basics.location.city")
    if not residence_city:
        unknowns["residence_city"] = _with_pending(
            "no confirmed city of residence, so the cost of living where you "
            "already live cannot be looked up",
            pending,
            "basics",
        )

    tax_residence = values.get("identity.tax_residence")
    if not tax_residence:
        unknowns["tax_residence"] = (
            "no confirmed tax residence, which is what decides how a remote "
            "salary is taxed. It is never inferred from your citizenship or "
            "your address"
        )

    citizenship = tuple(
        part.strip().upper()
        for part in (values.get("identity.citizenship") or "").split(",")
        if part.strip()
    )
    if not citizenship:
        unknowns["citizenship"] = (
            "no confirmed citizenship, which is what decides which immigration "
            "route applies. It is never inferred from your address"
        )

    return CandidateView(
        documented_years=years,
        years_basis=years_basis,
        education_level=level,
        education_evidence=level_evidence,
        target_roles=roles,
        salary_minimum=minimum,
        salary_currency=values.get("preferences.salary_currency"),
        citizenship=citizenship,
        residence_country=residence,
        residence_city=residence_city,
        tax_residence=tax_residence,
        skills=skills,
        skill_evidence=evidence,
        languages=languages,
        unknowns=unknowns,
        unconfirmed_counts=pending,
        as_of=today.isoformat(),
    )


def _with_pending(reason: str, pending: dict[str, int], area: str) -> str:
    """Name what the user could confirm to turn this UNSCORED into a number."""
    waiting = pending.get(area, 0)
    if not waiting:
        return reason
    return (
        f"{reason}. {waiting} extracted {area} value(s) are waiting for you to "
        f"confirm them, which would make this scorable."
    )


def _number(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


def _highest_qualification(
    values: dict[str, str], version: str
) -> tuple[str | None, str | None]:
    """The best confirmed qualification, read by the same patterns as a posting.

    Using one set of patterns for both sides means a posting asking for a
    "B.Eng" and a profile recording "B.Sc." land on the same rung, rather than
    on whichever rung two separate lists happened to agree about.
    """
    ladder = education_ladder(version)
    best_rank, best_level, evidence = -1, None, None

    for _index, entry in sorted(_group(values, "education").items()):
        text = " ".join(part for part in (entry.get("studyType"), entry.get("area")) if part)
        for level, pattern in DEGREE_LEVELS:
            match = pattern.search(text)
            if match:
                if ladder.get(level, -1) > best_rank:
                    best_rank, best_level = ladder[level], level
                    evidence = entry.get("studyType") or match.group(0)
                break

    return best_level, evidence


def _claimed_languages(values: dict[str, str]) -> frozenset[str]:
    """Languages the user confirmed, in the parser's own vocabulary.

    Normalised through :data:`career_scout.discovery.requirements.LANGUAGE_NAMES`, the
    same table that reads a posting, so "Deutsch" in a CV and "German" in a
    posting are one language rather than two. A word that table does not know is
    kept as written and lower-cased: it is the user's own claim about themselves,
    and dropping it would silently narrow what they said they speak.
    """
    found: set[str] = set()
    for path, value in values.items():
        if not path.startswith("languages["):
            continue
        if not (path.endswith(".language") or path.endswith("].language")):
            continue
        for part in re.split(r"[,/;]| and ", value):
            word = part.strip().lower()
            if not word:
                continue
            found.add(requirements_module.LANGUAGE_NAMES.get(word, word))
    return frozenset(found)


def _claimed_skills(
    values: dict[str, str], version: str
) -> tuple[frozenset[str], dict[str, str]]:
    """Vocabulary skills the user's own confirmed records name.

    Matched by the identical alternation used to read a posting, so "this
    posting asks for X and you have X" is one comparison rather than two
    different ones that happen to use the same word.
    """
    text = "\n".join(
        value
        for path, value in values.items()
        if path.startswith(("skills[", "certificates["))
    )
    matches = find_skill_matches(text, version)
    return frozenset(canonical for canonical, _ in matches), dict(matches)
