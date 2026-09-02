---
title: Capture Pipeline
audience: developer
module: capture
sources: [scripts/capture.py, hooks/hooks.json, docs/TELEMETRY-CONTRACT.md]
updated: 2026-09-02
related: [[pricing-and-cost]]
---

# Capture Pipeline

`scripts/capture.py` runs as the `Stop` and `SubagentStop` hook (wired in
`hooks/hooks.json`) for every Claude Code turn and subagent completion. It reads
the hook JSON on stdin, tails the session transcript for new usage entries, and
appends aggregated rows to the central SQLite DB at `~/.claude/telemetry/usage.db`.

## Never break a session

Capture runs inside the hook path of every turn, so a crash here is a crash the
user feels mid-session. `main()` wraps everything in a single `try/except` and
always exits 0 (`sys.exit(0)` after `main()` regardless of outcome) — a bad
transcript, a locked DB, a malformed sidecar can never surface to the user or
block the session.

Error logging itself is gated behind opt-in: `enabled` is set *before* the first
statement that can raise, and `log_error()` only fires `if enabled`. This isn't
just tidiness — before opt-in is established, we don't know the project wants
any disk writes from this hook at all, including a log file. A malformed-stdin
or unresolvable-cwd failure that happens pre-gate exits silently; only failures
after the project has opted in get recorded to `error.log`.

## Cursor/offset transcript tailing

`read_new_entries()` seeks to a stored byte offset and reads to EOF, then finds
the last `\n` in what it read and discards everything after it before parsing.
Transcripts are append-only but may be mid-write when the hook fires, so a
trailing partial line is not a parse failure to recover from — it's expected
input that must never be consumed. The offset only ever advances to the last
complete line; the unconsumed remainder gets picked up whole on the next
firing.

## Lock ordering: git before BEGIN IMMEDIATE

`main()` deliberately runs `git_meta()` (branch/sha) and `read_sidecar()`
*before* opening the write transaction. Git subprocess calls can take up to
~2s each; `connect()` opens with `timeout=5` and busy-waits for the SQLite
write lock. If enrichment ran *inside* the `BEGIN IMMEDIATE` transaction, it
would hold that lock for the duration of the git calls, and a concurrent hook
firing on a different transcript could exceed its own 5s busy-wait and silently
drop its event. Enrichment has no dependency on cursor/DB state, so it costs
nothing to hoist above the lock — and everything to leave inside it.

Once the lock is taken, the sequence (offset read → aggregate → insert →
cursor update) is intentionally serialized per-transcript via `BEGIN
IMMEDIATE`, so concurrent hook firings on the same transcript can't race the
read/aggregate/insert cycle into a double-count or a dropped event.

## Migration post-condition rule

