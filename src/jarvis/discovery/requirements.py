"""Reading a posting's requirements, once, at ingest — T032.

This is the hardest thing in the system and the easiest to get quietly wrong,
so it is worth saying what it is for. The sufficiency verdict asks whether a
person's confirmed record supports an honest application to *this* posting. It
can only ask that if something has read the posting. This module is that
something.

The failure mode that matters is **not** mis-parsing. It is parsing nothing and
not noticing. An empty requirement set matches no conditional rule, and a
profile trivially satisfies no rules — so an unread posting would be marked
*sufficient* and a package generated for a job nobody understood.

Measured, from the legacy corpus of 4,402 real postings: skill matching
returned nothing at all for 481 of 2,305 assessments, because the vocabulary
covered one person's domain. One posting was scored on the title
``"Oops something happened"``. This is the common case, not the corner case.

So every parse carries a :attr:`Requirements.confidence`, computed from how
much was actually found, and below the configured floor the sufficiency verdict
is ``not_evaluated`` — which does not authorise anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from jarvis import config as config_module

# --------------------------------------------------------------- patterns

#: Headings that introduce the part of a posting stating what is needed.
REQUIREMENT_MARKERS = (
    "requirements", "qualifications", "what you'll need", "what you will need",
    "what we're looking for", "what we are looking for", "who you are",
    "minimum qualifications", "basic qualifications", "required skills",
    "you have", "you bring", "your profile", "must have", "essential",
    "ihr profil", "anforderungen", "qualifikationen", "das bringen sie mit",
)

#: Headings that introduce duties rather than requirements. Matching skills
#: here is weaker evidence, so they are tracked separately.
RESPONSIBILITY_MARKERS = (
    "responsibilities", "what you'll do", "what you will do", "the role",
    "about the role", "your mission", "duties", "aufgaben", "ihre aufgaben",
)

#: "5+ years of experience", "minimum of 5 years", "at least 5 years". All
#: three forms, because a posting using only the second one otherwise parses as
#: stating no requirement at all — and "states no requirement" scores 100 on
#: the experience component, so a narrow pattern here reads as a good match.
YEARS_PATTERNS = (
    re.compile(
        r"(?P<years>\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*"
        r"(?:years?|yrs?|jahre)\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+)?"
        r"(?:experience|exp\b|erfahrung)",
        re.IGNORECASE,
    ),
    re.compile(r"minimum\s+(?:of\s+)?(?P<years>\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE),
    re.compile(r"at\s+least\s+(?P<years>\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE),
)

#: Above this, the match is a parse artefact rather than a requirement.
MAX_PLAUSIBLE_YEARS = 40


def required_years(text: str) -> int | None:
    """The smallest stated year requirement, because a posting states a minimum.

    ``None`` means nothing was stated, which is not the same as zero and is
    scored as "nothing to fall short of" rather than as a requirement of none.
    """
    found = [
        int(match.group("years"))
        for pattern in YEARS_PATTERNS
        for match in pattern.finditer(text or "")
        if 0 < int(match.group("years")) <= MAX_PLAUSIBLE_YEARS
    ]
    return min(found) if found else None


#: A master's degree, and not the other things "master" means in a posting:
#: Scrum Master, master data, Master Scheduler, Master Service Agreement. Bare
#: "master" counts only as "master's", "masters", or "master degree/of/in";
#: "MS" only dotted or as "MS in", because "MS Excel" and "MS Office" are
#: everywhere in analyst postings.
_MASTER = (
    r"(?<!scrum )\bmaster(?:['’]?s\b|\s+(?:degree|of|in)\b)"
    r"|\bm\.?sc\b|\bm\.s\.|\bms\s+in\b|\bm\.?eng\b|\bmba\b"
)

DEGREE_REQUIRED = re.compile(
    r"\b(?:bachelor|b\.?sc|b\.?s\.?|b\.?eng|ph\.?d|doctorate|diploma)\b"
    # "degree", but not "360-degree" or "a high degree of accuracy".
    r"|(?<![-\w])degree\b(?!\s+of\b)"
    r"|" + _MASTER,
    re.IGNORECASE,
)

#: Highest first. A posting naming both "Master's or PhD" asks for the lower of
#: the two, so :func:`parse` keeps the *last* level that matches; the profile
#: side (``profile_view``) wants the highest and keeps the first. Read here, at
#: ingest, so that the education component can score from the stored
#: requirements and never re-reads the description.
DEGREE_LEVELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("phd", re.compile(r"\bph\.?d\b|\bdoctorate\b", re.IGNORECASE)),
    ("master", re.compile(_MASTER, re.IGNORECASE)),
    (
        "bachelor",
        re.compile(
            r"\bbachelor'?s?\b|\bb\.?sc\b|\bb\.?eng\b|\bb\.?s\.?\b|"
            r"\bundergraduate degree\b|\bdegree in\b",
            re.IGNORECASE,
        ),
    ),
    ("diploma", re.compile(r"\bdiploma\b|\bassociate'?s? degree\b", re.IGNORECASE)),
)

#: A posting naming a professional-engineering accreditation or licensing body.
ACCREDITATION = re.compile(
    r"\bwashington accord\b|\bchartered engineer\b|\bc\.?eng\b|\bp\.?eng\b|"
    r"\bengineers australia\b|\bprofessional engineer\b|\bincorporated engineer\b|"
    r"\baccredited (?:engineering )?degree\b",
    re.IGNORECASE,
)

LANGUAGE_TEST = re.compile(
    r"\b(?:IELTS|TOEFL|PTE|Duolingo\s+English|TestDaF|DSH|Goethe|TELC|"
    r"CEFR\s*[A-C][12]|C1\s+(?:level|German|English)|B2\s+(?:level|German|English))\b",
    re.IGNORECASE,
)

LANGUAGE_FLUENCY = re.compile(
    r"\b(?:fluent|fluency|native|proficien\w+|business[- ]level|verhandlungssicher)\b"
    r"[^.\n]{0,40}\b(?:english|german|french|spanish|arabic|urdu|deutsch)\b",
    re.IGNORECASE,
)

#: The languages the fluency patterns can name. Eligibility compares a named
#: language against the user's own confirmed languages; a fluency demand whose
#: language could not be read is reported UNSCORED rather than guessed at.
LANGUAGE_NAMES: dict[str, str] = {
    "english": "english",
    "german": "german",
    "deutsch": "german",
    "deutschkenntnisse": "german",
    "french": "french",
    "français": "french",
    "spanish": "spanish",
    "español": "spanish",
    "dutch": "dutch",
    "nederlands": "dutch",
    "italian": "italian",
    "portuguese": "portuguese",
    "arabic": "arabic",
    "urdu": "urdu",
    "mandarin": "mandarin",
    "chinese": "mandarin",
    "japanese": "japanese",
    "korean": "korean",
    "swedish": "swedish",
    "danish": "danish",
    "norwegian": "norwegian",
    "finnish": "finnish",
    "polish": "polish",
    "turkish": "turkish",
    "malay": "malay",
    "thai": "thai",
    "vietnamese": "vietnamese",
}

#: Language names that are also ordinary English words. They count only when the
#: posting capitalised them. Measured over the 4,402-posting corpus: "polish" as
#: a verb produced a Polish fluency demand on a Quantum posting that reads
#: "polish Land three or more platform-native", and "mandarin", "dutch" and
#: "thai" carry the same risk. Capitalisation is the cheapest signal that
#: separates the language from the verb, and a posting that demands a language
#: without capitalising it is rare enough to lose.
AMBIGUOUS_LANGUAGE_WORDS: frozenset[str] = frozenset(
    {"polish", "mandarin", "dutch", "thai", "french"}
)

_LANGUAGE_ALTERNATION = "|".join(sorted(LANGUAGE_NAMES, key=len, reverse=True))

#: Three shapes a posting uses to demand a language, each capturing the language
#: itself: "fluent German", "German is required", "(German-speaking)".
_LANGUAGE_ALTERNATION = "|".join(sorted(LANGUAGE_NAMES, key=len, reverse=True))

#: Three shapes a posting uses to demand a language, each capturing the language
#: itself: "fluent German", "German is required", "(German-speaking)".
NAMED_LANGUAGE_DEMAND = re.compile(
    r"(?:\b(?:fluent|fluency|native|proficien\w+|business[- ]level|verhandlungssicher\w*|"
    r"sehr\s+gute|fließend\w*)\b[^.\n]{0,40}?\b(?P<lang1>" + _LANGUAGE_ALTERNATION + r")\b"
    r"|\b(?P<lang2>" + _LANGUAGE_ALTERNATION + r")\w*\b[^.\n]{0,30}?"
    r"\b(?:fluen\w+|native|required|mandatory|essential|erforderlich|voraussetzung)\b"
    r"|\b(?P<lang3>" + _LANGUAGE_ALTERNATION + r")[ -]speaking\b)",
    re.IGNORECASE,
)

#: A demand for citizenship or settled status, as opposed to a refusal to
#: sponsor. They are different sentences with different consequences: one can be
#: satisfied by a visa the employer will not pay for, the other cannot be
#: satisfied at all.
CITIZENSHIP_REQUIRED = re.compile(
    r"\b(?:must\s+be\s+a\s+(?:u\.?s\.?\s+)?citizen(?:\s+of\s+[A-Za-z ]{2,30})?|"
    r"(?:us|u\.s\.|eu|uk|canadian|australian)\s+citizenship\s+(?:is\s+)?required|"
    r"citizens?\s+(?:only|are\s+eligible)|permanent\s+residents?\s+only|"
    r"must\s+hold\s+(?:a\s+|an\s+)?(?:eu|uk|us|canadian|australian)\s+"
    r"(?:passport|citizenship))\b",
    re.IGNORECASE,
)

CERTIFICATION = re.compile(
    r"\b(?:PMP|CSCP|CPIM|Six\s+Sigma|Lean\s+Six\s+Sigma|Green\s+Belt|Black\s+Belt|"
    r"CFA|CPA|ACCA|CISSP|AWS\s+Certified|Azure\s+Certified|PE\s+licen[cs]e|"
    r"Chartered\s+Engineer|PEC\s+regist\w+|certified\s+\w+)\b",
    re.IGNORECASE,
)

CLEARANCE = re.compile(
    r"\b(?:security\s+clearance|clearance\s+required|TS/SCI|top\s+secret|"
    r"public\s+trust|sicherheitsüberprüfung)\b",
    re.IGNORECASE,
)

#: A refusal to sponsor. The negated forms matter more than they look: this
#: pattern is tested **before** :data:`SPONSORSHIP_OFFERED`, which matches the
#: bare phrase "visa sponsorship", so a refusal this misses is not merely
#: unrecorded — it is read as an *offer*. Measured before the negated forms were
#: added, "we cannot offer visa sponsorship" and "unable to provide visa
#: sponsorship" both parsed as sponsorship offered, which tells a user who needs
#: a visa that a role refusing to sponsor them is open.
NO_SPONSORSHIP = re.compile(
    r"(?:\b(?:no\s+(?:visa\s+)?sponsorship|not\s+able\s+to\s+sponsor|unable\s+to\s+sponsor|"
    r"without\s+sponsorship|must\s+be\s+authori[sz]ed\s+to\s+work|"
    r"work\s+authori[sz]ation\s+required)\b"
    r"|\b(?:can\s?not|cannot|can't|will\s+not|won't|do\s+not|does\s+not|are\s+not\s+able\s+to|"
    r"unable\s+to|not\s+in\s+a\s+position\s+to|neither\s+sponsor)\b[^.\n]{0,40}?"
    r"\bsponsor(?:ship|ing)?\b"
    r"|\b(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:not\s+(?:available|offered|provided)|"
    r"unavailable)\b)",
    re.IGNORECASE,
)

SPONSORSHIP_OFFERED = re.compile(
    r"\b(?:visa\s+sponsorship|we\s+sponsor|sponsorship\s+(?:is\s+)?(?:available|provided|"
    r"offered)|relocation\s+(?:support|package|assistance))\b",
    re.IGNORECASE,
)

SENIORITY = {
    "intern": re.compile(r"\b(?:intern|internship|working\s+student|werkstudent|trainee)\b", re.I),
    "junior": re.compile(r"\b(?:junior|entry[- ]level|graduate|associate|jr\.?)\b", re.I),
    "senior": re.compile(r"\b(?:senior|sr\.?|staff|principal|lead\b)\b", re.I),
    "manager": re.compile(r"\b(?:manager|head\s+of|director|vp\b|chief)\b", re.I),
}

DOCUMENT_ASKS = {
    "cover_letter": re.compile(r"\bcover(?:ing)?\s+letter|motivation\s+letter|anschreiben\b", re.I),
    "motivation_letter": re.compile(r"\b(?:letter\s+of\s+)?motivation\b", re.I),
    "statement_of_purpose": re.compile(r"\bstatement\s+of\s+purpose|SOP\b", re.I),
    "research_statement": re.compile(r"\bresearch\s+(?:statement|proposal)\b", re.I),
    "transcript": re.compile(r"\btranscripts?\b", re.I),
    "references": re.compile(r"\b(?:letters?\s+of\s+)?reference|referees?\b", re.I),
}


# ---------------------------------------------------------------- vocabulary


@lru_cache(maxsize=8)
def _skill_matcher(version: str) -> tuple[re.Pattern[str], dict[str, str]]:
    """One alternation over the whole vocabulary, plus phrase -> canonical.

    The obvious implementation compiles a pattern per synonym and searches each
    one. That is roughly 400 passes over every posting: against the 4,402-row
    legacy corpus it is ~1.8 million scans of a 5 KB string, and a single crawl
    takes minutes. One alternation searched once is the same answer in one
    pass, and requirement parsing happens on every posting of every crawl.

    Phrases are ordered longest first so ``lean six sigma`` wins over ``lean``
    at the same position — Python's ``|`` is first-match, not longest-match.

    Note this is marginally *different* from searching each skill separately,
    not merely faster: ordering is global rather than per-skill, so the longest
    phrase in the whole vocabulary wins a position rather than the longest
    phrase of whichever skill happened to be tested first. Measured over the
    4,402-row corpus the two agree on 4,387 postings and differ on 15, all of
    which gain a requirements-section match under the global ordering. The
    number marked unreadable is identical (74), so no posting changes whether
    it can authorise an application.
    """
    vocabulary = config_module.skill_vocabulary(version).get("skills", [])
    phrase_to_canonical: dict[str, str] = {}
    for skill in vocabulary:
        canonical = skill["canonical"]
        for phrase in {canonical.lower(), *(s.lower() for s in skill.get("synonyms", []))}:
            phrase_to_canonical.setdefault(phrase, canonical)

    if not phrase_to_canonical:
        return re.compile(r"(?!x)x"), {}

    ordered = sorted(phrase_to_canonical, key=len, reverse=True)
    alternation = "|".join(re.escape(p) for p in ordered)
    pattern = re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])", re.IGNORECASE)
    return pattern, phrase_to_canonical


def find_skill_matches(
    text: str, version: str = config_module.CURRENT_VERSION
) -> list[tuple[str, str]]:
    """``(canonical, matched phrase)`` for every vocabulary skill named in ``text``.

    First appearance wins, so the phrase stored beside a skill is the one a
    reader will find first when they go looking for it. Used both to read a
    posting and to read the user's own confirmed skill records, so the two
    sides of a skill comparison are matched by identical rules.
    """
    if not text:
        return []
    pattern, lookup = _skill_matcher(version)
    found: dict[str, str] = {}
    for match in pattern.finditer(text):
        canonical = lookup.get(match.group(0).lower())
        if canonical and canonical not in found:
            found[canonical] = match.group(0)
    return list(found.items())


def find_skills(text: str, version: str = config_module.CURRENT_VERSION) -> list[str]:
    """Canonical skills named in ``text``, in first-appearance order."""
    return [canonical for canonical, _phrase in find_skill_matches(text, version)]

def vocabulary_size(version: str = config_module.CURRENT_VERSION) -> int:
    return len(config_module.skill_vocabulary(version).get("skills", []))


# -------------------------------------------------------------------- model


@dataclass(slots=True)
class Requirements:
    """What a posting asks for, and how sure we are that we read it."""

    skills: list[str] = field(default_factory=list)
    skills_in_requirements_section: list[str] = field(default_factory=list)
    #: Skills named in the title. The strongest signal a posting gives about
    #: what it actually wants, and the only one the scorer can no longer
    #: recover later, because no step after ingest reads the description.
    skills_in_title: list[str] = field(default_factory=list)
    #: canonical -> the exact phrase that matched, so every match is a thing
    #: someone can point at in the posting rather than a claim about it.
    skill_phrases: dict[str, str] = field(default_factory=dict)
    required_years: int | None = None
    degree_required: bool = False
    #: 'phd' | 'master' | 'bachelor' | 'diploma' | None. Read at ingest so the
    #: education component scores from the stored row.
    degree_level: str | None = None
    degree_evidence: str | None = None
    accreditation_evidence: str | None = None
    seniority: str | None = None
    requires_language_test: bool = False
    requires_language_fluency: bool = False
    requires_certification: bool = False
    requires_clearance: bool = False
    sponsorship: str | None = None  # 'offered' | 'refused' | None
    #: Everything below is read once, here, where the text still exists, and is
    #: what :mod:`jarvis.eligibility` compares against the user's own profile.
    #: A later stage never re-reads the description — the text is not in the
    #: view it is handed — so a demand not captured here is UNSCORED there
    #: rather than silently absent.
    languages_required: list[str] = field(default_factory=list)
    #: language -> the phrase that demanded it. Per language on purpose: a
    #: posting asking for "Arabic and English fluency" must not show the English
    #: sentence as the reason a user lacking Arabic was blocked.
    language_evidence: dict[str, str] = field(default_factory=dict)
    clearance_evidence: str | None = None
    requires_citizenship: bool = False
    citizenship_evidence: str | None = None
    sponsorship_evidence: str | None = None
    document_types: list[str] = field(default_factory=list)
    confidence: float = 0.0
    signals: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The jsonb payload stored on ``opportunity.requirements``."""
        return {
            "skills": self.skills,
            "skills_in_requirements_section": self.skills_in_requirements_section,
            "skills_in_title": self.skills_in_title,
            "skill_phrases": self.skill_phrases,
            "required_years": self.required_years,
            "degree_required": self.degree_required,
            "degree_level": self.degree_level,
            "degree_evidence": self.degree_evidence,
            "accreditation_evidence": self.accreditation_evidence,
            "seniority": self.seniority,
            "requires_language_test": self.requires_language_test,
            "requires_language_fluency": self.requires_language_fluency,
            "requires_certification": self.requires_certification,
            "requires_clearance": self.requires_clearance,
            "sponsorship": self.sponsorship,
            "languages_required": self.languages_required,
            "language_evidence": self.language_evidence,
            "clearance_evidence": self.clearance_evidence,
            "requires_citizenship": self.requires_citizenship,
            "citizenship_evidence": self.citizenship_evidence,
            "sponsorship_evidence": self.sponsorship_evidence,
            "document_types": self.document_types,
            "signals": self.signals,
        }


