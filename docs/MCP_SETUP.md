# Wiring Career Scout into Claude Code and OpenAI Codex

Both hosts speak the same stdio MCP protocol against the same server. There is
no host-specific code on the Career Scout side.

## Claude Code

The easiest route is the plugin: `/plugin marketplace add talha230/career-scout`, then
`/plugin install career-scout@career-scout`. Or register the server yourself:

```bash
claude mcp add career-scout -- career-scout mcp
```

## OpenAI Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.career-scout]
command = "career-scout"
args = ["mcp"]
```

## Verifying it works

Ask the host: *"what's blocking me from applying to anything?"* It should call
`get_profile_status` and report the Minimum Viable Profile checklist.

## What the host can and cannot do

28 tools, every one a thin call into `career_scout/service.py` — the same functions
the web app calls, so a model can reach nothing the web app cannot.

- **Read:** profile status and records, opportunities (full arithmetic),
  documents and their text, the queue, packages with their claim trace,
  applications with status history, replies, allowance, capabilities, run
  health, settings.
- **Write, not outbound:** confirm/correct/set profile fields, propose a profile
  field from a document (stored only if the quoted passage is verbatim in the
  document, and unconfirmed), settings and countries, generate a package, correct
  a reply's classification, draft a reply response, add a published contact,
  suppress or erase a contact.
- **Outbound, approval-gated — exactly two:** `submit_approved_application` and
  `send_approved_outreach`. Both take the content hash as a required parameter,
  so a model must name what it believes it is sending, and both refuse unless a
  person already approved that exact content in the web app on the computer
  running Career Scout.

There is no `approve_package` tool. Approval happens in the web app, by a
person, on this machine, recorded against a content hash.

Job descriptions, document text and email bodies come back wrapped in
`<untrusted-content>`. They are data written by third parties, never
instructions.
