"""A per-account presentation variant for generated CVs — T043a, T043b.

Two people using Jarvis should not send out two documents that are visibly the
same template. A recruiter who recognises the layout twice has learned something
about the tool, not about the applicants.

Three properties hold, and the first is the one that matters:

**Presentation only.** Every dimension is typography or spacing. None of them
changes a word, a claim, a number, which experience is shown, or which sections
appear. A variant that changed what the document *said* would be fabrication
wearing a typeface, and the claim trace and QC gate run identically for all of
them. The permitted section orderings all contain exactly the same sections.

**Deterministic, not random.** The variant is derived from the account id by
hash. The same person's second application to the same employer has to look like
the same person wrote it; a CV that reshuffles itself on every render is worse
than a shared template, not better. Nothing here calls :mod:`random`.

**Every variant is ATS-safe.** The variation space is restricted to properties a
text extractor cannot see. Single column, standard headings, and nothing in
headers, footers, text boxes, layout tables or images are constant across all of
them, because those are what make the document parseable at all.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from jarvis import config as config_module
from jarvis.store import settings as settings_module

#: Where the resolved variant is stored, so it survives a config reshuffle.
SETTING_KEY = "cv_style_variant"


@lru_cache(maxsize=8)
def _style(version: str) -> dict[str, Any]:
    return config_module.load("cv_style", version)


def _pick(options: list, seed: bytes, dimension: str):
    """Choose one option for a dimension, deterministically.

    The dimension name is mixed into the digest so two dimensions with the same
    number of options do not move together — otherwise every account with a
    3-option body size also gets the matching 3-option line spacing, and the
    variants collapse into far fewer distinct looks than the space allows.
    """
    digest = hashlib.sha256(seed + dimension.encode("utf-8")).digest()
    return options[int.from_bytes(digest[:8], "big") % len(options)]


@dataclass(frozen=True, slots=True)
class Justification:
    """How each kind of line is aligned — T043a."""

    body_paragraphs: str = "justify"
    bullets: str = "left"
    headings: str = "left"
    hyphenation: bool = False


@dataclass(frozen=True, slots=True)
class StyleVariant:
    """One account's resolved presentation. Everything here is how it looks."""

    account_id: str
    config_version: str

    typeface: dict[str, str]
    body_size_pt: float
    heading_size_pt: float
    name_size_pt: float
    line_spacing: float
    space_after_paragraph_pt: float
    section_gap_pt: float
    heading_treatment: dict[str, Any]
    rule_weight_pt: float
    bullet_glyph: str
    date_format: str
    name_treatment: str
    contact_layout: str
    section_order: tuple[str, ...]
    justification: Justification = field(default_factory=Justification)

    @property
    def body_font(self) -> str:
        return self.typeface["body"]

    @property
    def heading_font(self) -> str:
        return self.typeface["heading"]

    def effective_date_format(self, *, has_month_precision: bool) -> str:
        """The date format to actually render with.

        ``yyyy_only`` is a real convention and is offered, but it is the one
        dimension whose value can *lose* information. When the profile holds
        month precision, style does not get to throw it away: a reader who cannot
        tell a three-month role from an eleven-month one has been given less than
        the profile knows. Style yields to substance.
        """
        if self.date_format == "yyyy_only" and has_month_precision:
            return "mon_yyyy"
        return self.date_format

    def as_payload(self) -> dict[str, Any]:
        """The JSON stored in ``setting`` and shown beside a generated document."""
        return {
            "account_id": self.account_id,
            "config_version": self.config_version,
            "typeface": dict(self.typeface),
            "body_size_pt": self.body_size_pt,
            "heading_size_pt": self.heading_size_pt,
            "name_size_pt": self.name_size_pt,
            "line_spacing": self.line_spacing,
            "space_after_paragraph_pt": self.space_after_paragraph_pt,
            "section_gap_pt": self.section_gap_pt,
            "heading_treatment": dict(self.heading_treatment),
            "rule_weight_pt": self.rule_weight_pt,
            "bullet_glyph": self.bullet_glyph,
            "date_format": self.date_format,
            "name_treatment": self.name_treatment,
            "contact_layout": self.contact_layout,
            "section_order": list(self.section_order),
            "justification": {
                "body_paragraphs": self.justification.body_paragraphs,
                "bullets": self.justification.bullets,
                "headings": self.justification.headings,
                "hyphenation": self.justification.hyphenation,
            },
        }


