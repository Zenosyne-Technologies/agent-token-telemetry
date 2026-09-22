---
doc: RLS + remote schema
type: handbook
status: active
summary: The remote Postgres schema and Row-Level Security model applied to the maintainer's Supabase project — uniform own-rows-only owner-scoping and why (projects.path is sensitive), the default-deny + FORCE + auth.uid() policy pattern, natural-key references, the unique-key/idempotent-upsert design (NULLS NOT DISTINCT) that matches the writer's on_conflict, and the manual two-user live-verification the maintainer must run because the suite only checks structure.
keywords: [rls, row-level-security, supabase, postgres, auth.uid, owner_id, own-rows-only, default-deny, force-rls, on-conflict, nulls-not-distinct, unique-key, idempotent-upsert, schema, live-verification]
level: project
audience: developer
module: storage
sources: [supabase/schema.sql, scripts/supabase_backend.py, tests/test_schema_sql.py, docs/TELEMETRY-CONTRACT.md]
related: ["[[supabase-backend]]", "[[storage-backend]]", "[[identity-model]]"]
created: 2026-09-22
updated: 2026-09-22
---

# RLS + remote schema

`supabase/schema.sql` is the hand-written, reviewed Postgres schema the
**maintainer** applies to their own Supabase project. It is **not** run by the
client: the laptop-side backend (`[[supabase-backend]]`) treats the remote schema
as provisioned out-of-band — its `ensure_schema()` is a no-op and its
`schema_version()` is `None`. The **RLS policies in this file ARE the
authorization control**; there is no other server-side gate on who can read whose
rows.

## The model — uniform own-rows-only

