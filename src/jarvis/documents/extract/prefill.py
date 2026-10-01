"""Candidates for the fields the deterministic layer cannot read — T023.

:mod:`jarvis.documents.extract.fields` reads what has shape. Job titles, role
summaries and skills have none, so they are offered as *candidates* drawn from
the document text: the user picks one, edits it, or types their own.

The rule that makes this safe is small and absolute. A candidate the user
**accepts unchanged** is recorded with the document and locator it came from,
because it is a value read from that document. A candidate the user **edits**,
or a field they type from scratch, is recorded with no document and no locator,
because nobody read it anywhere. :func:`record_choice` is the only way either
gets written, and it decides from the text alone.

Without that rule, the pre-fill is worse than nothing: it produces values
wearing document provenance that the document does not support, and everything
downstream — the claim trace, the QC check, the whole traceability guarantee —
is then resting on a lie told by a convenience feature.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from jarvis.documents.extract.fields import DATE_RANGE, ORG_SUFFIX, Extracted
from jarvis.documents.extract.sections import Section, by_name
from jarvis.documents.extract.text import ExtractedDocument, Locator
from jarvis.profile import records

#: Lines that are obviously not a value worth offering.
_NOISE = re.compile(r"^[\s\-_=~•·*#|>]+$")

#: Common job-title words, used only to rank candidates, never to decide.
_TITLE_HINT = re.compile(
    r"\b(?:engineer|manager|analyst|specialist|lead|head|director|officer|consultant|"
    r"coordinator|supervisor|executive|developer|scientist|designer|architect|intern|"
    r"associate|assistant|technician|planner|controller|administrator)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A suggestion for one field, with the text it was drawn from."""

    field_path: str
    value: str
    locator: Locator
    rank: float
    why: str

    def as_extracted(self) -> Extracted:
        return Extracted(self.field_path, self.value, self.locator, self.rank, "prefill")


def suggest_titles(document: ExtractedDocument, sections: list[Section]) -> list[Candidate]:
    """Job-title candidates: the line above each date range in the experience section.

    Convention again — employer, then title, then dates. The line immediately
    above the dates is the strongest candidate, and a title word in it raises
    the rank without being required.
    """
    out: list[Candidate] = []
    index = 0

    for section in by_name(sections, "experience"):
        for match in DATE_RANGE.finditer(section.text):
            preceding = _lines_before(section.text, match.start(), count=3)
            for distance, (line, offset) in enumerate(reversed(preceding)):
                if ORG_SUFFIX.search(line) or DATE_RANGE.search(line):
                    continue
                if not 3 <= len(line) <= 90:
                    continue
                absolute = section.start + offset
                out.append(
                    Candidate(
                        field_path=f"work[{index}].position",
                        value=line,
                        locator=document.locate(absolute, absolute + len(line)),
                        rank=(0.6 if _TITLE_HINT.search(line) else 0.35) - 0.05 * distance,
                        why="line above the dates in your experience section",
                    )
                )
            index += 1

    return _best_per_path(out)


def suggest_summaries(document: ExtractedDocument, sections: list[Section]) -> list[Candidate]:
    """Role-summary candidates: the prose beneath each date range."""
    out: list[Candidate] = []
    index = 0

    for section in by_name(sections, "experience"):
        matches = list(DATE_RANGE.finditer(section.text))
        for position, match in enumerate(matches):
            end = (
                matches[position + 1].start()
                if position + 1 < len(matches)
                else len(section.text)
            )
            body = section.text[match.end() : end]
            cleaned = " ".join(
                line.strip(" -•·*\t")
                for line in body.splitlines()
                if line.strip() and not _NOISE.match(line.strip())
            ).strip()
            if len(cleaned) >= 30:
                absolute = section.start + match.end()
                out.append(
                    Candidate(
                        field_path=f"work[{index}].summary",
                        value=cleaned[:600],
                        locator=document.locate(absolute, absolute + min(len(body), 200)),
                        rank=0.5,
                        why="the text beneath this role's dates",
                    )
                )
            index += 1

    return _best_per_path(out)


def suggest_skills(document: ExtractedDocument, sections: list[Section]) -> list[Candidate]:
    """Skill candidates: the comma- or bullet-separated items in the skills section."""
    out: list[Candidate] = []
    index = 0

    for section in by_name(sections, "skills"):
        local = 0
        for line in section.text.splitlines(keepends=True):
            # strip() first: the bullet set alone leaves the trailing newline
            # attached, and a skill named "Value Stream Mapping\n" never
            # matches the vocabulary it is meant to be compared against.
            stripped = line.strip().strip(" -•·*\t")
            start = section.start + local
            local += len(line)
            if not stripped or _NOISE.match(stripped):
                continue

            for part in re.split(r"[,;|/]|\s{2,}", stripped):
                item = part.strip().strip(" .")
                if not 2 <= len(item) <= 48 or item.lower().endswith(":"):
                    continue
                offset = section.text.find(item, start - section.start)
                absolute = section.start + offset if offset >= 0 else start
                out.append(
                    Candidate(
                        field_path=f"skills[{index}].name",
                        value=item,
                        locator=document.locate(absolute, absolute + len(item)),
                        rank=0.45,
                        why="listed in your skills section",
                    )
                )
                index += 1

    return out


def suggest_summary(document: ExtractedDocument, sections: list[Section]) -> list[Candidate]:
    """A professional-summary candidate, from the summary section if there is one."""
    for section in by_name(sections, "summary"):
        body = " ".join(section.text.split())
        if len(body) >= 40:
            return [
                Candidate(
                    field_path="basics.summary",
                    value=body[:800],
                    locator=document.locate(section.start, min(section.end, section.start + 200)),
                    rank=0.7,
                    why="your CV's own summary section",
                )
            ]
    return []


def suggest_all(document: ExtractedDocument, sections: list[Section]) -> list[Candidate]:
    return (
        suggest_summary(document, sections)
        + suggest_titles(document, sections)
        + suggest_summaries(document, sections)
        + suggest_skills(document, sections)
    )


# ----------------------------------------------------------------- recording


def record_choice(
    conn: sqlite3.Connection,
    candidate: Candidate,
    chosen_value: str,
    *,
    document_id: str,
) -> str:
    """Write what the user settled on, with honest provenance.

    Accepted unchanged, it came from the document and keeps its locator.
    Edited, it came from the user and keeps neither. The comparison ignores
    surrounding whitespace only — any real change to the words makes it typed.
    """
    unchanged = " ".join(chosen_value.split()) == " ".join(candidate.value.split())

    return records.add(
        conn,
        candidate.field_path,
        chosen_value,
        document_id=document_id if unchanged else None,
        locator=candidate.locator.as_dict() if unchanged else None,
        confirmed=True,
    )


def _lines_before(text: str, offset: int, *, count: int) -> list[tuple[str, int]]:
    """The last ``count`` non-blank lines ending before ``offset``."""
    out: list[tuple[str, int]] = []
    local = 0
    for line in text[:offset].splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not _NOISE.match(stripped):
            out.append((stripped, local + (len(line) - len(line.lstrip()))))
        local += len(line)
    return out[-count:]


def _best_per_path(candidates: list[Candidate]) -> list[Candidate]:
    """Keep the highest-ranked suggestion for each field path."""
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.field_path)
        if current is None or candidate.rank > current.rank:
            best[candidate.field_path] = candidate
    return sorted(best.values(), key=lambda c: c.field_path)
