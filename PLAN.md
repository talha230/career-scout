# Jarvis — executable plan

A local-first job and scholarship agent. Runs entirely on the user's own machine, stores
everything in their own SQLite database and their own Gmail, and exposes itself as an MCP server
that both **Claude Code** and **OpenAI Codex** can drive conversationally. A localhost web app
shows progress, opportunities, applications and reply tracking, and installs as a PWA on a phone
over the same Wi-Fi.

**Free at 100 users because nothing is shared.** There is no server, no hosted database, no egress
quota and no per-tenant isolation to get wrong. A hundred users is a hundred independent installs,
each bringing its own disk, its own Gmail quota and its own Google Cloud project. Cost to the
operator is zero at any user count; cost to the user is zero.

---

## 0. Install

```bash
pip install pipx && pipx install jarvis-agent
```

Development install from this repository:

```bash
pip install -e ".[dev]"
```

Runtime dependencies (`pyproject.toml`, exact pins resolved at T002):

| Group | Packages |
|---|---|
| Core | `sqlalchemy>=2.0.30`, `pydantic>=2.7`, `httpx`, `typer`, `rich`, `platformdirs`, `python-dateutil`, `jsonschema` |
| Web | `fastapi`, `uvicorn[standard]`, `apscheduler`, `jinja2` |
| MCP | `mcp>=1.2` (official Python SDK, stdio transport) |
| Documents | `pypdf`, `pdfplumber`, `python-docx`, `docx2pdf` fallback `weasyprint` |
| Google | `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2` |
| Crypto | `cryptography` (local-key encryption of `restricted` documents) |
| Dev | `pytest`, `pytest-cov`, `pytest-asyncio`, `ruff`, `vite` (web UI build) |

First run:

```bash
jarvis setup          # guided: data dir, Google OAuth client, profile import
jarvis serve          # http://localhost:8765  (also LAN: http://<your-ip>:8765)
jarvis run            # one full pipeline pass
jarvis mcp            # stdio MCP server (normally launched by Claude/Codex, not by hand)
```

### Wiring the MCP server

**Claude Code** — `claude mcp add jarvis -- jarvis mcp`, or in `.mcp.json`:

```json
{ "mcpServers": { "jarvis": { "command": "jarvis", "args": ["mcp"] } } }
```

**OpenAI Codex** — in `~/.codex/config.toml`:

```toml
[mcp_servers.jarvis]
command = "jarvis"
args = ["mcp"]
```

Both hosts speak the same stdio MCP protocol against the same server. No host-specific code.

---

## 1. Where everything lives

```
~/.jarvis/                       (platformdirs user_data_dir; overridable by JARVIS_HOME)
├── jarvis.db                    SQLite — the whole system of record
├── documents/
│   ├── source/                  uploaded CVs, letters, transcripts
│   ├── restricted/              passport, CNIC, tax, bank — encrypted at rest, local key
│   └── generated/               produced CVs, cover letters, worksheets
├── snapshots/                   fetched pages, for quote verification
├── config/                      versioned YAML: weights, filters, countries, sufficiency rules
├── credentials/                 client_secret.json + token.json, chmod 600
└── logs/
```

Nothing leaves this directory except: outbound HTTP to enumerated job sources, and the Gmail and
Drive APIs the user connected. **Both go through one audited module** (§6, invariant I-08).

**Gmail is the second system of record.** Every application is a real thread in the user's own
mailbox, labelled `Jarvis/Applied/<company>`, and every reply lands on that thread. If Jarvis is
uninstalled tomorrow, the user's complete application history is still in their Gmail, readable
without us. Drive holds one `drive.file` application record per application.

---

## 2. What was decided, and what it removes

| Decision | Consequence |
|---|---|
| **Data is local** | No GDPR controller burden, no Art 27 representative, no DPIA, no breach-notification apparatus, no processor agreements, no tenant isolation, no RLS, no cross-tenant tests. The user is the controller of their own data on their own machine. |
| **Each user owns their Google OAuth client** | The 100-user cap never applies — each project has exactly one user. No Google verification, no CASA assessment, no unverified-app ceiling. Refresh tokens do not expire (publish own consent screen to *In Production*; a Testing-status project expires refresh tokens every 7 days). |
| **AI comes from the MCP host, key optional** | Zero API cost when driven from Claude or Codex. Scheduled runs with no host attached are deterministic-only unless the user supplies a key; AI features then report `unavailable` with a reason, never an error or an empty result. |
| **Localhost + PWA over LAN** | No hosting bill, no public URL, no auth surface. Phone works on the same Wi-Fi. |
| **One SQLite per user** | The entire incremental-recompute apparatus is deleted. A full recompute of one user's ~2,300 assessments is a local query taking milliseconds against zero network egress. |

**Measured, from `data/jobfinder.db` (97.4 MB, 4,402 postings, 2,305 scores):** 16,478 B per
posting row, 7,067 B per assessment row, 184 B per rejection row. One user's full corpus is
therefore ~90 MB of local SQLite. That is the whole capacity question, and it is answered.

---

## 3. Invariants — tests that are never skipped

These survive the pivot because they were never about hosting. Each has a test; the suite fails
the build if any is skipped.

| # | Invariant | Test |
|---|---|---|
| I-01 | No send without a matching approval record | `test_send.py::TestApprovalRequired` |
| I-02 | Content changed after approval is refused | `test_send.py::TestContentBinding` |
| I-03 | Destination changed after approval is refused | `test_send.py::TestDestinationBinding` |
| I-04 | The sending mailbox is bound at approval | `test_send.py::TestDestinationBinding` |
| I-05 | An approval is consumed exactly once under concurrency | `test_send.py::TestSingleUse` |
| I-06 | A send that succeeds but fails to record is reconciled, never re-sent | `test_send.py::TestReconciliation` |
| I-07 | No bulk approve path exists, in the API or in MCP | `test_send.py::TestNoBulkPath, test_web.py::test_the_web_api_has_no_bulk_control` |
| I-08 | All outbound network traffic passes the audited chokepoint | `test_egress_chokepoint_runtime` |
| I-09 | No login, CAPTCHA, payment or account-creation capability exists | `test_forbidden_capabilities_absent` |
| I-10 | An untraceable claim blocks the document | `test_qc_blocks_untraceable_claim` |
| I-11 | Instructions injected into a posting produce no claim | `test_injection_produces_no_claim` |
| I-12 | An unconfirmed value never reaches an outbound document | `test_unconfirmed_excluded_from_output` |
| I-13 | Restricted document contents never enter a prompt or an output | `test_restricted_never_in_prompt_or_output` |
| I-14 | Send caps hold, per destination and overall, under concurrency | `test_send.py::TestCaps` |
| I-15 | A cap refusal leaves the approval valid and queued | `test_send.py::TestCaps` |
| I-16 | The queue releases in deadline order | `test_send.py::TestQueueOrder` |
| I-17 | Sufficiency blocks one opportunity, never the account | `test_profile.py::test_sufficiency_blocks_one_opportunity_not_the_account` |
| I-18 | Unparseable posting requirements fail **closed**, not open | `test_profile.py::test_unparsed_requirements_are_not_sufficient` |
| I-19 | Every score is reproducible by hand from stored inputs | `test_score_reproducible` |
| I-20 | A missing FX rate raises; there is no fallback | `test_missing_fx_rate_raises` |
| I-21 | The full pipeline runs with no AI available anywhere | `test_full_pipeline_no_ai` |
| I-22 | Quoted scholarship facts appear verbatim in the snapshot | `test_quote_in_snapshot` |
| I-23 | Thresholds come from settings, never from code constants | `test_thresholds.py (structural + behavioural, 8 tests)` |
| I-24 | Enabling a country leaves other countries byte-identical | `test_country_isolation` |
| I-25 | No MCP tool can create an approval or send without one | `test_mcp.py::test_no_approval_tool_exists, test_outbound_tools_require_the_content_hash` |
| I-26 | Retrieved content is never treated as instruction | `test_mcp.py::test_untrusted_content_is_wrapped` |
| I-27 | The generated Skill matches the live tool registry | `test_mcp.py::test_the_committed_skill_matches_the_registry` |
| I-28 | A contact erasure request removes the record globally | `test_contact_erasure_honoured` |
| I-29 | A backup restores into an empty data directory | `test_backup_restores` |
| I-30 | Rule approval is off by default, written only by the user on this computer (never MCP, never the LAN), applications only, and every rule approval records its rules and inputs | `test_rule_approval.py` (mutation-checked 2026-10-01) |

