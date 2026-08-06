# Telemetry stability contract

The interfaces external consumers (the agent-operating-kit's reporting, stats
collection, documentation agent — see its own `docs/agents/token-economics.md`) may
rely on. Breaking a promise here always bumps `PRAGMA user_version` and this doc
together, in the same commit.

## Source

SQLite DB at `~/.claude/telemetry/usage.db` (override: `$TOKEN_TELEMETRY_DB`), WAL
mode. This is the **authoritative** store in every storage mode. Availability check for
any consumer: the file exists AND a `projects` row's `path` matches the consumer's own
project root. Absent → the consumer omits its token output silently; nothing fails,
nothing warns.

## Storage modes (v0.3.0)

The opt-in marker `.claude/telemetry` gained content. Its **first line** selects
storage; the file is read from the project root (the nearest ancestor containing
`.git`, the same resolution the sidecar uses):

| First line | Mode | Effect |
|---|---|---|
| `central`, or an empty/contentless file | central (default) | central DB only — identical to v0.2.0 |
| `project` | project | central DB **plus** a project-local mirror |

Matching is case-insensitive and whitespace-trimmed; lines after the first are ignored
(free-form notes are safe there). **Every ambiguous case resolves to central** —
unreadable, oversized (>4 KiB read window), undecodable, or unrecognized content — so a
marker written by v0.1.0/v0.2.0 (an empty `touch`ed file) keeps its exact old behavior.

### Dual-write semantics

In project mode capture writes **both** DBs on every captured turn:

- **Central DB — authoritative.** Its `cursors` table alone drives what is read from a
  transcript, exactly as before. Nothing about the mirror can change what is captured.
- **Mirror DB — best effort**, at `<project-root>/.claude/telemetry-usage.db`. Written
  only *after* the central transaction commits and the central connection is closed, so
  it never holds the central write lock. It is created through the same
  `connect()`/`migrate()` path, so it carries the same schema version and the same
  pricing seed and every query in `commands/token-stats.md` runs against it unchanged.
- **Failures are swallowed.** Any mirror error (unwritable path, locked file, full disk)
  is appended to the central `~/.claude/telemetry/error.log` with a
  `mirror write failed: <path>` label and otherwise ignored — the central write and the
  session are never affected.
- **No cursors in the mirror.** The mirror's `cursors` table exists (same schema) but
  stays empty by design.
- **A symlink at the mirror path is refused**, not followed — it sits inside the repo
  and can therefore arrive committed, which would aim SQLite's writes at any file on
  the machine. The mirror is skipped for that capture and the refusal is logged;
  central capture continues normally.

### Mirror metadata (v0.4.0, schema v3)

The central `projects` row records where a project's mirror lives:

| column | type | meaning |
|---|---|---|
| `mirror_path` | TEXT | the project-local DB path this project is configured to mirror into; NULL = central-only storage |
| `mirror_last_at` | INTEGER | unix seconds — the event timestamp of the last captured turn that was configured to write a mirror |

Both are stamped **inside the central transaction, before the mirror write is
attempted** — they are **configured state, not a write receipt**. They stay stamped when
that mirror write then fails, and that is deliberate: the central DB must always know a
project-level copy is configured, precisely in the case where the mirror is broken. A
consumer must therefore never read `mirror_last_at` as "the mirror is current"; the only
evidence of a landed write is the mirror file itself. A recent `mirror_last_at` with a
missing or stale file at `mirror_path` means mirror writes are failing — see
`error.log`. Mirror DBs never stamp mirror metadata of their own (`mirror_path` stays
NULL inside a mirror). Central-mode projects never get it stamped at all, and a turn that
records no events (cursor advance only) stamps nothing — there is no event timestamp to
record. Both columns are cleared (`UPDATE … SET mirror_path = NULL, mirror_last_at =
NULL`) when a project switches back to central mode via `/token-telemetry:enable` or opts
out via `/token-telemetry:disable`, so they describe current configuration rather than
history; the mirror *file* is never deleted by either command. That clearing is
best-effort housekeeping done by the commands, not by capture: consumers must tolerate a
stale `mirror_path` on a project whose marker was edited or deleted by hand.

### `audit_log` (v0.4.0, schema v3)

```
audit_log(ts INTEGER NOT NULL, action TEXT NOT NULL, project TEXT NOT NULL, detail TEXT)
```

Append-only history of storage-management operations, written by the commands, never by
capture. Actions in use: `export` (`/storage-separate` wrote a validated export),
`delete-after-export` and `delete` (rows removed from the central DB, by
`/storage-separate` and `/storage-delete` respectively). `detail` is free text — the
export filename and/or the removed counts. Audit rows **outlive the project they
describe** and are never deleted by these commands. Consumers may read it; nothing in the
capture path depends on it.

**Duplicates are possible in the mirror, never in the central DB.** Because the mirror
keeps no cursor, replaying a transcript — the central DB being reset, moved or restored
from an older copy while the project-local file is kept — re-inserts rows that the
mirror already has. The re-inserted rows are **identical** across every column, so the
dedupe hint is the full row tuple: `SELECT DISTINCT ts, session_id, kind, agent,
model_id, in_tok, out_tok, cache_r, cache_w, cache_w_1h, dur_ms, branch, commit_sha,
issue_key, task_size, note FROM events` (or `GROUP BY` those columns). Consumers that need exact
totals should read the central DB.

