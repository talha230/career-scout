"""Text extraction, section segmentation, field reading and pre-fill."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jarvis.documents import store
from jarvis.documents.extract import fields, prefill, sections
from jarvis.documents.extract import text as text_extraction
from jarvis.profile import functions as fn
from jarvis.profile import records

CV = """ALEX MORGAN EXAMPLE
Industrial Engineer
alex@example.com | +20 100 123 4567
linkedin.com/in/alexexample

PROFESSIONAL SUMMARY
Industrial engineer with six years in manufacturing operations and CAPEX planning.

WORK EXPERIENCE
Pyraloop Egypt Industries Ltd.
Industrial Engineer, Project Engineering
March 2019 - Present
Led line balancing and CAPEX planning for the Egypt plant build-out.

Acme Manufacturing GmbH
Graduate Engineer
06/2017 - 02/2019
Time and motion studies across three production lines.

EDUCATION
University of Engineering and Technology, Lahore
BSc Industrial and Manufacturing Engineering, 2017

LANGUAGES
English - IELTS 7.5
Urdu - Native

SKILLS
Lean Manufacturing, Six Sigma, Value Stream Mapping
"""


@pytest.fixture
def cv_file(tmp_path: Path) -> Path:
    path = tmp_path / "cv.txt"
    path.write_text(CV, encoding="utf-8")
    return path


@pytest.fixture
def parsed(cv_file: Path) -> tuple[text_extraction.ExtractedDocument, list[sections.Section]]:
    document = text_extraction.extract(cv_file)
    return document, sections.split(document)


def _values(found: list[fields.Extracted]) -> dict[str, str]:
    return {item.field_path: item.value for item in found}


# ------------------------------------------------------------------- text


def test_extraction_reports_pages_and_hash(cv_file: Path) -> None:
    document = text_extraction.extract(cv_file)
    assert document.ok is True
    assert len(document.sha256) == 64
    assert document.bytes == cv_file.stat().st_size


def test_unsupported_type_names_what_to_do(tmp_path: Path) -> None:
    path = tmp_path / "cv.pages"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(text_extraction.UnsupportedDocument, match="PDF or DOCX"):
        text_extraction.extract(path)


def test_empty_file_is_not_ok(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    assert text_extraction.extract(path).ok is False


def test_a_document_with_no_text_layer_is_reported_not_emptied() -> None:
    """T019 — a scan must not present as a CV containing nothing.

    Returning "" here is indistinguishable from a CV the user forgot to fill
    in, and the user would be told their perfectly good certificate was blank.
    """
    ok, note = text_extraction._assess_text_layer("  \n \n", page_count=3)
    assert ok is False
    assert "no usable text layer" in note
    assert "scan" in note


def test_a_real_text_layer_passes() -> None:
    ok, note = text_extraction._assess_text_layer("x" * 500, page_count=2)
    assert ok is True
    assert note is None


def test_normalisation_folds_typographic_variants() -> None:
    assert "fi" in text_extraction.normalise_text("conﬁdential")
    assert " " not in text_extraction.normalise_text("a b")


def test_locators_point_at_real_text(parsed: tuple) -> None:
    document, _ = parsed
    match, locator = document.find(fields.EMAIL)[0]
    assert document.text[locator.start : locator.end] == "alex@example.com"
    assert locator.page == 1
    assert "alex@example.com" in locator.snippet


# --------------------------------------------------------------- sections


def test_sections_are_recognised(parsed: tuple) -> None:
    _, parts = parsed
    assert [s.name for s in parts] == [
        "header", "summary", "experience", "education", "languages", "skills",
    ]


def test_coverage_is_total_for_a_conventional_cv(parsed: tuple) -> None:
    _, parts = parsed
    assert sections.coverage(parts) == 1.0


def test_a_document_with_no_headings_is_one_unknown_section(tmp_path: Path) -> None:
    """Honest low confidence beats a structure that was not found."""
    path = tmp_path / "prose.txt"
    path.write_text("I worked at a factory for some years and enjoyed it.", encoding="utf-8")
    parts = sections.split(text_extraction.extract(path))
    assert [s.name for s in parts] == ["unknown"]
    assert sections.coverage(parts) == 0.0


def test_a_sentence_starting_with_a_heading_word_is_not_a_heading() -> None:
    assert sections.canonical_heading("Experience") == "experience"
    assert sections.canonical_heading("Experience in lean manufacturing since 2017.") is None
    assert sections.canonical_heading("My experience includes line balancing") is None


def test_headings_with_trailing_parentheticals_are_recognised() -> None:
    assert sections.canonical_heading("Work Experience (2017-2026)") == "experience"


def test_german_headings_are_recognised() -> None:
    assert sections.canonical_heading("BERUFSERFAHRUNG") == "experience"
    assert sections.canonical_heading("Ausbildung") == "education"


# ----------------------------------------------------------------- fields


def test_contact_details_are_read(parsed: tuple) -> None:
    document, parts = parsed
    found = _values(fields.extract_contact(document, parts))
    assert found["basics.email"] == "alex@example.com"
    assert found["basics.phone"] == "+20 100 123 4567"
    assert found["basics.name"] == "Alex Morgan Example"


def test_a_referees_email_further_down_is_not_read_as_the_candidates(tmp_path: Path) -> None:
    """Contact reading is bounded to the header, on purpose."""
    path = tmp_path / "cv.txt"
    path.write_text(
        "JANE DOE\njane@example.com\n\nREFERENCES\nProf Smith - smith@uni.de\n",
        encoding="utf-8",
    )
    document = text_extraction.extract(path)
    found = _values(fields.extract_contact(document, sections.split(document)))
    assert found["basics.email"] == "jane@example.com"
    assert "smith@uni.de" not in found.values()


def test_a_year_range_is_not_read_as_a_phone_number() -> None:
    assert fields._looks_like_phone("2017") is False
    assert fields._looks_like_phone("+20 100 123 4567") is True


def test_numeric_dates_are_parsed(parsed: tuple) -> None:
    """``06/2017 - 02/2019`` matched nothing before the numeric-month branch existed."""
    document, parts = parsed
    found = _values(fields.extract_experience(document, parts))
    assert found["work[1].startDate"] == "2017-06"
    assert found["work[1].endDate"] == "2019-02"


def test_named_month_dates_are_parsed(parsed: tuple) -> None:
    document, parts = parsed
    found = _values(fields.extract_experience(document, parts))
    assert found["work[0].startDate"] == "2019-03"
    assert found["work[0].endDate"] == "present"


def test_each_role_gets_its_own_employer(parsed: tuple) -> None:
    """A radius search labels the first role with the second company's name."""
    document, parts = parsed
    found = _values(fields.extract_experience(document, parts))
    assert found["work[0].employer"] == "Pyraloop Egypt Industries Ltd."
    assert found["work[1].employer"] == "Acme Manufacturing GmbH"


