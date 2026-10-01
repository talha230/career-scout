"""Comparing a posting's skills to the user's own — T034.

The legacy version read the posting text here, at scoring time, and took
"does the candidate have this skill" from a ``candidate_has`` flag inside
``config/skill_synonyms.json``. Both are changed, for the same reason.

**The posting is not re-read.** Requirements are parsed once, at ingest
(T032), and this module works from the stored JSON. That is what makes a score
reproducible: rescoring a posting a year later must use the requirements that
were read at the time, not whatever the description would parse to under a
newer vocabulary.

**``candidate_has`` in the config is ignored.** It is a fact about one person
living in a file that ships with the package, so a second user installing
Jarvis would inherit the first user's skills and every one of their
applications would claim them. What the user has comes from their own
confirmed ``profile_record`` rows, via :mod:`jarvis.matching.profile_view`.
The field is left in the config file rather than removed, because deleting it
would change the config fingerprint and therefore the meaning of every score
already stored under ``v1``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jarvis import config as config_module
from jarvis.matching.profile_view import CandidateView

#: Where a skill appeared -> how much it counts. From config, because the
#: judgement "a skill in the title is critical" is a tuning decision.
DEFAULT_CRITICALITY = {
    "title": "critical",
    "requirements": "important",
    "body": "nice_to_have",
}

_CONFIG_REGION_NAMES = {
    "title": "title",
    "requirements_section": "requirements",
    "description_body": "body",
}


@dataclass(frozen=True, slots=True)
class SkillHit:
    """One vocabulary skill the posting named, and whether the user has it."""

    canonical: str
    matched_phrase: str
    where: str  # 'title' | 'requirements' | 'body'
    criticality: str  # 'critical' | 'important' | 'nice_to_have'
    candidate_has: bool
    in_demand: bool

    def as_dict(self, weight: float) -> dict[str, Any]:
        return {
            "skill": self.canonical,
            "matched_phrase": self.matched_phrase,
            "where": self.where,
            "criticality": self.criticality,
            "weight": weight,
        }


@dataclass(slots=True)
class SkillGap:
    """The posting's skills, sorted into what the user can and cannot claim."""

    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    transferable: list[dict[str, str]] = field(default_factory=list)
    critical_missing: list[str] = field(default_factory=list)
    trainable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "missing": self.missing,
            "transferable": self.transferable,
            "critical_missing": self.critical_missing,
            "trainable": self.trainable,
        }


def criticality_by_region(version: str = config_module.CURRENT_VERSION) -> dict[str, str]:
    """Read the region -> criticality mapping out of the weights config."""
    rules = (
        config_module.weights(version)
        .get("components", {})
        .get("skill_match", {})
        .get("criticality_rules", [])
    )
    mapping = dict(DEFAULT_CRITICALITY)
    for rule in rules:
        region = _CONFIG_REGION_NAMES.get(rule.get("if_matched_in", ""))
        if region and rule.get("criticality"):
            mapping[region] = rule["criticality"]
    return mapping


def _vocabulary(version: str) -> tuple[set[str], dict[str, dict[str, str]]]:
    """``(skills flagged in demand, the transferable map)``."""
    data = config_module.skill_vocabulary(version)
    in_demand = {s["canonical"] for s in data.get("skills", []) if s.get("in_demand")}
    transferable = {
        key: value
        for key, value in (data.get("transferable_map") or {}).items()
        if not key.startswith("_") and isinstance(value, dict)
    }
    return in_demand, transferable


def hits_from_requirements(
    requirements: dict[str, Any],
    candidate: CandidateView,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> list[SkillHit]:
    """Every skill the stored requirements name, in first-region-wins order.

    Title beats requirements section beats body, because a skill in the title
    is what the role is, and a skill in the body may be describing the team.
    """
    in_demand, _transferable = _vocabulary(version)
    regions = criticality_by_region(version)
    phrases: dict[str, str] = requirements.get("skill_phrases") or {}

    in_title = list(requirements.get("skills_in_title") or [])
    in_section = [s for s in (requirements.get("skills_in_requirements_section") or [])
                  if s not in in_title]
    everywhere = [
        s for s in (requirements.get("skills") or [])
        if s not in in_title and s not in in_section
    ]

    hits: list[SkillHit] = []
    for where, names in (("title", in_title), ("requirements", in_section), ("body", everywhere)):
        for canonical in names:
            hits.append(
                SkillHit(
                    canonical=canonical,
                    matched_phrase=phrases.get(canonical, canonical),
                    where=where,
                    criticality=regions[where],
                    candidate_has=candidate.has(canonical),
                    in_demand=canonical in in_demand,
                )
            )
    return hits


def build_skill_gap(
    hits: list[SkillHit], *, version: str = config_module.CURRENT_VERSION
) -> SkillGap:
    """Sort the hits into matched, missing, transferable, critical, trainable."""
    _in_demand, transferable_map = _vocabulary(version)
    gap = SkillGap()

    for hit in hits:
        if hit.candidate_has:
            gap.matched.append(hit.canonical)
            continue

        gap.missing.append(hit.canonical)

        bridge = transferable_map.get(hit.canonical)
        if bridge:
            gap.transferable.append(
                {"skill": hit.canonical, "from": bridge["from"], "why": bridge["why"]}
            )

        if hit.criticality == "critical":
            gap.critical_missing.append(hit.canonical)

        # "Trainable" means worth learning and not already bridged by something
        # the user can honestly claim today.
        if hit.in_demand and not bridge:
            gap.trainable.append(hit.canonical)

    for bucket in (gap.matched, gap.missing, gap.critical_missing, gap.trainable):
        bucket.sort()
    return gap