# ------------------------------------------------------------------ parsing


def split_requirement_section(text: str) -> tuple[str, str]:
    """``(requirements, everything else)``.

    A skill named under "Requirements" is a requirement. The same word under
    "About us" is decoration, and treating them alike is how a posting ends up
    demanding skills it never asked for.
    """
    lowered = text.lower()
    starts = [lowered.find(marker) for marker in REQUIREMENT_MARKERS]
    starts = [s for s in starts if s >= 0]
    if not starts:
        return "", text

    start = min(starts)
    ends = [lowered.find(marker, start + 1) for marker in RESPONSIBILITY_MARKERS]
    ends = [e for e in ends if e > start]
    end = min(ends) if ends else len(text)
    return text[start:end], text[:start] + text[end:]


def named_languages(haystack: str) -> tuple[list[str], dict[str, str]]:
    """Which languages a posting demands, and the phrase that demanded each one.

    An empty list alongside a truthy ``requires_language_fluency`` is a real
    state and not a bug: the posting demands fluency in something this could not
    name. Eligibility reports that as UNSCORED, because guessing which language
    would hide a role over a language the user may well speak.

    Measured over the 4,402-posting corpus: 436 postings demand English, 148
    German, 38 Japanese, 37 French, and no posting demands fluency without
    naming a language this can read.

    One known limit, left as it is on purpose: matches do not overlap, so
    "Arabic and English fluency required" yields Arabic alone — the phrase that
    would have named English was consumed by the Arabic match. The cost is a
    demand this misses rather than one it invents, and a missed demand leaves the
    opportunity **visible** with a soft-hide that never fires. That is the safe
    direction: the other way round hides a role over a language the user speaks.
    """
    found: list[str] = []
    evidence: dict[str, str] = {}
    for match in NAMED_LANGUAGE_DEMAND.finditer(haystack):
        word = match.group("lang1") or match.group("lang2") or match.group("lang3")
        if not word:
            continue
        if word.lower() in AMBIGUOUS_LANGUAGE_WORDS and not word[0].isupper():
            continue
        canonical = LANGUAGE_NAMES[word.lower()]
        if canonical not in found:
            found.append(canonical)
            evidence[canonical] = match.group(0).strip()
    return found, evidence


