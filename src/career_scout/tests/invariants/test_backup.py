"""I-29 — a backup restores into an empty data directory. T087, T088, T089.

"A backup that has never been restored is not a backup." These tests restore
for real: take a backup in one ``CAREER_SCOUT_HOME``, move to a brand-new empty one,
restore, and check the data came back — the database, the documents, the
configuration — and that what should *not* come back (tokens, keys) did not.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from career_scout import backup
from career_scout.documents import store
from career_scout.profile import records
from career_scout.store import paths as paths_module
from career_scout.store import settings as settings_module
from career_scout.store.db import open_database

pytestmark = pytest.mark.invariant


def _populate(db: sqlite3.Connection, tmp_path: Path) -> None:
    records.add(db, "basics.name", "Ayesha Khan", confirmed=True)
    records.add(db, "work[0].employer", "Interloop Limited", confirmed=True)
    cv = tmp_path / "cv.txt"
    cv.write_text("Ayesha Khan\nProcess Engineer at Interloop Limited 2019-2024\n",
                  encoding="utf-8")
    store.ingest(db, cv, kind="cv")
    passport = tmp_path / "passport.txt"
    passport.write_bytes(b"PASSPORT AB1234567")
    store.ingest(db, passport, kind="passport")
    settings_module.set_value(db, "staleness_window_days", 30)
    paths_module.get_paths().google_token.write_text('{"token": "secret"}', encoding="utf-8")


def _move_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> paths_module.Paths:
    monkeypatch.setenv(paths_module.ENV_HOME, str(home))
    paths_module.reset_cache()
    return paths_module.get_paths()


def test_backup_restores(db, tmp_path, monkeypatch) -> None:
    """I-29 — into an empty data directory, with nothing else carried across."""
    _populate(db, tmp_path)
    taken = backup.take(db, kind="manual", target=tmp_path / "external")
    archive = Path(taken["path"])
    key_copy = tmp_path / "backup.key"
    shutil.copy(paths_module.get_paths().backup_key, key_copy)
    before = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
              for t in ("profile_record", "document", "setting")}
    db.close()

    fresh = _move_home(monkeypatch, tmp_path / "new-machine")
    assert not fresh.db.exists()
    result = backup.restore(archive, key_file=key_copy)
    assert result["ok"], result

    conn = open_database(fresh)
    try:
        after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
                 for t in before}
        assert after == before
        assert records.get(conn, "basics.name").value == "Ayesha Khan"
        assert settings_module.get(conn, "staleness_window_days") == 30
        for row in conn.execute("SELECT path, restricted FROM document"):
            assert Path(row["path"]).exists(), row["path"]
            assert str(fresh.root) in row["path"], "paths are rewritten to the new home"
    finally:
        conn.close()

    # Not carried: the Google token and the restricted-document key.
    assert not fresh.google_token.exists()
    assert not fresh.data_key.exists()


def test_the_archive_is_encrypted(db, tmp_path) -> None:
    _populate(db, tmp_path)
    archive = Path(backup.take(db, kind="manual", target=tmp_path / "ext")["path"])
    data = archive.read_bytes()
    assert b"Ayesha Khan" not in data
    assert b"SQLite format 3" not in data
    assert not data.startswith(b"PK")          # not a readable zip


def test_restore_refuses_a_non_empty_home(db, tmp_path) -> None:
    _populate(db, tmp_path)
    archive = Path(backup.take(db, kind="manual", target=tmp_path / "ext")["path"])
    with pytest.raises(backup.RestoreRefused, match="not empty"):
        backup.restore(archive, key_file=paths_module.get_paths().backup_key)


def test_restore_with_the_wrong_key_is_refused(db, tmp_path, monkeypatch) -> None:
    _populate(db, tmp_path)
    archive = Path(backup.take(db, kind="manual", target=tmp_path / "ext")["path"])
    wrong = tmp_path / "wrong.key"
    wrong.write_bytes(b"\x01" * 32)
    db.close()
    _move_home(monkeypatch, tmp_path / "elsewhere")
    with pytest.raises(backup.RestoreRefused, match="key"):
        backup.restore(archive, key_file=wrong)


def test_a_backup_record_is_written(db, tmp_path) -> None:
    taken = backup.take(db, kind="manual", target=tmp_path / "ext")
    row = db.execute("SELECT * FROM backup_record").fetchone()
    assert row["status"] == "ok" and row["path"] == taken["path"]
    assert row["size_bytes"] == Path(taken["path"]).stat().st_size


def test_rotation_keeps_the_configured_number(db, tmp_path) -> None:
    settings_module.set_value(db, "backup_keep_daily", 2)
    for _ in range(4):
        backup.take(db, kind="daily", target=tmp_path / "ext")
    live = [r for r in db.execute("SELECT * FROM backup_record WHERE kind = 'daily'")
            if Path(r["path"]).exists()]
    assert len(live) == 2


def test_a_restore_drill_is_recorded(db, tmp_path) -> None:
    _populate(db, tmp_path)
    taken = backup.take(db, kind="manual", target=tmp_path / "ext")
    result = backup.drill(db, taken["backup_id"])
    assert result["ok"], result
    row = db.execute("SELECT restored_at, restore_result FROM backup_record").fetchone()
    assert row["restored_at"] and "ok" in row["restore_result"]


def test_a_stale_or_failed_backup_is_an_alert(db, tmp_path) -> None:
    assert backup.status(db)["alert"]              # none at all is an alert
    backup.take(db, kind="manual", target=tmp_path / "ext")
    assert not backup.status(db)["alert"]
    db.execute("UPDATE backup_record SET taken_at = '2020-01-01T00:00:00Z'")
    assert "stale" in backup.status(db)["alert"]


def test_purge_makes_restricted_copies_unreadable(db, tmp_path) -> None:
    """T089 — destroying the key is the erasure; every copy dies with it."""
    from career_scout.documents import vault

    _populate(db, tmp_path)
    document_id = db.execute("SELECT id FROM document WHERE restricted = 1").fetchone()["id"]
    result = backup.purge_restricted_key(db, confirm=True)
    assert result["destroyed"]
    with pytest.raises(vault.KeyMissing):
        vault.open_restricted(db, document_id, purpose="test")


def test_export_writes_open_formats(db, tmp_path) -> None:
    """T089 — everything, readable without Career Scout."""
    import json

    _populate(db, tmp_path)
    out = backup.export(db, tmp_path / "export")
    profile = json.loads((out / "profile_records.json").read_text(encoding="utf-8"))
    assert any(r["value"] == "Ayesha Khan" for r in profile)
    assert (out / "README.txt").exists()
    assert not list(out.rglob("*.jvr")), "restricted files are never exported"
