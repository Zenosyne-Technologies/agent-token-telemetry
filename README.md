# token-telemetry

Claude Code plugin that records per-turn and per-subagent token usage into a
central SQLite database — with **zero model-token overhead** (capture runs in
Stop/SubagentStop hooks, outside the model loop).

v0.2.0 adds pricing-as-data (a versioned, effective-dated `pricing` table instead
of a hardcoded map) and kit-aware columns (`issue_key`, `task_size`, `note`) so
usage can be sliced by tracker issue, task size, and milestone — the seam the
**agent-operating-kit** consumes for cost-aware reporting. Install both from the
same marketplace for the full suite; each degrades gracefully without the other.

## Install

```
claude plugin marketplace add zenosyne-technologies/agent-operating-kit
claude plugin install token-telemetry@emprove
```

Restart Claude Code (hooks load at session start). Standalone (no marketplace):

```
claude plugin marketplace add /path/to/agent-token-telemetry
claude plugin install token-telemetry@agent-token-telemetry
```

## Use

- `/token-telemetry:enable` — opt this project in (creates `.claude/telemetry`
  marker; committable for team-wide opt-in)
- `/token-telemetry:disable` — opt out
- `/token-telemetry:token-stats` — totals, per-project/model/agent/milestone/
  tier/issue breakdown, cache hit rate, cost estimates priced from the
  `pricing` table
- `/token-telemetry:pricing-update` — agent-driven refresh: fetches Anthropic's
  currently published per-model pricing and inserts new effective-dated rows on
  change (history is never mutated — a rate change is always a new row)
- `/token-telemetry:schedule-pricing` — registers `pricing-update` as a weekly
  background scheduled agent where the host supports it, else prints the
  manual-cadence advice

Data lives in `~/.claude/telemetry/usage.db` (SQLite, WAL). Query it directly
with sqlite3/DuckDB/Grafana. Capture never breaks a session — capture errors go
to `~/.claude/telemetry/error.log` only for opted-in projects; failures before
the opt-in check exit silently. Cost is never stored — it is derived at query
time from the `pricing` table (see `docs/TELEMETRY-CONTRACT.md` for the exact
rate-resolution rule and the stability promise on consumed columns).

When a kit-managed project has telemetry enabled, the kit writes
`.claude/telemetry-context.json` at tracker-task start/switch; capture stamps
`issue_key`/`task_size`/`note` from it onto events, enabling per-issue and
per-milestone cost breakdowns and the kit's cost-per-issue closing comment.

## Design

See `docs/superpowers/specs/2026-07-17-token-telemetry-plugin-design.md` and
`docs/TELEMETRY-CONTRACT.md` for the v0.2.0 stability contract.

## Tests

```
python3 -m unittest tests.test_capture -v
```
