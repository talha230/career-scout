"""Discovery clients — T030d (fetch half).

Every request here goes through the real :class:`career_scout.net.Fetcher` — its
guards, its ``robots.txt`` check and its per-host limiter — with an
``httpx.MockTransport`` underneath, so nothing opens a socket. The payloads are
real items frozen from ``data/jobfinder.db`` (``fixtures/source_payloads.json``),
one per provider, rather than shapes invented to fit the mappers.
"""

from __future__ import annotations

import collections
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_scout import net
from career_scout.discovery import clients, countries
from career_scout.store import snapshots

FIXTURES = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "source_payloads.json").read_text(
        encoding="utf-8"
    )
)

LEGAL_NOTICE = {"legal": "By using the API you agree to link back to Remote OK."}


def _item(provider: str) -> dict[str, Any]:
    return json.loads(json.dumps(FIXTURES[provider]["item"]))


# ------------------------------------------------------------------ a fake web


class FakeWeb:
    """Serves the frozen payloads by URL, and counts every request."""

    def __init__(self) -> None:
        self.calls: collections.Counter[str] = collections.Counter()
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.robots: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls[url] += 1
        host = request.url.host

        if request.url.path == "/robots.txt":
            if host in self.robots:
                return httpx.Response(200, text=self.robots[host])
            return httpx.Response(404)

        for prefix, override in self.overrides.items():
            if url.startswith(prefix):
                return override(request)

        if host == "remoteok.com":
            return httpx.Response(200, json=[LEGAL_NOTICE, _item("remoteok")])
        if host == "himalayas.app":
            return httpx.Response(200, json={"jobs": [_item("himalayas")], "nextCursor": None})
        if host == "jobicy.com":
            return httpx.Response(200, json={"jobs": [_item("jobicy")], "success": True})
        if host == "www.arbeitnow.com":
            return httpx.Response(200, json={"data": [_item("arbeitnow")], "links": {"next": None}})
        if host == "boards-api.greenhouse.io":
            token = request.url.path.split("/")[3]
            jobs = [_item("greenhouse")] if token == "careem" else []
            return httpx.Response(200, json={"jobs": jobs})
        if host == "api.lever.co":
            token = request.url.path.split("/")[3]
            if token == "palantir":
                item = _item("lever")
                return httpx.Response(200, json=[item])
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    def fetcher(self) -> net.Fetcher:
        return net.Fetcher(transport=httpx.MockTransport(self.handler), sleep=lambda _s: None)


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


def _discover(conn: sqlite3.Connection, web: FakeWeb) -> clients.DiscoveryReport:
    with web.fetcher() as fetcher:
        return clients.discover(conn, fetcher, sleep=lambda _s: None)


def _result(report: clients.DiscoveryReport, source_id: str) -> clients.SourceResult:
    return next(r for r in report.sources if r.source_id == source_id)


# --------------------------------------------------------------------- planning


def test_the_plan_follows_the_enabled_packs(db: sqlite3.Connection) -> None:
    countries.install(db)

    planned = {fetch.source_id: fetch for fetch in clients.plan(db)}

    assert "remoteok" in planned
    assert "arbeitnow" in planned
    assert planned["arbeitnow"].asked_by == ("DE",)
    assert planned["arbeitnow"].max_pages > 1
    assert "greenhouse:careem" in planned
    assert "lever:palantir" in planned
    # No pack names workable, so the Foodics board is registered and never fetched.
    assert not any(source_id.startswith("workable:") for source_id in planned)


def test_a_source_no_enabled_pack_asks_for_is_not_planned(db: sqlite3.Connection) -> None:
    countries.install(db)
    for code in ("US", "GB", "AU", "PK"):
        countries.set_enabled(db, code, enabled=False)

    planned = {fetch.source_id for fetch in clients.plan(db)}

    # Germany alone: arbeitnow, greenhouse, remoteok. Lever is asked for by none.
    assert not any(source_id.startswith("lever:") for source_id in planned)
    assert "arbeitnow" in planned


