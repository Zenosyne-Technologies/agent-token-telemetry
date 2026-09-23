---
title: Reading Token Stats
audience: user
module: reporting
sources: [commands/token-stats.md, commands/project-stats.md, scripts/report.py, commands/pricing-update.md, commands/schedule-pricing.md, scripts/pricing_update.py]
updated: 2026-09-23
related: [[enabling-telemetry]], [[enabling-remote-telemetry]], [[operating-remote-telemetry]]
---

# Reading Token Stats

Run `/token-telemetry:token-stats` to see a summary of Claude Code usage for
this machine. If telemetry hasn't been enabled anywhere yet, it will tell you
so instead of showing empty numbers.

Everything below reads the same whether your telemetry is stored **locally**
or on the **shared remote** — see [[enabling-remote-telemetry]] if you're not
sure which one you're on. The one difference worth knowing: if you're on
remote, "today" is computed against the remote database's own clock, which can
disagree with your machine's local day if their timezones differ (see
[[operating-remote-telemetry]]).

## What it shows

- **Today's totals and a 7-day summary** — input tokens, output tokens, cache
  activity, and how many turns/subagent runs were recorded.
- **By project, by agent, by model, by tier** — the same totals broken down
  different ways, so you can see which project or which kind of agent is
  using the most. Tier reflects the AGENT'S ROLE (the main session vs. a
  named sub-agent persona), not just which model answered, so the **by
  tier** table can split what looks like one model's work across several
  rows. **By model** still shows one row per model — if that model served
  more than one role this week, its tier cell lists all of them (e.g.
  `orchestrator, heavy`) rather than repeating the model. When any of that
  usage came from an escalation persona, a further **by ladder rung**
  breakdown shows how much went to each escalation level; a `no rung
  (fallback)` row covers ladder usage that didn't come through a named
  escalation persona, so the rung rows always add up to the ladder total.
- **Estimated cost** — a dollar estimate per model, worked out from current
  published pricing. This is an estimate for visibility, not a bill.
- **By issue** — usage rolled up under a tracked issue key, when that
  information was available at the time.

## One row per project

`/token-telemetry:project-stats` answers a narrower question: how much has each
project used, in total, ever? One table, one line per project — sessions,
events, tokens in and out, an estimated cost, and the first and last days
anything was recorded — sorted by cost, most expensive first. Unlike the report
above there is no seven-day window; it is the whole history.

If a project's cost cell says *unpriced*, no rate is known for the models that
project used — it does not mean the work was free. If it says something like
"14 of 96 events unpriced", the figure shown is real but understates the total
by those events.

## Scoped rollups (a specific set of tracked issues)

The reporting tool can also be pointed at a specific set of tracked issue keys
and asked for the combined cost of just those — this is how a milestone or
other multi-issue rollup gets its total, by summing the matching issues rather
than by branch name. A rollup like this never shows a bare, unexplained "$0" —
if it comes back empty, the report says why: the requested set of issues
couldn't be resolved at all, telemetry hasn't recorded anything for this
project yet, or none of the requested issues have any recorded usage (which
usually means the scope itself is wrong, not that the work was free).

## Estimated costs

Some models don't have their own published price on file yet — their cost is
worked out from a stand-in rate instead (their model family's default, or the
rate for the nearest earlier version). When that happens, the cost figure
says so right in the cell: "N of M events at an estimated rate", or "the
whole figure is an estimate" when every event in that figure used a stand-in
rate. `/token-stats` also ends with a line naming any model that has no own
price on file, pointing you at `/token-telemetry:pricing-update` to refresh
the pricing table. Running that command fixes the rate going forward; a
future backfill may later correct the estimated cost of past events too.

## "seed rates (undated)"

Cost estimates are calculated using a pricing table that starts out with a
built-in set of default rates and no specific date attached. If a report
labels its rates as "seed rates (undated)", it means those defaults are still
in use — the actual current published pricing hasn't been pulled in yet.
Running `/token-telemetry:pricing-update` refreshes this with dated,
up-to-date rates. Once that's done, reports show the date the rates came into
effect instead. Either way, the cost shown is always an estimate for
budgeting, not an authoritative invoice.

Occasionally that refresh prints a `STALE-PRICE WARNING` instead of a new
rate for a given model — this means the published pricing page no longer
lists a current rate for it (its introductory-rate period has ended and
nothing has replaced it yet). Nothing changes for that model until the page
is updated; its reports keep using the last rate that was recorded.

## Backfilling estimated pricing windows

While refreshing rates, `/token-telemetry:pricing-update` can also notice
that some of a model's past usage was recorded using an estimated rate —
borrowed from a closely related model — before that model's own official
rate became available. When this happens, the command shows you exactly
which past events would change, the cost before and after, and asks which of
the changes you want to apply.

- A **bundle** is the smallest group of models that has to be corrected
  together for a fix to be complete. Sometimes correcting one model's
  estimate only works if a closely related model is corrected in the same
  step, so those are offered as a single option — you accept or skip the
  whole bundle, never part of it.
- You choose which bundles, if any, to apply; nothing is pre-selected. Every
  apply you approve goes through Claude Code's own permission prompt before
  anything is written to your telemetry data — that approval happens in the
  same session, every time.
- If none of what's on offer can be safely applied, the command shows you
  why and asks nothing further — there is nothing to approve in that case.
- This never happens automatically or in the background — see below for
  what the weekly scheduled refresh does instead.
- Between runs, the dashboard's own-price banner shows a one-line
  "Backfill available" reminder with the bundle count and combined cost
  change from the last check, for as long as that check still matches your
  data (local backend only). It only points you back here; it never applies
  anything — see [[enabling-remote-telemetry]], "Backfill available".

## Scheduled pricing refreshes never backfill

If you've registered `/token-telemetry:schedule-pricing` to run weekly in
the background, that scheduled run only refreshes rates going forward — it
never applies a pricing backfill, since nobody is present to approve it. If
your weekly schedule was registered before this safeguard existed, run
`/token-telemetry:schedule-pricing` again to re-register it so it explicitly
passes the flag that guarantees this.

## Names shown are cleaned up for display

Project, model, and issue names in these reports can come from files or
values that weren't written with a report table in mind. Before they're
shown, they're cleaned up — stray characters that would break the table
layout are escaped, hidden or invisible characters are stripped, and very
long values are trimmed — so a malformed or unusual name can't distort the
report or hide misleading text. If a name looks unexpectedly short, plain, or
ends in an ellipsis, this cleanup is why.

## Cache hit rate

Claude Code can reuse previously-processed context instead of reprocessing it
from scratch, which is both faster and cheaper. The cache hit rate shown in
the report is the share of input tokens that came from this reuse rather than
being processed fresh. A higher percentage generally means more efficient,
lower-cost sessions — repeated work on the same context is paying off. A low
or zero percentage isn't necessarily a problem, it just means most of what
was sent wasn't eligible for reuse (e.g. it was new or infrequently-repeated
context).
