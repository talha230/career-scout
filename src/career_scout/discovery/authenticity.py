"""Rejecting postings that are not real, applyable vacancies — T030c.

Two judgements, deliberately kept apart:

**Junk** — there is no posting behind the row. An error page, a test record, a
placeholder. The legacy engine scored a posting titled ``"Oops something
happened"`` at 61.14 and would have generated an application for it.

**Fraud** — there is a posting, and it is trying to take something from the
applicant: a fee, a bank detail, an identity document.

They are not merged into one "suspicious" number because their evidence, their
false-positive cost and what a reviewer should do about them all differ.

Junk is a single anchored match on the whole title. Fraud is a weighted sum of
phrase-shaped signals against a threshold in config, because no single keyword is
sufficient evidence of a scam — measured: the bare word "whatsapp" appears in 8
postings in the corpus and all 8 are legitimate.

Nothing here deletes. A rejection is a row naming its rule and quoting the text
that fired it, so it can be read back and argued with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from career_scout import config as config_module
from career_scout.discovery.normalize import normalize_text

JUNK = "junk"
FRAUD = "fraud"


@dataclass(frozen=True, slots=True)
class _JunkRule:
    id: str
    label: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, slots=True)
class _FraudSignal:
    id: str
    label: str
    strength: str
    weight: float
    patterns: tuple[re.Pattern[str], ...]


@lru_cache(maxsize=8)
def _junk_rules(version: str) -> tuple[_JunkRule, ...]:
    config = config_module.load("authenticity", version)
    rules = []
    for entry in config.get("junk", {}).get("rules", []):
        if not entry.get("enabled", True):
            continue
        rules.append(
            _JunkRule(
                id=entry["id"],
                label=entry["label"],
                # Anchored: the pattern must account for the entire title. A bare
                # substring match on "404" rejects "Room 404 Facilities Technician".
                patterns=tuple(
                    re.compile(rf"\A(?:{p})\Z", re.IGNORECASE)
                    for p in entry.get("whole_title_patterns", ())
                ),
            )
        )
    return tuple(rules)


@lru_cache(maxsize=8)
def _fraud_signals(version: str) -> tuple[tuple[_FraudSignal, ...], float]:
    config = config_module.load("authenticity", version).get("fraud", {})
    weights = config.get("weights", {})
    signals = tuple(
        _FraudSignal(
            id=entry["id"],
            label=entry["label"],
            strength=entry["strength"],
            weight=float(weights.get(entry["strength"], 0.0)),
            patterns=tuple(re.compile(p, re.IGNORECASE) for p in entry.get("patterns", ())),
        )
        for entry in config.get("signals", [])
    )
    return signals, float(config.get("reject_at", 1.0))


@lru_cache(maxsize=8)
def _negation(version: str) -> tuple[re.Pattern[str] | None, re.Pattern[str]]:
    """The negation vocabulary and the clause boundary, from config (I-23)."""
    config = config_module.load("authenticity", version).get("fraud", {}).get("negation", {})
    patterns = config.get("patterns") or []
    negation = (
        re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE) if patterns else None
    )
    boundary = re.compile(config.get("clause_boundaries") or r"[.!?;:,\n]")
    return negation, boundary


def _first_affirmed(
    patterns: tuple[re.Pattern[str], ...],
    haystack: str,
    negation: re.Pattern[str] | None,
    boundary: re.Pattern[str],
) -> re.Match[str] | None:
    """The first match that is not negated within its own clause.

    Every occurrence is tried, not just the first: a posting can carry an
    employer's anti-scam notice ("we will never ask you to pay a fee") and, in a
    scam, a real demand further down. Skipping the notice must not also skip the
    demand.
    """
    for pattern in patterns:
        for found in pattern.finditer(haystack):
            if not _negated(haystack, found.start(), negation, boundary):
                return found
    return None


def _negated(
    haystack: str, start: int, negation: re.Pattern[str] | None, boundary: re.Pattern[str]
) -> bool:
    """Whether a negation appears earlier in the clause containing ``start``.

    The clause, not the sentence: "Don't worry, you must pay a registration fee"
    has its negation in a clause the comma closed, and must still reject.
    """
    if negation is None:
        return False
    before = haystack[:start]
    edges = list(boundary.finditer(before))
    clause = before[edges[-1].end():] if edges else before
    return negation.search(clause) is not None


@dataclass(frozen=True, slots=True)
class Hit:
    """One rule or signal that fired, with the text that fired it."""

    id: str
    label: str
    matched_text: str
    weight: float = 0.0

    def __str__(self) -> str:
        return f"{self.id}: '{self.matched_text}'"


@dataclass(slots=True)
class Verdict:
    """Whether the posting is a real vacancy, and the evidence either way.

    ``rejected`` is the only field the pipeline branches on. The rest exists so a
    person reading the ``rejection`` row can decide the rule was wrong.
    """

    rejected: bool = False
    kind: str | None = None                  # junk | fraud
    rule: str | None = None
    reason: str = ""
    hits: list[Hit] = field(default_factory=list)
    score: float = 0.0
    threshold: float = 0.0

    @property
    def evidence(self) -> str:
        """The quoted text of every hit, for the ``rejection.evidence`` column."""
        return " | ".join(str(hit) for hit in self.hits)

    @property
    def flagged(self) -> bool:
        """Signals fired but did not reach the threshold — worth showing, not acting on."""
        return bool(self.hits) and not self.rejected


def check_junk(title: str | None, *, version: str = config_module.CURRENT_VERSION) -> Verdict:
    """Is the title an error string, a test record or a catch-all rather than a job?

    Matched against the *normalised* title, so punctuation and case do not let a
    placeholder through: ``"Oops, something happened!"`` normalises to
    ``"oops something happened"``.
    """
    normalized = normalize_text(title)
    if not normalized:
        # An absent title is already F10's job in hard_filters; it is not junk's
        # call to make, and two rules rejecting one row hides which one matters.
        return Verdict()

    for rule in _junk_rules(version):
        for pattern in rule.patterns:
            found = pattern.search(normalized)
            if found:
                return Verdict(
                    rejected=True,
                    kind=JUNK,
                    rule=rule.id,
                    reason=rule.label,
                    hits=[Hit(rule.id, rule.label, title or normalized)],
                )
    return Verdict()


def check_fraud(
    text: str | None,
    *,
    title: str | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> Verdict:
    """Sum the fraud signals present in the posting and compare to the threshold.

    Every signal that fires is recorded whether or not the total rejects, because
    a posting that scored 0.5 is a posting somebody should look at.
    """
    haystack = " ".join(filter(None, [title or "", text or ""]))
    signals, threshold = _fraud_signals(version)
    verdict = Verdict(threshold=threshold)
    if not haystack.strip():
        return verdict

    negation, boundary = _negation(version)

    for signal in signals:
        found = _first_affirmed(signal.patterns, haystack, negation, boundary)
        if found is not None:
            verdict.hits.append(
                Hit(signal.id, signal.label, found.group(0).strip(), signal.weight)
            )
            verdict.score += signal.weight
            # one hit per signal; three fee phrases are still one signal

    if verdict.score >= threshold:
        verdict.rejected = True
        verdict.kind = FRAUD
        strongest = max(verdict.hits, key=lambda hit: hit.weight)
        verdict.rule = strongest.id
        verdict.reason = (
            f"{strongest.label} (fraud score {verdict.score:.2f} ≥ {threshold:.2f})"
        )
    return verdict


def check(
    title: str | None,
    description: str | None = None,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Verdict:
    """The single call the ingest path makes. Junk first — it is cheaper and certain.

    A junk row is not scanned for fraud: there is no posting there to be
    fraudulent, and reporting two rejection reasons for one row obscures which
    one a reviewer should act on.
    """
    junk = check_junk(title, version=version)
    if junk.rejected:
        return junk
    return check_fraud(description, title=title, version=version)