def test_precision_is_never_invented() -> None:
    """A CV that says 2019 yields 2019, not 2019-01-01."""
    assert fields._normalise_date("2019") == "2019"
    assert fields._normalise_date("March 2019") == "2019-03"
    assert fields._normalise_date("03/2019") == "2019-03"
    assert fields._normalise_date("no date here") is None


def test_one_qualification_is_one_entry(parsed: tuple) -> None:
    """Institution, degree and year on adjacent lines are one education entry.

    Splitting them across indices makes the scholarship sufficiency rule —
    which needs all three together — unsatisfiable by an ordinary CV.
    """
    document, parts = parsed
    found = _values(fields.extract_education(document, parts))
    assert found["education[0].institution"].startswith("University of Engineering")
    assert found["education[0].studyType"] == "BSc"
    assert found["education[0].endDate"] == "2017"
    assert "education[1].studyType" not in found


def test_two_qualifications_get_two_entries(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text(
        "EDUCATION\nUniversity of Lahore\nBSc Engineering, 2017\n\n"
        "National University of Sciences and Technology\nMSc Management, 2021\n",
        encoding="utf-8",
    )
    document = text_extraction.extract(path)
    found = _values(fields.extract_education(document, sections.split(document)))
    assert found["education[0].studyType"] == "BSc"
    assert found["education[1].studyType"] == "MSc"


def test_language_test_is_read(parsed: tuple) -> None:
    """The field that most often decides whether an application can be made."""
    document, parts = parsed
    found = _values(fields.extract_languages(document, parts))
    assert "IELTS 7.5" in found.values()


# ---------------------------------------------------------------- prefill


def test_titles_are_suggested_not_extracted(parsed: tuple) -> None:
    document, parts = parsed
    suggestions = {c.field_path: c.value for c in prefill.suggest_titles(document, parts)}
    assert "Industrial Engineer, Project Engineering" in suggestions.values()


def test_skills_are_split_into_items(parsed: tuple) -> None:
    document, parts = parsed
    values = {c.value for c in prefill.suggest_skills(document, parts)}
    assert {"Lean Manufacturing", "Six Sigma", "Value Stream Mapping"} <= values


def test_summary_is_suggested_from_the_summary_section(parsed: tuple) -> None:
    document, parts = parsed
    suggested = prefill.suggest_summary(document, parts)
    assert suggested and "manufacturing operations" in suggested[0].value


def test_an_accepted_candidate_keeps_its_document_provenance(
    db: sqlite3.Connection, parsed: tuple
) -> None:
    document, parts = parsed
    db.execute(
        "INSERT INTO document (id, kind, filename, path, sha256, bytes, uploaded_at) "
        "VALUES ('d1', 'cv', 'cv.txt', '/x', 'abc', 1, '2026-01-01T00:00:00Z')"
    )
    candidate = prefill.suggest_titles(document, parts)[0]
    record_id = prefill.record_choice(db, candidate, candidate.value, document_id="d1")

    stored = next(r for r in records.current(db) if r.id == record_id)
    assert stored.from_document is True
    assert stored.locator is not None


def test_an_edited_candidate_is_recorded_as_typed(
    db: sqlite3.Connection, parsed: tuple
) -> None:
    """The rule that makes pre-fill safe rather than corrosive.

    An edited value was not read from the document. Recording it with the
    document's locator would put a claim the document does not support behind
    the whole traceability guarantee.
    """
    document, parts = parsed
    db.execute(
        "INSERT INTO document (id, kind, filename, path, sha256, bytes, uploaded_at) "
        "VALUES ('d1', 'cv', 'cv.txt', '/x', 'abc', 1, '2026-01-01T00:00:00Z')"
    )
    candidate = prefill.suggest_titles(document, parts)[0]
    record_id = prefill.record_choice(db, candidate, "Senior Principal Engineer",
                                      document_id="d1")

    stored = next(r for r in records.current(db) if r.id == record_id)
    assert stored.from_document is False
    assert stored.locator is None
    assert stored.value == "Senior Principal Engineer"


def test_whitespace_only_changes_still_count_as_accepted(
    db: sqlite3.Connection, parsed: tuple
) -> None:
    document, parts = parsed
    db.execute(
        "INSERT INTO document (id, kind, filename, path, sha256, bytes, uploaded_at) "
        "VALUES ('d1', 'cv', 'cv.txt', '/x', 'abc', 1, '2026-01-01T00:00:00Z')"
    )
    candidate = prefill.suggest_titles(document, parts)[0]
    record_id = prefill.record_choice(
        db, candidate, f"  {candidate.value}  ", document_id="d1"
    )
    assert next(r for r in records.current(db) if r.id == record_id).from_document is True


# ------------------------------------------------------------------ ingest


def test_ingest_writes_unconfirmed_proposals(db: sqlite3.Connection, cv_file: Path) -> None:
    result = store.ingest(db, cv_file, kind="cv")
    assert result.extracted_ok is True
    assert result.record_ids

    current = records.current(db)
    assert current
    assert all(r.confirmed is False for r in current)
    assert all(r.from_document for r in current)


def test_ingest_does_not_raise_completeness_on_its_own(
    db: sqlite3.Connection, cv_file: Path
) -> None:
    """Uploading a CV proposes; it does not assert."""
    store.ingest(db, cv_file, kind="cv")
    assert fn.completeness(db).percentage == 0.0
    assert fn.minimum_viable_profile(db).satisfied is False


def test_confirming_proposals_reaches_the_minimum_viable_profile(
    db: sqlite3.Connection, cv_file: Path
) -> None:
    """One upload plus confirmation should clear the checklist bar citizenship."""
    store.ingest(db, cv_file, kind="cv")
    for record in records.current(db):
        records.confirm(db, record.id)
    records.add(db, "identity.citizenship", "PK", confirmed=True)
    records.add(db, "identity.tax_residence", "EG", confirmed=True)
    records.add(db, "work[0].position", "Industrial Engineer", confirmed=True)

    assert fn.minimum_viable_profile(db).satisfied is True


@pytest.mark.invariant
def test_restricted_documents_are_never_read(db: sqlite3.Connection, tmp_path: Path) -> None:
    """I-13 — a passport is stored and hashed, and its contents are not extracted.

    Uploading an identity document so it is somewhere safe is not consent to
    have it parsed into a profile.
    """
    passport = tmp_path / "passport.txt"
    passport.write_text(
        "PASSPORT\nAB1234567\nALEX MORGAN EXAMPLE\nsecret@example.com\n", encoding="utf-8"
    )
    result = store.ingest(db, passport, kind="passport")

    assert result.restricted is True
    assert result.record_ids == ()
    assert records.current(db) == []

    stored = db.execute("SELECT restricted, sha256 FROM document").fetchone()
    assert stored["restricted"] == 1
    assert len(stored["sha256"]) == 64


def test_restricted_documents_go_to_the_encrypted_store(
    db: sqlite3.Connection, tmp_path: Path, paths
) -> None:
    passport = tmp_path / "passport.txt"
    passport.write_text("PASSPORT AB1234567", encoding="utf-8")
    store.ingest(db, passport, kind="passport")

    path = Path(db.execute("SELECT path FROM document").fetchone()["path"])
    assert path.parent == paths.restricted_documents


def test_a_scanned_document_is_recorded_as_un_extracted(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    scan = tmp_path / "degree.txt"
    scan.write_text("", encoding="utf-8")
    result = store.ingest(db, scan, kind="degree")

    assert result.extracted_ok is False
    assert result.needs_attention is True
    row = db.execute("SELECT extracted_ok, extraction_note FROM document").fetchone()
    assert row["extracted_ok"] == 0
    assert row["extraction_note"]


def test_an_oversized_file_is_refused_before_copying(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "MAX_BYTES", 10)
    big = tmp_path / "big.txt"
    big.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(store.DocumentTooLarge):
        store.ingest(db, big, kind="cv")
    assert db.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0


def test_deleting_a_document_withdraws_what_was_read_from_it(
    db: sqlite3.Connection, cv_file: Path
) -> None:
    """Withdrawn, not deleted: what was once claimed, and on what basis, survives."""
    result = store.ingest(db, cv_file, kind="cv")
    for record in records.current(db):
        records.confirm(db, record.id)
    assert records.confirmed_paths(db)

    store.delete(db, result.document_id)
    assert records.confirmed_paths(db) == set()
    assert len(records.current(db)) > 0


def test_listing_returns_metadata_only(db: sqlite3.Connection, tmp_path: Path) -> None:
    passport = tmp_path / "passport.txt"
    passport.write_text("PASSPORT AB1234567 SECRET", encoding="utf-8")
    store.ingest(db, passport, kind="passport")

    rows = store.listing(db)
    assert len(rows) == 1
    assert "AB1234567" not in str(dict(rows[0]))