`migrate(conn)` never trusts `PRAGMA user_version` alone as proof the schema
matches. A DB stamped for a version it doesn't actually have — an older build
that stamped too early, or a hand-edited/restored file — would otherwise fail
every future capture forever, since the fast-path check would keep skipping
migration. The fix is a post-condition, not a version check: after attempting
the `ALTER TABLE` statements (each individually idempotent, not wrapped in one
shared transaction, so a migrating process never blocks a peer's capture), the
code re-reads the actual column set and only stamps the version if the columns
are really present. If they aren't, the function returns without stamping — the
next connect() just retries the migration. The same logic applies to seeding the
`pricing` table: `CREATE TABLE` autocommits, so a crash between table creation
and the seed `INSERT` can leave an empty table that must still get seeded on the
next attempt; the code gates on the presence of seed rows (`SELECT 1 FROM
pricing WHERE source=?`), not on the table's existence.

Versions are applied as a **chain of hops**, each returning whether its own
shape landed: `migrate_v2()` (the kit-aware `events` columns + `pricing`) then
`migrate_v3()` (mirror metadata on `projects` + `audit_log`). `migrate_v3` runs
only if `migrate_v2` reported success — attempting v3 on a DB whose v2 hop just
failed would either fail again or, worse, stamp past a gap. A v1 DB therefore
walks the whole chain in a single `connect()`, and a DB that fails halfway
simply retries from where it stopped on the next hook firing. The fast path
checks the shape of *every* hop (v2 columns, mirror columns, `audit_log`), not
just the newest, which is what lets a DB stranded at any version heal itself.

## Storage modes: two DBs, one authority

The `.claude/telemetry` marker gained content in v0.3.0 — its **first line**
selects storage (`project` adds a project-local mirror; anything else, including
the empty file older versions wrote, means central-only). Every ambiguous case
(absent, oversized, undecodable, unrecognized) resolves to central, so a marker
written by an older version keeps its exact old behavior and a corrupted one
degrades to the mode that always works.

In project mode capture dual-writes, and the asymmetry is deliberate: the
central DB is authoritative because it alone owns `cursors`, so nothing about
the mirror can change what gets read from a transcript. `mirror_events()` runs
only *after* the central transaction has committed **and its connection is
closed** — opening a second DB while holding the central write lock is how one
slow mirror write would stall every peer capture on the machine. Mirror failures
are swallowed to `error.log` with a labelled context line; the mirror exists for
retention and portability and is never worth the authoritative write or the
session. It takes `BEGIN IMMEDIATE` for the same reason the central write does:
parallel firings share that one file, and a deferred transaction lets
`get_or_create`'s SELECT-then-INSERT race, with the loser's rows silently
dropped by the swallow-all rule. A **symlink at the mirror path is refused, not
resolved** — the path sits inside the repo and can therefore arrive committed,
which would aim SQLite's writes at any file on the machine.

## Mirror metadata is configured state, not a write receipt

v3's `projects.mirror_path` / `mirror_last_at` are stamped inside the **central**
transaction, before the mirror write is attempted, and stay stamped when it
fails. That is the point: a reader that inferred storage mode from a successful
mirror write would report project mode as central exactly when the mirror is
broken and needs attention. So the pair answers "a project-level copy is
configured, and this is the last captured event that was destined for it" —
never "the mirror is current". Only the file at `mirror_path` answers that, which
is why `/token-telemetry:storage-status` checks it separately.

Two gates follow from the same reasoning. The stamp is skipped on a turn with no
usage entries (`groups` empty, cursor-advance only) — there is no event
timestamp, and `latest_event_ts()` would otherwise invent `now()` for an event
that doesn't exist. And `stamp_mirror_meta()` no-ops when the v3 columns are
missing, i.e. on a DB whose v3 hop hasn't landed yet: metadata is never worth
failing a capture over. Mirror DBs never carry mirror metadata of their own —
the mirror is not itself mirrored. Clearing the pair when a project switches
back to central or opts out is command-side housekeeping (`enable`/`disable`),
not capture's job, so a hand-edited marker can leave a stale value behind.

## Sidecar enrichment: last-declared-task attribution

`read_sidecar()` reads `.claude/telemetry-context.json`, which the kit
rewrites whenever the agent switches tracker tasks. Any problem reading it —
absent, unreadable, malformed, or implausibly large (`SIDECAR_MAX_BYTES`) — is
a silent `None`; enrichment is never worth failing a capture over. The size
check runs first and cheapest, before any parse attempt, because a runaway
file is evidence it isn't agent-written context and parsing it would waste
time and memory on every single hook firing.

The sidecar's `issue_key` takes priority over `issue_key_from_git()`, which
falls back to parsing the leading tracker key off the last commit subject
(`ISSUE_KEY_RE`, anchored and colon-terminated so `feat:` or `WIP:` never
false-positive). This ordering exists because the sidecar reflects the task
*currently* in flight — accurate even mid-task before any commit lands —
while the commit-subject fallback is necessarily backward-looking to the last
commit. `sidecar_text()` further restricts sidecar values to scalars only:
sidecar files are agent-written JSON and could contain a dict or list, and
binding either into sqlite3 raises `ProgrammingError` — which would take
capture offline for as long as the bad file exists.

## Project name: a three-location ladder, first match wins

`project_name_from_kit()` reads the human project name from the kit's
`PROJECT-INFO.md` frontmatter (`project:` key). The kit document has moved
across its own versions, so the function checks a fixed ladder — `.marvin/`
(kit >=v0.21), then `.docs/` (kit v0.15-0.20), then `docs/` (kit <v0.15) — and
stops at the **first location whose file exists**. That file alone decides the
result: if its `project:` value is missing or an unresolved `{{PLACEHOLDER}}`,
the function returns `None` rather than falling through to the next rung, even
though a later location might hold a perfectly valid name. This keeps
resolution deterministic for a repo mid-migration between kit versions —
exactly one document is ever authoritative for a given capture, never a merge
of several. `stamp_project_name()` re-runs this resolution on every capture, so
a renamed or relocated `PROJECT-INFO.md` self-heals the stamped `projects.name`
on the next turn, with no migration step of its own.

## Pricing at query time, not capture time

`capture.py` never writes a cost column. It stamps raw token counts
(`in_tok`/`out_tok`/`cache_r`/`cache_w`) and lets the `pricing` table (seeded
at `effective_from=0`, superseded by dated rows as prices change) resolve cost
at query time — see [[pricing-and-cost]] for the rate-resolution rule
consumers use. This keeps a rate change a pure data insert: historical events
never need rewriting, and capture itself stays free of any pricing logic or
external dependency.
