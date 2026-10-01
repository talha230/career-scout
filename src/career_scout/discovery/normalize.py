"""Normalising a raw source payload into the shape the `opportunity` table holds — T030a.

Ported from ``jobfinder.phase2.normalize``. The rules below were measured against
the 4,402-posting corpus in ``data/jobfinder.db`` and each one exists because the
obvious alternative produced a wrong row.

Three rules carried over unchanged, because they are the ones that keep invented
values out of the store:

* **Pay is disclosed or it is NULL.** Zero, "competitive" and "DOE" are all *not
  disclosed*. RemoteOK returns ``0`` for "not stated", and writing that through
  as a salary of zero is the worst kind of fabricated figure: one that looks
  measured.
* **A location that does not match is ``unresolved``, never guessed.** Two
  unknowns are not the same place.
* **Seniority is never stripped from a title.** An earlier version treated
  senior/staff/lead as noise, which gave "Staff Software Engineer" and "Senior
  Software Engineer" at one company an identical fingerprint and merged them into
  one posting. They are different jobs with different pay.
"""

from __future__ import annotations

import functools
import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from career_scout import config as config_module

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Stripped for matching only. The stored `employer` keeps the name exactly as the
# source published it; only `employer_norm` is flattened.
_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|"
    r"gmbh|ag|plc|pvt|pte|bv|b\.v\.|nv|sa|s\.a\.|sas|srl|spa|as|ab|oy|kk|"
    r"holdings|group|technologies|technology|labs|the)\b",
    re.IGNORECASE,
)

# Boilerplate that decorates a title without changing which job it is: equal
# opportunity gender markers, work mode, contract type.
_TITLE_NOISE_RAW = re.compile(
    r"\((?:m|w|f|d|h|x)(?:\s*/\s*(?:m|w|f|d|h|x))+\)|"      # (m/w/d), (m/f/d), (w/m/x)
    r"\b(?:m|w|f)\s*/\s*(?:m|w|f|d)(?:\s*/\s*[a-z])?\b|"    # bare m/w/d
    r"\(all genders?\)|\ball genders?\b|"
    r"\(\s*[a-z]:in\s*\)",                                   # German gender colon forms
    re.IGNORECASE,
)
_TITLE_NOISE_NORMALIZED = re.compile(
    r"\b(full time|part time|fulltime|parttime|contract|permanent|temporary|"
    r"remote|hybrid|onsite|on site|all genders?|m w d|m f d|w m d|f m d|h f)\b",
    re.IGNORECASE,
)


def strip_html(value: str | None) -> str:
    """Flatten source markup to text.

    Greenhouse double-escapes its ``content`` field, so a single unescape leaves
    ``&lt;p&gt;`` in the text and every later keyword match silently fails on it.
    """
    if not value:
        return ""
    text = html.unescape(value)
    if "&lt;" in text or "&amp;" in text:
        text = html.unescape(text)
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_company(name: str | None) -> str:
    base = _COMPANY_SUFFIXES.sub(" ", normalize_text(name))
    return _WS_RE.sub(" ", base).strip()


def normalize_title(title: str | None) -> str:
    """Flatten a title for comparison, keeping every word that changes the job.

    The punctuated forms are stripped first, while the punctuation still exists,
    then again after normalisation has flattened ``m/w/d`` to ``m w d``.
    """
    if not title:
        return ""
    base = _TITLE_NOISE_RAW.sub(" ", title)
    base = normalize_text(base)
    base = _TITLE_NOISE_NORMALIZED.sub(" ", base)
    return _WS_RE.sub(" ", base).strip()


