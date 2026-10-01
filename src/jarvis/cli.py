"""``jarvis`` — the command line.

Six commands, and each one is a thin shell over a module that the web app and
the MCP server call identically. Nothing is implemented here that another
caller could not reach.

    jarvis setup      guided first run: data directory, Google client, profile
    jarvis doctor     what is configured, what is missing, what is stale
    jarvis serve      the local web app at http://localhost:8765
    jarvis run        one pipeline pass
    jarvis mcp        stdio MCP server (Claude Code / Codex launch this)
    jarvis backup     encrypted snapshot, and `restore` to bring one back
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jarvis import __version__
from jarvis.store import paths as paths_module
from jarvis.store import settings as settings_module
from jarvis.store.db import open_database

app = typer.Typer(
    name="jarvis",
    help="Local-first job and scholarship agent. Your data stays on your machine.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"jarvis {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", callback=_version_callback, is_eager=True,
                     help="Show the version and exit."),
    ] = False,
) -> None:
    """Local-first job and scholarship agent."""


# ------------------------------------------------------------------ doctor


@app.command()
def doctor() -> None:
    """Report what is configured, what is missing, and what is stale.

    Exits non-zero when something is wrong, so it is usable in a scheduled
    check as well as by hand.
    """
    paths = paths_module.get_paths()
    problems: list[str] = []

    table = Table(title="Jarvis", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail", overflow="fold")

    table.add_row("version", "ok", __version__)
    table.add_row("python", "ok", sys.version.split()[0])
    table.add_row("data directory", "ok", str(paths.root))

    # Database
    try:
        conn = open_database(paths)
    except Exception as exc:  # noqa: BLE001 - doctor reports rather than raises
        table.add_row("database", "FAIL", str(exc))
        console.print(table)
        raise typer.Exit(1) from exc

    counts = {
        name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]  # noqa: S608
        for name in ("opportunity", "assessment", "application", "profile_record", "document")
    }
    table.add_row("database", "ok", ", ".join(f"{k}={v}" for k, v in counts.items()))

    # Credentials — presence only. Never the contents.
    if paths.client_secret.exists():
        table.add_row("google client", "ok", str(paths.client_secret))
    else:
        table.add_row("google client", "missing", "run: jarvis setup google")
        problems.append("no Google OAuth client")

    cred = conn.execute(
        "SELECT account_label, needs_renewal, revoked_at FROM credential "
        "WHERE provider = 'google' ORDER BY connected_at DESC LIMIT 1"
    ).fetchone()
    if cred is None:
        table.add_row("gmail", "not connected", "run: jarvis setup google")
        problems.append("Gmail not connected")
    elif cred["revoked_at"]:
        table.add_row("gmail", "revoked", "reconnect your mailbox")
        problems.append("Gmail credential revoked")
    elif cred["needs_renewal"]:
        table.add_row("gmail", "needs renewal", f"{cred['account_label']} — reconnect your mailbox")
        problems.append("Gmail credential needs renewal")
    else:
        table.add_row("gmail", "ok", cred["account_label"] or "connected")

    # Liveness — a run that never happened produces no error of its own.
    last = conn.execute(
        "SELECT started_at, status FROM run WHERE status = 'ok' "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if last is None:
        table.add_row("last run", "never", "run: jarvis run")
        problems.append("no successful run yet")
    else:
        table.add_row("last run", "ok", last["started_at"])

    # Backups — one that has never been restored is not a backup.
    backup = conn.execute(
        "SELECT taken_at, status, restored_at FROM backup_record "
        "ORDER BY taken_at DESC LIMIT 1"
    ).fetchone()
    if backup is None:
        table.add_row("backup", "none", "run: jarvis backup")
        problems.append("no backup taken")
    elif backup["status"] != "ok":
        table.add_row("backup", "FAILED", backup["taken_at"])
        problems.append("most recent backup failed")
    else:
        drill = backup["restored_at"] or "never restored"
        table.add_row("backup", "ok", f"{backup['taken_at']} ({drill})")
        if not backup["restored_at"]:
            problems.append("backup has never been restore-tested")

    # Config. Both validators have always said doctor runs them; until T031 it
    # did not, so a weight table that failed to sum to 1 was reported nowhere.
    from jarvis import config as config_module
    from jarvis.discovery import countries

    config_problems = config_module.validate() + countries.validate()
    if config_problems:
        table.add_row("config", "FAIL", "; ".join(config_problems))
        problems.extend(config_problems)
    else:
        table.add_row("config", "ok", f"fingerprint {config_module.fingerprint()}")

    enabled = countries.enabled_codes(conn)
    if enabled:
        table.add_row("countries", "ok", ", ".join(enabled))
    else:
        table.add_row("countries", "none", "run: jarvis setup countries")
        problems.append("no country packs installed, so discovery would search nothing")

    # AI is optional by design: absent is a state, not a fault.
    provider = settings_module.get(conn, "ai_provider")
    table.add_row(
        "ai",
        "ok" if provider else "host only",
        provider or "driven by Claude Code / Codex; deterministic when unattended",
    )

    conn.close()
    console.print(table)

    if problems:
        console.print(Panel("\n".join(f"- {p}" for p in problems), title="needs attention",
                            border_style="yellow"))
        raise typer.Exit(1)
    console.print("[green]All checks passed.[/green]")


# --------------------------------------------------------------- discover


@app.command("discover")
def discover() -> None:
    """Fetch postings from every source the enabled country packs ask for.

    Read-only GETs through the audited network chokepoint, one request per second
    per host, ``robots.txt`` honoured. Every fetched page is stored under
    ``snapshots/``, and one ``run`` row records what each source produced — or why
    it produced nothing. A source that fails does not stop the others.
    """
    from jarvis import net
    from jarvis.discovery import clients

    conn = open_database(paths_module.get_paths())
    try:
        with net.Fetcher() as fetcher:
            report = clients.discover(conn, fetcher)
    finally:
        conn.close()

    table = Table(title=f"Discovery run {report.run_id[:8]}: {report.status}", header_style="bold")
    columns = ("Source", "Status", "Items", "Stored", "Seen before", "Rejected", "Out of scope")
    for column in columns:
        table.add_column(column)
    for result in report.sources:
        counts = result.counts()
        table.add_row(
            result.source_id,
            result.status,
            str(counts["items"]),
            str(counts["stored"]),
            str(counts["sightings"] + counts["merged"]),
            str(counts["rejected"]),
            str(sum(result.out_of_scope.values())),
        )
    console.print(table)

    if report.obstacles:
        console.print(
            Panel(
                "\n".join(f"- {o['source']}: {o['reason']}" for o in report.obstacles),
                title="sources that produced nothing",
                border_style="yellow",
            )
        )
    if report.status == "failed":
        raise typer.Exit(1)


# ------------------------------------------------------------------- paths


@app.command("where")
def where() -> None:
    """Print where your data is stored."""
    for key, value in paths_module.describe().items():
        console.print(f"{key:14} {value}")


# ------------------------------------------------------------------ setup


setup_app = typer.Typer(help="Guided first-run configuration.", no_args_is_help=True)
app.add_typer(setup_app, name="setup")


@setup_app.callback(invoke_without_command=True)
def setup_all(ctx: typer.Context) -> None:
    """Run the full guided setup."""
    if ctx.invoked_subcommand is not None:
        return
    paths = paths_module.get_paths()
    conn = open_database(paths)
    conn.close()
    console.print(
        Panel(
            f"Data directory ready at [bold]{paths.root}[/bold]\n\n"
            "Next:\n"
            "  1. [bold]jarvis setup google[/bold]  — connect your own Gmail\n"
            "  2. [bold]jarvis setup profile[/bold] — import your CV and documents\n"
            "  3. [bold]jarvis serve[/bold]         — open the dashboard",
            title="setup",
            border_style="green",
        )
    )


@setup_app.command("countries")
def setup_countries() -> None:
    """Install the country packs, and show which countries will be searched.

    Safe to run again after an upgrade: a pack's reference data is refreshed, and
    a country you switched off stays off.
    """
    from jarvis.discovery import countries

    conn = open_database(paths_module.get_paths())
    try:
        countries.install(conn)
        registered = countries.packs()
        table = Table(title="Country packs", header_style="bold")
        table.add_column("Code")
        table.add_column("Country")
        table.add_column("Searched")
        table.add_column("Sources")
        table.add_column("Savings floor", overflow="fold")
        enabled = set(countries.enabled_codes(conn))
        for code, pack in registered.items():
            floor = settings_module.resolve_savings_floor(conn, code)
            floor_text = (
                f"{floor.value:,.0f} USD/month ({floor.source})"
                if floor.value is not None
                else (floor.reason or "unsourced")
            )
            table.add_row(
                code,
                pack.name,
                "yes" if code in enabled else "no",
                ", ".join(pack.sources) or "-",
                floor_text,
            )
        console.print(table)
    finally:
        conn.close()


@setup_app.command("google")
def setup_google() -> None:
    """Connect your own Google account, through your own OAuth client.

    You create the OAuth client, in your own Google Cloud project. That is what
    keeps this free and uncapped: Google's 100-user limit applies per project,
    and your project has exactly one user — you. No verification, no security
    assessment, and refresh tokens that do not expire.
    """
    from jarvis.gmail.setup import run_setup_wizard

    run_setup_wizard(console)


@setup_app.command("profile")
def setup_profile(
    document: Annotated[
        list[Path] | None,
        typer.Option("--document", "-d", help="A CV or supporting document to read."),
    ] = None,
    master_cv: Annotated[
        Path | None,
        typer.Option("--master-cv", help="An existing master_cv.json to import."),
    ] = None,
    kind: Annotated[
        str, typer.Option(help="passport, cnic and tax_return are stored but never read."),
    ] = "cv",
) -> None:
    """Import a CV and supporting documents into your profile.

    Everything read from a document arrives as a **proposal**. Nothing is
    treated as fact until you have looked at it, so this is safe to run on
    whatever you have.
    """
    from jarvis.documents import store as document_store
    from jarvis.profile.importer import import_master_cv

    paths = paths_module.get_paths()
    conn = open_database(paths)
    try:
        if master_cv is not None:
            report = import_master_cv(conn, master_cv)
            console.print(f"[green]{report.summary()}[/green]")
            for note in report.skipped:
                console.print(f"  [dim]skipped {note}[/dim]")
            for warning in report.warnings:
                console.print(Panel(warning, border_style="yellow", title="worth knowing"))

        for path in document or []:
            result = document_store.ingest(conn, path, kind=kind, paths=paths)
            if result.restricted:
                console.print(f"[green]{path.name}[/green] stored securely; "
                              "its contents are never read.")
            elif not result.extracted_ok:
                console.print(Panel(result.note or "could not be read",
                                    title=path.name, border_style="yellow"))
            else:
                console.print(
                    f"[green]{path.name}[/green]: {len(result.record_ids)} fields proposed, "
                    f"{len(result.candidates)} suggestions across {result.page_count} page(s)"
                )

        _print_readiness(conn)
    finally:
        conn.close()


def _print_readiness(conn: Any) -> None:
    """Show the checklist as items, and the percentage as progress only."""
    from jarvis.profile.functions import completeness, minimum_viable_profile

    mvp = minimum_viable_profile(conn)
    progress = completeness(conn)

    table = Table(title="Readiness", show_header=True, header_style="bold")
    table.add_column("Checklist")
    table.add_column("State")
    table.add_column("Still needed", overflow="fold")
    for item in mvp.items:
        table.add_row(
            item.label,
            "ok" if item.satisfied else "missing",
            "" if item.satisfied else ", ".join(item.missing),
        )
    console.print(table)
    console.print(
        f"Profile completeness: [bold]{progress.percentage:.0f}%[/bold] "
        f"[dim](progress only — it gates nothing)[/dim]"
    )
    if mvp.satisfied:
        console.print("[green]Scheduled discovery is unlocked.[/green]")
    else:
        console.print("[yellow]Fill the checklist above to unlock scheduled discovery.[/yellow]")


# -------------------------------------------------------------------- run


@app.command()
def run(
    skip: Annotated[list[str] | None, typer.Option(
        help="Skip a step: discover, screen, sufficiency, generate, gmail, backup.")] = None,
) -> None:
    """Run one full pipeline pass. Nothing is sent: packages wait for your approval."""
    from jarvis import pipeline

    conn = open_database(paths_module.get_paths())
    try:
        result = pipeline.run(conn, skip=frozenset(skip or ()))
    finally:
        conn.close()

    table = Table(title=f"Run {result['run_id'][:8]}: {result['status']}", header_style="bold")
    table.add_column("Step")
    table.add_column("Result", overflow="fold")
    for step, counts in result["counts"].items():
        table.add_row(step, json.dumps(counts, default=str))
    console.print(table)
    if result["obstacles"]:
        console.print(Panel("\n".join(f"- {o['step']}: {o['reason']}" for o in result["obstacles"]),
                            title="obstacles", border_style="yellow"))
    if result["status"] == "failed":
        raise typer.Exit(1)


# ------------------------------------------------------------------ serve


@app.command()
def serve(
    port: Annotated[int | None, typer.Option(help="Override the configured port.")] = None,
    host: Annotated[str | None, typer.Option(help="Override the bind address.")] = None,
    certfile: Annotated[Path | None, typer.Option(
        help="TLS certificate. A phone installs the app as a PWA only over https.")] = None,
    keyfile: Annotated[Path | None, typer.Option(help="TLS private key for --certfile.")] = None,
    schedule: Annotated[bool, typer.Option(
        help="Run the daily pass and the Gmail poll in the background.")] = True,
) -> None:
    """Start the local web app, with the daily run and Gmail polling alongside it."""
    if (certfile is None) != (keyfile is None):
        console.print("[red]--certfile and --keyfile go together.[/red]")
        raise typer.Exit(2)
    import uvicorn

    from jarvis.store import settings as settings_module
    from jarvis.store.db import session
    from jarvis.web.app import create_app

    with session() as conn:
        port = port or int(settings_module.get(conn, "web_port"))
        host = host or str(settings_module.get(conn, "web_bind"))
    scheme = "https" if certfile else "http"
    console.print(f"Jarvis on {scheme}://localhost:{port}  (bound to {host})")
    background = None
    if schedule:
        from jarvis import scheduler

        background = scheduler.build()
        background.start()
        for job in scheduler.describe(background):
            console.print(f"  scheduled {job['id']}: next {job['next_run']}")
    try:
        uvicorn.run(
            create_app(), host=host, port=port, log_level="warning",
            ssl_certfile=str(certfile) if certfile else None,
            ssl_keyfile=str(keyfile) if keyfile else None,
        )
    finally:
        if background is not None:
            background.shutdown(wait=False)


# -------------------------------------------------------------------- mcp


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio.

    Claude Code and OpenAI Codex both launch this; you rarely run it by hand.

        claude mcp add jarvis -- jarvis mcp

        # ~/.codex/config.toml
        [mcp_servers.jarvis]
        command = "jarvis"
        args = ["mcp"]
    """
    from jarvis.mcp.server import main as run_server

    run_server()


