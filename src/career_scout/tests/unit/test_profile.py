"""Profile records, and the three functions that replaced the gate."""

from __future__ import annotations

import sqlite3

import pytest

from career_scout.profile import functions as fn
from career_scout.profile import records
from career_scout.profile.schema import COMPLETENESS, is_restricted, normalise


def _confirm(conn: sqlite3.Connection, **paths: str) -> None:
    for path, value in paths.items():
        records.add(conn, path.replace("__", "."), value, confirmed=True)


def _minimum(conn: sqlite3.Connection) -> None:
    """Exactly the Minimum Viable Profile, and nothing more."""
    records.add(conn, "basics.name", "Alex Example", confirmed=True)
    records.add(conn, "basics.email", "t@example.com", confirmed=True)
    records.add(conn, "identity.citizenship", "PK", confirmed=True)
    records.add(conn, "identity.tax_residence", "EG", confirmed=True)
    records.add(conn, "work[0].employer", "Pyraloop", confirmed=True)
    records.add(conn, "work[0].position", "Industrial Engineer", confirmed=True)
    records.add(conn, "work[0].startDate", "2019-03-01", confirmed=True)


def _document(conn: sqlite3.Connection, document_id: str = "d1") -> str:
    """A real document row, because profile_record.document_id is a foreign key.

    That constraint is the point: a record cannot claim to have been extracted
    from a document that does not exist.
    """
    conn.execute(
        "INSERT INTO document (id, kind, filename, path, sha256, bytes, uploaded_at) "
        "VALUES (?, 'cv', ?, ?, 'abc123', 1024, '2026-01-01T00:00:00Z')",
        (document_id, f"{document_id}.pdf", f"/documents/source/{document_id}.pdf"),
    )
    return document_id


