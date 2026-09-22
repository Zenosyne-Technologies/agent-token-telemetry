---
doc: Operating Remote Telemetry
type: handbook
status: active
summary: Running your own Supabase project as the remote telemetry backend — applying and re-applying the schema, the two-user-plus-anon RLS live-verification, the security model in plain terms (the publishable key vs. the secret key you must never use, per-user Auth + RLS, TLS, the 0600 token file), what data does and doesn't leave your machine, operational caveats (Postgres 15+, the "today" timezone caveat, key rotation), and reversibility.
keywords: [operate, remote, supabase, security, rls, schema, publishable-key, service-role, secret-key, tls, postgres-15, timezone, rotation, reversible, operator]
level: project
audience: user
module: storage
sources: [supabase/schema.sql, supabase/reports.sql, scripts/supabase_backend.py, scripts/settings.py, scripts/remote_migrate.py]
related: ["[[enabling-remote-telemetry]]", "[[migrating-to-remote]]", "[[reading-token-stats]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Operating Remote Telemetry

If you turned on the remote backend, **you are the operator** — there is no
separate admin role for this plugin, because you run your own Supabase project.
This page is the operator's guide: getting the database into a state that is
actually safe to write into, proving that it is, and understanding exactly what
does and does not leave your machine once it is.

It complements [[enabling-remote-telemetry]] (the setup walkthrough) and
[[migrating-to-remote]] (moving your existing data over) — this page is where
the schema, security and ongoing-operation details those two link out to
actually live.

## Applying the schema

Before anything can be written, two SQL files must be applied to your Supabase
project, **in the Supabase SQL editor, in this order**:

1. `supabase/schema.sql` — the tables and their Row-Level Security policies.
2. `supabase/reports.sql` — the views/functions `/token-stats`, `/project-stats`
   and `/info` read when your data lives remotely; it depends on the tables
   `schema.sql` creates.

Both files are written to be **re-run safely** — every statement is guarded, so
re-applying either one does not duplicate objects or lose data.

**Re-apply `reports.sql` whenever the plugin's report logic changes** (watch the
release notes for touches to `supabase/reports.sql` or `scripts/report.py`).
The remote reports are a second, hand-written copy of the same aggregation in
Postgres, so the two can drift out of step with each other if only one side is
updated — the project's own cross-dialect **golden test**
(`tests/test_report_parity.py`) is what catches that drift before a release
ships, but it only protects you once you've applied the matching `reports.sql`.
If `/token-stats` or `/project-stats` ever look wrong specifically for remote
data (and only remote data), re-applying the current `reports.sql` is the first
thing to check.

## Proving it's actually private — the live RLS check

Row-Level Security is the **only** thing standing between your data and anyone
else with access to the same Supabase project — there is no other server-side
gate. The automated test suite can only check that the *policies are shaped
correctly*; it cannot run against a real Postgres instance, so it cannot prove
that isolation actually holds. That proof is a manual, one-time check you run
yourself after applying the schema:

1. Apply the schema (above), if you haven't already.
2. Create two test users in Supabase Auth (A and B).
3. Signed in as each, insert a project/session/event under that user.
4. Signed in as A, confirm you see **only A's rows** — never B's, and never
   B's project path. Repeat as B and confirm the mirror.
5. Signed in as A, try to insert a row owned by B — it must be **rejected**.
6. With no login at all (just the publishable key), confirm every table
   returns **zero rows**.

If any step leaks a row across users, do not turn the remote backend on for
real use until it's fixed. The exact SQL for each step is in the developer
handbook [[rls-remote-schema]] — this is the same check, just the operator's
version of it.

## The security model, in plain terms

