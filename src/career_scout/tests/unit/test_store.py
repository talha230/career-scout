"""Paths, connection pragmas and the migration runner."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from career_scout.store import paths as paths_module
from career_scout.store.db import connect, immediate, migrate, transaction, utcnow

# ----------------------------------------------------------------- paths


def test_career_scout_home_env_wins(career_scout_home: Path) -> None:
    assert paths_module.resolve_root() == career_scout_home.resolve()


def test_blank_career_scout_home_falls_back_to_platform_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank value must not resolve to the current directory.

    Treating "" as a path scatters a user's CV and passport into whatever
    directory they happened to run the command from.
    """
    monkeypatch.setenv(paths_module.ENV_HOME, "   ")
    paths_module.reset_cache()
    root = paths_module.resolve_root()
    assert root != Path.cwd()
    assert "career-scout" in str(root).lower()


def test_ensure_creates_the_whole_layout(paths: paths_module.Paths) -> None:
    for relative in paths_module.LAYOUT:
        assert (paths.root / relative).is_dir(), relative


def test_credentials_directory_is_not_world_readable(paths: paths_module.Paths) -> None:
    mode = paths.credentials.stat().st_mode
    if os.name == "posix":
        assert mode & 0o077 == 0, oct(mode)
    else:
        # On Windows the POSIX bits are advisory; harden_directory resets the
        # ACL instead. Assert the call path ran rather than the bits.
        assert paths.credentials.is_dir()


def test_named_paths_sit_under_the_root(paths: paths_module.Paths) -> None:
    for candidate in (
        paths.db,
        paths.source_documents,
        paths.restricted_documents,
        paths.client_secret,
        paths.google_token,
        paths.data_key,
        paths.backups,
    ):
        assert paths.root in candidate.parents or candidate.parent == paths.root


# ------------------------------------------------------------ connection


