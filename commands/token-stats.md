---
description: Show token usage and cost statistics from the telemetry DB
allowed-tools: Bash(sqlite3:*)
---

Report token usage from `~/.claude/telemetry/usage.db`. If the file does not exist, tell the user no telemetry has been recorded yet (enable with `/token-telemetry:enable`) and stop.

Run these queries with `sqlite3 -header -column ~/.claude/telemetry/usage.db "<SQL>"`:

Totals for today (from start of day):

```sql
SELECT SUM(in_tok) AS input, SUM(out_tok) AS output,
       SUM(cache_r) AS cache_read, SUM(cache_w) AS cache_write,
       COUNT(*) AS events
FROM events WHERE ts >= strftime('%s','now','start of day');
```

Totals for the trailing 7 days (including today):

```sql
SELECT SUM(in_tok) AS input, SUM(out_tok) AS output,
       SUM(cache_r) AS cache_read, SUM(cache_w) AS cache_write,
       COUNT(*) AS events
FROM events WHERE ts >= strftime('%s','now','-7 days');
```

By project (last 7 days):

```sql
SELECT p.path, SUM(e.in_tok) AS input, SUM(e.out_tok) AS output,
       SUM(e.cache_r) AS cache_read, SUM(e.cache_w) AS cache_write, COUNT(*) AS events
FROM events e JOIN sessions s ON s.id = e.session_id
JOIN projects p ON p.id = s.project_id
WHERE e.ts >= strftime('%s','now','-7 days')
GROUP BY p.path ORDER BY output DESC;
```

By agent (last 7 days):

```sql
SELECT COALESCE(agent, CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END) AS agent,
       SUM(in_tok) AS input, SUM(out_tok) AS output, COUNT(*) AS events
FROM events WHERE ts >= strftime('%s','now','-7 days')
GROUP BY 1 ORDER BY output DESC;
```

By model, with cost estimate (last 7 days) — rates come from the `pricing` table, never
hardcoded. Each event prices at the rate in force at its own `ts`: the row with the
longest `model_prefix` that is a prefix of the model name, restricted to
`effective_from <= ts`, and among those the greatest `effective_from` (a later dated
rate supersedes the seed once its date arrives). `rate_effective_from` below is the
*latest* rate actually applied within that model's rows — if the 7-day window straddles
a price change, some of the cost may have priced at an earlier rate:

```sql
WITH priced AS (
  SELECT e.in_tok, e.out_tok, e.cache_r, e.cache_w, e.cache_w_1h, m.name AS model_name,
    (SELECT p.in_usd FROM pricing p
       WHERE m.name LIKE p.model_prefix || '%' AND p.effective_from <= e.ts
       ORDER BY LENGTH(p.model_prefix) DESC, p.effective_from DESC LIMIT 1) AS in_usd,
    (SELECT p.out_usd FROM pricing p
       WHERE m.name LIKE p.model_prefix || '%' AND p.effective_from <= e.ts
       ORDER BY LENGTH(p.model_prefix) DESC, p.effective_from DESC LIMIT 1) AS out_usd,
    (SELECT p.cache_r_usd FROM pricing p
       WHERE m.name LIKE p.model_prefix || '%' AND p.effective_from <= e.ts
       ORDER BY LENGTH(p.model_prefix) DESC, p.effective_from DESC LIMIT 1) AS cache_r_usd,
    (SELECT p.cache_w_usd FROM pricing p
       WHERE m.name LIKE p.model_prefix || '%' AND p.effective_from <= e.ts
       ORDER BY LENGTH(p.model_prefix) DESC, p.effective_from DESC LIMIT 1) AS cache_w_usd,
    (SELECT p.cache_w_1h_usd FROM pricing p
       WHERE m.name LIKE p.model_prefix || '%' AND p.effective_from <= e.ts
       ORDER BY LENGTH(p.model_prefix) DESC, p.effective_from DESC LIMIT 1) AS cache_w_1h_usd,
    (SELECT p.effective_from FROM pricing p
       WHERE m.name LIKE p.model_prefix || '%' AND p.effective_from <= e.ts
       ORDER BY LENGTH(p.model_prefix) DESC, p.effective_from DESC LIMIT 1) AS rate_effective_from
  FROM events e JOIN models m ON m.id = e.model_id
  WHERE e.ts >= strftime('%s','now','-7 days')
)
SELECT model_name,
       SUM(in_tok) AS input, SUM(out_tok) AS output,
       ROUND(SUM(in_tok*COALESCE(in_usd,0) + out_tok*COALESCE(out_usd,0)
             + cache_r*COALESCE(cache_r_usd,0)
             + (cache_w - cache_w_1h)*COALESCE(cache_w_usd,0)
             + cache_w_1h*COALESCE(cache_w_1h_usd, cache_w_usd, 0)) / 1000000.0, 4) AS est_cost_usd,
       CASE WHEN MAX(rate_effective_from) IS NULL THEN 'unpriced'
            WHEN MAX(rate_effective_from) = 0 THEN 'seed rates (undated)'
            ELSE date(MAX(rate_effective_from), 'unixepoch') END AS rates_as_of
FROM priced GROUP BY model_name ORDER BY output DESC;
```

