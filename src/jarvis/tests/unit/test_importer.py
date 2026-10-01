"""Importing an existing master CV."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from jarvis.profile import functions as fn
from jarvis.profile import records
from jarvis.profile.importer import import_master_cv

#: The repository's real master CV, used for one end-to-end check. Tests skip
#: rather than fail when it is absent, so the suite stands on its own.
REAL_MASTER_CV = Path(__file__).resolve().parents[4] / "data" / "master_cv.json"

SAMPLE: dict = {
    "basics": {
        "name": "Test Person",
        "email": "test@example.com",
        "phone": "+92 300 0000000",
        "summary": "An engineer.",
        "location": {"city": "Lahore", "region": "Punjab", "countryCode": "PK"},
    },
    "x_mobility": {
        "citizenship": ["PK"],
        "right_to_work_without_sponsorship": ["PK"],
        "passport": {"number": "AB1234567", "date_of_expiry": "2030-01-01"},
    },
    "work": [
        {
            "name": "Interloop Limited",
            "position": "Sr. Officer Planning",
            "startDate": "2021-04-01",
            "endDate": None,
            "summary": "Planning.",
            "verified": True,
        },
        {
            "name": "Indus Bricks",
            "position": "Intern",
            "startDate": "2019-02",
            "endDate": "2019-02",
            "verified": False,
        },
    ],
    "education": [
        {
            "institution": "UET Taxila",
            "studyType": "B.Sc.",
            "area": "Industrial Engineering",
            "endDate": "2019-08-16",
            "verified": True,
        }
    ],
    "skills": [{"name": "Lean Manufacturing", "keywords": ["5S", "Kaizen"]}],
    "certificates": [
        {"name": "PEC Registered Engineer", "issuer": "PEC", "verified": "partial"},
        {"name": "Six Sigma Green Belt", "verified": False},
    ],
    "x_achievements": [
        {"statement": "Cut changeover time by 22%.", "verified": True,
         "usable_in_applications": True},
        {"statement": "Unverifiable claim.", "verified": False,
         "usable_in_applications": False},
    ],
    "x_preferences": {"current_compensation": {"components": [{"amount": 1}]}},
}


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "master_cv.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _value(conn: sqlite3.Connection, path: str) -> str | None:
    record = records.get(conn, path)
    return record.value if record else None


# ------------------------------------------------------------------- mapping


def test_basics_are_imported_confirmed(db: sqlite3.Connection, tmp_path: Path) -> None:
    import_master_cv(db, _write(tmp_path, SAMPLE))
    assert _value(db, "basics.name") == "Test Person"
    assert _value(db, "basics.email") == "test@example.com"
    assert "basics.name" in records.confirmed_paths(db)


def test_a_written_summary_is_not_confirmed(db: sqlite3.Connection, tmp_path: Path) -> None:
    """Prose somebody wrote is a claim, not a fact read from a document."""
    import_master_cv(db, _write(tmp_path, SAMPLE))
    assert records.get(db, "basics.summary").confirmed is False


def test_verified_work_is_confirmed_and_unverified_is_not(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    import_master_cv(db, _write(tmp_path, SAMPLE))
    assert records.get(db, "work[0].employer").confirmed is True
    assert records.get(db, "work[1].employer").confirmed is False


def test_partial_verification_does_not_count_as_verified(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """A partly-verified claim is not one an application may rest on."""
    import_master_cv(db, _write(tmp_path, SAMPLE))
    assert records.get(db, "certificates[0].name").confirmed is False


def test_null_values_are_skipped_not_stored_as_empty(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    import_master_cv(db, _write(tmp_path, SAMPLE))
    assert records.get(db, "work[0].endDate") is None


def test_passport_details_are_never_imported(db: sqlite3.Connection, tmp_path: Path) -> None:
    """Restricted data is stored as a document and never enters the profile."""
    report = import_master_cv(db, _write(tmp_path, SAMPLE))
    stored = {r.field_path: r.value for r in records.current(db)}
    assert "AB1234567" not in stored.values()
    assert not any("passport" in p for p in stored)
    assert any("passport" in s for s in report.skipped)


def test_current_compensation_is_never_imported(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    report = import_master_cv(db, _write(tmp_path, SAMPLE))
    assert not any("compensation" in p for p in {r.field_path for r in records.current(db)})
    assert any("compensation" in s for s in report.skipped)


def test_only_usable_achievements_are_imported(db: sqlite3.Connection, tmp_path: Path) -> None:
    import_master_cv(db, _write(tmp_path, SAMPLE))
    statements = [r.value for r in records.current(db) if "achievement" in r.field_path]
    assert statements == ["Cut changeover time by 22%."]


def test_tax_residence_is_never_inferred(db: sqlite3.Connection, tmp_path: Path) -> None:
    """It decides how a remote salary is taxed, and it is not citizenship.

    Guessing it would put a fabricated value behind every remote projection.
    """
    report = import_master_cv(db, _write(tmp_path, SAMPLE))
    assert records.get(db, "identity.tax_residence") is None
    assert any("tax residence" in w.lower() for w in report.warnings)


def test_importing_does_not_satisfy_the_checklist_on_its_own(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    import_master_cv(db, _write(tmp_path, SAMPLE))
    mvp = fn.minimum_viable_profile(db)
    assert mvp.satisfied is False
    assert [i.key for i in mvp.outstanding] == ["tax_residence"]


# ------------------------------------------------------------ expiry handling


def test_an_expired_language_test_is_imported_unconfirmed(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """The important one.

    Importing a lapsed score as confirmed would let it satisfy a posting's live
    language requirement — a quiet falsehood behind an application.
    """
    data = dict(SAMPLE)
    data["languages"] = [
        {
            "language": "English",
            "fluency": "C1",
            "test": {"name": "PTE Academic", "overall": 70, "valid_until": "2025-10-02"},
        }
    ]
    report = import_master_cv(db, _write(tmp_path, data), today=date(2026, 9, 20))

    assert records.get(db, "languages[0].test").confirmed is False
    assert "languages[].test" not in records.confirmed_paths(db)
    assert _value(db, "languages[0].test_valid_until") == "2025-10-02"
    assert any("lapsed" in w for w in report.warnings)


def test_a_current_language_test_is_confirmed(db: sqlite3.Connection, tmp_path: Path) -> None:
    data = dict(SAMPLE)
    data["languages"] = [
        {
            "language": "English",
            "test": {"name": "IELTS", "overall": 7.5, "valid_until": "2028-01-01"},
        }
    ]
    import_master_cv(db, _write(tmp_path, data), today=date(2026, 9, 20))
    assert records.get(db, "languages[0].test").confirmed is True
    assert "languages[].test" in records.confirmed_paths(db)


def test_an_explicit_expired_status_is_honoured_without_a_date(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    data = dict(SAMPLE)
    data["languages"] = [
        {
            "language": "English",
            "test": {"name": "PTE", "overall": 70, "validity_status": "EXPIRED"},
        }
    ]
    import_master_cv(db, _write(tmp_path, data), today=date(2020, 1, 1))
    assert records.get(db, "languages[0].test").confirmed is False


def test_an_expired_test_makes_a_language_posting_insufficient(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """End to end: the expiry actually reaches the verdict."""
    data = dict(SAMPLE)
    data["languages"] = [
        {"language": "English",
         "test": {"name": "PTE", "overall": 70, "valid_until": "2025-10-02"}}
    ]
    import_master_cv(db, _write(tmp_path, data), today=date(2026, 9, 20))
    records.add(db, "identity.tax_residence", "PK", confirmed=True)

    verdict = fn.sufficiency(db, {
        "id": "o1", "kind": "job", "role_family": "industrial-engineering",
        "requirements": {"requires_language_test": True}, "requirements_confidence": 0.9,
    })
    assert verdict.verdict == "insufficient"
    assert "languages[].test" in verdict.missing_field_paths


# ------------------------------------------------------- against the real file


@pytest.mark.skipif(not REAL_MASTER_CV.exists(), reason="no master_cv.json in this checkout")
def test_the_real_master_cv_imports(db: sqlite3.Connection) -> None:
    report = import_master_cv(db, REAL_MASTER_CV, today=date(2026, 9, 20))

    assert report.imported > 50
    assert report.confirmed > 0
    assert records.get(db, "basics.name") is not None
    assert "identity.citizenship" in records.confirmed_paths(db)
    # Tax residence is the one thing that still has to be asked for.
    assert [i.key for i in fn.minimum_viable_profile(db).outstanding] == ["tax_residence"]
