"""Assigning an opportunity to a role family — T033.

The family is load-bearing in three places: it keys sufficiency rules, it
selects which discovery agents look for what, and it decides which
opportunities a profile change affects. So it is a real column with a real
deterministic source, not something inferred later by whoever needs it.

Two decisions worth stating.

**The title only.** A description that mentions "you'll work closely with our
data science team" must not turn a warehouse supervisor role into a data-science
job. Titles are short, written to be scanned, and are the thing a human would
use to answer the same question.

**No nearest match.** A title matching no pattern is ``unclassified``, and
``unclassified`` fails closed at sufficiency rather than being rounded into
whichever family it resembles most. Guessing here would silently apply the
wrong document requirements to an application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from jarvis import config as config_module

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class Family:
    key: str
    label: str
    patterns: tuple[re.Pattern[str], ...]


@lru_cache(maxsize=8)
def _families(version: str) -> tuple[Family, ...]:
    config = config_module.load("role_families", version)
    return tuple(
        Family(
            key=entry["key"],
            label=entry["label"],
            patterns=tuple(re.compile(p, re.IGNORECASE) for p in entry["patterns"]),
        )
        for entry in config.get("families", [])
    )


@dataclass(frozen=True, slots=True)
class Classification:
    """The family, and the pattern that decided it — so it can be checked."""

    key: str
    label: str
    matched_pattern: str | None
    matched_text: str | None

    @property
    def is_classified(self) -> bool:
        return self.key != UNCLASSIFIED


def classify(title: str, *, version: str = config_module.CURRENT_VERSION) -> Classification:
    """Assign a title to a family. First match wins, in configured order."""
    text = (title or "").strip()
    if not text:
        return Classification(UNCLASSIFIED, "Unclassified", None, None)

    for family in _families(version):
        for pattern in family.patterns:
            found = pattern.search(text)
            if found:
                return Classification(
                    key=family.key,
                    label=family.label,
                    matched_pattern=pattern.pattern,
                    matched_text=found.group(0),
                )

    return Classification(UNCLASSIFIED, "Unclassified", None, None)


def known_keys(version: str = config_module.CURRENT_VERSION) -> set[str]:
    return {f.key for f in _families(version)} | {UNCLASSIFIED}


def label_for(key: str, version: str = config_module.CURRENT_VERSION) -> str:
    for family in _families(version):
        if family.key == key:
            return family.label
    return "Unclassified"