def _job(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "o1",
        "kind": "job",
        "role_family": "industrial-engineering",
        "requirements": {},
        "requirements_confidence": 0.9,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------ path handling


def test_normalise_collapses_indices() -> None:
    assert normalise("work[3].position") == "work[].position"
    assert normalise("basics.name") == "basics.name"


def test_restricted_paths_are_recognised() -> None:
    assert is_restricted("identity.passport_number")
    assert is_restricted("x_preferences.current_compensation.components[0].amount")
    assert not is_restricted("work[0].position")


def test_no_restricted_path_carries_completeness_weight() -> None:
    """Uploading a passport must not be how you reach 100%."""
    assert not any(is_restricted(f.path) for f in COMPLETENESS)


# ------------------------------------------------------------- provenance


def test_typed_and_extracted_values_are_distinguishable(db: sqlite3.Connection) -> None:
    _document(db)
    typed = records.add(db, "basics.phone", "+20 100", confirmed=True)
    extracted = records.add(
        db, "basics.email", "t@example.com", document_id="d1",
        locator={"page": 1, "start": 40, "end": 55}, confirmed=True,
    )
    assert records.get(db, "basics.phone").from_document is False
    assert records.get(db, "basics.email").from_document is True
    assert typed != extracted


def test_a_locator_without_a_document_is_refused(db: sqlite3.Connection) -> None:
    """"Extracted" cannot be asserted about something nobody extracted."""
    with pytest.raises(records.ProvenanceError):
        records.add(db, "basics.name", "Someone", locator={"page": 1, "start": 0, "end": 4})


def test_extraction_is_unconfirmed_by_default(db: sqlite3.Connection) -> None:
    """A proposal the user has not looked at is not a fact."""
    _document(db)
    records.add(db, "basics.name", "Guessed Name", document_id="d1")
    assert records.get(db, "basics.name").confirmed is False
    assert "basics.name" not in records.confirmed_paths(db)


# ------------------------------------------------------------ supersession


def test_correction_retains_the_original(db: sqlite3.Connection) -> None:
    first = records.add(db, "basics.email", "wrong@example.com", confirmed=True)
    records.supersede(db, first, "right@example.com")

    assert records.get(db, "basics.email").value == "right@example.com"
    history = records.history(db, "basics.email")
    assert [r.value for r in history] == ["wrong@example.com", "right@example.com"]
    assert history[0].superseded_by == history[1].id


def test_a_superseded_record_cannot_be_superseded_again(db: sqlite3.Connection) -> None:
    first = records.add(db, "basics.email", "a@x.com", confirmed=True)
    records.supersede(db, first, "b@x.com")
    with pytest.raises(ValueError, match="already superseded"):
        records.supersede(db, first, "c@x.com")


def test_withdrawing_a_record_removes_it_from_confirmed_paths(db: sqlite3.Connection) -> None:
    record_id = records.add(db, "languages[0].test", "IELTS 7.5", confirmed=True)
    assert "languages[].test" in records.confirmed_paths(db)
    records.withdraw(db, record_id)
    assert "languages[].test" not in records.confirmed_paths(db)


def test_blank_values_do_not_count_as_confirmed(db: sqlite3.Connection) -> None:
    records.add(db, "basics.summary", "   ", confirmed=True)
    assert "basics.summary" not in records.confirmed_paths(db)


def test_conflicting_documents_both_persist(db: sqlite3.Connection) -> None:
    """Neither is overwritten; the user is shown the disagreement."""
    _document(db, "letter")
    _document(db, "payslip")
    records.add(db, "work[0].startDate", "2019-03-01", document_id="letter", confirmed=True)
    records.add(db, "work[0].startDate", "2019-04-15", document_id="payslip", confirmed=True)

    found = records.conflicts(db)
    assert len(found) == 1
    path, rows = found[0]
    assert path == "work[0].startDate"
    assert {r.value for r in rows} == {"2019-03-01", "2019-04-15"}


def test_next_index_appends_without_reading_everything(db: sqlite3.Connection) -> None:
    assert records.next_index(db, "work[]") == 0
    records.add(db, "work[0].employer", "A", confirmed=True)
    records.add(db, "work[1].employer", "B", confirmed=True)
    assert records.next_index(db, "work[]") == 2


# ----------------------------------------------------------- completeness


def test_completeness_is_zero_on_an_empty_profile(db: sqlite3.Connection) -> None:
    assert fn.completeness(db).percentage == 0.0


def test_completeness_is_reproducible_by_hand(db: sqlite3.Connection) -> None:
    """earned / total must equal the percentage shown, exactly."""
    _minimum(db)
    result = fn.completeness(db)
    assert result.earned == pytest.approx(
        sum(f.weight for f in COMPLETENESS if f.path in result.present)
    )
    assert result.percentage == pytest.approx(round(100 * result.earned / result.total, 1))


def test_completeness_names_every_missing_field_and_its_weight(db: sqlite3.Connection) -> None:
    _minimum(db)
    result = fn.completeness(db)
    assert result.missing
    for item in result.missing:
        assert item.label and item.weight > 0
    assert len(result.present) + len(result.missing) == len(COMPLETENESS)


def test_a_repeated_group_scores_once(db: sqlite3.Connection) -> None:
    """Four jobs must not outrank two."""
    records.add(db, "work[0].employer", "A", confirmed=True)
    one = fn.completeness(db).earned
    records.add(db, "work[1].employer", "B", confirmed=True)
    records.add(db, "work[2].employer", "C", confirmed=True)
    assert fn.completeness(db).earned == one


def test_unconfirmed_values_do_not_raise_completeness(db: sqlite3.Connection) -> None:
    _document(db)
    records.add(db, "basics.name", "X", document_id="d1")
    assert fn.completeness(db).percentage == 0.0


# --------------------------------------------------- minimum viable profile


def test_mvp_is_unsatisfied_on_an_empty_profile(db: sqlite3.Connection) -> None:
    result = fn.minimum_viable_profile(db)
    assert result.satisfied is False
    assert len(result.outstanding) == 4


def test_mvp_is_satisfied_by_a_handful_of_fields(db: sqlite3.Connection) -> None:
    """Minutes, not hours — and well under any completeness threshold."""
    _minimum(db)
    assert fn.minimum_viable_profile(db).satisfied is True
    assert fn.completeness(db).percentage < 60


def test_mvp_accepts_education_instead_of_work(db: sqlite3.Connection) -> None:
    """A graduate with no job history is not locked out."""
    records.add(db, "basics.name", "A", confirmed=True)
    records.add(db, "basics.email", "a@x.com", confirmed=True)
    records.add(db, "identity.citizenship", "PK", confirmed=True)
    records.add(db, "identity.tax_residence", "PK", confirmed=True)
    records.add(db, "education[0].institution", "UET", confirmed=True)
    records.add(db, "education[0].studyType", "BSc", confirmed=True)
    records.add(db, "education[0].endDate", "2018-09-01", confirmed=True)
    assert fn.minimum_viable_profile(db).satisfied is True


def test_mvp_reports_items_by_name_not_a_score(db: sqlite3.Connection) -> None:
    for item in fn.minimum_viable_profile(db).outstanding:
        assert item.label and item.rationale
        assert item.missing


def test_mvp_reports_the_nearest_alternative(db: sqlite3.Connection) -> None:
    """Partly-filled work history should be reported, not the untouched education path."""
    records.add(db, "work[0].employer", "Pyraloop", confirmed=True)
    records.add(db, "work[0].position", "Engineer", confirmed=True)
    history = next(i for i in fn.minimum_viable_profile(db).items if i.key == "history")
    assert history.missing == ("work[].startDate",)


# ------------------------------------------------------------- sufficiency


@pytest.mark.invariant
def test_unparsed_requirements_are_not_sufficient(db: sqlite3.Connection) -> None:
    """I-18 — a posting we could not read must fail closed.

    An empty requirement set matches no conditional rule. Treating that as
    "nothing required, therefore covered" would authorise an application to a
    posting nobody has read. Measured basis: 481 of 2,305 legacy assessments
    found no vocabulary match at all in the posting.
    """
    _minimum(db)
    result = fn.sufficiency(db, _job(requirements={}, requirements_confidence=0.05))
    assert result.verdict == "not_evaluated"
    assert result.may_generate is False
    assert "could not be read confidently" in (result.reason or "")


@pytest.mark.invariant
def test_a_confidently_parsed_posting_is_evaluated(db: sqlite3.Connection) -> None:
    _minimum(db)
    records.add(db, "work[0].summary", "Led line balancing", confirmed=True)
    result = fn.sufficiency(db, _job(requirements_confidence=0.9))
    assert result.verdict == "sufficient"
    assert result.may_generate is True


@pytest.mark.invariant
def test_sufficiency_blocks_one_opportunity_not_the_account(db: sqlite3.Connection) -> None:
    """I-17 — an insufficient verdict is scoped to the posting that caused it."""
    _minimum(db)
    records.add(db, "work[0].summary", "Led line balancing", confirmed=True)

    plain = _job(id="plain")
    demanding = _job(id="demanding", requirements={"requires_language_test": True})

    assert fn.sufficiency(db, plain).verdict == "sufficient"
    assert fn.sufficiency(db, demanding).verdict == "insufficient"


def test_insufficiency_names_the_exact_missing_paths(db: sqlite3.Connection) -> None:
    _minimum(db)
    records.add(db, "work[0].summary", "Led line balancing", confirmed=True)
    result = fn.sufficiency(db, _job(requirements={"requires_language_test": True}))
    assert result.missing_field_paths == ("languages[].test",)
    assert "languages[].test" in result.fields_examined


def test_filling_an_unrelated_field_changes_no_verdict(db: sqlite3.Connection) -> None:
    """The gate could be beaten by typing. Sufficiency cannot."""
    _minimum(db)
    records.add(db, "work[0].summary", "Led line balancing", confirmed=True)
    demanding = _job(requirements={"requires_language_test": True})
    before = fn.sufficiency(db, demanding)

    for path in ("basics.phone", "basics.summary", "skills[0].name", "certificates[0].name"):
        records.add(db, path, "something", confirmed=True)

    after = fn.sufficiency(db, demanding)
    assert after.verdict == before.verdict == "insufficient"
    assert after.missing_field_paths == before.missing_field_paths


def test_filling_the_required_field_flips_the_verdict(db: sqlite3.Connection) -> None:
    _minimum(db)
    records.add(db, "work[0].summary", "Led line balancing", confirmed=True)
    demanding = _job(requirements={"requires_language_test": True})
    assert fn.sufficiency(db, demanding).verdict == "insufficient"

    records.add(db, "languages[0].test", "IELTS 7.5", confirmed=True)
    assert fn.sufficiency(db, demanding).verdict == "sufficient"


def test_withdrawing_a_record_returns_the_verdict_to_insufficient(db: sqlite3.Connection) -> None:
    """A claim that is withdrawn must stop supporting anything queued on it."""
    _minimum(db)
    records.add(db, "work[0].summary", "Led line balancing", confirmed=True)
    test_id = records.add(db, "languages[0].test", "IELTS 7.5", confirmed=True)
    demanding = _job(requirements={"requires_language_test": True})
    assert fn.sufficiency(db, demanding).verdict == "sufficient"

    records.withdraw(db, test_id)
    assert fn.sufficiency(db, demanding).verdict == "insufficient"


def test_a_scholarship_is_judged_on_academic_record(db: sqlite3.Connection) -> None:
    _minimum(db)
    scholarship = _job(kind="scholarship")
    result = fn.sufficiency(db, scholarship)
    assert result.verdict == "insufficient"
    assert "education[].institution" in result.missing_field_paths


def test_requirements_accepts_json_text(db: sqlite3.Connection) -> None:
    """Rows come back from SQLite as text; the verdict must not depend on the caller."""
    _minimum(db)
    records.add(db, "work[0].summary", "x", confirmed=True)
    as_text = _job(requirements='{"requires_language_test": true}')
    assert fn.sufficiency(db, as_text).verdict == "insufficient"


def test_high_completeness_does_not_authorise_an_uncovered_application(
    db: sqlite3.Connection,
) -> None:
    """The two measures are independent by design."""
    for definition in COMPLETENESS:
        path = definition.path.replace("[]", "[0]")
        records.add(db, path, "filled", confirmed=True)
    records.withdraw(db, records.get(db, "languages[0].test").id)

    assert fn.completeness(db).percentage > 90
    verdict = fn.sufficiency(db, _job(requirements={"requires_language_test": True}))
    assert verdict.verdict == "insufficient"


# ------------------------------------------------------- verdicts and ranking


def test_ranked_unlocks_counts_opportunities_not_fields(db: sqlite3.Connection) -> None:
    """'Add your IELTS score - unlocks 14 opportunities' falls out of one query."""
    _minimum(db)
    records.add(db, "work[0].summary", "x", confirmed=True)

    for index in range(3):
        opportunity_id = f"o{index}"
        db.execute(
            "INSERT INTO opportunity (id, kind, title, employer, source_url, fetched_at, "
            "dedupe_key, employer_norm, title_norm, first_seen_at, last_seen_at) "
            "VALUES (?, 'job', 'Engineer', 'Acme', 'https://x/1', '2026-01-01T00:00:00Z', "
            "?, 'acme', 'engineer', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (opportunity_id, opportunity_id),
        )
        verdict = fn.sufficiency(db, _job(id=opportunity_id,
                                          requirements={"requires_language_test": True}))
        fn.store_verdict(db, opportunity_id, verdict)

    unlocks = fn.ranked_unlocks(db)
    assert unlocks[0].field_path == "languages[].test"
    assert unlocks[0].unlocks == 3
    assert unlocks[0].label == "Language test score"


def test_seed_definitions_writes_the_versioned_rules(db: sqlite3.Connection) -> None:
    fn.seed_definitions(db)
    count = db.execute("SELECT COUNT(*) FROM sufficiency_rule").fetchone()[0]
    assert count > 0
    fn.seed_definitions(db)  # idempotent for a given version
    assert db.execute("SELECT COUNT(*) FROM sufficiency_rule").fetchone()[0] == count
