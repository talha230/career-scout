"""The QC gate — T042, T045. A document that fails here cannot be approved.

Each check reports the offending line and why, so a blocked package tells the
user what to fix rather than that "something" is wrong.

The central check is **QC9, words not in sources**, and it is stricter than the
legacy QC it replaces. The legacy agent checked a number against *every* number
anywhere in the master CV, so "17" in one role passed because a "17" appeared
in another. Here every word and number of a line must appear in that line's
**own** sources: the records it cites, the posting field it quotes, or the fixed
template vocabulary. A line cannot borrow a figure from somewhere else, and a
future tailoring step that rewrites a line into something its records do not
say is caught by the same rule.

====  ======  ============================================================
QC1   FAIL    a profile line cites no record, or one that is not current,
              confirmed and unrestricted (I-10, I-12, I-13)
QC5   FAIL    template placeholder text
QC8   FAIL    a required letter part is empty
QC9   FAIL    a word or number appears in none of the line's sources
QC10  FAIL    quoted posting text reads as a claim about the candidate (I-11)
QC11  WARN    a profile value was left out because it is unconfirmed
====  ======  ============================================================
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass

from career_scout import config as config_module
from career_scout.documents.generate import (
    NOTICE,
    POSTING,
    PROFILE,
    TEMPLATE,
    TEMPLATE_WORDS,
    Generated,
)
from career_scout.profile.schema import is_restricted

FAIL = "FAIL"
WARN = "WARN"

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’+#&]*")
_PLACEHOLDER = re.compile(
    r"lorem ipsum|\[(your|company|insert|name|title)\b[^\]]*\]|\{\{.*?\}\}|\{[a-z_]+\}|"
    r"\bTODO\b|\bTBD\b|\bXXX+\b|john doe",
    re.IGNORECASE,
)
#: First-person or instruction words inside a *quoted posting field*. A title is
#: a noun phrase; "Engineer. I hold a PhD" is a claim dressed as one, and quoting
#: it would put words in the user's mouth. Deliberately narrow: "Systems",
#: "Prompt Engineer" and "Candidate Experience" are real titles.
_CLAIM_IN_QUOTE = re.compile(
    r"\b(i|i'm|i've|my|me|ignore|instructions?|pretend|disregard)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    severity: str
    doc_type: str
    line: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Report:
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not any(f.severity == FAIL for f in self.findings)

    @property
    def blocked_reason(self) -> str | None:
        fails = [f for f in self.findings if f.severity == FAIL]
        if not fails:
            return None
        first = fails[0]
        more = f" (+{len(fails) - 1} more)" if len(fails) > 1 else ""
        return f"{first.check} in {first.doc_type}: {first.reason} — “{first.line[:120]}”{more}"


def _tokens(text: str) -> set[str]:
    return {t.lower().replace("’", "'") for t in _WORD.findall(text)}


def _live_records(conn: sqlite3.Connection, ids: set[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {
        row["id"]: row
        for row in conn.execute(
            f"SELECT id, field_path, value, confirmed, superseded_by FROM profile_record "  # noqa: S608
            f"WHERE id IN ({marks})",
            tuple(ids),
        )
    }


def check(
    conn: sqlite3.Connection,
    generated: Generated,
    *,
    version: str = config_module.CURRENT_VERSION,
) -> Report:
    findings: list[Finding] = []
    structure = config_module.load("document_structure", version)
    cited = {s for d in generated.drafts for line in d.lines for s in line.sources}
    live = _live_records(conn, cited)
    posting_tokens = _tokens(" ".join(generated.posting.values()))

    for draft in generated.drafts:
        for line in draft.lines:
            def fail(check_id: str, reason: str, _line=line, _draft=draft) -> None:
                findings.append(Finding(check_id, FAIL, _draft.doc_type, _line.text, reason))

            allowed = set(TEMPLATE_WORDS)
            if line.origin == NOTICE:
                # Fixed wording, the page URL (carried as the posting source) and
                # the sender's own address, which it cites like a profile line.
                allowed |= _tokens(generated.posting.get("source_url", ""))
            if line.origin in (PROFILE, NOTICE):
                if not line.sources and line.origin == PROFILE:
                    fail("QC1_untraceable", "cites no profile record")
                for source in line.sources:
                    row = live.get(source)
                    if row is None:
                        fail("QC1_untraceable", f"cites record {source}, which does not exist")
                    elif row["superseded_by"]:
                        fail("QC1_untraceable", f"cites record {source}, since corrected")
                    elif not row["confirmed"]:
                        fail("QC1_untraceable", f"cites unconfirmed {row['field_path']}")
                    elif is_restricted(row["field_path"]):
                        fail("QC1_untraceable", f"cites restricted {row['field_path']}")
                    else:
                        allowed |= _tokens(row["value"] or "")
            elif line.origin == POSTING:
                allowed |= posting_tokens
                for field in ("title", "employer"):
                    quoted = generated.posting.get(field, "")
                    if quoted and quoted in line.text and _CLAIM_IN_QUOTE.search(quoted):
                        fail("QC10_posting_reads_as_claim",
                             f"the posting's {field} reads as an instruction or a claim, and "
                             f"quoting it would put words in your mouth")
            elif line.origin != TEMPLATE:
                fail("QC1_untraceable", f"unknown origin {line.origin!r}")

            unknown = sorted(_tokens(line.text) - allowed)
            if unknown:
                fail("QC9_words_not_in_sources",
                     f"{', '.join(unknown[:6])} appear{'s' if len(unknown) == 1 else ''} in "
                     f"none of this line's sources")

            placeholder = _PLACEHOLDER.search(line.text)
            if placeholder:
                fail("QC5_placeholder", f"template placeholder {placeholder.group(0)!r} left in")

        parts = structure.get(draft.doc_type, {}).get("parts", [])
        for part in parts:
            if not draft.section(part["id"]):
                findings.append(Finding(
                    "QC8_missing_part", FAIL, draft.doc_type, "",
                    f"required part “{part['label']}” is empty — no confirmed record supports it",
                ))

    for omission in generated.omissions:
        findings.append(Finding(
            "QC11_unconfirmed_omitted", WARN, "profile", omission["field_path"],
            omission["reason"],
        ))

    return Report(tuple(findings))