# ----------------------------------------------------------------- backup


@app.command("gen-skill")
def gen_skill() -> None:
    """Regenerate the Claude Skill from the live MCP tool registry.

    The Skill is generated, never hand-maintained: one that describes tools the
    server no longer has tells a model something untrue about what it can do.
    """
    from jarvis.mcp.skill import write

    target = write()
    console.print(f"[green]Wrote {target}[/green]")


@app.command()
def backup(
    target: Annotated[Path | None, typer.Option(
        help="Folder to write to (e.g. an external drive). Default: the backup_target "
             "setting, else <data dir>/backups.")] = None,
    drill: Annotated[bool, typer.Option(
        help="Also restore it into a scratch folder and check it.")] = True,
) -> None:
    """Take an encrypted backup of the database, documents and config."""
    from jarvis import backup as backup_module

    conn = open_database(paths_module.get_paths())
    try:
        taken = backup_module.take(conn, kind="manual", target=target)
        console.print(f"[green]Backup written:[/green] {taken['path']} "
                      f"({taken['size_bytes'] / 1024 / 1024:.1f} MB)")
        if drill:
            result = backup_module.drill(conn, taken["backup_id"])
            colour = "green" if result["ok"] else "red"
            console.print(f"[{colour}]Restore drill: {result['result']}[/{colour}]")
    finally:
        conn.close()
    console.print(
        f"\n[bold]Copy this key somewhere off this computer, once:[/bold] {taken['key_file']}\n"
        "Without it no backup can be restored — that is what keeps a lost drive safe.\n"
        "Backups never contain your Google token or your restricted-document key."
    )


