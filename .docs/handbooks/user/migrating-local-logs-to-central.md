---
doc: Migrating Local Logs to Central
type: handbook
status: active
summary: What `/token-telemetry:migrate-to-central` does — importing a project's local telemetry mirror (or a storage-separate export) back into the central DB, the keep-a-copy choice, what switching active collection means, and why re-running is always safe.
keywords: [migrate, import, mirror, central, storage, collection-switch, keep-a-copy, telemetry]
level: project
audience: user
module: storage
sources: [commands/migrate-to-central.md, scripts/manage.py]
related: ["[[reading-token-stats]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Migrating Local Logs to Central

Run `/token-telemetry:migrate-to-central` to pull a project's **local** telemetry
into the central database, so central reporting (`/token-stats`,
`/project-stats`) sees it.

It is the mirror image of `/token-telemetry:storage-separate`: where that command
*carves data out* of central into a standalone file, this one *brings a local
file back in*.

## When you would use it

- You captured usage in **project mode** (a copy lives at
  `<project>/.claude/telemetry-usage.db`), moved to a new machine, and want that
  history in the central DB there.
- Someone shared a project's telemetry file, or a `/storage-separate` export, and
  you want it counted in your central totals.

## What it does, in order

1. **Finds the source.** By default your project's own mirror
   (`.claude/telemetry-usage.db`); you can point it at an export file instead.
2. **Shows what will move** — the source's events and sessions next to the
   central totals, so you see the delta before anything is written.
3. **Checks compatibility.** If the source was written by a *newer* version of
   the plugin than the one you have, it stops rather than quietly dropping the
   columns it doesn't understand — upgrade the plugin first. If you have not set
   your name yet, it asks for it once (this is how imported sessions get
   attributed to you).
4. **Imports** the project's data into central and attributes its previously
   anonymous sessions to you.

## The keep-a-copy choice

After a successful import it asks whether to **keep the local source file**. The
default is **keep**, and this command never deletes it for you. Keeping it costs
nothing but a little disk; it is your safety copy. Remove it yourself later if
you want to.

## What "switching collection" means

Importing copies a **snapshot**. It does **not** change where new usage is
collected. If the project is in project mode, capture keeps writing *both* the
central DB and the local mirror on every turn — so the mirror keeps growing after
the import.

The command offers to switch the project to **central-only**: from then on the
local mirror is backup-only and is no longer written to. This is optional and
**off by default** — it only happens if you explicitly say yes, and it takes
effect on your next Claude Code session. Say no and nothing about collection
changes; the import stands on its own.

## Re-running is safe

The import de-duplicates on the whole row, so running it again imports only rows
that are not already central — a second run with nothing new adds **zero** rows.
If a run is interrupted, the central DB is left exactly as it was (the import is
all-or-nothing), and simply running it again finishes the job.

## What it never touches

- **Read cursors.** The central database alone decides what has already been read
  from a transcript; the import never copies cursor state, so it cannot make
  central re-read or skip anything.
- **Other projects.** Only the one project you name is imported; everything else
  in central is left alone.
