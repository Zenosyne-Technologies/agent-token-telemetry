---
title: Enabling Telemetry
audience: admin
module: capture
sources: [scripts/capture.py, hooks/hooks.json]
updated: 2026-08-05
related: [[reading-token-stats]]
---

# Enabling Telemetry

Token telemetry is off by default for every project. Nothing is recorded, and
no files are written, until a project explicitly opts in.

## Turning it on

Run `/token-telemetry:enable` in the project. This creates a marker file at
`.claude/telemetry` inside the project's git root (an empty file — its mere
presence is the signal). From then on, every completed turn and every
subagent run in that project is recorded automatically in the background.
Capturing usage costs no extra model tokens — it happens outside the
conversation entirely.

The marker file can be committed to the repo so the whole team gets telemetry
enabled automatically when they pull.

## Turning it off

Run `/token-telemetry:disable`. This removes the marker file. Any data already
recorded stays exactly where it is — disabling only stops new recording.

## Where the data lives

All projects share one database: `~/.claude/telemetry/usage.db` (SQLite). It
is not per-project — it's a single file on the machine that records usage
across every opted-in project, tagged by project path so it can be broken out
later. Anyone with file access can query it directly with `sqlite3` or a tool
like DuckDB or Grafana.

## Error log

If something goes wrong while recording (a locked database, a bad file), a
note is appended to `~/.claude/telemetry/error.log` — but only for projects
that have opted in. Nothing is ever logged for a project that hasn't turned
telemetry on, and a capture problem never interrupts or breaks the session
itself; at worst that one event is missed.

## Team rollout

Because opt-in is a single marker file, it's safe to commit and roll out to a
whole team through a normal PR. There's nothing else to configure — pricing
and reporting are handled separately by `/token-telemetry:token-stats` (see
[[reading-token-stats]]).