@app.command()
def restore(
    archive: Annotated[Path, typer.Argument(help="The .jvb backup file.")],
    key: Annotated[Path, typer.Option(help="The backup.key you copied off the machine.")],
) -> None:
    """Restore a backup into an EMPTY data directory (set JARVIS_HOME to a new folder)."""
    from jarvis import backup as backup_module

    try:
        result = backup_module.restore(archive, key_file=key)
    except backup_module.RestoreRefused as refused:
        console.print(f"[red]{refused}[/red]")
        raise typer.Exit(1) from refused
    console.print(f"[green]Restored.[/green] Database integrity: {result['integrity']}")
    console.print(result["note"])


@app.command()
def reconcile(
    application_id: Annotated[str, typer.Argument(help="The application stuck in 'sending'.")],
    message_id: Annotated[str | None, typer.Option(
        help="The Gmail message id, if you found the message in Sent.")] = None,
    not_sent: Annotated[bool, typer.Option(
        "--not-sent", help="You checked Sent and the message is not there.")] = False,
) -> None:
    """Resolve a send that was reserved but never recorded. Never sends again."""
    from jarvis import send, tracking

    if (message_id is None) == (not not_sent):
        console.print("[red]Give exactly one of --message-id or --not-sent.[/red]")
        raise typer.Exit(2)
    conn = open_database(paths_module.get_paths())
    try:
        row = conn.execute("SELECT status FROM application WHERE id = ?",
                           (application_id,)).fetchone()
        if row is None or row["status"] != "sending":
            console.print("[red]That application is not waiting to be reconciled.[/red]")
            raise typer.Exit(1)
        if message_id:
            send.reconcile(conn, application_id, message_id)
            console.print("[green]Recorded as sent.[/green]")
        else:
            tracking.transition(conn, application_id, "withdrawn", actor="human",
                                note="reconciled: the message never left (checked Sent)")
            console.print("Recorded as not sent. Prepare the package again to retry.")
    finally:
        conn.close()


@app.command()
def export(
    target: Annotated[Path, typer.Argument(help="An empty folder to write into.")],
) -> None:
    """Export everything in open formats (JSON and your documents)."""
    from jarvis import backup as backup_module

    conn = open_database(paths_module.get_paths())
    try:
        out = backup_module.export(conn, target)
    finally:
        conn.close()
    console.print(f"[green]Exported to {out}[/green]  (restricted documents are not exported)")


@app.command()
def purge(
    yes: Annotated[bool, typer.Option("--yes", help="Confirm. This cannot be undone.")] = False,
) -> None:
    """Destroy the restricted-document key: every copy, in every backup, becomes unreadable."""
    from jarvis import backup as backup_module

    if not yes:
        console.print("[red]This destroys your restricted documents everywhere, for good. "
                      "Run again with --yes to proceed.[/red]")
        raise typer.Exit(2)
    conn = open_database(paths_module.get_paths())
    try:
        result = backup_module.purge_restricted_key(conn, confirm=True)
    finally:
        conn.close()
    console.print("Key destroyed." if result["destroyed"] else "There was no key to destroy.")


if __name__ == "__main__":  # pragma: no cover
    app()
