"""Shared fixtures.

Two properties every test in this suite gets for free:

* ``JARVIS_HOME`` points at a temp directory, so no test can touch the real
  user's data. The path cache is cleared on the way in *and* on the way out,
  because it is process-global.
* The network is closed unless a test opts in with ``@pytest.mark.network``.
  A unit test that quietly reaches a job board is a test that fails on a train.
"""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.store import paths as paths_module
from jarvis.store.db import migrate, open_database


@pytest.fixture(autouse=True)
def jarvis_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every path lookup at a throwaway directory."""
    home = tmp_path / "jarvis-home"
    monkeypatch.setenv(paths_module.ENV_HOME, str(home))
    paths_module.reset_cache()
    yield home
    paths_module.reset_cache()


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the network unless the test is marked ``network``.

    This is the blunt version, applied to the whole suite. The sharper one —
    allowing connections that originate inside ``jarvis.net`` and failing every
    other call site — lives in ``invariants/test_egress_chokepoint.py``, which
    is where the guarantee is actually asserted.
    """
    if request.node.get_closest_marker("network"):
        return

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "this test tried to open a network connection; mark it @pytest.mark.network "
            "if that is deliberate, or stub the call"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def paths() -> paths_module.Paths:
    """A created, hardened data directory under the temp home."""
    return paths_module.get_paths()


@pytest.fixture
def db(paths: paths_module.Paths) -> Iterator[sqlite3.Connection]:
    """An open, migrated, empty database."""
    conn = open_database(paths)
    yield conn
    conn.close()


@pytest.fixture
def memory_db() -> Iterator[sqlite3.Connection]:
    """An in-memory database with the schema applied, for pure schema tests."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    yield conn
    conn.close()
