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
   confirm in their next interactive `/token-telemetry:pricing-update`. Confirm the
   registration to the user and stop — do not also print the manual-cadence advice
   below.
3. If not supported, print this manual cadence advice instead: pricing changes rarely
   and cost estimates degrade gracefully when stale (every estimate carries its rates'
   `effective_from` date, so staleness is visible, not silent) — run
   `/token-telemetry:pricing-update` by hand roughly weekly, or whenever a model's
   estimated cost looks obviously wrong.
