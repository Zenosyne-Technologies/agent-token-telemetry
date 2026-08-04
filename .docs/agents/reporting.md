# Producing reports

All reports render FROM a stats snapshot — never query the tracker directly. Collect first: dispatch the installed `.docs/agents/stats-collection-brief.md` (coordinates pre-resolved at install; fill SCOPE/PERIOD at dispatch), which writes `.docs/reports/<date>-stats[-<scope>].json` (schema v1). Then dispatch ONE Claude Sonnet 5 (claude-sonnet-5) render agent briefed with the snapshot path and the render type below. Renders are md files committed like any doc work.

## Renders

1. **Architect digest** (on-demand) → `.docs/reports/<date>-digest.md`. One page for the human running the sessions: what shipped in the period; demand-source mix (by_origin); quality posture (sev_open vs sev_closed, oldest_open_sev1_or_sev2, trend vs the previous digest's snapshot if one exists in `.docs/reports/`); where effort went (by_area; by_size mix); milestone progress; token economics when the snapshot carries `tokens` — period cost, tier split, cache hit rate, trend vs the previous snapshot, and a call-out when heavy-tier spend concentrates on small-sized issues. End with ≤3 actionable observations, not summaries.
2. **Milestone close-out** (lifecycle-triggered at milestone close — see CLAUDE.md standing rules) → `.docs/reports/<date>-closeout-<slug>.md` PLUS a comment on the milestone container (epic/milestone). Close-outs render FROM a milestone-scoped snapshot (collect with SCOPE milestone:<slug>) — never from a project-wide one. Contents: delivered vs planned scope (milestones[slug] open vs done), defects by area (defects_by_area), sizing mix of the milestone's work (by_size), research-pass outcomes if recorded, notable deviations; total milestone cost (from the milestone-scoped snapshot's `tokens`).
3. **Stakeholder page** (on-demand) → polished md at `.docs/reports/<date>-stakeholder.md`, or a tracker/Confluence doc where connected. Progress, roadmap position, quality summary; total estimated cost in absolute dollars with trend — one currency line, still no tiers, models, or token counts. STRIP agent internals: no origin:/size: labels, no tier or escalation talk, no agent names — a reader outside the team must see product progress only.

## Rules

- Snapshot first, always — a render without a fresh same-day snapshot starts by dispatching collection.
- Render briefs follow `briefing.md` (machine-consumed FINAL MESSAGE: `report: <path>` plus `comment: <url>` for close-outs).
- Numbers come from the snapshot verbatim — an agent that recomputes or estimates figures is doing it wrong.
- `tokens: null` in the snapshot → every render omits its cost content silently.
