---
doc: Enabling Remote Telemetry
type: handbook
status: active
summary: How to turn on the shared remote (Supabase) storage backend with `/token-telemetry:enable-remote` — choosing local vs remote, setting the project URL and the publishable-key env-var NAME (the key value is never entered in chat), applying the schema and running the RLS live-verification, logging in, ensuring your remote identity, then migrating your existing data or starting fresh, and how to switch back.
keywords: [enable, remote, supabase, backend, publishable-key, env-var, login, rls, migrate, start-fresh, reversible, telemetry]
level: project
audience: user
module: storage
sources: [commands/enable-remote.md, commands/enable.md, scripts/remote_migrate.py, scripts/settings.py, scripts/supabase_backend.py]
related: ["[[migrating-to-remote]]", "[[migrating-local-logs-to-central]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Enabling Remote Telemetry

By default your telemetry lives in a **local** SQLite database on your laptop.
Run `/token-telemetry:enable-remote` to instead send it to a **shared remote
database** — a Supabase project the maintainer runs — so your usage lives
alongside the team's. **Supabase is the only remote option today**; more backends
may follow.

This is a machine-level storage choice, separate from turning capture on for a
project (`/token-telemetry:enable`). Switching backends does not change your
per-project setup, and it is always reversible.

## Local vs remote

The command's first question is where telemetry should be stored:

- **Local** (the default) — nothing changes; events stay in the local DB. (Where
  that local file lives — central or in the project folder — is the separate
  choice `/token-telemetry:enable` makes.)
- **Remote (Supabase)** — events are written to the shared Supabase project. The
  rest of this page covers that path.

## What you set up, in order

### 1. Point at the remote (URL + key env-var name)

You give two things:

- the Supabase **project URL** (e.g. `https://<ref>.supabase.co`), and
- the **name of the environment variable** that holds the low-privilege
  *publishable* key (default `TOKEN_TELEMETRY_SUPABASE_KEY`).

These are written to your settings (mode `0600`) with **no secret in them** —
only the URL and the env-var *name*. You then `export` the key value in your own
shell, and the plugin reads it from the environment when it needs it:

```
export TOKEN_TELEMETRY_SUPABASE_KEY="<your publishable key>"
```

**The key value is never entered in chat and never stored in settings.** Only its
env-var name is recorded. If a remote is already configured, the command leaves
the existing setting untouched and tells you so.

### 2. Apply the schema and verify RLS (maintainer step)

Before anything can be written, the remote tables and their Row-Level Security
must exist. Whoever runs the Supabase project applies `supabase/schema.sql`, then
`supabase/reports.sql`, in the Supabase SQL editor, and runs the **two-user plus
anonymous live verification** that proves each person can see only their own rows.
See the developer handbooks [[rls-remote-schema]] and [[remote-read-parity]] for
the exact schema and the verification steps — the automated test suite only checks
structure, so the isolation check is done by hand.

The command then confirms from your machine that every expected table exists. If
the schema has not been applied it stops and tells you, rather than writing into a
half-provisioned database.

### 3. Log in

You sign in with your Supabase **email and password**. The password travels only
in the sign-in request over a secure connection — it is **never** typed into a
command line, echoed, logged, or stored. Only the resulting session token is kept
(mode `0600`), and your identity is linked to your remote account at this point.

### 4. Ensure your remote identity

Your usage is attributed to you, so a full name must be on file (the command asks
for one if it is missing). It then makes sure your **remote user record exists**,
so your first captured session has a valid owner to attach to. (If you skip this,
capture creates the record automatically on its first remote write — this just
does it up front.)

### 5. Migrate your existing data, or start fresh

Finally you choose what happens to the telemetry already in your local database:

- **Migrate it now** — hand off to `/token-telemetry:migrate-to-remote`, which
  uploads your whole central database, verifies the row counts, and switches
  collection over. See [[migrating-to-remote]] for exactly what that does.
- **Start fresh** — leave the existing local data where it is and simply switch
  new collection to the remote.

Either way your local `usage.db` is **kept**. It stays your backup, and it keeps
doing two jobs even after the switch: it remembers how far each transcript has
been read (the read cursors, which are never sent remote), and it holds any events
waiting to be re-sent if the remote is briefly unreachable (the offline buffer).

## When the switch takes effect

Switching collection is a **pointer flip**, not a data move. It takes effect on
the **next Claude Code session**, because capture hooks load when Claude Code
starts — so restart to begin writing to the remote.

## Going back to local

The switch is always reversible. To return to local collection at any time, flip
the backend back to local (the command shows you how, and
[[migrating-to-remote]] documents the same reversible flip). Your local database
never stopped being complete for the data it holds, so nothing is lost.

## What it never does

- **Asks for or stores your publishable key or password value.** The key is
  referenced by env-var name only; the password is used solely to sign in.
- **Deletes your local data.** The local database is only ever removed by you.
- **Changes your per-project capture setup.** Enabling remote storage is a
  separate, machine-level choice.
