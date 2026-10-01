"""SQLite connection handling and the migration runner.

Why plain ``sqlite3`` and not an ORM: the send path needs ``BEGIN IMMEDIATE``
held across a count, an insert and an update (invariant I-14), and the schema is
meant to be read by a person checking a number by hand. Both are clearer
without a session layer in between.

Pragmas set on every connection:

``journal_mode=WAL``
    The web app, a scheduled run and an MCP tool call can all be live at once.
    WAL lets readers proceed while one writer holds the database.
``foreign_keys=ON``
    Off by default in SQLite, which silently turns every reference into a
    suggestion.
``busy_timeout=5000``
    Concurrent writers wait rather than raising ``database is locked``
    immediately. The send path still uses ``BEGIN IMMEDIATE`` so the wait
    happens before any work, not halfway through it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from jarvis.store.paths import Paths, get_paths

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    """The one timestamp format used everywhere: ISO-8601 UTC, second precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(paths: Paths | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with the pragmas every caller depends on.

    ``isolation_level=None`` turns off the driver's implicit transaction
    handling, so ``BEGIN``/``BEGIN IMMEDIATE``/``COMMIT`` mean exactly what they
    say. Callers use :func:`transaction` or :func:`immediate` rather than
    issuing those by hand.
    """
    paths = paths or get_paths()
    if readonly:
        conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(paths.db, isolation_level=None)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A deferred transaction: commits on success, rolls back on any exception."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction that takes the lock up front.

    Required wherever a decision is read and then acted on — the send caps, the
    minimum-interval check, claiming an approval. A deferred transaction
    upgrades to a write lock only at the first write, which leaves a window in
    which two callers both read "4 sends used, limit 5" and both proceed.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'"
    ).fetchone()
    if not exists:
        return set()
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migration")}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every migration not yet recorded. Returns the versions applied.

    Each file is applied atomically together with its own ``schema_migration``
    row, so an interrupted migration leaves the database on the last complete
    version rather than half-way through one.

    The ``BEGIN``/``COMMIT`` live *inside* the script text rather than around
    the call: ``executescript`` issues an implicit ``COMMIT`` before it runs,
    which silently discards a transaction opened by the caller.
    """
    done = applied_versions(conn)
    applied: list[str] = []

    for path in _migration_files():
        version = path.stem
        if version in done:
            continue

        # The version is a filename stem we control, but it is still
        # interpolated into SQL, so quote it as a literal rather than trusting
        # the directory listing.
        literal_version = "'" + version.replace("'", "''") + "'"
        literal_now = "'" + utcnow() + "'"
        script = (
            "BEGIN;\n"
            + path.read_text(encoding="utf-8")
            + "\nINSERT INTO schema_migration (version, applied_at) VALUES "
            + f"({literal_version}, {literal_now});\n"
            + "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)

    return applied


def open_database(paths: Paths | None = None) -> sqlite3.Connection:
    """Open the database, creating and migrating it if necessary.

    The caller owns the connection and must close it. Prefer :func:`session`,
    which closes it for you — ``with open_database() as conn`` does **not**:
    sqlite3's context manager manages a transaction, not the connection, so
    that spelling silently leaks a file handle on every call.
    """
    paths = paths or get_paths()
    conn = connect(paths)
    migrate(conn)
    return conn


@contextmanager
def session(paths: Paths | None = None) -> Iterator[sqlite3.Connection]:
    """An open, migrated database that closes itself.

    The right default for a request, a tool call or a CLI command: short-lived,
    and guaranteed released even when the body raises.
    """
    conn = open_database(paths)
    try:
        yield conn
    finally:
        conn.close()
