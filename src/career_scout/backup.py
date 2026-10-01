"""Backups, restore, export and purge — T087, T088, T089.

**What a backup holds.** The database (a consistent copy via SQLite's online
backup API, never a raw file copy of a live WAL database), the documents
directory — restricted files still encrypted under the data key — generated
documents, snapshots (the evidence behind every sourced figure, I-22) and the
user's config. **Not** the Google token and **not** the restricted data key:
a stolen backup must not be a way into the mailbox, and destroying the data key
(:func:`purge_restricted_key`) must make restricted copies in every backup
unreadable at once.

**How it is protected.** The archive is encrypted with ``credentials/backup.key``
in 1 MiB AES-256-GCM chunks, each bound to its index and to whether it is the
last, so chunks cannot be reordered, dropped or truncated unnoticed. The user
copies ``backup.key`` off the machine once; without it a backup cannot be
restored, which is the point.

**Rotation.** ``backup_keep_daily`` daily and ``backup_keep_weekly`` weekly
archives are kept; older ones are deleted and their record marked "rotated out".

**A backup never restored is not a backup.** :func:`drill` restores into a
temporary directory, checks the database's integrity and every document's
hash, and records the result on the backup row. ``/health`` shows the last one.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from career_scout.store import paths as paths_module
from career_scout.store import settings as settings_module
from career_scout.store.db import utcnow

MAGIC = b"JVB1"
CHUNK = 1024 * 1024
#: Directories under CAREER_SCOUT_HOME that go into an archive.
INCLUDED = ("documents", "snapshots", "config")


class RestoreRefused(Exception):
    """The restore did not start, and nothing was written."""


# ------------------------------------------------------------------- keys


def _backup_key(paths: paths_module.Paths, *, create: bool) -> bytes:
    path = paths.backup_key
    if path.exists():
        return path.read_bytes()
    if not create:
        raise RestoreRefused(f"no backup key at {path}")
    key = AESGCM.generate_key(bit_length=256)
    path.write_bytes(key)
    paths_module.harden_file(path)
    return key


def _nonce(base: bytes, index: int) -> bytes:
    counter = int.from_bytes(base, "big") ^ index
    return counter.to_bytes(12, "big")


def _encrypt_stream(source: Path, target: Path, key: bytes) -> None:
    aead, base = AESGCM(key), os.urandom(12)
    size = source.stat().st_size
    chunks = max(1, -(-size // CHUNK))
    with source.open("rb") as src, target.open("wb") as out:
        out.write(MAGIC + base + struct.pack(">Q", chunks))
        for index in range(chunks):
            data = src.read(CHUNK)
            aad = struct.pack(">Q?", index, index == chunks - 1)
            sealed = aead.encrypt(_nonce(base, index), data, aad)
            out.write(struct.pack(">I", len(sealed)) + sealed)


def _decrypt_stream(source: Path, target: Path, key: bytes) -> None:
    aead = AESGCM(key)
    with source.open("rb") as src, target.open("wb") as out:
        if src.read(4) != MAGIC:
            raise RestoreRefused("not a Career Scout backup archive")
        base = src.read(12)
        (chunks,) = struct.unpack(">Q", src.read(8))
        try:
            for index in range(chunks):
                (length,) = struct.unpack(">I", src.read(4))
                aad = struct.pack(">Q?", index, index == chunks - 1)
                out.write(aead.decrypt(_nonce(base, index), src.read(length), aad))
        except (InvalidTag, struct.error) as exc:
            raise RestoreRefused(
                "the archive does not decrypt with this key, or it was altered or truncated"
            ) from exc
        if src.read(1):
            raise RestoreRefused("the archive has trailing data; it was altered")


# ------------------------------------------------------------------ take


def _snapshot_db(conn: sqlite3.Connection, target: Path) -> None:
    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()


def _kind_for_daily(conn: sqlite3.Connection) -> str:
    """A daily run becomes the weekly one when no weekly exists in the last 7 days."""
    last = conn.execute("SELECT max(taken_at) t FROM backup_record WHERE kind = 'weekly' "
                        "AND status = 'ok'").fetchone()["t"]
    week_ago = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "weekly" if last is None or last < week_ago else "daily"


def take(conn: sqlite3.Connection, *, kind: str = "manual",
         target: Path | None = None) -> dict[str, Any]:
    """Take one encrypted backup. Always writes a ``backup_record``, ok or failed."""
    paths = paths_module.get_paths()
    if kind == "daily":
        kind = _kind_for_daily(conn)
    configured = settings_module.get(conn, "backup_target")
    folder = Path(target or configured or paths.backups)
    folder.mkdir(parents=True, exist_ok=True)
    backup_id = str(uuid.uuid4())
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    archive = folder / f"career-scout-{kind}-{stamp}-{backup_id[:8]}.jvb"

    try:
        key = _backup_key(paths, create=True)
        with tempfile.TemporaryDirectory() as scratch:
            plain = Path(scratch) / "archive.zip"
            database = Path(scratch) / "career-scout.db"
            _snapshot_db(conn, database)
            with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as zipped:
                zipped.write(database, "career-scout.db")
                for top in INCLUDED:
                    base = paths.root / top
                    if not base.exists():
                        continue
                    for file in base.rglob("*"):
                        if file.is_file():
                            zipped.write(file, file.relative_to(paths.root).as_posix())
                zipped.writestr("manifest.json", json.dumps({
                    "backup_id": backup_id, "kind": kind, "taken_at": utcnow(),
                    "source_root": str(paths.root), "format": "JVB1",
                }))
            _encrypt_stream(plain, archive, key)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        conn.execute(
            "INSERT INTO backup_record (id, kind, taken_at, path, size_bytes, sha256, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok')",
            (backup_id, kind, utcnow(), str(archive), archive.stat().st_size, digest),
        )
    except Exception as exc:
        conn.execute(
            "INSERT INTO backup_record (id, kind, taken_at, path, size_bytes, sha256, status, "
            "error) VALUES (?, ?, ?, ?, 0, '', 'failed', ?)",
            (backup_id, kind, utcnow(), str(archive), f"{type(exc).__name__}: {exc}"),
        )
        raise
    rotated = _rotate(conn)
    return {"backup_id": backup_id, "kind": kind, "path": str(archive),
            "size_bytes": archive.stat().st_size, "rotated_out": rotated,
            "key_file": str(paths.backup_key)}


def _rotate(conn: sqlite3.Connection) -> int:
    removed = 0
    for kind, setting in (("daily", "backup_keep_daily"), ("weekly", "backup_keep_weekly")):
        keep = int(settings_module.get(conn, setting))
        rows = conn.execute(
            "SELECT id, path FROM backup_record WHERE kind = ? AND status = 'ok' "
            "AND COALESCE(error, '') != 'rotated out' ORDER BY taken_at DESC, rowid DESC",
            (kind,),
        ).fetchall()
        for row in rows[keep:]:
            Path(row["path"]).unlink(missing_ok=True)
            conn.execute("UPDATE backup_record SET error = 'rotated out' WHERE id = ?",
                         (row["id"],))
            removed += 1
    return removed


# ---------------------------------------------------------------- restore


def _extract(archive: Path, key: bytes, into: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as scratch:
        plain = Path(scratch) / "archive.zip"
        _decrypt_stream(archive, plain, key)
        with zipfile.ZipFile(plain) as zipped:
            for name in zipped.namelist():
                resolved = (into / name).resolve()
                if into.resolve() not in resolved.parents:
                    raise RestoreRefused(f"archive entry {name!r} escapes the data directory")
            zipped.extractall(into)
            return json.loads(zipped.read("manifest.json"))


def _rewrite_paths(conn: sqlite3.Connection, old_root: str, new_root: Path) -> None:
    """Documents store absolute paths; point them at the new home."""
    for row in conn.execute("SELECT id, path FROM document WHERE path != ''").fetchall():
        if row["path"].startswith(old_root):
            relative = Path(row["path"]).relative_to(old_root)
            conn.execute("UPDATE document SET path = ? WHERE id = ?",
                         (str(new_root / relative), row["id"]))


def restore(archive: Path, *, key_file: Path) -> dict[str, Any]:
    """Restore into the current, **empty** data directory (I-29)."""
    paths = paths_module.get_paths()
    if paths.db.exists():
        raise RestoreRefused(f"{paths.root} is not empty: it already has a database. "
                             f"Restore into a new, empty CAREER_SCOUT_HOME")
    key = Path(key_file).read_bytes()
    manifest = _extract(Path(archive), key, paths.root)

    from career_scout.store.db import open_database

    conn = open_database(paths)                  # migrates an older backup forward
    try:
        _rewrite_paths(conn, manifest["source_root"], paths.root)
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    shutil.copy(key_file, paths.backup_key)
    paths_module.harden_file(paths.backup_key)
    return {"ok": check == "ok", "integrity": check, "manifest": manifest,
            "note": "The Google connection and the restricted-document key are not in "
                    "backups. Reconnect with `career-scout setup google`; restricted documents "
                    "open only if you also kept a copy of data.key."}


def drill(conn: sqlite3.Connection, backup_id: str) -> dict[str, Any]:
    """Restore a backup into a scratch directory and check it. Records the result."""
    row = conn.execute("SELECT * FROM backup_record WHERE id = ?", (backup_id,)).fetchone()
    if row is None:
        raise KeyError(f"no backup {backup_id}")
    paths = paths_module.get_paths()
    problems: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            manifest = _extract(Path(row["path"]), _backup_key(paths, create=False), root)
            restored = sqlite3.connect(root / "career-scout.db")
            restored.row_factory = sqlite3.Row
            try:
                check = restored.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    problems.append(f"integrity: {check}")
                for doc in restored.execute(
                        "SELECT path, sha256, restricted FROM document WHERE path != ''"):
                    relative = Path(doc["path"]).relative_to(manifest["source_root"])
                    copy = root / relative
                    if not copy.exists():
                        problems.append(f"missing {relative}")
                    elif not doc["restricted"] and \
                            hashlib.sha256(copy.read_bytes()).hexdigest() != doc["sha256"]:
                        problems.append(f"hash differs for {relative}")
            finally:
                restored.close()
    except (RestoreRefused, OSError, zipfile.BadZipFile, ValueError) as exc:
        problems.append(str(exc))
    result = "ok" if not problems else "; ".join(problems[:5])
    conn.execute("UPDATE backup_record SET restored_at = ?, restore_result = ? WHERE id = ?",
                 (utcnow(), result, backup_id))
    return {"ok": not problems, "result": result}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """The newest backup, its last drill, and an alert if either is not right."""
    newest = conn.execute(
        "SELECT * FROM backup_record ORDER BY taken_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    drilled = conn.execute(
        "SELECT restored_at, restore_result FROM backup_record WHERE restored_at IS NOT NULL "
        "ORDER BY restored_at DESC LIMIT 1"
    ).fetchone()
    alert = None
    if newest is None:
        alert = "no backup has ever been taken"
    elif newest["status"] == "failed":
        alert = f"the newest backup failed: {newest['error']}"
    else:
        hours = float(settings_module.get(conn, "backup_stale_after_hours"))
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if newest["taken_at"] < cutoff:
            alert = f"the newest backup is stale (taken {newest['taken_at']})"
    return {
        "newest": dict(newest) if newest else None,
        "last_drill": dict(drilled) if drilled else None,
        "alert": alert,
        "key_file": str(paths_module.get_paths().backup_key),
    }


# ------------------------------------------------------------ export, purge


def export(conn: sqlite3.Connection, target: Path) -> Path:
    """Everything, in open formats, readable without Career Scout (T089).

    JSON for every table, the source and generated documents as they are.
    Restricted documents are left out: exporting them decrypted would put a
    passport in plaintext in a folder, which is the one thing this whole
    module exists to prevent. They are listed by name in the README.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608
        if table == "profile_record":
            from career_scout.profile.schema import is_restricted

            rows = [r | ({"value": "[restricted]"} if is_restricted(r["field_path"]) else {})
                    for r in rows]
        name = "profile_records" if table == "profile_record" else table
        (target / f"{name}.json").write_text(json.dumps(rows, indent=2, default=str),
                                             encoding="utf-8")
    paths = paths_module.get_paths()
    for sub in ("source", "generated"):
        source = paths.documents / sub
        if source.exists():
            shutil.copytree(source, target / "documents" / sub, dirs_exist_ok=True)
    restricted = [r["filename"] for r in conn.execute(
        "SELECT filename FROM document WHERE restricted = 1 AND path != ''")]
    readme = io.StringIO()
    readme.write("Career Scout export — every table as JSON, and your documents.\n\n")
    readme.write("Not included, deliberately: restricted documents (they stay encrypted in "
                 "Career Scout; open them there): " + (", ".join(restricted) or "none") + ".\n")
    (target / "README.txt").write_text(readme.getvalue(), encoding="utf-8")
    return target


def purge_restricted_key(conn: sqlite3.Connection, *, confirm: bool) -> dict[str, Any]:
    """Destroy the restricted-document key. Irreversible, by design (T089).

    Every restricted file — here and in every backup ever taken — becomes
    unreadable at once, because none of them ever held the key. The document
    rows remain, marked, so the record of what was uploaded survives.
    """
    if not confirm:
        raise PermissionError("purge destroys the key irreversibly; pass confirm=True")
    path = paths_module.get_paths().data_key
    existed = path.exists()
    if existed:
        length = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(os.urandom(length))
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
    conn.execute("UPDATE document SET extraction_note = 'key purged; contents unrecoverable' "
                 "WHERE restricted = 1")
    return {"destroyed": existed, "purged_at": utcnow()}
