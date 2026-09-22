---
doc: Migrating to the Remote Database
type: handbook
status: active
summary: What `/token-telemetry:migrate-to-remote` does — uploading the whole central telemetry DB to the maintainer's remote Supabase project, verifying the upload before switching collection to it, the keep-local default, and that the switch is reversible and the command re-runnable.
keywords: [migrate, remote, supabase, upload, collection-switch, keep-local, reversible, login, telemetry]
level: project
audience: user
module: storage
sources: [commands/migrate-to-remote.md, scripts/remote_migrate.py, scripts/supabase_backend.py]
related: ["[[migrating-local-logs-to-central]]", "[[enabling-remote-telemetry]]", "[[operating-remote-telemetry]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Migrating to the Remote Database

Run `/token-telemetry:migrate-to-remote` to upload your **whole central
telemetry database** to the shared remote database (a Supabase project the
maintainer runs) and switch new collection over to it, so your usage lives
alongside the team's instead of only on your laptop.

It is the network cousin of `/token-telemetry:migrate-to-central`: where that
one pulls a project's local file *into* your central DB, this one pushes your
entire central DB *up* to the remote.

## Before you start

Two things must already be true, both set up by whoever runs the remote:

- The remote is **configured** on your machine — the project URL is in your
  settings and the access key is in place.
- The remote **schema has been applied** — whoever operates the Supabase
  project has run it once (see [[operating-remote-telemetry]] for exactly how,
  and how they prove it's actually private before anyone uploads real data).

If either is missing the command stops and tells you, rather than uploading into
a database that is not ready. It never invents credentials or a schema.

## What it does, in order

1. **Logs you in.** It asks for your Supabase email and password, signs you in
   over a secure connection, and stores only the resulting session token (never
   your password). Your identity is linked to your remote account at this point.
2. **Checks the remote is ready.** It confirms the connection works and that
   every expected table exists. If the schema has not been applied it stops here.
3. **Shows what will move** — how many projects, sessions and events you have
   locally.
4. **Uploads everything**, in the right order, attributing every row to you.
5. **Verifies the upload** by comparing row counts on the remote against your
   local counts, table by table. **Only if they all match** does it offer to
   switch.

## The keep-local choice

After a verified upload it asks whether to **keep your local central database**.
The default is **keep**, and the command never deletes it. Keeping it is your
safety net: the remote is new, and the local file is your rollback. The local
file also keeps doing two jobs even after you switch — it remembers how far each
transcript has been read, and it holds any events waiting to be re-sent if the
remote is briefly unreachable.

## What "switching collection" means

Switching is a **pointer flip**, not a data move. After it, new usage is written
to the **remote**; your local `usage.db` becomes a backup for events (while
staying the read-position store and the offline buffer). It is:

- **Optional** — the upload stands on its own; you can decline the switch and
  keep collecting locally.
- **Only offered after the upload is verified** — a failed or mismatched upload
  never flips anything.
- **Reversible** — you can switch back to local collection at any time.
- **Effective next session** — capture reads the setting when Claude Code starts.

## Re-running is safe

Every upload is a merge, keyed on each row's natural identity, so running the
command again uploads only what is not already there — a second run with nothing
new changes nothing. If a run fails partway, your local database is untouched and
still authoritative, the switch is **not** made, and simply running it again
finishes the job.

## What it never does

- **Uploads your read cursors.** The record of how far each transcript has been
  read stays local always, so switching can never make capture re-read or skip
  anything.
- **Deletes your local data.** The central file is only ever removed by you, and
  only if you choose to, long after the remote has proven itself.
- **Puts your password anywhere but the login request.** It is never written to
  disk, a URL, or a log.
