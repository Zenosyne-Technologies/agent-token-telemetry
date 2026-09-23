---
description: Register weekly automatic pricing refresh, where the host supports it
---

Register `/token-telemetry:pricing-update` to run weekly as a background scheduled
agent.

1. Check whether this host supports scheduled/cron background agents (e.g. Claude Code
   routines). If it does not, skip to step 3.
2. If supported, register a weekly scheduled task that runs
   `/token-telemetry:pricing-update --unattended` (e.g. every Monday). The flag marks
   the run unattended: it refreshes rates but NEVER applies a pricing backfill — when
   one is available it only prints "backfill available" with the plan, for the user to
   confirm in their next interactive `/token-telemetry:pricing-update`. The flag also
   means the refresh's own interactive-only fallback never runs (AOS-143 correction,
   round 2, F1b): the plain refresh ends in one of exit 1 (REFUSED — a parser bound was
   violated), exit 2 (fetch failed — network down, timeout, or the fetch exceeded its
   own timeout/size bounds), or exit 3 (other error — the page didn't parse, or an
   unexpected error such as a DB error). An unattended run treats all three the same
   way: report and stop. It never takes the interactive fallback that exit 2 would
   otherwise unlock — which re-fetches the page and re-runs the script rather than
   writing anything by hand — since no one is present to review that second fetch.
   Confirm the registration to the user and stop — do not also print the
   manual-cadence advice below.
3. If not supported, print this manual cadence advice instead: pricing changes rarely
   and cost estimates degrade gracefully when stale (every estimate carries its rates'
   `effective_from` date, so staleness is visible, not silent) — run
   `/token-telemetry:pricing-update` by hand roughly weekly, or whenever a model's
   estimated cost looks obviously wrong.
