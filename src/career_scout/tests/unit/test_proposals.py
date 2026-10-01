"""AI-assisted proposals are validated before storage — T022."""

from __future__ import annotations

import pytest

from career_scout.documents import store
from career_scout.documents.extract import proposals
from career_scout.profile import records

CV = ("Ayesha Khan\nExperience\nProcess Engineer, Interloop Limited, March 2019 to January 2024.\n"
      "Led line balancing across 12 knitting lines.\n")


@pytest.fixture
def document_id(db, tmp_path) -> str:
    path = tmp_path / "cv.txt"
    path.write_text(CV, encoding="utf-8")
    return store.ingest(db, path, kind="cv").document_id


def test_a_quoted_proposal_is_stored_unconfirmed_with_a_real_locator(db, document_id) -> None:
    result = proposals.propose(db, document_id, "work[5].employer", "Interloop Limited",
                               "Process Engineer, Interloop   Limited, March 2019")
    record = next(r for r in records.current(db) if r.id == result["record_id"])
    assert record.confirmed is False and record.document_id == document_id
    assert CV[record.locator["start"]:].startswith("Process Engineer")


@pytest.mark.parametrize("field,value,quote,why", [
    ("work[5].employer", "Interloop Limited", "Process Engineer at Interloop", "does not appear"),
    ("work[5].startDate", "2016", "March 2019 to January 2024", "value does not appear"),
    ("identity.passport_number", "AB1", "Ayesha Khan", "restricted"),
    ("hobbies[0].name", "chess", "Ayesha Khan", "not a profile field"),
])
def test_an_invalid_proposal_is_refused_and_nothing_is_written(
        db, document_id, field, value, quote, why) -> None:
    before = len(records.current(db))
    with pytest.raises(proposals.ProposalRefused, match=why):
        proposals.propose(db, document_id, field, value, quote)
    assert len(records.current(db)) == before


def test_a_restricted_document_is_never_read(db, tmp_path) -> None:
    path = tmp_path / "passport.txt"
    path.write_bytes(b"PASSPORT AB1234567")
    document_id = store.ingest(db, path, kind="passport").document_id
    with pytest.raises(proposals.ProposalRefused, match="never read"):
        proposals.document_text(db, document_id)
