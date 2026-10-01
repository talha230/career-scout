"""Per-account CV presentation variants — T043a, T043b.

The tests that matter are not "does it produce a font". They are the three
properties that make this safe: it is deterministic, it varies only presentation,
and every variant shows the same sections.
"""

from __future__ import annotations

import pytest

from career_scout import config as config_module
from career_scout.documents import style_variant
from career_scout.store import settings as settings_module


class TestDeterminism:
    def test_the_same_account_always_gets_the_same_variant(self):
        # A CV that reshuffles itself on every render is worse than a shared
        # template: the same person's second application to one employer must
        # look like the same person wrote it.
        first = style_variant.derive("account-abc")
        second = style_variant.derive("account-abc")
        assert first.as_payload() == second.as_payload()

    def test_different_accounts_get_different_variants(self):
        variants = {
            style_variant.describe(style_variant.derive(f"account-{i}")) for i in range(40)
        }
        # Not all 40 need be unique, but a space this size collapsing to a handful
        # would mean the dimensions are moving together.
        assert len(variants) > 25

    def test_dimensions_do_not_move_together(self):
        # Without mixing the dimension name into the digest, every dimension with
        # the same option count picks the same index, and the variety collapses.
        seen = {
            (v.body_size_pt, v.line_spacing, v.section_gap_pt)
            for v in (style_variant.derive(f"acct-{i}") for i in range(60))
        }
        assert len(seen) > 8

    def test_an_empty_account_id_is_refused(self):
        with pytest.raises(ValueError):
            style_variant.derive("")

    def test_the_account_id_is_not_carried_into_the_rendered_style(self):
        variant = style_variant.derive("secret-account-id")
        rendered = style_variant.describe(variant)
        assert "secret-account-id" not in rendered

    def test_the_description_survives_a_cp1252_console(self):
        # This line is printed by `career-scout doctor`. A Windows console at the
        # default code page raises UnicodeEncodeError on an arrow or an ellipsis,
        # which turns a status line into a crash.
        for i in range(20):
            style_variant.describe(style_variant.derive(f"acct-{i}")).encode("cp1252")


class TestPresentationOnly:
    """Every dimension is how it looks. None of them is what it says."""

    CONTENT_WORDS = {
        "summary_text", "claims", "achievements", "highlights", "wording",
        "sentences", "skills_listed", "experience", "employer", "figures",
    }

    def test_no_dimension_names_content(self):
        style = config_module.load("cv_style")
        assert not self.CONTENT_WORDS & set(style["dimensions"])

    def test_every_permitted_order_shows_the_same_sections(self):
        # The variant decides the ORDER. What appears is decided by the profile
        # and the posting, never by the style.
        orders = config_module.load("cv_style")["section_orders"]["orders"]
        memberships = {frozenset(order) for order in orders}
        assert len(memberships) == 1

    def test_the_header_and_summary_are_never_buried(self):
        orders = config_module.load("cv_style")["section_orders"]["orders"]
        for order in orders:
            assert order[0] == "header"
            assert order[1] == "summary"

    def test_every_variant_keeps_the_same_sections(self):
        baseline = set(style_variant.derive("acct-0").section_order)
        for i in range(30):
            assert set(style_variant.derive(f"acct-{i}").section_order) == baseline


class TestAtsSafety:
    def test_only_core_fonts_are_offered(self):
        # A font the reader does not have is substituted silently, and the
        # careful spacing becomes meaningless.
        allowed = {
            "Calibri", "Cambria", "Georgia", "Arial", "Helvetica", "Garamond",
            "Times New Roman", "Verdana", "Book Antiqua", "Tahoma",
        }
        for entry in config_module.load("cv_style")["dimensions"]["typeface"]:
            assert {entry["body"], entry["heading"]} <= allowed

    def test_bullet_glyphs_survive_extraction_cleanly(self):
        # Checkmarks, arrows and emoji survive extraction as literal characters
        # and end up inside the parsed text of a responsibility.
        for glyph in config_module.load("cv_style")["dimensions"]["bullet_glyph"]:
            assert glyph in {"•", "–", "▪"}

    def test_body_size_stays_readable_and_compact(self):
        for size in config_module.load("cv_style")["dimensions"]["body_size_pt"]:
            assert 10 <= size <= 11

    def test_the_variant_space_is_large_enough_to_not_collide(self):
        assert style_variant.distinct_variant_count() > 10_000


class TestJustification:
    def test_body_prose_is_justified(self):
        assert style_variant.derive("acct-1").justification.body_paragraphs == "justify"

    def test_bullets_and_headings_stay_left_aligned(self):
        # Justifying a short line stretches its word spacing into visible rivers
        # of whitespace, which reads worse, not better.
        justification = style_variant.derive("acct-1").justification
        assert justification.bullets == "left"
        assert justification.headings == "left"

    def test_hyphenation_is_off(self):
        # A hyphenated break inside a surname or a tool name is a parsing hazard
        # for the sake of a tidier right edge.
        assert style_variant.derive("acct-1").justification.hyphenation is False

    def test_justification_does_not_vary_by_account(self):
        # It is a correctness decision about readability, not a style dimension.
        alignments = {
            style_variant.derive(f"acct-{i}").justification.body_paragraphs for i in range(20)
        }
        assert alignments == {"justify"}


class TestStyleYieldsToSubstance:
    """The one dimension that can lose information does not get to."""

    def _year_only_variant(self):
        for i in range(200):
            variant = style_variant.derive(f"acct-{i}")
            if variant.date_format == "yyyy_only":
                return variant
        pytest.skip("no account in the sample drew the yyyy_only format")

    def test_year_only_dates_are_dropped_when_months_are_known(self):
        variant = self._year_only_variant()
        assert variant.effective_date_format(has_month_precision=True) == "mon_yyyy"

    def test_year_only_dates_are_kept_when_months_are_not_known(self):
        variant = self._year_only_variant()
        assert variant.effective_date_format(has_month_precision=False) == "yyyy_only"

    def test_other_formats_are_untouched(self):
        for i in range(40):
            variant = style_variant.derive(f"acct-{i}")
            if variant.date_format != "yyyy_only":
                assert variant.effective_date_format(has_month_precision=True) == (
                    variant.date_format
                )


class TestPersistence:
    def test_the_variant_is_stored_on_first_use(self, db):
        variant = style_variant.resolve(db, "account-xyz")
        stored = settings_module.get(db, style_variant.SETTING_KEY)
        assert stored["account_id"] == "account-xyz"
        assert stored["typeface"] == variant.typeface

    def test_a_stored_variant_is_returned_unchanged(self, db):
        first = style_variant.resolve(db, "account-xyz")
        second = style_variant.resolve(db, "account-xyz")
        assert first.as_payload() == second.as_payload()

    def test_a_stored_variant_survives_a_config_change(self, db):
        # The point of storing rather than re-deriving: a CV whose typeface moved
        # because a default was retuned is the inconsistency this prevents.
        stored = style_variant.resolve(db, "account-xyz").as_payload()
        stored["typeface"] = {"body": "Georgia", "heading": "Georgia"}
        settings_module.set_value(db, style_variant.SETTING_KEY, stored)

        assert style_variant.resolve(db, "account-xyz").body_font == "Georgia"

    def test_a_different_account_supersedes_the_stored_variant(self, db):
        style_variant.resolve(db, "account-one")
        variant = style_variant.resolve(db, "account-two")
        assert variant.account_id == "account-two"
        assert variant.as_payload() == style_variant.derive("account-two").as_payload()
