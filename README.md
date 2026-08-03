# token-telemetry

Claude Code plugin that records per-turn and per-subagent token usage into a
central SQLite database — with **zero model-token overhead** (capture runs in
Stop/SubagentStop hooks, outside the model loop).

## Install

```
claude plugin marketplace add /path/to/agent-token-telemetry
claude plugin install token-telemetry@agent-token-telemetry
```

Restart Claude Code (hooks load at session start).

## Use

- `/token-telemetry:enable` — opt this project in (creates `.claude/telemetry`
  marker; committable for team-wide opt-in)
- `/token-telemetry:disable` — opt out
- `/token-telemetry:token-stats` — totals, per-project/model/agent breakdown,
  cache hit rate, cost estimates

Data lives in `~/.claude/telemetry/usage.db` (SQLite, WAL). Query it directly
with sqlite3/DuckDB/Grafana. Capture errors go to `~/.claude/telemetry/error.log`
and never break a session — that error log is written regardless of per-project
opt-in, since opt-in gates usage capture, not the plugin's own error log. Cost
is never stored — it is derived at query time from the pricing map in
`commands/token-stats.md`.

## Design

See `docs/superpowers/specs/2026-07-17-token-telemetry-plugin-design.md`.

## Tests

```
python3 -m unittest tests.test_capture -v
```
