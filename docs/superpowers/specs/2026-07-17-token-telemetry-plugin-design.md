# Token Telemetry Plugin — Design

**Date:** 2026-07-17 · **Status:** approved · **Source:** ~/Downloads/Agentic_Token_Telemetry_Architecture.md (adapted)

## Overview

A Claude Code plugin (`token-telemetry`) that records per-turn and per-subagent token usage into a central SQLite database via hooks — zero model-token overhead — plus a slash command for stats. This repo is the plugin.

## Requirements (decided)

- **Scope:** machine-wide DB at `~/.claude/telemetry/usage.db`; capture is **opt-in per project** via a `.claude/telemetry` marker file.
- **Granularity:** one event per main-agent turn (`Stop` hook) and per subagent completion (`SubagentStop` hook).
- **Reporting:** `/token-telemetry:token-stats` slash command; DB remains directly queryable (sqlite3/DuckDB/Grafana).
- **Token cost:** capture path consumes **no model tokens** (hooks run outside the model loop; transcript JSONL is the source of truth). Reporting costs tokens only when invoked.
- **Storage:** smallest format retaining meta — lookup tables for repeated strings, integer-heavy event rows, cost never stored (derived at query time from a pricing map).

## Rejected alternatives

- **MCP `log_usage()` server** (the source doc's design): tool schema in every session context + a call per task = permanent token cost; relies on the model remembering; needs a server process.
- **Built-in OTel export**: standing collector infra, generic schema, no git/task attribution.

## Plugin layout

```
.claude-plugin/plugin.json     # name, version, hooks wiring
hooks/hooks.json               # Stop + SubagentStop → capture script
scripts/capture.py             # entire capture path; Python stdlib ONLY (sqlite3, json)
commands/token-stats.md        # /token-telemetry:token-stats
commands/enable.md             # creates .claude/telemetry marker in cwd project
commands/disable.md            # removes it
```

## Capture flow (`scripts/capture.py`)

Input: hook JSON on stdin (`session_id`, `cwd`, `transcript_path`, `hook_event_name`).

1. **Opt-in gate first:** stat `<cwd>/.claude/telemetry`, else `<git root>/.claude/telemetry`; absent → exit 0 immediately.
2. **Incremental read:** `cursors` table stores last byte offset per transcript (append-only JSONL). Read only new bytes → constant cost regardless of session length; re-fired hooks re-read nothing (this is the dedup mechanism).
3. **Aggregate delta:** sum `message.usage` fields (input, output, cache-read, cache-creation) of new assistant entries, grouped by model → one row per model in the delta (normally exactly one). `dur_ms` = last − first timestamp in delta. Git branch/commit via `git -C <cwd>` (best-effort, empty on failure).
4. **Single transaction:** insert event row(s) + upsert cursor. DB in WAL mode (parallel sessions/subagents don't block).
5. **Never break a session:** top-level try/except; always exits 0 regardless; failures are logged to `~/.claude/telemetry/error.log` only once opt-in is established (step 1) — earlier failures exit silently.
6. Zero-token deltas (no new assistant usage) insert nothing.

## Schema

```sql
PRAGMA journal_mode=WAL;
CREATE TABLE projects(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL);
CREATE TABLE models  (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE sessions(id INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL,
                      project_id INTEGER NOT NULL REFERENCES projects(id));
CREATE TABLE events(
  ts         INTEGER NOT NULL,            -- unix seconds
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  kind       INTEGER NOT NULL,            -- 0 = main turn, 1 = subagent
  agent      TEXT,                        -- subagent type, NULL for main
  model_id   INTEGER NOT NULL REFERENCES models(id),
  in_tok     INTEGER NOT NULL DEFAULT 0,
  out_tok    INTEGER NOT NULL DEFAULT 0,
  cache_r    INTEGER NOT NULL DEFAULT 0,  -- cache read
  cache_w    INTEGER NOT NULL DEFAULT 0,  -- cache creation
  dur_ms     INTEGER,
  branch     TEXT,
  commit_sha TEXT                         -- short sha
);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_session ON events(session_id);
CREATE TABLE cursors(transcript TEXT PRIMARY KEY, offset INTEGER NOT NULL,
                     session_id INTEGER NOT NULL);
```

Schema created idempotently by `capture.py` on first run (`CREATE TABLE IF NOT EXISTS`). ~40–60 bytes/event row; 1M events well under 100 MB.

## Reporting

`/token-telemetry:token-stats`: command prompt instructs Claude to run prepared `sqlite3` queries and render: today + 7-day totals, breakdown by project / model / agent, cache-hit rate, cost estimate from an inline pricing map (USD per Mtok, maintained in the command file — never stored in the DB).

`/token-telemetry:enable` / `disable`: create/remove `.claude/telemetry` in the project root (committable → team-level opt-in).

## Testing

- Unit: `capture.py` parsing + cursor/dedup logic against fixture transcript JSONL (fresh session, incremental append, re-fire no-op, multi-model delta, malformed lines skipped, opt-out exit).
- E2E: enable in this repo, run a real turn, assert row exists; re-run stats; confirm re-fire adds nothing.

## Out of scope (v1)

CSV export, Grafana dashboards, PostgreSQL backend, per-API-call rows, pruning/retention.
