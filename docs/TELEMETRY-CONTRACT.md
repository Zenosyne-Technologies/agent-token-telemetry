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

### Project key: a worktree belongs to its main repository (schema v8)

`projects.path` is the project's **main repository root**. The checkout root is the
nearest ancestor of the session's cwd holding a `.git` entry. When that `.git` is a
linked-worktree pointer file, the key is the main repository instead, so every
`git worktree` (Claude Code's `<main>/.claude/worktrees/<name>` included) records under
its main repo's row and name. A pointer file counts as a worktree only when **all** of
these hold (else the checkout itself is the key, as before v8):

- `.git` is a regular file (not a symlink) of at most 4096 bytes with one `gitdir:` line;
- that gitdir resolves to `<common>/worktrees/<id>`, and its `commondir` file (≤4096
  bytes) resolves to `<common>`;
- `<common>` is a directory named `.git`, and its parent is a directory.

A submodule (`gitdir` under `.git/modules/…`), a bare repo's worktree (common dir not
named `.git`), a plain checkout and a non-git directory are their own project, unchanged.
**Spelling:** a checkout at `<M>/.claude/worktrees/<…>` keys as `<M>` exactly as spelled
(how the main repo's own sessions write it); any other worktree keys as the realpath of
the main root. Every capture (worktree or not) then reuses an existing row that is
realpath-equal to its key under that row's stored spelling, so symlinked and real
spellings of one repository share one row in either order. The lookup is exact-match
first; only a key with no exact row compares by realpath, and only against rows with
the same basename (of the key or its realpath) whose stored path still exists — a
deleted path is compared by string. Known gap: an alias whose last component is itself
a differently named symlink is not matched. A consumer running inside a
worktree resolves its own project root the same way. `/token-telemetry:enable` and
`/token-telemetry:disable` resolve the same root (`manage.py resolve-root`), so they act
on the main repository and all its worktrees; disable removes the opt-in marker from
the main root, every worktree, the current checkout and the cwd. Branch, commit sha and the
commit-subject `issue_key` fallback still come from the worktree checkout.

## Storage modes (v0.3.0)

The opt-in marker `.claude/telemetry` gained content. Its **first line** selects
storage; the file is read from the checkout root (the nearest ancestor containing
`.git`). Inside a linked worktree with no marker of its own, the main repository's
marker applies — both to opt-in and to the storage mode — and the mirror lives at the
main repository root:

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

Current: `PRAGMA user_version = 8`. Migrations are additive deltas applied in
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
- **v4 → v5** (v0.7.0) — `projects.name`: the human project name. Capture stamps it
  every turn from the kit's PROJECT-INFO.md frontmatter (`project:` key — the kit
  document wins over any other source), resolved via a three-location ladder:
  `.marvin/PROJECT-INFO.md` (kit >=v0.21), then `.docs/PROJECT-INFO.md` (kit
  v0.15-0.20), then `docs/PROJECT-INFO.md` (kit <v0.15) — the first of these that
  exists is the one read, and it alone decides the result (each candidate is
  resolved and must stay within the resolved repo root, so a symlink escaping
  it is treated as invalid at that location rather than falling through);
  `/token-telemetry:enable` registers a user-supplied name when no kit
  document exists at any of the three. NULL = unknown; reports fall back to
  the path basename.
