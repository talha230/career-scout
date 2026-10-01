"""Versioned configuration — weights, tiers, filters, countries, vocabulary.

Every number that decides a score lives here, not in code. Change a weight in
``scoring_weights.json`` and every score changes; no Python edit is involved.

**Versions are kept, not replaced.** An assessment records the
``config_version`` it was computed under, and an explanation shown a year later
has to reproduce the same arithmetic. That is only possible if the weights that
were in force are still loadable — so versions ship side by side under
``v1/``, ``v2/`` … and :func:`load` takes the version as an argument.

The package ships the defaults. On first run they are copied into
``JARVIS_HOME/config/<version>/`` where the user can edit them; an edited copy
takes precedence over the packaged one, and :func:`fingerprint` records what
was actually loaded so a changed file is visible rather than silent.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

from jarvis.store.paths import get_paths

PACKAGED = Path(__file__).parent

#: The version new assessments are computed under.
CURRENT_VERSION = "v1"

NAMES = (
    "scoring_weights",
    "tiers",
    "hard_filters",
    "skill_synonyms",
    "country_preferences",
    "sources",
    "document_structure",
    "email_categories",
    # Every file that decides a stored value belongs here, because NAMES is what
    # fingerprint() digests. role_families was missing until T031a: editing it
    # changes the family a posting gets, which selects its sufficiency rules and
    # so changes what is generated — with no change of fingerprint to show it.
    "role_families",
    "work_arrangement",
    "authenticity",
    "dedupe",
    "cv_style",
    # money.json holds every period, unit and rate conversion the savings
    # engine applies. Editing it changes a stored net-savings figure and so
    # whether an opportunity passes the floor, which is exactly what the
    # fingerprint exists to make visible.
    "money",
    # eligibility.json decides what SOFT-HIDES an opportunity rather than what
    # scores against it. Editing it changes which roles a user is shown at all,
    # which is the largest effect any config file here has.
    "eligibility",
    # country_packs.json decides which countries are searched at all, and which
    # sources serve each one. Enabling a pack must leave every other country's
    # output byte-identical (I-24), which is only checkable if the file that says
    # so is part of the fingerprint.
    "country_packs",
)


class ConfigMissing(FileNotFoundError):
    """A named config is absent from both the user's directory and the package."""


def available_versions() -> list[str]:
    """Every version this installation can load, packaged or user-edited."""
    found = {p.name for p in PACKAGED.iterdir() if p.is_dir() and p.name.startswith("v")}
    user_root = get_paths(ensure=False).config
    if user_root.is_dir():
        found |= {p.name for p in user_root.iterdir() if p.is_dir() and p.name.startswith("v")}
    return sorted(found)


def resolve(name: str, version: str = CURRENT_VERSION) -> Path:
    """Where a config actually comes from: the user's copy, else the package.

    The user's copy wins so an edit is respected. :func:`fingerprint` records
    the hash either way, so an edit never silently changes a stored score's
    meaning.
    """
    user_copy = get_paths(ensure=False).config / version / f"{name}.json"
    if user_copy.is_file():
        return user_copy

    packaged = PACKAGED / version / f"{name}.json"
    if packaged.is_file():
        return packaged

    raise ConfigMissing(
        f"no {name}.json for config version {version}; "
        f"available versions: {', '.join(available_versions()) or 'none'}"
    )


@lru_cache(maxsize=64)
def _load_cached(name: str, version: str, mtime: float) -> dict[str, Any]:
    return json.loads(resolve(name, version).read_text(encoding="utf-8"))


def load(name: str, version: str = CURRENT_VERSION) -> dict[str, Any]:
    """Load one config.

    Keyed on the file's modification time as well as its name, so a user
    editing a weight while the web app is running sees the new value on the
    next run rather than after a restart.
    """
    path = resolve(name, version)
    return _load_cached(name, version, path.stat().st_mtime)


def fingerprint(version: str = CURRENT_VERSION) -> str:
    """A short digest of every config file in force.

    Recorded beside ``config_version`` on an assessment. Two assessments with
    the same version but different fingerprints were computed under different
    numbers, which is exactly the drift a bare version string hides.
    """
    digest = hashlib.sha256()
    for name in NAMES:
        try:
            digest.update(resolve(name, version).read_bytes())
        except ConfigMissing:
            digest.update(b"<missing>")
    return digest.hexdigest()[:16]


def install_user_copy(version: str = CURRENT_VERSION, *, overwrite: bool = False) -> Path:
    """Copy the packaged config into ``JARVIS_HOME`` so the user can edit it."""
    target = get_paths().config / version
    target.mkdir(parents=True, exist_ok=True)

    source = PACKAGED / version
    if not source.is_dir():
        raise ConfigMissing(f"no packaged config for version {version}")

    for path in source.glob("*.json"):
        destination = target / path.name
        if overwrite or not destination.exists():
            shutil.copy2(path, destination)
    return target


# ------------------------------------------------------------- accessors
# Thin named readers, so call sites do not repeat string literals.


def weights(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("scoring_weights", version)


def tiers(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("tiers", version)


def hard_filters(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("hard_filters", version)


def skill_vocabulary(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("skill_synonyms", version)


def countries(version: str = CURRENT_VERSION) -> dict[str, Any]:
    config = load("country_preferences", version)
    if "_by_code" not in config:
        config = dict(config)
        config["_by_code"] = {c["code"]: c for c in config.get("countries", [])}
    return config


def sources(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("sources", version)


def money(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("money", version)


def eligibility(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("eligibility", version)


def country_packs(version: str = CURRENT_VERSION) -> dict[str, Any]:
    return load("country_packs", version)


def validate(version: str = CURRENT_VERSION) -> list[str]:
    """Check the invariants a config must satisfy. Returns problems, empty if sound.

    Run by ``jarvis doctor`` and by CI. A weight table that does not sum to 1
    silently rescales every score, which is the kind of fault that shows up as
    "the rankings look a bit odd" months later.
    """
    problems: list[str] = []

    try:
        table = weights(version)
    except ConfigMissing as exc:
        return [str(exc)]

    declared = table.get("weights", {})
    total = sum(entry["weight"] for entry in declared.values())
    expected = table.get("weights_must_sum_to", 1.0)
    if abs(total - expected) > 1e-9:
        problems.append(
            f"scoring_weights: component weights sum to {total}, expected {expected}"
        )

    for name, entry in declared.items():
        if not 0 <= entry["weight"] <= 1:
            problems.append(f"scoring_weights: {name} has weight {entry['weight']}")

    try:
        tier_table = tiers(version).get("tiers", [])
    except ConfigMissing as exc:
        problems.append(str(exc))
        tier_table = []

    for tier in tier_table:
        if tier["min_score"] > tier["max_score"]:
            problems.append(f"tiers: {tier['id']} has min above max")

    try:
        vocabulary = skill_vocabulary(version).get("skills", [])
        if len(vocabulary) < 20:
            problems.append(
                f"skill_synonyms: only {len(vocabulary)} skills. The vocabulary decides "
                f"whether a posting's requirements can be read at all; a narrow one makes "
                f"most postings unparseable and therefore not evaluated."
            )
    except ConfigMissing as exc:
        problems.append(str(exc))

    return problems
