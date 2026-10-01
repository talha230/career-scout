"""Snapshots — the page a figure was read from, kept on disk.

A stored number is only checkable if the page it came from is still readable. A
URL is not enough: the page changes, the employer takes the posting down, the
statistics office reissues the table. So every sourced figure names a snapshot,
and the snapshot is a file in ``CAREER_SCOUT_HOME/snapshots``.

Snapshots are **content-addressed**: the same bytes fetched twice are one file,
and a file's name is the hash of what is in it, so a snapshot cannot be edited
without its name ceasing to match. Beside each one sits a ``.meta.json`` naming
the URL and the time it was fetched, because a file full of numbers with no
provenance is not evidence of anything.

:func:`contains` is the check invariant I-22 rests on: the quote stored with a
figure has to appear in the snapshot. It compares on collapsed whitespace, and
against the tag-stripped text as well as the raw bytes, because a quote read out
of a rendered page will not match raw HTML character for character — the words
are there, the markup between them is not.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from career_scout.store.db import utcnow
from career_scout.store.paths import get_paths

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One stored page: where it lives, what it hashes to, where it came from."""

    path: Path
    sha256: str
    source_url: str
    fetched_at: str

    @property
    def relative_path(self) -> str:
        """The path as stored on a figure: relative to ``CAREER_SCOUT_HOME``.

        Relative on purpose. An absolute path stops resolving the moment the data
        directory moves or is restored onto another machine, and a restore that
        cannot find its own evidence is a backup that did not work.
        """
        return self.path.relative_to(get_paths(ensure=False).root).as_posix()


def store(
    content: str | bytes,
    *,
    source_url: str,
    fetched_at: str | None = None,
    suffix: str = ".html",
) -> Snapshot:
    """Write one snapshot and return it. Identical content is stored once."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(raw).hexdigest()

    root = get_paths().snapshots
    target = root / digest[:2] / f"{digest}{suffix}"
    meta = target.with_suffix(target.suffix + ".meta.json")
    _long(target.parent).mkdir(parents=True, exist_ok=True)

    fetched = fetched_at or utcnow()
    # Each file is written if it is missing, independently. Writing the metadata
    # only alongside a new data file meant one interrupted write left a snapshot
    # with no provenance for ever, because every later store saw the data file
    # and skipped both.
    if not _long(target).exists():
        _long(target).write_bytes(raw)
    if not _long(meta).exists():
        _long(meta).write_text(
            json.dumps(
                {"source_url": source_url, "fetched_at": fetched, "sha256": digest},
                indent=2,
            ),
            encoding="utf-8",
        )

    return Snapshot(path=target, sha256=digest, source_url=source_url, fetched_at=fetched)


def _long(path: Path) -> Path:
    """The path in a form Windows will open past 260 characters.

    A snapshot's name is a 64-character hash plus a double suffix, so the path
    runs ~95 characters past the data directory. The first live discovery run,
    from a deeply nested directory, failed on exactly this: the data file was
    written and its ``.meta.json`` was not. The ``\\\\?\\`` prefix switches off
    the legacy limit for that one call. Elsewhere it is the path unchanged.
    """
    if os.name != "nt":
        return path
    text = str(path.resolve())
    if text.startswith("\\\\?\\") or len(text) < 240:
        return path
    return Path("\\\\?\\" + text)


def resolve(path: str | Path) -> Path:
    """A stored snapshot path, absolute or relative to ``CAREER_SCOUT_HOME``."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return get_paths(ensure=False).root / candidate


def read(path: str | Path) -> str:
    """The snapshot's text. Undecodable bytes are replaced, never raised on."""
    return _long(resolve(path)).read_bytes().decode("utf-8", errors="replace")


def contains(path: str | Path, quote: str) -> bool:
    """Whether ``quote`` appears in the snapshot at ``path``.

    Compared on collapsed whitespace, and against the tag-stripped text as well
    as the raw content: a sentence quoted from a rendered page is separated by
    markup in the source, and requiring a byte-for-byte match would fail every
    real HTML snapshot while proving nothing extra.
    """
    if not quote.strip():
        return False
    try:
        content = read(path)
    except OSError:
        return False

    needle = _normalise(quote)
    return needle in _normalise(content) or needle in _normalise(_TAG.sub(" ", content))


def _normalise(text: str) -> str:
    """Collapse the differences that are not the words.

    HTML entities are unescaped first: a page writes ``AED&nbsp;6,000`` where the
    reader sees ``AED 6,000``, and comparing the two as written fails on a quote
    that is word for word correct. NFKC then folds the non-breaking space the
    entity became into an ordinary one, along with the other compatibility
    forms a copied quote picks up.
    """
    folded = unicodedata.normalize("NFKC", html.unescape(text))
    return _WHITESPACE.sub(" ", folded).strip().casefold()
