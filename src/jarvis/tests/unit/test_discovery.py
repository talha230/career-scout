"""Discovery: normalisation, work arrangement, authenticity, dedupe — T030a-c, T031a.

The strings asserted on are drawn from the real corpus in ``data/jobfinder.db``
wherever the corpus can answer the question, rather than invented. The cases that
matter most are the negative ones: the real jobs a careless rule would reject.
"""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.discovery import arrangement, authenticity, dedupe, normalize

# ----------------------------------------------------------------- normalise

class TestPay:
    """Pay is disclosed or it is NULL. Nothing in between gets invented."""

    def test_zero_is_not_a_salary(self):
        # RemoteOK returns 0 for "not stated". Writing it through as a salary of
        # zero is a fabricated figure that looks measured.
        info = normalize.pay_from_structured(0, 0)
        assert info.disclosed is False
        assert info.as_payload() is None

    def test_negative_is_not_a_salary(self):
        assert normalize.pay_from_structured(-5000, -1).disclosed is False

    def test_one_sided_range_is_still_disclosed(self):
        info = normalize.pay_from_structured(90000, None, "usd")
        assert info.disclosed is True
        assert (info.min_value, info.max_value) == (90000.0, None)
        assert info.currency == "USD"

    def test_min_above_max_is_swapped_and_said_so(self):
        info = normalize.pay_from_structured(120000, 90000)
        assert (info.min_value, info.max_value) == (90000.0, 120000.0)
        assert any("swapped" in note for note in info.notes)

    def test_missing_currency_is_noted_not_guessed(self):
        info = normalize.pay_from_structured(50000, 70000)
        assert info.currency is None
        assert any("no currency" in note for note in info.notes)

    def test_competitive_is_not_disclosed(self):
        info = normalize.pay_from_text("Salary: competitive, depending on experience.")
        assert info.disclosed is False
        assert any("declines" in note for note in info.notes)

    def test_year_range_is_not_a_salary(self):
        info = normalize.pay_from_text("Company history 2019 - 2024 of steady growth.")
        assert info.disclosed is False

    def test_experience_span_is_not_a_salary(self):
        assert normalize.pay_from_text("We want 10 - 15 years of experience.").disclosed is False

    def test_k_suffix_is_expanded(self):
        info = normalize.pay_from_text("Range is $90 - 120k per year")
        assert info.disclosed is True
        assert info.max_value == 120000.0

    def test_parsed_pay_is_labelled_lower_confidence(self):
        info = normalize.pay_from_text("We offer £45,000 - £55,000 per annum")
        assert info.source == "parsed_from_description"
        assert any("lower confidence" in note for note in info.notes)


class TestTitleAndCompany:
    def test_seniority_is_never_stripped(self):
        # The bug this prevents: these two merged into one posting when seniority
        # was treated as noise. They are different jobs with different pay.
        assert normalize.normalize_title("Staff Software Engineer") != normalize.normalize_title(
            "Senior Software Engineer"
        )

    def test_gender_markers_are_stripped(self):
        assert normalize.normalize_title("Projektmanager (m/w/d)") == "projektmanager"
        assert normalize.normalize_title("Engineer (all genders)") == "engineer"

    def test_work_mode_is_not_part_of_the_title(self):
        assert normalize.normalize_title("Data Analyst - Remote") == "data analyst"

    def test_legal_suffixes_do_not_distinguish_employers(self):
        assert normalize.normalize_company("Flexport, Inc.") == normalize.normalize_company(
            "Flexport"
        )
        assert normalize.normalize_company("Siemens AG") == "siemens"

    def test_greenhouse_double_escaping_is_unwound(self):
        # A single unescape leaves &lt;p&gt; in the text and every later keyword
        # match silently fails on it.
        assert normalize.strip_html("&amp;lt;p&amp;gt;Build things&amp;lt;/p&amp;gt;") == (
            "Build things"
        )


