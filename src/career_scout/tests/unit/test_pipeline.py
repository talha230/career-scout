"""One pipeline pass and the schedule around it — PLAN §5, T074."""

from __future__ import annotations

import json
import sqlite3

import pytest

from career_scout import pipeline, scheduler
from career_scout.profile import records
from career_scout.store import settings as settings_module
from career_scout.tests.invariants.test_documents import _opportunity, _profile


def _mvp(conn: sqlite3.Connection) -> None:
    _profile(conn)
    records.add(conn, "identity.citizenship", "PK", confirmed=True)
    records.add(conn, "identity.tax_residence", "PK", confirmed=True)


def test_a_run_always_writes_a_row_and_never_sends(db) -> None:
    result = pipeline.run(db, skip=frozenset({"discover"}))
    row = db.execute("SELECT * FROM run WHERE id = ?", (result["run_id"],)).fetchone()
    assert row["status"] in {"ok", "partial"} and row["finished_at"]
    assert db.execute("SELECT count(*) c FROM send_log").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) c FROM approval").fetchone()["c"] == 0


def test_discovery_waits_for_the_minimum_viable_profile(db) -> None:
    result = pipeline.run(db, skip=frozenset({"backup"}))
    discover = next(o for o in result["obstacles"] if o["step"] == "discover")
    assert "Minimum Viable Profile" in discover["reason"]
    assert "screen" in result["counts"], "the other steps still ran"


def test_a_pass_scores_checks_and_generates_the_top_opportunities(db) -> None:
    _mvp(db)
    settings_module.set_value(db, "generate_top_n", 1)
    for title in ("Industrial Engineer", "Process Engineer"):
        _opportunity(db, title=title)

    result = pipeline.run(db, skip=frozenset({"discover", "gmail", "backup"}))

    assert result["counts"]["screen"]["scored"] == 2
    assert result["counts"]["sufficiency"].get("sufficient") == 2
    assert result["counts"]["generate"]["generated"] == 1          # capped by the setting
    assert db.execute("SELECT count(*) c FROM assessment").fetchone()["c"] == 2
    queue = db.execute("SELECT state FROM application_package").fetchall()
    assert [r["state"] for r in queue] == ["awaiting_approval"]


def test_a_second_pass_does_not_regenerate_a_live_package(db) -> None:
    _mvp(db)
    _opportunity(db)
    pipeline.run(db, skip=frozenset({"discover", "gmail", "backup"}))
    again = pipeline.run(db, skip=frozenset({"discover", "gmail", "backup"}))
    assert again["counts"]["generate"]["generated"] == 0
    assert db.execute("SELECT count(*) c FROM application_package").fetchone()["c"] == 1


def test_a_failing_step_is_an_obstacle_not_the_end(db, monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pipeline, "_screen_all", broken)
    result = pipeline.run(db, skip=frozenset({"discover", "backup"}))
    assert result["status"] == "partial"
    assert any(o["step"] == "screen" and "disk full" in o["reason"] for o in result["obstacles"])
    assert "sufficiency" in result["counts"], "later steps still ran"
    stored = db.execute("SELECT obstacles FROM run WHERE id = ?", (result["run_id"],)).fetchone()
    assert "disk full" in stored["obstacles"]


def test_an_unconnected_mailbox_is_an_obstacle_with_its_reason(db) -> None:
    result = pipeline.run(db, skip=frozenset({"discover", "backup"}))
    gmail = next(o for o in result["obstacles"] if o["step"] == "gmail")
    assert "setup google" in gmail["reason"]


def test_the_backup_step_takes_a_backup(db) -> None:
    result = pipeline.run(db, skip=frozenset({"discover", "gmail"}))
    assert result["counts"]["backup"]["kind"] in {"daily", "weekly"}
    assert db.execute("SELECT status FROM backup_record").fetchone()["status"] == "ok"


# ------------------------------------------------------------------ T074


def test_the_schedule_comes_from_settings(db) -> None:
    settings_module.set_value(db, "discovery_schedule_hour", 5)
    settings_module.set_value(db, "gmail_poll_minutes", 30)
    background = scheduler.build()
    jobs = {job.id: job for job in background.get_jobs()}
    assert set(jobs) == {"daily", "gmail"}
    assert "hour='5'" in str(jobs["daily"].trigger)
    assert jobs["gmail"].trigger.interval.total_seconds() == 30 * 60
    assert all(job.max_instances == 1 for job in jobs.values())


def test_a_scheduled_job_that_crashes_still_leaves_a_run_row(db, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("could not even start")

    monkeypatch.setattr(pipeline, "run", explode)
    scheduler.daily_job()
    row = db.execute("SELECT * FROM run WHERE kind = 'full'").fetchone()
    assert row["status"] == "failed" and "could not even start" in row["error"]
    assert json.loads(row["obstacles"])


def test_reconcile_records_a_send_or_its_absence_and_never_resends(db) -> None:
    from typer.testing import CliRunner

    from career_scout import cli, send
    from career_scout.tests.invariants.test_send import CONTENT_HASH, _seed

    settings_module.set_value(db, "channel_email_autosend", True)
    approval_id, _ = _seed(db)

    def dies(_envelope):
        raise ConnectionError("network dropped after the provider accepted it")

    with pytest.raises(ConnectionError):
        send.send_approved(db, approval_id, expected_content_hash=CONTENT_HASH,
                           transport=dies, mailbox_credential_id="credential-1")
    stuck = send.unreconciled(db)[0]["id"]

    runner = CliRunner()
    assert runner.invoke(cli.app, ["reconcile", stuck]).exit_code == 2      # needs a choice
    result = runner.invoke(cli.app, ["reconcile", stuck, "--not-sent"])
    assert result.exit_code == 0, result.output
    row = db.execute("SELECT status FROM application WHERE id = ?", (stuck,)).fetchone()
    assert row["status"] == "withdrawn" and send.unreconciled(db) == []
    assert db.execute("SELECT count(*) c FROM send_log").fetchone()["c"] == 1   # no resend


@pytest.mark.parametrize("job", ["daily_job", "gmail_job"])
def test_jobs_open_their_own_session(job) -> None:
    import inspect

    source = inspect.getsource(getattr(scheduler, job))
    assert "session()" in source