def parse(
    text: str,
    *,
    title: str = "",
    kind: str = "job",
    version: str = config_module.CURRENT_VERSION,
) -> Requirements:
    """Read one posting. Never raises; an unreadable posting scores near zero."""
    text = text or ""
    haystack = f"{title}\n{text}"
    requirement_text, remainder = split_requirement_section(text)

    in_title = find_skills(title, version)
    in_section = find_skills(requirement_text, version)
    everywhere = find_skill_matches(haystack, version)
    phrases = dict(everywhere)
    ordered = in_title + [s for s in in_section if s not in in_title]
    found = ordered + [s for s, _ in everywhere if s not in ordered]

    years = required_years(haystack)

    seniority = next(
        (name for name, pattern in SENIORITY.items() if pattern.search(title or haystack)),
        None,
    )

    documents = [
        name for name, pattern in DOCUMENT_ASKS.items() if pattern.search(haystack)
    ]
    if "cv" not in documents:
        documents.insert(0, "cv")

    sponsorship, sponsorship_evidence = None, None
    refusal = NO_SPONSORSHIP.search(haystack)
    offer = SPONSORSHIP_OFFERED.search(haystack)
    if refusal:
        sponsorship, sponsorship_evidence = "refused", refusal.group(0)
    elif offer:
        sponsorship, sponsorship_evidence = "offered", offer.group(0)

    languages, language_evidence = named_languages(haystack)
    clearance = CLEARANCE.search(haystack)
    citizenship = CITIZENSHIP_REQUIRED.search(haystack)

    degree_level, degree_evidence = None, None
    # The lowest named level wins. The bachelor pattern's generic "degree in"
    # names no level, so it only counts when nothing else matched — otherwise
    # "Master's degree in X" would read as a bachelor requirement.
    for level, pattern in DEGREE_LEVELS:
        for match in pattern.finditer(haystack):
            generic = match.group(0).lower() == "degree in"
            if not generic or degree_level is None:
                degree_level, degree_evidence = level, match.group(0)
            if not generic:
                break

    accreditation = ACCREDITATION.search(haystack)

    result = Requirements(
        skills=found,
        skills_in_requirements_section=in_section,
        skills_in_title=in_title,
        skill_phrases=phrases,
        required_years=years,
        degree_required=bool(DEGREE_REQUIRED.search(haystack)),
        degree_level=degree_level,
        degree_evidence=degree_evidence,
        accreditation_evidence=accreditation.group(0) if accreditation else None,
        seniority=seniority,
        requires_language_test=bool(LANGUAGE_TEST.search(haystack)),
        requires_language_fluency=bool(LANGUAGE_FLUENCY.search(haystack)),
        requires_certification=bool(CERTIFICATION.search(haystack)),
        requires_clearance=bool(clearance),
        sponsorship=sponsorship,
        document_types=documents,
        languages_required=languages,
        language_evidence=language_evidence,
        clearance_evidence=clearance.group(0) if clearance else None,
        requires_citizenship=bool(citizenship),
        citizenship_evidence=citizenship.group(0) if citizenship else None,
        sponsorship_evidence=sponsorship_evidence,
    )
    result.confidence, result.signals = score_confidence(
        result, text=text, title=title, requirement_text=requirement_text,
        remainder=remainder, version=version,
    )
    return result


