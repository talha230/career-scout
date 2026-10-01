"""The schedule inside ``jarvis serve`` — T074.

Two jobs:

``daily``
    One full pipeline pass at ``discovery_schedule_hour`` (local time). It writes
    a ``run`` row whatever happens, because a scheduled pass that dies without a
    trace is indistinguishable from one that never started.
``gmail``
    A mailbox poll every ``gmail_poll_minutes``, so replies arrive between daily
    runs. It too writes a ``run`` row, and a disconnection is recorded as one —
    never as a quiet inbox.

Each job opens its own database session: the web app's request connections are
never shared with a background thread. A job that is still running when its
next turn comes is skipped rather than stacked (``max_instances=1``).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jarvis.store import settings as settings_module
from jarvis.store.db import session, utcnow

log = logging.getLogger("jarvis.scheduler")


def _record_crash(kind: str, exc: BaseException) -> None:
    """If the job itself could not even start its run row, still leave one."""
    try:
        with session() as conn:
            conn.execute(
                "INSERT INTO run (id, kind, started_at, finished_at, status, obstacles, error) "
                "VALUES (?, ?, ?, ?, 'failed', ?, ?)",
                (str(uuid.uuid4()), kind, utcnow(), utcnow(),
                 json.dumps([{"step": kind, "reason": str(exc)}]),
                 f"{type(exc).__name__}: {exc}"),
            )
    except Exception:  # noqa: BLE001 - last resort; the log line below still says it
        log.exception("could not record the failed %s run", kind)


def daily_job() -> None:
    from jarvis import pipeline

    try:
        with session() as conn:
            pipeline.run(conn)
    except Exception as exc:  # noqa: BLE001
        log.exception("daily run failed")
        _record_crash("full", exc)


def gmail_job() -> None:
    from jarvis import service

    try:
        with session() as conn:
            service.poll_mailbox(conn)
    except Exception as exc:  # noqa: BLE001
        log.exception("gmail poll failed")
        _record_crash("gmail", exc)


def build() -> BackgroundScheduler:
    """A scheduler with both jobs, configured from settings. Not started."""
    with session() as conn:
        hour = int(settings_module.get(conn, "discovery_schedule_hour"))
        poll_minutes = int(settings_module.get(conn, "gmail_poll_minutes"))
    scheduler = BackgroundScheduler()
    scheduler.add_job(daily_job, CronTrigger(hour=hour, minute=0), id="daily",
                      max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(gmail_job, IntervalTrigger(minutes=poll_minutes), id="gmail",
                      max_instances=1, coalesce=True)
    return scheduler


def describe(scheduler: BackgroundScheduler) -> list[dict[str, Any]]:
    return [{"id": job.id, "next_run": str(job.next_run_time) if job.next_run_time else None}
            for job in scheduler.get_jobs()]