@functools.lru_cache(maxsize=8)
def _country_index(
    version: str,
) -> tuple[
    dict[str, dict],
    tuple[tuple[str, re.Pattern[str], dict], ...],
    tuple[tuple[str, re.Pattern[str], dict], ...],
]:
    """Alias and city lookup tables built from the versioned country config.

    **The patterns are compiled here, once per config version, and never inside
    the matching loop.** Building them per call is the same fault the skill
    matcher already carries a warning about: measured on this corpus it cost
    263,722 ``re.compile`` calls and 55% of total ingest time, because there are
    more distinct alias patterns than Python's internal regex cache holds, so
    every posting re-compiled almost all of them.
    """
    config = config_module.load("country_preferences", version)
    by_code: dict[str, dict] = {}
    alias_pairs: list[tuple[str, dict]] = []
    city_pairs: list[tuple[str, dict]] = []

    for country in config.get("countries", []):
        by_code[country["code"]] = country
        for name in (country["name"], country["code"], *country.get("aliases", [])):
            normalized = normalize_text(name)
            if normalized:
                alias_pairs.append((normalized, country))
        # Cities and regions are matched after country names, because "Austin, TX"
        # still names the country unambiguously.
        for place in (*country.get("major_cities", []), *country.get("regions", [])):
            normalized = normalize_text(place)
            if normalized:
                city_pairs.append((normalized, country))

    # Longest first, so "united arab emirates" beats "emirates" and "new york"
    # beats "york". The order is what makes the longest name win, so it is kept
    # rather than collapsed into a single alternation: one alternation returns the
    # EARLIEST match, which for "Remote (US or Germany)" would answer US.
    alias_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    city_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def compiled(
        pairs: list[tuple[str, dict]],
    ) -> tuple[tuple[str, re.Pattern[str], dict], ...]:
        return tuple(
            (name, re.compile(rf"\b{re.escape(name)}\b"), country) for name, country in pairs
        )

    return by_code, compiled(alias_pairs), compiled(city_pairs)


_REMOTE_WORDS = re.compile(
    r"\b(remote|work from home|wfh|anywhere|distributed|telecommute|virtual)\b", re.IGNORECASE
)
_HYBRID_WORDS = re.compile(r"\bhybrid\b", re.IGNORECASE)
_WORLDWIDE_WORDS = re.compile(
    r"\b(worldwide|anywhere in the world|global|any location|any timezone)\b", re.IGNORECASE
)


@dataclass(slots=True)
class ResolvedLocation:
    location_raw: str | None
    city: str | None = None
    country_code: str | None = None
    country_name: str | None = None
    resolution: str = "unresolved"       # matched_alias | matched_city | remote | unresolved
    remote_status: str = "unknown"       # remote | hybrid | onsite | unknown
    remote_scope: str | None = None      # worldwide | region_locked | country_locked


def resolve_location(
    location_raw: str | None,
    *,
    extra_signals: str = "",
    version: str = config_module.CURRENT_VERSION,
) -> ResolvedLocation:
    """Resolve a free-text location, never guessing a country it did not match."""
    result = ResolvedLocation(location_raw=location_raw)
    haystack = f"{location_raw or ''} {extra_signals or ''}"

    if _HYBRID_WORDS.search(haystack):
        result.remote_status = "hybrid"
    elif _REMOTE_WORDS.search(haystack):
        result.remote_status = "remote"

    _by_code, alias_pairs, city_pairs = _country_index(version)
    normalized = normalize_text(location_raw)

    if normalized:
        # Word-boundary match, so "oman" does not fire inside "romania".
        for _alias, pattern, country in alias_pairs:
            if pattern.search(normalized):
                result.country_code = country["code"]
                result.country_name = country["name"]
                result.resolution = "matched_alias"
                break
        if result.country_code is None:
            for city, pattern, country in city_pairs:
                if pattern.search(normalized):
                    result.city = city.title()
                    result.country_code = country["code"]
                    result.country_name = country["name"]
                    result.resolution = "matched_city"
                    break

    if result.city is None and location_raw:
        head = location_raw.split(",")[0].strip()
        if head and not _REMOTE_WORDS.search(head) and len(head) < 64:
            result.city = head

    if result.remote_status == "remote":
        if _WORLDWIDE_WORDS.search(haystack):
            result.remote_scope = "worldwide"
        elif result.country_code:
            result.remote_scope = "country_locked"
        else:
            result.remote_scope = "region_locked"
        if result.country_code is None:
            result.resolution = "remote"

    if result.remote_status == "unknown" and result.country_code:
        result.remote_status = "onsite"

    return result


_CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",
    "₹": "INR", "AED": "AED", "SAR": "SAR", "QAR": "QAR", "PKR": "PKR",
    "MYR": "MYR", "RM": "MYR", "SGD": "SGD", "CAD": "CAD", "AUD": "AUD",
    "NZD": "NZD", "CHF": "CHF", "SEK": "SEK", "NOK": "NOK", "DKK": "DKK",
}

