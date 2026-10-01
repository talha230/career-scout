# Architecture decision records

Decisions taken during the build that a reader would otherwise have to reverse-engineer,
plus the ones where I departed from the brief and why.

---

## ADR-001 — SQLite as the committed default, PostgreSQL supported

**Context.** The build spec calls for a PostgreSQL schema. No PostgreSQL server is
installed on this machine and installing one was outside what the task authorised.

**Decision.** The schema is defined with SQLAlchemy ORM models that are dialect-agnostic.
`JOBFINDER_DATABASE_URL` selects the backend; it defaults to `sqlite:///data/jobfinder.db`.

**Consequence.** Switching to PostgreSQL is a connection string, not a rewrite. Two caveats:
the `raw_payload` column maps to `JSON` on SQLite and would be better as `JSONB` on
Postgres for indexing, and the dedupe queries have not been tested against Postgres'
collation rules.

**Status.** Flagged for your decision. Nothing depends on it until the dataset grows.

---

## ADR-002 — The original spec's numbered sections were not supplied

**Context.** The build prompt repeatedly cites "original §7, §8, §9, §10, §11, §15, §18,
§19, §21, §22, §24, §27". Only §8 (the nine scoring weights) is reproduced inline. The
other document was not provided; the only other file in Downloads was an unrelated
manufacturing-dashboard brief.

**Decision.** I built to the sections I had, and *derived* the rest — hard filters, tier
cut-offs, pipeline statuses, cover-letter structure, email categories, QC checks.

**Consequence.** Every derived artefact carries a `spec_note` field saying it was derived
and inviting correction. They are all config files, so replacing them with the real
definitions is an edit, not a rebuild:

| Section | Derived into |
|---|---|
| §9 hard filters | `config/hard_filters.json` |
| §10 skill-gap categories | `config/skill_synonyms.json` + `phase3/skills.py` |
| §11 tiers | `config/tiers.json` |
| §15 cover letter | `config/document_structure.json` |
| §18 pipeline statuses | `config/pipeline_statuses.json` |
| §19 email categories | `config/email_categories.json` |
| §21/§22 STAR & simulator | `phase6/star_bank.py`, `phase6/interview_sim.py` |
| §24 QC agent | `config/document_structure.json` → `qc` |
| §7 DB schema | `phase2/db.py` |

**If you have that document, send it.** These are the rules that decide which jobs you
never see.

---

## ADR-003 — Unscored ≠ zero, and unscored ≠ 50

**Context.** Several score components cannot be computed from real data. Salary is the
worst case: you have stated no expectation, and 90% of postings disclose nothing.

**Decision.** A component that cannot be computed is recorded as `UNSCORED` with a written
reason. Its weight is removed and the remaining weights renormalise to 1.0.

**Why not zero.** It would rank a job with an undisclosed salary below an identical job
with a bad one. That is an invented judgement.

**Why not a neutral 50.** A 50 is a fabricated number wearing the costume of a measurement.

**Consequence.** Scores are comparable only alongside their confidence label, so confidence
is displayed everywhere a score is, and a high score built on thin evidence is capped at
Tier B.

---

## ADR-004 — Fuzzy URL matching removed from dedupe

**Context.** The spec asks for dedupe on "company + title + location + URL similarity". The
first implementation used `SequenceMatcher` on URLs above 0.92.

**Problem.** Two postings on the same board differ only in a numeric id, so unrelated jobs
score 0.92–0.97. It merged 818 of Databricks' 819 postings into one. Separately,
canonicalisation was stripping the entire query string, and Greenhouse puts the job id in
`gh_jid` — so every job on some boards collapsed to one URL.

**Decision.** URL matching is exact-after-canonicalisation only. Canonicalisation strips
campaign parameters (`utm_*`, `gh_src`, `ref`) and keeps everything else.

**Result.** Duplicate rate fell from 75% to 5.5%, and the surviving merges are genuine
(the same posting listed twice, or the same role cross-posted). Regression-tested in
`tests/test_phase2_dedupe_and_salary.py`.

---

## ADR-005 — Seniority is signal, not noise

