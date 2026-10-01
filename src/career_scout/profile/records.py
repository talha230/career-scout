"""Reading and writing profile records — T021, T024, T025.

Two rules run through everything here.

**Nothing is overwritten.** A correction writes a new row and points the old
one at it through ``superseded_by``. The original stays readable, so "where did
this claim come from" always has an answer, including after the user changed
their mind.

**Typed and document-read values stay distinguishable.** A record carries
``document_id`` and ``locator`` when it was read out of a document, and both
are NULL when the user typed it. :func:`add` refuses to accept a locator
without a document, so "extracted" cannot be asserted about something nobody
extracted. The UI shows the two differently, and generated documents can be
restricted to confirmed, document-backed claims.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from career_scout.profile.schema import is_restricted, normalise
from career_scout.store.db import utcnow


class ProvenanceError(ValueError):
    """An attempt to record a value as extracted when it was not."""


@dataclass(frozen=True, slots=True)
class Record:
    id: str
    field_path: str
    value: str | None
    value_type: str
    document_id: str | None
    locator: dict[str, Any] | None
    confirmed: bool
    confirmed_at: str | None
    superseded_by: str | None
    created_at: str

    @property
    def from_document(self) -> bool:
        """True when a document was read for this value, rather than typed."""
        return self.document_id is not None

    @property
    def restricted(self) -> bool:
        return is_restricted(self.field_path)


def _row_to_record(row: sqlite3.Row) -> Record:
    return Record(
        id=row["id"],
        field_path=row["field_path"],
        value=row["value"],
        value_type=row["value_type"],
        document_id=row["document_id"],
        locator=json.loads(row["locator"]) if row["locator"] else None,
        confirmed=bool(row["confirmed"]),
        confirmed_at=row["confirmed_at"],
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
    )


def add(
    conn: sqlite3.Connection,
    field_path: str,
    value: Any,
    *,
    document_id: str | None = None,
    locator: dict[str, Any] | None = None,
    confirmed: bool = False,
    value_type: str = "string",
) -> str:
    """Record a value. Returns the new record id.

    ``confirmed=False`` is the default because an extraction the user has not
    looked at is a proposal, not a fact, and must not reach an outbound
    document.
    """
    if locator is not None and document_id is None:
        raise ProvenanceError(
            f"{field_path}: a locator points into a document, so document_id is required. "
            f"A typed value has neither."
        )

    record_id = str(uuid.uuid4())
    now = utcnow()
    conn.execute(
        "INSERT INTO profile_record (id, field_path, value, value_type, document_id, "
        "locator, confirmed, confirmed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            field_path,
            None if value is None else str(value),
            value_type,
            document_id,
            json.dumps(locator) if locator else None,
            int(confirmed),
            now if confirmed else None,
            now,
        ),
    )
    return record_id


def supersede(
    conn: sqlite3.Connection,
    record_id: str,
    value: Any,
    *,
    document_id: str | None = None,
    locator: dict[str, Any] | None = None,
    confirmed: bool = True,
) -> str:
    """Correct a record by writing a replacement and linking the original to it.

    A correction is confirmed by default: the user just looked at it and said
    what it should be.
    """
    row = conn.execute(
        "SELECT field_path, value_type, superseded_by FROM profile_record WHERE id = ?",
        (record_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no profile record {record_id}")
    if row["superseded_by"]:
        raise ValueError(
            f"record {record_id} was already superseded by {row['superseded_by']}; "
            f"correct the current record instead"
        )

    new_id = add(
        conn,
        row["field_path"],
        value,
        document_id=document_id,
        locator=locator,
        confirmed=confirmed,
        value_type=row["value_type"],
    )
    conn.execute("UPDATE profile_record SET superseded_by = ? WHERE id = ?", (new_id, record_id))
    return new_id


def confirm(conn: sqlite3.Connection, record_id: str) -> None:
    """Mark an extracted proposal as checked by the user."""
    updated = conn.execute(
        "UPDATE profile_record SET confirmed = 1, confirmed_at = ? "
        "WHERE id = ? AND superseded_by IS NULL",
        (utcnow(), record_id),
    ).rowcount
    if not updated:
        raise KeyError(f"no current profile record {record_id}")


def withdraw(conn: sqlite3.Connection, record_id: str) -> None:
    """Unconfirm a record without deleting it.

    Used when a claim turns out to be wrong. Anything queued that depended on
    it returns to insufficient, with this named as the cause.
    """
    conn.execute(
        "UPDATE profile_record SET confirmed = 0, confirmed_at = NULL WHERE id = ?",
        (record_id,),
    )


def current(conn: sqlite3.Connection, *, confirmed_only: bool = False) -> list[Record]:
    """Every record that has not been superseded."""
    sql = "SELECT * FROM profile_record WHERE superseded_by IS NULL"
    if confirmed_only:
        sql += " AND confirmed = 1"
    return [_row_to_record(r) for r in conn.execute(sql + " ORDER BY field_path, created_at")]


def confirmed_paths(conn: sqlite3.Connection) -> set[str]:
    """Normalised paths with a confirmed, non-empty, current value.

    The left-hand side of every sufficiency comparison. Normalised so that
    ``work[3].position`` satisfies a rule written as ``work[].position``.
    """
    return {
        normalise(r["field_path"])
        for r in conn.execute(
            "SELECT field_path FROM profile_record "
            "WHERE superseded_by IS NULL AND confirmed = 1 "
            "AND value IS NOT NULL AND TRIM(value) != ''"
        )
    }


def get(conn: sqlite3.Connection, field_path: str) -> Record | None:
    """The current record for one exact path."""
    row = conn.execute(
        "SELECT * FROM profile_record WHERE field_path = ? AND superseded_by IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (field_path,),
    ).fetchone()
    return _row_to_record(row) if row else None


def history(conn: sqlite3.Connection, field_path: str) -> list[Record]:
    """Every version of one path, oldest first. Corrections are never lost."""
    return [
        _row_to_record(r)
        for r in conn.execute(
            "SELECT * FROM profile_record WHERE field_path = ? ORDER BY created_at",
            (field_path,),
        )
    ]


def conflicts(conn: sqlite3.Connection) -> list[tuple[str, list[Record]]]:
    """Paths where two current records disagree — T015.

    Two documents can state different things about the same fact. Neither is
    overwritten; both persist and the user is shown the disagreement with the
    document each side came from.
    """
    grouped: dict[str, list[Record]] = {}
    for record in current(conn):
        grouped.setdefault(record.field_path, []).append(record)

    out: list[tuple[str, list[Record]]] = []
    for path, records in sorted(grouped.items()):
        values = {r.value for r in records if r.value is not None}
        if len(values) > 1:
            out.append((path, records))
    return out


def next_index(conn: sqlite3.Connection, pattern: str) -> int:
    """The next free index for a repeated group such as ``work[]``.

    Lets an importer append a job without first reading the whole profile.
    """
    prefix = pattern.split("[]")[0]
    highest = -1
    for row in conn.execute(
        "SELECT DISTINCT field_path FROM profile_record WHERE field_path LIKE ?",
        (f"{prefix}[%",),
    ):
        import re

        found = re.search(rf"^{re.escape(prefix)}\[(\d+)\]", row["field_path"])
        if found:
            highest = max(highest, int(found.group(1)))
    return highest + 1
