"""Where a user's data lives, and nowhere else.

Every path in Jarvis resolves through this module. Nothing writes to the
repository, to a temp directory, or to a location the user did not choose.

Resolution order for the data root:

1. ``JARVIS_HOME`` environment variable, if set. Tests set this to a temp
   directory; a user may set it to an encrypted volume or a synced folder.
2. ``platformdirs.user_data_dir("jarvis")`` — on Windows
   ``%LOCALAPPDATA%\\jarvis``, on macOS ``~/Library/Application Support/jarvis``,
   on Linux ``~/.local/share/jarvis``.

``credentials/`` holds the user's Google client secret and refresh token. It is
created ``0o700`` and its files ``0o600``. On Windows those bits are advisory,
so :func:`harden_credentials_dir` additionally strips inherited ACLs and grants
the current user alone — otherwise "the token is protected" would be a claim
rather than a property.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from platformdirs import user_data_dir

ENV_HOME = "JARVIS_HOME"
APP_NAME = "jarvis"

#: Subdirectories created on first use, relative to the data root.
LAYOUT: tuple[str, ...] = (
    "documents/source",
    "documents/restricted",
    "documents/generated",
    "snapshots",
    "config",
    "credentials",
    "logs",
    "backups",
)

#: Directories whose contents are private even from other accounts on the machine.
PRIVATE: frozenset[str] = frozenset({"credentials", "documents/restricted"})


@dataclass(frozen=True, slots=True)
class Paths:
    """Resolved locations for one installation.

    Construct via :func:`get_paths` rather than directly, so that a single
    process sees one consistent root.
    """

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "jarvis.db"

    @property
    def documents(self) -> Path:
        return self.root / "documents"

    @property
    def source_documents(self) -> Path:
        return self.root / "documents" / "source"

    @property
    def restricted_documents(self) -> Path:
        return self.root / "documents" / "restricted"

    @property
    def generated_documents(self) -> Path:
        return self.root / "documents" / "generated"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def credentials(self) -> Path:
        return self.root / "credentials"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def client_secret(self) -> Path:
        return self.credentials / "client_secret.json"

    @property
    def google_token(self) -> Path:
        return self.credentials / "token.json"

    @property
    def data_key(self) -> Path:
        """Local key encrypting the restricted document store.

        Destroying this file makes every restricted document unreadable in
        every copy, including backups taken before the deletion. That is the
        property ``jarvis purge`` relies on.
        """
        return self.credentials / "data.key"

    @property
    def backup_key(self) -> Path:
        """Key encrypting backup archives. Copy it off this machine once.

        Separate from :attr:`data_key` on purpose: a backup must be restorable
        after the machine is lost (so its key cannot be the one that dies with
        it), and purging restricted documents must reach every backup (so the
        archive never contains the data key).
        """
        return self.credentials / "backup.key"

    def ensure(self) -> Paths:
        """Create the directory tree if it is absent, and harden the private parts."""
        self.root.mkdir(parents=True, exist_ok=True)
        for relative in LAYOUT:
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        for relative in PRIVATE:
            harden_directory(self.root / relative)
        return self


def resolve_root(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the data root without creating anything.

    ``override`` wins over the environment, which wins over the platform
    default. An empty or whitespace-only ``JARVIS_HOME`` is treated as unset
    rather than as the current directory, because the latter silently scatters
    a user's personal data across whatever directory they happened to be in.
    """
    if override is not None:
        return Path(override).expanduser().resolve()

    from_env = os.environ.get(ENV_HOME)
    if from_env and from_env.strip():
        return Path(from_env).expanduser().resolve()

    return Path(user_data_dir(APP_NAME, appauthor=False)).resolve()


@lru_cache(maxsize=1)
def _cached_paths(root: str) -> Paths:
    return Paths(Path(root))


def get_paths(override: str | os.PathLike[str] | None = None, *, ensure: bool = True) -> Paths:
    """Return the :class:`Paths` for this installation.

    The result is cached per root, so repeated calls in one process are cheap
    and consistent. Tests that move ``JARVIS_HOME`` between cases must call
    :func:`reset_cache`; the ``jarvis_home`` fixture does this for them.
    """
    paths = _cached_paths(str(resolve_root(override)))
    return paths.ensure() if ensure else paths


def reset_cache() -> None:
    """Forget the cached root. Used by tests that relocate ``JARVIS_HOME``."""
    _cached_paths.cache_clear()


def _icacls(*args: str) -> None:
    """Run Windows ``icacls`` from its absolute path.

    Invoking it as a bare name would resolve through ``PATH``, so anyone able
    to write to a ``PATH`` directory could substitute their own binary — in the
    one function whose job is to protect a refresh token.
    """
    # os.environ upper-cases names on Windows, so SYSTEMROOT is the portable spelling.
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    executable = Path(system_root) / "System32" / "icacls.exe"
    if not executable.exists():  # pragma: no cover - non-standard Windows
        raise FileNotFoundError(f"icacls not found at {executable}")
    subprocess.run(  # noqa: S603 - absolute executable, no shell, fixed arguments
        [str(executable), *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def harden_directory(path: Path) -> None:
    """Restrict ``path`` to the current user.

    POSIX bits are set everywhere. On Windows they are advisory, so we also
    reset the ACL: ``/inheritance:r`` drops inherited entries (which typically
    include ``Users``), then the current account is granted full control. A
    failure here is reported rather than swallowed, because silently leaving a
    refresh token world-readable is exactly the outcome this exists to prevent.
    """
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # POSIX bits are best-effort on Windows
        path.chmod(stat.S_IRWXU)

    if sys.platform != "win32":
        return

    account = os.environ.get("USERNAME")
    if not account:  # pragma: no cover - always set on Windows
        return
    try:
        _icacls(str(path), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F")
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        raise PermissionError(
            f"could not restrict {path} to {account}; refusing to store credentials "
            f"in a directory whose permissions are unknown"
        ) from exc


def harden_file(path: Path) -> None:
    """Restrict a single file (a token, a key) to the current user."""
    with contextlib.suppress(OSError):  # POSIX bits are best-effort on Windows
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    if sys.platform != "win32":
        return

    account = os.environ.get("USERNAME")
    if not account:  # pragma: no cover
        return
    try:
        _icacls(str(path), "/inheritance:r", "/grant:r", f"{account}:F")
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        raise PermissionError(f"could not restrict {path} to {account}") from exc


def describe() -> dict[str, str]:
    """Human-readable locations, for ``jarvis doctor`` and the settings screen."""
    paths = get_paths(ensure=False)
    return {
        "root": str(paths.root),
        "database": str(paths.db),
        "documents": str(paths.documents),
        "credentials": str(paths.credentials),
        "backups": str(paths.backups),
        "source": ENV_HOME if os.environ.get(ENV_HOME) else "platform default",
    }