**Three rules carried from the review, because they were real bugs:**

- **I-08 is enforced at runtime, not by import-graph grep.** The test patches
  `socket.socket.connect` to raise unless the call stack passes through `jarvis.net`, then runs a
  full pipeline pass and a document render. An import scan cannot see a headless browser or
  `google-api-python-client`'s own transport; a socket patch sees everything.
- **I-14 uses `BEGIN IMMEDIATE`** around count-check, insert and `last_send_at` update. Counting in
  one statement and inserting in another is a race two concurrent senders win together.
- **I-18 makes an unparseable posting `not_evaluated`, which is not `sufficient`.** Measured
  evidence: in the legacy data 481 of 2,305 assessments recorded *"the posting names no skill in
  the vocabulary"*, and one posting scored 61.14 on the title `"Oops something happened"`. Empty
  requirements must not silently authorise an application.

---

## 4. Data model (SQLite, one file per user)

**Reference** — `country`, `source`, `opportunity`, `reference_figure`, `fx_rate`,
`config_version`.

`opportunity(id, kind, country_iso2, role_family, title, employer, work_arrangement, requirements
json, requirements_confidence real, description, raw_payload, apply_route, apply_target, deadline,
pay_disclosed json, pay_estimate json, funding_disclosed json, source_id, source_url NOT NULL,
fetched_at NOT NULL, dedupe_key, employer_norm, title_norm, expired_at)`

- `role_family` is populated at ingest from a versioned title-pattern table, with an explicit
  `unclassified` value that fails closed at sufficiency. It is load-bearing for sufficiency rules
  and agent dispatch, so it is a real column with a real deterministic source.
- `requirements_confidence` drives I-18.
- Text stays in SQLite. There is no egress quota to protect and no reason to split it out.

**Profile** — `profile_record(id, field_path, value, document_id, locator, confirmed,
superseded_by, created_at)`, `document(id, kind, path, sha256, restricted, extracted_ok,
extraction_note)`, `worked_example`, `settings`.

**Assessment** — `assessment(opportunity_id, match_score, confidence, formula, weights, inputs,
unscored_components, projection, passes_floor, floor_applied, floor_source, eligibility_verdict,
pay_basis, sufficiency, config_version, computed_at)`. One table — no hot/trace split, because
locally there is nothing to save. Every projection line carries both `reference_figure_id` **and
`fx_rate_id`**, so an explanation regenerates to the same numbers years later.

`rejection(opportunity_id, rule, reason, run_id)` — every hard filter, logged.

**Sufficiency** — `sufficiency_rule(version, document_type, role_family, kind,
required_field_paths, conditional_on, label, rationale)`,
`sufficiency_verdict(opportunity_id, verdict, rules_applied, fields_examined, missing_field_paths,
rule_version, computed_at)`.

**Application** — `application_package(id, opportunity_id, documents json, claim_trace json,
rendered_sha256, qc_verdict, blocked_reason)`,
`approval(id, package_id, approved_at, content_hash, rendered_hash, destination_hash,
destination_snapshot json, mailbox_credential_id, consumed_at)`,
`application(id, package_id, channel, status, sent_at, provider_message_id, gmail_thread_id,
gmail_label, drive_record_url, submission_evidence)`,
`application_status_history`, `reply(id, application_id NULL, gmail_message_id, classification,
classification_corrected_to, received_at)`.

`approval` binds five things, not two: content, **rendered bytes**, destination, route, and
**which mailbox it leaves from**. All five were holes the review found in the hosted design and
all five are one column each here.

**Outreach** — `contact(id, name, role, institution, email, published_source_url NOT NULL,
published_read_at, do_not_contact, notice_sent_at, erased_at)`,
`contact_suppression(email_sha256, kind, requested_at)` — global and permanent, checked in the
send path, holding a hash rather than an address.

**Operations** — `run(id, kind, started_at, finished_at, status, counts json, obstacles json)`,
`backup_record`, `send_log(sent_at, destination_country, kind)`.

---

## 5. Pipeline

```
jarvis run
  1  refresh reference data      → reference_figure (supersede, never update), fx_rate
  2  discover                    → per country × role family, incremental from high-water mark
  3  normalise, dedupe, parse    → opportunity + requirements + requirements_confidence
  4  filter                      → rejection rows, each naming its rule
  5  score                       → assessment with formula, weights, inputs, projection
  6  sufficiency                 → sufficiency_verdict per shortlisted opportunity
  7  generate                    → packages for sufficient opportunities, QC, claim trace
  8  poll gmail                  → replies classified, threads labelled, status history written
  9  digest                      → what is new, what needs approval, what unlocks the most
 10  backup                      → encrypted snapshot of ~/.jarvis to a chosen local/external path
```

Steps 1–6 and 8–10 need no AI and no key. Step 7 assembles from templates without AI; tailored
prose is an enhancement, not a requirement.

**Sending is not in the pipeline.** It happens only through `send_approved()`, called from the web
app or from an MCP tool, and only against an approval the user created by a deliberate act.

---

## 6. The two chokepoints

**Outbound HTTP** — `jarvis/net/__init__.py` is the only module that may open a socket. It
identifies itself by User-Agent, rate-limits per host, honours `robots.txt`, refuses any source
marked `manual_review_only`, and is GET-only except on the enumerated send channels. Verified by
I-08 at runtime.

**Sending** — `jarvis/send/__init__.py::send_approved(approval_id)`:

```
BEGIN IMMEDIATE
  claim    UPDATE approval SET consumed_at=now() WHERE id=? AND consumed_at IS NULL
  verify   approval exists · content_hash · rendered_hash · destination_hash ·
           mailbox_credential_id · channel opted in · deadline not passed ·
           per-country cap · global cap · minimum interval
  reserve  INSERT application(status='sending')  +  INSERT send_log
COMMIT
  call Gmail
  reconcile on provider_message_id; label the thread; write the Drive record
```

Nine conditions. Counted once, here, and cited everywhere else rather than re-listed.

Caps default to 10 per destination country and 20 in total per rolling 24 hours, with a 4-minute
minimum interval — protecting the user's own Gmail reputation, which is the real risk. They are
settings, not constants (I-23). The queue releases in ascending deadline, nulls last, then
approval time (I-16), so a long backlog does not send tomorrow's deadline next week.

---

## 7. Web application

FastAPI serves a Vite-built SPA from `jarvis/web/static`, bound `0.0.0.0:8765`, with a PWA manifest
and service worker so a phone on the same Wi-Fi can install it.

| Route | Shows |
|---|---|
| `/` | Progress: profile completeness as a bar, Minimum Viable Profile as a checklist, last run and its age, counts by stage |
| `/opportunities` | Ranked shortlist — score, formula, weights, inputs, confidence, every sourced projection line with its date, sufficiency verdict |
| `/opportunities/:id` | Full posting, the arithmetic behind the score, what is missing and what it would unlock |
| `/queue` | Awaiting approval. One package per screen, its claim trace beside it, its destination shown in full. No bulk control exists anywhere |
| `/applications` | Every application: status, history with actor and timestamp, linked Gmail thread, replies and their classification |
| `/tracking` | Reply inbox — classified, correctable, unlinked replies retained not discarded |
| `/outreach` | Contacts with their published source, notice status, suppression |
| `/settings` | Thresholds, countries, channels, caps, Google connection, AI key, data directory, backup |
| `/health` | Last run per kind and its age, backup records and last restore drill, disk use |

Stale data is shown as stale — every screen carries the age of the run that produced it.

---

## 8. MCP surface

One server, `jarvis mcp`, stdio, identical for Claude Code and Codex. It is a **client of the local
API** — it holds no database handle, implements no rule, and introduces no path the web app does
not already have.

**Read** — `get_profile_status`, `list_opportunities`, `get_opportunity`, `explain_score`,
`list_queue`, `get_package`, `list_applications`, `get_application`, `list_replies`,
`get_allowance`, `get_capability_status`, `get_run_health`.

**Write, not outbound** — `upload_document`, `confirm_profile_field`, `correct_profile_field`,
`set_threshold`, `set_country_enabled`, `generate_package`, `classify_reply_correction`,
`add_contact`, `suppress_contact`.

