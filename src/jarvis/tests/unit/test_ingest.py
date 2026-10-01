"""The ingest stage — normalise, judge, dedupe, store. T030d (store half).

What is asserted here is the behaviour of the stage as a whole: that nothing
vanishes without a row saying why, that the same vacancy from two boards is one
opportunity, and that a rejected posting is kept rather than dropped.
"""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.discovery import authenticity, ingest


@pytest.fixture
def store(memory_db: sqlite3.Connection) -> sqlite3.Connection:
    memory_db.executemany(
        "INSERT INTO country (iso2, name, enabled) VALUES (?, ?, 1)",
        [("US", "United States"), ("DE", "Germany")],
    )
    memory_db.executemany(
        "INSERT INTO source (id, name, provider, base_url, access_mode, legal_basis) "
        "VALUES (?, ?, ?, ?, 'api', 'public API')",
        [
            ("board-a", "Board A", "greenhouse", "https://a.example"),
            ("board-b", "Board B", "lever", "https://b.example"),
        ],
    )
    return memory_db


def posting(**overrides) -> ingest.RawPosting:
    values = {
        "source_id": "board-a",
        "source_url": "https://a.example/jobs/1",
        "title": "Industrial Engineer",
        "employer": "Acme Manufacturing",
        "location_raw": "Austin, United States",
        "description": "You will improve throughput across three assembly lines. "
                       "Requirements: 3 years of experience in process improvement.",
        "fetched_at": "2026-09-01T00:00:00Z",
    }
    values.update(overrides)
    return ingest.RawPosting(**values)


class TestStoring:
    def test_a_new_posting_is_stored(self, store):
        outcome = ingest.ingest_one(store, posting())
        assert outcome.outcome == ingest.STORED
        row = store.execute("SELECT * FROM opportunity").fetchone()
        assert row["title"] == "Industrial Engineer"
        assert row["country_iso2"] == "US"
        assert row["is_canonical"] == 1

    def test_requirements_are_parsed_once_at_ingest(self, store):
        # No later step reads `description`, so a posting parsed here is parsed
        # or it is never parsed at all.
        ingest.ingest_one(store, posting())
        row = store.execute("SELECT * FROM opportunity").fetchone()
        assert row["requirements"] is not None
        assert row["requirements_confidence"] > 0

    def test_the_arrangement_is_classified_at_ingest(self, store):
        ingest.ingest_one(store, posting(location_raw="Remote - worldwide"))
        assert store.execute("SELECT * FROM opportunity").fetchone()["work_arrangement"] == "remote"

    def test_a_freelance_posting_is_stored_as_a_project(self, store):
        ingest.ingest_one(store, posting(title="Freelance Process Engineer"))
        assert (
            store.execute("SELECT * FROM opportunity").fetchone()["work_arrangement"] == "project"
        )

    def test_undisclosed_pay_is_null_not_zero(self, store):
        # 86% of the corpus discloses no pay. A zero here would be a fabricated
        # figure wearing the costume of a measurement.
        ingest.ingest_one(store, posting(pay_min=0, pay_max=0))
        assert store.execute("SELECT * FROM opportunity").fetchone()["pay_disclosed"] is None

    def test_disclosed_pay_is_kept(self, store):
        ingest.ingest_one(store, posting(pay_min=90000, pay_max=120000, pay_currency="usd"))
        payload = store.execute("SELECT * FROM opportunity").fetchone()["pay_disclosed"]
        assert payload is not None and "90000" in payload

    def test_the_source_sighting_is_recorded(self, store):
        ingest.ingest_one(store, posting())
        assert store.execute("SELECT count(*) c FROM opportunity_source").fetchone()["c"] == 1

    def test_an_unresolved_country_is_stored_as_null(self, store):
        ingest.ingest_one(store, posting(location_raw="Somewhere lovely"))
        assert store.execute("SELECT * FROM opportunity").fetchone()["country_iso2"] is None


class TestRejecting:
    def test_a_junk_posting_is_rejected(self, store):
        outcome = ingest.ingest_one(store, posting(title="Oops something happened"))
        assert outcome.outcome == ingest.REJECTED

    def test_a_rejected_posting_is_kept_not_dropped(self, store):
        # Rejections are recorded, not deleted: disable the rule, re-run, and the
        # posting comes back.
        ingest.ingest_one(store, posting(title="CHECK BACK SOON"))
        assert store.execute("SELECT count(*) c FROM opportunity").fetchone()["c"] == 1

    def test_the_rejection_names_its_rule_and_quotes_the_evidence(self, store):
        ingest.ingest_one(store, posting(title="404"))
        row = store.execute("SELECT * FROM rejection").fetchone()
        assert row["rule"].startswith("J01")
        assert row["reason"]
        assert "404" in row["evidence"]

    def test_a_fraudulent_posting_is_rejected(self, store):
        outcome = ingest.ingest_one(
            store,
            posting(description="You must pay a registration fee of $50 before onboarding."),
        )
        assert outcome.outcome == ingest.REJECTED
        assert store.execute("SELECT * FROM rejection").fetchone()["rule"].startswith("S01")

    def test_a_real_posting_writes_no_rejection(self, store):
        ingest.ingest_one(store, posting())
        assert store.execute("SELECT count(*) c FROM rejection").fetchone()["c"] == 0

    def test_a_later_sighting_lifts_a_rejection_the_rules_no_longer_support(
        self, store, monkeypatch
    ):
        # Before this, a known dedupe key was a sighting that never re-ran the
        # check, so a rejection under a since-disabled rule stood for ever.
        first = ingest.ingest_one(store, posting(title="CHECK BACK SOON"))
        assert first.is_rejected

        monkeypatch.setattr(ingest.authenticity, "check", lambda *_a, **_k: authenticity.Verdict())
        ingest.ingest_one(
            store, posting(title="CHECK BACK SOON", fetched_at="2026-09-02T00:00:00Z")
        )

        row = store.execute("SELECT * FROM rejection").fetchone()
        assert row["opportunity_id"] == first.opportunity_id   # kept, not deleted
        assert row["lifted_at"] == "2026-09-02T00:00:00Z"
        assert row["stage"] == "authenticity"

    def test_a_sighting_still_rejected_writes_no_repeat_row(self, store):
        ingest.ingest_one(store, posting(title="CHECK BACK SOON"))
        ingest.ingest_one(store, posting(title="CHECK BACK SOON"))
        rows = store.execute("SELECT * FROM rejection").fetchall()
        assert len(rows) == 1 and rows[0]["lifted_at"] is None

    def test_the_rejection_is_linked_to_its_posting(self, store):
        outcome = ingest.ingest_one(store, posting(title="Test"))
        row = store.execute("SELECT * FROM rejection").fetchone()
        assert row["opportunity_id"] == outcome.opportunity_id


