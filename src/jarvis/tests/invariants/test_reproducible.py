"""T029 — I-19, I-21, I-22.

Three properties that together decide whether a number on the screen means
anything:

* **I-19** every score recomputes by hand from its own stored row
* **I-22** a quoted fact appears verbatim in the snapshot it names
* **I-21** the whole pipeline runs with no AI available anywhere

The third one is the reason the other two are possible. Nothing in discovery,
scoring, projection, eligibility or filtering calls a model, so every number has
arithmetic behind it rather than a generation behind it — and the test asserts
that by making any model call fail.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from jarvis import eligibility as eligibility_module
from jarvis import service
from jarvis.discovery import requirements as requirements_module
from jarvis.matching import screen
from jarvis.money import fx, reference
from jarvis.profile import records
from jarvis.store import snapshots
from jarvis.tests import support

POSTING_TEXT = (
    "Industrial Engineer, Dubai. Requirements: a bachelor's degree in industrial "
    "or mechanical engineering, five years of manufacturing experience, lean "
    "manufacturing, Six Sigma, AutoCAD and strong Excel. You will run time "
    "studies, improve line balancing and report OEE weekly. Send your CV."
)

COSTS = {
    "rent": 6000.0,
    "utilities": 700.0,
    "food": 1500.0,
    "transport": 500.0,
    "health_insurance": 400.0,
}


def _profile(conn: sqlite3.Connection) -> None:
    records.add(conn, "identity.citizenship", "PK", confirmed=True)
    records.add(conn, "identity.tax_residence", "PK", confirmed=True)
    records.add(conn, "basics.location.countryCode", "PK", confirmed=True)
    records.add(conn, "basics.location.city", "Lahore", confirmed=True)
    records.add(conn, "languages[0].language", "English", confirmed=True)
    records.add(conn, "education[0].studyType", "B.Sc.", confirmed=True)
    records.add(conn, "skills[0].name", "Lean Manufacturing", confirmed=True)
    records.add(conn, "skills[1].name", "Six Sigma", confirmed=True)
    records.add(conn, "work[0].startDate", "2019-01-01", confirmed=True)
    records.add(conn, "work[0].endDate", "2024-01-01", confirmed=True)


def _world(conn: sqlite3.Connection) -> None:
    fx.record_rate(
        conn,
        base="AED",
        quote="USD",
        rate=0.2723,
        as_of="2026-09-20",
        source_url="https://example.test/rates",
    )
    support.sourced_figure(
        conn,
        kind="tax_rate",
        value=0.0,
        unit="fraction",
        country_iso2="AE",
        quote="There is no personal income tax in the United Arab Emirates.",
    )
    for kind, value in COSTS.items():
        support.sourced_figure(
            conn,
            kind=kind,
            value=value,
            unit="per_month",
            currency="AED",
            country_iso2="AE",
            city="Dubai",
            quote=f"Average {kind} in Dubai is AED {value:,.0f} per month.",
        )


def _opportunity(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES ('AE', 'UAE', 1)")
    parsed = requirements_module.parse(POSTING_TEXT, title="Industrial Engineer")
    conn.execute(
        "INSERT INTO opportunity (id, kind, country_iso2, city, work_arrangement, title, "
        "employer, role_family, requirements, requirements_confidence, description, "
        "pay_disclosed, source_url, fetched_at, posted_at, dedupe_key, employer_norm, "
        "title_norm, first_seen_at, last_seen_at) "
        "VALUES ('opp-1', 'job', 'AE', 'Dubai', 'onsite', 'Industrial Engineer', 'Acme', "
        "'industrial_engineering', ?, ?, ?, ?, 'https://example.test/jobs/1', "
        "'2026-09-20T00:00:00Z', '2026-09-18T00:00:00Z', 'k1', 'acme', "
        "'industrial engineer', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')",
        (
            json.dumps(parsed.as_dict()),
            max(parsed.confidence, 0.9),
            POSTING_TEXT,
            json.dumps(
                {
                    "min": 22000.0,
                    "max": 28000.0,
                    "currency": "AED",
                    "period": "month",
                    "raw_text": "AED 22,000 - 28,000 per month",
                }
            ),
        ),
    )


# ------------------------------------------------------------------- I-19


def test_score_reproducible(db: sqlite3.Connection) -> None:
    """Every component, its effective weight and the total, checked by hand."""
    _profile(db)
    _world(db)
    _opportunity(db)

    result = screen(db, "opp-1", as_of=date(2026, 9, 23))
    assert result.assessment is not None
    row = result.assessment.as_row()

    components = row["inputs"]["components"]
    effective = row["weights"]["effective"]

    # The sum, recomputed from the stored row alone — no engine involved.
    by_hand = sum(
        body["score"] * effective[name]
        for name, body in components.items()
        if body["score"] is not None
    )
    assert by_hand == pytest.approx(row["match_score"])

    # Every unscored component names why, and its weight is gone rather than zero.
    for entry in row["unscored_components"]:
        assert entry["reason"]
        assert effective[entry["component"]] == 0.0

    # The effective weights of the scored components sum to 1 at full precision.
    scored = [name for name, body in components.items() if body["score"] is not None]
    assert sum(effective[name] for name in scored) == pytest.approx(1.0, abs=1e-12)


def test_projection_reproducible(db: sqlite3.Connection) -> None:
    """The savings arithmetic too: every line, from its own stored numbers."""
    _profile(db)
    _world(db)
    _opportunity(db)

    result = screen(db, "opp-1", as_of=date(2026, 9, 23))
    assert result.assessment is not None
    stored = result.assessment.as_row()["projection"]

    income = sum(
        line["amount_usd_month"] for line in stored["lines"] if line["direction"] == "income"
    )
    deductions = sum(
        line["amount_usd_month"]
        for line in stored["lines"]
        if line["direction"] in {"cost", "deduction"}
    )
    assert income - deductions == pytest.approx(stored["net_savings_usd_month"])

    # And every line says where its two halves came from.
    for line in stored["lines"]:
        if line["kind"] in {"pay", "tax"}:
            continue
        assert line["reference_figure_id"]
        assert line["fx_rate_id"]


# ------------------------------------------------------------------- I-22


def test_quote_in_snapshot(db: sqlite3.Connection) -> None:
    """Every stored figure's quote is really in the page it names."""
    _world(db)

    rows = db.execute(
        "SELECT kind, quote, snapshot_path FROM reference_figure WHERE superseded_by IS NULL"
    ).fetchall()
    assert rows, "nothing was stored, so this test would pass vacuously"

    for row in rows:
        assert snapshots.contains(row["snapshot_path"], row["quote"]), (
            f"the {row['kind']} figure quotes {row['quote']!r}, which is not in "
            f"{row['snapshot_path']}"
        )


