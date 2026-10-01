"""T031b — Himalayas and Jobicy, from payloads shaped as the live APIs sent them (2026-09-25)."""

from __future__ import annotations

from jarvis import config as config_module
from jarvis.discovery import clients

HIMALAYAS = {"comments": "...", "jobs": [{
    "title": "Danish Transcription Expert", "companyName": "micro1",
    "employmentType": "Contractor", "minSalary": 24, "maxSalary": 30, "salaryPeriod": "hourly",
    "currency": "USD", "locationRestrictions": ["Denmark"], "categories": ["Transcription"],
    "description": "<p>Transcribe Danish audio.</p>", "pubDate": 1790401136,
    "applicationLink": "https://himalayas.app/companies/micro1/jobs/danish-transcription-expert",
    "guid": "https://himalayas.app/companies/micro1/jobs/danish-transcription-expert",
}]}

JOBICY = {"friendlyNotice": "...", "jobs": [{
    "id": 154087, "url": "https://jobicy.com/jobs/154087-orioledb-developer-amer",
    "jobTitle": "OrioleDB Developer (AMER)", "companyName": "Supabase",
    "jobIndustry": ["Software Engineering"], "jobType": ["Full-Time"],
    "jobGeo": "LATAM,  Canada,  USA", "jobDescription": "<p>Drive OrioleDB.</p>",
    "pubDate": "2026-09-25T20:54:07+00:00",
}]}


def _raw(provider: str, data: dict):
    item = clients.items_of(provider, data)[0]
    return clients.to_raw(provider, item, source_id=provider, company=None,
                          fetched_at="2026-09-25T00:00:00Z", snapshot_path=None)


def test_himalayas_maps_with_its_link_back_url_and_an_hourly_period() -> None:
    raw = _raw("himalayas", HIMALAYAS)
    assert raw.source_url.startswith("https://himalayas.app/")     # the required link-back
    assert (raw.title, raw.employer, raw.location_raw) == (
        "Danish Transcription Expert", "micro1", "Denmark")
    assert raw.pay_period == "hour"             # never read as an annual salary
    assert raw.posted_at.startswith("2026-09")  # epoch seconds, as UTC
    assert "remote" in raw.tags and raw.apply_route == "portal"


def test_jobicy_maps_and_undisclosed_pay_stays_undisclosed() -> None:
    raw = _raw("jobicy", JOBICY)
    assert raw.source_url == raw.apply_target == "https://jobicy.com/jobs/154087-orioledb-developer-amer"
    assert raw.pay_min is None and raw.pay_period is None
    assert raw.employment_types == ["Full-Time"]


def test_an_unknown_pay_period_is_not_guessed() -> None:
    odd = {"jobs": [{**HIMALAYAS["jobs"][0], "salaryPeriod": "per-gig"}]}
    assert _raw("himalayas", odd).pay_period is None


def test_both_are_registered_enabled_with_their_terms_and_marketplaces_are_manual() -> None:
    registry = config_module.sources()
    enabled = {s["name"]: s for s in registry["aggregators"] if s.get("enabled")}
    assert {"himalayas", "jobicy"} <= set(enabled)
    assert all(enabled[n]["terms"] for n in ("himalayas", "jobicy"))
    manual = {s["name"] for s in registry["manual_review_only"]}
    assert {"Upwork", "Fiverr", "Freelancer.com"} <= manual
    assert not config_module.validate(), config_module.validate()
