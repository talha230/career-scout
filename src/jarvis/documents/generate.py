"""Assembling application documents from the confirmed profile — T043, T044.

**This module selects, orders and omits. It never writes a claim.** Every line
it emits is one of three things, and says which:

``profile``
    Text taken from confirmed ``profile_record`` values, carrying the ids of
    every record it came from. That list of ids *is* the claim trace (T044).
``posting``
    Connective text quoting the posting's own title or employer — a statement
    about the vacancy, not about the candidate.
``template``
    A fixed sentence written here, making no factual claim ("I would welcome
    the chance to discuss the role.").

Only **confirmed** values are read (I-12). An unconfirmed value is left out and
listed in the omission report with its reason, so what was dropped is visible
rather than silently missing (T045). Restricted paths are never read at all
(I-13), and never listed with their value.

**The posting's description is never read here.** It is untrusted third-party
text, and the only posting fields that reach a document are the title and the
employer, quoted inside a fixed sentence (I-11).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from jarvis import config as config_module
from jarvis.documents.style_variant import StyleVariant
from jarvis.profile import records as records_module
from jarvis.profile.schema import index_of

PROFILE = "profile"
POSTING = "posting"
TEMPLATE = "template"
#: The outreach notice (T085): fixed wording, the page the contact was found on,
#: and the sender's own address — the only three things it may contain.
NOTICE = "notice"

HEADING = "heading"
CONTENT = "content"
BULLET = "bullet"
PARAGRAPH = "paragraph"

#: How the importer joins a role's highlights into one record.
HIGHLIGHT_SEPARATOR = " • "

#: Every word the fixed sentences below use. QC allows these and nothing else
#: beyond what a line's own sources contain, so a word that appears in no source
#: and not here is an invention — whatever put it there.
_TEMPLATE_TEXT = """
    i am a an writing to apply for the position at applying offered by your posting asks
    my record includes most recent role is background covers roles would welcome chance
    discuss hold in from present and dear hiring manager yours sincerely selected
    achievements summary key skills experience education certifications projects languages
    application work has of with committee programme this prefilled answers submit
    yourself site on employer's details contact name email phone location url apply
    here nothing sent jarvis
    thank confirming that received invitation glad speak will reply shortly times suit
    message information asked letting know considering me you be
    notice holds address found remove deleted contacted again sender about it not
    jan feb mar apr may jun jul aug sep oct nov dec january february march april june
    july august september october november december
"""
TEMPLATE_WORDS: frozenset[str] = frozenset(_TEMPLATE_TEXT.split())

_MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")
_ISO_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")
_PRESENT = {"present", "current", "now", "ongoing"}


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a document, and where every word of it came from."""

    text: str
    kind: str
    origin: str
    section: str
    sources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sources"] = list(self.sources)
        return payload


@dataclass(slots=True)
class Draft:
    doc_type: str
    lines: list[Line] = field(default_factory=list)

    def add(self, text: str, kind: str, origin: str, section: str,
            sources: tuple[str, ...] = ()) -> None:
        text = " ".join(text.split())
        if text:
            self.lines.append(Line(text, kind, origin, section, sources))

    def section(self, name: str) -> list[Line]:
        return [line for line in self.lines if line.section == name]

    def as_dict(self) -> dict[str, Any]:
        return {"doc_type": self.doc_type, "lines": [line.as_dict() for line in self.lines]}


@dataclass(slots=True)
class Generated:
    """Everything one generation produced, before QC and rendering."""

    opportunity_id: str
    kind: str
    posting: dict[str, str]
    drafts: list[Draft]
    omissions: list[dict[str, Any]]

    def claim_trace(self) -> dict[str, list[str]]:
        """Line text to the record ids behind it, for every profile line."""
        trace: dict[str, list[str]] = {}
        for draft in self.drafts:
            for line in draft.lines:
                if line.origin == PROFILE:
                    trace.setdefault(line.text, [])
                    for source in line.sources:
                        if source not in trace[line.text]:
                            trace[line.text].append(source)
        return trace


# ------------------------------------------------------------------ profile


