---
name: setup
description: >-
  First-run setup for Career Scout on this computer: install the career-scout command, create
  the local data directory, install country packs, import the user's CV, and walk
  them through connecting their own Gmail and opening the web app. Use when the
  user installs the Career Scout plugin, asks how to start, or the career-scout MCP tools
  report that Career Scout is not set up.
---

# Set up Career Scout

Career Scout runs entirely on the user's machine. Their data lives in one local folder
(`~/.career-scout`, or `CAREER_SCOUT_HOME`). Nothing is uploaded anywhere, and there is no
account to create except the user's own Google Cloud project for Gmail.

Work through the steps in order. Run the commands yourself where it says so;
the Google step must be done by the user in their own terminal because it opens
a browser and asks them to sign in.

## 1. Check `uv`

Run `uv --version`. If it is missing, stop and ask the user to install it from
https://docs.astral.sh/uv/getting-started/installation/ — do not download or run
an installer yourself. The plugin's MCP server also needs `uv`.

## 2. Install the `career-scout` command

```bash
uv tool install --force git+https://github.com/talha230/career-scout@v0.2.0
```

Then confirm with `career-scout --help`. If the command is not found, `uv tool update-shell`
adds uv's tool folder to PATH; the user needs a new terminal afterwards.

## 3. Create the data directory and country packs

```bash
career-scout setup
career-scout setup countries
```

Show the user the country table. Countries are switched on or off later in the
web app's Settings or with the `set_country_enabled` tool.

## 4. Import the CV

Ask the user for the path to their CV (PDF or DOCX). Then run:

```bash
career-scout setup profile --document "<path to CV>"
```

Everything read from a document is a **proposal**, not a fact. Tell the user to
confirm their fields (web app → Profile, or ask you to walk through them with
`list_profile_records` and `confirm_profile_field`). Only confirmed values ever
reach an application.

For a passport, national ID or tax document use `--kind passport` (or `cnic`,
`tax_return`): it is stored encrypted and never read.

## 5. Connect Gmail — the user does this

Ask the user to run this **in their own terminal**:

```bash
career-scout setup google
```

It explains every click. They create their own Google Cloud OAuth client, which
keeps Career Scout free and uncapped. The step people miss: **publish the consent
screen to "In Production"**. Left in "Testing", Google drops the login every
seven days and reply tracking silently stops.

## 6. Start the web app

Ask the user to run, in their own terminal, and leave it running:

```bash
career-scout serve
```

Then open http://localhost:8765. The home screen lists anything still to set up.
`career-scout serve` also runs the daily search and Gmail checks.

## 7. Approval and auto-approval

Explain this plainly:

- Every application waits in **Queue** for the user to read and approve it, on
  this computer. You (Claude) cannot approve anything.
- Sending is off until they switch on `channel_email_autosend` in Settings.
- If they want less clicking, they can write **auto-approval rules** in
  Settings → Auto-approval: a minimum match score, a list of allowed countries,
  and a daily limit. Rules apply to applications only, never to replies or
  outreach, and skip anything with warnings. Only the user can change these, in
  the web app on this computer; the `set_setting` tool refuses them.

## 8. Check

Call the `get_profile_status` tool and report what is still missing before
Career Scout can search.
