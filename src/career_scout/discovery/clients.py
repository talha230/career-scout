"""Fetching postings from the registered sources — T030d (fetch half).

Ingest (:mod:`career_scout.discovery.ingest`) is pure and already tested. This module
is the other half: it turns the enabled country packs into a list of requests,
makes them through :class:`career_scout.net.Fetcher` — the only socket owner in the
process — stores every page it fetched, maps each item onto a
:class:`~career_scout.discovery.ingest.RawPosting`, and hands the batch to ingest.

    plan(conn)                       -> the requests the enabled packs ask for
    discover(conn, fetcher)          -> fetch, snapshot, ingest; one ``run`` row
    to_raw(provider, item, ...)      -> RawPosting | None   (pure, per provider)

Five decisions shape it.

**One source failing never fails the run.** A board that returns a 500, times
out, stops being JSON or is disallowed by ``robots.txt`` becomes an *obstacle*
on the run row, with its reason, and every other source still runs. The run is
``partial`` rather than ``failed`` unless nothing succeeded at all. A run that
dies on the first flaky board produces no error anyone will read at 7am.

**Retries are GET-only and live here, not in the chokepoint.** ``Fetcher`` also
carries the Gmail send path, and a retried POST to ``gmail.send`` is a duplicate
email. Discovery reads, so it may retry; the counts and backoff come from the
etiquette block in ``sources.json``, not from constants here.

**Every fetched page is snapshotted before it is interpreted.** The posting's own
URL is not evidence — postings are taken down — and the page as it was fetched is
what every requirement was parsed from. Each stored opportunity names its page.

**Only an explicit decision drops a posting.** A board fetched because the US pack
asked for it also posts in Sweden, Brazil and nowhere in particular. A posting is
skipped only when it resolves to a country whose pack is switched **off**, and it
is counted as ``out_of_scope`` with its country rather than vanishing. A posting
in a country with no pack, or in no resolvable country, is kept: leaving it out
would narrow the search without anybody having decided to.

**The high-water mark is honest about what it saves.** It is the newest
``posted_at`` seen per source. For ``arbeitnow``, which pages newest-first, it
stops paging once a whole page is older — a real saving. The other four return
their whole board in one response, so there is nothing to skip at the network
level, and re-seeing a known posting is not waste: it is the sighting that moves
``last_seen_at``, which is how a posting that disappears is eventually noticed.
"""

from __future__ import annotations

import collections
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from career_scout import config as config_module
from career_scout import net
from career_scout.discovery import countries, ingest, normalize
from career_scout.store import snapshots
from career_scout.store.db import transaction, utcnow

#: Providers this module can read. A source in ``sources.json`` whose provider is
#: not here is reported as an obstacle, never guessed at.
PROVIDERS = ("remoteok", "arbeitnow", "greenhouse", "lever", "workable", "himalayas", "jobicy")

#: HTTP statuses worth a retry. A 404 is an answer, not a hiccup: the board has
#: gone, and retrying it three times only delays saying so.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class SourceFailed(Exception):
    """One source could not be read. Carried into the run's obstacles."""


# ------------------------------------------------------------------ planning


@dataclass(frozen=True, slots=True)
class Fetch:
    """One request the enabled packs have asked for."""

    source_id: str
    provider: str
    url: str
    company: str | None
    asked_by: tuple[str, ...]
    rate_per_minute: int
    max_pages: int = 1


def _etiquette(version: str) -> dict[str, Any]:
    return config_module.sources(version).get("_meta", {}).get("etiquette", {})


def _rate_per_minute(version: str) -> int:
    seconds = float(_etiquette(version).get("min_seconds_between_requests", 1.0))
    return max(1, int(60 / seconds)) if seconds > 0 else 60


def plan(conn: sqlite3.Connection, version: str = config_module.CURRENT_VERSION) -> list[Fetch]:
    """The requests the enabled packs ask for, one per source or board.

    Driven by :func:`countries.sources_for`, so a source no enabled pack names is
    never fetched "just in case", and a disabled board or aggregator in
    ``sources.json`` is skipped even when a pack names its provider.
    """
    registry = config_module.sources(version)
    wanted = countries.sources_for(conn, version)
    rate = _rate_per_minute(version)
    fetches: list[Fetch] = []

    for aggregator in registry.get("aggregators", []):
        name = aggregator["name"]
        if name not in wanted or not aggregator.get("enabled"):
            continue
        fetches.append(
            Fetch(
                source_id=name,
                provider=aggregator["provider"],
                url=aggregator["url"],
                company=None,
                asked_by=wanted[name],
                rate_per_minute=rate,
                max_pages=int(aggregator.get("max_pages", 3)) if aggregator.get("paginated") else 1,
            )
        )

    for provider, body in registry.get("employer_boards", {}).items():
        if provider not in wanted or not body.get("enabled"):
            continue
        for board in body.get("boards", []):
            if not board.get("enabled", True):
                continue
            fetches.append(
                Fetch(
                    source_id=f"{provider}:{board['token']}",
                    provider=body["provider"],
                    url=body["url_template"].format(token=board["token"]),
                    company=board["company"],
                    asked_by=wanted[provider],
                    rate_per_minute=rate,
                )
            )

    return fetches