class Profile:
    """The confirmed, non-restricted profile, indexed by path.

    Where two current records share a path, the newest confirmed one is used;
    an unconfirmed record whose path has no confirmed value is an omission.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.by_path: dict[str, records_module.Record] = {}
        self.omissions: list[dict[str, Any]] = []
        pending: dict[str, records_module.Record] = {}

        for record in records_module.current(conn):
            if record.restricted:
                continue                      # never read, never listed with a value
            if record.value is None or not record.value.strip():
                continue
            if record.confirmed:
                held = self.by_path.get(record.field_path)
                if held is None or record.created_at >= held.created_at:
                    self.by_path[record.field_path] = record
            else:
                pending[record.field_path] = record

        for path, record in sorted(pending.items()):
            if path not in self.by_path:
                self.omissions.append({
                    "field_path": path,
                    "record_id": record.id,
                    "value": record.value,
                    "reason": "not confirmed — confirm it and it can be used",
                })

    def get(self, path: str) -> tuple[str, str] | None:
        record = self.by_path.get(path)
        return (record.value or "", record.id) if record else None

    def text(self, path: str) -> str | None:
        found = self.get(path)
        return found[0] if found else None

    def ids(self, *paths: str) -> tuple[str, ...]:
        return tuple(self.by_path[p].id for p in paths if p in self.by_path)

    def indices(self, group: str) -> list[int]:
        found = {
            index_of(path) for path in self.by_path if path.startswith(f"{group}[")
        }
        return sorted(i for i in found if i is not None)


# -------------------------------------------------------------------- dates


def format_date(raw: str | None, style: str) -> str | None:
    """Render a profile date in the account's style. Never invents precision."""
    if not raw:
        return None
    value = raw.strip()
    if value.lower() in _PRESENT:
        return "Present"
    match = _ISO_DATE.match(value)
    if not match:
        return value                                   # shown as written
    year, month = match.group(1), match.group(2)
    if month is None:
        return year
    m = int(month)
    if not 1 <= m <= 12:
        return value
    if style == "mm_yyyy":
        return f"{month}/{year}"
    if style == "month_yyyy":
        return f"{_MONTHS_LONG[m - 1]} {year}"
    if style == "yyyy_only":
        return year
    return f"{_MONTHS_SHORT[m - 1]} {year}"


def _has_month(profile: Profile) -> bool:
    return any(
        path.endswith(("startDate", "endDate")) and _ISO_DATE.match(record.value or "")
        and _ISO_DATE.match(record.value or "").group(2)
        for path, record in profile.by_path.items()
    )


def _span(profile: Profile, prefix: str, date_style: str) -> tuple[str, tuple[str, ...]]:
    """``(Mar 2021 – Jun 2023)``, or ``(from Mar 2021)`` when no end is confirmed.

    An unconfirmed end date is never written as "Present": that would claim a
    current employment the record does not support.
    """
    start = format_date(profile.text(f"{prefix}.startDate"), date_style)
    end = format_date(profile.text(f"{prefix}.endDate"), date_style)
    ids = profile.ids(f"{prefix}.startDate", f"{prefix}.endDate")
    if start and end:
        return f"({start} – {end})", ids
    if start:
        return f"(from {start})", ids
    if end:
        return f"(to {end})", ids
    return "", ids


# ---------------------------------------------------------------- relevance


def _posting_skills(requirements: dict[str, Any]) -> list[str]:
    skills = requirements.get("skills") or []
    return [str(s).lower() for s in skills if isinstance(s, str)]


def _relevance(text: str, wanted: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for skill in wanted if skill and skill in lowered)


def article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


# ----------------------------------------------------------------------- CV