**Context.** Title normalisation stripped `senior|junior|lead|principal|staff` before
fingerprinting, to merge cosmetic variants.

**Problem.** "Staff Software Engineer" and "Senior Software Engineer" at one company
produced an identical fingerprint and merged. They are different jobs with different pay.

**Decision.** Strip only genuine boilerplate — `(m/w/d)`, `(all genders)`, contract type,
work mode. Seniority and level numerals survive.

---

## ADR-006 — Career alignment uses token overlap, not character similarity

**Context.** Career alignment used `SequenceMatcher` on the title against your target roles.

**Problem.** "Firmware Engineer" vs "Process Engineer" scored 0.75 — purely for sharing
"Engineer". The first ranking run put Firmware Engineer, Android BSP Engineer and Payroll
Analyst in the top 20.

**Decision.** Jaccard overlap of token sets, with a containment bonus when every word of a
target role appears in the title. "Firmware/Process" is now 1 of 3 tokens = 33.

---

## ADR-007 — Skill match is unscored below three recognised skills

**Context.** Skill match is a ratio over the skills the vocabulary recognises in a posting.

**Problem.** A firmware role mentioning only "Python" and "root cause analysis" scored
100% on skills — a perfect match to a job requiring nothing you can do.

**Decision.** Below three recognised skills the component reports UNSCORED, with the
reason that the posting is probably outside your domain. Threshold lives in
`config/scoring_weights.json`.

---

## ADR-008 — "Company Quality" is renamed in the UI and carries a disclaimer

**Context.** §8 allocates 5% to Company Quality. The honest sources for that (Glassdoor and
similar) are prohibited by Rule 2, and no licensed employer-rating dataset is configured.

**Decision.** The component is computed from three observable signals already in the
database — posting completeness, pay transparency, and how many roles that company has
open in our data. It is labelled **"Company signals"** in the dashboard and carries a note
stating plainly that it is not a measure of what the company is like to work for.

**Alternative rejected.** Dropping the component would silently reweight the other eight
against a spec that allocates it 5%. Inventing a reputation score would be worse.

---

## ADR-009 — Data integrity and application readiness are separate verdicts

**Context.** Phase 1's DoD is that `validate_profile.py` passes. Two blocking questions are
unanswered (your current employment status, and your expired English test).

**Decision.** The validator reports two verdicts. **Data integrity** covers fabrication,
placeholders, broken provenance and impossible dates — currently PASS. **Application
readiness** covers unanswered blocking questions — currently BLOCKED.

**Rationale.** An incomplete profile is a different condition from a wrong one. The file is
honest; it is just not yet safe to generate submissions from. `--require-ready` is the gate
Phase 5 uses.

---

## ADR-010 — The dashboard has no backend, and statuses round-trip through a file

**Context.** §27 calls for a React/PWA dashboard. A static site cannot write to the database.

**Decision.** `phase4/export_data.py` writes real pipeline output to `dashboard/public/data`.
Status changes are held in `localStorage`, exported as JSON, and written back with
`phase4/import_statuses.py`.

**Consequence.** The round-trip is friction, and that is the point: reaching "applied" is
recorded as `changed_by='human'`, which is where the Rule 6 approval gate is persisted.

**Also.** A single combined `jobs.json` was 25 MB because the per-component explanatory text
repeated on all 2,214 rows. It is now a 2 MB index plus per-job detail files, with the
static text emitted once into `scoring_reference.json`.

---

## ADR-011 — `--verified-only` exists because almost nothing about you is third-party verified

**Context.** Phase 5's DoD says every claim in a generated CV should map to a
`verified: true` record. Of your documents, only the two employer letters, the degree, the
PTE report, the passport and one academic reference are third-party. Your achievements,
skill levels, certifications and school results exist only in documents you wrote yourself.

**Decision.** Two modes. Default allows self-reported content — normal for a CV — but the QC
agent reports every such claim as `QC6_unverified_claim`, so it is never invisible.
`--verified-only` produces the thinner, fully-attested document the DoD describes literally,
and records what it dropped and why.

**What this means for you.** Six of your certifications and two of your school
qualifications currently have no supporting document. See open questions Q5 and Q6.
