# Telemetry stability contract

The interfaces external consumers (the agent-operating-kit's reporting, stats
collection, documentation agent — see its own `docs/agents/token-economics.md`) may
rely on. Breaking a promise here always bumps `PRAGMA user_version` and this doc
together, in the same commit.

## Source

SQLite DB at `~/.claude/telemetry/usage.db` (override: `$TOKEN_TELEMETRY_DB`), WAL
mode. Availability check for any consumer: the file exists AND a `projects` row's
`path` matches the consumer's own project root. Absent → the consumer omits its token
output silently; nothing fails, nothing warns.

## Schema version

Current: `PRAGMA user_version = 2`. Migrations are additive deltas applied in
`capture.py`'s `migrate()`, run from `connect()`, and are idempotent — safe to run
concurrently from multiple hook invocations. v1 → v2 added the three `events` columns
below and the `pricing` table; no v1 column was renamed or removed.

## Consumed columns — `events`

| column | type | notes |
|---|---|---|
| `ts` | INTEGER | unix seconds |
| `session_id` | INTEGER | FK → `sessions.id` |
| `kind` | INTEGER | 0 = main session, 1 = subagent |
| `agent` | TEXT | subagent type name, nullable |
| `model_id` | INTEGER | FK → `models.id` |
| `in_tok`, `out_tok`, `cache_r`, `cache_w` | INTEGER | token counts |
| `branch` | TEXT | git branch at capture time; kit milestones use `milestone/<slug>` |
| `commit_sha` | TEXT | short sha at capture time |
| `issue_key` | TEXT | **v2.** From the context sidecar, else a `<KEY>:` commit-subject fallback, else null |
| `task_size` | TEXT | **v2.** From the sidecar's `size`, else null |
| `note` | TEXT | **v2.** From the sidecar's `summary`, else null |

`sessions(id, uuid, project_id)`, `projects(id, path)`, `models(id, name)` are stable
lookup tables unchanged since v1.

## Tier mapping

Model name prefix → kit tier, mirroring the `pricing` table's own prefixes (used by
`commands/token-stats.md`'s by-tier breakdown and the kit's own
`docs/agents/token-economics.md`):

| Model prefix | Tier |
|---|---|
| `claude-fable-*` | orchestrator |
| `claude-opus-*` | heavy |
| `claude-sonnet-*` | small |
| `claude-haiku-*` | micro |

`events.agent` + `events.kind` further distinguish main-session vs subagent work
within a tier.

## Per-issue recipe (with pre-v2 fallback)

Preferred: `events.issue_key = '<KEY>'` (populated from schema v2 onward). Rows
recorded before v2 predate the column and need the fallback instead: `commit_sha IN
(git log --format=%h --grep='^<KEY>:')`, matched against both short and long `%h`
lengths (git's default abbreviation length can change per-repo). A complete per-issue
query unions both: `issue_key = '<KEY>' OR commit_sha IN (...)`. This is the same
recipe the kit's documentation agent uses for its cost-per-issue closing comment.

## Pricing table

```
pricing(provider, model_prefix, model_version, in_usd, out_usd, cache_r_usd,
        cache_w_usd, effective_from, source)
UNIQUE(provider, model_prefix, model_version, effective_from)
```

Cost is never stored per event — always derived at query time. The rate for a given
event is the `pricing` row with the **longest `model_prefix` that is a prefix of the
model name**, restricted to `effective_from <= events.ts`, and among those the
**greatest `effective_from`** (a later dated rate supersedes an earlier one once its
date arrives). See `commands/token-stats.md` for the reference query.

**History is never mutated.** A rate change is always a new `INSERT` with today's date
as `effective_from` and a `source` URL — rows are never `UPDATE`d or `DELETE`d, so a
past event always re-prices identically no matter when the query runs. `INSERT OR
IGNORE` against the unique key makes same-day reruns of `pricing-update` a no-op.

**`effective_from = 0` is the seed marker, not a timestamp.** The v0.2.0 migration
seeds four rows (`claude-fable-`, `claude-opus-`, `claude-sonnet-`, `claude-haiku-`,
`source='seed-v0.2.0'`) at `effective_from = 0` so they price *all* history until a
dated row supersedes them. Any consumer that renders an estimate's rate date **must**
special-case `effective_from = 0` as "seed rates (undated)" — never format it as an
epoch date (1970-01-01).

## Context sidecar (kit → telemetry)

`capture.py` reads `.claude/telemetry-context.json` from the **project root only**
(the nearest ancestor containing `.git`, same resolution `is_enabled()` uses) — a
sidecar written under a subdirectory's `.claude/` is not picked up. Malformed, absent,
or non-dict content is silently treated as no sidecar; capture never fails on it.
Non-scalar values (a JSON object/array under any key) are dropped for that key
(coerced to null) rather than stringified. Shape:

```json
{"issue_key": "<KEY>", "project": "<name>", "size": "<size>", "summary": "<one sentence>"}
```

Fields land on every event recorded by that hook invocation. When no sidecar is
present, `issue_key` falls back to the `<KEY>:` prefix of the last commit subject;
`task_size`/`note` have no fallback and stay null.

## Secrets

DB paths and cost figures are shareable. Transcript contents are never read by capture
and never exposed by any query built against this contract.