class TestLocation:
    def test_country_is_matched_from_an_alias(self):
        resolved = normalize.resolve_location("Dubai, UAE")
        assert resolved.country_code == "AE"
        assert resolved.resolution == "matched_alias"

    def test_country_is_inferred_from_a_city(self):
        resolved = normalize.resolve_location("Munich")
        assert resolved.country_code == "DE"
        assert resolved.resolution == "matched_city"

    def test_an_unmatched_location_is_never_guessed(self):
        resolved = normalize.resolve_location("Somewhere lovely")
        assert resolved.country_code is None
        assert resolved.resolution == "unresolved"

    def test_word_boundaries_stop_a_substring_match(self):
        # "oman" must not fire inside "romania".
        assert normalize.resolve_location("Bucharest, Romania").country_code != "OM"

    def test_worldwide_remote_is_scoped(self):
        resolved = normalize.resolve_location("Remote - anywhere in the world")
        assert resolved.remote_status == "remote"
        assert resolved.remote_scope == "worldwide"

    def test_a_resolved_country_with_no_remote_signal_is_onsite(self):
        assert normalize.resolve_location("Berlin, Germany").remote_status == "onsite"


class TestVisaSignal:
    def test_refusal_beats_an_incidental_mention(self):
        offered, evidence = normalize.detect_visa_signal(
            "We cannot sponsor visas for this role, though visa sponsorship is common here."
        )
        assert offered is False
        assert "Explicitly excludes" in evidence

    def test_sponsorship_is_detected_with_its_quote(self):
        offered, evidence = normalize.detect_visa_signal("Visa sponsorship is available.")
        assert offered is True
        assert "sponsorship" in evidence.lower()

    def test_silence_is_not_a_refusal(self):
        assert normalize.detect_visa_signal("We build warehouses.") == (False, None)


class TestUrlAndKey:
    def test_campaign_tracking_is_stripped(self):
        assert normalize.canonical_url(
            "https://boards.greenhouse.io/acme/jobs/123?utm_source=x&gh_src=abc"
        ) == normalize.canonical_url("https://boards.greenhouse.io/acme/jobs/123")

    def test_the_job_identifier_survives(self):
        # Greenhouse puts the job id in gh_jid. Dropping the whole query string
        # collapses an employer's entire board to one URL.
        first = normalize.canonical_url("https://x.io/embed/job_app?for=acme&gh_jid=111")
        second = normalize.canonical_url("https://x.io/embed/job_app?for=acme&gh_jid=222")
        assert first != second

    def test_the_dedupe_key_is_stable_across_cosmetic_difference(self):
        assert normalize.dedupe_key("Flexport, Inc.", "Senior Analyst (m/w/d)", "US", "Austin") == (
            normalize.dedupe_key("Flexport", "Senior Analyst", "us", "austin")
        )

    def test_seniority_changes_the_dedupe_key(self):
        assert normalize.dedupe_key("Acme", "Staff Engineer", "US", "Austin") != (
            normalize.dedupe_key("Acme", "Senior Engineer", "US", "Austin")
        )


# --------------------------------------------------------------- arrangement

