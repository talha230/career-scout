# Jarvis

A local-first job and scholarship agent. It finds opportunities, scores them with arithmetic you can
check by hand, writes application documents where every line traces to something you actually did,
sends them from **your own Gmail** once you have approved each one, and tracks the replies — all on
your own machine.

It is also an **MCP server**, so you can drive it conversationally from **Claude Code** or
**OpenAI Codex**.

**The governing rule: no fabricated value ever reaches a document, the database or a generated
file.** Every opportunity carries its source URL and fetch time. Every score is a weighted sum
stored with its inputs. Every sentence in a generated CV cites the profile record it came from, and
a QC gate blocks any line whose words are not in its own sources. Where something cannot be known,
it says `UNSCORED` with the reason instead of filling the gap.

---

## Why it is free, and stays free

Nothing is shared. There is no server, no hosted database and no account on anyone else's
infrastructure. A hundred users is a hundred independent installs that never meet.

You create **your own** Google Cloud project for Gmail. Google caps an unverified OAuth app at 100
users — per project — so your project has exactly one user: you. No cap, no verification, no paid
security assessment, and your credentials never pass through anyone else's client.

---

## Install as a Claude Code plugin

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and Claude Code.
In Claude Code:

```
/plugin marketplace add talha230/jarvis-plugin
/plugin install jarvis@jarvis-plugin
```

Then ask Claude to **"set up Jarvis"** (or run `/jarvis:setup`). It installs the `jarvis`
command, creates your local data folder, imports your CV, and walks you through
connecting your own Gmail. Everything stays on your computer; the plugin talks to
Jarvis through a local MCP server.

### Approval

Nothing is sent until you approve it in the web app, on this computer. Claude cannot
approve. If you want less clicking, write your own **auto-approval rules** in
Settings → Auto-approval (minimum score, allowed countries, daily limit). They are
off by default, apply to applications only, skip anything with warnings, and only you
can change them — the MCP tools refuse.

## Install without Claude

```bash
uv tool install git+https://github.com/talha230/jarvis-plugin@v0.1.1
```

For development, from a clone:

```bash
pip install -e ".[dev]"
```

## First run

```bash
jarvis setup            # data directory, country packs, profile import
jarvis setup google     # your own Google client — once, about eight minutes
jarvis serve            # the web app at http://localhost:8765, plus the daily run
```

`jarvis setup google` tells you exactly which buttons to press, including the one people get wrong:
**publish your consent screen to "In Production"**. Left in "Testing", Google expires your login
every seven days and reply tracking stops — which looks exactly like a quiet job market. Jarvis
reports an expired or revoked connection as "reconnect your mailbox", never as zero replies.

Then open `http://localhost:8765`. The home screen lists anything still to set up and what breaks
if you skip it.

## How an application happens

1. **Discovery** runs daily (or `jarvis run`) against the job sources your country packs name —
   public APIs only, through one audited network module that honours `robots.txt` and each source's
   terms.
2. **Screening and scoring.** Hard filters reject non-vacancies with a stored, reversible reason;
   eligibility soft-hides what you cannot take, with the posting sentence and your record side by
   side; each score keeps its formula, weights and inputs.
3. **Sufficiency.** An application is written only if your *confirmed* profile supports one for
   *this* posting. A posting whose requirements could not be read is `not_evaluated`, which never
   authorises a document.
4. **Generation.** A CV and letter from confirmed records only, rendered once to DOCX and hashed.
   Unconfirmed values are left out and listed.
5. **Your approval** — in the web app, one package at a time, **on the computer running Jarvis**
   (not from a phone on the Wi-Fi). You see every line beside the record behind it, and the full
   destination address.
6. **Sending** — only if the email channel is switched on in Settings, within your daily caps, from
   your own Gmail. The thread is labelled `Jarvis/Applied/<company>` and a record of what each line
   claims is written to your Drive.
7. **Tracking.** Replies in those threads are classified by published rules, correctable, and move
   the application's status with a full history.

Postings without an email address get a **portal worksheet** instead: prefilled answers and the
link. Jarvis never logs into or submits anything on an employer's site.