def derive(account_id: str, *, version: str = config_module.CURRENT_VERSION) -> StyleVariant:
    """Derive the variant for an account. Same input, same output, always.

    ``account_id`` is any stable per-install identifier. It is hashed, never
    stored in the document and never rendered, so it cannot leak into a CV.
    """
    if not account_id:
        raise ValueError("account_id is required: a variant must be reproducible")

    style = _style(version)
    dimensions = style["dimensions"]
    seed = hashlib.sha256(f"jarvis-cv-style:{version}:{account_id}".encode()).digest()

    # Read field by field rather than unpacking: the config block also carries a
    # `rationale` key for the reader, which is not a constructor argument.
    declared = style["justification"]
    justification = Justification(
        body_paragraphs=declared["body_paragraphs"],
        bullets=declared["bullets"],
        headings=declared["headings"],
        hyphenation=declared["hyphenation"],
    )

    return StyleVariant(
        account_id=account_id,
        config_version=version,
        typeface=_pick(dimensions["typeface"], seed, "typeface"),
        body_size_pt=_pick(dimensions["body_size_pt"], seed, "body_size_pt"),
        heading_size_pt=_pick(dimensions["heading_size_pt"], seed, "heading_size_pt"),
        name_size_pt=_pick(dimensions["name_size_pt"], seed, "name_size_pt"),
        line_spacing=_pick(dimensions["line_spacing"], seed, "line_spacing"),
        space_after_paragraph_pt=_pick(
            dimensions["space_after_paragraph_pt"], seed, "space_after_paragraph_pt"
        ),
        section_gap_pt=_pick(dimensions["section_gap_pt"], seed, "section_gap_pt"),
        heading_treatment=_pick(dimensions["heading_treatment"], seed, "heading_treatment"),
        rule_weight_pt=_pick(dimensions["rule_weight_pt"], seed, "rule_weight_pt"),
        bullet_glyph=_pick(dimensions["bullet_glyph"], seed, "bullet_glyph"),
        date_format=_pick(dimensions["date_format"], seed, "date_format")["id"],
        name_treatment=_pick(dimensions["name_treatment"], seed, "name_treatment"),
        contact_layout=_pick(dimensions["contact_layout"], seed, "contact_layout"),
        section_order=tuple(_pick(style["section_orders"]["orders"], seed, "section_order")),
        justification=justification,
    )


def resolve(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> StyleVariant:
    """The account's stored variant, deriving and storing it on first use.

    Stored rather than re-derived every time, because the derivation reads a
    config file that a user may edit. A CV whose typeface changed because a
    default was retuned is exactly the inconsistency this is meant to prevent.
    """
    try:
        stored = settings_module.get(conn, SETTING_KEY)
    except settings_module.UnknownSetting:
        stored = None

    if isinstance(stored, dict) and stored.get("account_id") == account_id:
        return _from_payload(stored)

    variant = derive(account_id, version=version)
    settings_module.set_value(conn, SETTING_KEY, variant.as_payload())
    return variant


def _from_payload(payload: dict[str, Any]) -> StyleVariant:
    just = payload.get("justification", {})
    return StyleVariant(
        account_id=payload["account_id"],
        config_version=payload.get("config_version", config_module.CURRENT_VERSION),
        typeface=payload["typeface"],
        body_size_pt=payload["body_size_pt"],
        heading_size_pt=payload["heading_size_pt"],
        name_size_pt=payload["name_size_pt"],
        line_spacing=payload["line_spacing"],
        space_after_paragraph_pt=payload["space_after_paragraph_pt"],
        section_gap_pt=payload["section_gap_pt"],
        heading_treatment=payload["heading_treatment"],
        rule_weight_pt=payload["rule_weight_pt"],
        bullet_glyph=payload["bullet_glyph"],
        date_format=payload["date_format"],
        name_treatment=payload["name_treatment"],
        contact_layout=payload["contact_layout"],
        section_order=tuple(payload["section_order"]),
        justification=Justification(
            body_paragraphs=just.get("body_paragraphs", "justify"),
            bullets=just.get("bullets", "left"),
            headings=just.get("headings", "left"),
            hyphenation=just.get("hyphenation", False),
        ),
    )


def distinct_variant_count(version: str = config_module.CURRENT_VERSION) -> int:
    """How many distinct looks the configured space can produce.

    Reported by ``jarvis doctor`` so the number is a measured fact rather than a
    claim: if it ever drops low enough that collisions are likely, that is
    visible before anyone's CV looks like somebody else's.
    """
    style = _style(version)
    dimensions = style["dimensions"]
    total = 1
    for key in (
        "typeface", "body_size_pt", "heading_size_pt", "name_size_pt", "line_spacing",
        "space_after_paragraph_pt", "section_gap_pt", "heading_treatment",
        "rule_weight_pt", "bullet_glyph", "date_format", "name_treatment", "contact_layout",
    ):
        total *= len(dimensions[key])
    return total * len(style["section_orders"]["orders"])


def describe(variant: StyleVariant) -> str:
    """One line a person can read, for the settings screen and ``jarvis doctor``.

    Kept ASCII-only on purpose. This string is printed to a terminal, and a
    Windows console at the default cp1252 code page raises ``UnicodeEncodeError``
    on an arrow or an ellipsis — turning a status line into a crash.
    """
    return json.dumps(
        {
            "font": f"{variant.body_font}/{variant.heading_font}",
            "body_pt": variant.body_size_pt,
            "headings": variant.heading_treatment["id"],
            "bullet_codepoint": f"U+{ord(variant.bullet_glyph):04X}",
            "dates": variant.date_format,
            "order": " > ".join(variant.section_order[:4]) + " ...",
            "body_alignment": variant.justification.body_paragraphs,
        }
    )