# ---------------------------------------------------------------------- mapping


def test_the_remoteok_legal_notice_is_not_a_posting() -> None:
    items = clients.items_of("remoteok", [LEGAL_NOTICE, _item("remoteok")])

    assert len(items) == 1
    assert "legal" not in items[0]


def test_greenhouse_maps_its_real_shape() -> None:
    raw = clients.to_raw(
        "greenhouse",
        _item("greenhouse"),
        source_id="greenhouse:careem",
        company="Careem",
        fetched_at="2026-09-24T00:00:00Z",
        snapshot_path="snapshots/x.json",
    )

    assert raw is not None
    assert raw.employer == "Careem"
    assert raw.title == "Account Manager II"
    assert raw.location_raw == "Dubai, United Arab Emirates"
    assert raw.posted_at == "2026-07-06T13:26:14Z"  # -04:00 normalised to UTC
    assert raw.snapshot_path == "snapshots/x.json"


def test_lever_dates_survive_being_milliseconds() -> None:
    """All 491 legacy Lever postings lost their date to this. It was March 2024."""
    raw = clients.to_raw(
        "lever",
        _item("lever"),
        source_id="lever:palantir",
        company="Palantir Technologies",
        fetched_at="2026-09-24T00:00:00Z",
        snapshot_path=None,
    )

    assert raw is not None
    assert raw.posted_at == "2024-03-25T21:50:16Z"
    assert raw.employer == "Palantir Technologies"
    assert "London" in (raw.location_raw or "")


def test_arbeitnow_remote_flag_becomes_a_tag() -> None:
    raw = clients.to_raw(
        "arbeitnow",
        _item("arbeitnow"),
        source_id="arbeitnow",
        company=None,
        fetched_at="2026-09-24T00:00:00Z",
        snapshot_path=None,
    )

    assert raw is not None
    assert "remote" in raw.tags
    assert raw.employer == "Enpal"


def test_workable_location_is_assembled_from_its_parts() -> None:
    raw = clients.to_raw(
        "workable",
        _item("workable"),
        source_id="workable:foodics",
        company="Foodics",
        fetched_at="2026-09-24T00:00:00Z",
        snapshot_path=None,
    )

    assert raw is not None
    # city, state, country as the source splits them; the real payload has a state.
    assert raw.location_raw == "Cairo, Cairo Governorate, Egypt"
    assert raw.posted_at == "2026-08-03T00:00:00Z"


def test_an_item_without_a_url_is_not_mapped() -> None:
    item = _item("greenhouse")
    item.pop("absolute_url")

    raw = clients.to_raw(
        "greenhouse",
        item,
        source_id="greenhouse:careem",
        company="Careem",
        fetched_at="2026-09-24T00:00:00Z",
        snapshot_path=None,
    )

    assert raw is None


def test_a_zero_salary_is_not_a_salary(db: sqlite3.Connection, web: FakeWeb) -> None:
    """The RemoteOK fixture publishes salary_min "0". Absent, not zero."""
    raw = clients.to_raw(
        "remoteok",
        _item("remoteok"),
        source_id="remoteok",
        company=None,
        fetched_at="2026-09-24T00:00:00Z",
        snapshot_path=None,
    )

    assert raw is not None
    assert raw.pay_currency is None
    assert raw.pay_period is None

    # And through ingest: nothing stored as disclosed pay.
    _discover(db, web)
    stored = db.execute(
        "SELECT pay_disclosed FROM opportunity WHERE title = 'Oops something happened'"
    ).fetchone()
    assert stored["pay_disclosed"] is None


# ------------------------------------------------------------------ the run


