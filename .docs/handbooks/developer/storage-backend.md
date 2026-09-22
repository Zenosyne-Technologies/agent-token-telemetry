---
doc: Storage Backend
type: handbook
status: active
summary: The StorageBackend seam — the abstract interface capture's write path and report's read path call instead of sqlite3 directly, LocalSqliteBackend as today's only implementation, and how a remote backend (Supabase, later) plugs in by implementing the same methods while the read path stays SQL-coupled for now.
keywords: [storage-backend, localsqlitebackend, seam, open_ro, write_events, cursors, capabilities, refactor, supabase]
level: project
audience: developer
module: storage
sources: [scripts/storage.py, scripts/capture.py, scripts/report.py, docs/TELEMETRY-CONTRACT.md]
related: ["[[capture-pipeline]]", "[[identity-model]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Storage Backend

`scripts/storage.py` holds the seam between telemetry's logic and where its data
physically lives. Capture and reports call **a backend**, not `sqlite3`. Today
there is exactly one backend — local SQLite — and the seam changes nothing about
behaviour; its whole point is that a *second* backend (a server-hosted DB such as
Supabase, a later phase) can be dropped in behind the same interface.

## `StorageBackend`

An abstract base class whose method set is exactly the union of what the code
does with storage **today** — no speculative remote-only methods:

| method | what it is today |
|---|---|
| `open()` / `close()` | acquire / release a read-write session (`capture.connect`) |
| `open_ro()` | a read-only connection to the store, or `None` when absent — the former `report.open_ro()` |
| `schema_version()` / `ensure_schema()` | `PRAGMA user_version` / run the migration ladder (`capture.migrate`) |
| `write_events(...)` | one firing's aggregated event rows (`capture.insert_events`); returns the session id |
| `cursor_get()` / `cursor_set()` | read / upsert a transcript's read offset |
| `capabilities()` | honest feature flags (a `Caps` dataclass) the callers can branch on |

`Caps` reports, for the local backend: `server_side_aggregation=True` (the report
SQL runs in-process), `owns_cursors=True` (cursor authority is local),
`multi_user=False` (no per-user isolation), `supports_upsert=True`,
`writable=True`.

## `LocalSqliteBackend`

Today's only implementation. It wraps a single `usage.db`-family file — the
central DB, a project mirror, or an export — and every method delegates to the
existing, proven `capture` functions, so the bytes written and read are
identical to calling those functions directly. It is a *home* for code that
already existed, not a rewrite.

Where the seam is wired in:

- **Central write path** (`capture.main`): opens a `LocalSqliteBackend`, drives
  the same `BEGIN IMMEDIATE` transaction over its connection, reads the cursor
  through `cursor_get`, and closes through the backend. The lock ordering,
  cursor authority, and the never-break-a-session `sys.exit(0)` are unchanged.
- **Mirror write** (`capture.mirror_events`): the project-local mirror is simply
  a *second* `LocalSqliteBackend` on the mirror file — same seam, opened after
  the central commit and outside the central lock, exactly as before.
- **Reports** (`report.open_ro`): obtain their read-only connection from
  `LocalSqliteBackend(db).open_ro()`.

## Read path is still SQL-coupled

Reports and the dashboard embed SQLite-dialect SQL (CTEs, `strftime`,
`LIKE`-prefix pricing joins). For now the read path keeps that SQL and merely
obtains its connection through the seam — a REST/remote backend cannot execute
arbitrary SQL, so a **named, backend-neutral read abstraction** (`read_for_report`)
is deliberately *not* introduced here. It arrives with the remote read-parity
phase, when a non-SQL backend actually needs it. `scripts/dashboard.py` still
uses its own `open_ro` and is a follow-up for that same phase.

## Adding a remote backend

Implement `StorageBackend`: `write_events` POSTs a firing's rows; `open_ro`
returns whatever the read parity phase decides; `capabilities()` reports the
truth (e.g. `owns_cursors=False`, `multi_user=True`). Cursors stay authoritative
and local regardless of where events go. Nothing in capture's transaction
orchestration or the report SQL has to change to add the class — that is the
value the seam buys.