- **Two keys exist; only one is safe to use.** The **publishable key** is
  low-privilege — it identifies your project but, on its own, cannot isolate
  users or bypass anything. The **secret / service_role key** bypasses Row-Level
  Security entirely. **Never use it here** — this plugin has no configuration
  path for it, will never ask for it, and if you ever paste one into an env var
  meant for the publishable key, every row in your project becomes readable and
  writable by anyone who has it. Only the publishable key belongs in
  `TOKEN_TELEMETRY_SUPABASE_KEY` (or whichever env-var name you chose).
- **The key value never appears in chat, settings, or the repo.** Only the
  *name* of the env var holding it is written to your settings file; you
  `export` the actual value in your own shell.
- **Per-user identity, not a shared key.** Once you log in (email + password,
  sent only in the request body over TLS, never stored), every write and read
  carries your personal Supabase Auth token. Row-Level Security matches that
  token's identity (`auth.uid()`) against each row's owner, so you only ever
  see and write your own rows — even though everyone shares the same
  publishable key.
- **The connection itself is verified, not just encrypted.** Every request uses
  standard TLS with certificate verification on — there is no mode that skips
  checking who you're actually talking to.
- **The one thing stored on disk is your Auth token**, in a file (`credentials.json`,
  beside your local telemetry database) that only your own account can read
  (mode `0600`). It refreshes itself automatically as it nears expiry.

### What leaves your machine, and what never does

What is uploaded: token counts and derived cost figures, model names, your
project's name (and, yes, its local directory **path** — see the note below),
agent names, and tracker issue keys. All of it is owner-scoped by the Row-Level
Security policies above, so none of it is visible to anyone but you.

What is **never** uploaded, under any circumstances: **transcript content**.
Capture never reads it for anything beyond counting tokens, and no query this
plugin runs anywhere ever exposes it. Your **full name** is the only piece of
personal information this system stores at all (so it can attribute usage to
you) — everything else is usage numbers and identifiers, not content.

The project **path** is worth calling out on its own: it is a local directory
path on your machine, and it does travel to the remote so your sessions can be
grouped by project. It is exactly why the whole schema — not just the
sensitive tables — is scoped so that only you can ever read your own rows; see
[[rls-remote-schema]] for why that choice was made uniform.

## Operational caveats

- **Postgres 15 or newer is required.** The `events` table's uniqueness
  constraint relies on `UNIQUE NULLS NOT DISTINCT`, a Postgres 15 feature, so
  that a re-sent event with blank optional fields (like a missing branch name)
  still merges instead of duplicating. Supabase provisions Postgres 15+ by
  default, so this is normally nothing you need to act on — it only matters if
  you're pointing at an unusually old, self-managed Postgres instance.
- **"Today" can disagree between local and remote** if your Supabase project's
  database timezone doesn't match your machine's local timezone — `/token-stats`
  computes "today" using each store's own idea of the start of the day. If the
  remote and local reports ever seem to be off by one day's worth of events near
  midnight, this is why; set your project's database timezone to match your own
  if it matters to you. The 7-day window is unaffected — it's pure arithmetic.
- **Rotating the publishable key** needs no reconfiguration on your end: your
  settings only ever stored the env var's *name*, not its value, so generating
  a new key in Supabase and re-`export`ing it under the same name is enough.
- **Rotating your login session**: your Auth token refreshes itself
  automatically near expiry. To force a clean break (for example after a
  suspected leak of `credentials.json`), delete that file and log in again —
  and revoke the old session from your Supabase project's Auth settings if you
  want to be certain the old token can't be reused.

## Reversibility

Switching back to local collection is always available and always safe —
see [[enabling-remote-telemetry]] and [[migrating-to-remote]] for the reversible
switch itself. What makes it safe: your **local database never stops being
the durable copy**. Even while the remote backend is active, the local SQLite
file keeps doing three jobs — it is your backup, it is the only place read
cursors (how far each transcript has been read) are ever kept, and it is the
offline outbox that queues events if the remote is briefly unreachable. Nothing
about turning the remote off or on ever depends on the remote database having
been reachable, correct, or even reachable at all.
