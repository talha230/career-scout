"""Role-family classification.

Measured against the 4,402-title legacy corpus: 10.1% unclassified, and
inspection of that remainder shows it is genuine junk — "Open Vacancies",
"CHECK BACK SOON", "Test", company names with no role in them. An earlier
pattern set left 51.8% unclassified, which is the mirror failure of the one
I-18 guards: over-blocking real work rather than admitting unread work.
"""

from __future__ import annotations

import pytest

from jarvis import config as config_module
from jarvis.discovery.role_family import UNCLASSIFIED, classify, known_keys, label_for

pytestmark = pytest.mark.usefixtures("jarvis_home")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Industrial Engineer", "industrial-engineering"),
        ("Manufacturing Engineer II", "industrial-engineering"),
        ("Supply Chain Planner", "supply-chain"),
        ("Procurement Specialist", "supply-chain"),
        ("Quality Engineer", "quality"),
        ("Technical Program Manager", "project-management"),
        ("Senior Data Analyst", "data-analytics"),
        ("Staff Software Engineer - Backend", "software-engineering"),
        ("Mechanical Design Engineer", "mechanical-engineering"),
        ("Controls Engineer", "electrical-engineering"),
        ("Civil Engineer", "civil-engineering"),
        ("Financial Analyst", "finance"),
        ("Enterprise Account Executive", "sales"),
        ("Postdoctoral Researcher", "research"),
        ("Technical Recruiter", "hr"),
        ("Senior Product Manager", "product"),
        ("Growth Marketing Lead", "marketing"),
        ("Security Engineer, Detection", "security"),
        ("Customer Support Specialist", "customer-support"),
        ("Corporate Counsel", "legal"),
        ("Registered Nurse", "healthcare"),
        ("Maintenance Technician", "skilled-trades"),
    ],
)
def test_real_titles_land_in_the_right_family(title: str, expected: str) -> None:
    assert classify(title).key == expected


def test_specific_beats_general() -> None:
    """Ordering is load-bearing: 'Quality Engineer' is quality, not generic engineering."""
    assert classify("Quality Engineer").key == "quality"
    assert classify("Data Engineer").key == "data-analytics"
    assert classify("Engineering Manager").key == "software-engineering"


def test_the_generic_fallbacks_catch_real_roles_naming_no_discipline() -> None:
    """Half the corpus was unclassified before these existed."""
    assert classify("Chief Engineer").key == "engineering-general"
    assert classify("Senior Manager").key == "general-professional"
    assert classify("Operations Director").key == "operations"


@pytest.mark.parametrize(
    "title",
    [
        "Oops something happened",
        "Open Vacancies",
        "CHECK BACK SOON",
        "Test",
        "Join Our Team",
        "A glimpse of the pool",
        "",
        "   ",
    ],
)
def test_junk_stays_unclassified(title: str) -> None:
    """These are verbatim from the real corpus. None of them is a job."""
    assert classify(title).key == UNCLASSIFIED


def test_classification_says_which_pattern_decided_it() -> None:
    """A verdict nobody can check is not deterministic in any useful sense."""
    result = classify("Senior Industrial Engineer")
    assert result.matched_pattern
    assert result.matched_text
    assert result.matched_text.lower() in "senior industrial engineer"


def test_unclassified_carries_no_pattern() -> None:
    result = classify("CHECK BACK SOON")
    assert result.matched_pattern is None
    assert result.is_classified is False


def test_only_the_title_is_used() -> None:
    """A description mentioning data science must not reclassify a warehouse role.

    The function takes no description at all, which is the enforcement.
    """
    assert classify("Warehouse Associate").key == "skilled-trades"


def test_classification_is_case_insensitive() -> None:
    assert classify("SENIOR INDUSTRIAL ENGINEER").key == "industrial-engineering"
    assert classify("senior industrial engineer").key == "industrial-engineering"


def test_family_keys_are_unique() -> None:
    """A duplicate key makes label_for ambiguous and the config unreadable."""
    families = config_module.load("role_families")["families"]
    keys = [f["key"] for f in families]
    assert len(keys) == len(set(keys)), [k for k in keys if keys.count(k) > 1]


def test_every_family_has_a_label_and_patterns() -> None:
    for family in config_module.load("role_families")["families"]:
        assert family["label"].strip()
        assert family["patterns"]


def test_known_keys_includes_unclassified() -> None:
    assert UNCLASSIFIED in known_keys()
    assert "industrial-engineering" in known_keys()


def test_label_for_falls_back_rather_than_raising() -> None:
    assert label_for("no-such-family") == "Unclassified"
    assert label_for("quality") == "Quality & Reliability"


def test_generic_fallbacks_are_last_in_the_config() -> None:
    """If they moved earlier they would swallow every specific family."""
    keys = [f["key"] for f in config_module.load("role_families")["families"]]
    assert keys[-2:] == ["engineering-general", "general-professional"]
