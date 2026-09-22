---
doc: Remote Migration (Command B)
type: handbook
status: active
summary: The central→remote migration path — the remote_migrate worker and its command, sync-users-first, the local→remote owner_id stamping and natural-key transformation, the count-validated copy-then-switch pointer flip and its rollback, remote schema detection without a version pragma, and the write-path self-heal of the users row.
keywords: [migrate, remote, supabase, owner_id, sync-users, count-validation, pointer-flip, rollback, self-heal, postgrest, upsert]
level: project
audience: developer
module: storage
sources: [scripts/remote_migrate.py, scripts/supabase_backend.py, commands/migrate-to-remote.md, supabase/schema.sql, scripts/settings.py]
related: ["[[supabase-backend]]", "[[rls-remote-schema]]", "[[migration-commands]]", "[[identity-model]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Remote Migration (Command B)

Command B uploads the entire central SQLite DB to the remote Supabase project and
switches collection to it. It follows the `storage-*` command shape: the
interactive UX lives in `commands/migrate-to-remote.md`, and every deterministic
step — every remote request and every SQL read — lives in
`scripts/remote_migrate.py`, which drives `SupabaseBackend`. All remote I/O goes
through that one TLS-verified, bounded-timeout, header-only-secret,
no-service_role transport; no new HTTP path is introduced.

## The command / worker split

The command prompts and sequences; the worker does. Its subcommands split
check-from-do so the pointer is never flipped before a verified upload:

| subcommand | does |
|---|---|
| `login` | reads `{email,password}` from **stdin** (never argv), calls `SupabaseBackend.login`, persists only the returned tokens (0600), reconciles identity to `auth.uid()` |
| `preflight` | reports `configured` / `identity_set` / `session` / `reachable` / `schema_present` (+ `missing_tables`) |
| `local-counts` | local per-table totals, no network |
| `migrate` | the do: sync users first, FK-ordered chunked owner-stamped upload, then remote-vs-local count validation; prints `counts_match` — **never flips** |
| `set-backend` | flips `active_backend` to `supabase` (forward) or `local` (rollback), 0600 |

## sync-users FIRST

`_sync_users` upserts the migrating user's `users` row before any FK-referencing
row (req 14), because every other remote table references `users(uuid)`. Two
constraints shape it:

- **RLS.** The `users` policy is `WITH CHECK (auth.uid() = uuid)`, so the caller
  may only write a row whose `uuid` equals their own `auth.uid()`. "Upsert every
  local users row" therefore collapses, in practice, to syncing the **current
  owner's** row keyed by the auth uid — other identities in a local `users` table
  cannot be written remotely and are not needed (all data is stamped to the
  current owner anyway).
- **Hybrid identity.** The remote row's `uuid` is the reconciled `auth.uid()`
  (the `owner` every data row is stamped with), while its `name`/`created_at` come
  from the **local** `users` row (keyed by the local uuid) or, failing that,
  settings + now. See [[identity-model]].

## The local→remote transformation

The local schema carries `owner_id` only on `sessions`; the remote schema
requires it on projects/models/pricing/sessions/events. `_read_upload_rows` is
where the shapes are bridged:

- **owner stamping.** Every owner-scoped row is stamped with `owner =
  SupabaseBackend.remote_owner_id()` (the reconciled `auth.uid()` after login).
  The migrating user owns all their local data, so this is uniform.
- **retro-link first.** `migrate_lib.retro_link` fills any `NULL`
  `sessions.owner_id` locally before the read, keeping the local store
  consistent (the upload stamps the remote owner regardless).
- **natural keys, not local ids.** Local integer ids are meaningless across
  databases, so `sessions` carry `project_path` (from the projects join) and
  `events` carry `session_uuid` + `model_name` (from the sessions/models joins) —
  matching the remote foreign references.
- **cursors are never read.** They are authoritative and stay local (the one hard
  invariant); the upload never touches them.

Upload order is `users → projects → models → pricing → sessions → events`, each a
chunked (`MIGRATE_CHUNK`) `merge-duplicates` upsert on the owner-scoped unique key
from `supabase_backend.UPSERT_ON_CONFLICT` — so a resumed or re-run migration
converges with no duplicates.

## Remote compatibility detection without a version pragma

The remote is Postgres/PostgREST — there is no `PRAGMA user_version`. `preflight`
is pragmatic and honest: it probes each expected table with an owner-scoped
`count=exact` request. A `404` on a table means the schema (`supabase/schema.sql`)
has not been applied (`schema_present=no`, with `missing_tables=…`); a transport
error means `reachable=no`. This is presence detection, not a structural diff —
the remote schema is provisioned out-of-band by the maintainer.

## Count-validated copy-then-switch, and rollback

The migration is copy-then-switch and the flip is gated:

1. `migrate` uploads, then reads remote row counts (`SupabaseBackend.count_rows`,
   which parses the `Content-Range` total and, under RLS, counts only the owner's
   rows) and compares them to local counts for the five data tables. It prints
   `counts_match=yes|no` and exits non-zero on any mismatch or upload failure.
   **It never flips the pointer itself.**
2. The command flips only on `counts_match=yes`, via `set-backend --backend
   supabase` → `settings.set_active_backend` (0600). The flip is a pointer move,
   atomic, and **reversible** (`--backend local`).

On any upload failure the migration raises out of the chunked upsert (these
migration methods deliberately raise, unlike the never-break-a-session write
path), the worker returns non-zero, the pointer is left unflipped, and the local
DB stays authoritative — the partial remote rows are harmless because the next
run upserts over them. The operation is audited locally in `audit_log`.

## Write-path self-heal of the users row

Separately from the migration, `SupabaseBackend._ensure_user_row` (called at the
top of `_push`) upserts the current user's `users` row before the first
FK-referencing push, once per process (`_user_synced`), with
`resolution=ignore-duplicates` so a real `created_at` from a migration is never
clobbered. This closes the P7 finding for the fresh-enable path: a user who
switched to the remote backend without running a full migration still gets a
valid parent row, so their events do not loop in the outbox on the `users`
foreign key. It is cheap and never breaks a session — a failure routes the firing
to the outbox like any other, and the latch stays unset so the next firing
retries. When no full name is on file the row cannot satisfy `users.name NOT
NULL`, so it no-ops.