def register_sources(
    conn: sqlite3.Connection,
    fetches: Iterable[Fetch],
    version: str = config_module.CURRENT_VERSION,
) -> None:
    """A ``source`` row for every planned fetch.

    ``opportunity.source_id`` is a foreign key into ``source``, and nothing in
    production wrote those rows — the ingest tests seed their own. Existing rows
    keep their ``high_water_mark``.
    """
    basis = config_module.sources(version).get("_meta", {}).get(
        "compliance_rule", "official or public API registered in sources.json"
    )
    for fetch in fetches:
        conn.execute(
            "INSERT INTO source (id, name, provider, base_url, access_mode, legal_basis, "
            "rate_limit_per_minute) VALUES (?, ?, ?, ?, 'api', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET base_url = excluded.base_url, "
            "rate_limit_per_minute = excluded.rate_limit_per_minute",
            (
                fetch.source_id,
                fetch.company or fetch.source_id,
                fetch.provider,
                fetch.url,
                basis,
                fetch.rate_per_minute,
            ),
        )


# ------------------------------------------------------------------ fetching


@dataclass(slots=True)
class Page:
    """One fetched response: the parsed JSON and where its bytes were stored."""

    url: str
    data: Any
    snapshot_path: str
    fetched_at: str


def _get(
    fetcher: net.Fetcher,
    fetch: Fetch,
    url: str,
    *,
    retries: int,
    backoff: list[float],
    sleep: Callable[[float], None],
) -> net.Response:
    """One GET through the chokepoint, retried on a transient status or error.

    A refusal from the chokepoint itself — ``robots.txt``, a manual-review source,
    a forbidden header — is not retried. It is a decision, not a failure.
    """
    last_error: str = ""
    for attempt in range(retries + 1):
        try:
            response = fetcher.get(
                url,
                access_mode="api",
                rate_per_minute=fetch.rate_per_minute,
                headers={"Accept": "application/json"},
            )
        except net.NetworkRefusal:
            raise
        except net.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code not in RETRY_STATUSES:
                return response
            last_error = f"HTTP {response.status_code}"

        if attempt < retries:
            sleep(backoff[min(attempt, len(backoff) - 1)] if backoff else 0)

    raise SourceFailed(f"{url} failed after {retries + 1} attempts: {last_error}")