The mirror exists for retention and reuse — it travels with the repo or the team share
— not as a second source of truth. `/token-telemetry:enable` git-ignores it by default
(`.claude/telemetry-usage.db*`) while noting that committing it is a valid team choice.

## Schema version

Current: `PRAGMA user_version = 5`. Migrations are additive deltas applied in
`capture.py`'s `migrate()`, run from `connect()`, and are idempotent — safe to run
concurrently from multiple hook invocations. Hops run in order and each is gated on its
own post-condition: a version is stamped only once the shape it promises is verifiably
present, so a failed hop simply retries on the next connect rather than stranding the DB,
and v3 is never attempted on a DB whose v2 hop failed. The fast path re-checks the actual
shape rather than trusting the stamp, so a DB stamped for a version it does not have
heals itself.

- **v1 → v2** — the three `events` columns below and the `pricing` table.
- **v2 → v3** (v0.4.0) — `projects.mirror_path` and `projects.mirror_last_at`, plus the
  `audit_log` table (both documented above).
- **v3 → v4** (v0.5.0) — cache writes split by TTL: `events.cache_w_1h` (the 1-hour
  portion; **`cache_w` stays the TTL-agnostic total**, so every pre-v4 query keeps
  working and the 5m portion is `cache_w - cache_w_1h`) and `pricing.cache_w_1h_usd`
  (the 1h write rate, 2× input vs 1.25× for 5m; NULL = unknown — cost queries must fall
  back to `cache_w_usd`, which reproduces the pre-v4 estimate).
- **v5 → v6** (v0.10.0) — per-event agent metrics: `events.api_calls` (API calls in
  the slice, counted after the message.id dedupe) and `events.ctx_tokens` (context
  size when the slice ended — the input side of its last call: input + cache read +
  cache write; this is the number Claude Code's own token gauge shows for an agent).
  NULL on pre-v6 rows = unknown, never backfilled.
- **v4 → v5** (v0.7.0) — `projects.name`: the human project name. Capture stamps it
  every turn from the kit's `.docs/PROJECT-INFO.md` frontmatter (`project:` key —
  the kit document wins over any other source); `/token-telemetry:enable` registers a
  user-supplied name when no kit document exists. NULL = unknown; reports fall back
  to the path basename.

No column has ever been renamed or removed. v0.3.0 changed no schema at all — it added
storage modes. A project-local mirror is byte-for-byte the same schema as the central DB;
so is a `/storage-separate` export, which is built through the same `connect()`.

## Consumed columns — `events`

| column | type | notes |
|---|---|---|
| `ts` | INTEGER | unix seconds |
| `session_id` | INTEGER | FK → `sessions.id` |
| `kind` | INTEGER | 0 = main session, 1 = subagent |
| `agent` | TEXT | subagent type name (namespaced where the harness provides it, e.g. `marvin:developer`), nullable; **always NULL on kind=0 rows** since v0.8.1 — sub-agent usage comes from the per-agent transcript sweep, never from main-transcript slices |
| `model_id` | INTEGER | FK → `models.id` |
| `in_tok`, `out_tok`, `cache_r`, `cache_w` | INTEGER | token counts; `cache_w` is the TTL-agnostic cache-write total |
| `cache_w_1h` | INTEGER | **v4.** 1-hour portion of `cache_w` (5m portion = `cache_w - cache_w_1h`); 0 on pre-v4 rows |
| `branch` | TEXT | git branch at capture time; kit milestones use `milestone/<slug>` |
| `commit_sha` | TEXT | short sha at capture time |
| `issue_key` | TEXT | **v2.** From the context sidecar, else a `<KEY>:` commit-subject fallback, else null |
| `task_size` | TEXT | **v2.** From the sidecar's `size`, else null |
| `api_calls` | INTEGER | **v6.** API calls in the slice (post-dedupe); NULL = pre-v6 |
| `ctx_tokens` | INTEGER | **v6.** context size at slice end (last call's input + cache read + cache write); NULL = pre-v6 |
| `note` | TEXT | **v2.** From the sidecar's `summary`, else null. **v0.9.0**: `backlog-capture` marks a first-capture roll-up of pre-telemetry history (cursor started at 0 and the aggregated span exceeded 24h; `dur_ms` carries the span; a real sidecar note always wins). Consumers exclude these from windowed figures and include them in all-time views |

`sessions(id, uuid, project_id)` and `models(id, name)` are stable lookup tables
unchanged since v1; `projects(id, path)` gained the two nullable `mirror_*` columns in
v3 and the nullable `name` column in v5 (both above) and is otherwise unchanged. A row's absence is meaningful: storage-management
commands delete a project's `projects`/`sessions`/`events`/`cursors` rows outright, so a
consumer must treat "no project row" as "no data", never as an error.

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
        cache_w_usd, cache_w_1h_usd, effective_from, source)
UNIQUE(provider, model_prefix, model_version, effective_from)
```

Cost is never stored per event — always derived at query time. The rate for a given
event is the `pricing` row with the **longest `model_prefix` that is a prefix of the
model name**, restricted to `effective_from <= events.ts`, and among those the
**greatest `effective_from`** (a later dated rate supersedes an earlier one once its
date arrives). See `commands/token-stats.md` for the reference query. `cache_w_usd` is
the 5-minute write rate (1.25× input); `cache_w_1h_usd` (v4) is the 1-hour write rate
(2× input) and is NULL on rows that predate the split — price `cache_w_1h` tokens with
`COALESCE(cache_w_1h_usd, cache_w_usd)` so pre-v4 rows keep producing the estimate they
always did.

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
