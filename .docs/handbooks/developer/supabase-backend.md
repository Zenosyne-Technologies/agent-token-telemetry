---
doc: Supabase backend
type: handbook
status: active
summary: The Supabase remote write backend — per-user Auth JWT + RLS on auth.uid(), the publishable key read from env by name and the Auth token in a mode-0600 file, the PostgREST upsert write path with certificate-verified TLS, the offline outbox that keeps a session from ever breaking on a remote failure, and how identity reconciles the local uuid to auth.uid(). RLS policies and remote reads are separate later phases.
keywords: [supabase, postgrest, gotrue, auth-jwt, rls, publishable-key, outbox, tls, urllib, remote-backend, owner_id, credentials]
level: project
audience: developer
module: storage
sources: [scripts/supabase_backend.py, scripts/settings.py, scripts/capture.py, scripts/storage.py, docs/TELEMETRY-CONTRACT.md]
related: ["[[storage-backend]]", "[[identity-model]]", "[[capture-pipeline]]", "[[rls-remote-schema]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Supabase backend

`scripts/supabase_backend.py` is the first remote `StorageBackend` — a
write-only sibling of `LocalSqliteBackend` that pushes one firing's events to a
Supabase project over its REST API. It exists behind the same seam
(`[[storage-backend]]`), adds **zero dependencies** (stdlib `urllib` + default
TLS only), and obeys capture's iron rule: **a remote failure never breaks a
session.** RLS policies + the remote schema (P7) and remote reads (P9) are
separate phases; this backend is honest that it does neither.

## Auth model — per-user Auth JWT + RLS, never a bypass key

Writes authenticate as the **user**, not as the project:

- **Transport key** — the low-privilege **publishable** (anon-tier) key, sent in
  the `apikey` header. It cannot isolate users on its own.
- **Identity** — a per-user **Supabase Auth (GoTrue) JWT**, sent as
  `Authorization: Bearer <access_token>`. Row-Level Security keys on
  `auth.uid()`, so each user reads and writes only their **own** rows.

Why not anon-only: an unauthenticated request maps to the `anon` role and
`auth.uid()` is null, so `owner_id = auth.uid()` cannot gate rows — a shared key
would expose everyone's data. Why never the **secret / bypass key (the
service-role tier)**: it carries `BYPASSRLS` and would defeat isolation entirely;
a CLI runs on users' laptops, which is exactly where such a key must never live.
The backend never reads, stores, or supports it — there is no config path to it.

Grant choice (flagged): `login(email, password)` uses the OAuth2 **password
grant** (`POST /auth/v1/token?grant_type=password`) because a first-party CLI has
no browser for a redirect flow. Credentials travel in the request **body** over
verified TLS, never in the URL, and are never stored — only the returned tokens
are. `login` is the token-acquisition entry the interactive login command (P11)
will call; this phase never prompts.

## Credential storage

| Credential | Where | Sensitivity |
|---|---|---|
| Publishable key | env var whose **NAME** lives in `settings.json` (`supabase.publishable_key_env`, default `TOKEN_TELEMETRY_SUPABASE_KEY`); the **value** is read from env at use time | low-priv transport, never persisted |
| Auth access/refresh token | mode-0600 `credentials.json` beside the usage DB (never a repo) | sensitive — the only on-disk secret |

Session read precedence is **env → keychain → 0600 file** (`TOKEN_TELEMETRY_SUPABASE_SESSION`
can inject a token for CI; the keychain hook is a documented extension point that
defaults to the file). No secret ever enters a URL, a query string, a log, or a
commit — auth is header-based by construction, and the one network call site
(`_request`) permits only `https://` with a **certificate-verified default TLS
context** (`ssl.create_default_context`; verification is never disabled).

## Write path, outbox, and never breaking a session

`write_events` runs **outside every local lock, after the central commit** — the
same discipline as the project mirror. Per firing it:

1. re-sends any backlog (the outbox, below), then
2. upserts `projects`, `models`, `sessions` **and** `events` in FK order with
   `Prefer: resolution=merge-duplicates`, so every step is idempotent.

Every table carries `owner_id` and every upsert's `on_conflict` is the
**owner-scoped** unique key from `supabase/schema.sql`
(`supabase_backend.UPSERT_ON_CONFLICT`): `(owner_id, path)`, `(owner_id, name)`,
`(owner_id, uuid)`, and for `events` the full-row-identity key
(`EVENTS_ON_CONFLICT`). A re-drained outbox firing therefore **merges rather than
duplicating** — including events, which the remote schema keys `NULLS NOT
DISTINCT` so nullable columns still dedupe. Rows are addressed by **natural key**
(path / name / uuid), not the local integer ids, which are meaningless across
databases; the composite `(owner_id, natural-key)` foreign references are
resolved by the remote schema (`[[rls-remote-schema]]`). Event values come from
`capture.derive_event_fields` — the **one** derivation shared with the local
SQLite insert — so the two write paths cannot drift.

On **any** remote error or timeout the firing is swallowed-and-logged and
retained in a local **offline outbox** (`outbox.db`, a local queue), then
re-sent oldest-first on a later firing; draining stops at the first failure so a
persistent outage costs one bounded timeout, not one per queued firing. A
missing publishable key or absent Auth session simply routes to the outbox — no
network call, no raised error. `write_events` never raises, so capture always
reaches its unconditional `sys.exit(0)`.

**Cursors are never remote.** The local SQLite DB stays the authoritative cursor
store and the durable outbox (memo §4c); `SupabaseBackend` owns no cursors
(`owns_cursors=False`) and its `cursor_*` methods refuse.

## Identity reconcile

The local uuid (`[[identity-model]]`) is minted offline and independent of
Supabase. On first successful `login`, `settings.record_auth_identity` reconciles
it with the Auth `auth.uid()`: the local uuid is preserved and `user.auth_uid` is
recorded when they differ (hybrid identity). Remote rows then stamp `owner_id` =
the Auth uid — what RLS matches — while local rows keep the original uuid.

## How capture reaches it (guarded write-through)

`storage.remote_backend_if_active(settings)` returns a ready backend **only** when
`active_backend == "supabase"` and its config is present, else `None`. Capture
calls it after the mirror block; on the default `local` backend it is a single
dict lookup that returns `None`, so the local write path is byte-for-byte
unchanged. This is an additive write-through for P6, wrapped in its own
try/except as defense in depth. The real `active_backend` **dispatch** — routing
the sole authoritative write to the remote and flipping the pointer — is P8.

## What lives in later phases

RLS policies + the remote Postgres schema (P7) are delivered — see
`[[rls-remote-schema]]` for the own-rows-only policy model, the schema, and the
maintainer's live-verification steps. Remote reads (`read_for_report`,
`/token-stats` over the remote) are **P9**; the interactive login/enable UX and
the install "remote" option are **P11**. See
`docs/TELEMETRY-CONTRACT.md` and the design memo
`.docs/researches/2026-09-22-external-db-supabase-design.md` (§1, §6, §9).