## Commands

| Command | What it does |
|---|---|
| `jarvis setup` / `setup google` / `setup profile` / `setup countries` | Guided first run |
| `jarvis doctor` | What is configured, missing or stale. Exits non-zero if something is wrong |
| `jarvis where` | Where your data lives |
| `jarvis serve` | Web app on port 8765, with the daily pass and Gmail polling. `--certfile/--keyfile` for https (a phone installs the app as a PWA only over https) |
| `jarvis run` | One full pipeline pass. Sends nothing |
| `jarvis discover` | Discovery on its own |
| `jarvis backup` | Encrypted backup, then a restore drill to prove it restores |
| `jarvis restore <file> --key <backup.key>` | Restore into an empty data directory |
| `jarvis export <folder>` | Everything in open formats (JSON and your documents) |
| `jarvis purge --yes` | Destroy the restricted-document key: every copy, in every backup, becomes unreadable |
| `jarvis mcp` | The MCP server — Claude Code and Codex launch this |
| `jarvis gen-skill` | Regenerate the Claude Skill from the live tool list |

## Driving it from Claude Code or Codex

**Claude Code**

```bash
claude mcp add jarvis -- jarvis mcp
```

**OpenAI Codex** — in `~/.codex/config.toml`:

```toml
[mcp_servers.jarvis]
command = "jarvis"
args = ["mcp"]
```

28 tools, one implementation: every tool calls the same service layer the web app does, so the MCP
server can reach nothing the web app cannot. The model reads your profile, opportunities, queue,
applications and replies; confirms or corrects fields; proposes profile values from a document (kept
only if the quoted passage is really in it, and stored unconfirmed); generates packages; and may
send **what you already approved**, naming its content hash. It cannot approve anything. Job
descriptions and emails reach it wrapped as untrusted content. See [docs/MCP_SETUP.md](docs/MCP_SETUP.md).

## Where your data lives

```
~/.jarvis/                 (or wherever JARVIS_HOME points)
├── jarvis.db              SQLite — the system of record
├── documents/
│   ├── source/            your uploaded CVs and letters
│   ├── restricted/        passport, ID, tax, bank — encrypted (AES-256-GCM), never read
│   └── generated/         what Jarvis produced, rendered once and hashed
├── snapshots/             every fetched page, the evidence behind every sourced figure
├── credentials/           Google token, data.key, backup.key — locked to your account
└── backups/
```

Nothing leaves this directory except requests to the job sources you configured and the Gmail and
Drive calls acting on your own account — all through one module, asserted by a test that patches the
socket layer.

**Copy `credentials/backup.key` somewhere off this computer once.** Backups are useless without it,
which is what keeps a lost drive safe. Backups never contain your Google token or `data.key`.

## What it deliberately cannot do

No portal logins. No CAPTCHA solving. No stored portal passwords. No payments. No creating accounts
on your behalf. No scraping LinkedIn, Indeed or Glassdoor. No guessed, generated or purchased email
addresses — a contact is stored only if its address appears verbatim on a page the employer
published. No bulk approve. No MCP tool that approves. No approving from another device. No
telemetry. No auto-reply to an offer.

The safety property is the absence of the code path, and tests assert those absences rather than
trusting this paragraph.

When something breaks, [docs/RUNBOOK.md](docs/RUNBOOK.md) says what it looks like and what to do.

---

## Development

```bash
python -m pytest src/jarvis/tests -q && python -m ruff check src/jarvis
npm run build:jarvis --prefix dashboard      # rebuilds src/jarvis/web/static (committed)
npm run dev:jarvis --prefix dashboard        # UI dev server on :5174, proxies /api to jarvis serve
```

The plan and its 29 never-skipped invariants: [PLAN.md](PLAN.md).

The original single-user pipeline is still in `src/jobfinder/` (83 passing tests) with its measured
dataset in `data/jobfinder.db` — 4,402 postings. The dataset is the fixture corpus the parsing and
scoring work is tested against; its behaviours have been ported and re-tested in `src/jarvis/`.
