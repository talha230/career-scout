"""I-13 — restricted document contents never enter a prompt or an output. T013, T014.

"Prompt" here is every surface a model or a person reads: MCP tool results, the
web API, generated documents, and the database itself. "Output" includes the
disk: a restricted file is stored encrypted, so its contents are not sitting in
plaintext under JARVIS_HOME for anything to read.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jarvis import service
from jarvis.documents import store, vault
from jarvis.store.paths import get_paths

pytestmark = pytest.mark.invariant

MARKER = "ZX-PASSPORT-7731-SECRET"


def _upload(db: sqlite3.Connection, tmp_path: Path) -> str:
    source = tmp_path / "passport.txt"
    source.write_bytes(f"PASSPORT\n{MARKER}\nHOLDER NAME\n".encode())
    return store.ingest(db, source, kind="passport").document_id


def test_restricted_never_in_prompt_or_output(db: sqlite3.Connection, tmp_path: Path) -> None:
    _upload(db, tmp_path)
    (tmp_path / "passport.txt").unlink()     # the original is gone; only Jarvis's copy remains

    # 1. Not on disk in plaintext, anywhere under the data directory.
    for path in get_paths().root.rglob("*"):
        if path.is_file():
            assert MARKER.encode() not in path.read_bytes(), f"plaintext in {path}"

    # 2. Not in any table.
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in db.execute(f"SELECT * FROM {table}"):  # noqa: S608 - table names from sqlite
            assert MARKER not in json.dumps([str(v) for v in tuple(row)]), table

    # 3. Not in anything the web app or a model reads.
    surfaces = [
        service.list_documents(db), service.list_profile_records(db),
        service.profile_status(db), service.list_opportunities(db),
    ]
    assert MARKER not in json.dumps(surfaces, default=str)

    from jarvis.mcp import server as mcp_server

    assert MARKER not in json.dumps(mcp_server.list_documents(), default=str)
    assert MARKER not in json.dumps(mcp_server.list_profile_records(), default=str)


def test_the_owner_can_still_open_their_own_file(db: sqlite3.Connection, tmp_path: Path) -> None:
    """The process *can* decrypt; the property is that there is one place it does."""
    document_id = _upload(db, tmp_path)
    data = service.open_restricted_document(db, document_id, from_loopback=True)
    assert MARKER.encode() in data
    audit = db.execute("SELECT * FROM restricted_access").fetchall()
    assert len(audit) == 1 and audit[0]["document_id"] == document_id


def test_a_restricted_file_is_not_opened_over_the_network(db, tmp_path) -> None:
    document_id = _upload(db, tmp_path)
    with pytest.raises(PermissionError):
        service.open_restricted_document(db, document_id, from_loopback=False)
    assert db.execute("SELECT count(*) c FROM restricted_access").fetchone()["c"] == 0


def test_the_hash_is_of_the_original_bytes(db, tmp_path) -> None:
    import hashlib

    document_id = _upload(db, tmp_path)
    stored = db.execute("SELECT sha256 FROM document WHERE id = ?", (document_id,)).fetchone()
    original = f"PASSPORT\n{MARKER}\nHOLDER NAME\n".encode()
    assert stored["sha256"] == hashlib.sha256(original).hexdigest()


def test_destroying_the_key_makes_every_copy_unreadable(db, tmp_path) -> None:
    """The property `jarvis purge` relies on: no key, no plaintext — backups included."""
    document_id = _upload(db, tmp_path)
    path = Path(db.execute("SELECT path FROM document WHERE id = ?", (document_id,))
                .fetchone()["path"])
    copy = tmp_path / "backup-copy.bin"
    copy.write_bytes(path.read_bytes())

    get_paths().data_key.unlink()
    with pytest.raises(vault.KeyMissing):
        service.open_restricted_document(db, document_id, from_loopback=True)
    with pytest.raises(vault.KeyMissing):
        vault.decrypt(copy.read_bytes(), associated=document_id)


def test_a_tampered_file_is_refused_not_returned(db, tmp_path) -> None:
    document_id = _upload(db, tmp_path)
    path = Path(db.execute("SELECT path FROM document WHERE id = ?", (document_id,))
                .fetchone()["path"])
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(vault.Tampered):
        service.open_restricted_document(db, document_id, from_loopback=True)