def test_pragmas_are_set(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_keys_are_enforced(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO rejection (opportunity_id, rule, reason, rejected_at) "
            "VALUES ('no-such-opportunity', 'F01', 'x', ?)",
            (utcnow(),),
        )


def test_transaction_rolls_back_on_error(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError), transaction(db):
        db.execute(
            "INSERT INTO country (iso2, name, enabled) VALUES ('ZZ', 'Nowhere', 1)"
        )
        raise ValueError("deliberate")
    assert db.execute("SELECT COUNT(*) FROM country WHERE iso2='ZZ'").fetchone()[0] == 0


def test_immediate_takes_the_write_lock_up_front(db: sqlite3.Connection) -> None:
    """A second writer must be refused while an IMMEDIATE transaction is open.

    This is the property the send caps rely on: read-then-act is only safe if
    the lock is held from the read, not from the first write.
    """
    other = connect()
    other.execute("PRAGMA busy_timeout = 0")
    try:
        with immediate(db), pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


# ------------------------------------------------------------ migrations


def test_migrate_is_idempotent(memory_db: sqlite3.Connection) -> None:
    assert migrate(memory_db) == []  # already applied by the fixture


def test_every_migration_is_recorded(memory_db: sqlite3.Connection) -> None:
    recorded = {r["version"] for r in memory_db.execute("SELECT version FROM schema_migration")}
    assert "001_initial" in recorded


def test_003_backfills_stage_and_retires_repeat_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows written before 003 get a stage; per-run repeats are lifted, not deleted."""
    import career_scout.store.db as db_module

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    everything = db_module._migration_files()
    monkeypatch.setattr(db_module, "_migration_files", lambda: everything[:2])
    migrate(conn)
    conn.execute(
        "INSERT INTO opportunity (id, kind, title, employer, source_url, fetched_at, "
        "dedupe_key, employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('o', 'job', 't', 'e', 'u', 'x', 'k', 'e', 't', 'x', 'x')"
    )
    conn.executemany(
        "INSERT INTO rejection (opportunity_id, rule, reason, rejected_at) VALUES ('o', ?, 'r', ?)",
        [("F08_x", "2026-01-01"), ("F08_x", "2026-01-02"), ("J01_x", "2026-01-03")],
    )

    monkeypatch.setattr(db_module, "_migration_files", lambda: everything)
    assert migrate(conn)[0] == "003_rejection_lift"

    rows = conn.execute("SELECT * FROM rejection ORDER BY id").fetchall()
    assert [r["stage"] for r in rows] == ["hard_filter", "hard_filter", "authenticity"]
    assert [r["lifted_at"] for r in rows] == [None, "2026-01-02", None]
    assert rows[1]["lifted_reason"] == f"repeat of rejection #{rows[0]['id']}"


def test_expected_tables_exist(memory_db: sqlite3.Connection) -> None:
    present = {
        r["name"]
        for r in memory_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    expected = {
        "country", "source", "opportunity", "opportunity_source", "reference_figure",
        "fx_rate", "document", "profile_record", "worked_example", "setting",
        "assessment", "rejection", "sufficiency_rule", "sufficiency_verdict",
        "application_package", "approval", "application", "application_status_history",
        "reply", "contact", "contact_suppression", "outreach_message", "credential",
        "run", "send_log", "backup_record",
    }
    assert expected <= present, expected - present


# --------------------------------------------- schema rules that are guarantees


def test_country_floor_requires_its_basis(memory_db: sqlite3.Connection) -> None:
    """A sourced number cannot be stored without its source.

    Pakistan's default floor stays NULL until a figure is sourced; the schema
    makes "2000 because that is the fallback" unstorable as a country default.
    """
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO country (iso2, name, enabled, default_savings_floor) "
            "VALUES ('PK', 'Pakistan', 1, 2000.0)"
        )


def test_opportunity_requires_source_and_timestamp(memory_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO opportunity "
            "(id, kind, title, employer, source_url, fetched_at, dedupe_key, "
            " employer_norm, title_norm, first_seen_at, last_seen_at) "
            "VALUES ('o1', 'job', 'Engineer', 'Acme', NULL, NULL, 'k', 'acme', 'engineer', ?, ?)",
            (utcnow(), utcnow()),
        )


def test_contact_requires_a_published_source(memory_db: sqlite3.Connection) -> None:
    """Only published contact details may be stored, enforced structurally."""
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO contact (id, name, email, published_source_url, "
            "published_read_at, created_at) VALUES ('c1', 'Prof X', 'x@uni.de', NULL, ?, ?)",
            (utcnow(), utcnow()),
        )


def test_sufficiency_defaults_to_not_evaluated(memory_db: sqlite3.Connection) -> None:
    """The safe default. 'sufficient' must be reached deliberately, never by omission."""
    memory_db.execute(
        "INSERT INTO opportunity "
        "(id, kind, title, employer, source_url, fetched_at, dedupe_key, "
        " employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('o1','job','Engineer','Acme','https://x/1',?,'k','acme','engineer',?,?)",
        (utcnow(), utcnow(), utcnow()),
    )
    memory_db.execute(
        "INSERT INTO assessment "
        "(opportunity_id, config_version, formula, weights, inputs, computed_at) "
        "VALUES ('o1', 'v1', 'sum(w*s)', '{}', '{}', ?)",
        (utcnow(),),
    )
    row = memory_db.execute("SELECT sufficiency FROM assessment").fetchone()
    assert row["sufficiency"] == "not_evaluated"


def test_approval_is_human_only(memory_db: sqlite3.Connection) -> None:
    """There is no value of approved_by that records a model's approval."""
    memory_db.execute(
        "INSERT INTO opportunity "
        "(id, kind, title, employer, source_url, fetched_at, dedupe_key, "
        " employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('o1','job','Engineer','Acme','https://x/1',?,'k','acme','engineer',?,?)",
        (utcnow(), utcnow(), utcnow()),
    )
    memory_db.execute(
        "INSERT INTO application_package "
        "(id, opportunity_id, documents, claim_trace, rendered_sha256, qc_verdict, generated_at) "
        "VALUES ('p1','o1','[]','{}','abc','pass',?)",
        (utcnow(),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO approval (id, package_id, approved_by, approved_at, content_hash, "
            "rendered_hash, destination_hash, destination_snapshot, mailbox_credential_id) "
            "VALUES ('a1','p1','model',?, 'c','r','d','{}','cred1')",
            (utcnow(),),
        )


def test_blocked_package_must_name_a_reason(memory_db: sqlite3.Connection) -> None:
    memory_db.execute(
        "INSERT INTO opportunity "
        "(id, kind, title, employer, source_url, fetched_at, dedupe_key, "
        " employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('o1','job','Engineer','Acme','https://x/1',?,'k','acme','engineer',?,?)",
        (utcnow(), utcnow(), utcnow()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO application_package "
            "(id, opportunity_id, documents, claim_trace, rendered_sha256, qc_verdict, "
            " generated_at) VALUES ('p1','o1','[]','{}','abc','blocked',?)",
            (utcnow(),),
        )


def test_one_approval_per_package(memory_db: sqlite3.Connection) -> None:
    """Two approvals for one package would defeat the single-consumption guard."""
    memory_db.execute(
        "INSERT INTO opportunity "
        "(id, kind, title, employer, source_url, fetched_at, dedupe_key, "
        " employer_norm, title_norm, first_seen_at, last_seen_at) "
        "VALUES ('o1','job','Engineer','Acme','https://x/1',?,'k','acme','engineer',?,?)",
        (utcnow(), utcnow(), utcnow()),
    )
    memory_db.execute(
        "INSERT INTO application_package "
        "(id, opportunity_id, documents, claim_trace, rendered_sha256, qc_verdict, generated_at) "
        "VALUES ('p1','o1','[]','{}','abc','pass',?)",
        (utcnow(),),
    )
    args = (utcnow(),)
    memory_db.execute(
        "INSERT INTO approval (id, package_id, approved_at, content_hash, rendered_hash, "
        "destination_hash, destination_snapshot, mailbox_credential_id) "
        "VALUES ('a1','p1',?, 'c','r','d','{}','cred1')",
        args,
    )
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO approval (id, package_id, approved_at, content_hash, rendered_hash, "
            "destination_hash, destination_snapshot, mailbox_credential_id) "
            "VALUES ('a2','p1',?, 'c','r','d','{}','cred1')",
            args,
        )