class TestDeduping:
    def test_the_same_vacancy_twice_is_one_opportunity(self, store):
        ingest.ingest_one(store, posting())
        outcome = ingest.ingest_one(store, posting())
        assert outcome.outcome == ingest.SIGHTING
        assert store.execute("SELECT count(*) c FROM opportunity").fetchone()["c"] == 1

    def test_a_second_board_is_recorded_as_another_sighting(self, store):
        first = ingest.ingest_one(store, posting())
        ingest.ingest_one(
            store, posting(source_id="board-b", source_url="https://b.example/9")
        )
        sources = store.execute(
            "SELECT * FROM opportunity_source WHERE opportunity_id = ?", (first.opportunity_id,)
        ).fetchall()
        assert {row["source_id"] for row in sources} == {"board-a", "board-b"}

    def test_a_sighting_moves_last_seen_but_never_first_seen(self, store):
        ingest.ingest_one(store, posting(fetched_at="2026-09-01T00:00:00Z"))
        ingest.ingest_one(
            store,
            posting(source_id="board-b", source_url="https://b.example/9",
                    fetched_at="2026-09-05T00:00:00Z"),
        )
        row = store.execute("SELECT * FROM opportunity").fetchone()
        assert row["first_seen_at"] == "2026-09-01T00:00:00Z"
        assert row["last_seen_at"] == "2026-09-05T00:00:00Z"

    def test_a_near_duplicate_is_merged_and_kept(self, store):
        ingest.ingest_one(store, posting(title="Industrial Engineer"))
        outcome = ingest.ingest_one(
            store,
            posting(title="Industrial Engineers", source_url="https://a.example/jobs/2"),
        )
        assert outcome.outcome == ingest.MERGED
        assert store.execute("SELECT count(*) c FROM opportunity").fetchone()["c"] == 2

    def test_a_merge_records_which_rule_fired(self, store):
        ingest.ingest_one(store, posting(title="Industrial Engineer"))
        ingest.ingest_one(
            store,
            posting(title="Industrial Engineers", source_url="https://a.example/jobs/2"),
        )
        evidence = store.execute("SELECT * FROM opportunity_duplicate").fetchone()
        assert evidence["rule"]
        assert evidence["similarity"] >= 0.93
        assert evidence["evidence"]

    def test_the_merged_row_points_at_the_first_one_seen(self, store):
        first = ingest.ingest_one(store, posting(title="Industrial Engineer"))
        second = ingest.ingest_one(
            store,
            posting(title="Industrial Engineers", source_url="https://a.example/jobs/2"),
        )
        row = store.execute(
            "SELECT * FROM opportunity WHERE id = ?", (second.opportunity_id,)
        ).fetchone()
        assert row["is_canonical"] == 0
        assert row["canonical_id"] == first.opportunity_id

    def test_two_genuinely_different_roles_both_survive(self, store):
        ingest.ingest_one(store, posting(title="Business Operations Analyst"))
        outcome = ingest.ingest_one(
            store,
            posting(
                title="Strategy & Business Operations Analyst",
                source_url="https://a.example/jobs/2",
            ),
        )
        assert outcome.outcome == ingest.STORED
        canonical = store.execute(
            "SELECT count(*) c FROM opportunity WHERE is_canonical = 1"
        ).fetchone()["c"]
        assert canonical == 2


class TestBatch:
    def test_every_posting_is_accounted_for(self, store):
        report = ingest.ingest_many(
            store,
            [
                posting(title="Industrial Engineer"),
                posting(title="Industrial Engineer"),                      # sighting
                posting(title="Oops something happened",
                        source_url="https://a.example/jobs/3"),            # junk
                posting(title="Quality Manager", source_url="https://a.example/jobs/4"),
            ],
        )
        assert report.seen == 4
        assert (report.stored, report.sightings, report.rejected) == (2, 1, 1)

    def test_rejections_are_reported_for_a_human_to_scan(self, store):
        report = ingest.ingest_many(store, [posting(title="404")])
        assert len(report.rejections) == 1
        rule, title, _reason = report.rejections[0]
        assert rule.startswith("J01")
        assert title == "404"

    def test_arrival_order_decides_the_canonical_record(self, store):
        # The canonical record is the first one seen, so sorting the batch would
        # quietly rewrite which row the others point at.
        report = ingest.ingest_many(
            store,
            [
                posting(title="Industrial Engineer"),
                posting(title="Industrial Engineers", source_url="https://a.example/jobs/2"),
            ],
        )
        assert report.merged == 1
        canonical = store.execute(
            "SELECT title FROM opportunity WHERE is_canonical = 1"
        ).fetchone()
        assert canonical["title"] == "Industrial Engineer"
