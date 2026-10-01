"""Deterministic field extraction — T021.

Only what can be read reliably, and only with a locator attached. Email
addresses, phone numbers, dates, employer and institution names, section
boundaries: these have shape, and shape can be matched without a model.

Everything read here arrives ``confirmed=False``. It is a proposal the user
checks, not a fact. The distinction matters downstream — an unconfirmed value
never reaches an outbound document — and it is what makes this safe to run
without asking.

What this module deliberately does *not* do is infer. It will not guess that a
capitalised line is a job title, or that two dates near each other belong to
the same role. Those become *candidates* (:mod:`jarvis.documents.extract.prefill`)
which the user confirms or replaces, and a candidate the user edits is recorded
as typed, never as extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.documents.extract.sections import Section, by_name
from jarvis.documents.extract.text import ExtractedDocument, Locator

# ------------------------------------------------------------------ patterns

EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Deliberately strict: it wants an international prefix or a long run of digits
# with separators. Loose phone patterns match years, postcodes and ID numbers,
# and a wrong phone number on an application is worse than a missing one.
PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{1,4}\)[\s.\-]?)?"
    r"\d{2,4}(?:[\s.\-]\d{2,4}){1,4}(?![\w.])"
)

URL = re.compile(r"\bhttps?://[^\s<>\"')\]]+", re.IGNORECASE)
LINKEDIN = re.compile(r"\b(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.IGNORECASE)

MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
PRESENT = r"present|current|to date|now|ongoing|heute|aktuell"

#: One date, in the three forms CVs actually use: ``March 2019``, ``03/2019``,
#: and a bare ``2019``. The numeric-month branch is not optional decoration —
#: without it ``06/2017 - 02/2019`` matches nothing at all, because the year
#: alone cannot absorb the ``06/`` in front of it.
_DATE = (
    rf"(?:(?:{MONTHS})[\s.,\-]+|(?:0?[1-9]|1[0-2])\s*[./\-]\s*)?(?:19|20)\d{{2}}"
)

#: "March 2019 - Present", "03/2019 — 12/2021", "2019-2021"
DATE_RANGE = re.compile(
    rf"(?P<from>{_DATE})"
    rf"\s*(?:-|–|—|to|bis|until)\s*"
    rf"(?P<to>{_DATE}|{PRESENT})",
    re.IGNORECASE,
)

YEAR = re.compile(r"\b(?:19|20)\d{2}\b")

#: Legal-form suffixes are the most reliable employer signal there is.
ORG_SUFFIX = re.compile(
    r"\b(?:GmbH|AG|SE|KG|mbH|Ltd\.?|Limited|LLC|L\.L\.C\.|Inc\.?|Corp\.?|Corporation|"
    r"PLC|plc|Pvt\.?|Private Limited|Pty|S\.A\.|B\.V\.|N\.V\.|S\.p\.A\.|Co\.|Company|"
    r"Group|Holdings?|Industries|Technologies|Solutions|Systems|Services)\b"
)

INSTITUTION = re.compile(
    r"\b(?:University|Universität|Universidad|Institute|Institut|College|Politechnic|"
    r"Polytechnic|Academy|Akademie|School of|Faculty of|Hochschule|UET|NUST|COMSATS)\b",
    re.IGNORECASE,
)

DEGREE = re.compile(
    r"\b(?:Ph\.?D|Doctorate|Doctoral|M\.?Sc|MSc|M\.?S\.?|Master(?:'s)?|MBA|M\.?Eng|MEng|"
    r"B\.?Sc|BSc|B\.?S\.?|Bachelor(?:'s)?|B\.?Eng|BEng|B\.?E\.?|B\.?Tech|M\.?Tech|"
    r"Diploma|Associate|Matriculation|Intermediate|FSc|HSSC|SSC)\b",
    re.IGNORECASE,
)

LANGUAGE_TEST = re.compile(
    r"\b(?P<test>IELTS|TOEFL|PTE|Duolingo|DELF|DALF|TestDaF|DSH|Goethe|TELC|CEFR)\b"
    r"[^\n]{0,40}?(?P<score>\b(?:[A-C][12]|\d{1,3}(?:\.\d)?)\b)",
    re.IGNORECASE,
)


# -------------------------------------------------------------------- model


@dataclass(frozen=True, slots=True)
class Extracted:
    """One value read from a document, with where it came from."""

    field_path: str
    value: str
    locator: Locator
    #: How much this particular rule is trusted, 0.0-1.0. Used to order what
    #: the user is asked to confirm first, never to decide anything on its own.
    confidence: float
    rule: str


def _unique(found: list[Extracted]) -> list[Extracted]:
    """Drop repeats of the same value for the same path, keeping the first."""
    seen: set[tuple[str, str]] = set()
    out: list[Extracted] = []
    for item in found:
        key = (item.field_path, item.value.lower())
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ------------------------------------------------------------------ contact


def extract_contact(document: ExtractedDocument, sections: list[Section]) -> list[Extracted]:
    """Email, phone, links, and the name.

    Restricted to the header and any contact section where one exists. A CV
    that lists a referee's email further down must not have it read as the
    candidate's own.
    """
    regions = by_name(sections, "header") + by_name(sections, "contact")
    window = (0, regions[-1].end) if regions else (0, min(len(document.text), 1200))

    found: list[Extracted] = []

    for index, (match, locator) in enumerate(document.find(EMAIL)):
        if not (window[0] <= match.start() < window[1]):
            continue
        found.append(Extracted(
            field_path="basics.email" if index == 0 else f"basics.email_alt[{index - 1}]",
            value=match.group(0).lower(),
            locator=locator,
            confidence=0.95 if index == 0 else 0.6,
            rule="email-pattern",
        ))

    phones = [
        (m, loc) for m, loc in document.find(PHONE)
        if window[0] <= m.start() < window[1] and _looks_like_phone(m.group(0))
    ]
    for index, (match, locator) in enumerate(phones[:2]):
        found.append(Extracted(
            field_path="basics.phone" if index == 0 else "basics.phone_alt",
            value=" ".join(match.group(0).split()),
            locator=locator,
            confidence=0.8 if match.group(0).strip().startswith("+") else 0.55,
            rule="phone-pattern",
        ))

    for match, locator in document.find(LINKEDIN):
        found.append(Extracted("basics.profiles[0].url", match.group(0), locator, 0.9,
                               "linkedin-pattern"))
        break

    name = _extract_name(document, sections)
    if name is not None:
        found.append(name)

    return _unique(found)


def _looks_like_phone(raw: str) -> bool:
    """Reject the year ranges and ID numbers a loose pattern would swallow."""
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return False
    # A bare four-digit group is a year, not a number anyone can be called on.
    return not re.fullmatch(r"(?:19|20)\d{2}", digits)


def _extract_name(document: ExtractedDocument, sections: list[Section]) -> Extracted | None:
    """The candidate's name, read from the top of the document.

    Convention does the work: a CV opens with the person's name on its own
    line, above the contact details. Anything that fails these checks is left
    for the user to type, because a wrong name on an application is
    unrecoverable.
    """
    header = by_name(sections, "header")
    region_end = header[0].end if header else min(len(document.text), 400)

    offset = 0
    for line in document.text[:region_end].splitlines(keepends=True):
        stripped = line.strip()
        start = offset
        offset += len(line)

        if not stripped or len(stripped) > 60:
            continue
        if EMAIL.search(stripped) or URL.search(stripped) or YEAR.search(stripped):
            continue
        if any(ch.isdigit() for ch in stripped):
            continue
        words = stripped.replace(",", " ").split()
        if not 2 <= len(words) <= 5:
            continue
        # Title Case or ALL CAPS, every word alphabetic.
        if not all(w.replace("-", "").replace("'", "").isalpha() for w in words):
            continue
        if not (stripped.isupper() or all(w[0].isupper() for w in words)):
            continue

        value = " ".join(w.capitalize() for w in words) if stripped.isupper() else stripped
        return Extracted(
            field_path="basics.name",
            value=value,
            locator=document.locate(start, start + len(stripped)),
            confidence=0.75,
            rule="header-name-line",
        )
    return None


# ------------------------------------------------------------------ history


def extract_experience(document: ExtractedDocument, sections: list[Section]) -> list[Extracted]:
    """Employers and date ranges from the experience section.

    Each detected date range opens one entry: dates anchor a role more reliably
    than titles do, because every CV writes them and they are machine-readable.

    The employer is then searched for **inside that entry's own region**, not
    within a radius of the date. A radius picks whichever employer line happens
    to be fewer characters away, which for a compact entry is routinely the
    *next* job's employer — so the first role in a CV gets labelled with the
    second company's name. Bounding the search to the text between the previous
    entry's dates and this one's makes that impossible, and preferring the last
    match above the date follows the near-universal convention of writing the
    employer before the period worked.
    """
    found: list[Extracted] = []
    index = 0

    for section in by_name(sections, "experience"):
        matches = list(DATE_RANGE.finditer(section.text))
        for position, match in enumerate(matches):
            absolute = section.start + match.start()
            locator = document.locate(absolute, absolute + len(match.group(0)))

            start_date = _normalise_date(match.group("from"))
            end_raw = match.group("to")
            end_date = (
                "present"
                if re.fullmatch(PRESENT, end_raw.strip(), re.IGNORECASE)
                else _normalise_date(end_raw)
            )
            if start_date:
                found.append(
                    Extracted(f"work[{index}].startDate", start_date, locator, 0.85, "date-range")
                )
            if end_date:
                found.append(
                    Extracted(f"work[{index}].endDate", end_date, locator, 0.85, "date-range")
                )

            above_from = matches[position - 1].end() if position else 0
            below_to = (
                matches[position + 1].start() if position + 1 < len(matches) else len(section.text)
            )
            employer = _employer_for_entry(
                section, above=(above_from, match.start()), below=(match.end(), below_to)
            )
            if employer:
                text, local_offset = employer
                where = section.start + local_offset
                found.append(
                    Extracted(
                        f"work[{index}].employer",
                        text,
                        document.locate(where, where + len(text)),
                        0.7,
                        "legal-form-suffix",
                    )
                )
            index += 1

    return _unique(found)


def _employer_for_entry(
    section: Section, *, above: tuple[int, int], below: tuple[int, int]
) -> tuple[str, int] | None:
    """The employer line for one entry: last one above the dates, else first below.

    Both regions are bounded by the neighbouring entries, so an employer can
    never be borrowed from an adjacent role.
    """
    candidates = _lines_matching(section.text, ORG_SUFFIX, *above)
    if candidates:
        return candidates[-1]

    candidates = _lines_matching(section.text, ORG_SUFFIX, *below)
    return candidates[0] if candidates else None


def _lines_matching(
    text: str, pattern: re.Pattern[str], start: int, end: int
) -> list[tuple[str, int]]:
    """Every line in ``text[start:end]`` matching ``pattern``, with its offset."""
    out: list[tuple[str, int]] = []
    local = start
    for line in text[start:end].splitlines(keepends=True):
        stripped = line.strip()
        if stripped and pattern.search(stripped):
            out.append((stripped[:120], local + (len(line) - len(line.lstrip()))))
        local += len(line)
    return out


def extract_education(document: ExtractedDocument, sections: list[Section]) -> list[Extracted]:
    """Institutions, degrees and graduation years, grouped into qualifications.

    One qualification is usually written across two or three lines — the
    institution on one, the degree and year on the next. So lines accumulate
    into the current entry, and the index advances only when a field arrives
    that the current entry already has. Advancing per line instead would split
    a single degree into ``education[0].institution`` and
    ``education[1].studyType``, and the sufficiency rule for a scholarship —
    which needs institution, studyType and endDate *together* — would then
    never be satisfied by a perfectly ordinary CV.
    """
    found: list[Extracted] = []
    index = 0
    entry: dict[str, Extracted] = {}

    def flush() -> None:
        nonlocal index, entry
        if entry:
            found.extend(entry.values())
            index += 1
            entry = {}

    for section in by_name(sections, "education"):
        local = 0
        for line in section.text.splitlines(keepends=True):
            stripped = line.strip()
            start = section.start + local
            local += len(line)
            if not stripped:
                continue

            institution = INSTITUTION.search(stripped)
            degree = DEGREE.search(stripped)
            years = YEAR.findall(stripped)
            if not (institution or degree):
                continue

            # A field we already hold means this line belongs to the next one.
            if (institution and "institution" in entry) or (degree and "studyType" in entry):
                flush()

            if institution:
                entry["institution"] = Extracted(
                    f"education[{index}].institution", stripped[:120],
                    document.locate(start, start + len(stripped)), 0.7, "institution-keyword",
                )
            if degree:
                entry["studyType"] = Extracted(
                    f"education[{index}].studyType", degree.group(0),
                    document.locate(start + degree.start(), start + degree.end()),
                    0.75, "degree-keyword",
                )
            if years and "endDate" not in entry:
                entry["endDate"] = Extracted(
                    f"education[{index}].endDate", max(years),
                    document.locate(start, start + len(stripped)), 0.65, "year-in-line",
                )
        flush()

    return _unique(found)


def extract_languages(document: ExtractedDocument, sections: list[Section]) -> list[Extracted]:
    """Language test results — the field that most often blocks an application."""
    found: list[Extracted] = []
    regions = by_name(sections, "languages") or sections

    for index, section in enumerate(regions):
        for match in LANGUAGE_TEST.finditer(section.text):
            absolute = section.start + match.start()
            found.append(Extracted(
                f"languages[{index}].test",
                f"{match.group('test').upper()} {match.group('score')}",
                document.locate(absolute, absolute + len(match.group(0))),
                0.8, "language-test-pattern",
            ))
    return _unique(found)


# ------------------------------------------------------------------ helpers


_MONTH_NUMBERS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _normalise_date(raw: str) -> str | None:
    """``March 2019`` and ``03/2019`` both become ``2019-03``; a bare year stays a year.

    Precision is never invented. A CV that says "2019" yields "2019", not
    "2019-01-01", because a made-up month is a fabricated value like any other.
    """
    text = raw.strip().lower()
    year = YEAR.search(text)
    if not year:
        return None

    for prefix, number in _MONTH_NUMBERS.items():
        if prefix in text:
            return f"{year.group(0)}-{number}"

    numeric = re.search(r"\b(0?[1-9]|1[0-2])\s*[./\-]\s*(?:19|20)\d{2}", text)
    if numeric:
        return f"{year.group(0)}-{int(numeric.group(1)):02d}"

    return year.group(0)




# ------------------------------------------------------------------- driver


def extract_all(document: ExtractedDocument, sections: list[Section]) -> list[Extracted]:
    """Everything the deterministic layer can read, in one pass."""
    return (
        extract_contact(document, sections)
        + extract_experience(document, sections)
        + extract_education(document, sections)
        + extract_languages(document, sections)
    )
