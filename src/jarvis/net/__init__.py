"""The single audited outbound access point.

Nothing else in Jarvis may open a socket. ``test_egress_chokepoint`` enforces
that at runtime by patching ``socket.socket.connect`` and rejecting any
connection whose call stack does not pass through this package.

Four rules, all applied here rather than at the call sites, so no caller can
forget one:

1. **A source marked ``manual_review_only`` is never fetched.** Sources without
   a usable API — LinkedIn, Indeed, Glassdoor — are surfaced to the user as
   places to search by hand. The refusal happens before the request is built.
2. **GET only, except on the enumerated send channels.** Discovery reads.
   Exactly two hosts may be written to, and both are Google APIs acting on the
   user's own mailbox and Drive.
3. **Credentials go to send channels and nowhere else.** An ``Authorization``
   header aimed at a job board is a bug that leaks the user's Google token to a
   third party, so it raises instead of sending.
4. **Every request identifies itself, is rate-limited per host, and honours
   ``robots.txt``.**
"""

from __future__ import annotations

import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from jarvis import __version__

USER_AGENT: Final = (
    f"JarvisAgent/{__version__} (+local-first job search assistant; "
    f"operated by the account holder on their own machine)"
)

#: The only hosts that may receive a non-GET request, and the only hosts a
#: credential may be sent to. Both act on the user's own Google account.
SEND_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "gmail.googleapis.com",
        "www.googleapis.com",
        "oauth2.googleapis.com",
        "accounts.google.com",
    }
)

CREDENTIAL_HEADERS: Final[frozenset[str]] = frozenset(
    {"authorization", "x-goog-api-key", "cookie", "proxy-authorization"}
)

DEFAULT_TIMEOUT: Final = httpx.Timeout(20.0, connect=10.0)

#: Re-exported so callers can catch a transport failure and type a response
#: without importing the HTTP library themselves. The tripwire in
#: ``test_egress_chokepoint`` refuses any sibling that does, because a module
#: that can import ``httpx`` can build its own client and bypass every guard here.
HTTPError = httpx.HTTPError
Response = httpx.Response


class NetworkRefusal(Exception):
    """Base class for a request this module declined to make."""


class SourceNotFetchable(NetworkRefusal):
    """The source's terms forbid automated access."""


class MethodNotAllowed(NetworkRefusal):
    """A write was attempted against a host that may only be read."""


class CredentialLeak(NetworkRefusal):
    """A credential was about to be sent to a host that should never see one."""


class RobotsDisallowed(NetworkRefusal):
    """``robots.txt`` disallows this path for our user agent."""


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# ---------------------------------------------------------------- guards
# Each is a pure function so it can be tested, and called by `request` so it
# cannot be skipped.


def check_fetch_allowed(url: str, access_mode: str) -> None:
    """Refuse a source whose terms do not permit automated access."""
    if access_mode == "manual_review_only":
        raise SourceNotFetchable(
            f"{host_of(url)} is registered manual_review_only; it is surfaced to the "
            f"user as a manual search suggestion and is never fetched"
        )
    if access_mode not in {"api", "feed"}:
        raise SourceNotFetchable(f"unknown access_mode {access_mode!r} for {host_of(url)}")


def check_method_allowed(method: str, url: str) -> None:
    """GET and HEAD anywhere permitted; anything else only on a send host."""
    if method.upper() in {"GET", "HEAD"}:
        return
    if host_of(url) not in SEND_HOSTS:
        raise MethodNotAllowed(
            f"{method.upper()} to {host_of(url)} is refused; only {sorted(SEND_HOSTS)} "
            f"may receive a write, and only on the user's own account"
        )


def check_headers_allowed(url: str, headers: dict[str, str] | None) -> None:
    """Refuse to carry a credential anywhere but a send host."""
    if not headers:
        return
    if host_of(url) in SEND_HOSTS:
        return
    for name in headers:
        if name.lower() in CREDENTIAL_HEADERS:
            raise CredentialLeak(
                f"refusing to send {name!r} to {host_of(url)}; credentials are carried "
                f"only to {sorted(SEND_HOSTS)}"
            )


