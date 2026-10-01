"""Talking to Gmail and Drive through the audited chokepoint — T056, T059, T061.

Every call here goes through :class:`jarvis.net.Fetcher`. The Google client
libraries are deliberately *not* used for this: ``google-api-python-client``
carries its own HTTP transport, which would open sockets outside ``jarvis.net``
and put the send path beyond the egress invariant (I-08). Gmail's and Drive's
REST APIs are small enough to call directly.

**Token handling (T059).** The token file is written ``chmod 600`` and never
enters the database. An access token is refreshed shortly before it expires.
When Google refuses the refresh — revoked, or a Testing-mode project's 7-day
expiry — the credential is marked ``needs_renewal`` and
:class:`MailboxDisconnected` is raised naming Google as the cause. The user sees
"reconnect your mailbox", never an empty inbox: silence about replies is exactly
what an expired token looks like otherwise, and it is indistinguishable from a
quiet job market.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from jarvis import net
from jarvis.store.paths import Paths, get_paths, harden_file

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret

#: Google's API quota is far above this; the spacing is courtesy and keeps a
#: first poll of a large mailbox from looking like a burst.
RATE_PER_MINUTE = 240


class MailboxDisconnected(Exception):
    """Google no longer accepts this connection. The user must reconnect.

    ``by_google=False`` is for a cause on this machine — the token file is
    missing — so the message does not blame Google for something it did not do.
    """

    def __init__(self, detail: str = "", *, by_google: bool = True) -> None:
        cause = ("Google has disconnected your mailbox" if by_google
                 else "There is no Google connection on this computer")
        super().__init__(
            f"{cause} — reconnect it with `jarvis setup google`. Until then no replies "
            f"can be read and nothing can be sent. "
            + (f"({'Google said' if by_google else 'detail'}: {detail})" if detail else "")
        )


class GoogleError(Exception):
    """Google answered with an error that is not a disconnection."""


def _parse_expiry(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class GoogleClient:
    """One user's Gmail and Drive, over REST, through ``jarvis.net``."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        credential_id: str,
        *,
        fetcher: net.Fetcher | None = None,
        paths: Paths | None = None,
        now: Any = None,
    ) -> None:
        self._conn = conn
        self.credential_id = credential_id
        self._paths = paths or get_paths()
        # robots.txt governs crawling; an authenticated API call on the user's own
        # account is not a crawl. Every other guard in Fetcher still applies.
        self._fetcher = fetcher or net.Fetcher(respect_robots=False)
        self._now = now or (lambda: datetime.now(UTC))
        self._token: dict[str, Any] | None = None

    # ---------------------------------------------------------------- tokens

    def _load(self) -> dict[str, Any]:
        if self._token is None:
            path = self._paths.google_token
            if not path.exists():
                raise MailboxDisconnected(f"no token file at {path}", by_google=False)
            self._token = json.loads(path.read_text(encoding="utf-8"))
        return self._token

    def _save(self) -> None:
        path = self._paths.google_token
        path.write_text(json.dumps(self._token), encoding="utf-8")
        harden_file(path)

    def _mark_disconnected(self) -> None:
        self._conn.execute(
            "UPDATE credential SET needs_renewal = 1 WHERE id = ?", (self.credential_id,)
        )

    def refresh(self) -> None:
        token = self._load()
        if not token.get("refresh_token"):
            self._mark_disconnected()
            raise MailboxDisconnected("the stored token has no refresh token")
        response = self._fetcher.post(
            token.get("token_uri") or TOKEN_URI,
            data={
                "client_id": token.get("client_id", ""),
                "client_secret": token.get("client_secret", ""),
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token",
            },
            rate_per_minute=RATE_PER_MINUTE,
        )
        if response.status_code in (400, 401):
            body = _json(response)
            if body.get("error") in {"invalid_grant", "unauthorized_client", "invalid_client"}:
                self._mark_disconnected()
                raise MailboxDisconnected(body.get("error_description") or body["error"])
        if response.status_code != 200:
            raise GoogleError(f"token refresh failed: HTTP {response.status_code}")
        body = _json(response)
        token["token"] = body["access_token"]
        token["expiry"] = (
            self._now() + timedelta(seconds=int(body.get("expires_in", 3600)))
        ).isoformat().replace("+00:00", "Z")
        self._save()

    def ensure_token(self) -> None:
        """Refresh now if needed, so a disconnection is found before a send is reserved."""
        self._access_token()

    def _access_token(self) -> str:
        token = self._load()
        expiry = _parse_expiry(token.get("expiry"))
        soon = self._now() + timedelta(seconds=60)
        if not token.get("token") or expiry is None or expiry <= soon:
            self.refresh()
        return str(self._load()["token"])

    # ---------------------------------------------------------------- calls

    def call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """One authenticated request. A 401 refreshes once and retries.

        Retrying after a 401 is safe for a send: Google refused the request
        before acting on it, so nothing was delivered the first time.
        """
        extra = kwargs.pop("headers", {})
        for attempt in (1, 2):
            headers = {**extra, "Authorization": f"Bearer {self._access_token()}"}
            response = self._fetcher.request(
                method, url, headers=headers, rate_per_minute=RATE_PER_MINUTE, **kwargs
            )
            if response.status_code == 401 and attempt == 1:
                self.refresh()
                continue
            if response.status_code == 401:
                self._mark_disconnected()
                raise MailboxDisconnected("the access token was refused twice")
            if response.status_code >= 400:
                raise GoogleError(
                    f"{method} {url.split('?')[0]} -> HTTP {response.status_code}: "
                    f"{_json(response).get('error', {})}"
                )
            return _json(response)
        raise GoogleError("unreachable")  # pragma: no cover

    def get(self, url: str, **params: Any) -> dict[str, Any]:
        return self.call("GET", url, params=params or None)

    def post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.call("POST", url, json=body)


def label_id(client: GoogleClient, name: str) -> str:
    """The id of a Gmail label, creating it if the mailbox does not have it yet."""
    for label in client.get(f"{GMAIL}/labels").get("labels", []):
        if label.get("name") == name:
            return str(label["id"])
    created = client.post(f"{GMAIL}/labels", {
        "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
    })
    return str(created["id"])


def _json(response: net.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def mailbox_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the UI, doctor and health say about the mailbox. Never silent.

    ``needs_renewal`` is reported as a Google disconnection with the action to
    take — not as "0 new replies", which would read as a quiet week.
    """
    row = conn.execute(
        "SELECT c.id, c.account_label, c.needs_renewal, c.connected_at, "
        "m.last_polled_at, m.last_status, m.last_error FROM credential c "
        "LEFT JOIN mailbox_state m ON m.credential_id = c.id "
        "WHERE c.provider = 'google' AND c.revoked_at IS NULL "
        "ORDER BY c.connected_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"connected": False, "state": "not_connected",
                "message": "No mailbox connected. Run `jarvis setup google`."}
    if row["needs_renewal"] or row["last_status"] == "disconnected":
        return {"connected": False, "state": "reconnect", "account": row["account_label"],
                "message": "Google disconnected your mailbox — reconnect it with "
                           "`jarvis setup google`. Replies are NOT being read until you do.",
                "last_polled_at": row["last_polled_at"]}
    return {"connected": True, "state": "ok", "account": row["account_label"],
            "credential_id": row["id"], "last_polled_at": row["last_polled_at"],
            "last_status": row["last_status"], "last_error": row["last_error"]}
