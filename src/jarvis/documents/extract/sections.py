"""Splitting a CV into its sections — T020.

CVs have no schema, but they have a strong convention: a short line, often
capitalised, naming what follows. That convention is what this module reads.

Three signals, all cheap and all inspectable:

* the line matches a known heading vocabulary (``EXPERIENCE``, ``Berufserfahrung``)
* the line is short, and is not a sentence
* it is visually set apart — all-caps, title case, or followed by a rule

Nothing here guesses. A line that is not confidently a heading is body text,
and a document with no recognised headings yields one ``unknown`` section
covering everything — which downstream treats as low confidence rather than as
a parsed CV.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.documents.extract.text import ExtractedDocument, Locator

#: Canonical section name -> the headings that introduce it. English first,
#: then the languages the enabled countries actually produce CVs in.
HEADINGS: dict[str, tuple[str, ...]] = {
    "experience": (
        "experience", "work experience", "professional experience", "employment",
        "employment history", "work history", "career history", "career summary",
        "professional background", "relevant experience", "berufserfahrung",
        "beruflicher werdegang", "erfahrung",
    ),
    "education": (
        "education", "academic background", "academic qualifications", "qualifications",
        "educational qualifications", "academic record", "ausbildung", "studium",
        "bildung",
    ),
    "skills": (
        "skills", "technical skills", "core competencies", "competencies", "expertise",
        "areas of expertise", "key skills", "technical competencies", "kenntnisse",
        "fähigkeiten", "kompetenzen",
    ),
    "certifications": (
        "certifications", "certificates", "licenses", "licences",
        "professional certifications", "accreditations", "zertifikate",
    ),
    "publications": (
        "publications", "papers", "research", "research experience",
        "conference papers", "publikationen",
    ),
    "projects": ("projects", "key projects", "selected projects", "projekte"),
    "languages": ("languages", "language skills", "sprachen", "sprachkenntnisse"),
    "summary": (
        "summary", "profile", "professional summary", "career objective", "objective",
        "about me", "personal statement", "profil", "kurzprofil",
    ),
    "awards": ("awards", "honours", "honors", "achievements", "accomplishments",
               "auszeichnungen"),
    "references": ("references", "referees", "referenzen"),
    "contact": ("contact", "contact details", "personal details", "kontakt",
                "persönliche daten"),
    "interests": ("interests", "hobbies", "activities", "interessen"),
}

_LOOKUP: dict[str, str] = {
    phrase: name for name, phrases in HEADINGS.items() for phrase in phrases
}

#: A heading is short. Anything longer is a sentence that happens to start with
#: a heading word.
MAX_HEADING_WORDS = 5
MAX_HEADING_CHARS = 48

_DECORATION = re.compile(r"^[\s\-_=~•·*#|:>\d.]+|[\s\-_=~•·*#|:<]+$")
_SENTENCE_END = re.compile(r"[.!?;]\s*$")


@dataclass(frozen=True, slots=True)
class Section:
    """One region of the document, with everything needed to cite it."""

    name: str            # canonical name, or 'unknown'
    heading: str | None  # the line as it actually appeared
    start: int           # offset into ExtractedDocument.text
    end: int
    text: str
    locator: Locator

    @property
    def is_recognised(self) -> bool:
        return self.name != "unknown"


def canonical_heading(line: str) -> str | None:
    """The canonical section a line introduces, or None if it is not a heading."""
    cleaned = _DECORATION.sub("", line).strip()
    if not cleaned or len(cleaned) > MAX_HEADING_CHARS:
        return None
    if _SENTENCE_END.search(cleaned):
        return None
    if len(cleaned.split()) > MAX_HEADING_WORDS:
        return None

    lowered = cleaned.lower()
    if lowered in _LOOKUP:
        return _LOOKUP[lowered]

    # "Professional Experience (2019-2026)" and similar trailing parentheticals.
    trimmed = re.sub(r"\s*[\(\[].*$", "", lowered).strip()
    if trimmed in _LOOKUP:
        return _LOOKUP[trimmed]

    # Set-apart formatting is corroborating evidence, not proof on its own: it
    # only promotes a line whose words already appear in the vocabulary.
    if cleaned.isupper() and len(cleaned) > 2:
        for phrase, name in _LOOKUP.items():
            if phrase in lowered:
                return name
    return None


def split(document: ExtractedDocument) -> list[Section]:
    """Divide a document into sections, in document order.

    Text before the first recognised heading becomes a ``header`` section —
    that is where the name, email and phone almost always live, so it is worth
    naming rather than discarding.
    """
    if not document.text.strip():
        return []

    boundaries: list[tuple[int, int, str, str]] = []  # start, end, canonical, raw
    offset = 0
    for line in document.text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            name = canonical_heading(stripped)
            if name is not None:
                boundaries.append((offset, offset + len(line), name, stripped))
        offset += len(line)

    if not boundaries:
        return [_section(document, "unknown", None, 0, len(document.text))]

    sections: list[Section] = []
    first_start = boundaries[0][0]
    if document.text[:first_start].strip():
        sections.append(_section(document, "header", None, 0, first_start))

    for index, (_, body_start, name, raw) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(document.text)
        sections.append(_section(document, name, raw, body_start, end))

    return sections


def _section(
    document: ExtractedDocument, name: str, heading: str | None, start: int, end: int
) -> Section:
    return Section(
        name=name,
        heading=heading,
        start=start,
        end=end,
        text=document.text[start:end],
        locator=document.locate(start, min(end, start + 80)),
    )


def by_name(sections: list[Section], name: str) -> list[Section]:
    """Every section of one kind. A CV may legitimately have two."""
    return [s for s in sections if s.name == name]


def coverage(sections: list[Section]) -> float:
    """How much of the document fell inside a recognised section, 0.0-1.0.

    The honest confidence signal for segmentation: a CV whose headings were all
    recognised scores near 1.0, and one this module could not read scores near
    0.0 rather than pretending to a structure it did not find.
    """
    if not sections:
        return 0.0
    total = sum(s.end - s.start for s in sections)
    if total == 0:
        return 0.0
    recognised = sum(s.end - s.start for s in sections if s.is_recognised)
    return round(recognised / total, 3)