def build_cv(profile: Profile, posting: dict[str, Any], variant: StyleVariant,
             structure: dict[str, Any]) -> Draft:
    draft = Draft("cv")
    wanted = _posting_skills(posting.get("requirements") or {})
    date_style = variant.effective_date_format(has_month_precision=_has_month(profile))

    # --- header
    name = profile.get("basics.name")
    if name:
        shown = name[0].upper() if variant.name_treatment == "upper" else name[0]
        draft.add(shown, HEADING, PROFILE, "header", (name[1],))
    label = profile.get("basics.label")
    if label:
        draft.add(label[0], CONTENT, PROFILE, "header", (label[1],))
    contact_paths = [p for p in ("basics.email", "basics.phone") if profile.get(p)]
    if contact_paths:
        draft.add(" · ".join(profile.text(p) or "" for p in contact_paths), CONTENT, PROFILE,
                  "header", profile.ids(*contact_paths))
    place_paths = [p for p in ("basics.location.city", "basics.location.region",
                               "basics.location.countryCode") if profile.get(p)]
    if place_paths:
        draft.add(", ".join(profile.text(p) or "" for p in place_paths), CONTENT, PROFILE,
                  "header", profile.ids(*place_paths))

    # --- summary: verbatim, never rewritten per job
    summary = profile.get("basics.summary")
    if summary:
        draft.add("Summary", HEADING, TEMPLATE, "summary")
        draft.add(summary[0], PARAGRAPH, PROFILE, "summary", (summary[1],))

    # --- key skills, the posting's skills first
    skill_lines: list[tuple[int, str, tuple[str, ...]]] = []
    for index in profile.indices("skills"):
        skill_name = profile.get(f"skills[{index}].name")
        if not skill_name:
            continue
        keywords = profile.get(f"skills[{index}].keywords")
        text = skill_name[0]
        sources = (skill_name[1],)
        if keywords:
            words = [k.strip() for k in keywords[0].split(",") if k.strip()]
            words.sort(key=lambda k: -_relevance(k, wanted))   # stable: ties keep order
            text = f"{skill_name[0]}: {', '.join(words[: structure['max_skills_shown']])}"
            sources += (keywords[1],)
        skill_lines.append((_relevance(text, wanted), text, sources))
    if skill_lines:
        draft.add("Key Skills", HEADING, TEMPLATE, "key_skills")
        for _score, text, sources in sorted(skill_lines, key=lambda t: -t[0]):
            draft.add(text, BULLET, PROFILE, "key_skills", sources)

    # --- experience, most recent first
    roles = []
    for index in profile.indices("work"):
        prefix = f"work[{index}]"
        position, employer = profile.get(f"{prefix}.position"), profile.get(f"{prefix}.employer")
        if not (position and employer):
            continue
        roles.append((profile.text(f"{prefix}.startDate") or "", index))
    if roles:
        draft.add("Experience", HEADING, TEMPLATE, "experience")
    for _start, index in sorted(roles, reverse=True):
        prefix = f"work[{index}]"
        span, span_ids = _span(profile, prefix, date_style)
        draft.add(
            f"{profile.text(f'{prefix}.position')} — {profile.text(f'{prefix}.employer')} {span}",
            HEADING, PROFILE, "experience",
            profile.ids(f"{prefix}.position", f"{prefix}.employer") + span_ids,
        )
        role_summary = profile.get(f"{prefix}.summary")
        if role_summary:
            draft.add(role_summary[0], PARAGRAPH, PROFILE, "experience", (role_summary[1],))
        highlights = profile.get(f"{prefix}.highlights")
        if highlights:
            parts = highlights[0].split(HIGHLIGHT_SEPARATOR.strip())
            items = [h.strip() for h in parts if h.strip()]
            ranked = sorted(items, key=lambda h: -_relevance(h, wanted))
            for item in ranked[: structure["max_highlights_per_role"]]:
                draft.add(item, BULLET, PROFILE, "experience", (highlights[1],))

    achievements = [profile.get(f"x_achievements[{i}].statement")
                    for i in profile.indices("x_achievements")]
    achievements = [a for a in achievements if a]
    if achievements:
        draft.add("Selected achievements", CONTENT, TEMPLATE, "experience")
        for text, record_id in achievements:
            draft.add(text, BULLET, PROFILE, "experience", (record_id,))

    # --- education
    education = []
    for index in profile.indices("education"):
        prefix = f"education[{index}]"
        institution = profile.get(f"{prefix}.institution")
        if not institution:
            continue
        study, area = profile.text(f"{prefix}.studyType"), profile.text(f"{prefix}.area")
        span, span_ids = _span(profile, prefix, date_style)
        degree = " in ".join(p for p in (study, area) if p)
        text = f"{degree} — {institution[0]}" if degree else institution[0]
        score = profile.text(f"{prefix}.score")
        text = f"{text} {span}".strip() + (f", {score}" if score else "")
        education.append((
            profile.text(f"{prefix}.endDate") or "",
            text,
            profile.ids(f"{prefix}.institution", f"{prefix}.studyType", f"{prefix}.area",
                        f"{prefix}.score") + span_ids,
        ))
    if education:
        draft.add("Education", HEADING, TEMPLATE, "education")
        for _end, text, sources in sorted(education, reverse=True):
            draft.add(text, BULLET, PROFILE, "education", sources)

    # --- certifications
    certs = []
    for index in profile.indices("certificates"):
        prefix = f"certificates[{index}]"
        cert = profile.get(f"{prefix}.name")
        if not cert:
            continue
        issuer, when = profile.text(f"{prefix}.issuer"), profile.text(f"{prefix}.date")
        text = cert[0] + (f" — {issuer}" if issuer else "")
        text += f" ({format_date(when, date_style)})" if when else ""
        certs.append((text, profile.ids(f"{prefix}.name", f"{prefix}.issuer", f"{prefix}.date")))
    if certs:
        draft.add("Certifications", HEADING, TEMPLATE, "certifications")
        for text, sources in certs:
            draft.add(text, BULLET, PROFILE, "certifications", sources)

    # --- languages
    langs = []
    for index in profile.indices("languages"):
        prefix = f"languages[{index}]"
        language = profile.get(f"{prefix}.language")
        if not language:
            continue
        fluency, test = profile.text(f"{prefix}.fluency"), profile.text(f"{prefix}.test")
        text = language[0] + (f": {fluency}" if fluency else "") + (f" ({test})" if test else "")
        sources = profile.ids(f"{prefix}.language", f"{prefix}.fluency", f"{prefix}.test")
        langs.append((text, sources))
    if langs:
        draft.add("Languages", HEADING, TEMPLATE, "languages")
        for text, sources in langs:
            draft.add(text, BULLET, PROFILE, "languages", sources)

    return draft