def test_a_full_run_stores_snapshots_sources_and_a_run_row(
    db: sqlite3.Connection, web: FakeWeb
) -> None:
    report = _discover(db, web)

    assert report.status == "ok", report.obstacles
    run = db.execute("SELECT * FROM run WHERE id = ?", (report.run_id,)).fetchone()
    assert run["kind"] == "discover"
    assert run["status"] == "ok"
    assert run["finished_at"] is not None
    assert json.loads(run["obstacles"]) == []
    assert json.loads(run["counts"])["totals"]["items"] == report.totals()["items"]

    # Every stored opportunity names a page that exists on disk.
    stored = db.execute("SELECT id, snapshot_path, source_id FROM opportunity").fetchall()
    assert stored
    for row in stored:
        assert row["snapshot_path"]
        assert snapshots.resolve(row["snapshot_path"]).is_file()

    # And every source it came from has a row, which the foreign key requires.
    source_ids = {row["id"] for row in db.execute("SELECT id FROM source")}
    assert {row["source_id"] for row in stored} <= source_ids


def test_the_junk_posting_is_stored_and_rejected_not_dropped(
    db: sqlite3.Connection, web: FakeWeb
) -> None:
    """"Oops something happened" arrives, is kept, and carries its reason."""
    _discover(db, web)

    row = db.execute(
        "SELECT o.id, r.rule FROM opportunity o JOIN rejection r ON r.opportunity_id = o.id "
        "WHERE o.title = 'Oops something happened'"
    ).fetchone()

    assert row is not None
    assert row["rule"]


def test_a_posting_in_an_unpacked_country_is_kept(db: sqlite3.Connection, web: FakeWeb) -> None:
    """Careem's Dubai posting: AE has no pack, and nobody decided to exclude it."""
    _discover(db, web)

    row = db.execute("SELECT country_iso2 FROM opportunity WHERE employer = 'Careem'").fetchone()

    assert row is not None
    assert row["country_iso2"] == "AE"


def test_a_switched_off_country_is_counted_not_stored(
    db: sqlite3.Connection, web: FakeWeb
) -> None:
    """The Palantir posting is in London. Switch GB off and it is out of scope."""
    countries.install(db)
    countries.set_enabled(db, "GB", enabled=False)

    report = _discover(db, web)

    assert _result(report, "lever:palantir").out_of_scope == {"GB": 1}
    assert db.execute(
        "SELECT COUNT(*) AS n FROM opportunity WHERE employer = 'Palantir Technologies'"
    ).fetchone()["n"] == 0
    assert report.totals()["out_of_scope"] == 1


def test_running_twice_is_a_sighting_not_a_second_row(
    db: sqlite3.Connection, web: FakeWeb
) -> None:
    first = _discover(db, web)
    count = db.execute("SELECT COUNT(*) AS n FROM opportunity").fetchone()["n"]

    second = _discover(db, web)

    assert db.execute("SELECT COUNT(*) AS n FROM opportunity").fetchone()["n"] == count
    assert second.totals()["stored"] == 0
    assert second.totals()["sightings"] == first.totals()["stored"] + first.totals()["rejected"]


def test_the_high_water_mark_is_recorded(db: sqlite3.Connection, web: FakeWeb) -> None:
    _discover(db, web)

    mark = db.execute(
        "SELECT high_water_mark FROM source WHERE id = 'greenhouse:careem'"
    ).fetchone()["high_water_mark"]

    assert mark == "2026-07-06T13:26:14Z"


# ------------------------------------------------------------------ failures


def test_one_failing_source_makes_the_run_partial(db: sqlite3.Connection, web: FakeWeb) -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/stripe/"
    web.overrides[url] = lambda _r: httpx.Response(503)

    report = _discover(db, web)

    assert report.status == "partial"
    stripe = _result(report, "greenhouse:stripe")
    assert stripe.status == "failed"
    assert "503" in (stripe.error or "")
    # Retried: one attempt plus max_retries from the etiquette block.
    attempts = sum(count for called, count in web.calls.items() if called.startswith(url))
    assert attempts == 4
    # And everything else still ran.
    assert _result(report, "greenhouse:careem").status == "ok"
    obstacles = json.loads(
        db.execute("SELECT obstacles FROM run WHERE id = ?", (report.run_id,)).fetchone()[0]
    )
    assert any(o["source"] == "greenhouse:stripe" for o in obstacles)


