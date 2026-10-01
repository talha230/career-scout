"""The guided Google connection — T057.

**Why the user creates their own OAuth client.** Google caps an unverified app
at 100 users, and that cap is lifted only by verification; for the restricted
``gmail.readonly`` scope, verification additionally requires an annual
third-party security assessment. A single shared client would therefore hit a
hard ceiling at exactly the scale this project targets.

Giving each user their own Google Cloud project moves the arithmetic: the cap
is *per project*, and each project has exactly one user. A hundred users is a
hundred projects of one. No verification, no assessment, no ceiling, no cost —
and the user's credentials never pass through anyone else's client.

**Why "In Production" and not "Testing".** Google's OAuth documentation is
explicit: a project with an external consent screen in ``Testing`` issues a
refresh token *expiring in 7 days*. Left in Testing, every mailbox connection
dies weekly and presents as "no replies arrived", which is indistinguishable
from a quiet job market. Publishing to In Production — while still unverified —
gives a refresh token that does not expire. The user sees an "unverified app"
interstitial once, for an app they created themselves.

**Scopes, at the minimum that works.**

``gmail.readonly``
    Restricted. Required to read replies. Requested always.
``gmail.send``
    Sensitive, not restricted. Requested only where auto-send is switched on.
``drive.file``
    Non-sensitive. Grants access only to files Jarvis itself creates, which is
    exactly what the application record needs and nothing more.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
import webbrowser
from typing import TYPE_CHECKING, Any

from jarvis.store import settings as settings_module
from jarvis.store.db import open_database, utcnow
from jarvis.store.paths import Paths, get_paths, harden_file

if TYPE_CHECKING:
    from rich.console import Console

SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.file"
SCOPE_PROFILE = "https://www.googleapis.com/auth/userinfo.email"

CONSOLE_URL = "https://console.cloud.google.com/projectcreate"

#: Created on first connect. Every application becomes a real, labelled thread
#: in the user's own mailbox, so the record survives Jarvis being uninstalled.
LABELS: tuple[str, ...] = (
    "Jarvis/Applied",
    "Jarvis/Reply",
    "Jarvis/Interview",
    "Jarvis/Offer",
    "Jarvis/Rejected",
    "Jarvis/Outreach",
)

WIZARD_STEPS = """\
[bold]1.[/bold]  Create a Google Cloud project (free, no card):
     {console_url}
     Name it anything — "jarvis" is fine.

[bold]2.[/bold]  Enable two APIs in that project:
     • Gmail API
     • Google Drive API
     APIs & Services → Library → search → Enable.

[bold]3.[/bold]  Configure the OAuth consent screen:
     • User type: [bold]External[/bold]
     • Fill in app name, your email, developer email
     • Add the scopes below when prompted (or skip; they are requested at
       connect time either way)
     • [bold red]Publish the app to "In Production"[/bold red]
       Leaving it in "Testing" makes Google expire your login every 7 days,
       and your reply tracking will silently stop. Unverified is fine —
       you will see a one-time "Google hasn't verified this app" screen for
       the app you just created yourself. Click Advanced → Continue.

[bold]4.[/bold]  Create the credential:
     APIs & Services → Credentials → Create credentials →
     [bold]OAuth client ID[/bold] → Application type: [bold]Desktop app[/bold]
     Download the JSON.

[bold]5.[/bold]  Save that file as:
     [bold]{client_secret}[/bold]