`cache_w` is the TTL-agnostic total; `cache_w_1h` is the 1-hour portion (billed at 2×
input vs 1.25× for 5-minute writes), so the formula prices `cache_w - cache_w_1h` at the
5m rate and the rest at the 1h rate — falling back to the 5m rate when a pricing row
predates the split (`cache_w_1h_usd` NULL), which reproduces the pre-v4 estimate.

A model with no matching `pricing` row (no prefix matches) shows `est_cost_usd` of 0 and
`rates_as_of` of `unpriced` — report it as unpriced, not as free. **`effective_from = 0`
always means "seed rates (undated)" — never render it as an epoch date (1970-01-01).**

Main vs subagent split and cache hit rate (last 7 days):

```sql
SELECT CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END AS kind,
       SUM(in_tok) AS input, SUM(out_tok) AS output,
       ROUND(100.0 * SUM(cache_r) / NULLIF(SUM(in_tok) + SUM(cache_r), 0), 1) AS cache_hit_pct
FROM events WHERE ts >= strftime('%s','now','-7 days') GROUP BY kind;
```

By milestone (branch-scoped; all-time — milestones can span more than 7 days):

```sql
SELECT branch, SUM(in_tok) AS input, SUM(out_tok) AS output,
       SUM(cache_r) AS cache_read, SUM(cache_w) AS cache_write, COUNT(*) AS events
FROM events WHERE branch LIKE 'milestone/%' GROUP BY branch ORDER BY output DESC;
```

By tier (last 7 days) — model prefix mapped to the kit's tier names, mirroring
`docs/TELEMETRY-CONTRACT.md` and the kit's `token-economics.md`:

```sql
SELECT CASE
    WHEN m.name LIKE 'claude-fable-%' THEN 'orchestrator'
    WHEN m.name LIKE 'claude-opus-%' THEN 'heavy'
    WHEN m.name LIKE 'claude-sonnet-%' THEN 'small'
    WHEN m.name LIKE 'claude-haiku-%' THEN 'micro'
    ELSE 'unknown'
  END AS tier,
  SUM(e.in_tok) AS input, SUM(e.out_tok) AS output, COUNT(*) AS events
FROM events e JOIN models m ON m.id = e.model_id
WHERE e.ts >= strftime('%s','now','-7 days')
GROUP BY tier ORDER BY output DESC;
```

By issue (all-time; rows with a tagged issue key only):

```sql
SELECT issue_key, SUM(in_tok) AS input, SUM(out_tok) AS output,
       SUM(cache_r) AS cache_read, SUM(cache_w) AS cache_write, COUNT(*) AS events
FROM events WHERE issue_key IS NOT NULL GROUP BY issue_key ORDER BY output DESC;
```

`issue_key` is only populated from schema v2 onward (sidecar or commit-subject
fallback). For a single specific issue, including rows recorded before v2, the kit's
documentation agent uses this same by-issue recipe plus a git-log fallback:
`commit_sha IN (git log --format=%h --grep='^<KEY>:')` (match both short and long `%h`
lengths) unioned with `issue_key = '<KEY>'`, per `docs/TELEMETRY-CONTRACT.md`.

Present: a short headline (today's totals + estimated 7-day cost, with its rates-as-of
label), then the breakdown tables. Keep it compact.