class TestArrangement:
    def test_structured_field_wins(self):
        result = arrangement.classify("Analyst", employment_types=["Freelance"])
        assert result.kind == arrangement.PROJECT
        assert result.signal == "structured_field"

    def test_freelance_in_the_title_is_a_project(self):
        # 'Recruiter freelance' and 'Freelance Recruiter:in' are both real corpus rows.
        assert arrangement.classify("Recruiter freelance").kind == arrangement.PROJECT

    def test_contract_manufacturing_is_not_freelance(self):
        # THE false positive this module exists to avoid. 'Contract' is ordinary
        # vocabulary in supply chain and manufacturing, which is this user's field.
        result = arrangement.classify(
            "Supply Chain Manager",
            description=(
                "You will own contract negotiation with our contract manufacturing "
                "partners and manage suppliers under contract."
            ),
        )
        assert result.kind != arrangement.PROJECT

    def test_a_named_engagement_phrase_is_freelance(self):
        result = arrangement.classify(
            "Data Engineer", description="This role is offered on a contract basis."
        )
        assert result.kind == arrangement.PROJECT

    def test_remote_is_detected_from_the_location_field(self):
        assert arrangement.classify("Engineer", location_raw="Remote").kind == arrangement.REMOTE

    def test_remote_is_detected_from_a_board_tag(self):
        assert arrangement.classify("Engineer", tags=["remote"]).kind == arrangement.REMOTE

    def test_an_explicit_refusal_beats_the_word_remote(self):
        result = arrangement.classify(
            "Engineer", description="This position is not remote; you must be in the office."
        )
        assert result.kind == arrangement.ONSITE

    def test_project_beats_remote(self):
        # How someone is engaged and paid outranks where they sit.
        result = arrangement.classify("Freelance Designer", location_raw="Remote")
        assert result.kind == arrangement.PROJECT

    def test_hybrid_resolves_to_onsite_and_keeps_the_word(self):
        result = arrangement.classify("Analyst", description="Hybrid, 3 days a week in office.")
        assert result.kind == arrangement.ONSITE
        assert "hybrid" in result.evidence.lower()

    def test_a_published_place_infers_onsite_and_names_it(self):
        result = arrangement.classify(
            "Mechanical Engineer", description="You will design things.",
            resolved_place="Berlin, DE",
        )
        assert result.kind == arrangement.ONSITE
        assert result.signal == "location"
        assert "Berlin, DE" in result.evidence

    def test_a_published_place_never_overrides_a_stated_arrangement(self):
        # The inference is the last tier. An employer that says "remote" while
        # listing a head office is offering a remote job.
        result = arrangement.classify(
            "Engineer", description="This is a fully remote role.", resolved_place="Berlin, DE"
        )
        assert result.kind == arrangement.REMOTE

        result = arrangement.classify(
            "Freelance Engineer", resolved_place="Berlin, DE"
        )
        assert result.kind == arrangement.PROJECT

    def test_an_unreadable_arrangement_stays_null(self):
        # Never defaults to onsite: onsite drives relocation and cost-of-living
        # arithmetic, so guessing it is the most expensive way to be wrong.
        result = arrangement.classify("Mechanical Engineer", description="You will design things.")
        assert result.kind is None
        assert result.is_known is False

    def test_every_returned_kind_is_storable(self):
        for text in ("Freelance Dev", "Remote Dev", "Onsite Dev", "Dev"):
            result = arrangement.classify(text)
            assert result.kind in (*arrangement.VALUES, None)


# -------------------------------------------------------------- authenticity

class TestJunk:
    @pytest.mark.parametrize(
        "title",
        ["Oops something happened", "CHECK BACK SOON", "404", "Test", "test Copy", "12650000"],
    )
    def test_real_corpus_junk_is_rejected(self, title):
        verdict = authenticity.check_junk(title)
        assert verdict.rejected is True
        assert verdict.kind == authenticity.JUNK

    @pytest.mark.parametrize(
        "title",
        ["SDR", "Sales", "Recruiter", "Porter", "barista", "Labourers", "QA Lead", "Accountant"],
    )
    def test_real_short_titles_are_not_junk(self, title):
        # Measured: these are all genuine vacancies in the corpus. A minimum-length
        # rule would have rejected every one, which is why there is no such rule.
        assert authenticity.check_junk(title).rejected is False

    def test_junk_matching_is_anchored_not_substring(self):
        assert authenticity.check_junk("Room 404 Facilities Technician").rejected is False
        assert authenticity.check_junk("Test Engineer").rejected is False
        assert authenticity.check_junk("Demolition Supervisor").rejected is False

    def test_a_catch_all_is_junk_under_its_own_rule(self):
        verdict = authenticity.check_junk("Open Vacancies")
        assert verdict.rejected is True
        assert verdict.rule == "J02_not_a_specific_vacancy"

    def test_junk_quotes_the_title_it_rejected(self):
        assert "Oops" in authenticity.check_junk("Oops something happened").evidence

    def test_an_absent_title_is_left_to_the_hard_filter(self):
        # F10 already rejects a posting with no title. Two rules rejecting one row
        # hides which one matters.
        assert authenticity.check_junk("").rejected is False
        assert authenticity.check_junk(None).rejected is False


