"""I-09 — no login, CAPTCHA, payment or account-creation capability exists. T011.

**A tripwire, and documented as one.** It scans the package source for the
libraries and code paths those capabilities need — browser automation, CAPTCHA
solvers, payment SDKs, login and sign-up routines — and fails the build the
moment one appears. It cannot prove absence: code can be written to dodge a
denylist. The control is structural and lives elsewhere (``jarvis.net`` refuses
every non-GET outside the user's own Google APIs, and refuses credentials to any
other host); this test is the early warning that someone is building toward a
forbidden capability, caught at review rather than in production.

This test was ticked as done under T011 before it existed; it was written on
2026-09-25 when the invariant table was checked against the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import jarvis

pytestmark = pytest.mark.invariant

#: Libraries whose only purpose here would be a forbidden capability.
FORBIDDEN_IMPORTS = (
    "selenium", "playwright", "pyppeteer", "mechanize", "undetected_chromedriver",
    "twocaptcha", "anticaptchaofficial", "python_anticaptcha", "capsolver", "deathbycaptcha",
    "stripe", "braintree", "paypalrestsdk", "square",
)

#: Function or route names that would be one of the four forbidden things.
FORBIDDEN_NAMES = re.compile(
    r"\bdef\s+(login|log_in|sign_in|signin|signup|sign_up|register_account|create_account|"
    r"solve_captcha|bypass_captcha|checkout|make_payment|pay|charge_card|submit_portal|"
    r"portal_login)\b|"
    r"@app\.(post|get|put)\(\"/api/(login|signup|register|checkout|pay)\b",
)


def _package_sources() -> list[Path]:
    root = Path(jarvis.__file__).parent
    return [p for p in root.rglob("*.py") if "tests" not in p.parts]


def test_forbidden_capabilities_absent() -> None:
    offenders: list[str] = []
    for path in _package_sources():
        text = path.read_text(encoding="utf-8")
        for library in FORBIDDEN_IMPORTS:
            if re.search(rf"^\s*(import|from)\s+{library}\b", text, re.M):
                offenders.append(f"{path.name}: imports {library}")
        for match in FORBIDDEN_NAMES.finditer(text):
            offenders.append(f"{path.name}: {match.group(0).strip()}")
    assert not offenders, offenders


def test_no_mcp_tool_is_a_forbidden_capability() -> None:
    from jarvis.mcp import server

    for name in server.tool_names():
        assert not re.search(r"login|signup|register|captcha|pay|checkout|portal", name), name


def test_the_tripwire_itself_fires() -> None:
    """Without this, a green result could mean the patterns match nothing at all."""
    assert re.search(r"^\s*(import|from)\s+selenium\b", "from selenium import webdriver", re.M)
    assert FORBIDDEN_NAMES.search("def solve_captcha(image):")
    assert FORBIDDEN_NAMES.search('@app.post("/api/login")')
    assert not FORBIDDEN_NAMES.search("def playlist(self):")
