---
doc: Identity Model
type: handbook
status: active
summary: How a person is attached to their telemetry — the central `settings.json` (uuid + full name, mode 0600), the `users` table and `sessions.owner_id`, where the name is captured (the interactive enable command, never capture), and how capture stamps `owner_id` on new sessions without ever prompting.
keywords: [identity, users, owner_id, settings.json, uuid, full-name, pii, register-user]
level: project
audience: developer
module: identity
sources: [scripts/settings.py, scripts/manage.py, scripts/capture.py, commands/enable.md, docs/TELEMETRY-CONTRACT.md]
related: ["[[capture-pipeline]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Identity Model

Telemetry can attribute a session to a person. The authoritative schema for the
identity tables is `docs/TELEMETRY-CONTRACT.md` (schema v7); this page explains
the moving parts and, above all, *which side of the interactive/hook boundary
each one lives on*.

## The boundary that shapes everything

`scripts/capture.py` runs as the `Stop`/`SubagentStop` hook — non-interactive,
must never block or break a session (see [[capture-pipeline]]). So **capture may
never prompt for anything**, including a name. Identity is therefore split in two:

- **Writing identity is interactive** — the full name is asked for, the uuid is
  minted, `settings.json` is written and the `users` row is inserted, all inside
  the `/token-telemetry:enable` command flow.
- **Reading identity is passive** — capture only *reads* the central settings to
  learn the current uuid, and stamps it onto new sessions. If there is no
  identity, or the settings file is malformed, capture proceeds exactly as it did
  before identity existed (`owner_id` stays NULL).

## Central settings — `~/.claude/telemetry/settings.json`

`scripts/settings.py` owns this file. It lives beside the usage DB (the parent of
`$TOKEN_TELEMETRY_DB`, else `~/.claude/telemetry/`) — **outside any repo**, so a
name can never be committed. Shape in this phase:

```json
{"user": {"uuid": "<uuid4>", "full_name": "<name>"}, "active_backend": "local"}
```

- **Mode 0600.** The full name is PII, so `write_settings()` opens the temp file
  `0600` from creation and force-`chmod`s the final file `0600` (tightening an
  older loose file). It writes to a temp file then `os.replace()`s it in, so a
  concurrent reader sees the whole old or whole new file, never a half-written one.
- **uuid stability.** `ensure_identity()` mints a UUIDv4 **once**, on first name
  capture, and reuses it on every later call — re-running enable never re-mints.
  Only the name is updated in place if it changed. This is a *local-first* uuid;
  reconciling it with a remote auth identity is a later phase.
- **Total reads.** `read_settings()` / `current_owner_id()` return "no identity"
  (`{}` / `None`) for an absent, partial, or corrupt file and never raise — that
  is what lets capture read them on the hook path safely. A `user` block missing
  a non-empty string `uuid` reads as no identity, not a half-identity.
- `active_backend` defaults to `"local"`; a later phase adds other backends and
  the pointer flip. `ensure_identity()` preserves an already-set value.

## Capturing the name — `manage.py register-user`

The interactive half is one `manage.py` subcommand, so the enable command holds
no raw SQL (the same discipline as `register-name`; see the `manage.py` header).
`register-user --name "<full name>"`:

1. calls `settings.ensure_identity()` — mint-if-absent uuid + `settings.json`;
2. upserts `users(uuid, name, created_at)` keyed on uuid — a new uuid inserts
   with `created_at = now`; an existing uuid only updates `name`, never
   `created_at`.

The name is bound as a SQL **argument**, never interpolated, and is never echoed
to stdout — PII stays out of command output, logs and the tracker. `enable.md`
skips the prompt entirely when a `full_name` is already on file.

## Stamping `owner_id` — capture's only identity write

`sessions.owner_id` is a nullable FK to `users.uuid` (NULL = pre-identity, never
backfilled here — retro-linking existing sessions is a later phase). Capture
reads the uuid once via `settings.current_owner_id()`, hoisted **above the write
lock** with the rest of the enrichment (git/sidecar), then threads it into
`insert_events()`. It is stamped **only when a session row is created** — an
existing session keeps whatever `owner_id` it had, so identity-less rows are
never rewritten.

Two safety gates make identity-less and degraded captures byte-for-byte
unchanged:

- `owner_id` is `None` → the original two-column `sessions` INSERT runs, so a
  user who never set a name is completely unaffected.
- Even with a uuid set, the three-column INSERT runs only when the `owner_id`
  column actually exists (checked per new session); a DB whose v7 hop has not
  landed falls back to the two-column INSERT rather than failing capture.

**foreign_keys is OFF** on capture's connections (no `PRAGMA foreign_keys`), so
stamping a uuid never requires the `users` row to pre-exist and can never raise
an FK error on the hook path. In the normal flow the central `users` row is
inserted by `register-user` before any capture stamps it anyway. Any settings- or
identity-related failure is swallowed and the session is never affected, the same
rule the rest of capture follows.