class TestFraud:
    @pytest.mark.parametrize(
        "description",
        [
            "We bring voice, SMS, WhatsApp and AI together into one workspace.",
            "Direktansprache von Kandidaten per Telefon, WhatsApp und über Portale.",
            "Work on launching SMS and potentially WhatsApp within SFMC.",
        ],
    )
    def test_a_bare_messaging_mention_is_not_fraud(self, description):
        # MEASURED: a bare 'whatsapp' rule flagged 8 of 8 legitimate corpus postings
        # (Aircall, Superchat, Careem, Airbnb, and a recruiter's outreach channels).
        assert authenticity.check_fraud(description).rejected is False

    @pytest.mark.parametrize(
        ("employer", "description"),
        [
            # Every one of these is a REAL vacancy in data/jobfinder.db that an
            # earlier version of these rules rejected. The rules matched the noun
            # ('placement fee', 'gift card', 'crypto payment') when what makes a
            # scam is the applicant being asked to pay. 8 false positives, 0 true
            # positives, on a corpus of 4,402.
            ("Samsara", "want their hard work to mean something beyond a placement fee "
                        "or a quarterly target"),
            ("Spotify", "Own the end-to-end payments forecast including processing fees, "
                        "vendor mix, and refunds"),
            ("Stripe", "Enterprise features on-reader forms, tipping, gift cards; "
                       "Payment features DCC, surcharge, multi-capture"),
            ("Stripe", "deep understanding of stablecoin protocols, blockchain networks, "
                       "and crypto payment solutions"),
            ("Airbnb", "Lead platform strategy for gift card capabilities as a core part "
                       "of the Loyalty & Incentives domain"),
        ],
    )
    def test_payments_companies_are_not_scams(self, employer, description):
        verdict = authenticity.check_fraud(description)
        assert verdict.rejected is False, f"{employer} rejected on: {verdict.evidence}"

    def test_a_demanded_fee_rejects_on_its_own(self):
        verdict = authenticity.check_fraud(
            "A refundable registration fee of $50 is required before onboarding."
        )
        assert verdict.rejected is True
        assert verdict.kind == authenticity.FRAUD

    def test_identity_documents_up_front_reject(self):
        verdict = authenticity.check_fraud("Send your passport copy with your application.")
        assert verdict.rejected is True

    def test_two_medium_signals_reject_together(self):
        verdict = authenticity.check_fraud(
            "Apply via WhatsApp. No experience needed, earn $900 per week. "
            "Guaranteed income from day one."
        )
        assert verdict.rejected is True
        assert len(verdict.hits) >= 2

    def test_one_weak_signal_flags_without_rejecting(self):
        verdict = authenticity.check_fraud("Send your CV to hiring@gmail.com")
        assert verdict.rejected is False
        assert verdict.flagged is True
        assert verdict.score < verdict.threshold

    def test_a_signal_fires_once_however_often_it_appears(self):
        verdict = authenticity.check_fraud(
            "You must pay a registration fee. Candidates must pay a processing fee. "
            "An onboarding fee of $40 is required to apply."
        )
        assert len([hit for hit in verdict.hits if hit.id == "S01_applicant_must_pay"]) == 1

    def test_clean_text_produces_no_hits(self):
        verdict = authenticity.check_fraud(
            "You will lead process improvement across three production lines."
        )
        assert verdict.hits == []
        assert verdict.rejected is False

    @pytest.mark.parametrize(
        ("employer", "description"),
        [
            # From the first live discovery run, 2026-09-24. Airbnb's own anti-scam
            # notice rejected 155 of its 160 postings before the negation guard.
            ("Airbnb", "Our recruiters will never ask for your Social Security number, "
                       "bank account details, passport, or payment app information "
                       "while you are interviewing. We'll also never ask you to pay a "
                       "fee, send money, deposit or cash a check, or purchase "
                       "work-related equipment during the interview process."),
            # The employer declining to pay recruiters: negated, and not the applicant.
            ("ABS", "ABS and Affiliated Companies (ABS) will not pay a fee to any "
                    "third-party agency without a valid ABS Master Service Agreement."),
        ],
    )
    def test_an_anti_scam_notice_is_not_a_scam(self, employer, description):
        verdict = authenticity.check_fraud(description)
        assert verdict.rejected is False, f"{employer} rejected on: {verdict.evidence}"

    def test_a_reassurance_in_another_clause_does_not_excuse_a_demand(self):
        """The guard is clause-bound, or it becomes the scam's opening line."""
        verdict = authenticity.check_fraud(
            "Don't worry, you must pay a registration fee of $50 before your start date."
        )
        assert verdict.rejected is True

    def test_a_demand_after_an_anti_scam_notice_still_rejects(self):
        """Skipping the notice must not skip a real demand further down."""
        verdict = authenticity.check_fraud(
            "We will never ask you to pay a fee during interviews. "
            "Selected candidates must pay a training fee of $120 to begin."
        )
        assert verdict.rejected is True
        # The evidence is the demand in the second sentence, not the notice.
        assert "candidates must pay" in verdict.evidence


