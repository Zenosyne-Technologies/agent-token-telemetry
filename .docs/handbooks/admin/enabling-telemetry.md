---
title: Enabling Telemetry
audience: admin
module: capture
sources: [scripts/capture.py, hooks/hooks.json, commands/enable.md, commands/disable.md, commands/storage-status.md, commands/storage-separate.md, commands/storage-delete.md]
updated: 2026-08-06
related: [[reading-token-stats]]
---

# Enabling Telemetry

Token telemetry is off by default for every project. Nothing is recorded, and
no files are written, until a project explicitly opts in.

## Turning it on

Run `/token-telemetry:enable` in the project. This creates a marker file at
`.claude/telemetry` inside the project's git root. From then on, every
completed turn and every subagent run in that project is recorded automatically
in the background. Capturing usage costs no extra model tokens — it happens
outside the conversation entirely.

Enabling asks one question: **where the data should be kept.**

- **Central only** (the default) — everything goes to the one database on this
  machine, and nothing is written inside the project.
- **Project folder** — the same central database is still written, *and* a copy
  of this project's rows is kept alongside the project at
  `.claude/telemetry-usage.db`, so the history travels with the repo or a team
  share.

The project option is an **extra copy, not a redirect**: choosing it never stops
the central recording. The copy is ignored by git by default (the enable command
adds it to `.gitignore`); committing it instead is a legitimate team choice if
you want shared history in the repo. Re-running enable and picking central again
switches the mode back and stops the extra copies — the file already written is
left alone for you to keep or delete.

The marker file records the chosen mode on its first line and can be committed
to the repo, so the whole team gets telemetry enabled the same way when they
pull.

## Turning it off

Run `/token-telemetry:disable`. This removes the marker file. Any data already
recorded stays exactly where it is — disabling only stops new recording — and a
project-folder copy is left in place too.

## Where the data lives

All projects share one database: `~/.claude/telemetry/usage.db` (SQLite). It
is not per-project — it's a single file on the machine that records usage
across every opted-in project, tagged by project path so it can be broken out
later. Anyone with file access can query it directly with `sqlite3` or a tool
like DuckDB or Grafana.

## Managing what has been collected

Four commands cover the housekeeping. The first two only look; the last two
change things and both ask before they do.

- **`/token-telemetry:storage-status`** — where everything actually sits: the
  central database's size and how many events it holds, then a line per project
  with its event count, whether a project-folder copy is configured, how big
  that copy is, and when it was last written. A copy that belongs to a checkout
  on another machine or an unplugged drive is reported as *not accessible on
  this machine* — that is normal, not an error. If a project shows recent
  activity but its copy is missing or stale, the copies are failing; the reason
  will be in the error log below.
- **`/token-telemetry:project-stats`** — one table of per-project totals:
  sessions, events, tokens in and out, an estimated cost, and when the project
  was first and last seen. Useful for deciding which project's history is worth
  keeping or worth clearing out.
- **`/token-telemetry:storage-separate`** — moves one project's history out of
  the shared database into its own file next to it (named after the project and
  today's date). It counts the exported rows against the central ones and shows
  you both before it offers to remove anything, refuses to overwrite an existing
  file, and only then asks whether to delete that project from the central
  database. Answering no is a perfectly good outcome: you get the standalone
  file and keep everything as it was. Good for archiving a finished project or
  handing its usage history to someone else.
- **`/token-telemetry:storage-delete`** — removes one project's history from the
  central database for good. It offers the export route first, shows exactly
  what will go, and requires you to type the project's folder name back before
  it deletes anything. There is no undo.

Both removal commands touch only the project you chose, and neither deletes a
project-folder copy — that file is yours to keep or remove. Deleted space isn't
returned to the operating system until you run `VACUUM` on the database, which
the commands will tell you about but never do on their own; it rewrites the
whole file and locks it while it runs, so pick your moment. Every export and
every deletion is recorded permanently inside the database, and
`/token-telemetry:storage-status` shows the most recent of those entries.

Deleting a project's data does **not** turn telemetry off — if the project is
still opted in, recording starts again on the next turn. Use
`/token-telemetry:disable` for that.

## Error log

If something goes wrong while recording (a locked database, a bad file), a
note is appended to `~/.claude/telemetry/error.log` — but only for projects
that have opted in. Nothing is ever logged for a project that hasn't turned
telemetry on, and a capture problem never interrupts or breaks the session
itself; at worst that one event is missed.

## Team rollout

Because opt-in is a single marker file, it's safe to commit and roll out to a
whole team through a normal PR — and because the file's first line carries the
storage choice, everyone who pulls it gets the same one. There's nothing else to
configure — pricing and reporting are handled separately by
`/token-telemetry:token-stats` (see [[reading-token-stats]]).
