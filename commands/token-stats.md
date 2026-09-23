---
description: Show token usage and cost statistics from the telemetry DB
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py":*)
---

Run this single command and output its stdout **verbatim** — it is already
finished markdown (headline, then windowed and per-project/model/agent/tier/
milestone/issue breakdowns). Add no commentary and no reformatting; explain
only if the user asks a follow-up question. If the script fails, show its
stderr.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" token-stats
```

The report is deterministic and read-only (the DB opens `mode=ro`); every event
prices at the rate in force at its own timestamp per
`docs/TELEMETRY-CONTRACT.md`. For a single specific issue including pre-v2
rows, the kit's per-issue recipe (issue_key union commit-sha git-log fallback)
in that contract still applies.

Cost figures carry their qualifiers inline: `N of M events at an estimated
rate` — or `the whole figure is an estimate` when every event is — for events
priced at a family default or an ancestor row (the contract's "Own price vs
estimate"). `N of M events unpriced` (no pricing row resolved) appears only
alongside that estimated marker on the same figure; it says nothing extra when
the figure has no estimated events at all. The report's last line, `No own
published price for …`, names the models with at least one non-zero-token
event and no own pricing row, and points to `/token-telemetry:pricing-update`;
it is absent when every such model has one (a model whose events are all
zero-token, e.g. a synthetic bookkeeping model, is never named — there is
nothing of theirs to price). With `--scope KEY1,KEY2` the output is the Scoped rollup alone — it
replaces the normal report, so there is no `No own published price` line —
and its cost line carries the same qualifiers.
