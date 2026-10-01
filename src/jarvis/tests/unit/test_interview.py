"""Interview practice and the STAR bank — T090."""

from __future__ import annotations

from datetime import date

from jarvis import interview, matching
from jarvis.profile import records
from jarvis.tests.invariants.test_documents import _opportunity, _profile


def test_a_story_never_invents_its_situation_or_task(db) -> None:
    _profile(db)
    story = interview.star_bank(db)[0]
    assert story["action"]["status"] == "sourced"
    assert story["situation"]["status"] == "needs_your_input" and story["situation"]["text"] is None
    assert story["task"]["status"] == "needs_your_input"
    # "removed 3 waiting steps" carries a number, so the result is on record.
    assert story["result"]["status"] == "sourced"
    assert story["ready"] is False and set(story["gaps"]) == {"situation", "task"}


def test_an_unconfirmed_achievement_makes_no_story(db) -> None:
    records.add(db, "x_achievements[0].statement", "Saved millions", confirmed=False)
    assert interview.star_bank(db) == []


def test_the_users_own_answers_complete_a_story(db) -> None:
    _profile(db)
    record_id = interview.star_bank(db)[0]["record_id"]
    story = interview.save_star_answer(db, record_id, situation="Changeovers took an hour.",
                                       task="I was asked to halve them.")
    assert story["ready"] and story["situation"]["source"] == "your answer"
    # Saving again updates in place rather than stacking rows.
    interview.save_star_answer(db, record_id, situation="Changeovers took 70 minutes.",
                               task="Halve them.")
    assert db.execute("SELECT count(*) c FROM worked_example").fetchone()["c"] == 1


def test_questions_come_from_the_posting_and_state_nothing_about_the_user(db) -> None:
    _profile(db)
    records.add(db, "basics.location.countryCode", "PK", confirmed=True)
    opportunity_id = _opportunity(db)
    matching.store_assessment(db, matching.screen(db, opportunity_id,
                                                  as_of=date(2026, 9, 24)).assessment)
    qs = interview.questions(db, opportunity_id)
    ids = {q["id"] for q in qs}
    assert "motivation" in ids and "mobility" in ids            # US posting, PK resident
    text = " ".join(q["text"] + q["why"] for q in qs)
    assert "passport" not in text.lower() and "pakistan" not in text.lower()


def test_scoring_reads_only_the_typed_answer_and_shows_its_arithmetic() -> None:
    answer = ("When I joined, the line was losing 18% of output to changeovers. I was asked to "
              "cut that. I ran a SMED study with the team over 6 weeks and standardised the "
              "setup. As a result changeover time went from 60 to 25 minutes. " * 2)
    result = interview.score(answer, ["SMED", "lean manufacturing"])
    detected = result["components"]["structure"]["detected"]
    assert detected == ["situation", "task", "action", "result"]
    assert result["components"]["specificity"]["numbers"] >= 3
    recomputed = sum(c["score"] * c["weight"] for c in result["components"].values())
    assert abs(recomputed - result["total"]) < 0.05
    assert interview.score("", ["SMED"])["total"] == 0.0


def test_the_rubric_lives_in_config() -> None:
    from jarvis import config as config_module

    rubric = config_module.load("interview")["rubric"]
    assert abs(sum(r["weight"] for r in rubric.values()) - 1.0) < 1e-9
