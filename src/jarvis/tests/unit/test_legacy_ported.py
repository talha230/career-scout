"""T091 — the legacy phase-6 suite, run against the new code.

Inputs and expectations are copied verbatim from ``tests/test_phase6_email_and_interview.py``;
only the call sites are translated to the ``jarvis`` API. Where a legacy
expectation was *deliberately* changed by the plan, it is listed at the bottom
and not asserted, rather than being quietly edited here.
"""

from __future__ import annotations

import pytest

from jarvis import interview
from jarvis.gmail import classify


@pytest.mark.parametrize("subject,body,expected", [
    ("Interview invitation — Industrial Engineer",
     "We would like to invite you to an interview. Please book a time using calendly.com/x.",
     "interview_invitation"),
    ("Your application to Lucid Motors",
     "After careful review we have decided to proceed with other candidates. "
     "We wish you every success in your job search.",
     "rejection"),
    ("Offer of employment",
     "We are delighted to offer you the position. The compensation package is set out below.",
     "offer"),
    ("Next step: technical assessment",
     "Please complete the online assessment by Friday. The link is on HackerRank.",
     "assessment_request"),
    ("We received your application",
     "Thank you for your application. We are reviewing all applications and will be in touch.",
     "application_acknowledgement"),
    ("Documents needed",
     "Please send a copy of your passport and your degree certificate, plus reference details.",
     "document_request"),
    ("Visa sponsorship for your role",
     "We can confirm the visa sponsorship process and the relocation package for this position.",
     "visa_or_relocation"),
])
def test_classification_of_realistic_messages(subject, body, expected):
    result = classify.classify(subject, body)
    assert result.category == expected, f"scores were {result.scores}"
    assert result.stored != "unclassified"
    assert result.matched, "a classification must show what matched"


def test_ambiguous_message_is_unclassified_not_forced():
    result = classify.classify("Hello", "Just checking in about the thing we discussed.")
    assert result.stored == "unclassified"


def test_classification_is_deterministic():
    args = ("Interview invitation", "We would like to schedule a call with you.")
    first, second = classify.classify(*args), classify.classify(*args)
    assert (first.category, first.score) == (second.category, second.score)


def test_offer_outranks_acknowledgement_when_both_match():
    result = classify.classify(
        "Thank you for your application — and an offer",
        "Thank you for your application. We are pleased to offer you the position.",
    )
    assert result.category == "offer"


GAP_SKILLS = ["Lean Manufacturing", "Six Sigma"]


def test_empty_answer_scores_zero_rather_than_being_skipped():
    result = interview.score("", GAP_SKILLS)
    assert result["total"] == 0.0 and result["word_count"] == 0


def test_strong_answer_outscores_weak_answer():
    weak = "I have used lean before. It went well."
    strong = (
        "When I joined the toe-closing section at Interloop the line was running well below "
        "its rated output and the problem was changeover. I was asked to raise throughput "
        "without adding headcount, so my task was to find where the time was going. "
        "I ran a time study across 3 shifts, built a value stream map, and used Minitab to "
        "test which changeover steps actually drove the variation. I then standardised the "
        "method and trained the operators over 6 weeks using Lean Manufacturing principles "
        "and Six Sigma tooling. As a result changeover time fell and departmental productivity "
        "rose by 23 percent, which we tracked in Power BI against the previous quarter baseline."
    )
    weak_score = interview.score(weak, GAP_SKILLS)
    strong_score = interview.score(strong, GAP_SKILLS)
    assert strong_score["total"] > weak_score["total"] + 30
    assert strong_score["components"]["specificity"]["numbers"] > 0


def test_score_is_reproducible_from_its_components():
    result = interview.score("I ran a time study and productivity rose 12 percent "
                             "after I standardised the method over 4 weeks.", GAP_SKILLS)
    by_hand = sum(c["score"] * c["weight"] for c in result["components"].values())
    assert by_hand == pytest.approx(result["total"], abs=0.1)


# Deliberately not ported, each by a decision in PLAN.md:
#   test_send_scope_is_never_requested — T058 requests gmail.send when auto-send is opted in.
#   test_module_exposes_only_the_four_permitted_operations — the legacy Gmail client was
#       read-only by design; the new one sends through the approval gate (T056).
#   test_blocked_achievement_produces_no_star_story — blocked achievements are never
#       imported (profile/importer.py), so there is nothing to exclude later.
#   test_security_clearance_posting_is_rejected_with_named_rule (phase 3) — clearance moved
#       from a hard filter to an eligibility dimension (T037/T038).