def fetch_pages(
    fetcher: net.Fetcher,
    fetch: Fetch,
    *,
    high_water_mark: str | None = None,
    version: str = config_module.CURRENT_VERSION,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Page]:
    """Fetch every page this source needs, snapshotting each before reading it."""
    etiquette = _etiquette(version)
    retries = int(etiquette.get("max_retries", 3))
    backoff = [float(s) for s in etiquette.get("backoff_seconds", [2, 5, 15])]

    pages: list[Page] = []
    url: str | None = fetch.url
    for _ in range(max(fetch.max_pages, 1)):
        if not url:
            break

        response = _get(fetcher, fetch, url, retries=retries, backoff=backoff, sleep=sleep)
        if response.status_code != 200:
            raise SourceFailed(f"{url} answered HTTP {response.status_code}")

        fetched_at = utcnow()
        # Stored before it is parsed: if the parse fails, the page that broke it
        # is on disk to look at, rather than gone with the exception.
        snapshot = snapshots.store(
            response.content, source_url=url, fetched_at=fetched_at, suffix=".json"
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceFailed(
                f"{url} returned something that is not JSON ({exc}); the page is kept "
                f"at {snapshot.relative_path}. The source may have changed shape."
            ) from exc

        pages.append(Page(url, data, snapshot.relative_path, fetched_at))

        if fetch.provider != "arbeitnow":
            break
        if high_water_mark and _page_entirely_older(data, high_water_mark):
            break
        url = ((data.get("links") or {}).get("next")) if isinstance(data, dict) else None

    return pages


def _page_entirely_older(data: Any, high_water_mark: str) -> bool:
    """Whether every dated item on an arbeitnow page predates the mark."""
    items = data.get("data", []) if isinstance(data, dict) else []
    dates = [normalize.parse_datetime(item.get("created_at")) for item in items]
    known = [d for d in dates if d is not None]
    mark = normalize.parse_datetime(high_water_mark)
    if not known or mark is None or len(known) < len(items):
        # An undated item could be anything; keep paging rather than guess.
        return False
    return all(d < mark for d in known)


# ------------------------------------------------------------------ mapping


def items_of(provider: str, data: Any) -> list[dict[str, Any]]:
    """The postings inside one page, per provider's envelope."""
    if provider == "remoteok":
        if not isinstance(data, list):
            return []
        # Index 0 is RemoteOK's legal notice. Read as a posting it has no
        # employer and no title: a phantom row in a system built on no phantoms.
        return [item for item in data[1:] if isinstance(item, dict) and item.get("id")]
    if provider == "arbeitnow":
        items = data.get("data", []) if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict) and item.get("slug")]
    if provider in {"greenhouse", "workable"}:
        items = data.get("jobs", []) if isinstance(data, dict) else []
        return [
            item
            for item in items
            if isinstance(item, dict) and (item.get("id") or item.get("shortcode"))
        ]
    if provider == "himalayas":
        items = data.get("jobs", []) if isinstance(data, dict) else []
        return [i for i in items
                if isinstance(i, dict) and (i.get("guid") or i.get("applicationLink"))]
    if provider == "jobicy":
        items = data.get("jobs", []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict) and i.get("id") and i.get("url")]
    if provider == "lever":
        return [item for item in data if isinstance(item, dict) and item.get("id")] if isinstance(
            data, list
        ) else []
    return []


def _date(value: Any) -> str | None:
    """A source date as the one stored format: ISO-8601 **UTC**.

    Converted before formatting. Greenhouse publishes ``-04:00`` offsets, and
    formatting that datetime with a literal ``Z`` stamps New York local time as
    UTC — four hours wrong, on the field the staleness filter and the
    high-water mark both compare.
    """
    parsed = normalize.parse_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_raw(
    provider: str,
    item: dict[str, Any],
    *,
    source_id: str,
    company: str | None,
    fetched_at: str,
    snapshot_path: str | None,
) -> ingest.RawPosting | None:
    """Map one provider item onto a RawPosting, or ``None`` if it has no URL.

    ``None`` only for a posting that cannot be traced or applied to. A posting
    with a URL but no title or employer is still returned: ingest stores it and
    the F10 filter rejects it with a row saying so, which is visible where a
    silent drop here would not be.
    """
    mapper = _MAPPERS.get(provider)
    if mapper is None:
        return None
    fields = mapper(item, company)
    if not fields.get("source_url"):
        return None
    return ingest.RawPosting(
        source_id=source_id,
        raw_payload=item,
        fetched_at=fetched_at,
        snapshot_path=snapshot_path,
        **fields,
    )


def _positive(value: Any) -> bool:
    """Whether a source's pay field holds a real figure. RemoteOK sends ``"0"``."""
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _remoteok(item: dict[str, Any], _company: str | None) -> dict[str, Any]:
    tags = [str(t) for t in item.get("tags") or []]
    # The string "0" is truthy, so a plain truthiness test labelled a salary nobody
    # published as annual USD. Only a positive figure carries a currency.
    has_pay = _positive(item.get("salary_min")) or _positive(item.get("salary_max"))
    return {
        "source_url": item.get("url") or item.get("apply_url") or "",
        "title": (item.get("position") or "").strip(),
        "employer": (item.get("company") or "").strip(),
        "location_raw": item.get("location") or None,
        "description": item.get("description"),
        "description_format": "html",
        # A remote-only board: the tag is the source's own statement, and it is
        # what the arrangement classifier's second tier reads.
        "tags": [*tags, "remote"],
        "posted_at": _date(item.get("date") or item.get("epoch")),
        "apply_target": item.get("apply_url") or item.get("url"),
        "apply_route": "portal",
        # RemoteOK's structured salary is annual USD.
        "pay_min": item.get("salary_min"),
        "pay_max": item.get("salary_max"),
        "pay_currency": "USD" if has_pay else None,
        "pay_period": "year" if has_pay else None,
    }


