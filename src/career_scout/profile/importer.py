"""Importing an existing master CV — T027.

The legacy *Job Finder* kept a hand-authored ``master_cv.json`` in a
JSON-Resume shape with extensions, carrying a ``verified`` flag and a source
reference on most entries. That file is the richest profile this project has,
and it is imported rather than retyped.

Two rules govern the mapping.

**``verified`` decides ``confirmed``.** An entry the legacy project marked
verified against a document arrives confirmed. Everything else arrives as a
proposal for the user to check. ``"partial"`` counts as unverified, because a
partly-verified claim is not one an application may rest on.

**An expired credential does not support a claim.** The master CV's PTE
Academic score lapsed in October 2025. Importing it as a confirmed language
test would let it satisfy a posting's live language requirement, which is
exactly the sort of quiet falsehood the whole provenance apparatus exists to
prevent. So the score is imported *unconfirmed*, its expiry date is imported
confirmed, and the user is told.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from career_scout.profile import records


@dataclass(slots=True)
class ImportReport:
    """What was brought in, and what the user needs to know about it."""

    imported: int = 0
    confirmed: int = 0
    proposals: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.imported} fields imported — {self.confirmed} confirmed, "
            f"{self.proposals} awaiting your check"
        )


def _verified(entry: dict[str, Any]) -> bool:
    """``True`` only for a literal True. ``"partial"`` is not verification."""
    return entry.get("verified") is True


def import_master_cv(
    conn: sqlite3.Connection,
    path: Path,
    *,
    today: date | None = None,
) -> ImportReport:
    """Read ``master_cv.json`` into profile records."""
    today = today or date.today()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    report = ImportReport()

    def put(field_path: str, value: Any, *, confirmed: bool) -> None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return
        records.add(conn, field_path, value, confirmed=confirmed)
        report.imported += 1
        if confirmed:
            report.confirmed += 1
        else:
            report.proposals += 1

    _import_basics(data, put)
    _import_identity(data, put, report)
    _import_work(data, put)
    _import_education(data, put)
    _import_skills(data, put)
    _import_preferences(data, put, report)
    _import_languages(data, put, report, today)
    _import_certificates(data, put)
    _import_achievements(data, put)

    return report


# ------------------------------------------------------------------ sections


def _import_basics(data: dict[str, Any], put: Any) -> None:
    basics = data.get("basics", {})
    # Identity and contact details come straight from the person, and the
    # legacy project validated them against documents.
    put("basics.name", basics.get("name"), confirmed=True)
    put("basics.email", basics.get("email"), confirmed=True)
    put("basics.phone", basics.get("phone"), confirmed=True)
    put("basics.phone_alt", basics.get("phone_alt"), confirmed=False)
    put("basics.label", basics.get("label"), confirmed=False)

    # The summary is prose someone wrote, not a fact read from a document.
    put("basics.summary", basics.get("summary"), confirmed=False)

    location = basics.get("location", {})
    put("basics.location.city", location.get("current_city") or location.get("city"),
        confirmed=True)
    put("basics.location.region", location.get("region"), confirmed=True)
    put("basics.location.countryCode", location.get("countryCode"), confirmed=True)


def _import_identity(data: dict[str, Any], put: Any, report: ImportReport) -> None:
    mobility = data.get("x_mobility", {})

    citizenship = mobility.get("citizenship") or []
    if citizenship:
        put("identity.citizenship", ",".join(citizenship), confirmed=True)

    # Tax residence is not in the legacy schema and is not the same question as
    # where someone lives or holds a passport — it decides how a remote salary
    # is taxed. Guessing it would put a fabricated value behind every remote
    # projection, so it is left for the user.
    report.warnings.append(
        "Tax residence is not in the master CV and was not inferred from your "
        "citizenship or address — it changes every remote-role calculation. "
        "Set it in Settings before the first run."
    )

    right_to_work = mobility.get("right_to_work_without_sponsorship") or []
    if right_to_work:
        put("identity.right_to_work", ",".join(right_to_work), confirmed=True)

    # Passport details are restricted: never imported into the profile.
    if mobility.get("passport"):
        report.skipped.append("passport details (restricted — stored as a document, never read)")

    if data.get("x_preferences", {}).get("current_compensation"):
        report.skipped.append("current compensation (used only to evaluate offers)")


def _import_preferences(data: dict[str, Any], put: Any, report: ImportReport) -> None:
    """Target roles and pay expectation — what the scorer ranks against.

    Target roles are imported **unconfirmed**. The master CV records them as
    derived from the CV title, the CDR and a cover letter, which makes them a
    reading of what somebody seems to want rather than a statement of it. The
    career-alignment component reports UNSCORED until the user confirms the
    list, which is the correct answer to "how close is this title to what you
    are looking for" when nobody has said what they are looking for.
    """
    preferences = data.get("x_preferences", {})

    roles = preferences.get("target_roles") or []
    for index, role in enumerate(roles):
        put(f"preferences.target_roles[{index}]", role, confirmed=False)
    if roles:
        report.warnings.append(
            f"{len(roles)} target roles were read from your CV and cover letter rather "
            f"than stated by you. Confirm or edit them: until you do, no opportunity "
            f"can be scored on how well its title matches what you want."
        )

    # A salary expectation is a decision, not a fact to be read out of a
    # document. The legacy profile left it open (x_open_questions.Q9) and every
    # one of the 2,305 stored assessments reported salary UNSCORED because of
    # it. Inventing a figure here would silently rank every posting against a
    # number the user never chose.
    expectation = preferences.get("salary_expectation")
    if isinstance(expectation, dict) and expectation.get("minimum") is not None:
        put("preferences.salary_minimum", expectation["minimum"], confirmed=False)
        put("preferences.salary_currency", expectation.get("currency"), confirmed=False)
    else:
        report.skipped.append(
            "salary expectation (not stated; the salary component reports UNSCORED "
            "until you set one in Settings)"
        )


def _import_work(data: dict[str, Any], put: Any) -> None:
    for index, entry in enumerate(data.get("work", [])):
        confirmed = _verified(entry)
        put(f"work[{index}].employer", entry.get("name"), confirmed=confirmed)
        put(f"work[{index}].position", entry.get("position"), confirmed=confirmed)
        put(f"work[{index}].startDate", entry.get("startDate"), confirmed=confirmed)
        put(f"work[{index}].endDate", entry.get("endDate"), confirmed=confirmed)
        put(f"work[{index}].location", entry.get("location"), confirmed=confirmed)
        put(f"work[{index}].summary", entry.get("summary"), confirmed=False)

        highlights = entry.get("highlights") or []
        if highlights:
            put(f"work[{index}].highlights", " • ".join(str(h) for h in highlights),
                confirmed=False)


def _import_education(data: dict[str, Any], put: Any) -> None:
    for index, entry in enumerate(data.get("education", [])):
        confirmed = _verified(entry)
        put(f"education[{index}].institution", entry.get("institution"), confirmed=confirmed)
        put(f"education[{index}].studyType", entry.get("studyType"), confirmed=confirmed)
        put(f"education[{index}].area", entry.get("area"), confirmed=confirmed)
        put(f"education[{index}].startDate", entry.get("startDate"), confirmed=confirmed)
        put(f"education[{index}].endDate", entry.get("endDate"), confirmed=confirmed)
        if entry.get("score") is not None:
            put(f"education[{index}].score", f"{entry['score']} {entry.get('score_scale', '')}"
                .strip(), confirmed=confirmed)


def _import_skills(data: dict[str, Any], put: Any) -> None:
    for index, entry in enumerate(data.get("skills", [])):
        # A self-declared skill is a claim, not a verified fact.
        put(f"skills[{index}].name", entry.get("name"), confirmed=False)
        keywords = entry.get("keywords") or []
        if keywords:
            put(f"skills[{index}].keywords", ", ".join(keywords), confirmed=False)


def _import_languages(
    data: dict[str, Any], put: Any, report: ImportReport, today: date
) -> None:
    for index, entry in enumerate(data.get("languages", [])):
        put(f"languages[{index}].language", entry.get("language"), confirmed=True)

        fluency = entry.get("fluency")
        if isinstance(fluency, str):
            put(f"languages[{index}].fluency", fluency, confirmed=False)

        test = entry.get("test")
        if not isinstance(test, dict):
            continue

        name = test.get("name")
        overall = test.get("overall")
        if name is None or overall is None:
            continue

        valid_until = test.get("valid_until")
        expired = _is_expired(valid_until, today) or \
            str(test.get("validity_status", "")).upper() == "EXPIRED"

        # An expired score is imported so the user can see it, but unconfirmed
        # so it cannot satisfy a posting's live language requirement.
        put(f"languages[{index}].test", f"{name} {overall}", confirmed=not expired)
        if valid_until:
            put(f"languages[{index}].test_valid_until", valid_until, confirmed=True)

        if expired:
            report.warnings.append(
                f"Your {name} score ({overall}) lapsed on {valid_until}. It is on file but "
                f"is not treated as a current result, so postings that require a language "
                f"test will show as insufficient until you retake it."
            )


def _import_certificates(data: dict[str, Any], put: Any) -> None:
    for index, entry in enumerate(data.get("certificates", [])):
        confirmed = _verified(entry)
        put(f"certificates[{index}].name", entry.get("name"), confirmed=confirmed)
        put(f"certificates[{index}].issuer", entry.get("issuer"), confirmed=confirmed)
        put(f"certificates[{index}].date", entry.get("date"), confirmed=confirmed)


def _import_achievements(data: dict[str, Any], put: Any) -> None:
    for index, entry in enumerate(data.get("x_achievements", [])):
        if not entry.get("usable_in_applications", False):
            continue
        put(f"x_achievements[{index}].statement", entry.get("statement"),
            confirmed=_verified(entry))


def _is_expired(valid_until: str | None, today: date) -> bool:
    if not valid_until:
        return False
    try:
        return date.fromisoformat(valid_until[:10]) < today
    except ValueError:
        return False