def test_a_404_is_an_answer_and_is_not_retried(db: sqlite3.Connection, web: FakeWeb) -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/nuro/"
    web.overrides[url] = lambda _r: httpx.Response(404)

    report = _discover(db, web)

    assert _result(report, "greenhouse:nuro").status == "failed"
    attempts = sum(count for called, count in web.calls.items() if called.startswith(url))
    assert attempts == 1


def test_robots_disallow_is_a_refusal_and_nothing_is_fetched(
    db: sqlite3.Connection, web: FakeWeb
) -> None:
    web.robots["api.lever.co"] = "User-agent: *\nDisallow: /\n"

    report = _discover(db, web)

    for result in report.sources:
        if result.source_id.startswith("lever:"):
            assert result.status == "refused"
            assert "robots.txt" in (result.error or "")
    assert not any(
        called.startswith("https://api.lever.co/v0/") for called in web.calls
    ), "a disallowed path was requested anyway"


def test_a_page_that_is_not_json_is_kept_for_inspection(
    db: sqlite3.Connection, web: FakeWeb
) -> None:
    web.overrides["https://remoteok.com/api"] = lambda _r: httpx.Response(
        200, text="<html>We are down for maintenance</html>"
    )

    report = _discover(db, web)

    remoteok = _result(report, "remoteok")
    assert remoteok.status == "failed"
    assert "changed shape" in (remoteok.error or "")
    kept = [
        path
        for path in Path(snapshots.resolve("snapshots")).rglob("*.json")
        if not path.name.endswith(".meta.json")
        and b"maintenance" in path.read_bytes()
    ]
    assert kept, "the page that broke the parse should be on disk"


def test_every_source_failing_fails_the_run(db: sqlite3.Connection, web: FakeWeb) -> None:
    for prefix in (
        "https://remoteok.com/",
        "https://www.arbeitnow.com/",
        "https://boards-api.greenhouse.io/",
        "https://api.lever.co/",
        "https://himalayas.app/",
        "https://jobicy.com/",
    ):
        web.overrides[prefix] = lambda _r: httpx.Response(404)

    report = _discover(db, web)

    assert report.status == "failed"
    assert db.execute(
        "SELECT status FROM run WHERE id = ?", (report.run_id,)
    ).fetchone()["status"] == "failed"


def test_arbeitnow_stops_paging_at_the_high_water_mark(
    db: sqlite3.Connection, web: FakeWeb
) -> None:
    """The one source where the mark saves a request, because it pages newest-first."""
    old = _item("arbeitnow")
    old["created_at"] = 1700000000  # 2023-11-14
    pages = {
        "1": {"data": [old], "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"}},
        "2": {"data": [old], "links": {"next": None}},
    }

    def arbeitnow(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("page", "1")])

    web.overrides["https://www.arbeitnow.com/"] = arbeitnow
    countries.install(db)
    fetch = next(f for f in clients.plan(db) if f.source_id == "arbeitnow")

    with web.fetcher() as fetcher:
        without_mark = clients.fetch_pages(fetcher, fetch, sleep=lambda _s: None)
        with_mark = clients.fetch_pages(
            fetcher, fetch, high_water_mark="2026-01-01T00:00:00Z", sleep=lambda _s: None
        )

    assert len(without_mark) == 2
    assert len(with_mark) == 1


def test_a_disk_error_on_one_source_does_not_end_the_run(
    db: sqlite3.Connection, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by the first live run: an OSError escaped and failed every source."""
    real_store = snapshots.store

    def store(content, *, source_url, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if "remoteok" in source_url:
            raise OSError("No space left on device")
        return real_store(content, source_url=source_url, **kwargs)

    monkeypatch.setattr(clients.snapshots, "store", store)

    report = _discover(db, web)

    assert report.status == "partial"
    remoteok = _result(report, "remoteok")
    assert remoteok.status == "failed"
    assert "No space left on device" in (remoteok.error or "")
    assert _result(report, "greenhouse:careem").status == "ok"
