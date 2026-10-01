"""The canonical profile field paths — T026's definition tables as data.

A ``profile_record.field_path`` is a dotted path into a JSON-Resume-shaped
profile, with ``[n]`` for repeated entries::

    basics.name
    basics.location.countryCode
    work[0].position
    education[1].institution

Definitions below use ``[]`` to mean "any index", so one row describes
``work[0].position`` and ``work[7].position`` alike. :func:`matches` does the
comparison; nothing else needs to know the syntax.

Three separate questions are answered from three separate tables, and keeping
them separate is deliberate:

:data:`COMPLETENESS`
    A weighted progress indicator. It gates nothing. Publishing the missing
    fields and their weights — which honesty requires — would otherwise hand
    anyone below a threshold a precise map of what to type to get past it, and
    a value typed to clear a gate arrives downstream wearing the same
    provenance as one read from a document.
:data:`MINIMUM_VIABLE_PROFILE`
    A short checklist of named items that unlocks scheduled discovery. Minutes,
    not hours.
:data:`SUFFICIENCY_RULES`
    What an application to *this* opportunity would actually need. Decided per
    opportunity, so filling unrelated fields changes no verdict.

Restricted documents contribute zero weight and appear in no sufficiency rule:
somebody who never uploads a passport can still reach 100% and apply to
everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COMPLETENESS_VERSION = 1
MVP_VERSION = 1
SUFFICIENCY_VERSION = 1

_INDEX = re.compile(r"\[\d+\]")


def normalise(field_path: str) -> str:
    """Collapse concrete indices to ``[]`` so a path can be matched to a rule."""
    return _INDEX.sub("[]", field_path)


def matches(field_path: str, pattern: str) -> bool:
    """Does a concrete path satisfy a definition pattern?"""
    return normalise(field_path) == normalise(pattern)


def index_of(field_path: str) -> int | None:
    """The first repeated-entry index in a path, if it has one."""
    found = re.search(r"\[(\d+)\]", field_path)
    return int(found.group(1)) if found else None


# --------------------------------------------------------------- completeness


@dataclass(frozen=True, slots=True)
class CompletenessField:
    path: str
    weight: float
    label: str
    #: A repeated group counts once when at least one entry is present, so a
    #: person with four jobs is not scored higher than one with two.
    repeated: bool = False


COMPLETENESS: tuple[CompletenessField, ...] = (
    CompletenessField("basics.name", 8, "Your full name"),
    CompletenessField("basics.email", 8, "Email address"),
    CompletenessField("basics.phone", 4, "Phone number"),
    CompletenessField("basics.location.city", 3, "City you live in"),
    CompletenessField("basics.location.countryCode", 4, "Country you live in"),
    CompletenessField("basics.summary", 4, "A short professional summary"),
    CompletenessField("identity.citizenship", 8, "Citizenship"),
    CompletenessField("identity.tax_residence", 6, "Country you pay tax in"),
    CompletenessField("work[].employer", 9, "Employer name", repeated=True),
    CompletenessField("work[].position", 9, "Job title", repeated=True),
    CompletenessField("work[].startDate", 7, "Start date", repeated=True),
    CompletenessField("work[].endDate", 4, "End date (or present)", repeated=True),
    CompletenessField("work[].summary", 4, "What you did there", repeated=True),
    CompletenessField("education[].institution", 7, "Institution", repeated=True),
    CompletenessField("education[].area", 4, "Field of study", repeated=True),
    CompletenessField("education[].studyType", 4, "Degree type", repeated=True),
    CompletenessField("education[].endDate", 4, "Graduation date", repeated=True),
    CompletenessField("skills[].name", 5, "Skills", repeated=True),
    CompletenessField("languages[].language", 3, "Languages", repeated=True),
    CompletenessField("languages[].test", 3, "Language test score", repeated=True),
    CompletenessField("certificates[].name", 3, "Certifications", repeated=True),
    CompletenessField("x_achievements[].statement", 4, "Achievements", repeated=True),
)

TOTAL_WEIGHT = sum(f.weight for f in COMPLETENESS)


# ------------------------------------------------------- minimum viable profile


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    key: str
    label: str
    rationale: str
    #: Satisfied when **every** path in one of these groups is confirmed.
    #: Groups are alternatives; paths within a group are conjunctive.
    any_of: tuple[tuple[str, ...], ...]


MINIMUM_VIABLE_PROFILE: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        key="identity",
        label="Your name and a way to reach you",
        rationale="A document cannot be addressed or signed without these.",
        any_of=(("basics.name", "basics.email"),),
    ),
    ChecklistItem(
        key="citizenship",
        label="Your citizenship",
        rationale="Visa eligibility and sponsorship requirements are decided by it.",
        any_of=(("identity.citizenship",),),
    ),
    ChecklistItem(
        key="tax_residence",
        label="Where you pay tax",
        rationale="Remote roles are evaluated against your own tax residence, not the employer's.",
        any_of=(("identity.tax_residence",),),
    ),
    ChecklistItem(
        key="history",
        label="One job or one qualification, with dates",
        rationale="Without a dated record there is nothing an application can honestly claim.",
        any_of=(
            ("work[].employer", "work[].position", "work[].startDate"),
            ("education[].institution", "education[].studyType", "education[].endDate"),
        ),
    ),
)


# ---------------------------------------------------------------- sufficiency


@dataclass(frozen=True, slots=True)
class SufficiencyRule:
    """What one document type needs before it can be written honestly.

    ``conditional_on`` restricts the rule to opportunities whose parsed
    requirements match — a language certificate is required only where the
    posting states one. An opportunity whose requirements could not be parsed
    confidently does not match any conditional rule, which is why an
    unparseable posting yields ``not_evaluated`` rather than ``sufficient``.
    """

    document_type: str
    required_field_paths: tuple[str, ...]
    label: str
    rationale: str
    role_family: str | None = None
    kind: str | None = None
    conditional_on: dict[str, object] | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


SUFFICIENCY_RULES: tuple[SufficiencyRule, ...] = (
    SufficiencyRule(
        document_type="cv",
        required_field_paths=("basics.name", "basics.email"),
        label="Contact details",
        rationale="A CV with no way to reply is not an application.",
    ),
    SufficiencyRule(
        document_type="cv",
        required_field_paths=("work[].employer", "work[].position", "work[].startDate"),
        label="At least one dated role",
        rationale="Experience claims must trace to a dated employment record.",
        kind="job",
    ),
    SufficiencyRule(
        document_type="cv",
        required_field_paths=(
            "education[].institution",
            "education[].studyType",
            "education[].endDate",
        ),
        label="A completed qualification",
        rationale="Scholarship applications are assessed on academic record.",
        kind="scholarship",
    ),
    SufficiencyRule(
        document_type="cover_letter",
        required_field_paths=("basics.name", "basics.email", "work[].summary"),
        label="Something to say about your work",
        rationale="A cover letter with no described experience has to invent one.",
        kind="job",
    ),
    SufficiencyRule(
        document_type="cv",
        required_field_paths=("languages[].test",),
        label="A language test result",
        rationale="This posting states a language requirement, so the score must be on record.",
        conditional_on={"requires_language_test": True},
        tags=("conditional",),
    ),
    SufficiencyRule(
        document_type="cv",
        required_field_paths=("certificates[].name",),
        label="The professional certification this posting requires",
        rationale="The posting names a certification; claiming it needs a record of it.",
        conditional_on={"requires_certification": True},
        tags=("conditional",),
    ),
    SufficiencyRule(
        document_type="motivation_letter",
        required_field_paths=("basics.summary", "education[].area"),
        label="Academic direction",
        rationale="A motivation letter must say what you study and why, from your own record.",
        kind="scholarship",
    ),
    SufficiencyRule(
        document_type="statement_of_purpose",
        required_field_paths=(
            "basics.summary",
            "education[].institution",
            "education[].area",
            "x_achievements[].statement",
        ),
        label="Research background",
        rationale="A statement of purpose rests on a documented academic record.",
        kind="scholarship",
        conditional_on={"requires_statement_of_purpose": True},
        tags=("conditional",),
    ),
)


#: Document types an application needs, by opportunity kind, when the posting
#: does not say. Overridden by ``opportunity.requirements.document_types``.
DEFAULT_DOCUMENT_TYPES: dict[str, tuple[str, ...]] = {
    "job": ("cv", "cover_letter"),
    "scholarship": ("cv", "motivation_letter"),
}


#: Field paths that never contribute to completeness and never appear in a
#: sufficiency rule. Their contents also never enter a prompt or an outbound
#: document.
RESTRICTED_PATHS: frozenset[str] = frozenset(
    {
        "identity.national_id",
        "identity.passport_number",
        "identity.date_of_birth",
        "x_preferences.current_compensation",
        "financial.bank",
        "financial.tax_return",
    }
)


def is_restricted(field_path: str) -> bool:
    normalised = normalise(field_path)
    return any(normalised.startswith(p) for p in RESTRICTED_PATHS)