# --------------------------------------------------------------- confidence


#: What each signal is worth. Data, so the floor can be reasoned about against
#: a real corpus rather than tuned by feel.
CONFIDENCE_WEIGHTS: dict[str, float] = {
    "has_body": 0.15,            # there is enough text to be a real posting
    "has_requirements_section": 0.25,   # it says which part states requirements
    "skills_in_section": 0.25,   # vocabulary matched inside that part
    "any_skills": 0.10,          # vocabulary matched anywhere
    "structural_signal": 0.15,   # years, degree, seniority, language, certification
    "title_present": 0.10,
}

#: Below this many characters a "posting" is a stub, a redirect, or an error
#: page. The legacy corpus had five such rows, one titled "Oops something
#: happened", and it was scored 61.14.
MIN_BODY_CHARS = 400


def score_confidence(
    result: Requirements,
    *,
    text: str,
    title: str,
    requirement_text: str,
    remainder: str,
    version: str = config_module.CURRENT_VERSION,
) -> tuple[float, dict[str, Any]]:
    """How much of this posting we actually read, 0.0-1.0, with the workings.

    Deterministic and hand-checkable like every other number here: the signals
    dictionary lists exactly which terms fired and what each was worth.
    """
    body_length = len(re.sub(r"\s+", " ", text).strip())

    signals: dict[str, Any] = {
        "body_chars": body_length,
        "vocabulary_size": vocabulary_size(version),
        "weights": CONFIDENCE_WEIGHTS,
    }

    earned: dict[str, float] = {}
    if body_length >= MIN_BODY_CHARS:
        earned["has_body"] = CONFIDENCE_WEIGHTS["has_body"]
    if requirement_text.strip():
        earned["has_requirements_section"] = CONFIDENCE_WEIGHTS["has_requirements_section"]
    if result.skills_in_requirements_section:
        earned["skills_in_section"] = CONFIDENCE_WEIGHTS["skills_in_section"]
    if result.skills:
        earned["any_skills"] = CONFIDENCE_WEIGHTS["any_skills"]
    if any((
        result.required_years is not None,
        result.degree_required,
        result.seniority,
        result.requires_language_test,
        result.requires_certification,
    )):
        earned["structural_signal"] = CONFIDENCE_WEIGHTS["structural_signal"]
    if title.strip():
        earned["title_present"] = CONFIDENCE_WEIGHTS["title_present"]

    signals["earned"] = earned

    # A posting too short to be real cannot be confidently read no matter what
    # matched inside it, so the body gate caps rather than merely scores.
    total = sum(earned.values())
    if body_length < MIN_BODY_CHARS:
        total = min(total, 0.2)
        signals["capped_reason"] = (
            f"body is {body_length} characters, below the {MIN_BODY_CHARS} needed "
            f"to be a readable posting"
        )

    return round(min(total, 1.0), 3), signals


def explain(result: Requirements) -> str:
    """A human-readable account of the confidence figure."""
    lines = [f"confidence {result.confidence:.3f}"]
    for name, value in result.signals.get("earned", {}).items():
        lines.append(f"  + {value:.2f}  {name}")
    if "capped_reason" in result.signals:
        lines.append(f"  capped: {result.signals['capped_reason']}")
    return "\n".join(lines)
