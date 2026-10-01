"""Interview practice and the STAR bank — T090, ported from ``src/jobfinder/phase6``.

Two rules carried over unchanged, because breaking either sends someone into an
interview with a story they cannot defend:

**No invented scenarios.** A STAR story is assembled from the user's own
confirmed records. The records hold outcomes and duties, rarely the situation
or the task; those elements are marked ``needs_your_input`` with a question to
answer, never filled with something plausible. The user's own answers are
stored in ``worked_example`` as typed, not as extracted.

**Only the user's own answer is scored.** Questions come from this posting's
skill gap; the rubric (``config/v1/interview.json``) scores the text the user
typed and nothing else. No model answer is generated to compare against.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from functools import lru_cache
from typing import Any

from career_scout import config as config_module
from career_scout.store.db import utcnow

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s*%?")


@lru_cache(maxsize=4)
def _config(version: str = config_module.CURRENT_VERSION) -> dict[str, Any]:
    return config_module.load("interview", version)


@lru_cache(maxsize=4)
def _tool_words(version: str = config_module.CURRENT_VERSION) -> tuple[str, ...]:
    """Named tools and methods: the skill vocabulary, not one person's toolbox."""
    vocabulary = config_module.skill_vocabulary(version)
    words = set()
    for skill in vocabulary.get("skills", []):
        words.add(skill["canonical"].lower())
        words.update(s.lower() for s in skill.get("synonyms", []) if len(s) > 2)
    return tuple(sorted(words))


# ------------------------------------------------------------------ STAR bank