class TestCheck:
    def test_junk_short_circuits_before_fraud(self):
        # There is no posting there to be fraudulent, and two reasons for one row
        # obscures which one a reviewer should act on.
        verdict = authenticity.check("404", "Pay a registration fee to apply.")
        assert verdict.kind == authenticity.JUNK

    def test_a_real_posting_passes(self):
        verdict = authenticity.check(
            "Industrial Engineer", "You will improve throughput on the assembly line."
        )
        assert verdict.rejected is False


# -------------------------------------------------------------------- dedupe

@pytest.fixture
def store(memory_db: sqlite3.Connection) -> sqlite3.Connection:
    """A migrated database with the countries these tests reference.

    ``opportunity.country_iso2`` is a foreign key, so a country has to exist
    before a posting can claim it — which is the point of the constraint.
    """
    memory_db.executemany(
        "INSERT INTO country (iso2, name, enabled) VALUES (?, ?, 1)",
        [("US", "United States"), ("DE", "Germany")],
    )
    return memory_db


def _insert(conn: sqlite3.Connection, **overrides) -> dict:
    """Insert a minimal opportunity and return the row as a dict."""
    row = {
        "id": "opp-1",
        "kind": "job",
        "title": "Industrial Engineer",
        "employer": "Acme",
        "country_iso2": "US",
        "city": "Austin",
        "work_arrangement": "onsite",
        "source_url": "https://acme.example/jobs/1",
        "url_canonical": "acme.example/jobs/1",
        "fetched_at": "2026-09-01T00:00:00Z",
        "first_seen_at": "2026-09-01T00:00:00Z",
        "last_seen_at": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    row.setdefault("employer_norm", normalize.normalize_company(row["employer"]))
    row.setdefault("title_norm", normalize.normalize_title(row["title"]))
    row.setdefault(
        "dedupe_key",
        normalize.dedupe_key(row["employer"], row["title"], row["country_iso2"], row["city"]),
    )
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO opportunity ({columns}) VALUES ({placeholders})", tuple(row.values())
    )
    return row


