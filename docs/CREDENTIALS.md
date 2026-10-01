# Credentials

Nothing in this project ships with a credential, and nothing stubs one. Where a
credential is missing, the code stops and says so.

## Currently required from you

### 1. Google OAuth client — Phase 6 (Gmail)

**Status: NOT SUPPLIED. Phase 6 cannot connect until you do this.**

This must be done by hand in Google Cloud Console; it cannot be scripted or
guessed:

1. https://console.cloud.google.com/ → create or select a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen**
   - User type: **External**
   - Add your own address as a **Test user**
   - Scopes: `gmail.readonly` and `gmail.compose`
   - **Do not add `gmail.send`.** The code never asks for it, so the token it
     holds is physically unable to send mail. That is the enforcement behind the
     rule that nothing reaches an employer without you pressing send.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Application type: **Desktop app**
5. Download the JSON, save it as `secrets/google_client_secret.json`.

Then:

```bash
python -m jobfinder.phase6.connect_gmail --auth
```

A browser opens once. After you approve, a token is written to
`secrets/gmail_token.json`. Both files are in `.gitignore`.

Check it worked:

```bash
python -m jobfinder.phase6.connect_gmail --test
```

## Not required, but would improve results

### USAJOBS API key
`config/sources.json` registers USAJOBS as `enabled: false`. Get a free key at
https://developer.usajobs.gov/APIRequest, then set `JOBFINDER_USAJOBS_KEY` and
flip `enabled` to true. Low value for you — US federal roles are almost all
restricted to US citizens.

### PostgreSQL
The build spec calls for PostgreSQL; SQLite is the committed default because no
Postgres server is installed on this machine. The SQLAlchemy models are
database-agnostic, so switching is a connection string:

```bash
set JOBFINDER_DATABASE_URL=postgresql+psycopg://user:password@localhost/jobfinder
python -m jobfinder.phase2.ingest
```

See `docs/DECISIONS.md` ADR-001.

## Deliberately absent

No API key exists, or should exist, for LinkedIn, Indeed or Glassdoor. Their
terms prohibit automated collection. They are listed under `manual_review_only`
in `config/sources.json` and are searched by hand in a browser.