Then run [bold]jarvis setup google[/bold] again.
"""


class GoogleSetupError(RuntimeError):
    """Setup could not complete, with a reason the user can act on."""


def required_scopes(conn: sqlite3.Connection) -> list[str]:
    """The minimum set for this user's current opt-ins.

    ``gmail.send`` is requested only where auto-send is on, so a user who never
    switches it on never grants the platform the ability to send as them.
    """
    scopes = [SCOPE_PROFILE, SCOPE_READ, SCOPE_DRIVE]
    if settings_module.get(conn, "channel_email_autosend"):
        scopes.insert(1, SCOPE_SEND)
    return scopes


def run_setup_wizard(console: Console, *, open_browser: bool = True) -> None:
    """Walk the user through creating their own client, then connect."""
    paths = get_paths()
    conn = open_database(paths)
    try:
        if not paths.client_secret.exists():
            _print_instructions(console, paths)
            if open_browser:
                # A headless machine with no browser is not an error; the
                # instructions above already carry the URL.
                with contextlib.suppress(Exception):
                    webbrowser.open(CONSOLE_URL)
            return

        _validate_client_secret(paths.client_secret)
        scopes = required_scopes(conn)
        console.print("Opening your browser to authorise these scopes:")
        for scope in scopes:
            console.print(f"  - {scope}")
        console.print(
            "\n[dim]You will see 'Google hasn't verified this app'. That is your own "
            "app, unverified by design — click Advanced, then Continue.[/dim]\n"
        )

        authorise(paths, scopes)
        # Everything after the consent flow goes through jarvis.net (I-08).
        from jarvis.gmail.google import GoogleClient

        pending = record_credential(conn, "pending", scopes, paths)
        client = GoogleClient(conn, pending, paths=paths)
        email = client.get("https://www.googleapis.com/oauth2/v2/userinfo").get("email", "unknown")
        conn.execute("UPDATE credential SET account_label = ? WHERE id = ?", (email, pending))
        created = ensure_labels(client)

        console.print(f"[green]Connected {email}.[/green]")
        if created:
            console.print(f"Created Gmail labels: {', '.join(created)}")
        console.print(
            "\nYour applications will appear as labelled threads in your own mailbox. "
            "If you ever uninstall Jarvis, that record stays with you."
        )
    finally:
        conn.close()


def _print_instructions(console: Console, paths: Paths) -> None:
    from rich.panel import Panel

    console.print(
        Panel(
            WIZARD_STEPS.format(
                console_url=CONSOLE_URL,
                client_secret=paths.client_secret,
            ),
            title="Connect your own Google account",
            border_style="cyan",
        )
    )
    console.print(
        "[dim]Why your own client: Google caps an unverified app at 100 users, per "
        "project. Your project has one user — you — so the cap never applies, there "
        "is nothing to verify, and nothing to pay.[/dim]"
    )


def _validate_client_secret(path: Any) -> None:
    """Fail early and specifically on the two mistakes people actually make."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleSetupError(f"{path} is not readable JSON: {exc}") from exc

    if "web" in data:
        raise GoogleSetupError(
            f"{path} is a *Web application* client. Jarvis runs on your own machine "
            f"and needs a *Desktop app* client. Create a new one and download it again."
        )
    if "installed" not in data:
        raise GoogleSetupError(
            f"{path} does not look like an OAuth client download. Expected a key "
            f"'installed' (Desktop app)."
        )


def authorise(paths: Paths, scopes: list[str]) -> Any:
    """Run the local loopback consent flow and persist the token.

    ``InstalledAppFlow`` binds a short-lived loopback server on 127.0.0.1 and
    Google redirects to it. Nothing is exposed beyond the machine.

    **The one call outside jarvis.net.** The consent flow's code-for-token
    exchange happens inside ``google_auth_oauthlib``, once, at setup, by hand.
    Every later call — userinfo, labels, send, poll, refresh, Drive — goes
    through :class:`jarvis.gmail.google.GoogleClient` and the chokepoint.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise GoogleSetupError(
            "Google libraries are not installed. Run: pip install 'jarvis-agent'"
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(str(paths.client_secret), scopes)
    credentials = flow.run_local_server(port=0, prompt="consent", open_browser=True)

    paths.google_token.write_text(credentials.to_json(), encoding="utf-8")
    harden_file(paths.google_token)
    return credentials


def record_credential(
    conn: sqlite3.Connection, email: str, scopes: list[str], paths: Paths
) -> str:
    """Record the connection. The token itself never enters the database."""
    credential_id = str(uuid.uuid4())
    conn.execute("UPDATE credential SET revoked_at = ? WHERE provider = 'google' "
                 "AND revoked_at IS NULL", (utcnow(),))
    conn.execute(
        "INSERT INTO credential (id, provider, account_label, scopes, token_path, "
        "connected_at, needs_renewal) VALUES (?, 'google', ?, ?, ?, ?, 0)",
        (credential_id, email, json.dumps(scopes), str(paths.google_token), utcnow()),
    )
    return credential_id


def ensure_labels(client: Any) -> list[str]:
    """Create the Jarvis label tree in the user's mailbox if it is absent."""
    from jarvis.gmail.google import GMAIL

    existing = {label["name"] for label in client.get(f"{GMAIL}/labels").get("labels", [])}
    created: list[str] = []
    for name in LABELS:
        if name in existing:
            continue
        client.post(f"{GMAIL}/labels", {"name": name, "labelListVisibility": "labelShow",
                                        "messageListVisibility": "show"})
        created.append(name)
    return created
