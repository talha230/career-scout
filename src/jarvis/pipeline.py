"""One pipeline pass — ``jarvis run`` and the daily job in ``jarvis serve`` (PLAN §5, T074).

    discover → screen and score → sufficiency → generate → rule approval
    → poll gmail → backup

**Sending is not in the pipeline.** Generated packages wait in the queue for a
person. The one exception to "a person approves" is the user's own rule set
(I-30): off by default, writable only by them on this computer, applications
only. A rule approval still waits for the send gate like any other.

**A step that fails is an obstacle, not the end of the run.** Every step is
wrapped: a source outage, a disconnected mailbox or a full disk is recorded with
its reason and the remaining steps still run. One ``run`` row of kind ``full``
is written whatever happens — ``ok``, ``partial`` or ``failed`` — because a
scheduled pass that dies without a row is indistinguishable from one that never
started.

Discovery is gated on the Minimum Viable Profile: until it is satisfied the
search itself is skipped, with that reason, and everything else still runs.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from jarvis import matching
from jarvis.profile import functions as profile_functions
from jarvis.store import settings as settings_module
from jarvis.store.db import utcnow

Step = Callable[[], dict[str, Any]]


def _screen_all(conn: sqlite3.Connection, run_id: str, today: date | None) -> dict[str, Any]:
    from jarvis.matching import profile_view

    view = profile_view.build(conn, as_of=today)
    counts = {"screened": 0, "rejected": 0, "scored": 0}
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM opportunity WHERE is_canonical = 1 AND expired_at IS NULL"
    )]
    for opportunity_id in ids:
        # Authenticity rejections are decided at ingest; screening them again
        # would score a posting that is not a real vacancy.
        if conn.execute(
            "SELECT 1 FROM rejection WHERE opportunity_id = ? AND stage = 'authenticity' "
            "AND lifted_at IS NULL", (opportunity_id,),
        ).fetchone():
            continue
        counts["screened"] += 1
        result = matching.screen(conn, opportunity_id, as_of=today, candidate=view,
                                 run_id=run_id)
        if result.rejected:
            counts["rejected"] += 1
        else:
            matching.store_assessment(conn, result.assessment, run_id=run_id)
            counts["scored"] += 1
    return counts


def _sufficiency_all(conn: sqlite3.Connection) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT o.* FROM opportunity o JOIN assessment a ON a.opportunity_id = o.id "
        "WHERE NOT EXISTS (SELECT 1 FROM rejection r WHERE r.opportunity_id = o.id "
        "AND r.lifted_at IS NULL)"
    ).fetchall():
        verdict = profile_functions.sufficiency(conn, dict(row))
        profile_functions.store_verdict(conn, row["id"], verdict)
        conn.execute("UPDATE assessment SET sufficiency = ? WHERE opportunity_id = ?",
                     (verdict.verdict, row["id"]))
        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
    return counts


def _generate_top(conn: sqlite3.Connection) -> dict[str, Any]:
    """Packages for the best sufficient opportunities that do not have a live one."""
    from jarvis.documents import package

    limit = int(settings_module.get(conn, "generate_top_n"))
    candidates = conn.execute(
        "SELECT o.id FROM opportunity o JOIN assessment a ON a.opportunity_id = o.id "
        "JOIN sufficiency_verdict v ON v.opportunity_id = o.id "
        "WHERE v.verdict = 'sufficient' AND COALESCE(a.eligibility_verdict, '') != 'ineligible' "
        "AND NOT EXISTS (SELECT 1 FROM rejection r WHERE r.opportunity_id = o.id "
        "                AND r.lifted_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM application_package p WHERE p.opportunity_id = o.id "
        "                AND p.superseded_by IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM application x WHERE x.opportunity_id = o.id) "
        "ORDER BY a.match_score DESC NULLS LAST LIMIT ?", (limit,),
    ).fetchall()
    counts = {"generated": 0, "blocked": 0, "not_sufficient": 0}
    for row in candidates:
        try:
            pkg = package.generate_package(conn, row["id"])
        except package.NotSufficient:
            counts["not_sufficient"] += 1
            continue
        counts["generated" if pkg["qc_verdict"] == "pass" else "blocked"] += 1
    return counts


def _approve_by_rules(conn: sqlite3.Connection) -> dict[str, Any]:
    from jarvis import approval

    result = approval.approve_by_rules(conn)
    if "skipped" in result:
        raise _Skipped(result["skipped"])
    return result


def run(
    conn: sqlite3.Connection,
    *,
    fetcher: Any = None,
    google_client_factory: Any = None,
    today: date | None = None,
    skip: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Run every step, record one ``run`` row, and return what happened."""
    run_id = str(uuid.uuid4())
    started = utcnow()
    conn.execute("INSERT INTO run (id, kind, started_at, status) VALUES (?, 'full', ?, 'running')",
                 (run_id, started))
    counts: dict[str, Any] = {}
    obstacles: list[dict[str, str]] = []

    def discover() -> dict[str, Any]:
        checklist = profile_functions.minimum_viable_profile(conn)
        if not checklist.satisfied:
            raise _Skipped("the Minimum Viable Profile is not satisfied: "
                           + ", ".join(i.label for i in checklist.outstanding))
        from jarvis import net
        from jarvis.discovery import clients

        own = fetcher is None
        active = fetcher or net.Fetcher()
        try:
            report = clients.discover(conn, active)
        finally:
            if own:
                active.close()
        for obstacle in report.obstacles:
            obstacles.append({"step": "discover", "reason": f"{obstacle['source']}: "
                                                            f"{obstacle['reason']}"})
        return {"status": report.status, "sources": len(report.sources)}

    def gmail() -> dict[str, Any]:
        from jarvis import service

        result = service.poll_mailbox(conn, client_factory=google_client_factory)
        if result["state"] != "ok":
            raise _Skipped(result.get("message") or result["state"])
        return result["counts"]

    def backup() -> dict[str, Any]:
        if not settings_module.get(conn, "backup_enabled"):
            raise _Skipped("backups are switched off in settings")
        from jarvis import backup as backup_module

        return backup_module.take(conn, kind="daily")

    steps: list[tuple[str, Step]] = [
        ("discover", discover),
        ("screen", lambda: _screen_all(conn, run_id, today)),
        ("sufficiency", lambda: _sufficiency_all(conn)),
        ("generate", lambda: _generate_top(conn)),
        ("rule_approval", lambda: _approve_by_rules(conn)),
        ("gmail", gmail),
        ("backup", backup),
    ]
    failed = 0
    for name, step in steps:
        if name in skip:
            continue
        try:
            counts[name] = step()
        except _Skipped as reason:
            obstacles.append({"step": name, "reason": str(reason)})
            counts[name] = {"skipped": str(reason)}
        except Exception as exc:  # noqa: BLE001 - one step never ends the run
            failed += 1
            obstacles.append({"step": name, "reason": f"{type(exc).__name__}: {exc}",
                              "trace": traceback.format_exc(limit=3)})
            counts[name] = {"failed": str(exc)}

    ran = len([s for s in steps if s[0] not in skip])
    status = "ok" if not obstacles else ("failed" if failed == ran else "partial")
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ?, counts = ?, obstacles = ?, error = ? "
        "WHERE id = ?",
        (utcnow(), status, json.dumps(counts, default=str), json.dumps(obstacles),
         None if failed == 0 else f"{failed} step(s) failed", run_id),
    )
    return {"run_id": run_id, "status": status, "counts": counts, "obstacles": obstacles}


class _Skipped(Exception):
    """A step that could not run for a stated, expected reason."""
