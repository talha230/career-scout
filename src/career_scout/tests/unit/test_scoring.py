"""The scoring core: renormalisation, confidence, tiers, and hand-reproducibility."""

from __future__ import annotations

import pytest

from career_scout import config as config_module
from career_scout.matching.scoring import (
    Component,
    assign_tier,
    combine,
    component_weights,
    confidence_for,
    unscored,
)

pytestmark = pytest.mark.usefixtures("career_scout_home")


def _scored(name: str, score: float, weight: float) -> Component:
    return Component(
        name=name, score=score, weight=weight,
        formula=f"{name} = stated", inputs={"value": score},
    )


# -------------------------------------------------------------------- config


def test_packaged_config_is_valid() -> None:
    """A weight table that does not sum to 1 silently rescales every score."""
    assert config_module.validate() == []


def test_component_weights_sum_to_one() -> None:
    assert sum(component_weights().values()) == pytest.approx(1.0)


def test_fingerprint_is_stable_and_short() -> None:
    first = config_module.fingerprint()
    assert first == config_module.fingerprint()
    assert len(first) == 16


def test_a_user_edit_changes_the_fingerprint(paths) -> None:
    """An edited weight must not silently change what a stored score meant."""
    before = config_module.fingerprint()
    config_module.install_user_copy()
    edited = paths.config / config_module.CURRENT_VERSION / "tiers.json"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert config_module.fingerprint() != before


def test_missing_version_names_what_is_available() -> None:
    with pytest.raises(config_module.ConfigMissing, match="available versions"):
        config_module.load("scoring_weights", "v99")


# --------------------------------------------------------------- combination


def test_a_fully_scored_result_uses_declared_weights() -> None:
    components = [_scored("a", 80, 0.5), _scored("b", 60, 0.5)]
    result = combine(components)
    assert result.total == pytest.approx(70.0)
    assert result.confidence == "high"
    assert result.unscored == []


def test_total_is_reproducible_by_hand_from_the_stored_row() -> None:
    """The definition of done for every number this project shows."""
    result = combine([_scored("a", 83, 0.25), _scored("b", 61, 0.2), _scored("c", 44, 0.15)])
    assert result.recompute() == pytest.approx(result.total)


def test_effective_weights_are_stored_at_full_precision() -> None:
    """Rounding to 6dp made them sum to 1.000002 and hand recomputation drifted."""
    result = combine([_scored("a", 50, 0.25), _scored("b", 50, 0.2), _scored("c", 50, 0.15)])
    assert sum(result.effective_weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_an_unscored_component_is_dropped_and_the_rest_renormalised() -> None:
    """Not zero-filled, not neutral-filled. Both are invented numbers."""
    result = combine([
        _scored("a", 80, 0.5),
        unscored("b", 0.5, "the posting discloses no salary"),
    ])
    assert result.total == pytest.approx(80.0)
    assert result.unscored == ["b"]
    assert result.effective_weights["a"] == pytest.approx(1.0)
    assert result.effective_weights["b"] == 0.0


def test_an_unscored_component_contributes_nothing_and_says_why() -> None:
    result = combine([_scored("a", 80, 0.5), unscored("b", 0.5, "no salary disclosed")])
    body = result.components["b"]
    assert body["score"] is None
    assert body["contribution"] is None
    assert body["unscored_reason"] == "no salary disclosed"


def test_an_unscored_component_must_carry_a_reason() -> None:
    """Silence is indistinguishable from a bug."""
    with pytest.raises(ValueError, match="must say why"):
        Component(name="x", score=None, weight=0.1, formula="none")


def test_a_score_outside_the_range_is_refused() -> None:
    with pytest.raises(ValueError, match="outside 0-100"):
        Component(name="x", score=140, weight=0.1, formula="none")


def test_everything_unscored_is_unscored_not_zero() -> None:
    """Zero would rank this below a posting that genuinely scored badly."""
    result = combine([unscored("a", 0.5, "no data"), unscored("b", 0.5, "no data")])
    assert result.tier == "UNSCORED"
    assert result.arithmetic == "UNSCORED"
    assert result.unscored == ["a", "b"]


def test_the_formula_names_what_was_redistributed() -> None:
    result = combine([_scored("a", 80, 0.5), unscored("salary", 0.5, "not disclosed")])
    assert "salary" in result.formula
    assert "effective_weight" in result.formula


def test_arithmetic_string_expands_every_term() -> None:
    result = combine([_scored("a", 80, 0.5), _scored("b", 60, 0.5)])
    assert result.arithmetic.count("x") == 2
    assert result.arithmetic.endswith("= 70.00")


# --------------------------------------------------------------- confidence


def test_confidence_reflects_how_much_was_measurable() -> None:
    assert confidence_for(0) == "high"
    assert confidence_for(1) == "medium"
    assert confidence_for(2) == "medium"
    assert confidence_for(3) == "low"


def test_the_legacy_salary_case_lands_at_medium() -> None:
    """86% of real postings disclose no pay; that alone must not read as low trust."""
    result = combine([
        _scored("skill_match", 70, 0.25),
        _scored("experience", 100, 0.2),
        unscored("salary", 0.1, "the posting discloses no salary"),
    ])
    assert result.confidence == "medium"


# -------------------------------------------------------------------- tiers


def test_a_high_score_on_thin_evidence_is_capped() -> None:
    """Nine components at 90 is a different claim from three components at 90."""
    high = assign_tier(95.0, "high")
    thin = assign_tier(95.0, "low")
    assert thin != high
    assert thin == "B"


def test_tier_is_assigned_from_config_not_code() -> None:
    table = config_module.tiers()["tiers"]
    for tier in table:
        midpoint = (tier["min_score"] + tier["max_score"]) / 2
        assert assign_tier(midpoint, "high") in {t["id"] for t in table}


# -------------------------------------------------------------- provenance


def test_every_result_records_the_config_it_used() -> None:
    """Without this, an explanation shown a year later cannot be checked."""
    result = combine([_scored("a", 80, 1.0)])
    assert result.config_version == config_module.CURRENT_VERSION
    assert len(result.config_fingerprint) == 16


def test_identical_inputs_give_identical_results() -> None:
    first = combine([_scored("a", 83, 0.25), _scored("b", 61, 0.2)])
    second = combine([_scored("a", 83, 0.25), _scored("b", 61, 0.2)])
    assert first.total == second.total
    assert first.arithmetic == second.arithmetic
    assert first.effective_weights == second.effective_weights