# -------------------------------------------------------------- letters


def _latest_role(profile: Profile) -> str | None:
    roles = [
        (profile.text(f"work[{i}].startDate") or "", f"work[{i}]")
        for i in profile.indices("work")
        if profile.get(f"work[{i}].position") and profile.get(f"work[{i}].employer")
    ]
    return max(roles)[1] if roles else None


def _signature(draft: Draft, profile: Profile, section: str) -> None:
    paths = [p for p in ("basics.name", "basics.email", "basics.phone") if profile.get(p)]
    if paths:
        draft.add(" · ".join(profile.text(p) or "" for p in paths), CONTENT, PROFILE,
                  section, profile.ids(*paths))


def _evidence(draft: Draft, profile: Profile, section: str, limit: int = 2) -> None:
    """A confirmed achievement, or failing that the latest role's first highlight."""
    used = 0
    for index in profile.indices("x_achievements"):
        found = profile.get(f"x_achievements[{index}].statement")
        if found and used < limit:
            draft.add(found[0], PARAGRAPH, PROFILE, section, (found[1],))
            used += 1
    if used:
        return
    latest = _latest_role(profile)
    highlights = profile.get(f"{latest}.highlights") if latest else None
    if highlights:
        first = highlights[0].split(HIGHLIGHT_SEPARATOR.strip())[0].strip()
        draft.add(first, PARAGRAPH, PROFILE, section, (highlights[1],))


def build_cover_letter(profile: Profile, posting: dict[str, Any]) -> Draft:
    draft = Draft("cover_letter")
    title, employer = posting["title"], posting["employer"]

    draft.add(f"I am writing to apply for the {title} position at {employer}.",
              PARAGRAPH, POSTING, "P1_opening")
    label = profile.get("basics.label")
    latest = _latest_role(profile)
    if label:
        draft.add(f"I am {article(label[0])} {label[0]}.", PARAGRAPH, PROFILE, "P1_opening",
                  (label[1],))
    elif latest:
        draft.add(
            f"My most recent role is {profile.text(f'{latest}.position')} at "
            f"{profile.text(f'{latest}.employer')}.",
            PARAGRAPH, PROFILE, "P1_opening",
            profile.ids(f"{latest}.position", f"{latest}.employer"),
        )

    # P2 — only skills the posting names AND a confirmed skill record holds.
    wanted = _posting_skills(posting.get("requirements") or {})
    matched: list[tuple[str, str]] = []
    for index in profile.indices("skills"):
        for path in (f"skills[{index}].name", f"skills[{index}].keywords"):
            found = profile.get(path)
            if not found:
                continue
            for term in (t.strip() for t in found[0].split(",")):
                seen = {m[0].lower() for m in matched}
                if term and term.lower() in wanted and term.lower() not in seen:
                    matched.append((term, found[1]))
    if matched:
        names = [m[0] for m in matched[:4]]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        draft.add(f"My record includes {joined}.", PARAGRAPH, PROFILE, "P2_why_this_role",
                  tuple(dict.fromkeys(m[1] for m in matched[:4])))
    role_summary = profile.get(f"{latest}.summary") if latest else None
    if role_summary:
        draft.add(role_summary[0], PARAGRAPH, PROFILE, "P2_why_this_role", (role_summary[1],))
    elif not matched:
        summary = profile.get("basics.summary")
        if summary:
            draft.add(summary[0], PARAGRAPH, PROFILE, "P2_why_this_role", (summary[1],))

    _evidence(draft, profile, "P3_evidence")

    employers = []
    for index in profile.indices("work"):
        found = profile.get(f"work[{index}].employer")
        if found and found[0] not in [e[0] for e in employers]:
            employers.append(found)
    if employers:
        names = [e[0] for e in employers[:4]]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        draft.add(f"My background covers roles at {joined}.", PARAGRAPH, PROFILE,
                  "P4_fit_and_gaps", tuple(e[1] for e in employers[:4]))

    draft.add("I would welcome the chance to discuss the role.", PARAGRAPH, TEMPLATE, "P5_close")
    _signature(draft, profile, "P5_close")
    return draft


