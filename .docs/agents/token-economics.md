# Token & cost telemetry contract

The single description of the seam between the kit and the token-telemetry plugin. Every consumer (stats collection, reporting, documentation agent) reads this file instead of re-deriving these rules.

## Source

SQLite DB at `~/.claude/telemetry/usage.db` (override: `$TOKEN_TELEMETRY_DB`), tables `events`/`sessions`/`projects`/`models`/`pricing`. Available when the file exists AND a `projects` row's path matches this repo's root. **Absent → every consumer omits its token output silently; nothing fails, nothing warns.**

## Scoping recipes

`events.branch` is capture-time corroboration for a single event only — **never**
a grouping or filter key. Gitflow (kit ≥v0.22) no longer names milestone
branches `milestone/<slug>`, so a query grouping on that pattern silently
matches nothing rather than erroring.

- **Milestone/version**: resolve to the milestone's issue-key set first (the
  native mapping documented in `.docs/agents/tracker-config.md` — epics carry
  a `milestone:<slug>` label under the virtual-milestone rule), then apply the
  per-issue recipe below to each key and sum across the set.
  `scripts/report.py --scope KEY1,KEY2,...` implements exactly this and
  renders three distinguishable empty states instead of a bare `$0`: an
  unparseable/empty key set (scope resolution failed), a project with zero
  telemetry at all (telemetry absent), and zero of N scoped keys having rows
  (broken scope) — never collapse these into an unexplained zero.
- **Period**: `events.ts` window (the report's date range).
- **Per-issue**: `events.issue_key = '<KEY>'` when rows are tagged directly
  (preferred); fallback `events.commit_sha IN (git log --format=%h
  --grep='^<KEY>:')` (match both `%h` lengths) when `issue_key` is null,
  restricted to shas whose commit **subject line** actually starts with
  `<KEY>:` — the `--grep` pass is a prefilter only, since it can also match a
  later body paragraph and misattribute the commit.

## Tier mapping

Model name prefix → kit tier, mirrors the pricing table's own prefixes:

| Model prefix | Tier |
|---|---|
| `claude-fable-*` | orchestrator |
| `claude-opus-*` | heavy |
| `claude-sonnet-*` | small |
| `claude-haiku-*` | micro |

`events.agent` + `events.kind` distinguish main-session vs subagent work within a tier.

## Pricing

Never stored per event. Cost derives at query time from the telemetry DB's `pricing` table (provider × model × model-version, effective-dated): join against the rate in force at `events.ts` (latest `effective_from` ≤ ts), longest-prefix model match. A reported cost figure always carries its `effective_from` date. Seed rows carry `effective_from = 0` — render them as "seed rates (undated)", never as a 1970 date.

## Context sidecar

When telemetry is enabled, write the repo-root `.claude/telemetry-context.json` at tracker-task start and rewrite it on every task switch:

```json
{"issue_key": "<KEY>", "project": "<name>", "size": "<size>", "summary": "<one sentence>"}
```

Delete it when leaving tracker work (no active task). Capture stamps these fields onto events as they are written. Gitignored — install/upgrade add the ignore line. Attribution is last-declared-task: events land under whichever task was declared most recently, even across a race with a switch. That approximation is documented, not hidden.

## Snapshot `tokens` object

Stats snapshots (schema v2) carry a `tokens` key, null when telemetry is absent — every consumer must handle both. Keys: `in`, `out`, `cache_r`, `cache_w`, `cache_hit_pct`, `by_tier` (orchestrator/heavy/small/micro → `{out, est_cost_usd}`), `by_model`, `main_vs_subagent`, `est_cost_usd`, `events`.

## Secrets

DB paths and cost figures are shareable. Transcript contents are not — no query or render built from this contract ever exposes transcript text. Anything posted to the PM tool (comments, attachments) stays governed by `.docs/agents/security.md`.
