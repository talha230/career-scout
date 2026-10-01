"""Deciding whether a posting is remote, onsite or a project engagement — T031a.

This column is load-bearing in three places, which is why it is read once at
ingest and stored rather than re-derived by whoever needs it:

* **Savings.** A remote role is taxed where the candidate lives; an onsite role
  is taxed where the employer is and carries relocation and local living costs.
  The two produce different money.
* **Eligibility.** An onsite role abroad needs a visa. A worldwide-remote one
  frequently does not.
* **Search.** Remote and freelance work are things the user is explicitly
  looking for, not residue left over after the onsite roles are counted.

Three decisions, each stated in ``work_arrangement.json`` and repeated here
because they are the ones that would otherwise look like bugs:

**Unknown stays NULL.** A posting whose arrangement cannot be read is not
"onsite". Onsite is the value that triggers relocation and cost-of-living
arithmetic, so guessing it is the most expensive possible mistake.

**There is no hybrid bucket.** A hybrid role requires physical presence in the
employer's city on some days, so for cost of living, tax residence, relocation
and visa it behaves as onsite. The hybrid signal survives in ``evidence``.

**Project beats remote.** A freelance contract delivered remotely is a project
engagement first — how someone is engaged and paid decides whether there is a
salary at all, which outranks where they sit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from career_scout import config as config_module

REMOTE = "remote"
ONSITE = "onsite"
PROJECT = "project"

#: The values ``opportunity.work_arrangement`` permits, plus the absent case.
VALUES = (REMOTE, ONSITE, PROJECT)


@dataclass(frozen=True, slots=True)
class _Rules:
    title: tuple[re.Pattern[str], ...]
    body: tuple[re.Pattern[str], ...]
    structured: frozenset[str]
    negations: tuple[re.Pattern[str], ...]
    hybrid: tuple[re.Pattern[str], ...]


def _compile(patterns: list[str] | None) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns or ())


@lru_cache(maxsize=8)
def _rules(version: str) -> dict[str, _Rules]:
    config = config_module.load("work_arrangement", version)
    built: dict[str, _Rules] = {}
    for key in VALUES:
        entry = config.get(key, {})
        built[key] = _Rules(
            title=_compile(entry.get("title_patterns")),
            body=_compile(entry.get("body_patterns")),
            structured=frozenset(v.lower() for v in entry.get("structured_values", ())),
            negations=_compile(entry.get("negations")),
            hybrid=_compile(entry.get("hybrid_patterns")),
        )
    return built


@dataclass(frozen=True, slots=True)
class Arrangement:
    """The decision, and what produced it — so it can be checked, not trusted.

    ``kind`` is None when nothing matched. That is a real answer, stored as NULL.
    """

    kind: str | None
    matched_pattern: str | None = None
    matched_text: str | None = None
    signal: str | None = None          # structured_field | title | body
    evidence: str | None = None

    @property
    def is_known(self) -> bool:
        return self.kind is not None

    @property
    def is_remote(self) -> bool:
        return self.kind == REMOTE

    @property
    def is_project(self) -> bool:
        return self.kind == PROJECT


def _first_match(
    patterns: tuple[re.Pattern[str], ...], text: str
) -> tuple[re.Pattern[str], re.Match[str]] | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return pattern, found
    return None


def classify(
    title: str | None,
    *,
    description: str | None = None,
    employment_types: list[str] | None = None,
    tags: list[str] | None = None,
    location_raw: str | None = None,
    resolved_place: str | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> Arrangement:
    """Read the arrangement from what the employer actually published.

    Checked in descending order of how much the source is committing to:

    1. its **own employment-type field**, which is a statement by the employer;
    2. the **title**, which is short and written to be scanned;
    3. the **body**, which is prose and the most likely to mislead;
    4. finally ``resolved_place``, if the caller passes one.

    ``tags`` and ``location_raw`` join the title tier — a board that files a
    posting under a ``remote`` tag or a location of "Remote" is labelling it as
    deliberately as a title does.

    **On ``resolved_place``.** An employer that publishes a physical address for
    a role and says nothing about remote has told you something: the job is at
    that address. Reading that as onsite is an inference, not a reading, so it is
    the last tier, it is only available when the caller supplies a place that
    actually resolved, and it names the place in its evidence — it never hides
    inside a default. Callers that would rather have NULL simply omit the
    argument. Measured on the corpus: without this tier 64% of postings have no
    arrangement at all, which leaves most of them unscoreable for savings.
    """
    rules = _rules(version)
    title_text = title or ""
    label_text = " ".join(filter(None, [title_text, location_raw or "", " ".join(tags or [])]))
    body_text = description or ""

    declared = {value.strip().lower() for value in (employment_types or []) if value}

    # 1 — the source's own structured field, project first (see module docstring).
    for kind in (PROJECT, REMOTE, ONSITE):
        hit = declared & rules[kind].structured
        if hit:
            if kind == REMOTE and _first_match(rules[REMOTE].negations, body_text):
                break
            value = sorted(hit)[0]
            return Arrangement(
                kind=kind,
                matched_text=value,
                signal="structured_field",
                evidence=f"source published employment type '{value}'",
            )

    # 2 — the title, the location field and the board's own tags.
    for kind in (PROJECT, REMOTE):
        found = _first_match(rules[kind].title, label_text)
        if found:
            pattern, match = found
            if kind == REMOTE and _first_match(rules[REMOTE].negations, body_text):
                continue
            return Arrangement(
                kind=kind,
                matched_pattern=pattern.pattern,
                matched_text=match.group(0),
                signal="title",
                evidence=f"'{match.group(0)}' in title/tags/location",
            )

    # 3 — the body. A project engagement is only ever read from a phrase naming the
    # engagement, never from the bare word "contract"; see the config's
    # false_positive_note.
    found = _first_match(rules[PROJECT].body, body_text)
    if found:
        pattern, match = found
        return Arrangement(
            kind=PROJECT,
            matched_pattern=pattern.pattern,
            matched_text=match.group(0),
            signal="body",
            evidence=f"'{match.group(0)}' in description",
        )

    negated = _first_match(rules[REMOTE].negations, body_text)
    if not negated:
        found = _first_match(rules[REMOTE].body, body_text)
        if found:
            pattern, match = found
            return Arrangement(
                kind=REMOTE,
                matched_pattern=pattern.pattern,
                matched_text=match.group(0),
                signal="body",
                evidence=f"'{match.group(0)}' in description",
            )

    # A hybrid role resolves to onsite, keeping the word in its evidence.
    hybrid = _first_match(rules[ONSITE].hybrid, f"{label_text} {body_text}")
    if hybrid:
        pattern, match = hybrid
        return Arrangement(
            kind=ONSITE,
            matched_pattern=pattern.pattern,
            matched_text=match.group(0),
            signal="body",
            evidence=(
                f"hybrid ('{match.group(0)}') — recorded as onsite because it requires "
                f"presence in the employer's city"
            ),
        )

    for tier, patterns in (("title", rules[ONSITE].title), ("body", rules[ONSITE].body)):
        text = label_text if tier == "title" else body_text
        found = _first_match(patterns, text)
        if found:
            pattern, match = found
            return Arrangement(
                kind=ONSITE,
                matched_pattern=pattern.pattern,
                matched_text=match.group(0),
                signal=tier,
                evidence=f"'{match.group(0)}' in {tier}",
            )

    if negated:
        return Arrangement(
            kind=ONSITE,
            matched_pattern=negated[0].pattern,
            matched_text=negated[1].group(0),
            signal="body",
            evidence=f"remote explicitly refused: '{negated[1].group(0)}'",
        )

    if resolved_place:
        return Arrangement(
            kind=ONSITE,
            matched_text=resolved_place,
            signal="location",
            evidence=(
                f"inferred from the published work location ({resolved_place}); "
                f"the posting states no remote or project arrangement"
            ),
        )

    return Arrangement(kind=None, evidence="no arrangement stated in title, tags or description")
