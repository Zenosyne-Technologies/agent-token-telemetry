---
doc: Enabling Remote Telemetry
type: handbook
status: active
summary: How to turn on the shared remote (Supabase) storage backend with `/token-telemetry:enable-remote` — choosing local vs remote, setting the project URL and the publishable-key env-var NAME (the key value is never entered in chat), applying the schema and running the RLS live-verification, logging in, ensuring your remote identity, then migrating your existing data or starting fresh, how to switch back, and the dashboard's own-price warning banner with its "backfill available" line.
keywords: [enable, remote, supabase, backend, publishable-key, env-var, login, rls, migrate, start-fresh, reversible, telemetry, dashboard, price-warning-banner, estimated-rate, backfill-available]
level: project
audience: user
module: storage
sources: [commands/enable-remote.md, commands/enable.md, scripts/remote_migrate.py, scripts/settings.py, scripts/supabase_backend.py, scripts/dashboard.py, scripts/dashboard.html, scripts/backfill_summary.py]
related: ["[[operating-remote-telemetry]]", "[[migrating-to-remote]]", "[[migrating-local-logs-to-central]]", "[[reading-token-stats]]", "[[enabling-telemetry]]"]
created: 2026-09-22
updated: 2026-09-26
---

# Enabling Remote Telemetry

By default your telemetry lives in a **local** SQLite database on your laptop.
Run `/token-telemetry:enable-remote` to instead send it to a **shared remote
database** — a Supabase project you or a teammate runs — so your usage lives
alongside the team's. **Supabase is the only remote option today**; more backends
may follow.

This is a machine-level storage choice, separate from turning capture on for a
project ([[enabling-telemetry]], `/token-telemetry:enable`). Switching backends
does not change your per-project setup, and it is always reversible.

## Remote telemetry, at a glance

Going remote is three steps, each documented on its own page:

1. **Enable** (this page) — point at your Supabase project, apply the schema,
   log in, and either migrate your existing data or start fresh.
2. **Migrate** ([[migrating-to-remote]]) — upload your whole central database
   in one verified pass, if you didn't do it during enable.
3. **Read** ([[reading-token-stats]]) — `/token-stats` and `/project-stats`
   show the same numbers whether your data lives locally or remotely; nothing
   about reading your reports changes once you're on remote.

If you're the one running the Supabase project the team writes to, **you are
the operator** — [[operating-remote-telemetry]] is the page for you: applying
and re-applying the schema, proving Row-Level Security actually isolates users,
the security model in plain terms, and the operational caveats (Postgres
version, timezone, key rotation) that only matter once the remote is live.

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

### 2. Apply the schema and verify RLS (operator step)

Before anything can be written, the remote tables and their Row-Level Security
must exist. Whoever runs the Supabase project applies the schema and proves
Row-Level Security actually isolates users — see [[operating-remote-telemetry]]
for exactly how, including the security model and what data does and doesn't
leave your machine once it's live.

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

## The dashboard, once you're on remote

`/token-telemetry:dashboard` always shows **this machine's local telemetry** —
it reads the local `usage.db` the same way whichever backend is active. Once
you switch to remote, the dashboard's header adds a small note saying so and
pointing you at `/token-telemetry:token-stats` for the central (all-machines)
view. A full remote dashboard view is planned but not built yet.

## When a logged model has no price of its own

Whether you're local or remote, the dashboard shows a banner near the top of
the page whenever it has logged usage (at any time, not just the period
you're currently viewing) for a model that has no published price of its
own. It splits what it finds into two groups:

- **Priced at an estimated rate** — Anthropic hasn't published a rate for
  this exact model, so a related rate is used instead (its version family's
  default rate, or the nearest earlier model's rate). Each entry shows how
  many times you used it, the date range, and the estimated cost. If some of
  that model's usage happened before a rate could be found at all, the cost
  says so — e.g. "$2.70 for 1 priced event; 2 unpriced, not counted" — rather
  than quietly leaving those events out of the total.
- **No price at all** — no rate could be found for this model at all; its
  cost is not counted anywhere.

Either way, the banner points you at `/token-telemetry:pricing-update` to
refresh the pricing table. It disappears once every model you've logged has
its own price.

### "Backfill available"

The banner can also carry one more line, even when no model is listed above
it:

> Backfill available: 2 bundles, -$272.46 — run /token-telemetry:pricing-update to review and confirm (plan computed 3 hours ago).

It means the last time `/token-telemetry:pricing-update` checked (see
[[reading-token-stats]], "Backfilling estimated pricing windows"), some past
usage was still priced at an estimate that the model's own, later-published
rate could correct. It tells you how many bundles are on offer, the combined
cost change if you applied all of them, and how old that check is.

- **The dashboard never applies anything.** There is no button for it. Run
  `/token-telemetry:pricing-update`, review the plan, pick the bundles you
  want, and approve Claude Code's permission prompt — only then is anything
  written.
- **It never goes stale silently.** The line is only shown while your pricing
  table and the relevant past usage are exactly as they were when that check
  ran. A pricing refresh, an applied backfill, or imported older usage hides
  it until the next check; new usage you record today does not.
- **It stays hidden when there is nothing reliable to show** — no check has
  run yet, the check found nothing to offer, or its saved result is missing
  or damaged. That is never an error; the rest of the page is unaffected.
- **Not shown on the remote backend.** Backfill only corrects your local
  telemetry database, so while remote is your active backend the dashboard
  hides this line rather than suggest a correction the remote store can't
  receive.

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