Every table is readable and writable **only by its owner**. `users` is keyed by
its `uuid` (which equals the caller's `auth.uid()`); every other table carries
`owner_id uuid NOT NULL` and is gated by `auth.uid() = owner_id`.

The scoping is **uniform** — `projects`, `models` and `pricing` are owner-scoped
exactly like `sessions` and `events`, rather than being shared dimension/lookup
tables. This is deliberate and security-driven: **`projects.path` is a user's
local directory path** and must never be visible cross-user. A shared `projects`
table would leak one user's filesystem layout to every other user, so the safe,
simple choice is to scope *everything* to its owner and keep no shared surface at
all. `models` and `pricing` follow the same rule for consistency even though they
are less sensitive.

## The policy pattern — default-deny + FORCE + auth.uid()

Each of the six tables (`users`, `projects`, `models`, `pricing`, `sessions`,
`events`; **no `cursors`** — the read cursor is authoritative and stays local)
gets the identical treatment:

```sql
ALTER TABLE public.<t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.<t> FORCE  ROW LEVEL SECURITY;
CREATE POLICY <t>_owner_all ON public.<t>
  FOR ALL TO authenticated
  USING (auth.uid() = owner_id)          -- users: auth.uid() = uuid
  WITH CHECK (auth.uid() = owner_id);
```

Why each piece matters:

- **ENABLE + default-deny.** With RLS enabled and exactly one policy per table,
  any command not permitted by a policy is denied. There is no `anon`/`public`
  policy and no `USING (true)`, so an **unauthenticated request sees and writes
  nothing** — `auth.uid()` is null for the `anon` role, so `auth.uid() =
  owner_id` is never true.
- **FORCE.** RLS normally does not apply to the table owner; `FORCE` makes it
  apply even there, so a mistaken owner-context query cannot read across users
  (defense in depth).
- **`TO authenticated`.** The policy only ever runs for an authenticated caller.
  This is the second half of keeping `anon` out.
- **`USING` + `WITH CHECK`.** `USING` filters what an authenticated user can
  read/update/delete (their own rows); `WITH CHECK` rejects an insert/update that
  would set `owner_id` to anyone else — a user cannot write a row they would not
  be allowed to read.
- **Privilege floor beneath RLS.** `anon` and PUBLIC are `REVOKE`d and DML is
  `GRANT`ed to `authenticated` only, so an anon request is refused at the grant
  gate *before* RLS is even consulted. No **secret / bypass (service-role tier)**
  role is granted or required — normal capture never depends on a role that
  bypasses RLS.

## Natural-key references

Remote rows are addressed by **natural key**, never the local integer ids (which
are meaningless across databases). References are **composite** `(owner_id,
<natural key>)`:

- `sessions (owner_id, project_path)` → `projects (owner_id, path)`
- `events (owner_id, session_uuid)` → `sessions (owner_id, uuid)`
- `events (owner_id, model_name)` → `models (owner_id, name)`

Because the FK carries `owner_id` and `WITH CHECK` forces `owner_id =
auth.uid()`, a row can only ever reference **the owner's own** parent row. RLS
and referential integrity reinforce each other: an event cannot point at another
user's session even if a caller tried.

## Idempotent upsert — unique keys match the writer's on_conflict

Every table the client upserts carries a UNIQUE (or PRIMARY KEY) constraint whose
columns equal that writer's `on_conflict` target
(`supabase_backend.UPSERT_ON_CONFLICT`), so `Prefer: resolution=merge-duplicates`
is a real idempotent merge and a **re-drained outbox firing does not duplicate**:

| table | owner-scoped key = writer on_conflict |
|---|---|
| projects | `(owner_id, path)` |
| models | `(owner_id, name)` |
| sessions | `(owner_id, uuid)` |
| events | `(owner_id, session_uuid, model_name, ts, kind, agent, in_tok, out_tok, cache_r, cache_w, cache_w_1h, dur_ms, branch, commit_sha, issue_key, task_size, note, api_calls, ctx_tokens)` |
| pricing | `(owner_id, provider, model_prefix, model_version, effective_from)` |

The `events` key is the **full row identity** — the union of the contract's
mirror-dedupe tuple (which includes the model reference) and the v6 per-slice
metrics, prefixed by `owner_id`. It is the superset that can never false-merge two
genuinely distinct events. It is declared **`UNIQUE NULLS NOT DISTINCT`**
(Postgres 15+, which Supabase runs): several event columns are nullable (`agent`,
`dur_ms`, `branch`, `commit_sha`, `issue_key`, `task_size`, `note`,
`api_calls`, `ctx_tokens`), and under default SQL semantics two NULLs are
*distinct*, so a plain `UNIQUE` would let a re-sent row with a NULL slip through
as a duplicate. `NULLS NOT DISTINCT` collapses them.

`pricing` is owner-scoped and present for the future bulk migration; the
per-firing capture path does not write it (it upserts only projects, models,
sessions, events).

## What the tests check vs. what the maintainer must verify

`tests/test_schema_sql.py` parses `schema.sql` and asserts **structure only** —
every table enables/forces RLS, no policy is open to anon/public or uses
`USING (true)`, every policy is `TO authenticated` and references `auth.uid()`,
every upserted table has a matching unique key, and no bypass-key token appears.
Postgres RLS cannot run in this stdlib/SQLite suite, so these are a **tripwire,
not proof of enforcement**.

**Live RLS enforcement is verified by the maintainer** against a real
Supabase/Postgres instance. The suite cannot and does not prove that a user
truly cannot read another user's rows. Run this once after applying the schema:

1. **Apply the schema.** Paste `supabase/schema.sql` into the Supabase SQL editor
   (or `psql`) and run it. Re-running is safe — the DDL is guarded/idempotent.
2. **Create two users.** In Supabase Auth, create users A and B. Insert a
   `users` row for each (`uuid` = that user's `auth.uid()`), signed in as that
   user so RLS accepts the `WITH CHECK`.
3. **Seed owned rows.** Signed in as A, insert a `projects`/`sessions`/`events`
   chain with `owner_id = A`. Do the same as B with `owner_id = B`.
4. **Confirm isolation.** Signed in as A, `SELECT * FROM events` (and
   `projects`, `sessions`) — you must see **only A's rows**, never B's. Repeat as
   B for the mirror result. Confirm `projects.path` for B is invisible to A.
5. **Confirm cross-owner write is rejected.** Signed in as A, try to insert a row
   with `owner_id = B` — the `WITH CHECK` must reject it.
6. **Confirm anon reads nothing.** With only the publishable/anon key and **no**
   Auth session, `SELECT * FROM events` (and every table) must return **zero
   rows** (or be refused). No policy serves `anon`.

If any step leaks a row across owners, the policies are broken — do not enable the
remote backend until it is fixed. See the design memo
`.docs/researches/2026-09-22-external-db-supabase-design.md` (§5, §6c) and the
schema contract `docs/TELEMETRY-CONTRACT.md`.