- **v5 → v6** (v0.10.0) — per-event agent metrics: `events.api_calls` (API calls in
  the slice, counted after the message.id dedupe) and `events.ctx_tokens` (context
  size when the slice ended — the input side of its last call: input + cache read +
  cache write; this is the number Claude Code's own token gauge shows for an agent).
  NULL on pre-v6 rows = unknown, never backfilled.
- **v6 → v7** — identity foundation: the `users(uuid, name, created_at)` table and a
  nullable `sessions.owner_id` (FK → `users.uuid`). Additive and inert — this hop only
  lays the schema down; nothing yet mints uuids or stamps `owner_id`. **NULL `owner_id` =
  pre-identity, never backfilled except by a later retro-link step.**
- **v7 → v8** — a **data** step, no shape change: the one-time fold of
  linked-worktree `projects` rows into their main repository's row (see *Project key*
  above). A row folds when its path is `<M>/.claude/worktrees/<name…>` (`<M>` = the
  prefix before the FIRST such component, at least one name segment after it) AND `<M>`
  is realpath-equal to another row's path or is an existing directory with a `.git`
  directory — or when its path still exists and resolves to a different main root. The
  main row is created (spelled `<M>`) when absent. `sessions.project_id` is reassigned;
  the main row takes the worktree's `name` only if it has none, and its
  `mirror_path`/`mirror_last_at` pair only if it has no mirror configured; the worktree
  row is deleted. `events` and `cursors` hang off sessions/transcripts and do not move;
  `audit_log.project` is historical free text and stays untouched. A path that still
  exists with its own `.git` directory (a real clone under `.claude/worktrees/`), or with
  a `.git` file that fails the worktree check above (a submodule), is a separate
  repository and never folds. Fold and stamp share one transaction. The fold is
  idempotent: a DB at v8 skips it on the fast path; a v8 DB whose shape check fails
  (the self-heal path) re-walks the hop chain, runs the fold again and changes nothing
  already folded — it folds only worktree rows that appeared since. **Limitation:** a worktree that lived
  outside `.claude/worktrees/` and has since been deleted cannot be recognised, so its
  row stays. **Remote (Supabase):** new captures key correctly (same resolution), but
  remote history is **not** folded — see the developer handbook `capture-pipeline.md`.

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
| `branch` | TEXT | git branch at capture time — corroboration for a single work item only, **never a grouping key**: gitflow (kit >=v0.22) no longer names milestone branches `milestone/<slug>`, so a query that groups or filters on that pattern silently matches nothing. Use the per-issue/per-scope recipe below instead |
| `commit_sha` | TEXT | short sha at capture time |
| `issue_key` | TEXT | **v2.** From the context sidecar, else a `<KEY>:` commit-subject fallback, else null |
| `task_size` | TEXT | **v2.** From the sidecar's `size`, else null |
| `api_calls` | INTEGER | **v6.** API calls in the slice (post-dedupe); NULL = pre-v6 |
| `ctx_tokens` | INTEGER | **v6.** context size at slice end (last call's input + cache read + cache write); NULL = pre-v6 |
| `note` | TEXT | **v2.** From the sidecar's `summary`, else null. **v0.9.0**: `backlog-capture` marks a first-capture roll-up of pre-telemetry history (cursor started at 0 and the aggregated span exceeded 24h; `dur_ms` carries the span; a real sidecar note always wins). Consumers exclude these from windowed figures and include them in all-time views |

`models(id, name)` is a stable lookup table unchanged since v1; `sessions(id, uuid,
project_id)` gained the nullable `owner_id` column in **v7** (FK → `users.uuid`; NULL =
pre-identity, never backfilled except by a later retro-link step) and is otherwise
unchanged since v1; `projects(id, path)` gained the two nullable `mirror_*` columns in
v3 and the nullable `name` column in v5 (both above) and is otherwise unchanged.
`users(uuid, name, created_at)` is the **v7** identity table — inert in this phase
(nothing yet writes rows to it). A row's absence is meaningful: storage-management
commands delete a project's `projects`/`sessions`/`events`/`cursors` rows outright, so a
consumer must treat "no project row" as "no data", never as an error.

## Tier mapping

**ROLE-based, not model-based** — this section mirrors the agent-operating-kit's
`templates/marvin/agents/token-economics.md` (kit v0.30.0) verbatim: since that kit
version the orchestrator runs on the heavy tier's own model, so a model name prefix
can no longer tell orchestrator and heavy-worker cost apart. Used by
`commands/token-stats.md`'s by-tier breakdown (`scripts/report.py`'s `tier_case()`,
mirrored in Postgres by `supabase/reports.sql`).

| Events | Tier |
|---|---|
| main session (`events.kind = 0`, `events.agent` NULL) | orchestrator |
| `marvin:developer` · `marvin:researcher` · `marvin:validator-*` | heavy |
| `marvin:escalation-*` | ladder (per rung from `events.agent`: high · xhigh · max · frontier) |
| `marvin:developer-small` · `marvin:documenter` | small |
| `marvin:ponytail` | micro |

Any other agent — a persona the kit hasn't named, or a NULL agent on a row that is
NOT the main session (e.g. `kind = 1` with no agent) — falls back to its model name's
prefix: `claude-opus-*` heavy · `claude-sonnet-*` small · `claude-haiku-*` micro ·
`claude-fable-*` ladder; no match is `unknown`. Tier names and order match the kit.

