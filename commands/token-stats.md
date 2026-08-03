---
description: Show token usage and cost statistics from the telemetry DB
allowed-tools: Bash(sqlite3:*)
---

Report token usage from `~/.claude/telemetry/usage.db`. If the file does not exist, tell the user no telemetry has been recorded yet (enable with `/token-telemetry:enable`) and stop.

Run these queries with `sqlite3 -header -column ~/.claude/telemetry/usage.db "<SQL>"`:

Totals (today and last 7 days):

```sql
SELECT CASE WHEN ts >= strftime('%s','now','start of day') THEN 'today' ELSE 'last 7d' END AS period,
       SUM(in_tok) AS input, SUM(out_tok) AS output,
       SUM(cache_r) AS cache_read, SUM(cache_w) AS cache_write,
       COUNT(*) AS events
FROM events WHERE ts >= strftime('%s','now','-7 days')
GROUP BY period ORDER BY period DESC;
```

By project (last 7 days):

```sql
SELECT p.path, SUM(e.in_tok) AS input, SUM(e.out_tok) AS output,
       SUM(e.cache_r) AS cache_read, COUNT(*) AS events
FROM events e JOIN sessions s ON s.id = e.session_id
JOIN projects p ON p.id = s.project_id
WHERE e.ts >= strftime('%s','now','-7 days')
GROUP BY p.path ORDER BY output DESC;
```

By model (last 7 days):

```sql
SELECT m.name, SUM(e.in_tok) AS input, SUM(e.out_tok) AS output,
       SUM(e.cache_r) AS cache_read, SUM(e.cache_w) AS cache_write
FROM events e JOIN models m ON m.id = e.model_id
WHERE e.ts >= strftime('%s','now','-7 days')
GROUP BY m.name ORDER BY output DESC;
```

Main vs subagent split and cache hit rate (last 7 days):

```sql
SELECT CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END AS kind,
       SUM(in_tok) AS input, SUM(out_tok) AS output,
       ROUND(100.0 * SUM(cache_r) / NULLIF(SUM(in_tok) + SUM(cache_r), 0), 1) AS cache_hit_pct
FROM events WHERE ts >= strftime('%s','now','-7 days') GROUP BY kind;
```

Then estimate cost for the by-model results using this pricing map (USD per million tokens; cache read ≈ 0.1× input, cache write ≈ 1.25× input). These are estimates — flag unknown models as unpriced:

| model prefix | input | output | cache read | cache write |
|---|---|---|---|---|
| claude-fable-5 | 10.00 | 50.00 | 1.00 | 12.50 |
| claude-opus-4 (any) | 5.00 | 25.00 | 0.50 | 6.25 |
| claude-sonnet | 3.00 | 15.00 | 0.30 | 3.75 |
| claude-haiku | 1.00 | 5.00 | 0.10 | 1.25 |

Cost per model = (input × in$ + output × out$ + cache_read × cr$ + cache_write × cw$) / 1,000,000.

Present: a short headline (today's totals + estimated 7-day cost), then the breakdown tables. Keep it compact.