def _arbeitnow(item: dict[str, Any], _company: str | None) -> dict[str, Any]:
    job_types = [str(t) for t in item.get("job_types") or []]
    tags = [str(t) for t in item.get("tags") or []]
    if item.get("remote"):
        tags.append("remote")
    return {
        "source_url": item.get("url") or "",
        "title": (item.get("title") or "").strip(),
        "employer": (item.get("company_name") or "").strip(),
        "location_raw": item.get("location") or None,
        "description": item.get("description"),
        "description_format": "html",
        "employment_types": job_types,
        "tags": tags,
        "posted_at": _date(item.get("created_at")),
        "apply_target": item.get("url"),
        "apply_route": "portal",
    }


def _greenhouse(item: dict[str, Any], company: str | None) -> dict[str, Any]:
    offices = [o.get("name", "") for o in item.get("offices") or [] if isinstance(o, dict)]
    departments = [d.get("name", "") for d in item.get("departments") or [] if isinstance(d, dict)]
    place = item.get("location")
    location = place.get("name") if isinstance(place, dict) else None
    return {
        "source_url": item.get("absolute_url") or "",
        "title": (item.get("title") or "").strip(),
        "employer": (item.get("company_name") or company or "").strip(),
        "location_raw": location,
        # HTML-escaped HTML; normalize.strip_html unescapes twice.
        "description": item.get("content"),
        "description_format": "html",
        "tags": [*offices, *departments],
        "posted_at": _date(item.get("first_published") or item.get("updated_at")),
        "apply_target": item.get("absolute_url"),
        "apply_route": "portal",
    }


def _lever(item: dict[str, Any], company: str | None) -> dict[str, Any]:
    categories = item.get("categories") or {}
    description = item.get("descriptionPlain") or item.get("description") or ""
    for section in item.get("lists") or []:
        if isinstance(section, dict):
            description += f"\n{section.get('text', '')}\n{section.get('content', '')}"
    commitment = categories.get("commitment")
    salary = item.get("salaryRange") or {}
    interval = str(salary.get("interval") or "").replace("per-", "") or None
    return {
        "source_url": item.get("hostedUrl") or item.get("applyUrl") or "",
        "title": (item.get("text") or "").strip(),
        "employer": (company or "").strip(),
        "location_raw": categories.get("location"),
        "description": description,
        "description_format": "html",
        "employment_types": [commitment] if commitment else [],
        "tags": [v for v in categories.values() if isinstance(v, str)]
        + ([item["workplaceType"]] if isinstance(item.get("workplaceType"), str) else []),
        # Epoch MILLISECONDS; see normalize.parse_datetime.
        "posted_at": _date(item.get("createdAt")),
        "apply_target": item.get("applyUrl") or item.get("hostedUrl"),
        "apply_route": "portal",
        "pay_min": salary.get("min"),
        "pay_max": salary.get("max"),
        "pay_currency": salary.get("currency"),
        "pay_period": interval,
    }


def _workable(item: dict[str, Any], company: str | None) -> dict[str, Any]:
    bits = [item.get("city"), item.get("state"), item.get("country")]
    location = ", ".join(str(b) for b in bits if b) or item.get("location")
    tags = ["remote"] if item.get("telecommuting") else []
    return {
        "source_url": item.get("url") or item.get("application_url") or item.get("shortlink") or "",
        "title": (item.get("title") or "").strip(),
        "employer": (company or "").strip(),
        "location_raw": location,
        "description": item.get("description") or item.get("requirements"),
        "description_format": "html",
        "employment_types": [item["employment_type"]] if item.get("employment_type") else [],
        "tags": tags,
        "posted_at": _date(item.get("published_on") or item.get("created_at")),
        "apply_target": item.get("application_url") or item.get("url"),
        "apply_route": "portal",
    }


#: A source's own period words, onto the periods config/v1/money.json converts.
#: Anything else is stored as None, so the savings engine reports it rather than
#: reading an hourly rate as a salary.
_PERIODS = {"hourly": "hour", "hour": "hour", "daily": "day", "day": "day",
            "weekly": "week", "week": "week", "monthly": "month", "month": "month",
            "yearly": "year", "annual": "year", "annually": "year", "year": "year"}


