"""I-30 — rule-based approval: off by default, written only by the user, applications only.

A rule approval is the one approval no person clicks, so every way it could be
switched on by someone other than the user, or widened past what they wrote,
is a test here.
"""

from __future__ import annotations

import json
import socket
import sqlite3

import pytest
from fastapi.testclient import TestClient

from career_scout import approval
from career_scout.mcp import server as mcp_server
from career_scout.store import settings as settings_module
from career_scout.tests.unit.test_web import WRITE, _package
from career_scout.web import app as web_app


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback only, for the asyncio self-pipe behind TestClient on Windows."""
    real = socket.socket.connect

    def loopback_only(self: socket.socket, address: object) -> None:
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            raise RuntimeError(f"this test tried to reach {address!r}")
        real(self, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)


def _qualifying(db: sqlite3.Connection, *, score: float = 85.0, confidence: str = "high",
                country: str = "US") -> str:
    """A package awaiting approval that the rules below accept. Returns its id."""
    pkg = _package(db)
    opportunity_id = db.execute("SELECT opportunity_id FROM application_package WHERE id = ?",
                                (pkg["id"],)).fetchone()[0]
    db.execute("INSERT OR IGNORE INTO country (iso2, name, enabled) VALUES (?, ?, 1)",
               (country, country))
    db.execute("UPDATE opportunity SET country_iso2 = ? WHERE id = ?", (country, opportunity_id))
    db.execute("DELETE FROM assessment WHERE opportunity_id = ?", (opportunity_id,))
    db.execute(
        "INSERT INTO assessment (opportunity_id, config_version, match_score, confidence, "
        "formula, weights, inputs, eligibility_verdict, computed_at) "
        "VALUES (?, 'test', ?, ?, 'sum', '{}', '{}', 'eligible', '2026-09-30T00:00:00Z')",
        (opportunity_id, score, confidence),
    )
    db.execute(
        "INSERT OR REPLACE INTO sufficiency_verdict (opportunity_id, verdict, rules_applied, "
        "fields_examined, rule_version, computed_at) "
        "VALUES (?, 'sufficient', '[]', '[]', 1, '2026-09-30T00:00:00Z')", (opportunity_id,),
    )
    return pkg["id"]


def _rules(db: sqlite3.Connection, **overrides: object) -> None:
    values = {"auto_approve_enabled": True, "auto_approve_min_score": 70,
              "auto_approve_countries": ["us"], "auto_approve_max_per_day": 3, **overrides}
    for key, value in values.items():
        settings_module.set_value(db, key, value)


def _state(db: sqlite3.Connection, package_id: str) -> str:
    return db.execute("SELECT state FROM application_package WHERE id = ?",
                      (package_id,)).fetchone()[0]


def test_rule_approval_is_off_by_default(db) -> None:
    package_id = _qualifying(db)
    result = approval.approve_by_rules(db)
    assert result["approved"] == 0
    assert _state(db, package_id) == "awaiting_approval"
    assert db.execute("SELECT COUNT(*) FROM approval").fetchone()[0] == 0


def test_switched_on_without_written_rules_approves_nothing(db) -> None:
    package_id = _qualifying(db)
    settings_module.set_value(db, "auto_approve_enabled", True)
    result = approval.approve_by_rules(db)
    assert result["approved"] == 0 and "not written" in result["skipped"]
    assert _state(db, package_id) == "awaiting_approval"


def test_a_package_inside_the_rules_is_approved_and_records_why(db) -> None:
    package_id = _qualifying(db)
    _rules(db)
    result = approval.approve_by_rules(db)
    assert result["approved_ids"] == [package_id]
    assert _state(db, package_id) == "approved"
    row = db.execute("SELECT approved_by, rule_basis, content_hash FROM approval "
                     "WHERE package_id = ?", (package_id,)).fetchone()
    assert row["approved_by"] == "rule"
    basis = json.loads(row["rule_basis"])
    assert basis["rules"]["min_score"] == 70 and basis["rules"]["countries"] == ["US"]
    assert basis["inputs"]["score"] == 85.0 and basis["inputs"]["country"] == "US"
    assert row["content_hash"] == db.execute(
        "SELECT content_sha256 FROM application_package WHERE id = ?", (package_id,)
    ).fetchone()[0]


@pytest.mark.parametrize(("setup", "reason"), [
    ({"score": 60.0}, "below"),
    ({"confidence": "low"}, "confidence"),
    ({"country": "DE"}, "allowed countries"),
])
def test_a_package_outside_the_rules_is_held_with_its_reason(db, setup, reason) -> None:
    package_id = _qualifying(db, **setup)
    _rules(db)
    result = approval.approve_by_rules(db)
    assert result["approved"] == 0
    assert reason in result["held"][0]["reason"]
    assert _state(db, package_id) == "awaiting_approval"


def test_replies_and_outreach_are_never_rule_approved(db) -> None:
    package_id = _qualifying(db)
    db.execute("UPDATE application_package SET kind = 'outreach' WHERE id = ?", (package_id,))
    _rules(db)
    result = approval.approve_by_rules(db)
    assert result["approved"] == 0
    assert "always need a person" in result["held"][0]["reason"]
    assert _state(db, package_id) == "awaiting_approval"


def test_a_package_with_warnings_waits_for_a_person(db) -> None:
    package_id = _qualifying(db)
    db.execute("UPDATE application_package SET warnings = ? WHERE id = ?",
               (json.dumps(["You already applied to this role."]), package_id))
    _rules(db)
    assert approval.approve_by_rules(db)["approved"] == 0


def test_the_daily_limit_holds(db) -> None:
    package_id = _qualifying(db)
    _rules(db, auto_approve_max_per_day=0)
    result = approval.approve_by_rules(db)
    assert result["approved"] == 0 and "limit" in result["held"][0]["reason"]
    assert _state(db, package_id) == "awaiting_approval"


@pytest.mark.parametrize("key", sorted(settings_module.HUMAN_ONLY_KEYS))
def test_the_mcp_host_cannot_write_the_rules(db, key) -> None:
    """I-25 extended: a model that could write the rules could approve its own sends."""
    before = settings_module.get(db, key)
    result = mcp_server.set_setting(key, json.dumps(True if key.endswith("enabled") else 1))
    assert result["changed"] is False and "this computer" in result["reason"]
    assert settings_module.get(db, key) == before


def test_every_rule_setting_is_human_only() -> None:
    assert {k for k in settings_module.DEFAULTS if k.startswith("auto_approve")} \
        == settings_module.HUMAN_ONLY_KEYS
    assert "auto_approve_enabled" in settings_module.HUMAN_ONLY_KEYS


def test_the_rules_are_written_only_from_this_computer(db) -> None:
    phone = TestClient(web_app.create_app(), base_url="http://localhost:8765",
                       client=("192.168.1.23", 50000))
    refused = phone.put("/api/settings/auto_approve_enabled", json={"value": True},
                        headers=WRITE)
    assert refused.status_code == 403
    assert settings_module.get(db, "auto_approve_enabled") is False

    here = TestClient(web_app.create_app(), base_url="http://localhost:8765",
                      client=("127.0.0.1", 50000))
    accepted = here.put("/api/settings/auto_approve_enabled", json={"value": True},
                        headers=WRITE)
    assert accepted.status_code == 200, accepted.text
    assert settings_module.get(db, "auto_approve_enabled") is True


@pytest.mark.parametrize(("approved_by", "basis"), [
    ("rule", None), ("human", "{}"), ("model", "{}"),
])
def test_the_schema_refuses_an_unexplained_or_unknown_approver(db, approved_by, basis) -> None:
    package_id = _qualifying(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO approval (id, package_id, approved_by, approved_at, content_hash, "
            "rendered_hash, destination_hash, destination_snapshot, mailbox_credential_id, "
            "rule_basis) VALUES ('a1', ?, ?, 'now', 'h', 'r', 'd', '{}', 'c1', ?)",
            (package_id, approved_by, basis),
        )
