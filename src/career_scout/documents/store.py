"""Taking a document in, and turning it into reviewable proposals — T024, T019.

One entry point, :func:`ingest`, which:

1. classifies the document, routing ``restricted`` kinds to the encrypted store
2. copies it under ``CAREER_SCOUT_HOME`` and records its SHA-256
3. reads its text, or records honestly that it could not
4. segments it, extracts what has shape, and suggests the rest
5. writes every field as an **unconfirmed** ``profile_record``

Restricted documents — passport, national ID, tax returns, bank and salary
records — are hashed and written **encrypted** (:mod:`career_scout.documents.vault`),
and **nothing is extracted from them**. (Until T014 this docstring said
"encrypted" while the code copied them in plaintext.) Their contents never
enter a profile record, a prompt, or a generated document. A person who
uploads a passport so it is somewhere safe has not thereby agreed to it being
read.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from career_scout.documents import vault
from career_scout.documents.extract import fields as field_extraction
from career_scout.documents.extract import prefill, sections
from career_scout.documents.extract import text as text_extraction
from career_scout.profile import records
from career_scout.store.db import utcnow
from career_scout.store.paths import Paths, get_paths

#: Document kinds whose contents are never read. Recorded, encrypted, and left
#: alone.
RESTRICTED_KINDS: frozenset[str] = frozenset(
    {"passport", "national_id", "cnic", "tax_return", "bank_statement", "payslip",
     "salary_certificate", "visa", "birth_certificate"}
)

#: Kinds we extract a profile from.
READABLE_KINDS: frozenset[str] = frozenset(
    {"cv", "resume", "cover_letter", "experience_letter", "reference_letter",
     "degree", "transcript", "certificate", "other"}
)


class DocumentTooLarge(ValueError):
    """Refused before anything is copied."""


#: 50 MB. Generous for a CV, and a guard against a mistyped path pointing at a
#: disk image.
MAX_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Ingested:
    document_id: str
    kind: str
    restricted: bool
    extracted_ok: bool
    note: str | None
    record_ids: tuple[str, ...]
    candidates: tuple[prefill.Candidate, ...]
    page_count: int

    @property
    def needs_attention(self) -> bool:
        """True when the user must do something before this document is useful."""
        return not self.restricted and not self.extracted_ok


def ingest(
    conn: sqlite3.Connection,
    source: Path,
    *,
    kind: str = "cv",
    paths: Paths | None = None,
) -> Ingested:
    """Take a document in and turn it into reviewable proposals."""
    paths = paths or get_paths()
    source = Path(source).expanduser().resolve()

    if not source.is_file():
        raise FileNotFoundError(f"no file at {source}")
    size = source.stat().st_size
    if size > MAX_BYTES:
        raise DocumentTooLarge(
            f"{source.name} is {size / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_BYTES // 1024 // 1024} MB"
        )

    restricted = kind in RESTRICTED_KINDS
    document_id = str(uuid.uuid4())

    if restricted:
        # Read once, hash the original bytes, and write only ciphertext. The
        # plaintext never lands under CAREER_SCOUT_HOME (T014).
        data = source.read_bytes()
        digest, actual_size = hashlib.sha256(data).hexdigest(), len(data)
        destination = paths.restricted_documents / f"{document_id}.jvr"
        vault.store(data, destination, document_id=document_id)
        del data
        _record_document(
            conn, document_id, kind, source.name, destination, digest, actual_size,
            restricted=True, extracted_ok=False,
            note="restricted: stored and hashed, contents deliberately not read",
            page_count=None,
        )
        return Ingested(
            document_id=document_id, kind=kind, restricted=True, extracted_ok=False,
            note="stored securely; its contents are never read", record_ids=(),
            candidates=(), page_count=0,
        )

    destination = paths.source_documents / f"{document_id}{source.suffix.lower()}"
    shutil.copy2(source, destination)
    digest, actual_size = text_extraction.sha256_of(destination)

    try:
        document = text_extraction.extract(destination)
    except text_extraction.UnsupportedDocument as exc:
        _record_document(
            conn, document_id, kind, source.name, destination, digest, actual_size,
            restricted=False, extracted_ok=False, note=str(exc), page_count=None,
        )
        return Ingested(
            document_id=document_id, kind=kind, restricted=False, extracted_ok=False,
            note=str(exc), record_ids=(), candidates=(), page_count=0,
        )

    _record_document(
        conn, document_id, kind, source.name, destination, digest, actual_size,
        restricted=False, extracted_ok=document.ok, note=document.note,
        page_count=document.page_count,
    )

    if not document.ok:
        # A scan is not a failure to be swallowed. It is reported, so the user
        # can supply a text version or turn on OCR.
        return Ingested(
            document_id=document_id, kind=kind, restricted=False, extracted_ok=False,
            note=document.note, record_ids=(), candidates=(),
            page_count=document.page_count,
        )

    parts = sections.split(document)
    extracted = field_extraction.extract_all(document, parts)
    candidates = prefill.suggest_all(document, parts)

    record_ids = [
        records.add(
            conn,
            item.field_path,
            item.value,
            document_id=document_id,
            locator=item.locator.as_dict(),
            confirmed=False,  # a proposal, until the user looks at it
        )
        for item in extracted
    ]

    return Ingested(
        document_id=document_id,
        kind=kind,
        restricted=False,
        extracted_ok=True,
        note=None,
        record_ids=tuple(record_ids),
        candidates=tuple(candidates),
        page_count=document.page_count,
    )


def _record_document(
    conn: sqlite3.Connection,
    document_id: str,
    kind: str,
    filename: str,
    path: Path,
    digest: str,
    size: int,
    *,
    restricted: bool,
    extracted_ok: bool,
    note: str | None,
    page_count: int | None,
) -> None:
    conn.execute(
        "INSERT INTO document (id, kind, filename, path, sha256, bytes, restricted, "
        "extracted_ok, extraction_note, page_count, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (document_id, kind, filename, str(path), digest, size, int(restricted),
         int(extracted_ok), note, page_count, utcnow()),
    )


def delete(conn: sqlite3.Connection, document_id: str) -> None:
    """Remove a document and everything read from it.

    Profile records extracted from it are withdrawn rather than deleted, so the
    history of what was once claimed — and on what basis — survives the
    document going away.
    """
    row = conn.execute("SELECT path, restricted FROM document WHERE id = ?",
                       (document_id,)).fetchone()
    if row is None:
        raise KeyError(f"no document {document_id}")

    for record in conn.execute(
        "SELECT id FROM profile_record WHERE document_id = ? AND superseded_by IS NULL",
        (document_id,),
    ).fetchall():
        records.withdraw(conn, record["id"])

    path = Path(row["path"])
    if path.exists():
        if row["restricted"]:
            _overwrite(path)
        path.unlink()

    conn.execute("UPDATE document SET path = '', extraction_note = ? WHERE id = ?",
                 ("deleted by the user", document_id))


def _overwrite(path: Path) -> None:
    """Best-effort overwrite before unlinking a restricted file.

    On an SSD this does not guarantee the blocks are gone — wear levelling
    means only destroying the data key does that, which is what ``career-scout purge``
    is for. It is still worth doing, and saying plainly what it does and does
    not achieve is better than implying more.
    """
    try:
        length = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(os.urandom(length))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def listing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Metadata only. A restricted document's contents are never returned."""
    return conn.execute(
        "SELECT id, kind, filename, sha256, bytes, restricted, extracted_ok, "
        "       extraction_note, page_count, uploaded_at "
        "FROM document WHERE path != '' ORDER BY uploaded_at DESC"
    ).fetchall()
