"""Envelope encryption for restricted documents — T014.

**Stated plainly: this process can decrypt these files.** The master key sits in
``credentials/data.key`` on the same machine, readable by the same user. The
protection is not that decryption is impossible; it is that

1. the files are never on disk in plaintext, so a backup copied to a USB stick,
   a synced folder or a cloud drive carries ciphertext;
2. there is exactly **one** function that decrypts, :func:`open_restricted`,
   and it records every call in ``restricted_access``;
3. destroying the key (``jarvis purge``) makes every copy unreadable at once,
   including backups taken before the purge.

Format, per file: ``JVR1`` · 12-byte nonce · 2-byte length · wrapped file key ·
12-byte nonce · AES-256-GCM ciphertext. Each file has its own random key,
wrapped under the master key; the document id is bound as associated data, so a
file cannot be swapped under another document's row.
"""

from __future__ import annotations

import os
import sqlite3
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jarvis.store.db import utcnow
from jarvis.store.paths import Paths, get_paths, harden_file

MAGIC = b"JVR1"


class KeyMissing(Exception):
    """The data key is gone — purged, or never created on this machine."""


class Tampered(Exception):
    """The ciphertext does not authenticate. Refused rather than returned."""


def _master_key(paths: Paths, *, create: bool) -> bytes:
    path = paths.data_key
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise KeyMissing(f"{path} is not a 256-bit key")
        return key
    if not create:
        raise KeyMissing(
            "the restricted-document key is not on this machine (purged, or from another "
            "install); these files cannot be read"
        )
    key = AESGCM.generate_key(bit_length=256)
    path.write_bytes(key)
    harden_file(path)
    return key


def encrypt(data: bytes, *, associated: str, paths: Paths | None = None) -> bytes:
    paths = paths or get_paths()
    master = _master_key(paths, create=True)
    file_key = AESGCM.generate_key(bit_length=256)
    aad = associated.encode()
    wrap_nonce, body_nonce = os.urandom(12), os.urandom(12)
    wrapped = AESGCM(master).encrypt(wrap_nonce, file_key, aad)
    body = AESGCM(file_key).encrypt(body_nonce, data, aad)
    return MAGIC + wrap_nonce + struct.pack(">H", len(wrapped)) + wrapped + body_nonce + body


def decrypt(blob: bytes, *, associated: str, paths: Paths | None = None) -> bytes:
    """Decrypt one blob. Private to this module's single caller in spirit.

    Kept importable so a test can prove a *copy* is unreadable once the key is
    gone. Nothing in the package calls it except :func:`open_restricted`.
    """
    paths = paths or get_paths()
    master = _master_key(paths, create=False)
    if not blob.startswith(MAGIC):
        raise Tampered("not a Jarvis restricted file")
    try:
        offset = len(MAGIC)
        wrap_nonce = blob[offset:offset + 12]
        (length,) = struct.unpack(">H", blob[offset + 12:offset + 14])
        wrapped = blob[offset + 14:offset + 14 + length]
        rest = blob[offset + 14 + length:]
        aad = associated.encode()
        file_key = AESGCM(master).decrypt(wrap_nonce, wrapped, aad)
        return AESGCM(file_key).decrypt(rest[:12], rest[12:], aad)
    except (InvalidTag, struct.error, ValueError) as exc:
        raise Tampered("the file does not authenticate; it was altered or belongs "
                       "to another document") from exc


def store(data: bytes, destination: Path, *, document_id: str) -> None:
    """Write ``data`` encrypted. Plaintext never touches the disk here."""
    destination.write_bytes(encrypt(data, associated=document_id))
    harden_file(destination)


def open_restricted(conn: sqlite3.Connection, document_id: str, *, purpose: str) -> bytes:
    """**The single audited decrypt point.** The user opening their own file.

    Every call is recorded before the plaintext is returned. Nothing in the
    pipeline, the MCP server or document generation calls this.
    """
    row = conn.execute("SELECT path, restricted FROM document WHERE id = ?",
                       (document_id,)).fetchone()
    if row is None or not row["path"]:
        raise KeyError(f"no document {document_id}")
    if not row["restricted"]:
        raise ValueError("not a restricted document")
    conn.execute(
        "INSERT INTO restricted_access (document_id, opened_at, purpose) VALUES (?, ?, ?)",
        (document_id, utcnow(), purpose),
    )
    return decrypt(Path(row["path"]).read_bytes(), associated=document_id)