def _himalayas(item: dict[str, Any], _company: str | None) -> dict[str, Any]:
    # Terms (himalayas.app/api, read 2026-09-25): free, no auth; link back to the
    # Himalayas URL and name Himalayas as the source; do not resubmit its jobs to
    # other boards; data refreshes daily. source_url is that link-back URL.
    link = item.get("applicationLink") or item.get("guid") or ""
    employment = item.get("employmentType")
    places = [str(p) for p in item.get("locationRestrictions") or []]
    return {
        "source_url": link,
        "title": (item.get("title") or "").strip(),
        "employer": (item.get("companyName") or "").strip(),
        "location_raw": ", ".join(places) if places else "Remote",
        "description": item.get("description"),
        "description_format": "html",
        "employment_types": [employment] if employment else [],
        "tags": [*(item.get("categories") or []), "remote"],
        "posted_at": _date(item.get("pubDate")),
        "apply_target": link,
        "apply_route": "portal",
        "pay_min": item.get("minSalary"),
        "pay_max": item.get("maxSalary"),
        "pay_currency": item.get("currency"),
        "pay_period": _PERIODS.get(str(item.get("salaryPeriod") or "").lower()),
    }


def _jobicy(item: dict[str, Any], _company: str | None) -> dict[str, Any]:
    # Terms (the API's own friendlyNotice): credit Jobicy with a direct link to
    # the source, and send applications to the original job URL in the feed.
    return {
        "source_url": item.get("url") or "",
        "title": (item.get("jobTitle") or "").strip(),
        "employer": (item.get("companyName") or "").strip(),
        "location_raw": item.get("jobGeo") or "Remote",
        "description": item.get("jobDescription"),
        "description_format": "html",
        "employment_types": [str(t) for t in item.get("jobType") or []],
        "tags": [*(item.get("jobIndustry") or []), "remote"],
        "posted_at": _date(item.get("pubDate")),
        "apply_target": item.get("url"),
        "apply_route": "portal",
        # Jobicy names these fields annual, and sends them only when disclosed.
        "pay_min": item.get("annualSalaryMin"),
        "pay_max": item.get("annualSalaryMax"),
        "pay_currency": item.get("salaryCurrency"),
        "pay_period": ("year" if item.get("annualSalaryMin") or item.get("annualSalaryMax")
                       else None),
    }


_MAPPERS: dict[str, Callable[[dict[str, Any], str | None], dict[str, Any]]] = {
    "himalayas": _himalayas,
    "jobicy": _jobicy,
    "remoteok": _remoteok,
    "arbeitnow": _arbeitnow,
    "greenhouse": _greenhouse,
    "lever": _lever,
    "workable": _workable,
}


# ------------------------------------------------------------------ the run


@dataclass(slots=True)
class SourceResult:
    """What one source produced, or why it produced nothing."""

    source_id: str
    status: str  # 'ok' | 'failed' | 'refused'
    pages: int = 0
    items: int = 0
    unmappable: int = 0
    out_of_scope: dict[str, int] = field(default_factory=dict)
    report: ingest.IngestReport | None = None
    newest_posted_at: str | None = None
    error: str | None = None

    def counts(self) -> dict[str, Any]:
        report = self.report or ingest.IngestReport()
        return {
            "status": self.status,
            "pages": self.pages,
            "items": self.items,
            "stored": report.stored,
            "sightings": report.sightings,
            "merged": report.merged,
            "rejected": report.rejected,
            "unmappable": self.unmappable,
            "out_of_scope": self.out_of_scope,
        }


@dataclass(slots=True)
class DiscoveryReport:
    """The whole run, as written to its ``run`` row."""

    run_id: str
    status: str
    sources: list[SourceResult]

    @property
    def obstacles(self) -> list[dict[str, str]]:
        return [
            {"source": result.source_id, "status": result.status, "reason": result.error or ""}
            for result in self.sources
            if result.status != "ok"
        ]

    def totals(self) -> dict[str, int]:
        total: collections.Counter[str] = collections.Counter()
        for result in self.sources:
            counts = result.counts()
            for key in ("items", "stored", "sightings", "merged", "rejected", "unmappable"):
                total[key] += counts[key]
            total["out_of_scope"] += sum(result.out_of_scope.values())
        return dict(total)


