---
title: Capture Pipeline
audience: developer
module: capture
sources: [scripts/capture.py, hooks/hooks.json]
updated: 2026-08-05
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
matches. A DB stamped v2 without the v2 columns — an older build that stamped
too early, or a hand-edited/restored file — would otherwise fail every future
capture forever, since the fast-path check would keep skipping migration. The
fix is a post-condition, not a version check: after attempting the `ALTER
TABLE` statements (each individually idempotent, not wrapped in one shared
transaction, so a migrating process never blocks a peer's capture), the code
re-reads `event_columns(conn)` and only stamps `user_version=2` if the columns
are actually present. If they aren't, the function returns without stamping —
the next connect() just retries the migration. The same logic applies to
seeding the `pricing` table: `CREATE TABLE` autocommits, so a crash between
table creation and the seed `INSERT` can leave an empty table that must still
get seeded on the next attempt; the code gates on the presence of seed rows
(`SELECT 1 FROM pricing WHERE source=?`), not on the table's existence.

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

## Pricing at query time, not capture time

`capture.py` never writes a cost column. It stamps raw token counts
(`in_tok`/`out_tok`/`cache_r`/`cache_w`) and lets the `pricing` table (seeded
at `effective_from=0`, superseded by dated rows as prices change) resolve cost
at query time — see [[pricing-and-cost]] for the rate-resolution rule
consumers use. This keeps a rate change a pure data insert: historical events
never need rewriting, and capture itself stays free of any pricing logic or
external dependency.
