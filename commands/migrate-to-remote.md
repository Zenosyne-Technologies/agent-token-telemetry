---
description: Migrate the central telemetry DB to the remote (Supabase) host and switch collection to it
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py":*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(cat:*), Bash(ls:*), Read, AskUserQuestion
---

Upload the **whole central telemetry DB** to the maintainer's remote Supabase
project and switch active collection to it. This is the network, security-gated
migration: **central → remote**. All remote I/O runs through
`${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py`, which uses the TLS-verified
`SupabaseBackend` (no service_role, secrets header-only, bounded timeout). It is
**copy-then-switch**: the collection pointer is flipped **only after** a verified
full upload, and the flip is **reversible**. Re-running is always safe — every
remote write is an idempotent upsert.

Never echo the user's password or full name into a commit, an issue, a URL, or a
log — the password travels only in the login request body over TLS and is never
persisted; the name is PII.

### 1. Preconditions

The remote must already be **configured** (a `supabase` block with the project
URL in `~/.claude/telemetry/settings.json`, and the publishable-key env var set)
and its **schema applied** (`supabase/schema.sql` run once in the Supabase SQL
editor by the maintainer). If the remote is not configured, stop and say so —
configuring it is a separate step.

### 2. Authenticate

Ask the user for their Supabase **email** and **password** (AskUserQuestion, free
text via Other; ask for the password only if you do not already hold it this
turn). Then log in, passing the credentials on **stdin** (never as arguments, so
they cannot land in a process listing):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" login <<'JSON'
{"email": "<email>", "password": "<password>"}
JSON
```

`login_ok=yes` means the Auth session was stored (mode 0600) and the local
identity reconciled to the remote `auth.uid()`. On failure, report it and stop —
without a session nothing uploads.

### 3. Verify the remote is ready

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" preflight
```

Read its `key=value` lines. The remote has no schema-version pragma, so readiness
is probed by counting each expected table:

- **`schema_present=no`** with a `missing_tables=…` line → `supabase/schema.sql`
  has **not** been applied to the remote. **Stop here** and tell the maintainer
  to apply it first — do not upload into a half-provisioned remote.
- **`reachable=no`** → the remote is unreachable. Stop and report.
- **`session=no`** → login did not take; go back to step 2.
- All `yes` → proceed.

### 4. Show what will move

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" local-counts
```

Report the per-table local totals so the user sees the volume before uploading.

### 5. Ensure an identity exists

The upload attributes every row to the current user. If step 2 reported the
identity was newly reconciled that is enough; but if no local identity/full name
is on file at all (`cat ~/.claude/telemetry/settings.json` shows no `full_name`),
ask the user for their **full name** (AskUserQuestion, free text via Other) and
establish it first:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" register-user --name "<full name>"
```

### 6. Migrate (upload + count-validate — no flip yet)

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" migrate
```

This syncs the `users` row **first** (so remote foreign keys resolve), then
uploads projects → models → pricing → sessions → events in FK order, stamping the
owner id on every row and deriving event natural keys from the local joins.
**Cursors are never uploaded.** After the upload it validates that remote row
counts equal local counts per table and prints `counts_match=yes|no`.

- **`counts_match=yes`** (exit 0) → the upload is verified; go to step 7.
- **`upload_ok=no`** or **`counts_match=no`** (non-zero exit) → **stop**. The
  pointer is **not** flipped, the local DB stays authoritative, and the partial
  remote rows are harmless (a later re-run upserts over them). Report the reason
  and offer to re-run.

### 7. Keep the local DB?

Ask plainly whether to **keep the local central DB**. Default is **keep** — the
remote is new and unproven, and the local file is your rollback (it also stays
the durable cursor store and offline outbox). This command **never deletes** the
central DB; only the user removes it, and only much later once the remote is
trusted.

### 8. Switch collection (the flip) — only after step 6 passed

State plainly:

> Capture will now write to the **remote**. The local `usage.db` becomes
> backup-only for events — but it stays the authoritative **cursor** store and the
> **offline outbox** (events are retained locally and re-sent if the remote is
> briefly unreachable). This is a pointer flip, not a data move: it is atomic and
> reversible.

Then flip:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" set-backend --backend supabase
```

This writes `active_backend=supabase` to `settings.json` (mode 0600). It takes
effect on the next Claude Code session (capture hooks load at start). To roll
back at any time, flip it back:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" set-backend --backend local
```

### 9. Report

The per-table counts that moved and that they matched, whether the local DB was
kept, that collection is now the remote (or was left local if you did not flip),
and that the switch is reversible and the whole command re-runnable.