# ----------------------------------------------------------- rate limiting


@dataclass
class _HostLimiter:
    """Minimum spacing between requests to one host."""

    min_interval: float
    last_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    sleep: Any = time.sleep

    def wait(self) -> None:
        with self.lock:
            gap = time.monotonic() - self.last_at
            if gap < self.min_interval:
                self.sleep(self.min_interval - gap)
            self.last_at = time.monotonic()


class Fetcher:
    """The only HTTP client in the process.

    Construct one per run. ``robots_cache`` is per-instance so a long-lived
    process does not act on a stale policy forever.
    """

    def __init__(
        self,
        *,
        default_rate_per_minute: int = 20,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        """``transport`` and ``sleep`` exist for tests and nothing else.

        A test hands in an ``httpx.MockTransport``, so every guard, the robots
        check and the rate limiter run exactly as they do in production while no
        socket is opened; and a no-op ``sleep``, so the per-host spacing is still
        computed but not waited out. Production passes neither.
        """
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            transport=transport,
        )
        self._sleep = sleep
        self._limiters: dict[str, _HostLimiter] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._default_interval = 60.0 / max(default_rate_per_minute, 1)
        self._respect_robots = respect_robots
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- policy ---------------------------------------------------------

    def _limiter(self, host: str, rate_per_minute: int | None) -> _HostLimiter:
        with self._lock:
            limiter = self._limiters.get(host)
            if limiter is None:
                interval = 60.0 / rate_per_minute if rate_per_minute else self._default_interval
                limiter = _HostLimiter(min_interval=interval, sleep=self._sleep)
                self._limiters[host] = limiter
            return limiter

    def _robots_allows(self, url: str) -> bool:
        """Fetch and cache ``robots.txt`` for the host, failing open on error.

        Failing open is deliberate and narrow: a host that cannot serve
        ``robots.txt`` has not disallowed anything, and treating a 500 as a
        prohibition would make discovery depend on an unrelated endpoint's
        health. Every other guard still applies.
        """
        host = host_of(url)
        parsed = urlparse(url)
        if host not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            try:
                response = self._client.get(robots_url, timeout=httpx.Timeout(10.0))
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    parser = None  # type: ignore[assignment]
            except httpx.HTTPError:
                parser = None  # type: ignore[assignment]
            self._robots[host] = parser

        parser = self._robots[host]
        if parser is None:
            return True
        return parser.can_fetch(USER_AGENT, url)

    # -- the one way out ------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        access_mode: str = "api",
        rate_per_minute: int | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a request, or refuse it and say why.

        Every guard runs before the socket is touched, so a refusal costs
        nothing and leaves no trace on the far end.
        """
        check_fetch_allowed(url, access_mode)
        check_method_allowed(method, url)
        check_headers_allowed(url, headers)

        if (
            self._respect_robots
            and method.upper() in {"GET", "HEAD"}
            and not self._robots_allows(url)
        ):
            raise RobotsDisallowed(f"robots.txt disallows {url} for {USER_AGENT}")

        self._limiter(host_of(url), rate_per_minute).wait()
        return self._client.request(method, url, headers=headers, **kwargs)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """Only reachable for the enumerated send hosts; see :func:`check_method_allowed`."""
        return self.request("POST", url, **kwargs)


__all__ = [
    "SEND_HOSTS",
    "HTTPError",
    "Response",
    "USER_AGENT",
    "CredentialLeak",
    "Fetcher",
    "MethodNotAllowed",
    "NetworkRefusal",
    "RobotsDisallowed",
    "SourceNotFetchable",
    "check_fetch_allowed",
    "check_headers_allowed",
    "check_method_allowed",
    "host_of",
]
