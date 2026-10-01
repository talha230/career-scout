"""Reply classification — T062. Deterministic, offline, explainable.

Ported from the legacy ``phase6/classify.py``. A message is scored against every
category's patterns in ``config/v1/email_categories.json``; the highest total
wins, ties going to the more urgent category. Below the minimum score it is
``unclassified`` with its partial scores attached — **never** pushed into the
nearest category, because a guess shown as a classification would move an
application's status on nobody's evidence.

The category then maps (``maps_to`` in the config) to one of the five stored
classes the ``reply`` table allows, or ``unclassified``.

The email body is untrusted third-party text. It is matched against regexes
here and nothing else: it is never an instruction to anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from jarvis import config as config_module

STORED_CLASSES = frozenset({
    "acknowledgement", "rejection", "interview_invite", "offer", "information_request",
    "unclassified",
})

#: The application status each stored class moves an application to (T064).
STATUS_FOR = {
    "acknowledgement": "acknowledged",
    "rejection": "rejected",
    "interview_invite": "interview",
    "offer": "offer",
    "information_request": "info_requested",
}


@dataclass(frozen=True, slots=True)
class Classification:
    stored: str
    category: str
    label: str
    score: int
    action: str
    matched: tuple[str, ...] = ()
    scores: dict[str, int] = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        """Score relative to the threshold, capped at 1. A reading aid, not a probability."""
        minimum = _rules()["minimum_score_to_classify"]
        return round(min(1.0, self.score / (2 * minimum)), 2) if self.score else 0.0

    def detail(self) -> dict[str, Any]:
        return {"category": self.category, "label": self.label, "score": self.score,
                "action": self.action, "matched": list(self.matched), "scores": self.scores}


@lru_cache(maxsize=4)
def _config(version: str = config_module.CURRENT_VERSION) -> dict[str, Any]:
    return config_module.load("email_categories", version)


def _rules() -> dict[str, Any]:
    return _config()["classification_rules"]


@lru_cache(maxsize=4)
def _compiled(version: str = config_module.CURRENT_VERSION) -> tuple[Any, ...]:
    out = []
    for category in _config(version)["categories"]:
        if category.get("maps_to") not in STORED_CLASSES:
            raise ValueError(f"{category['id']} maps to {category.get('maps_to')!r}, "
                             f"which the reply table does not allow")
        patterns = tuple(
            (p["weight"], p["regex"], re.compile(p["regex"], re.IGNORECASE))
            for p in category["patterns"]
        )
        out.append((category, patterns))
    return tuple(out)


def classify(subject: str, body: str, sender: str = "") -> Classification:
    # The subject carries more signal than the body, so it is counted twice.
    haystack = f"{subject}\n{subject}\n{sender}\n{body or ''}"
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}
    for category, patterns in _compiled():
        total = 0
        for weight, source, pattern in patterns:
            found = pattern.search(haystack)
            if found:
                total += weight
                matched.setdefault(category["id"], []).append(
                    f"[{weight}] {source} -> {found.group(0)[:60]!r}")
        if total:
            scores[category["id"]] = total

    if not scores:
        return Classification("unclassified", "unclassified", "Unclassified", 0,
                              "Read it yourself — no rule matched.")

    by_id = {c["id"]: c for c, _ in _compiled()}
    best_id = min(scores, key=lambda cid: (-scores[cid], by_id[cid]["priority"]))
    best = by_id[best_id]
    if scores[best_id] < _rules()["minimum_score_to_classify"]:
        return Classification(
            "unclassified", "unclassified", "Unclassified", scores[best_id],
            f"Read it yourself. Closest was {best['label']!r} at {scores[best_id]}, below "
            f"the threshold of {_rules()['minimum_score_to_classify']}.",
            tuple(matched.get(best_id, ())), scores,
        )
    return Classification(best["maps_to"], best["id"], best["label"], scores[best_id],
                          best["action"], tuple(matched[best_id]), scores)
