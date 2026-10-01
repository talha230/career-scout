"""Judging when two postings are the same vacancy — T030b.

Three rules, applied in descending order of confidence. Every match records which
rule fired and how close it was, so a merge can be inspected rather than trusted.

The canonical record is the **oldest seen**. A later arrival is marked
non-canonical and pointed at it; nothing is deleted, because a merge is a
judgement that hides one vacancy behind another and a wrong one must be
recoverable.

**Every rule is an indexed lookup.** An earlier version compared the candidate
against all canonical postings, which is quadratic over the whole table and does
not finish at real feed sizes. Fuzzy comparison now happens only within the
candidate's own employer — the only place a genuine cross-posting can be, since
two different employers are never the same job.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from career_scout import config as config_module
from career_scout.store.db import utcnow

IDENTICAL_KEY = "identical_dedupe_key"
IDENTICAL_URL = "identical_url"
SIMILARITY = "employer_title_place_similarity"


@lru_cache(maxsize=8)
def _thresholds(version: str) -> tuple[float, float]:
    config = config_module.load("dedupe", version)
    return (
        float(config["title_similarity_threshold"]),
        float(config["city_similarity_threshold"]),
    )


def similarity(left: str | None, right: str | None, *, floor: float = 0.0) -> float:
    """Title similarity, optionally short-circuited below ``floor``.

    ``ratio()`` is the expensive part of dedupe: it runs for every pair of
    postings an employer has open, which is quadratic within that employer and
    dominates a full corpus load once the table is large.

    ``real_quick_ratio`` and ``quick_ratio`` are documented **upper bounds** on
    ``ratio``, computed from length and from character counts respectively. When
    an upper bound already falls below the floor the real ratio cannot reach it,
    so returning 0.0 there is exact, not approximate — this changes runtime and
    never changes a merge decision.
    """
    if not left or not right:
        return 0.0
    matcher = SequenceMatcher(None, left, right)
    if floor > 0.0 and (
        matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor
    ):
        return 0.0
    return matcher.ratio()


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    """A merge, and the argument for it."""

    canonical_id: str
    duplicate_id: str | None
    rule: str
    similarity: float
    evidence: str


def _same_place(
    left: sqlite3.Row | dict,
    right: sqlite3.Row | dict,
    city_threshold: float,
) -> tuple[bool, str]:
    """Two postings are in the same place if their countries agree, or both are remote.

    An unresolved country is **not** a match. 715 postings in the corpus carry no
    country at all, so treating two unknowns as equal would merge them wholesale.
    """
    left_country, right_country = left["country_iso2"], right["country_iso2"]
    if left_country and right_country:
        if left_country != right_country:
            return False, f"different countries ({left_country} vs {right_country})"
        left_city, right_city = left["city"], right["city"]
        if left_city and right_city:
            score = similarity(left_city.lower(), right_city.lower())
            if score < city_threshold:
                return False, f"different cities ({left_city} vs {right_city})"
        return True, f"same country {left_country}"

    if left["work_arrangement"] == "remote" and right["work_arrangement"] == "remote":
        return True, "both remote"
    return False, "location not comparable"


def find_duplicate(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> DuplicateMatch | None:
    """Find the canonical opportunity this candidate duplicates, if any.

    ``candidate`` is the normalised posting about to be stored; it may or may not
    already have a row. Pass ``id`` when it does, so a posting is never compared
    against itself.
    """
    title_threshold, city_threshold = _thresholds(version)
    candidate_id = candidate.get("id")

    # Rule 1 — identical dedupe key. Employer, title and place all agree.
    # `opportunity_dedupe` is a UNIQUE index, so this is a single indexed probe.
    #
    # Deliberately NOT restricted to canonical rows. A posting can hold this key
    # and already be merged into some other vacancy by rule 2 or 3; looking only
    # at canonical rows then finds nothing, and the caller goes on to insert a
    # second row with a key the UNIQUE index already holds. Found by replaying the
    # 4,402-posting corpus, where it raised IntegrityError. The match resolves
    # through the chain, so the sighting attaches to whatever row now represents
    # the vacancy.
    row = conn.execute(
        "SELECT id FROM opportunity WHERE dedupe_key = ? AND id IS NOT ?",
        (candidate["dedupe_key"], candidate_id),
    ).fetchone()
    if row:
        return DuplicateMatch(
            canonical_id=canonical_for(conn, row["id"]),
            duplicate_id=candidate_id,
            rule=IDENTICAL_KEY,
            similarity=1.0,
            evidence=f"identical dedupe key {candidate['dedupe_key']}",
        )

    # Rule 2 — identical apply URL once campaign tracking is stripped. Same
    # reasoning: the same URL is the same vacancy whether or not the row holding
    # it is the one that now represents it.
    url = candidate.get("url_canonical")
    if url:
        row = conn.execute(
            "SELECT id FROM opportunity "
            "WHERE url_canonical = ? AND id IS NOT ? "
            "ORDER BY first_seen_at ASC LIMIT 1",
            (url, candidate_id),
        ).fetchone()
        if row:
            return DuplicateMatch(
                canonical_id=canonical_for(conn, row["id"]),
                duplicate_id=candidate_id,
                rule=IDENTICAL_URL,
                similarity=1.0,
                evidence=f"same canonical URL {url}",
            )

    # Rule 3 — same employer, then a near-identical title in the same place.
    # Bounded by how many roles one employer has open, via the employer_norm index.
    siblings = conn.execute(
        "SELECT * FROM opportunity "
        "WHERE employer_norm = ? AND id IS NOT ? AND is_canonical = 1 "
        "ORDER BY first_seen_at ASC",
        (candidate["employer_norm"], candidate_id),
    ).fetchall()

    for other in siblings:
        score = similarity(
            other["title_norm"], candidate["title_norm"], floor=title_threshold
        )
        if score < title_threshold:
            continue
        place_ok, place_reason = _same_place(other, candidate, city_threshold)
        if not place_ok:
            continue
        return DuplicateMatch(
            canonical_id=other["id"],
            duplicate_id=candidate_id,
            rule=SIMILARITY,
            similarity=score,
            evidence=(
                f"employer '{candidate['employer_norm']}', title similarity {score:.3f} "
                f"('{other['title']}' vs '{candidate['title']}'), {place_reason}"
            ),
        )

    return None


def record_duplicate(
    conn: sqlite3.Connection,
    match: DuplicateMatch,
    *,
    run_id: str | None = None,
    version: str = config_module.CURRENT_VERSION,
) -> None:
    """Mark the duplicate non-canonical and write the evidence for the merge.

    The caller owns the transaction. Both statements must land together: a row
    marked non-canonical with no evidence row is a merge nobody can inspect.
    """
    if match.duplicate_id is None:
        raise ValueError("cannot record a merge for a candidate that has no row yet")

    conn.execute(
        "UPDATE opportunity SET is_canonical = 0, canonical_id = ? WHERE id = ?",
        (match.canonical_id, match.duplicate_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO opportunity_duplicate "
        "(canonical_id, duplicate_id, rule, similarity, evidence, config_version, "
        " run_id, merged_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            match.canonical_id,
            match.duplicate_id,
            match.rule,
            match.similarity,
            match.evidence,
            version,
            run_id,
            utcnow(),
        ),
    )


def record_additional_source(
    conn: sqlite3.Connection,
    opportunity_id: str,
    *,
    source_id: str,
    source_url: str,
    fetched_at: str | None = None,
) -> None:
    """Note that an existing posting also appeared in another source.

    This is the rule-1 case where the candidate has no row of its own: the same
    vacancy arriving from a second board is not a new opportunity, it is another
    sighting of one. ``last_seen_at`` moves; ``first_seen_at`` never does.
    """
    seen = fetched_at or utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO opportunity_source "
        "(opportunity_id, source_id, source_url, fetched_at) VALUES (?, ?, ?, ?)",
        (opportunity_id, source_id, source_url, seen),
    )
    conn.execute(
        "UPDATE opportunity SET last_seen_at = ? WHERE id = ?",
        (seen, opportunity_id),
    )


def canonical_for(conn: sqlite3.Connection, opportunity_id: str) -> str:
    """Resolve a posting to the canonical row that represents it.

    Follows the chain rather than assuming one hop: B may merge into A, then C
    into B before A and B are compacted. The walk is bounded by the number of
    rows visited, so a cycle written by a bug cannot hang the pipeline.
    """
    seen: set[str] = set()
    current = opportunity_id
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT canonical_id, is_canonical FROM opportunity WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row["is_canonical"] or not row["canonical_id"]:
            return current
        current = row["canonical_id"]
    return current
