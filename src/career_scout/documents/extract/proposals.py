"""AI-assisted proposals, validated before storage — T022.

The model is untrusted, whichever model it is. It reads a document through the
MCP host (the text arrives wrapped as untrusted content) and may *propose* one
field at a time, together with the exact passage it read the value from. This
module then decides, deterministically:

1. the document exists, is not restricted, and has a text layer;
2. the field path is one the profile schema knows, and is not restricted;
3. the quoted passage appears **verbatim** in the document's own text — so the
   locator is real, found here rather than asserted by the model;
4. the proposed value appears inside that passage (compared case- and
   space-insensitively) — so the model cannot read "2019" and propose "2016".

A proposal that passes is stored exactly like any other extraction: with the
document id and a locator, **unconfirmed**. It reaches no outbound document
until the user confirms it (I-12). One that fails is refused with the reason
and nothing is written.

The optional-API-key path (a scheduled run with no host attached) is not built:
``capability_status`` reports tailored extraction as host-only, which is the
honest state rather than an error.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from career_scout.documents.extract import text as text_extraction
from career_scout.profile import records
from career_scout.profile.schema import COMPLETENESS, SUFFICIENCY_RULES, is_restricted, normalise

#: Paths a proposal may target beyond the completeness and sufficiency tables.
_EXTRA_PATHS = frozenset({
    "basics.label", "basics.location.region", "work[].location", "work[].highlights",
    "education[].startDate", "education[].score", "skills[].keywords",
    "languages[].fluency", "certificates[].issuer", "certificates[].date",
})


class ProposalRefused(Exception):
    """The proposal was not stored, and why."""


def _known_paths() -> frozenset[str]:
    paths = {normalise(f.path) for f in COMPLETENESS}
    for rule in SUFFICIENCY_RULES:
        paths.update(normalise(p) for p in rule.required_field_paths)
    return frozenset(paths | {normalise(p) for p in _EXTRA_PATHS})


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def document_text(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    """The extracted text of one non-restricted document, for a model to read."""
    row = conn.execute("SELECT path, restricted, extracted_ok, filename FROM document "
                       "WHERE id = ?", (document_id,)).fetchone()
    if row is None or not row["path"]:
        raise KeyError(f"no document {document_id}")
    if row["restricted"]:
        raise ProposalRefused("restricted documents are never read (I-13)")
    extracted = text_extraction.extract(Path(row["path"]))
    if not extracted.ok:
        raise ProposalRefused(f"this document has no readable text: {extracted.note}")
    return {"document_id": document_id, "filename": row["filename"], "text": extracted.text,
            "pages": len(extracted.pages)}


def propose(
    conn: sqlite3.Connection,
    document_id: str,
    field_path: str,
    value: str,
    quote: str,
) -> dict[str, Any]:
    """Validate one proposed field against the document and store it unconfirmed."""
    if is_restricted(field_path):
        raise ProposalRefused(f"{field_path} is restricted and is never read into the profile")
    if normalise(field_path) not in _known_paths():
        raise ProposalRefused(f"{field_path!r} is not a profile field this schema knows")
    if not value or not value.strip() or not quote or not quote.strip():
        raise ProposalRefused("a proposal needs a value and the passage it was read from")

    text = document_text(conn, document_id)["text"]
    squashed_text, squashed_quote = _squash(text), _squash(quote)
    offset = squashed_text.find(squashed_quote)
    if offset < 0:
        raise ProposalRefused("the quoted passage does not appear in the document as written")
    if _squash(value) not in squashed_quote:
        raise ProposalRefused("the proposed value does not appear in the quoted passage")

    # The locator points into the document's own text, found here — the model's
    # claim about where it read something is not taken on trust.
    start = _raw_offset(text, squashed_quote)
    extracted = text_extraction.extract(
        Path(conn.execute("SELECT path FROM document WHERE id = ?", (document_id,))
             .fetchone()["path"]))
    page = next((p.number for p in extracted.pages if p.start <= start < p.end), 1)
    record_id = records.add(
        conn, field_path, value.strip(), document_id=document_id,
        locator={"page": page, "start": start, "end": start + len(quote),
                 "snippet": quote.strip()[:200], "proposed_by": "mcp_host"},
        confirmed=False,
    )
    return {"record_id": record_id, "field_path": field_path, "value": value.strip(),
            "confirmed": False, "page": page,
            "note": "stored as a proposal; confirm it before it can appear in an application"}


def _raw_offset(text: str, squashed_quote: str) -> int:
    """Map a match in the whitespace-collapsed text back to an offset in the original."""
    first_word = squashed_quote.split(" ")[0]
    for match in re.finditer(re.escape(first_word), text, re.IGNORECASE):
        if _squash(text[match.start():]).startswith(squashed_quote):
            return match.start()
    return 0
