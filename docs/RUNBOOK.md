# Jarvis runbook

Six things actually go wrong. For each: how it shows itself, what to check, and what to do. Every
one of them is surfaced on the **Health** screen (`/health`) and by `jarvis doctor`; none of them
fails silently.

---

## 1. Google disconnected your mailbox

**Looks like:** a banner "Google disconnected your mailbox — reconnect it"; Tracking says
"Replies are not being read"; `gmail` runs on `/health` are `failed` with the error naming Google.
It is **never** shown as "no new replies".

**Why:** the refresh token was revoked (you removed access in your Google account, changed your
password with "sign out everywhere", or the consent screen was left in *Testing*, which expires
tokens every 7 days). "There is no Google connection on this computer" instead means the token file
itself is missing — for example after restoring a backup, which deliberately never contains it.

**Do:**

```bash
jarvis setup google
```

Check the consent screen is published to **In Production** in your Google Cloud project. Approved
packages stay queued while disconnected; nothing is lost and nothing is sent twice.

## 2. No run in N days

**Looks like:** `/health` "Stale: no successful full run in 48 hours" (the `run_staleness_alert_hours`
setting); the home screen's "Last run" is old.

**Why:** `jarvis serve` was not running at `discovery_schedule_hour`, the computer was asleep, or
every step failed.

**Do:** open `/health` → Runs. A `failed` or `partial` run lists its obstacles with reasons (a
source down, the Minimum Viable Profile incomplete, the mailbox disconnected). Run one pass by hand:

```bash
jarvis run
```

A step that fails is an obstacle; the others still run. If discovery says the Minimum Viable Profile
is not satisfied, confirm the checklist items on the home screen.

## 3. A send that needs reconciling

**Looks like:** `/health` "N send(s) need reconciling"; an application stuck in `sending`.

**Why:** the send was reserved and Gmail was called, but the result was not recorded — the process
died, or the network dropped mid-request. Gmail has no idempotency key, so Jarvis **cannot know
whether the message left**, and it will **never** send it again on its own.

**Do:** look in Gmail → Sent for the message to that address.

- **It is there:** record it (the message id is in Gmail → Show original):

  ```bash
  jarvis reconcile <application id> --message-id <gmail message id>
  ```

- **It is not there:** the message did not leave. Record that, then prepare the package again from
  the opportunity and approve the new one — the old approval is consumed and cannot be reused, by
  design:

  ```bash
  jarvis reconcile <application id> --not-sent
  ```

## 4. A source changed shape

**Looks like:** a `discover` run is `partial`; the obstacle reads "…returned something that is not
JSON … the page is kept at snapshots/…" or a source suddenly returns 0 items.

**Why:** the provider changed its API. Nothing was guessed from the broken response: the raw page
is saved under `snapshots/` before parsing, so you can see what arrived.

**Do:** open the snapshot named in the obstacle. If the source moved its URL or changed its fields,
update `config/v1/sources.json` (or the mapper in `jarvis/discovery/clients.py`) and add the new
payload to `tests/fixtures/source_payloads.json`. The other sources keep working meanwhile.

## 5. A backup that failed or went stale

**Looks like:** `/health` "Backup: the newest backup failed: …" or "…is stale (taken …)"
(`backup_stale_after_hours`), or "Last restore drill: never".

**Why:** the `backup_target` folder is unplugged or full (an external drive not mounted), backups
were switched off, or no daily run happened.

**Do:**

```bash
jarvis backup --target <folder you can write to>
```

This also runs a restore drill and records the result. Confirm `credentials/backup.key` has a copy
somewhere off the machine; without it no backup can be restored. To prove a restore end to end on a
spare folder:

```bash
set JARVIS_HOME=C:\temp\jarvis-restore-test
jarvis restore <path\to\backup.jvb> --key <path\to\backup.key>
```

## 6. A document that would not extract

**Looks like:** Profile shows no proposals after an upload; the document list says "no text layer"
or the extraction note explains the failure.

**Why:** a scanned PDF has no text layer — it is reported as un-extracted, never as an empty
result. Or the file type is unsupported.

**Do:** upload a text-based version (export the PDF from Word, or a DOCX), or install the optional
OCR extra (`uv tool install --force "jarvis-agent[ocr] @ git+https://github.com/talha230/jarvis-plugin@v0.1.1"`) and upload again. You can also type the fields
directly; typed values are marked as typed, never as extracted. Restricted documents (passport, ID,
tax, bank) are never read at all — that is intentional, not a failure.
