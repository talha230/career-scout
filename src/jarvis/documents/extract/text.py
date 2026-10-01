"""Reading text out of a document, with a locator for every character — T018, T019.

The output of this module is the substrate everything downstream stands on. A
field extracted without a locator cannot be pointed at, and a claim that cannot
be pointed at has no provenance — so every extraction carries the page and the
character range it came from, and the snippet is re-read from the document
rather than remembered.

**A document with no text layer is reported, not silently emptied.** A scanned
degree certificate or an experience letter photographed on a phone yields zero
characters from a text extractor. Returning "" would present as a CV containing
nothing, which is indistinguishable from a CV the user forgot to fill in.
:class:`ExtractedDocument.ok` is False in that case and :attr:`note` says why,
so the UI can ask for a different file or offer OCR.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Below this many characters per page, a PDF almost certainly has no text
#: layer. Chosen from the legacy corpus: real CV pages run to thousands of
#: characters, and a scan yields a handful of stray ligatures at most.
MIN_CHARS_PER_PAGE = 40


class UnsupportedDocument(ValueError):
    """A file type this extractor does not read."""


@dataclass(frozen=True, slots=True)
class Locator:
    """Where a value was read from. Serialised into ``profile_record.locator``."""

    page: int
    start: int
    end: int
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        return {"page": self.page, "start": self.start, "end": self.end,
                "snippet": self.snippet}


@dataclass(frozen=True, slots=True)
class Page:
    number: int          # 1-based, as a person would cite it
    start: int           # inclusive offset into ExtractedDocument.text
    end: int             # exclusive
    text: str


@dataclass(slots=True)
class ExtractedDocument:
    """Text plus enough structure to cite any part of it."""

    path: Path
    sha256: str
    bytes: int
    text: str
    pages: list[Page]
    ok: bool
    note: str | None = None
    kind: str = "unknown"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page_of(self, offset: int) -> int:
        """Which 1-based page a global offset falls on."""
        for page in self.pages:
            if page.start <= offset < page.end:
                return page.number
        return self.pages[-1].number if self.pages else 1

    def locate(self, start: int, end: int, *, context: int = 60) -> Locator:
        """Build a locator, re-reading the snippet from the text itself.

        The snippet is sliced from :attr:`text` rather than passed in, so it
        cannot drift from what the document actually says.
        """
        low = max(0, start - context)
        high = min(len(self.text), end + context)
        snippet = " ".join(self.text[low:high].split())
        return Locator(page=self.page_of(start), start=start, end=end, snippet=snippet)

    def find(self, pattern: str | re.Pattern[str]) -> list[tuple[re.Match[str], Locator]]:
        """Every match of a pattern, each with its locator."""
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        return [(m, self.locate(m.start(), m.end())) for m in compiled.finditer(self.text)]


# ------------------------------------------------------------------ helpers


def sha256_of(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def normalise_text(raw: str) -> str:
    """Make offsets stable and matching predictable, without moving characters.

    NFKC folds the typographic variants that CV exporters emit — ligatures,
    full-width forms, non-breaking spaces — so a regex for ``fi`` matches
    ``ﬁ``. Line structure is preserved because section detection depends on it.
    Replacements are length-preserving wherever possible so locators computed
    against the result still point at sensible places in the original.
    """
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace(" ", " ").replace("​", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of spaces/tabs but never newlines.
    return re.sub(r"[ \t]+", " ", text)


# ------------------------------------------------------------------- PDF


def extract_pdf(path: Path) -> ExtractedDocument:
    """Read a PDF's text layer. Reports rather than invents when there is none."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedDocument("pdfplumber is required to read PDFs") from exc

    digest, size = sha256_of(path)
    pages: list[Page] = []
    chunks: list[str] = []
    offset = 0

    with pdfplumber.open(str(path)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            body = normalise_text(page.extract_text() or "")
            if body and not body.endswith("\n"):
                body += "\n"
            pages.append(Page(number=number, start=offset, end=offset + len(body), text=body))
            chunks.append(body)
            offset += len(body)

    text = "".join(chunks)
    ok, note = _assess_text_layer(text, len(pages))
    return ExtractedDocument(
        path=path, sha256=digest, bytes=size, text=text, pages=pages,
        ok=ok, note=note, kind="pdf", meta={"page_count": len(pages)},
    )


def _assess_text_layer(text: str, page_count: int) -> tuple[bool, str | None]:
    """Decide whether what came back is a real text layer — T019."""
    if page_count == 0:
        return False, "the PDF has no pages"

    stripped = re.sub(r"\s", "", text)
    per_page = len(stripped) / page_count
    if per_page < MIN_CHARS_PER_PAGE:
        return False, (
            f"this file has no usable text layer "
            f"({len(stripped)} characters across {page_count} page"
            f"{'s' if page_count != 1 else ''}). It is most likely a scan or a "
            f"photograph. Upload a text PDF or DOCX if you have one, or enable OCR "
            f"with: pip install 'jarvis-agent[ocr]'"
        )
    return True, None


# ------------------------------------------------------------------ DOCX


def extract_docx(path: Path) -> ExtractedDocument:
    """Read a DOCX.

    Word has no pages until something lays it out, so the whole document is
    reported as page 1. Tables are read row by row, because CVs routinely put
    dates and employers in a two-column table and dropping them loses exactly
    the fields that matter most.
    """
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedDocument("python-docx is required to read DOCX files") from exc

    digest, size = sha256_of(path)
    document = docx.Document(str(path))

    lines: list[str] = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.replace("\n", " ").strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))

    text = normalise_text("\n".join(lines))
    pages = [Page(number=1, start=0, end=len(text), text=text)]
    stripped = re.sub(r"\s", "", text)
    ok = len(stripped) >= MIN_CHARS_PER_PAGE
    note = None if ok else "this DOCX contains almost no text"

    return ExtractedDocument(
        path=path, sha256=digest, bytes=size, text=text, pages=pages,
        ok=ok, note=note, kind="docx",
        meta={"paragraphs": len(document.paragraphs), "tables": len(document.tables)},
    )


# ------------------------------------------------------------------ plain


def extract_plain(path: Path) -> ExtractedDocument:
    digest, size = sha256_of(path)
    text = normalise_text(path.read_text(encoding="utf-8", errors="replace"))
    pages = [Page(number=1, start=0, end=len(text), text=text)]
    ok = bool(text.strip())
    return ExtractedDocument(
        path=path, sha256=digest, bytes=size, text=text, pages=pages,
        ok=ok, note=None if ok else "the file is empty", kind="text",
    )


EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".txt": extract_plain,
    ".md": extract_plain,
}


def extract(path: Path) -> ExtractedDocument:
    """Read any supported document.

    A ``.doc``, ``.pages`` or image file raises rather than being half-read:
    the user gets a specific message naming what to convert it to.
    """
    suffix = path.suffix.lower()
    extractor = EXTRACTORS.get(suffix)
    if extractor is None:
        raise UnsupportedDocument(
            f"{suffix or 'this file'} is not readable. Supported: "
            f"{', '.join(sorted(EXTRACTORS))}. Export it as PDF or DOCX and try again."
        )
    return extractor(path)
