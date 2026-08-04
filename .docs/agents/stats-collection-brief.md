# Agent brief: collect tracker statistics (Jira)

Fill the placeholders, then hand this brief verbatim to an agent (works at the small-worker tier; micro-model if your Jira MCP tools are reliable).

---

Collect issue statistics for agent-token-telemetry from Jira. Work synchronously, no sub-agents.

TOOLS: one tool-search call for: searchJiraIssuesUsingJql (Atlassian MCP).

TARGET: Jira site zenosyne.atlassian.net, project key AOS. SCOPE: {{SCOPE: "project" | "milestone:<slug>"}} — fill at dispatch time (milestone scope adds `AND labels = "milestone:<slug>"` to every query). PERIOD: last {{PERIOD_DAYS}} days — fill at dispatch time.

1. QUERY (JQL, count-only where possible; respect SCOPE): totals per label value for each dimension — type:*, area:*, origin:*, size:*, and milestone:* labels (each with open vs done split for `milestones`); sev1..sev4 split into open (`statusCategory != Done`) and closed; defects per area (issues labeled type:bug, counted per area:* label, open+closed); `created >= -{{PERIOD_DAYS}}d` and `resolved >= -{{PERIOD_DAYS}}d` counts; oldest unresolved issue labeled sev1-critical or sev2-high (key, or "none").
2. WRITE the snapshot to `.docs/reports/<YYYY-MM-DD>-stats[-<scope-slug>].json` (create `.docs/reports/` if absent; overwrite same-day same-scope file — re-runs are idempotent) with EXACTLY these top-level keys: stats_schema (2), generated, scope, pm_tool ("jira"), project_key ("AOS"), issues_scanned, by_type, by_area, by_origin, by_size, sev_open, sev_closed, defects_by_area, milestones, period ({created, closed, days}), oldest_open_sev1_or_sev2, tokens (per `.docs/agents/token-economics.md`: null when the telemetry DB is absent). Dimension objects map label value → count; omit zero-count values.
3. TOKENS (optional): if the telemetry DB is available per `.docs/agents/token-economics.md`, query it for the same SCOPE and PERIOD and set the snapshot's `tokens` object per the contract (tiers, models, cache hit rate, est_cost_usd from the pricing table); otherwise set `tokens: null`.
4. COMMIT the snapshot file (selective add; attribution: none per this project's settings — no AI attribution in the commit message).

FINAL MESSAGE (machine-consumed): `snapshot: <path>`, `issues-scanned: <n>`, then any failures. Nothing else.