def _candidate(**overrides) -> dict:
    row = {
        "id": None,
        "title": "Industrial Engineer",
        "employer": "Acme",
        "country_iso2": "US",
        "city": "Austin",
        "work_arrangement": "onsite",
        "url_canonical": "acme.example/jobs/1",
    }
    row.update(overrides)
    row.setdefault("employer_norm", normalize.normalize_company(row["employer"]))
    row.setdefault("title_norm", normalize.normalize_title(row["title"]))
    row.setdefault(
        "dedupe_key",
        normalize.dedupe_key(row["employer"], row["title"], row["country_iso2"], row["city"]),
    )
    return row


class TestDedupe:
    def test_an_identical_key_is_the_same_vacancy(self, store):
        _insert(store)
        match = dedupe.find_duplicate(store, _candidate())
        assert match is not None
        assert match.rule == dedupe.IDENTICAL_KEY

    def test_the_same_url_from_another_board_is_the_same_vacancy(self, store):
        _insert(store)
        match = dedupe.find_duplicate(
            store, _candidate(title="Industrial Engineer II", employer="Acme Corp")
        )
        assert match is not None
        assert match.rule == dedupe.IDENTICAL_URL

    def test_a_near_identical_title_at_one_employer_merges(self, store):
        _insert(store, title="Industrial Engineer")
        match = dedupe.find_duplicate(
            store, _candidate(title="Industrial Engineers", url_canonical="other.example/9")
        )
        assert match is not None
        assert match.rule == dedupe.SIMILARITY
        assert match.similarity >= 0.93

    def test_the_threshold_keeps_genuinely_different_roles_apart(self, store):
        # MEASURED: at 0.88 these merged. They are different jobs.
        _insert(store, title="Business Operations Analyst")
        match = dedupe.find_duplicate(
            store,
            _candidate(
                title="Strategy & Business Operations Analyst",
                url_canonical="other.example/9",
            ),
        )
        assert match is None

    def test_two_unresolved_countries_are_not_the_same_place(self, store):
        # 715 corpus postings have no country at all. Treating two unknowns as
        # equal would merge them wholesale. Exercised on the fuzzy rule, which is
        # the only one that consults place — an identical key is identical
        # regardless, because the key already includes the (empty) country.
        _insert(store, title="Industrial Engineer", country_iso2=None, city=None,
                work_arrangement="onsite")
        match = dedupe.find_duplicate(
            store,
            _candidate(
                title="Industrial Engineers", country_iso2=None, city=None,
                work_arrangement="onsite", url_canonical="other.example/9",
            ),
        )
        assert match is None

    def test_two_remote_postings_with_no_country_are_the_same_place(self, store):
        _insert(store, title="Industrial Engineer", country_iso2=None, city=None,
                work_arrangement="remote")
        match = dedupe.find_duplicate(
            store,
            _candidate(
                title="Industrial Engineers", country_iso2=None, city=None,
                work_arrangement="remote", url_canonical="other.example/9",
            ),
        )
        assert match is not None
        assert match.rule == dedupe.SIMILARITY

    def test_different_countries_never_merge(self, store):
        _insert(store, country_iso2="US")
        match = dedupe.find_duplicate(
            store, _candidate(country_iso2="DE", url_canonical="other.example/9")
        )
        assert match is None

    def test_a_different_employer_is_never_the_same_job(self, store):
        _insert(store, employer="Acme")
        match = dedupe.find_duplicate(
            store, _candidate(employer="Globex", url_canonical="other.example/9")
        )
        assert match is None

    def test_a_posting_is_not_compared_against_itself(self, store):
        row = _insert(store)
        assert dedupe.find_duplicate(store, row) is None

    def test_recording_a_merge_marks_it_and_writes_the_evidence(self, store):
        _insert(store, id="opp-1")
        _insert(store, id="opp-2", title="Industrial Engineers",
                url_canonical="acme.example/jobs/2")
        match = dedupe.DuplicateMatch("opp-1", "opp-2", dedupe.SIMILARITY, 0.97, "because")
        dedupe.record_duplicate(store, match, run_id="run-7")

        row = store.execute("SELECT * FROM opportunity WHERE id = 'opp-2'").fetchone()
        assert row["is_canonical"] == 0
        assert row["canonical_id"] == "opp-1"

        evidence = store.execute("SELECT * FROM opportunity_duplicate").fetchone()
        assert evidence["rule"] == dedupe.SIMILARITY
        assert evidence["evidence"] == "because"
        assert evidence["run_id"] == "run-7"
        assert evidence["config_version"] == "v1"

    def test_a_merge_is_never_a_delete(self, store):
        _insert(store, id="opp-1", title="Industrial Engineer")
        _insert(store, id="opp-2", title="Industrial Engineers",
                url_canonical="acme.example/jobs/2")
        dedupe.record_duplicate(
            store, dedupe.DuplicateMatch("opp-1", "opp-2", dedupe.IDENTICAL_URL, 1.0, "e")
        )
        assert store.execute("SELECT count(*) c FROM opportunity").fetchone()["c"] == 2

    def test_recording_a_merge_twice_is_harmless(self, store):
        _insert(store, id="opp-1", title="Industrial Engineer")
        _insert(store, id="opp-2", title="Industrial Engineers",
                url_canonical="acme.example/jobs/2")
        match = dedupe.DuplicateMatch("opp-1", "opp-2", dedupe.IDENTICAL_URL, 1.0, "e")
        dedupe.record_duplicate(store, match)
        dedupe.record_duplicate(store, match)
        assert store.execute(
            "SELECT count(*) c FROM opportunity_duplicate"
        ).fetchone()["c"] == 1

    def test_a_merge_without_a_row_is_refused(self, store):
        with pytest.raises(ValueError):
            dedupe.record_duplicate(
                store, dedupe.DuplicateMatch("opp-1", None, dedupe.IDENTICAL_URL, 1.0, "e")
            )

    def test_an_additional_source_is_a_sighting_not_a_new_row(self, store):
        _insert(store, id="opp-1", source_id=None)
        store.execute(
            "INSERT INTO source (id, name, provider, base_url, access_mode, legal_basis) "
            "VALUES ('src-2', 'Board', 'greenhouse', 'https://b.example', 'api', 'public API')"
        )
        dedupe.record_additional_source(
            store, "opp-1", source_id="src-2",
            source_url="https://b.example/1", fetched_at="2026-09-05T00:00:00Z",
        )
        assert store.execute("SELECT count(*) c FROM opportunity").fetchone()["c"] == 1
        row = store.execute("SELECT * FROM opportunity WHERE id='opp-1'").fetchone()
        assert row["last_seen_at"] == "2026-09-05T00:00:00Z"
        assert row["first_seen_at"] == "2026-09-01T00:00:00Z"   # never moves

    def test_the_canonical_chain_is_followed(self, store):
        _insert(store, id="opp-1", title="Industrial Engineer")
        _insert(store, id="opp-2", title="Industrial Engineers", url_canonical="a/2")
        _insert(store, id="opp-3", title="Industrial Engineering", url_canonical="a/3")
        dedupe.record_duplicate(
            store, dedupe.DuplicateMatch("opp-1", "opp-2", dedupe.IDENTICAL_URL, 1.0, "e")
        )
        dedupe.record_duplicate(
            store, dedupe.DuplicateMatch("opp-2", "opp-3", dedupe.IDENTICAL_URL, 1.0, "e")
        )
        assert dedupe.canonical_for(store, "opp-3") == "opp-1"

    def test_a_cycle_terminates(self, store):
        _insert(store, id="opp-1", title="Industrial Engineer")
        _insert(store, id="opp-2", title="Industrial Engineers", url_canonical="a/2")
        store.execute(
            "UPDATE opportunity SET is_canonical=0, canonical_id='opp-2' WHERE id='opp-1'"
        )
        store.execute(
            "UPDATE opportunity SET is_canonical=0, canonical_id='opp-1' WHERE id='opp-2'"
        )
        assert dedupe.canonical_for(store, "opp-1") in {"opp-1", "opp-2"}
