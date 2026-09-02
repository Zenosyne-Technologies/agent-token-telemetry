---
title: Reading Token Stats
audience: user
module: reporting
sources: [commands/token-stats.md, commands/project-stats.md, scripts/report.py]
updated: 2026-09-02
related: [[enabling-telemetry]]
---

# Reading Token Stats

Run `/token-telemetry:token-stats` to see a summary of Claude Code usage for
this machine. If telemetry hasn't been enabled anywhere yet, it will tell you
so instead of showing empty numbers.

## What it shows

- **Today's totals and a 7-day summary** — input tokens, output tokens, cache
  activity, and how many turns/subagent runs were recorded.
- **By project, by agent, by model, by tier** — the same totals broken down
  different ways, so you can see which project or which kind of agent is
  using the most.
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

## "seed rates (undated)"

Cost estimates are calculated using a pricing table that starts out with a
built-in set of default rates and no specific date attached. If a report
labels its rates as "seed rates (undated)", it means those defaults are still
in use — the actual current published pricing hasn't been pulled in yet.
Running `/token-telemetry:pricing-update` refreshes this with dated,
up-to-date rates. Once that's done, reports show the date the rates came into
effect instead. Either way, the cost shown is always an estimate for
budgeting, not an authoritative invoice.

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
