"""Deterministic weighted scoring — T034.

Ported from the legacy ``jobfinder.phase3.scoring``, whose core was already
right: a weighted sum over components that each carry their own formula and
inputs, with renormalisation when a component cannot be computed.

Three properties are preserved exactly, because they are the reason the numbers
mean anything.

**No component calls a model.** Every score is arithmetic over stored inputs.

**An uncomputable component is ``UNSCORED``, never zero and never 50.** Both of
those are invented numbers wearing the costume of a measurement. Its weight is
removed and the rest renormalised, and the row records which components were
dropped and what the effective weights became. In the legacy corpus this was
not a corner case: salary was unscorable in every one of 2,305 assessments,
because 86% of postings disclose no pay.

**The stored row is enough to recompute the total by hand.** Effective weights
are stored at full precision — the legacy code found that rounding them to six
decimal places made them sum to 1.000002, and a hand recomputation then drifted
from the engine. Rounding belongs in the display layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jarvis import config as config_module


@dataclass(slots=True)
class Component:
    """One scored dimension, carrying everything needed to check it."""

    name: str
    score: float | None
    weight: float
    formula: str
    inputs: dict[str, Any] = field(default_factory=dict)
    unscored_reason: str | None = None

    def __post_init__(self) -> None:
        if self.score is None and not self.unscored_reason:
            raise ValueError(
                f"component {self.name!r} is unscored with no reason. An unscored "
                f"component must say why, or it is indistinguishable from a bug."
            )
        if self.score is not None and not 0 <= self.score <= 100:
            raise ValueError(f"component {self.name!r} scored {self.score}, outside 0-100")

    @property
    def scored(self) -> bool:
        return self.score is not None

    def as_dict(self, effective_weight: float) -> dict[str, Any]:
        return {
            "score": self.score,
            "weight": self.weight,
            "effective_weight": effective_weight,
            "contribution": None if self.score is None else self.score * effective_weight,
            "formula": self.formula,
            "inputs": self.inputs,
            "unscored_reason": self.unscored_reason,
        }


@dataclass(slots=True)
class Score:
    """The complete result, reproducible by hand from its own fields."""

    total: float
    tier: str
    confidence: str
    components: dict[str, dict[str, Any]]
    effective_weights: dict[str, float]
    unscored: list[str]
    formula: str
    arithmetic: str
    config_version: str
    config_fingerprint: str

    def recompute(self) -> float:
        """Recompute the total from the stored components. Used by the trace test."""
        return sum(
            body["score"] * body["effective_weight"]
            for body in self.components.values()
            if body["score"] is not None
        )


def combine(
    components: list[Component],
    *,
    config_version: str = config_module.CURRENT_VERSION,
) -> Score:
    """Weighted sum with renormalisation over the components that could be scored."""
    scored = [c for c in components if c.scored]
    unscored = [c.name for c in components if not c.scored]

    if not scored:
        # Every component failed. Reporting 0 would rank this posting below one
        # that genuinely scored badly, which is a different claim.
        return Score(
            total=0.0,
            tier="UNSCORED",
            confidence="low",
            components={c.name: c.as_dict(0.0) for c in components},
            effective_weights=dict.fromkeys(unscored, 0.0),
            unscored=unscored,
            formula="no component could be scored from available data",
            arithmetic="UNSCORED",
            config_version=config_version,
            config_fingerprint=config_module.fingerprint(config_version),
        )

    scored_weight_total = sum(c.weight for c in scored)
    effective = {c.name: c.weight / scored_weight_total for c in scored}
    for name in unscored:
        effective[name] = 0.0

    total = sum(c.score * effective[c.name] for c in scored)  # type: ignore[operator]
    confidence = confidence_for(len(unscored), config_version)

    terms = " + ".join(f"({c.score:.2f} x {effective[c.name]:.4f})" for c in scored)
    return Score(
        total=total,
        tier=assign_tier(total, confidence, config_version),
        confidence=confidence,
        components={c.name: c.as_dict(effective[c.name]) for c in components},
        effective_weights=effective,
        unscored=unscored,
        formula=(
            "total = SUM(component_score x effective_weight); effective_weight = "
            "component_weight / SUM(weights of scored components). "
            f"Unscored and redistributed: {unscored or 'none'}."
        ),
        arithmetic=f"{terms} = {total:.2f}",
        config_version=config_version,
        config_fingerprint=config_module.fingerprint(config_version),
    )


def confidence_for(unscored_count: int, version: str = config_module.CURRENT_VERSION) -> str:
    """A 78 built from six components is not the same claim as a 78 built from nine."""
    if unscored_count == 0:
        return "high"
    if unscored_count <= 2:
        return "medium"
    return "low"


def assign_tier(
    total: float, confidence: str, version: str = config_module.CURRENT_VERSION
) -> str:
    """Bucket a score, refusing to award a top tier on thin evidence."""
    table = config_module.tiers(version)
    for tier in table.get("tiers", []):
        if tier["min_score"] <= total <= tier["max_score"]:
            required = tier.get("requires_confidence_at_least")
            if required and confidence == "low" and required in ("medium", "high"):
                return "B"  # cap a thin-evidence high score
            return tier["id"]
    return table.get("filtered_tier", "REJECT")


def component_weights(version: str = config_module.CURRENT_VERSION) -> dict[str, float]:
    """The declared weight of each component, from config."""
    return {
        name: entry["weight"]
        for name, entry in config_module.weights(version).get("weights", {}).items()
    }


def unscored(name: str, weight: float, reason: str, **inputs: Any) -> Component:
    """Build an UNSCORED component. The reason is mandatory by construction."""
    return Component(
        name=name,
        score=None,
        weight=weight,
        formula="not computed",
        inputs=inputs,
        unscored_reason=reason,
    )