**Outbound, approval-gated** — exactly two: `send_approved_application(package_id, content_hash)`
and `send_approved_outreach(outreach_id, content_hash)`. Both take the hash as a required
parameter, so a model must name what it believes it is sending. Both refuse on any of the nine
conditions.

**There is no `approve_package` tool and there will not be one.** Approval is a human act in the
web app. A model may tell the user something is waiting; it may send what they already approved;
it cannot stand in for them. No tool takes an array, a filter, or an `_all` suffix.

Retrieved posting text and email bodies are wrapped in an untrusted-content envelope before they
reach a model (I-26). The Claude Skill in `.claude/skills/jarvis/SKILL.md` and the Codex tool
descriptions are both **generated** from the registry by `jarvis gen-skill`; CI fails on drift
(I-27).

---

## 9. Tasks

Test-first: every `(inv)` task writes a failing test before the task that makes it pass.

### Phase 0 — Package (T001–T008)

- [x] T001 Delete `specs/`, `.specify/`; keep `src/jobfinder/`, `data/`, `config/`, `tests/`, `Scholorship Finder/`
- [x] T002 `pyproject.toml` — package `jarvis`, entry point `jarvis = jarvis.cli:app`, pinned deps from §0
- [x] T003 Package skeleton: `jarvis/{cli,store,net,send,discovery,matching,money,eligibility,profile,documents,gmail,web,mcp,agents}/`
- [x] T004 `jarvis/store/paths.py` — `JARVIS_HOME` resolution via `platformdirs`, directory creation, `chmod 600` on `credentials/`
- [x] T005 `jarvis/store/schema.py` + migration runner; SQLite `WAL`, `foreign_keys=ON`, `busy_timeout`
- [x] T006 `pytest` fixtures: temp `JARVIS_HOME`, seeded DB, frozen clock, offline socket guard
- [x] T007 `ruff` + `pytest` CI on pull request and merge only, not every push. `.github/workflows/ci.yml`: on `pull_request` and `push` to `main`; installs, lints, runs the whole suite (invariants never skipped, I-27 fails on Skill drift), rebuilds the SPA and fails if the committed `web/static` is stale. **Not yet exercised:** this directory is not a git repository, so the workflow has never run on GitHub
- [x] T008 `jarvis --version`, `jarvis doctor` — environment, disk, permissions, Google connection, last run

### Phase 1 — Chokepoints and safety (T009–T016)

- [x] T009 **(inv)** `test_egress_chokepoint_runtime` — socket patch, full pipeline, document render. **Correction 2026-09-25:** until then the file held the guard and module-level checks but **no full pipeline pass and no render under the guard** — the test named in §3 did not exist. Both now do: `test_rendering_a_package_opens_no_connection`, and `test_egress_chokepoint_runtime`, which runs discovery (mock transport), screening, sufficiency, generation, rendering and backup under the socket guard
- [x] T010 `jarvis/net/` — the only socket owner. UA, per-host rate limit, `robots.txt`, `manual_review_only` refusal, GET-only outside send channels; T009 passes
- [x] T011 **(inv)** `test_forbidden_capabilities_absent` — tripwire denylist, documented as early warning not control. **Correction 2026-09-25:** this was ticked but **the test did not exist**; found when §3 was checked test-by-test against the suite. `tests/invariants/test_forbidden.py` now scans the package for browser automation, CAPTCHA solvers, payment SDKs and login/sign-up/checkout code paths and MCP tool names, with a self-test proving the patterns fire. The §3 table was also corrected to name the tests that actually exist for every invariant (many were covered under other names, e.g. `TestApprovalRequired` for I-01); all 29 now resolve to a real test
- [x] T012 Render PDFs with network disabled; no browser may resolve a remote host. **Resolved by construction:** the renderer is pure `python-docx` and the output is DOCX, the ATS-standard format; there is no PDF engine and no browser in the path. `test_rendering_a_package_opens_no_connection` generates and renders a full package under the socket guard, so a PDF step, font fetch or browser added later must pass it too
- [x] T013 **(inv)** `test_restricted_never_in_prompt_or_output`. **Observed failing first**, on a real defect: `documents/store.py` said restricted files were "copied, hashed and encrypted" while `ingest()` `shutil.copy2`'d them in plaintext — the test found the passport text at `documents/restricted/<id>.txt`. The old `test_restricted_documents_go_to_the_encrypted_store` only checked the directory, so it passed against plaintext. The new test scans every file under JARVIS_HOME, every table, the service outputs and the MCP read tools for a marker
- [x] T014 Restricted-document envelope encryption, local key in `credentials/`, one audited decrypt point — the user opening their own file. `documents/vault.py`: per-file AES-256-GCM key wrapped under `credentials/data.key`, document id bound as associated data (a file cannot be swapped under another row); the SHA-256 is of the original bytes; plaintext never touches disk. **The module states plainly that the process can decrypt**; the property is the single access point `open_restricted`, which writes `restricted_access` (migration 006) before returning bytes, and is reachable only from the web app on loopback — not from MCP. Tampered ciphertext raises; a missing key raises `KeyMissing`; T013 passes
- [x] T015 **(inv)** `test_missing_fx_rate_raises`, and `jarvis/money/fx.py` with no fallback. Four refusals, each one a number a user could not check otherwise: no nearest-date fallback, no triangulation through a third currency (two rates from two sources presented as one), no implicit identity for an unreadable currency, and no silently ageing rate — `current_rate` returns the newest rate **with its date** and refuses past `fx_rate_max_age_days`. One thing it does do: read a stored pair backwards at `1 / rate`, labelled `inverted`, because a rate table holds USD->PKR and a PKR salary still has to reach USD
- [x] T016 **(inv)** `test_thresholds_from_settings` — no threshold is a code constant. Two halves, because either alone passes while the property is broken: a structural half asserting every declared threshold is read by name somewhere outside `DEFAULTS` (a threshold that is declared and then hardcoded at its call site still appears on the settings screen, still accepts a new value, and still changes nothing), and a behavioural half that changes each threshold and asserts the answer moves. Thresholds whose consumer is not built yet are listed with the task that will read them, and a separate test fails once one of them *is* read

### Phase 2 — Profile (T017–T027)

- [x] T017 **(inv)** `test_unconfirmed_excluded_from_output`; confirm FAILS
- [x] T018 `extract/text.py` — PDF text layer + DOCX, page and character-offset locators
- [x] T019 `extract/scanned.py` — **detect a missing text layer and report the document as un-extracted** rather than returning silence. Optional OCR if `pytesseract` is present; never a silent empty result
- [x] T020 `extract/sections.py` — experience, education, skills, publications, references
- [x] T021 `extract/fields.py` — deterministic extraction of what can be read reliably: contact details, dates, employers, institutions. Writes `profile_record` with `confirmed=false` and a locator
- [x] T022 `extract/proposals.py` — AI-assisted proposals via the MCP host, **validated by the deterministic layer before storage**. MCP tools `get_document_text` (non-restricted only, wrapped untrusted) and `propose_profile_field`: stored only if the field path is known and unrestricted, the quoted passage appears verbatim in the document (the locator is found by Jarvis, not taken from the model), and the value appears inside the passage; stored with the document id and **unconfirmed**. The optional-API-key path for unattended runs is **not built** — capability reporting stays host-only, which is the honest state
- [x] T023 `extract/prefill.py` — candidate pre-fill from document text for the rest, so a keyless user confirms rather than types. **A typed value is never recorded as extracted**
- [x] T024 Document upload, `restricted` routing, SHA-256, `document` rows
- [x] T025 Confirm/correct by supersession, originals retained
- [x] T026 `completeness()` (progress only, gates nothing) and `minimum_viable_profile()` (the checklist that unlocks scheduled runs)
- [x] T027 Import `data/master_cv.json`, `data/star_bank.json` and the document manifest as account data

### Phase 3 — Discovery and scoring (T028–T041)