The role split shows up in `by_tier` (and `by_rung`), NOT in `by_model`: that stays
ONE row per model, as before role tiering, but its tier column now lists EVERY tier
the model actually served that window, comma-joined in the kit's display order
(orchestrator, heavy, ladder, small, micro — `unknown` last) — e.g. a model used both
as the main session and as a `marvin:developer` subagent reads `orchestrator, heavy`
on its single row. `by_rung` breaks the `ladder` rows of `by_tier` down by escalation
rung, read only from the named `marvin:escalation-<rung>` persona (`events.agent`) —
never inferred from a model or effort setting; a `ladder` row with no such name (the
model-prefix fallback, or an unrecognized `marvin:escalation-*` suffix) is grouped
under the `no rung (fallback)` label instead of dropped, so `by_rung`'s rows always
sum to `by_tier`'s ladder total.

## Scoping recipes (with pre-v2 fallback)

There is no reliable grouping key for "everything spent on this milestone/effort" —
`branch` is capture-time corroboration for one event, not a stable label to group or
filter by (see the `branch` row above). Scope a rollup by caller-supplied issue key(s)
instead:

Preferred: `events.issue_key = '<KEY>'` (populated from schema v2 onward). Rows
recorded before v2 predate the column and need the fallback instead: `commit_sha IN
(git log --format=%h --grep='^<KEY>:')`, matched against both short and long `%h`
lengths (git's default abbreviation length can change per-repo). A complete per-issue
query unions both: `issue_key = '<KEY>' OR commit_sha IN (...)`. This is the same
recipe the kit's documentation agent uses for its cost-per-issue closing comment.

**Scoping to a set of keys** (e.g. every issue in a milestone): apply the recipe above
per key — try `issue_key = '<KEY>'` first, fall back to the `commit_sha` search only
for a key with zero tagged rows — then sum across the set. `report.py`'s `--scope
KEY1,KEY2,...` flag implements exactly this and renders the result as three-state:
an unparseable/empty key set fails scope resolution outright; a project with zero
events at all reads as telemetry absent, not as zero spend; and zero of N scoped keys
having rows reads as a broken scope, not as zero spend. A bare `$0`/`0` render for a
rollup that matched nothing is exactly the failure mode this replaces (`branch LIKE
'milestone/%'` reading as "spent nothing" once gitflow stopped naming branches that
way) — never repeat it for a different empty-match reason.

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

**History is never mutated.** A rate change is always a new `INSERT` with a `source`
URL, dated today — except an arrived `starting <d>` scheduled increase, which is dated
`d` (its real start; see "`pricing-update` mints only the rate in force today" below).
Rows are never `UPDATE`d or `DELETE`d, so a past event always re-prices identically no
matter when the query runs — with one explicit exception: the consent-gated backfill
("Third narrow case" below) INSERTs a row dated in the past for events that were priced
at an ESTIMATE, which does change what those specific events re-price to. It is still
not a mutation (no row is ever `UPDATE`d or `DELETE`d) and it never touches an event
that was already priced by its own row. `INSERT OR IGNORE` against the unique key makes
same-day reruns of `pricing-update` a no-op.

**Narrow exception — a withdrawn forecast may be deleted.** A pricing row may be
`DELETE`d in exactly two cases: (1) it is **future-dated and not yet in effect**
(`effective_from > now`), or (2) it recorded a **forecast** — a `starting <date>`
scheduled increase that was written before its date arrived — that the publisher
**subsequently withdrew** (the announced rate changed or was cancelled before it ever
took effect). The reason immutability does not protect these rows is that they never
priced a real charge: a withdrawn prediction is not a record of what was actually
billed, so deleting it corrects the table rather than rewriting history. Every other
row — one whose `effective_from <= now` and whose rate was, at some point, the rate
actually charged — is immutable and is never `UPDATE`d or `DELETE`d. `pricing-update`
itself never mints a future-dated row: a scheduled increase is recorded only on the
first run on or after its effective date, so case (2) cannot arise from the script's
own normal run. It is not reachable through the command's fallback either (AOS-143
correction, round 2, F1b): that fallback no longer parses or inserts a row by hand —
it re-fetches the page and re-runs this same script — so case (2) currently has no
documented path at all; it is recorded here only because nothing but a manual write
could ever have produced it.

**Third narrow case — a consent-gated backfill may date an INSERT in the past.** Not a
`DELETE` and not an `UPDATE`: the two cases above stay the only deletions. When a
model's events were priced at an **estimate** (their resolved row was a family default
or ancestor row — "Own price vs estimate" below) before that model's own row was first
minted, `pricing_update.py --backfill-plan` offers, per such prefix P, one extra row: a
copy of P's earliest own row R0, dated the UTC start of the day of the earliest
estimated event P is the own row for. The plan reads the DB read-only (writes no DB row)
and caches a plan summary in `backfill-plan.json` next to the DB for the dashboard (see
"Dashboard plan summary" below); it lists every event whose resolved row would change
(other models sharing the prefix included, under their own names), the window and its
span, and cost now → after. `--backfill-apply <prefix>...`
re-plans, writes all named rows in one transaction or none, and verifies that exactly
the planned events changed, rolling back otherwise; it accepts only arguments of the
strict prefix shape the parser mints (`claude-<family>-<version>` or a legacy alias),
rejecting anything else with exit 2 before the DB is opened. This is not a history rewrite: it
replaces an ESTIMATE — a family-default or ancestor rate that was never that model's
own published rate — with the model's own published rate, only for events that were
estimated, only with the user's explicit consent in the interactive
`/token-telemetry:pricing-update` flow (an unattended or scheduled run never applies),
and as an `INSERT` recording its provenance in `source`
(`backfill:<R0 source>; confirmed <YYYY-MM-DD>`). An event priced by a model's own row
is never re-priced: a candidate whose row would change one (or price a previously
unpriced event) is refused, not offered. **Own-row closure:** an apply is valid only if,
after it, every event in its impact set resolves to its OWN model's row — a candidate
whose row would otherwise move another model's estimated events onto a row that is
still an estimate for them cannot apply alone; the plan offers it as a BUNDLE with that
other model's own candidate when one exists, and refuses it, naming the model it cannot
close, when none does. `--backfill-apply` refuses a named prefix set that is not closed
under this requirement, and its post-apply verification additionally checks that every
impacted event is no longer estimated. Nothing is ever `UPDATE`d or `DELETE`d.
Limitation: the backfill writes the **local** central DB only; the remote (Supabase)
backend's `pricing` table is not touched, and carrying such rows there is future work
(remote pricing sync).

**Dashboard plan summary (`backfill-plan.json`).** A plan can take tens of seconds on
a large DB, so the dashboard never computes one. Every `--backfill-plan` run (markdown
or `--json`) over the DB the dashboard reads (`$TOKEN_TELEMETRY_DB`, else
`~/.claude/telemetry/usage.db`; a `--db` pointing at any other file writes nothing)
writes a summary sidecar beside it, `backfill-plan.json`:
`{version: 1, computedAt: <epoch s>, bundles: <offered rows, candidates + confirm-only>,
deltaText: <the plan's "everything offered" signed delta, e.g. "-$272.46", or null>,
fingerprint: <64 hex>}`. A successful `--backfill-apply` on that DB deletes it. It is
written atomically (a fresh `O_EXCL|O_NOFOLLOW` 0600 temp file, then rename), never
through a symlink (a symlink or non-regular file at that name is refused and left
alone), and only when the fingerprint taken just before the plan equals the one taken
just after it. Writing it never changes the plan's stdout or exit code; a failure
prints one note to stderr. **Staleness is keyed to a fingerprint, not an age:** SHA-256
over every `pricing` row plus count / rowid / `ts` / `model_id` / token aggregates of
the events dated before the plan horizon — the latest first-row `effective_from` of any
non-family-default prefix (no backfill row can re-price an event at or after its
prefix's first own row, so later events cannot change a plan). The dashboard recomputes
it on each `/api/data` (only when a valid, non-empty summary exists) and shows the
banner's "Backfill available: N bundle(s), <delta> — run
`/token-telemetry:pricing-update` to review and confirm (plan computed <age>)." line
only on an exact match: a pricing refresh, an apply, an import or deletion of pre-horizon
events all hide it until the next plan, while ordinary new capture does not. A missing,
unreadable, oversized, corrupt, wrongly-typed, symlinked or stale summary yields no
line, never an error. The dashboard never applies a backfill and offers no control that
does. **Remote backend:** the plan and apply cover the local DB only, so the remote
(Supabase) backend cannot supply a plan — the line is always hidden while
`active_backend` is `supabase`; `supabase/reports.sql` has no counterpart.

**`effective_from = 0` is the seed marker, not a timestamp.** The v0.2.0 migration
seeds four rows (`claude-fable-`, `claude-opus-`, `claude-sonnet-`, `claude-haiku-`,
`source='seed-v0.2.0'`) at `effective_from = 0` so they price *all* history until a
dated row supersedes them. Any consumer that renders an estimate's rate date **must**
special-case `effective_from = 0` as "seed rates (undated)" — never format it as an
epoch date (1970-01-01).

**Own price vs estimate.** Four defined terms, each implemented once per dialect
(Python `capture.is_family_default` / `capture.is_ancestor_row` /
`capture.is_estimated`, SQLite `capture.family_default_sql` /
`capture.ancestor_row_sql` / `capture.estimated_sql`, Postgres the `estimated`
column of `report_model_pricing` in `supabase/reports.sql` — the single Postgres
definition of the expression; `report_priced_events` merely PASSES IT THROUGH,
via its per-event LATERAL join onto the resolved `report_model_pricing` row,
rather than re-deriving it):

- **Family default row** — a pricing row whose `model_prefix` matches
  `^claude-[a-z]+-$`: a bare family prefix such as `claude-opus-` or `claude-fable-`,
  including the `effective_from = 0` seed rows. It is the family's fallback rate for
  any model of that family without a closer row. `claude-opus-5-5`,
  `claude-opus-4-2025`, `claude-opus-4-`, `claude-3-5-haiku` and `claude-sonnet-4`
  are **not** family default rows.
- **Ancestor row** — for a given model, a row that matches it only because the model
  is an unlisted point release of the row's version. Let R be the model name with the
  row's `model_prefix` removed from its start; the row is an ancestor row when R
  matches `^-[0-9]{1,2}(-|$)` (a point-release segment): `claude-opus-5` for
  `claude-opus-5-5` (R = `-5`). A date snapshot is **not** an ancestor:
  `claude-haiku-4-5` for `claude-haiku-4-5-20251001` (R = `-20251001`) and
  `claude-opus-4-2025` for `claude-opus-4-20250514` (R = `0514`) are the model's own
  rows.
- **Estimated event** — an event whose resolved pricing row (the resolution above,
  unchanged) is a family default row **or** an ancestor row for its model. Its cost is
  an estimate, not necessarily that model's published price. An unpriced event (no row
  resolves) is neither estimated nor own-priced.
- **Model without own price** — a model name with at least one event bearing a
  non-zero token count (input, output, cache read or cache write) for which **no**
  pricing row whose prefix is a prefix of the name is its own row (neither a family
  default row nor an ancestor row for that name), at any `effective_from`. Every event
  of such a model is estimated or unpriced (a model with no matching row at all, e.g.
  a non-Claude model, is therefore included). A model that has an own row is not one,
  even if some of its older events predate that row and are still estimated. A model
  whose events are **all** zero-token (e.g. a synthetic bookkeeping model such as
  `<synthetic>`) is excluded even when it has no own row: there is nothing of theirs
  to price, so naming them in the footer would be noise.

The report data carries these flags (`report_priced_events.estimated` — sourced
from `report_model_pricing.estimated`, not recomputed — per-row estimated-event
counts, the list of models without own price); they do not change any computed
cost.

**`pricing-update` mints only the rate in force today, per listed version.** Each
model version read off the published page gets its own specific prefix(es)
(`claude-<family>-<version>`, or the legacy alias prefixes such as `claude-3-5-haiku`
/ `claude-opus-4-0`) — including each family's newest version — at the rate in force
on the run date:

- a `through <d>` intro rate is in force while `d >= today` and is minted dated
  today; once `d < today` it is **expired** and mints nothing — a stale page footnote
  never re-asserts an intro rate;
- a `starting <d>` increase is minted dated `d` once `d <= today`, never before;
- a version's unconditional rate is minted dated today **only if** that version has
  no in-force conditional, so a pre-increase or post-intro rate never overrides the
  rate actually charged;
- an in-force `through` wins regardless of where it sits relative to the version's
  unconditional row on the page;
- when the page lists several unconditional rows for one version (e.g. a long-context
  row), the **first** one listed is the version's rate;
- the `claude-<family>-` family default row, dated today, carries the in-force rate of
  the family's **newest** version (its first-listed one — the page lists newest first),
  whether that version is listed unconditionally or only conditionally (`through` /
  `starting`), by the same in-force rule as the version's own row. A family whose only
  listings are conditional still gets its family row.

**Known limitation — expired intro with no other rate.** When a version's only listing
is an expired `through <d>` intro (no unconditional row, no in-force `starting`), the
page states no rate in force today: nothing is minted for that version (nor for its
family default when it is the family's newest version), and the run report prints a
`STALE-PRICE WARNING` line naming the version and its intro end date. Its events keep
the last recorded rate until the page publishes a post-intro rate. They are **not**
flagged estimated: that would require storing the row's condition (`through <d>`),
which the `pricing` table does not hold. The mirror case — the family's newest version
is listed only with a **future** `starting <d>` (`d > today`) — also mints no rate for
it and no family row either; the run report prints a `FUTURE-RATE WARNING` line instead,
saying the family default keeps its last recorded rate until the increase's date arrives.

**Parser bounds (AOS-143, corrected) — what a fetched page can mint is bounded, because
a bad row here is permanent.** A security review found the parser could be made to mint
rows it should not; `pricing_update.py` refuses on four axes, before any row reaches the
database. The first fix shipped had its own bug (F1, found in a later validation pass)
and has since been corrected — see below.

**Three distinct exit codes, so the command's manual fallback can never bypass a
bound (AOS-143 correction, F1).** A plain refresh run (no `--backfill-*` flag) ends in
one of: `EXIT_BOUNDS_REFUSED` (1) — the page read and parsed fine but violated a bound
below; `EXIT_FETCH_FAILED` (2) — the page (or `--html` file) could not be read at all;
`EXIT_OTHER_ERROR` (3) — the page read fine but its structure did not parse (layout
changed, table/column not found) or another unexpected error occurred. The command's
manual fallback (`commands/pricing-update.md`) runs **only** on `EXIT_FETCH_FAILED` —
the first fix shipped let a bound refusal (both the rate-bounds and the row-count
checks below) share the same exit code as a fetch/layout failure, so the fallback's
own unbounded manual read would run for exactly the pages these bounds exist to
refuse. A structural parse failure gets its own code too, and also never falls back:
a page that fetched fine but did not parse as expected cannot be told apart from one
altered in transit, so it is never handed to the fallback either.

**Round 2 (F1b): the fallback itself closed, and only on an interactive run.** A
security re-validation found that even with the three codes above separated, a
hostile server could force `EXIT_FETCH_FAILED` on demand (a 403 to the script's
fixed User-Agent, a 500, a redirect loop, a truncated body), then serve the agent's
own differently-identified fetch a different page — and the command's prose fallback
applied looser bounds than the script's (a per-row skip instead of refusing the whole
page; no family/prefix-shape check; no cap on an unattended run). The fallback no
longer parses or inserts anything itself: it re-fetches the page and re-runs
`pricing_update.py --html <file>`, so every bound above applies through the one
implementation, and it never runs on an unattended or scheduled run at all — on
`EXIT_FETCH_FAILED` those report and stop, same as the other two codes (see
`commands/schedule-pricing.md`). A related finding in the same pass (hang/DoS): the
fetch itself now enforces one wall-clock deadline across connect + the full read
(`FETCH_TIMEOUT_S`) and a body-size cap (`FETCH_MAX_BYTES`), since `urlopen`'s own
`timeout=` only bounds each individual socket operation and a server that trickles
data could otherwise avoid it indefinitely.

- **Backdated `starting`.** An in-force `starting <d>` row is normally minted dated `d`
  however far back — `starting January 1, 1970` would mint `effective_from = 0`,
  re-pricing that prefix's entire history. The refusal is **per row**, and applies to
  `starting` rows only:
  1. In-force status is decided from the unfiltered page, exactly as before AOS-143:
     it picks each version's in-force rows, and a version with any in-force conditional
     keeps its unconditional base rate suppressed — whether or not that conditional is
     later refused.
  2. Each candidate row is then skipped only if it is itself a `starting` row dated more
     than 365 days before today. Refusal skips ONLY that row's `INSERT`: every other row
     of the same version still mints — a newer arrived `starting` increase at its own
     date, an in-force `through` intro dated today. A `through` row is never refused,
     whatever its date.
  3. The family default takes the newest version's latest surviving in-force row; when
     every in-force row of that version was refused, no family row is minted and the
     family default keeps its last recorded rate. Nothing that was suppressed before
     AOS-143 becomes mintable because of a refusal.
  4. A refused row that is already in the database — every one of its prefixes holds a
     row at that date with identical rates, minted when the increase first arrived — is
     a no-op and prints no warning. Any other refused row prints one
     `BACKDATED-STARTING WARNING` line and the run proceeds: "a starting row dated `<d>`
     is more than 365 days old and was not recorded; the rows already recorded for
     `<version>` are unchanged".

  An arrived increase within 365 days mints at its date even when later rows already
  exist for that prefix, and even when the page also lists an older, refused `starting`
  row for the same version; `INSERT OR IGNORE` keeps a re-run at an already-recorded
  date a no-op. There is no other bound on `starting` dates.
- **Unbounded rate magnitude, and malformed numeric tokens (AOS-143, corrected).**
  `money()` matches the WHOLE contiguous `$`-prefixed numeric-ish token (digits, commas,
  dots, exponent markers, signs) and requires it to be a plain decimal number — ASCII
  digits with at most one `.` — or refuses. A thousands separator (`$1,500`), more than
  one decimal point (`$4.00.00`), or any exponent marker, complete (`$1e309`) or dangling
  after a bare `.` (`$1.e3`), is refused outright rather than silently truncated to its
  leading digits (`$1,500`/`$1e309`/`$1.e3` used to mint `$1`). A well-formed number that
  survives that check is still bounded in `parse_models()`: non-finite (a several-hundred-
  digit rate cell overflows `float()` to `inf` with no exception raised), over
  $10,000/MTok, or — for `in_usd`/`out_usd` only, cache rates keep no lower bound — under
  $0.01/MTok. Any of these refuses the **whole run** atomically (nothing written, single
  transaction, `EXIT_BOUNDS_REFUSED`).
- **Unbounded row count.** A page listing thousands of model rows would mint thousands
  of candidate pricing rows in one run. More than 500 candidate rows in one run refuses
  the whole run atomically (nothing planned or applied; `EXIT_BOUNDS_REFUSED`, the same
  code the rate-bounds checks above use — both are the script REFUSING a page bound, a
  different failure mode from either a fetch or a structural parse failure).
- **Raw page text and fetch-error text in printed messages.** The two parse-failure
  messages that embed page text (an unrecognized header, an unparseable rate cell), and
  now `main()`'s fetch-failure message too (its exception text can carry an attacker- or
  MITM-controlled raw HTTP status line — AOS-143 correction), strip ASCII control
  characters and DEL, the C1 control range (U+0080-U+009F, including U+0085 NEL and
  U+009B — the 8-bit form of CSI, usable to start a terminal escape sequence without a
  7-bit ESC byte), and Unicode bidi-control characters (U+200E LRM, U+200F RLM, U+061C
  ALM, U+202A-U+202E, U+2066-U+2069 — the first three were missing from the original
  strip set), and cap length, before the message is ever constructed.
- **Unbounded per-row warning lines (AOS-143, corrected).** A STALE-PRICE,
  BACKDATED-STARTING or FUTURE-RATE warning is not a candidate row, so the 500-row cap
  above does not bound how many of them one run can print — the command prints stdout
  "verbatim" into the agent's context, so an adversarial or just very large page could
  otherwise print thousands of warning lines. Each category is capped at 20 lines plus
  one "... and N more" summary line (`WARNING_CAP`); the rows/family defaults themselves
  are unaffected.

A model the page does not list is priced by the longest matching prefix, unchanged:
an unlisted point release of a listed version (e.g. `claude-opus-5-5` while the page
lists Opus 5) prices at its nearest listed ancestor's own row (`claude-opus-5`) for
events on or after that row's `effective_from`, and before it at whatever row resolved
before — typically the family default. Either way its events are **estimated** until a
refresh lands its own row. A refresh is forward-only: minted rows are dated today (or at
an arrived `starting` date), so events before them keep the rate they already had —
unless the user confirms the consent-gated backfill described under "History is never
mutated".

## Context sidecar (kit → telemetry)

`capture.py` reads `.claude/telemetry-context.json` from the **checkout root** (the
nearest ancestor containing `.git`) — the kit writes it into the checkout the session
runs in — and, inside a linked worktree that has none, from the main repository root.
A sidecar written under a subdirectory's `.claude/` is not picked up. Malformed, absent,
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
