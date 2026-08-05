---
description: One table of per-project token usage, cost and activity dates from the central DB
allowed-tools: Bash(sqlite3:*), Bash(ls:*), Bash(cat:*), Read
---

A single per-project overview. **Read-only — never create, migrate or modify a DB here.**
For where the data physically lives use `/token-telemetry:storage-status`; for
time-windowed and per-model/agent/tier breakdowns use `/token-telemetry:token-stats`.

Central DB: `~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB` when set. `ls -l` it
first — if it does not exist, say no telemetry has been recorded yet (enable with
`/token-telemetry:enable`) and stop.

Run this one query with `sqlite3 -header -column`. Each event prices at the rate in force
at its own `ts` — the `pricing` row with the longest `model_prefix` matching the model
name and the greatest `effective_from <= ts` — exactly as `commands/token-stats.md` does.
All-time, no window.

```sql
WITH priced AS (
  SELECT p.path AS project, s.id AS session_id, e.ts AS ts,
         e.in_tok, e.out_tok, e.cache_r, e.cache_w,
    (SELECT pr.in_usd FROM pricing pr
       WHERE m.name LIKE pr.model_prefix || '%' AND pr.effective_from <= e.ts
       ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC LIMIT 1) AS in_usd,
    (SELECT pr.out_usd FROM pricing pr
       WHERE m.name LIKE pr.model_prefix || '%' AND pr.effective_from <= e.ts
       ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC LIMIT 1) AS out_usd,
    (SELECT pr.cache_r_usd FROM pricing pr
       WHERE m.name LIKE pr.model_prefix || '%' AND pr.effective_from <= e.ts
       ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC LIMIT 1) AS cache_r_usd,
    (SELECT pr.cache_w_usd FROM pricing pr
       WHERE m.name LIKE pr.model_prefix || '%' AND pr.effective_from <= e.ts
       ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC LIMIT 1) AS cache_w_usd,
    (SELECT pr.effective_from FROM pricing pr
       WHERE m.name LIKE pr.model_prefix || '%' AND pr.effective_from <= e.ts
       ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC LIMIT 1) AS rate_from
  FROM projects p
  LEFT JOIN sessions s ON s.project_id = p.id
  LEFT JOIN events   e ON e.session_id = s.id
  LEFT JOIN models   m ON m.id = e.model_id
)
SELECT project,
       COUNT(DISTINCT session_id) AS sessions,
       COUNT(ts) AS events,
       COALESCE(SUM(in_tok), 0) AS input,
       COALESCE(SUM(out_tok), 0) AS output,
       ROUND(COALESCE(SUM(in_tok * COALESCE(in_usd, 0) + out_tok * COALESCE(out_usd, 0)
             + cache_r * COALESCE(cache_r_usd, 0) + cache_w * COALESCE(cache_w_usd, 0)), 0)
             / 1000000.0, 4) AS est_cost_usd,
       CASE WHEN MAX(rate_from) IS NULL THEN 'unpriced'
            WHEN MAX(rate_from) = 0 THEN 'seed rates (undated)'
            ELSE date(MAX(rate_from), 'unixepoch') END AS rates_as_of,
       date(MIN(ts), 'unixepoch') AS first_seen,
       date(MAX(ts), 'unixepoch') AS last_activity
FROM priced GROUP BY project ORDER BY est_cost_usd DESC, output DESC;
```

Render **one** markdown table, rows in the query's order:

```markdown
| project | sessions | events | input | output | est. cost | first seen | last activity |
|---|---|---:|---:|---:|---:|---|---|
| `/Users/me/dev/foo` | 41 | 512 | 1,204,331 | 88,204 | $12.4412 (seed rates) | 2026-06-02 | 2026-08-06 (today) |
```

- Thousands separators on token counts; humanize the two dates with a relative age in
  parentheses (`today`, `3 days ago`, `2 months ago`).
- Carry the `rates_as_of` label into the cost cell rather than adding a column:
  `seed rates (undated)` → `(seed rates)`, a date → `(rates 2026-07-01)`, `unpriced` →
  render the cost as `unpriced`, never `$0` — it means no `pricing` prefix matched that
  project's models, not that the work was free. **`effective_from = 0` is the seed
  marker; never render it as a date (1970-01-01).**
- A project with events but no matching rates, and a project with no events yet (zeros,
  empty dates), are both normal rows — say nothing more about them.

Close with one line only: if any row shows `seed rates`, note that
`/token-telemetry:pricing-update` replaces the seed with dated published rates. No other
commentary, no other tables.