- [x] T028 **(inv)** `test_country_isolation` — frozen fixtures, canonicalised projection, `computed_at`/`run_id`/`fetched_at` excluded **by name**; confirm FAILS. **Not observed failing first**, and worth saying why rather than hiding it: T031's machinery was written in the same pass, and isolation holds *structurally* — nothing downstream of ingest reads `country.enabled`, because enablement decides what is searched and never how a posting is read. So the test guards the first change that breaks that, and to show it can fail at all it carries a sensitivity check (a newer Berlin rent figure moves DE's canonical projection) and a check that `as_of` survives canonicalisation while the three named fields do not — a test that dropped "anything timestamp-shaped" would also drop the field a bug would change. Six frozen postings (US, GB, DE, AU, PK, IT), each with its own sourced figures and rates; switching IT on, and AU off, leaves every other country's full assessment row byte-identical, and a disabled country's stored posting still resolves, scores and projects
- [x] T029 **(inv)** `test_score_reproducible`, `test_quote_in_snapshot`, `test_full_pipeline_no_ai`. The reproducibility tests recompute the total from the stored row alone — no engine involved — and check that the effective weights of the scored components sum to 1 at full precision and that every dropped component's weight is 0 with a reason; a second test does the same to the projection, summing its income and cost lines back to the stored net. I-22 needed a store: `jarvis/store/snapshots.py` writes content-addressed snapshots with a `.meta.json` naming the URL and fetch time, and `reference.add_figure` now **refuses a figure whose quote is not in the page it names** (`QuoteNotInSnapshot`). That caught 37 of this repository's own tests citing snapshot paths that were never written, which is exactly the hole the invariant exists to close; they go through one helper that stores a real page. The comparison unescapes entities, folds NFKC and strips tags before matching, because a quote read from a rendered page is separated by markup in the source — requiring a byte-for-byte match would fail every real snapshot while proving nothing. `test_full_pipeline_no_ai` patches `httpx.Client.request`/`send` to raise and then runs ingest-parsed requirements, role family, filters, scoring, projection, eligibility and the read side the UI and MCP call
- [x] T030 Port `src/jobfinder/phase2` to `jarvis/discovery/`, every request through `net/` — split, not one task:
  - [x] T030a `normalize.py` — company/title/text normalisation, location resolution, salary (disclosed or NULL, never 0), visa signal, dedupe key
  - [x] T030b `dedupe.py` — the three rules in confidence order, each merge writing which rule fired and its similarity. **Merge, never delete**: the later arrival is marked non-canonical and pointed at the canonical row, so a wrong merge is recoverable. Needed migration `002`: `opportunity_duplicate` for the evidence, plus `url_canonical` and `city` on `opportunity`
  - [x] T030c `authenticity.py` — reject postings that are not real vacancies. Two separate judgements, never conflated: **junk** (no real posting behind it — `"Oops something happened"`, `"CHECK BACK SOON"`, `"Test"`) and **fraud** (advance-fee, payment request, credential harvesting, contact-only-on-WhatsApp/Telegram). Each writes a `rejection` row naming the rule and quoting the evidence. **Measured over all 4,402 postings: 10 junk rejections, 0 fraud false positives.** **Then the first live run rejected 155 of Airbnb's 160 postings as fraud** on its own anti-scam notice — "We'll also never ask you to pay a fee, send money…" — plus an ABS posting declining to pay recruitment agencies. The August corpus contained neither sentence. The config's claim that every pattern named the applicant as payer was false for one pattern (bare `pay a fee`); it now requires `you`/`candidate`/`applicant` in the same clause, and every signal passes a **clause-bounded negation guard** whose vocabulary lives in `authenticity.json`. Clause, not sentence: "Don't worry, you must pay a registration fee" still rejects, and a demand after an anti-scam notice still rejects. Rechecked: August corpus unchanged at 10 junk / 0 fraud; live run 156 → 0 The first draft of the fraud rules rejected 8 real vacancies (Stripe, Airbnb, Samsara, Spotify) by matching the nouns "placement fee", "gift card" and "crypto payment" — companies that *build payment products* discuss them constantly. Every pattern now requires the applicant to be named as the payer; the five strings are locked in as regression tests
  - [x] T030d `ingest.py` and `clients.py`. Ingest is split from fetching on purpose: it is a pure function of a payload plus the database, so the whole stage is testable without a socket. Order is normalise → parse requirements → classify → authenticity → dedupe → store. **A rejected posting is still stored**, with a `rejection` row naming the rule. Dedupe runs *before* the insert, because an identical key is refused by a UNIQUE index — the same vacancy from a second board is a sighting in `opportunity_source`, not a row.
    - `clients.py`: `plan()` turns the enabled packs into requests via `countries.sources_for`, so nothing is fetched "just in case"; five provider mappers (RemoteOK, Arbeitnow, Greenhouse, Lever, Workable) onto `RawPosting`; `discover()` fetches, **snapshots every page before parsing it**, ingests one transaction per source and writes one `run` row whatever happens. **One source failing never fails the run** — it becomes an obstacle with its reason, and the run is `partial`. **Retries are GET-only and live in the client, not the chokepoint**, because `Fetcher` also carries the Gmail send path and a retried POST is a duplicate email; counts and backoff come from `sources.json`'s etiquette block, and a 404 is an answer, not retried. A posting is dropped only when it resolves to a country whose pack is **switched off**, and is counted `out_of_scope` with its country; a posting in an unpacked country or none is kept, since excluding it would narrow the search without anyone deciding to. The high-water mark stops Arbeitnow paging once a page is entirely older; the other four return the whole board in one response, and re-seeing a known posting is the sighting that moves `last_seen_at`. `jarvis discover` runs it by hand. Tests drive the real `Fetcher` — guards, robots, limiter — over `httpx.MockTransport` with one real payload per provider frozen from `data/jobfinder.db`.
    - **Two foreign keys would have failed the first real fetch.** `opportunity.country_iso2` and `opportunity.source_id` reference tables nothing in production populated — the ingest tests seeded their own. `countries.install()` now writes a reference row (`enabled = 0`, no floor, not a pack) for every country location resolution can return, and `discover()` registers a `source` row per planned fetch.
    - **Lever dates.** `createdAt` is epoch *milliseconds*; read as seconds it is a year around 55,000, which raised and returned `None`. **All 491 Lever postings in the legacy corpus had no date**, and since the staleness filter keeps an unknown date, a posting from March 2024 read as live. Fixed in `normalize.parse_datetime`. Separately, dates are converted to UTC before formatting: Greenhouse's `-04:00` offsets were being stamped `Z` four hours wrong on the field the staleness filter and the high-water mark compare.
    - **First live run, 2026-09-24**: 19 of 19 sources ok, 4,895 items, 4,739 stored, 48 out of scope, **every posting dated and every posting snapshotted**, 598 with disclosed pay. It found three faults the frozen corpus could not: a snapshot `.meta.json` path past Windows' 260-character limit (now written with the `\\?\` long-path prefix, and each of the two files is written if missing independently); an `OSError` while storing one source's page that escaped and failed every source after it (now that source's failure only); and the fraud rule below
- [x] T031 Country packs `pk, us, au, de, gb` enabled and `it, dk, se` disabled, each self-contained; T028 passes. `config/v1/country_packs.json` (fingerprinted) and `jarvis/discovery/countries.py`. A pack is the whole country-specific decision: searched or not, **which registered sources serve it** — named per pack, not inherited from a global default that would shift every country when one is added — and its default savings floor with its basis. **Every floor ships `null`**: none of the five has a sourced figure, and a plausible number there decides shortlists. PK then reports `UNSCORED — default not yet sourced` and the others fall through to the labelled USD 2,000 fallback, so nothing claims to be local data. `validate()` refuses a floor with no basis, a source not in `sources.json`, and an enabled pack with no sources (a country that looks covered and is searched by nothing). `install()` is idempotent and **never re-enables a country the user switched off**. `sources_for()` gives discovery its shape: one entry per source, naming the enabled countries that asked for it. The source notes record honestly how thin coverage is — 13 of 14 probed Greenhouse boards are US-headquartered, no PK- or AU-specific source exists yet (U2), and the PK pack names only `remoteok` rather than implying coverage it does not have. `jarvis setup countries` installs and shows the packs with each floor's resolution; `jarvis doctor` now reports the enabled countries **and runs the config validators, which both docstrings had long claimed it did and it did not** — a weight table that failed to sum to 1 was reported nowhere
- [x] T031a `arrangement.py` — classify `work_arrangement` as `remote`, `onsite` or `project`. `project` is the freelance/contract case and is a **first-class search target**, not a leftover. Four tiers, in descending order of what the source commits to: its own employment-type field, the title/tags/location, the body, and last an explicit inference from a published work location. An arrangement none of those reach stays NULL rather than defaulting to onsite. Measured over the corpus: 69.1% onsite, 18.4% remote, 0.9% project, 11.6% genuinely unreadable. The `project` share is low because every source in the corpus is an employer board — freelance marketplaces are T031b
- [x] T031b Remote and freelance source packs. **Himalayas and Jobicy enabled** (they were registered-but-disabled awaiting exactly this decision), with mappers from payloads captured live through `jarvis.net` on 2026-09-25 and frozen as fixtures. Terms read, not assumed: Himalayas (himalayas.app/api) — link back, name the source, no resubmission to other boards, polling beyond daily pointless; Jobicy (its own `friendlyNotice`) — credit with a link, applications to the original URL. Both satisfied by storing and showing the source URL and republishing nothing. Hourly pay is mapped to `hour`, never read as annual; an unknown period is stored `None`. Both named in every enabled pack. **Upwork, Fiverr, Freelancer.com registered `manual_review_only`**: no open, keyless listing API was found for this project and access needs a registered developer application — stated as "not verified further" rather than claimed. The `project` arrangement share will rise with Himalayas' contractor roles; it has not been measured on a live run yet
- [x] T032 **Requirement parsing at ingest** — split like extraction, not one task:
  - [x] T032a Requirement-section segmentation
  - [x] T032b Requirement-sentence identification
  - [x] T032c Vocabulary matching with a **coverage metric**, over a vocabulary widened past the 52 industrial-engineering skills in `config/skill_synonyms.json`
  - [x] T032d Required document-type and seniority detection
  - [x] T032e `requirements_confidence`, and a labelled fixture set of 150 postings drawn from `data/jobfinder.db`
- [x] T033 `role_family` classification from a versioned title-pattern table, `unclassified` explicit
- [x] T034 Port `src/jobfinder/phase3` scoring to `jarvis/matching/`; write formula, weights, inputs, unscored components with reasons and renormalisation
- [x] T035 Savings engine — on-site, remote against the user's own tax residence, undisclosed pay estimated from official statistics and ranked below disclosed. Every line stores `reference_figure_id` **and `fx_rate_id`**. `money/reference.py` holds the sourced figures (superseded, never updated) and reports **specificity**: a national average standing in for a city comes back `granularity='country'` and the line says so, because a country is not a rent. `money/savings.py` projects net USD/month, and the asymmetry is the load-bearing part — a net figure needs pay *and* tax *and* every required cost line, or it is `None` with its reasons, since a missing cost does not make the cost smaller, it makes the **saving look bigger**, which is the direction that moves somebody countries for a job that does not pay for itself. A remote role is taxed at the user's confirmed `tax_residence` and costed at their confirmed city; neither is inferred from citizenship or an address. The low end of a published range is used, because the top is a salary nobody was offered; an hourly rate converts under a stated full-time assumption carried on the line and drops confidence to `low`. Every period, unit and rate conversion lives in `config/v1/money.json` with its basis, so it is fingerprinted. `Projection.sort_key` puts every estimate below every disclosed figure. `score()` attaches the projection and `as_row()` fills `projection`, `passes_floor`, `floor_applied`, `floor_source` and `pay_basis`; an **unreadable posting is not projected at all**, because a savings figure on a posting nobody could read would be the one number on the screen that looked solid. `capability_status` reports the projection unavailable, naming what is missing, while no rates or figures are on file
- [x] T036 Scholarship funding — T = stipend + allowances + visa-capped work income, zero where prohibited or unverified; waivers reduce cost, never income. `money/funding.py` returns the **same `Projection`** as the salary engine and reuses its cost-of-living, tax and currency-normalisation path, so a shortlist holds both kinds and one screen explains either. Three asymmetries, each because the other choice flatters an award: **a waiver reduces the cost line to zero and is never added to income** (a 30,000 waiver on the income side reports a stipend as a salary, and the arithmetic then says a student can live on it); **unverified work income is an explicit zero line naming its reason**, since counting hours a visa may not permit at a wage nobody published invents a job, and zero understates rather than overstates; **tuition the scheme is silent about is charged in full**, because an award that says nothing about fees has not waived them. Permitted work is capped at the lower of the scheme's hours and the published `visa_rule` limit, and the line carries the assumption that those hours are actually worked. An award silent about tax is taxed at the country's effective rate with a note saying so. `funding_disclosed`'s shape is documented in the module that first consumes it, and `resolve_funding_floor` takes its own override before falling through to the savings floor — what you need left over does not depend on whether it arrived as a salary or a stipend
- [x] T037 Eligibility against the user's own citizenship, residence and qualifications; soft-hide with a reason, never delete. `jarvis/eligibility/` answers four questions — right to work, security clearance, qualification, language — each against the user's **own confirmed profile records** rather than against literals in a config table, which is what the legacy filters held (`candidate_languages: english, urdu, punjabi`, `citizenship: PK`). Every dimension returns `ok`, `blocked` or `unscored` with the posting sentence and the profile fact side by side, and only `ineligible` hides. Three rules keep the hide safe: **missing profile data never blocks** (no confirmed citizenship makes the clearance and right-to-work dimensions `unscored`, because blocking on absence hides the world from a user with an unfinished profile); **a demand that could not be read is `unscored`, not satisfied**; and **residence counts toward the right to work, saying that it assumed so** — the opposite error hides a job the person is entitled to hold, and nobody can ask about a posting they never saw. The engine is pure (no connection, no clock) and `score()` attaches the verdict to `eligibility_verdict` / `eligibility_detail` while the row, the score and the projection all survive.
  - `config/v1/eligibility.json` holds what excludes rather than what scores, because each entry is a judgement about how much a stated demand really excludes. **Only a doctorate blocks**: measured over the 4,402-posting corpus, 1,134 postings state a bachelor's, 396 a Master's, 326 a PhD, and treating a Master's demand as exclusion would soft-hide those 396 from a bachelor holder whom many of those employers would in fact interview. It is a scored shortfall instead; qualification blocks fell from 28 to 11 of the 445 postings that survive the hard filters.
  - Three demands are now read **at ingest**, where the text still exists, since no later stage re-reads a description: the named language with the phrase that demanded each one, the clearance evidence, and a citizenship demand as distinct from a refusal to sponsor. Measured: 436 postings demand English, 148 German, 38 Japanese, 37 French, and none demands fluency without naming a language the parser can read. Two corpus-driven corrections came out of this: language names that are ordinary English words (`polish`, `mandarin`, `dutch`, `thai`, `french`) count only when the posting capitalised them — "you will polish Land three or more platform-native apps" was producing a Polish fluency demand — and evidence is stored **per language**, so a posting asking for "Arabic and English fluency" cannot show the English sentence as the reason a user lacking Arabic was blocked.
  - **A real bug fixed on the way**: `NO_SPONSORSHIP` is tested before `SPONSORSHIP_OFFERED`, which matches the bare phrase "visa sponsorship", so a refusal the first pattern missed was read as an **offer**. "We cannot offer visa sponsorship" and "unable to provide visa sponsorship" both parsed as sponsorship offered — telling a user who needs a visa that a role refusing to sponsor them is open. Negated forms added; 13 of 4,402 postings corrected, 4 of them from `offered` to `refused` (Clera, Lightning AI), with no false positives in the changed set
- [x] T038 Hard filters writing `rejection` rows naming the rule; port `config/hard_filters.json`. The split from T037 is the substance of this task: **a hard filter asks about the posting** (is this a real, current, professional vacancy somewhere the user will consider) and rejects with a stored, reversible row; **eligibility asks about the reader** and soft-hides. The five profile-dependent legacy rules (F01 sponsorship, F02 clearance, F03 citizenship, F04 degree, F06 language) are `enabled: false` with a `moved_to` naming where they went, because each encoded one candidate's own situation as config literals. Five remain: F10 missing essential data, F08 non-professional title, F07 staleness, F05 experience far above documented, F09 excluded countries.
  - **An unknown never fires a filter**: a posting with no date (or an unreadable one) is not stale, and a user whose documented years cannot be established is not compared against a year requirement. Both are the same fault — treating a missing value as a fact — and here it costs a whole board.
  - F07's window is the `staleness_window_days` **setting**, not a number in the filter table, so one value governs what discovery re-fetches and what scoring rejects. F05's threshold stays generous at +6 years because a stated year requirement is often aspirational and experience is already a scored component. `apply()` is pure — both numbers arrive as arguments — and `screen()` resolves them from settings, applies the filters **before** scoring, and writes the rejection row.
  - A filter that asks to match the body **raises** rather than quietly matching the title: the posting view carries no description by design, and pretending otherwise would make a rule look like it worked. Body detection belongs at ingest.
  - **Reversibility is real, for both stages** (migration 003, `store/rejections.reconcile`). A re-assessment that passes *lifts* its stage's rows (`lifted_at`/`lifted_reason`/`lifted_run_id`) — never deletes; a still-rejected posting writes no repeat row. Authenticity is re-judged on every ingest sighting, which it previously never was: a rejection under a since-disabled rule stood for ever. `get_opportunity` shows lifted rows as `active: false`; `list_opportunities` carries a `rejected` flag.
  - Measured over the corpus at a frozen 2026-09-23: 3,957 of 4,402 rejected — 3,858 by staleness, 97 non-professional titles (Werkstudent, Intern), 2 on experience (Spotify asking 15 years, Match Group 13, against 6.62 documented). The staleness figure is an artefact worth knowing rather than a filter working well: the whole corpus was ingested on one day, 2026-08-08, so with a 45-day window every posting falls off the cliff together. Of the 445 survivors, eligibility reports 272 eligible, 116 ineligible and 57 unscored
- [x] T039 **(inv)** `test_unparsed_requirements_not_sufficient` — low `requirements_confidence` yields `not_evaluated`, which does not authorise generation; confirm FAILS
- [x] T040 **(inv)** `test_sufficiency_is_per_opportunity`, and `sufficiency()` as a set comparison over `sufficiency_rule`; T039 passes
- [x] T041 Threshold resolution — user override → country default with its recorded basis → USD 2,000. Pakistan's default stays null and reports `UNSCORED — default not yet sourced` until a figure is sourced

### Phase 4 — Documents, approval, send (T042–T056)

- [x] T042 **(inv)** `test_qc_blocks_untraceable_claim`, `test_injection_produces_no_claim`. **Written after the code, not before — said plainly.** To show they can fail, three mutants were run against `tests/invariants/test_documents.py` (QC9 switched off; the posting description leaked into a letter; the confirmed filter removed): each turned 2-3 tests red. The I-12 test `test_unconfirmed_excluded_from_output`, which T017 had ticked, **did not exist**; it does now, and asserts against the rendered DOCX
- [x] T043 Port `src/jobfinder/phase5` to `jarvis/documents/` — ATS DOCX, single column, standard headings, nothing in headers, footers, text boxes, layout tables or images. `generate.py` builds from **confirmed, unrestricted** `profile_record` values only; every line is `profile` (with the record ids behind it), `posting` (title/employer quoted in a fixed sentence) or `template`. The posting description is never read. `render.py` writes DOCX with the style variant; a test opens the file and asserts no tables, images, text boxes, header or footer text. An unconfirmed end date is written `(from Mar 2019)`, never `Present`
- [x] T043a Justified body text on prose paragraphs, with hyphenation off. Justification is a paragraph property, invisible to a text extractor, so it changes how the CV reads to a human and nothing about what an ATS parses. **Bullet lines and headings stay left-aligned** — justifying a short line stretches it into visible rivers of whitespace, which reads worse, not better
- [x] T043b `style_variant.py` — a per-account presentation variant, derived deterministically from the account id, so two people using Jarvis do not send out two visibly identical documents. **20,995,200 distinct looks** across 14 dimensions; derived once and stored in `setting`, so a retuned default cannot change a CV's look between two applications to one employer. One asymmetry worth knowing: `yyyy_only` dates are dropped in favour of `mon_yyyy` whenever the profile actually holds month precision — style never discards substance. **Presentation only**: typeface pairing, heading treatment, rule weight, spacing scale and section order among the orderings the structure permits. It is seeded and stored, not random, so a candidate's second application to the same employer looks like the same person wrote it. It **never varies content, claims, wording or section membership** — a variant that changed what the document says would be fabrication wearing a typeface (I-10, I-12), and the QC and claim trace run identically for every variant
- [x] T044 Claim trace mapping every sentence to a `profile_record` id; T042 passes. Stored on the package as `{line: [record ids]}`
- [x] T045 QC blocking with the offending line and reason; omission report for excluded unconfirmed values. `documents/qc.py`: **QC9 words-not-in-sources** replaces the legacy global number check — every word and number of a line must appear in *that line's own* sources, so "12" from one record cannot pass in a line citing another (the legacy QC allowed any number found anywhere in the CV). QC1 untraceable/unconfirmed/restricted/superseded, QC5 placeholders, QC8 empty letter part, QC10 a quoted posting field that reads as a first-person claim or instruction (deliberately narrow: "Systems", "Prompt Engineer" and "Candidate Experience" are real titles). Unconfirmed values are QC11 warnings and listed in `omissions`; restricted values appear in neither
- [x] T046 **Render once, at generation**, and store `rendered_sha256`. Nothing is re-rendered at send. Two hashes per package (migration 004): `content_sha256` over the lines, sources, route and destination — what a person approves — and `rendered_sha256` over the files. The Gmail transport re-reads each file and refuses (condition 3) if a byte differs from the approved hash
- [x] T047 **(inv)** `test_send_requires_approval`, `test_hash_mismatch_refused`, `test_destination_change_refused`, `test_mailbox_change_refused`. The destination check recomputes the hash from where the posting points **now** and compares it to the approved one — an earlier version compared the stored hash to itself, which is vacuous and passes everything
- [x] T048 **(inv)** `test_concurrent_send_sends_once` (6 real threads, 6 connections, one barrier), `test_send_reconciles_not_resends`
- [x] T049 **(inv)** `test_send_caps_enforced`, `test_cap_holds_item_approved`, `test_queue_releases_by_deadline`
- [x] T050 **(inv)** `test_no_bulk_approve` — asserted on the MCP surface and now on the send path too: no parameter takes a list, and no function in `jarvis.send` creates an approval
- [x] T051 Approval recording content hash, rendered hash, destination hash, destination snapshot and `mailbox_credential_id`. `jarvis/approval.py`, outside `jarvis.send` (I-07). The caller must pass the content hash it displayed; approval is refused unless `from_loopback` (owner decision 2026-09-24), unless QC passed, unless it is the newest package, and without a connected, non-disconnected mailbox
- [x] T052 `send/send_approved()` per §6 — `BEGIN IMMEDIATE`, nine conditions, reserve then call then reconcile. Verify happens **before** the claim so a cap refusal leaves the approval queued (I-15); the claim is a conditional UPDATE so exactly one concurrent caller wins (I-05); the `application` row is written in state `sending` **before** the provider is called, so a send that succeeds while its follow-up write fails is reconciled on `provider_message_id`, never retried (I-06). The transport is injected, so the gate is tested without a network and this module imports no provider
- [x] T053 Per-channel opt-in, every channel default off. Checked before the destination, because the channel is one of the three things the destination hash binds and an unknown channel would otherwise be reported as "destination changed"
- [x] T054 Portal route — prefilled worksheet plus the posting URL, submitting nothing. A posting with no usable email address gets a `worksheet` document; approval refuses to send it, and `record_manual_submission` records that the user submitted it themselves (`channel = manual`)
- [x] T055 Duplicate-application check on `employer_norm` + `title_norm`, warned before approval. Stored in the package `warnings` and shown on the queue
- [x] T056 `jarvis/gmail/send.py` — send from the user's own mailbox, apply the `Jarvis/Applied/<company>` label, write the Drive `drive.file` record containing what the CV claims, which profile record backs each claim, and how it differs from the master CV. **Over Gmail/Drive REST through `jarvis.net`**, not `googleapiclient`, whose own transport put every Google call outside I-08 (the egress test never ran setup, so it did not notice); `setup.py` was moved off it too, leaving only the one-time consent-flow token exchange inside `google_auth_oauthlib`. Labelling and the Drive record run after reconciliation; their failures go to `application.followup_errors` and are never retried as a send

### Phase 5 — Google, tracking, record keeping (T057–T065)

- [x] T057 `jarvis setup google` — the guided wizard: create a Google Cloud project, enable Gmail and Drive APIs, create an OAuth **Desktop** client, **publish the consent screen to In Production** (a Testing-status project expires refresh tokens every 7 days), download `client_secret.json`, run the local loopback consent flow
- [x] T058 Scopes requested at the minimum: `gmail.readonly` always, `gmail.send` only where auto-send is opted in, `drive.file` for records. `drive.file` is non-sensitive and grants access only to files Jarvis itself creates
- [x] T059 Token storage `chmod 600`, refresh handling, and **expiry surfaced as "reconnect your mailbox" attributed to Google — never as an absence of replies**. `gmail/google.py`: refresh 60s before expiry; `invalid_grant` marks the credential `needs_renewal` and raises `MailboxDisconnected`; `mailbox_status` says "Replies are NOT being read until you do"; a send refreshes **before** the gate reserves, so a disconnection never leaves a phantom `sending` row
- [x] T060 Gmail label taxonomy created on first connect: `Jarvis/Applied`, `Jarvis/Reply`, `Jarvis/Interview`, `Jarvis/Offer`, `Jarvis/Rejected`, `Jarvis/Outreach`
- [x] T061 Polling via `history.list` + `messages.get`, incremental from the stored history id (`mailbox_state`, migration 005). Resync on the first poll or an expired history id reads application threads **plus a Gmail search limited to the domains applied to** — found by a test: a reply that opened a new thread before any history id existed was otherwise missed. Messages outside application threads and domains are never stored. Each poll writes a `run` row; a disconnection is a failed run naming Google
- [x] T062 Reply classification into the five categories, with a labelled fixture set **built as its own task** — synthetic or consented, never real employer correspondence committed to the repository. `tests/fixtures/replies.json`: 34 synthetic messages including traps ("pleased to extend an invitation to interview"). First run 32/34, both misses `unclassified` (the safe direction); two rules widened, now 34/34 — **on the tuning set, so a regression floor, not held-out accuracy**. Asserted separately: nothing is ever read as an offer that is not one. Category-to-class mapping is `maps_to` in the config
- [x] T063 Classification correction retaining the original; unlinked replies retained, not discarded. A reply from an employer domain matching several applications is stored unlinked (`sender_domain_ambiguous`) and linked by hand
- [x] T064 Status history with previous value, new value, timestamp and actor on every transition. `jarvis/tracking.py`: automatic transitions only move forward, so a late acknowledgement cannot undo an interview; a human may set any status
- [x] T065 Auto-reply: draft a response to a classified reply, route it through the same approval, hash, destination, mailbox and cap rules as an application. **An auto-reply is a send, and no send skips the gate**. A reply draft is a package of kind `reply` (migration 005), and the gate re-derives its destination from the reply's sender. Drafts are template-only and commit to nothing the profile lacks; **an offer is never drafted**

### Phase 6 — Web application (T066–T074)

- [x] T066 FastAPI app, `0.0.0.0:8765`, SPA served from `jarvis/web/static`, JSON API mirroring §8's read tools. `jarvis/web/app.py`: every route is a one-line call into `service.py`; `/api/*` misses are 404, anything else falls back to `index.html` (503 until T067 builds one). No login, per §2 — but two guards against *other websites*: Host must be `localhost` or an IP literal (DNS rebinding → 421), and every write needs `X-Jarvis: 1` (a cross-site form cannot set it without a preflight nobody answers → 403). `jarvis serve` reads `web_port`/`web_bind` from settings. The scheduler is still T074
- [x] T067 Vite SPA scaffold; reuse the existing `dashboard/` components where they fit. A second Vite entry in `dashboard/jarvis/` (`vite.jarvis.config.js`) sharing the legacy dashboard's `node_modules` and `styles.css`; `npm run build:jarvis --prefix dashboard` writes `src/jarvis/web/static`, which is committed because the wheel ships it and pip users have no node. `base: "/"` (nested routes break relative assets — tested). History-API router (no router dependency), `api.js` adds `X-Jarvis: 1` on writes and only renders http(s) posting URLs as links. `/opportunities` is live as the wiring proof: rejected and ineligible rows collapsed behind a counted toggle, never dropped. Every other screen names the task that builds it. The legacy components did not fit the new payload shape (tier/company/score → match_score/employer/sufficiency); the stylesheet did
- [x] T068 PWA manifest, service worker, icons; installable over LAN. `dashboard/jarvis/public/`: manifest, 192/512 + maskable + apple-touch PNGs (drawn once from the legacy SVG; committed, so Pillow is not a dependency), `sw.js`. Assets cache-first, pages network-first with the shell offline, **`GET /api` network-first with the offline copy labelled `X-Jarvis-Cached-At`** — the app shows "Offline — this is a saved copy from …", because a stale list shown as current misrepresents what is open. **Non-GET is never cached or replayed**; an approval happens against the live server or not at all. Verified in a browser: worker active, offline copy labelled, offline write fails. `.js`/`.webmanifest` MIME types are pinned — Windows' registry can map `.js` to `text/plain` and the worker then never registers. **Constraint the plan did not account for:** browsers register a service worker only on https or `localhost`. Over plain-http LAN a phone gets a home-screen shortcut, not an offline installable app; `jarvis serve --certfile/--keyfile` serves https for the full install (the user supplies the certificate, e.g. `mkcert`)
- [x] T069 `/` progress, `/opportunities`, `/opportunities/:id` with the full arithmetic. Home shows the completeness bar, the Minimum Viable Profile checklist, what to fill in next, counts by stage and the last run's age. The detail screen shows every component with declared and effective weight and contribution, each UNSCORED reason, the arithmetic line, every projection line with its direction, source link, date, fx id and any assumption, eligibility findings with posting and profile evidence, rejections (lifted ones shown as lifted), and a "Prepare the application" button disabled unless sufficiency says `sufficient`. A `/profile` screen was added (not in §7): confirming is what unlocks everything, so it has to be one click, with typed vs document provenance shown
- [x] T070 `/queue` — one package per screen, claim trace beside it, destination in full, **no bulk control in the DOM**; T050 passes against the UI too. **Approval is accepted only from loopback** (owner decision 2026-09-24): the approve endpoint reads the socket's address, never `X-Forwarded-For` (tested: a header claiming 127.0.0.1 from a LAN client is refused). The queue list is links only — no checkbox, no approve button; each package screen shows every line beside the record it cites (field path, typed/document, NOT CONFIRMED / SINCE CORRECTED flags), QC findings, omissions, warnings and the content hash. `test_the_web_api_has_no_bulk_control` asserts one approve route and no list-typed body anywhere. Verified in a browser: approve from localhost succeeded; the portal flow recorded a manual submission
- [x] T071 `/applications`, `/tracking` with correction, `/outreach`. Applications show status, history (from, to, actor, time, note), Gmail thread and Drive record links and follow-up errors; a person can set a status. Tracking lists replies with the rules that matched, a correction control that keeps the original, manual linking for unlinked replies, and "Draft a response" (never for offers). A disconnected mailbox is a banner, not an empty list
- [x] T072 `/settings` — thresholds, countries, channels, caps, Google, AI key, data directory, backup target. Every setting the server knows appears in a group (unlisted ones fall into "Other", so none is hidden); booleans are toggles; the AI key is write-only and never displayed; countries toggle with their floor resolution shown; Google connection state with the terminal command to change it
- [x] T072a First-run onboarding — `service.onboarding()` returns each step (data directory, profile, Gmail, channel opt-ins, backup key) with **what breaks if it is skipped**, all skippable. Shown on `/` while any are outstanding and on `/health` always
- [x] T073 `/health` — run ages, backup and last restore drill, disk use; every screen stamped with the age of its data. Also sends awaiting reconciliation (with the instruction to check Sent, never to resend), the mailbox state, stale-run and stale-backup alerts. Every screen uses one `Age` component with the exact time on hover. **Two date bugs fixed on the way:** `get_allowance` and `run_health` compared stored `...T...Z` timestamps with SQLite's `datetime('now')`, whose space sorts before `T` — every earlier send that day counted inside the rolling window, and a run from earlier that day never read as stale
- [x] T074 APScheduler inside `serve` for the daily pass, with `run` rows written whether it succeeds or fails. `jarvis/scheduler.py`: the daily pass at `discovery_schedule_hour` and a Gmail poll every `gmail_poll_minutes` (both settings, I-23), each in its own session, `max_instances=1`; a job that crashes before its own run row still writes a failed one. `jarvis/pipeline.py` is the pass itself, and `jarvis run` now runs it: discover (gated on the Minimum Viable Profile) → screen and score (new `matching.store_assessment`) → sufficiency → generate packages for the top `generate_top_n` → poll Gmail → backup. **A failing step is an obstacle, never the end of the run**; nothing is approved or sent

### Phase 7 — MCP (T075–T082)

- [x] T075 **(inv)** `test_mcp_no_unapproved_send`, `test_mcp_no_approve_tool`, `test_mcp_no_bulk_outbound`, `test_mcp_outbound_requires_hash`; confirm FAILS
- [x] T076 **(inv)** `test_mcp_wraps_untrusted_content`
- [x] T077 Tool registry per §8; the two outbound tools take the content hash; T075 passes
- [x] T078 stdio server holding no database handle and no credential, calling the local API; `test_mcp_tools_map_to_api`
- [x] T079 Untrusted-content envelope for posting text and email bodies; T076 passes
- [x] T080 `jarvis gen-skill` emitting `.claude/skills/jarvis/SKILL.md` from the registry
- [x] T081 **(inv)** `test_skill_matches_registry` — CI fails on drift
- [ ] T082 **(confirm)** Verify against both hosts: `claude mcp add jarvis -- jarvis mcp`, and `[mcp_servers.jarvis]` in `~/.codex/config.toml`. Record both transcripts. **Half done, and the other half is the owner's:** `jarvis mcp` was driven over real stdio by the official MCP Python client (SDK 2.x, protocol 2025-11-25) — initialize, 28 tools listed, no `approve*` tool, `get_profile_status` and `list_queue` answered correctly, and the outbound tool refused an unapproved package with condition 1. Transcript: `docs/mcp_transcripts/stdio_official_client.txt`. Registering Jarvis in the owner's own Claude Code and Codex configuration was **not** done by the build, because it changes their setup; run the two commands in §0 and add their transcripts beside it

### Phase 8 — Outreach, durability, ship (T083–T094)

- [x] T083 **(inv)** `test_contact_erasure_honoured`, `test_no_generated_contacts`. Seen failing first (module absent), then passing. Beyond `NOT NULL`: `add_contact` fetches the page through `jarvis.net`, snapshots it and refuses unless the address — and the name, if given — appears **verbatim** on it (the I-22 check, reused), so `s.malik@` guessed from `sara.malik@` is refused
- [x] T083a Contact discovery — `outreach.discover_from_posting` reads addresses the posting itself publishes, with the capitalised name before each (leading "Contact"/"Email" dropped); the apply address and no-reply senders are excluded; candidates only, stored only through the verified `add_contact`. Team/about pages are added through the same verified call with their URL. A role with no published contact stays without one
- [x] T084 Outreach drafting through the same approval, hash, destination, mailbox and cap path as an application. A package of kind `outreach`; the gate re-derives its destination from the contact row and refuses (condition 4) a contact who has since objected, and outreach needs **its own** opt-in (`channel_outreach`) on top of email
- [x] T085 Notice in every outreach message: who holds the details (the sender's own address), the page it came from, and "reply remove" — and that route works: the poller calls `outreach.handle_objection` on replies in outreach threads and erases the contact. QC has a `notice` origin allowed only fixed wording, the page URL and the cited sender address
- [x] T086 `contact_suppression` global and permanent, checked in the send path; a referee named inside an uploaded document never enters the outreach store — `add_contact` refuses an address found in any of the user's profile records. Suppression stores a SHA-256, compared case-insensitively; suppressing also blocks any unsent drafted package
- [x] T087 **(inv)** `test_backup_restores` into an empty `JARVIS_HOME`. Seen failing first (module absent). The restore test moves to a brand-new home, restores, and checks counts, values, settings and document files — with paths rewritten to the new home — and that the Google token and data key did **not** come across
- [x] T088 `jarvis backup` / `jarvis restore` — `jarvis/backup.py`: SQLite online-backup snapshot + documents, snapshots and config, zipped and encrypted in 1 MiB AES-256-GCM chunks bound to index and last-chunk flag (reorder/truncation detected) under a separate `credentials/backup.key` the user copies off the machine; restore refuses a non-empty home and a wrong key, guards zip paths, migrates forward. Rotation 7 daily + 4 weekly (a daily becomes the weekly when none exists in 7 days). `backup_record` written ok or failed; `drill()` restores into a scratch folder and checks integrity and hashes, recording the result; `status()` alerts on none, failed, or older than `backup_stale_after_hours`. **Design note:** the archive key is not `data.key`, because a backup must survive losing the machine and purge must reach every backup — so backups never contain `data.key`
- [x] T089 `jarvis export` — every table as JSON plus source and generated documents, restricted profile values masked and restricted files **not** exported (listed by name in README.txt); `jarvis purge --yes` overwrites and deletes `data.key`, after which every restricted copy — including in backups — raises `KeyMissing`
- [x] T090 Port the interview prep and STAR bank from `src/jobfinder/phase6`. `jarvis/interview.py` + `config/v1/interview.json`: STAR stories from **confirmed** achievements only — situation and task are `needs_your_input` with a question, never invented; the user's answers are stored in `worked_example`; the result is sourced only when the statement carries a number. Questions come from the posting's skill gap and **state no fact about the user** (the legacy prompts hardcoded one person's passport expiry and degree accreditation). The rubric weights moved to versioned config (I-23) and named tools come from the skill vocabulary instead of a hardcoded toolbox. On `/opportunities/:id` (practice with scored answers and their arithmetic) and `/profile` (the STAR bank)
- [x] T091 Run the full ported legacy suite against the new store and **report any failure verbatim** rather than adjusting the test. The legacy suite passes 83/83 — but it imports `jobfinder.*` and tests the legacy engines, not the new store, so that number alone proves little. The phase-2/3/5 behaviours were ported with their own `jarvis` tests. The phase-6 inputs and expectations were copied **verbatim** into `tests/unit/test_legacy_ported.py` and run against the new classifier and interview scorer: **13/13 pass, no failures to report.** Four legacy expectations were deliberately changed by the plan and are listed, not asserted: `gmail.send` is requested when opted in (T058); the Gmail client now sends through the gate (T056); blocked achievements are never imported; security clearance moved from hard filter to eligibility (T037/T038)
- [x] T092 `docs/RUNBOOK.md` — the six real failure modes, each with how it shows up, why, and what to do. Writing it exposed that reconciling a stuck send meant calling Python by hand, so `jarvis reconcile <id> --message-id … | --not-sent` was added (tested: never resends)
- [x] T093 `README.md` rewritten: install, first run, how an application happens end to end, every command, both MCP host configs, where data lives and the backup key, what is deliberately absent and why. `docs/MCP_SETUP.md` updated to the 28-tool surface. `google-api-python-client` and `google-auth-httplib2` removed from the dependencies: nothing imports them any more
- [ ] T094 **(confirm)** Full invariant suite green. Then first real send, verified against the approved hash, destination and mailbox, with the cap decremented. **First half done:** the full suite is green (770 tests; every invariant I-01 to I-29 resolves to a real test in §3, checked name by name on 2026-09-25, and none is skipped), ruff clean. **The real send is the owner's to do**, and deliberately cannot be done by the build: it needs their own Google project (`jarvis setup google`), the email channel switched on, and an approval clicked on their own computer. Steps: import the profile and confirm it; `jarvis run`; open `/queue`, read a package, approve it; switch on `channel_email_autosend`; Send. Then check `/applications` shows it with its Gmail thread and Drive record, Sent holds exactly one message to the approved address, and `/api/allowance` shows one fewer remaining

---

## 10. What is deliberately absent

No portal login, no CAPTCHA solving, no stored portal passwords, no payments, no account creation
on third-party services, no scraping of LinkedIn, Indeed or Glassdoor, no purchased or
pattern-generated email addresses, no bulk approval, no `approve_package` MCP tool, no telemetry,
and no network call to any host the user did not configure. The safety property is the absence of
the code path, and I-09 asserts it.

---

## 11. Open, and honest about it

| # | Unknown | Resolve by | Blocks |
|---|---|---|---|
| U1 | Pakistan's default savings floor and its documented basis | PBS wage data + a cost-of-living source, recorded with its basis | T041 for PK only |
| U2 | Per-country source availability and terms for PK, US, AU, DE, GB | One research pass per country before T031 | Country packs |
| U3 | Official wage-statistics endpoints (BLS, Destatis, ONS, ABS, PBS) | Same pass | T035's estimates |
| U4 | Whether any employer publishes a real application-submission API | Survey Greenhouse, Lever, Ashby. **Expect none** — they publish job-board *read* APIs | Whether a second send channel exists at all |
| U5 | `drive.file` per-user write quota | Google's Drive quota page; measure one write | Nothing yet |

Nothing here can invalidate the architecture. That is the difference this pivot bought.