def build_motivation_letter(profile: Profile, posting: dict[str, Any]) -> Draft:
    draft = Draft("motivation_letter")
    draft.add(f"I am applying for the {posting['title']} offered by {posting['employer']}.",
              PARAGRAPH, POSTING, "P1_opening")

    degrees = []
    for index in profile.indices("education"):
        prefix = f"education[{index}]"
        institution = profile.get(f"{prefix}.institution")
        study, area = profile.text(f"{prefix}.studyType"), profile.text(f"{prefix}.area")
        if institution and study:
            degrees.append((profile.text(f"{prefix}.endDate") or "", prefix, study, area,
                            institution[0]))
    if degrees:
        _end, prefix, study, area, institution = max(degrees)
        degree = f"{study} in {area}" if area else study
        draft.add(f"I hold {article(degree)} {degree} from {institution}.", PARAGRAPH, PROFILE,
                  "P2_academic_record",
                  profile.ids(f"{prefix}.studyType", f"{prefix}.area", f"{prefix}.institution"))

    summary = profile.get("basics.summary")
    if summary:
        draft.add(summary[0], PARAGRAPH, PROFILE, "P3_direction", (summary[1],))
    _evidence(draft, profile, "P4_evidence")
    draft.add("I would welcome the chance to discuss the programme.", PARAGRAPH, TEMPLATE,
              "P5_close")
    _signature(draft, profile, "P5_close")
    return draft


def build_worksheet(profile: Profile, posting: dict[str, Any]) -> Draft:
    """T054 — the portal route. Prefilled answers for the user to submit themselves.

    Nothing here is sent. It is the same confirmed data, laid out as the fields
    an application form usually asks for, next to the posting URL.
    """
    draft = Draft("worksheet")
    draft.add("Submit this yourself on the employer's site. Nothing here is sent by Jarvis.",
              PARAGRAPH, TEMPLATE, "about")
    if posting.get("source_url"):
        draft.add(f"Apply here: {posting['source_url']}", CONTENT, POSTING, "about")
    for label, path in (("Name", "basics.name"), ("Email", "basics.email"),
                        ("Phone", "basics.phone"), ("Location", "basics.location.city")):
        found = profile.get(path)
        if found:
            draft.add(f"{label}: {found[0]}", CONTENT, PROFILE, "details", (found[1],))
    return draft


# ------------------------------------------------------------------ entry


def load_posting(conn: sqlite3.Connection, opportunity_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, kind, title, employer, requirements, source_url, apply_route, apply_target, "
        "employer_norm, title_norm, country_iso2 FROM opportunity WHERE id = ?",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no opportunity {opportunity_id}")
    posting = dict(row)
    posting["requirements"] = json.loads(row["requirements"] or "{}")
    # Only the title and employer reach a sentence. Collapse them to one line
    # and cap them, so a posting cannot smuggle a paragraph in through its title.
    for key in ("title", "employer"):
        posting[key] = " ".join(str(row[key] or "").split())[:120]
    return posting


def document_types(posting: dict[str, Any], *, portal: bool) -> list[str]:
    stated = posting["requirements"].get("document_types") or []
    kind = posting.get("kind", "job")
    chosen = ["cv", "motivation_letter" if kind == "scholarship" else "cover_letter"]
    # A posting that names a statement of purpose gets a motivation letter in its
    # place rather than nothing: there is no separate SoP generator yet.
    if "motivation_letter" in stated and "motivation_letter" not in chosen:
        chosen.append("motivation_letter")
    if portal:
        chosen.append("worksheet")
    return chosen


def build(
    conn: sqlite3.Connection,
    opportunity_id: str,
    variant: StyleVariant,
    *,
    portal: bool,
    version: str = config_module.CURRENT_VERSION,
) -> Generated:
    posting = load_posting(conn, opportunity_id)
    profile = Profile(conn)
    structure = config_module.load("document_structure", version)["cv"]

    builders = {
        "cv": lambda: build_cv(profile, posting, variant, structure),
        "cover_letter": lambda: build_cover_letter(profile, posting),
        "motivation_letter": lambda: build_motivation_letter(profile, posting),
        "worksheet": lambda: build_worksheet(profile, posting),
    }
    drafts = [builders[t]() for t in document_types(posting, portal=portal)]
    return Generated(
        opportunity_id=opportunity_id,
        kind=posting.get("kind") or "job",
        posting={k: posting[k] for k in ("title", "employer")}
        | {"source_url": posting.get("source_url") or ""},
        drafts=drafts,
        omissions=profile.omissions,
    )