_UNDISCLOSED_TOKENS = re.compile(
    r"\b(competitive|negotiable|doe|depending on experience|market rate|"
    r"tbd|to be discussed|not disclosed|undisclosed)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class PayInfo:
    """What the employer published about pay. Absent is absent."""

    disclosed: bool = False
    min_value: float | None = None
    max_value: float | None = None
    currency: str | None = None
    period: str | None = None
    raw_text: str | None = None
    source: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any] | None:
        """The JSON written to ``opportunity.pay_disclosed``, or None.

        An undisclosed salary returns ``None`` rather than a dict of nulls, so the
        column distinguishes "the employer published nothing" from "the employer
        published a zero".
        """
        if not self.disclosed:
            return None
        return {
            "min": self.min_value,
            "max": self.max_value,
            "currency": self.currency,
            "period": self.period,
            "raw_text": self.raw_text,
            "source": self.source,
            "notes": list(self.notes),
        }


def pay_from_structured(
    min_value: Any,
    max_value: Any,
    currency: str | None = None,
    period: str | None = None,
    source: str = "api_structured_field",
) -> PayInfo:
    """Build pay from a source's own numeric fields.

    Zero and negative numbers are ABSENT, not a salary — see the module docstring.
    """
    info = PayInfo(source=source)

    def clean(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    low, high = clean(min_value), clean(max_value)
    if low is None and high is None:
        info.notes.append("Source exposed no usable pay figure; stored as not disclosed.")
        return info

    if low is not None and high is not None and low > high:
        low, high = high, low
        info.notes.append("Source returned min > max; values swapped.")

    info.disclosed = True
    info.min_value, info.max_value = low, high
    info.currency = (currency or "").upper() or None
    info.period = period
    if info.currency is None:
        info.notes.append("Pay figure disclosed but the source stated no currency.")
    return info


_RANGE_RE = re.compile(
    r"(?P<cur>[$€£]|USD|EUR|GBP|AED|SAR|QAR|PKR|MYR|SGD|CAD|AUD|NZD|CHF)?\s*"
    r"(?P<low>\d{1,3}(?:[,.\s]\d{3})+|\d{2,7})\s*"
    r"(?:-|to|–|—)\s*"
    r"(?P<cur2>[$€£]|USD|EUR|GBP|AED|SAR|QAR|PKR|MYR|SGD|CAD|AUD|NZD|CHF)?\s*"
    r"(?P<high>\d{1,3}(?:[,.\s]\d{3})+|\d{2,7})"
    r"\s*(?P<suffix>k|per year|/year|per annum|p\.a\.|annually|per month|/month|monthly|"
    r"per hour|/hour|hourly)?",
    re.IGNORECASE,
)


def pay_from_text(text: str | None) -> PayInfo:
    """Best-effort parse of a pay range out of description prose.

    Lower confidence than a structured field, so it is tagged
    ``parsed_from_description`` and says so in its notes. **86% of the corpus
    discloses no pay at all**, so this path is the exception, not the norm.
    """
    info = PayInfo(source="parsed_from_description")
    if not text:
        return info

    window = text[:4000]
    if _UNDISCLOSED_TOKENS.search(window) and not _RANGE_RE.search(window):
        info.notes.append("Description explicitly declines to state pay.")
        return info

    match = _RANGE_RE.search(window)
    if not match:
        return info

    def to_number(raw: str, is_k: bool) -> float | None:
        try:
            number = float(re.sub(r"[,\s]", "", raw))
        except ValueError:
            return None
        return number * 1000 if is_k else number

    is_k = (match.group("suffix") or "").lower() == "k"
    low = to_number(match.group("low"), is_k)
    high = to_number(match.group("high"), is_k)
    if low is None or high is None or low <= 0 or high <= 0:
        return info

    # "2020 - 2024" is a date range and "10 - 15 years" is a span. Neither is pay.
    if high < 100 or (1900 < low < 2100 and 1900 < high < 2100):
        info.notes.append("Numeric range rejected as not pay (looks like years).")
        return info

    symbol = (match.group("cur") or match.group("cur2") or "").strip()
    currency = _CURRENCY_SYMBOLS.get(symbol.upper() if len(symbol) > 1 else symbol)

    suffix = (match.group("suffix") or "").lower()
    if "month" in suffix:
        period = "month"
    elif "hour" in suffix:
        period = "hour"
    elif suffix:
        period = "year"
    else:
        period = None

    info.disclosed = True
    info.min_value, info.max_value = low, high
    info.currency = currency
    info.period = period
    info.raw_text = match.group(0).strip()
    info.notes.append("Parsed from description text, not a structured field — lower confidence.")
    return info


_VISA_POSITIVE = re.compile(
    r"(visa\s+sponsorship\s+(is\s+)?(available|provided|offered)|"
    r"we\s+sponsor\s+visas?|sponsorship\s+available|will\s+sponsor|"
    r"relocation\s+(package|assistance|support)\s+(is\s+)?(available|provided|offered)|"
    r"visa[\s_-]?sponsorship)",
    re.IGNORECASE,
)
_VISA_NEGATIVE = re.compile(
    r"(no\s+visa\s+sponsorship|unable\s+to\s+sponsor|cannot\s+sponsor|"
    r"not\s+able\s+to\s+(provide|offer)\s+sponsorship|"
    r"sponsorship\s+is\s+not\s+available|must\s+have\s+the\s+right\s+to\s+work|"
    r"no\s+sponsorship\s+(is\s+)?available)",
    re.IGNORECASE,
)


def detect_visa_signal(text: str | None, tags: list[str] | None = None) -> tuple[bool, str | None]:
    """Return ``(sponsorship_offered, evidence)``.

    An explicit refusal beats an incidental mention: a posting that says "no
    sponsorship" while using the word "visa" must not be flagged as friendly.
    """
    haystack = " ".join(filter(None, [text or "", " ".join(tags or [])]))
    if not haystack.strip():
        return False, None

    negative = _VISA_NEGATIVE.search(haystack)
    if negative:
        return False, f"Explicitly excludes sponsorship: '{negative.group(0)}'"

    positive = _VISA_POSITIVE.search(haystack)
    if positive:
        start = max(0, positive.start() - 60)
        return True, haystack[start : positive.end() + 60].strip()

    return False, None


def dedupe_key(
    employer: str,
    title: str,
    country_code: str | None,
    city: str | None,
) -> str:
    """The stable identity hash written to ``opportunity.dedupe_key``.

    Derived from normalised employer, normalised title and place. Two sources
    publishing the same vacancy produce the same key; two different seniorities at
    one employer do not.
    """
    parts = [
        normalize_company(employer),
        normalize_title(title),
        (country_code or "").lower(),
        normalize_text(city),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def parse_datetime(value: Any) -> datetime | None:
    """Parse the several date shapes these APIs return. Never invents a date."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        # Lever's createdAt is epoch MILLISECONDS. Read as seconds it is a year
        # around 55,000, which raises and came back None: all 491 Lever postings
        # in the legacy corpus lost their date that way. A missing date is kept by
        # the staleness filter as "unknown", so a posting from March 2024 was
        # shown as live. No real posting has a seconds timestamp above 1e11
        # (the year 5138), so anything larger is milliseconds.
        if abs(seconds) > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for parser in (
        datetime.fromisoformat,
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z"),
    ):
        try:
            parsed = parser(text)
        except (ValueError, TypeError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def canonical_url(url: str | None) -> str:
    """Strip campaign tracking so two links to one posting compare equal.

    Every parameter that identifies *which* posting it is survives — Greenhouse
    puts the job id in ``gh_jid``, so discarding the query string collapses an
    employer's whole board to a single URL.
    """
    from urllib.parse import parse_qsl, urlparse

    if not url:
        return ""
    parsed = urlparse(url.strip().lower())
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key not in _TRACKING_PARAM_NAMES and not key.startswith("utm_")
    ]
    base = f"{parsed.netloc}{parsed.path}".rstrip("/")
    if kept:
        return f"{base}?" + "&".join(f"{k}={v}" for k, v in sorted(kept))
    return base


# Params identifying the campaign, not the job. Everything else is KEPT.
_TRACKING_PARAM_NAMES = {
    "gh_src", "source", "ref", "src", "lever-origin", "lever-source",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
}