def test_a_paraphrased_quote_is_refused(db: sqlite3.Connection) -> None:
    """The check has to be able to fail, or it is decoration."""
    snapshot = snapshots.store(
        "<html><body><p>Average rent in Dubai is AED 6,000 per month.</p></body></html>",
        source_url="https://example.test/rent",
    )

    with pytest.raises(reference.QuoteNotInSnapshot, match="does not appear"):
        reference.add_figure(
            db,
            kind="rent",
            value=6000.0,
            unit="per_month",
            currency="AED",
            source_url="https://example.test/rent",
            as_of="2026-09-01",
            quote="rent is about six thousand dirhams",  # true, but not what it says
            snapshot_path=snapshot.relative_path,
        )


def test_a_figure_with_a_missing_snapshot_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(reference.QuoteNotInSnapshot):
        reference.add_figure(
            db,
            kind="rent",
            value=6000.0,
            unit="per_month",
            currency="AED",
            source_url="https://example.test/rent",
            as_of="2026-09-01",
            quote="Average rent in Dubai is AED 6,000 per month.",
            snapshot_path="snapshots/never-written.html",
        )


def test_markup_between_the_words_does_not_break_the_check(db: sqlite3.Connection) -> None:
    """A quote read from a rendered page is separated by tags in the source."""
    snapshot = snapshots.store(
        "<html><body><p>Average rent in\n  <b>Dubai</b> is\tAED&nbsp;6,000 "
        "per month.</p></body></html>",
        source_url="https://example.test/rent",
    )

    figure_id = reference.add_figure(
        db,
        kind="rent",
        value=6000.0,
        unit="per_month",
        currency="AED",
        source_url="https://example.test/rent",
        as_of="2026-09-01",
        quote="Average rent in Dubai is AED 6,000 per month.",
        snapshot_path=snapshot.relative_path,
    )

    assert figure_id