def star_bank(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One story per confirmed achievement; every element sourced or a question."""
    answers = {
        json.loads(r["record_ids"] or "[]")[0]: r
        for r in conn.execute("SELECT * FROM worked_example WHERE record_ids IS NOT NULL")
        if json.loads(r["record_ids"] or "[]")
    }
    stories = []
    for record in conn.execute(
        "SELECT id, field_path, value FROM profile_record WHERE superseded_by IS NULL "
        "AND confirmed = 1 AND field_path LIKE 'x_achievements[%].statement' ORDER BY field_path"
    ):
        statement = record["value"] or ""
        own = answers.get(record["id"])

        def element(name: str, prompt: str, own=own) -> dict[str, Any]:
            if own is not None and (own[name] or "").strip():
                return {"text": own[name], "source": "your answer", "status": "sourced"}
            return {"text": None, "source": None, "status": "needs_your_input", "prompt": prompt}

        situation = element("situation",
            f"What was going wrong before you started? For “{statement[:70]}” the record holds "
            f"the outcome but not the problem: the baseline, who was affected, why it mattered.")
        task = element("task", "What exactly were you asked to do, and what were you "
                               "accountable for? Assigned, or did you volunteer?")
        action = {"text": statement, "source": record["field_path"], "status": "sourced",
                  "record_id": record["id"]}
        if own is not None and (own["result"] or "").strip():
            result = {"text": own["result"], "source": "your answer", "status": "sourced"}
        elif _NUMBER.search(statement):
            result = {"text": statement, "source": record["field_path"], "status": "sourced",
                      "record_id": record["id"]}
        else:
            result = {"text": None, "source": None, "status": "needs_your_input",
                      "prompt": "What was the measured result, over what period, against what "
                                "baseline?"}
        parts = {"situation": situation, "task": task, "action": action, "result": result}
        sourced = sum(1 for p in parts.values() if p["status"] == "sourced")
        stories.append({
            "record_id": record["id"], "title": statement[:90], **parts,
            "completeness": sourced / 4, "ready": sourced == 4,
            "gaps": [n for n, p in parts.items() if p["status"] != "sourced"],
        })
    return stories


def save_star_answer(
    conn: sqlite3.Connection, record_id: str, *, situation: str = "", task: str = "",
    result: str = "",
) -> dict[str, Any]:
    """The user's own words for a story's missing elements. Stored as typed."""
    row = conn.execute("SELECT value FROM profile_record WHERE id = ? AND confirmed = 1 "
                       "AND superseded_by IS NULL", (record_id,)).fetchone()
    if row is None:
        raise KeyError(f"no confirmed achievement {record_id}")
    existing = conn.execute("SELECT id FROM worked_example WHERE record_ids = ?",
                            (json.dumps([record_id]),)).fetchone()
    if existing:
        conn.execute("UPDATE worked_example SET situation = ?, task = ?, result = ? WHERE id = ?",
                     (situation, task, result, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO worked_example (id, label, situation, task, action, result, record_ids, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), (row["value"] or "")[:80], situation, task, row["value"], result,
             json.dumps([record_id]), utcnow()),
        )
    return next(s for s in star_bank(conn) if s["record_id"] == record_id)


# ------------------------------------------------------------------ questions


def questions(conn: sqlite3.Connection, opportunity_id: str) -> list[dict[str, Any]]:
    """Questions grounded in this posting's skill gap. They state no fact about the user."""
    row = conn.execute(
        "SELECT o.title, o.country_iso2, a.inputs FROM opportunity o "
        "LEFT JOIN assessment a ON a.opportunity_id = o.id WHERE o.id = ?", (opportunity_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no opportunity {opportunity_id}")
    gap = json.loads(row["inputs"] or "{}").get("skill_gap", {}) if row["inputs"] else {}
    limits = _config()["questions"]
    out: list[dict[str, Any]] = []
    for skill in gap.get("matched", [])[: limits["matched_skills"]]:
        out.append({"id": f"skill:{skill}", "competency": skill,
                    "text": f"Tell me about a time you used {skill} to solve a real problem. "
                            f"Walk me through what you actually did.",
                    "why": f"The posting names {skill} and your confirmed record claims it — "
                           f"expect to back it up."})
    for skill in (gap.get("critical_missing") or gap.get("missing") or [])[: limits["gap_skills"]]:
        out.append({"id": f"gap:{skill}", "competency": skill,
                    "text": f"This role involves {skill}, which is not on your CV. How would "
                            f"you approach it?",
                    "why": f"{skill} is a gap for this posting. A prepared, honest answer beats "
                           f"being surprised."})
    out.append({"id": "motivation", "competency": "Motivation",
                "text": f"Why this role — {row['title']} — and why now?",
                "why": "Asked in almost every interview."})
    residence = conn.execute(
        "SELECT value FROM profile_record WHERE field_path = 'basics.location.countryCode' "
        "AND confirmed = 1 AND superseded_by IS NULL").fetchone()
    if row["country_iso2"] and residence and residence["value"] != row["country_iso2"]:
        out.append({"id": "mobility", "competency": "Mobility",
                    "text": f"This role is in {row['country_iso2']}. Talk me through your "
                            f"relocation and work authorisation.",
                    "why": "It is in a country other than the one you live in, so it will come "
                           "up early."})
    return out


def score(answer: str, posting_skills: list[str]) -> dict[str, Any]:
    """Score one typed answer against the published rubric."""
    config = _config()
    rubric, band = config["rubric"], config["length_band"]
    text = (answer or "").strip()
    lowered = text.lower()
    words = len(text.split())
    if words == 0:
        return {"total": 0.0, "components": {}, "feedback": ["No answer given."], "word_count": 0}

    feedback: list[str] = []
    present = [n for n, patterns in config["star_markers"].items()
               if any(re.search(p, lowered) for p in patterns)]
    missing = [n for n in config["star_markers"] if n not in present]
    if missing:
        feedback.append(f"No clear {', '.join(missing)}. Say what the situation was and what "
                        f"changed.")
    numbers = len(_NUMBER.findall(text))
    tools = sum(1 for tool in _tool_words() if re.search(rf"\b{re.escape(tool)}\b", lowered))
    timeframes = len(re.findall(config["timeframe_pattern"], text, re.IGNORECASE))
    if numbers == 0:
        feedback.append("No numbers. A measured before and after is the strongest evidence.")
    if tools == 0:
        feedback.append("No named tools or methods. Name them.")
    hits = sum(1 for s in posting_skills if s.lower() in lowered)
    if posting_skills and hits == 0:
        feedback.append(f"Doesn't connect to what the posting asked for "
                        f"({', '.join(posting_skills[:3])}).")
    if words < band["min_words"]:
        length = 100.0 * words / band["min_words"]
        feedback.append(f"Too short at {words} words — aim for {band['min_words']}-"
                        f"{band['max_words']}.")
    elif words > band["max_words"]:
        length = max(band["long_floor"],
                     100.0 - (words - band["max_words"]) * band["long_penalty_per_word"])
        feedback.append(f"Too long at {words} words — trim toward {band['max_words']}.")
    else:
        length = 100.0

    scores = {
        "structure": 100.0 * len(present) / 4,
        "specificity": 100.0 * min(1.0, (numbers + tools + timeframes) / 4),
        "relevance": 100.0 * min(1.0, hits / 3) if posting_skills else 100.0,
        "length": length,
    }
    components = {name: {"score": round(value, 1), "weight": rubric[name]["weight"],
                         "formula": rubric[name]["formula"]} for name, value in scores.items()}
    components["structure"]["detected"] = present
    components["specificity"].update(numbers=numbers, tools=tools, timeframes=timeframes)
    total = sum(c["score"] * c["weight"] for c in components.values())
    return {"total": round(total, 1), "components": components,
            "arithmetic": " + ".join(f"({c['score']} x {c['weight']})"
                                     for c in components.values()) + f" = {total:.1f}",
            "feedback": feedback or ["Structured, specific, and tied to the posting."],
            "word_count": words}
