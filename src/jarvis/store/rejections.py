"""The one writer of ``rejection`` rows, shared by both stages that reject.

A rejection is a verdict under the rules as they stood, not a fact about the
posting. So each re-assessment **reconciles** its own stage's rows with its
latest verdict: still rejected under the same rule is a no-op (no duplicate
row per run); passing lifts every active row of that stage. Lifting stamps the
row rather than deleting it, so the history of a verdict stays readable.

A rejection under a *different* rule of the same stage does not lift the old
one. Both checks return the first rule that fires, so an unreported rule has
not been shown to pass — lifting it would record a pass nobody measured.
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

from jarvis.store.db import utcnow

AUTHENTICITY = "authenticity"
HARD_FILTER = "hard_filter"


class Verdict(NamedTuple):
    rule: str
    reason: str
    evidence: str | None = None


def reconcile(
    conn: sqlite3.Connection,
    opportunity_id: str,
    stage: str,
    verdict: Verdict | None,
    *,
    run_id: str | None = None,
    at: str | None = None,
) -> None:
    """Bring ``stage``'s active rejections for one posting in line with ``verdict``.

    ``verdict`` is ``None`` when the posting passed that stage.
    """
    at = at or utcnow()
    if verdict is None:
        conn.execute(
            "UPDATE rejection SET lifted_at = ?, lifted_reason = ?, lifted_run_id = ? "
            "WHERE opportunity_id = ? AND stage = ? AND lifted_at IS NULL",
            (at, f"passed {stage} on re-assessment", run_id, opportunity_id, stage),
        )
        return

    # The partial UNIQUE index makes an already-active rule a no-op.
    conn.execute(
        "INSERT OR IGNORE INTO rejection "
        "(opportunity_id, stage, rule, reason, evidence, run_id, rejected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (opportunity_id, stage, verdict.rule, verdict.reason, verdict.evidence, run_id, at),
    )