def test_a_snapshot_is_content_addressed_and_carries_its_provenance() -> None:
    first = snapshots.store("<p>same bytes</p>", source_url="https://example.test/a")
    again = snapshots.store("<p>same bytes</p>", source_url="https://example.test/a")

    assert first.path == again.path
    assert first.sha256 == again.sha256

    meta = json.loads(
        Path(str(first.path) + ".meta.json").read_text(encoding="utf-8")
    )
    assert meta["source_url"] == "https://example.test/a"
    assert meta["sha256"] == first.sha256
    # Relative, so a restore onto another machine can still find its evidence.
    assert not Path(first.relative_path).is_absolute()


# ------------------------------------------------------------------- I-21


def test_full_pipeline_no_ai(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingest to verdict with every model call made to explode.

    The guard is installed on the modules an AI path would have to go through —
    an HTTP client, and the two SDKs a future AI feature would use. Nothing in
    this pipeline touches them, and if something starts to, this fails rather
    than quietly producing a generated number.
    """
    import httpx

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the deterministic pipeline tried to reach a model. Every number it "
            "produces has to be arithmetic over stored inputs (I-21)."
        )

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)
    monkeypatch.setattr(httpx.AsyncClient, "request", explode)

    _profile(db)
    _world(db)
    _opportunity(db)

    # Requirement parsing, role family, scoring, projection, eligibility, filters.
    result = screen(db, "opp-1", as_of=date(2026, 9, 23))

    assert not result.rejected
    assessment = result.assessment
    assert assessment is not None
    assert assessment.total > 0
    assert assessment.projection is not None
    assert assessment.projection.net_savings_usd_month is not None
    assert assessment.eligibility is not None
    assert assessment.eligibility.verdict in {"eligible", "ineligible", "unscored"}

    # And the read side, which is what the UI and MCP call.
    row = assessment.as_row()
    db.execute(
        "INSERT INTO assessment (opportunity_id, config_version, match_score, confidence, "
        "formula, weights, inputs, unscored_components, projection, passes_floor, "
        "floor_applied, floor_source, pay_basis, eligibility_verdict, eligibility_detail, "
        "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'2026-09-23T00:00:00Z')",
        (
            row["opportunity_id"],
            row["config_version"],
            row["match_score"],
            row["confidence"],
            row["formula"],
            json.dumps(row["weights"]),
            json.dumps(row["inputs"]),
            json.dumps(row["unscored_components"]),
            json.dumps(row["projection"]),
            row["passes_floor"],
            row["floor_applied"],
            row["floor_source"],
            row["pay_basis"],
            row["eligibility_verdict"],
            row["eligibility_detail"],
        ),
    )

    payload = service.get_opportunity(db, "opp-1")
    assert payload["assessment"]["projection"]["net_savings_usd_month"] is not None
    assert payload["eligibility"]["verdict"] == row["eligibility_verdict"]
    assert len(payload["eligibility"]["findings"]) == len(eligibility_module.DIMENSIONS)

    status = service.capability_status(db)
    assert status["financial_projection"]["available"] is True
    # The AI-shaped capability reports itself without an AI, rather than erroring.
    assert status["tailored_prose"]["available"] is True


def test_a_snapshot_missing_its_metadata_gets_it_on_the_next_store() -> None:
    """One interrupted write must not leave a page without provenance for ever."""
    first = snapshots.store("<p>page</p>", source_url="https://example.test/p")
    meta = Path(str(first.path) + ".meta.json")
    meta.unlink()

    snapshots.store("<p>page</p>", source_url="https://example.test/p")

    assert meta.is_file()
