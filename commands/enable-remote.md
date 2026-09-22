---
description: Turn on the remote (Supabase) telemetry backend for this machine — configure it, log in, ensure identity, then migrate or start fresh
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py":*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(cat:*), Bash(ls:*), Read, AskUserQuestion
---

Turn ON a **remote storage backend** so this machine's telemetry is written to a
shared database instead of only the local file. **Supabase is the only remote
option today** (more backends may follow). This is a machine-level choice: it
sets the central `active_backend`, separate from per-project capture enablement
(`/token-telemetry:enable`). All remote I/O runs through
`${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py`, which uses the TLS-verified
`SupabaseBackend` (no service_role, secrets header-only, bounded timeout). It
**reuses** the already-built + validated worker paths — this command adds no new
credential, TLS, or migration logic.

**Never ask for or accept a Supabase publishable key or a password *value* in
these prompts.** The publishable key is referenced by env-var NAME only (the user
`export`s the value in their shell). The login password travels only on **stdin**
to the worker, over TLS, and is never argv-passed, echoed, logged, or stored —
only the returned Auth tokens persist (mode 0600). A full name is PII: never echo
it into a commit, an issue, a URL, or a log.

### 1. Offer the storage choice

Ask the user where telemetry should be stored (AskUserQuestion, two options):

- **Local** (current default) — events stay in the local SQLite DB
  (`~/.claude/telemetry/usage.db`). Nothing changes; this command has nothing to
  do. Tell them and stop.
- **Remote (Supabase)** — events are written to a shared Supabase project. This
  is the only remote option for now; more backends may follow. Continue below.

### 2. Configure the remote (URL + key env-var NAME — no secret)

**a. Project URL.** Ask for the Supabase **project URL** (AskUserQuestion, free
text via Other, e.g. `https://<ref>.supabase.co`).

**b. Publishable-key env-var NAME.** Ask for the **name of the env var** that
holds the low-privilege publishable key. Offer the default
`TOKEN_TELEMETRY_SUPABASE_KEY` as the primary option. **Never ask for the key
value.** Then write the config (it stores only the URL and the NAME, mode 0600,
and does nothing if a block already exists):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" configure-remote --url "<URL>" --key-env "<ENV_NAME>"
```

`configured=yes …` means the block was written; `already_configured=yes …` means
one was already present and was left untouched (report the existing url/key_env).
Then tell the user to **export the key value in their shell** so the plugin can
read it at use time, e.g.:

```
export TOKEN_TELEMETRY_SUPABASE_KEY="<their publishable key>"
```

State plainly that the key value is never entered here and never stored in
settings — only its env-var name is.

### 3. Apply the schema + verify RLS (maintainer step)

The remote tables and Row-Level Security must exist before anything is written.
Direct the user (or the maintainer who runs the Supabase project) to, in order:

1. Apply `supabase/schema.sql`, then `supabase/reports.sql`, in the Supabase SQL
   editor (see the handbook **[[enabling-remote-telemetry]]** and the developer
   handbooks **rls-remote-schema** and **remote-read-parity**).
2. Run the **two-user + anon live RLS verification** those handbooks describe —
   the automated suite only checks structure, so isolation is proven by hand.

Then confirm the tables actually exist from this machine (this needs step 4's
login first if `session=no` — do step 4, then re-run):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" preflight
```

- **`schema_present=no`** with `missing_tables=…` → the schema has not been
  applied. **Stop** and have the maintainer apply it before continuing.
- **`reachable=no`** → the remote is unreachable. Stop and report.
- All `yes` → proceed.

### 4. Log in

Ask for the user's Supabase **email** and **password** (AskUserQuestion, free text
via Other). Pass them on **stdin** (never as arguments):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" login <<'JSON'
{"email": "<email>", "password": "<password>"}
JSON
```

`login_ok=yes` means the Auth session was stored (mode 0600) and the local
identity reconciled to the remote `auth.uid()`. On failure, report it and stop —
without a session nothing can be written.

### 5. Ensure an identity, then the remote users row

Every remote row is attributed to the current user, so a full name must be on
file. If `cat ~/.claude/telemetry/settings.json` shows no `full_name`, ask for the
user's **full name** (AskUserQuestion, free text via Other) and establish it:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" register-user --name "<full name>"
```

Then ensure the **remote `users` row** exists (so the first captured session/event
has a valid parent to reference — the explicit form of the write-path self-heal):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" sync-user
```

`users_row_synced=yes …` confirms it. (If you skip this, capture self-heals the
row on its first remote write anyway — this just does it now.)

### 6. Existing data: migrate now, or start fresh

Ask the user (AskUserQuestion) what to do with the telemetry already in their
local central DB:

- **Migrate it now** → hand off to **`/token-telemetry:migrate-to-remote`**, which
  does the count-validated upload and the collection flip. Do not re-implement it
  here; that command owns the verified copy-then-switch. Then you are done.
- **Start fresh** → leave the existing local data where it is and just flip
  collection to the remote:

  ```
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" set-backend --backend supabase
  ```

Either way, state plainly: the local `usage.db` is **kept** as a backup and stays
the authoritative **cursor** store and the **offline outbox** (events are retained
locally and re-sent if the remote is briefly unreachable). The switch is a pointer
flip, not a data move — it takes effect on the **next Claude Code session** (capture
hooks load at start), so restart to begin writing remotely.

### 7. Reversibility

The switch is always reversible — go back to local collection at any time:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remote_migrate.py" set-backend --backend local
```

### 8. Report

Tell the user: the remote is configured (URL + key env-var name, no secret
stored), whether they migrated existing data or started fresh, that collection is
now the remote (effective next session), that the local DB is kept as
backup/cursor-store/outbox, and that the switch is reversible. Point them at
`/token-telemetry:info` to check status.