def discover(
    conn: sqlite3.Connection,
    fetcher: net.Fetcher,
    *,
    run_id: str | None = None,
    version: str = config_module.CURRENT_VERSION,
    sleep: Callable[[float], None] = time.sleep,
) -> DiscoveryReport:
    """Fetch every planned source, snapshot, ingest, and write one ``run`` row.

    The ``run`` row is written as ``running`` before the first request and closed
    whatever happens, so a run that crashes still leaves a row saying it started
    and did not finish — the liveness check in ``career-scout doctor`` reads that.
    """
    run_id = run_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO run (id, kind, started_at, status) VALUES (?, 'discover', ?, 'running')",
        (run_id, utcnow()),
    )

    results: list[SourceResult] = []
    status = "failed"
    error: str | None = None
    try:
        # Install first: plan() reads which countries are enabled, and on a fresh
        # data directory there are no country rows until the packs are installed.
        with transaction(conn):
            countries.install(conn, version)
        fetches = plan(conn, version)
        with transaction(conn):
            register_sources(conn, fetches, version)

        off = countries.switched_off(conn, version)
        for fetch in fetches:
            results.append(_one_source(conn, fetcher, fetch, off, run_id, version, sleep))

        succeeded = sum(1 for r in results if r.status == "ok")
        if not fetches:
            status, error = "failed", "no enabled country pack asks for any source"
        elif succeeded == len(results):
            status = "ok"
        elif succeeded:
            status = "partial"
        else:
            status = "failed"
    except Exception as exc:  # noqa: BLE001 - recorded on the run row, then re-raised
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report = DiscoveryReport(run_id=run_id, status=status, sources=results)
        conn.execute(
            "UPDATE run SET finished_at = ?, status = ?, counts = ?, obstacles = ?, error = ? "
            "WHERE id = ?",
            (
                utcnow(),
                status,
                json.dumps(
                    {
                        "totals": report.totals(),
                        "sources": {r.source_id: r.counts() for r in results},
                    }
                ),
                json.dumps(report.obstacles),
                error,
                run_id,
            ),
        )

    return report


def _one_source(
    conn: sqlite3.Connection,
    fetcher: net.Fetcher,
    fetch: Fetch,
    switched_off: frozenset[str],
    run_id: str,
    version: str,
    sleep: Callable[[float], None],
) -> SourceResult:
    """Fetch, map, scope and ingest one source. Never raises for a source fault."""
    result = SourceResult(source_id=fetch.source_id, status="ok")
    mark_row = conn.execute(
        "SELECT high_water_mark FROM source WHERE id = ?", (fetch.source_id,)
    ).fetchone()
    high_water_mark = mark_row["high_water_mark"] if mark_row else None

    try:
        pages = fetch_pages(
            fetcher, fetch, high_water_mark=high_water_mark, version=version, sleep=sleep
        )
    except net.NetworkRefusal as exc:
        result.status, result.error = "refused", str(exc)
        return result
    except (SourceFailed, net.HTTPError) as exc:
        result.status, result.error = "failed", str(exc)
        return result
    except OSError as exc:
        # Storing the snapshot failed: disk full, permissions, a path too long.
        # The first live run died here and took every remaining source with it.
        # It is this source's failure, reported with its cause; the next source
        # still runs, and if the disk is the problem they will all say so.
        result.status = "failed"
        result.error = f"could not store the fetched page: {type(exc).__name__}: {exc}"
        return result

    result.pages = len(pages)
    postings: list[ingest.RawPosting] = []
    out_of_scope: collections.Counter[str] = collections.Counter()

    for page in pages:
        for item in items_of(fetch.provider, page.data):
            result.items += 1
            raw = to_raw(
                fetch.provider,
                item,
                source_id=fetch.source_id,
                company=fetch.company,
                fetched_at=page.fetched_at,
                snapshot_path=page.snapshot_path,
            )
            if raw is None:
                result.unmappable += 1
                continue

            country = normalize.resolve_location(
                raw.location_raw, extra_signals=" ".join(raw.tags), version=version
            ).country_code
            if country and country in switched_off:
                out_of_scope[country] += 1
                continue

            postings.append(raw)
            if raw.posted_at and (
                result.newest_posted_at is None or raw.posted_at > result.newest_posted_at
            ):
                result.newest_posted_at = raw.posted_at

    result.out_of_scope = dict(out_of_scope)

    # One transaction per source: a malformed item that breaks ingest loses this
    # source's batch, not the whole run's, and never half a batch.
    try:
        with transaction(conn):
            result.report = ingest.ingest_many(conn, postings, run_id=run_id, version=version)
            if result.newest_posted_at and (
                high_water_mark is None or result.newest_posted_at > high_water_mark
            ):
                conn.execute(
                    "UPDATE source SET high_water_mark = ? WHERE id = ?",
                    (result.newest_posted_at, fetch.source_id),
                )
    except sqlite3.Error as exc:
        result.status = "failed"
        result.error = f"ingest failed and the batch was rolled back: {exc}"
        result.report = None

    return result
