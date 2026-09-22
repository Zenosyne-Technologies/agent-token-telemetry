-- ===========================================================================
-- token-telemetry — remote Postgres schema + Row-Level Security (AOS-104 P7)
-- SECURITY-GATED: the RLS policies in this file ARE the authorization control.
-- ===========================================================================
--
-- This is the hand-written, reviewed schema the MAINTAINER applies to their own
-- Supabase (Postgres) project — it is NOT run by the client. The laptop-side
-- capture backend (`scripts/supabase_backend.py`) treats the remote schema as
-- provisioned out-of-band: its `ensure_schema()` is a deliberate no-op and its
-- `schema_version()` returns None. Apply this file once (Supabase SQL editor, or
-- `psql`), then re-apply is safe — every statement is guarded / idempotent.
--
-- POLICY MODEL — own-rows-only, uniformly owner-scoped
-- ---------------------------------------------------------------------------
-- Every table is readable and writable ONLY by its owner. `users` is keyed by
-- its `uuid` (== auth.uid()); every other table carries `owner_id uuid NOT NULL`
-- and is gated by `auth.uid() = owner_id`. The model is uniform (no shared
-- dimension tables) on purpose: `projects.path` is a user's local directory
-- path and must never be visible cross-user, so models/pricing/projects are
-- owner-scoped exactly like sessions and events rather than shared lookups.
--
-- Defense in depth, applied to EVERY table below:
--   * ENABLE + FORCE ROW LEVEL SECURITY (FORCE = RLS applies even to the table
--     owner, so a mistaken owner-context query cannot read across users).
--   * DEFAULT-DENY: exactly one policy per table, scoped `TO authenticated`.
--     No policy exists for `anon`/`public`, so an unauthenticated request — for
--     which `auth.uid()` is null and which maps to the `anon` role — sees and
--     writes nothing. There is no `USING (true)` anywhere.
--   * Privilege floor beneath RLS: DML is granted to `authenticated` only;
--     `anon` and PUBLIC are revoked, so an anon request is refused at the
--     grant gate before RLS is even consulted.
--   * NO secret / bypass-key (the service-role tier) is referenced or required.
--     Normal capture NEVER depends on a role that bypasses RLS.
--
-- NATURAL KEYS — remote rows are addressed by natural key, never the local
-- integer ids (which are meaningless across databases). Foreign references are
-- composite `(owner_id, <natural key>)`, so a row can only ever reference the
-- OWNER's own parent row — RLS and referential integrity reinforce each other.
--
-- IDEMPOTENT UPSERT — every table the client upserts carries a UNIQUE
-- constraint matching that writer's `on_conflict` target, so a re-drained outbox
-- firing MERGES instead of duplicating (fixes the P6 duplicate-events finding).
-- The `events` uniqueness is declared `NULLS NOT DISTINCT` (Postgres 15+, which
-- Supabase runs) so that rows whose nullable columns (agent, dur_ms, branch, …)
-- are NULL still collapse on re-send — under default SQL semantics two NULLs are
-- distinct and the merge would silently fail.
--
-- LIVE ENFORCEMENT IS VERIFIED BY THE MAINTAINER. The client test-suite can only
-- assert the STRUCTURE of this file (see tests/test_schema_sql.py). That a user
-- truly cannot read another user's rows, and that an anon request reads nothing,
-- must be confirmed against a real Postgres/Supabase instance — see the
-- developer handbook `rls-remote-schema.md` for the two-user verification steps.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Tables (types mapped from the SQLite v7 shape; cursors are intentionally
-- ABSENT — the read cursor is authoritative and stays LOCAL, never remote).
-- ---------------------------------------------------------------------------

-- users: one row per human, uuid-addressed. The uuid IS the identity and equals
-- the Supabase Auth auth.uid() for that user (hybrid identity: minted locally,
-- reconciled to auth.uid() on first remote login). name is PII (the full name).
CREATE TABLE IF NOT EXISTS public.users (
  uuid       uuid   PRIMARY KEY,
  name       text   NOT NULL,
  created_at bigint NOT NULL
);

-- projects: owner-scoped. path is a LOCAL directory path (sensitive — the reason
-- the whole model is owner-scoped). name/mirror_* mirror the local v3/v5 columns
-- so the remote stays schema-equivalent; only owner_id + path are written by the
-- per-firing capture path (name/mirror_* are populated by the bulk migration).
CREATE TABLE IF NOT EXISTS public.projects (
  owner_id       uuid   NOT NULL REFERENCES public.users(uuid) ON DELETE CASCADE,
  path           text   NOT NULL,
  name           text,
  mirror_path    text,
  mirror_last_at bigint,
  -- UNIQUE(owner_id, path) — matches the writer's projects on_conflict target.
  PRIMARY KEY (owner_id, path)
);

-- models: owner-scoped lookup of model names. Owner-scoping (rather than a shared
-- table) keeps the model uniform and avoids any cross-user shared surface.
CREATE TABLE IF NOT EXISTS public.models (
  owner_id uuid NOT NULL REFERENCES public.users(uuid) ON DELETE CASCADE,
  name     text NOT NULL,
  -- UNIQUE(owner_id, name) — matches the writer's models on_conflict target.
  PRIMARY KEY (owner_id, name)
);

-- pricing: owner-scoped copy of the per-user pricing ladder (each user owns their
-- own rates; there is no shared pricing surface, by the uniform-owner-scoping
-- rule). Not written by the per-firing capture path — populated by the bulk
-- central->remote migration (P8). UNIQUE matches the local pricing unique key.
CREATE TABLE IF NOT EXISTS public.pricing (
  owner_id       uuid   NOT NULL REFERENCES public.users(uuid) ON DELETE CASCADE,
  provider       text   NOT NULL,
  model_prefix   text   NOT NULL,
  model_version  text   NOT NULL DEFAULT '',
  in_usd         double precision,
  out_usd        double precision,
  cache_r_usd    double precision,
  cache_w_usd    double precision,
  cache_w_1h_usd double precision,
  effective_from bigint NOT NULL,
  source         text,
  UNIQUE (owner_id, provider, model_prefix, model_version, effective_from)
);

-- sessions: owner-scoped. uuid is the Claude Code session id (text, not the user
-- uuid). References its project by the composite natural key (owner_id, path), so
-- a session can only ever belong to one of the OWNER's own projects.
CREATE TABLE IF NOT EXISTS public.sessions (
  owner_id     uuid NOT NULL REFERENCES public.users(uuid) ON DELETE CASCADE,
  uuid         text NOT NULL,
  project_path text NOT NULL,
  -- UNIQUE(owner_id, uuid) — matches the writer's sessions on_conflict target.
  PRIMARY KEY (owner_id, uuid),
  FOREIGN KEY (owner_id, project_path)
    REFERENCES public.projects (owner_id, path) ON DELETE CASCADE
);

-- events: owner-scoped, the hot high-row table. Columns map the SQLite v7 events
-- shape 1:1 (token counts, cache split, per-slice metrics), with the local
-- integer FKs replaced by natural keys: session_uuid -> sessions, model_name ->
-- models. owner_id is carried denormalized on every event so RLS on events is a
-- flat `auth.uid() = owner_id` with no join.
--
-- The UNIQUE constraint below is the event's full row identity (owner + session +
-- model + every derived value column) and is the writer's events on_conflict
-- target: a re-sent identical firing merges (idempotent), while two genuinely
-- distinct events never collide. NULLS NOT DISTINCT so nullable columns still
-- dedupe on re-send. This is the union of the contract's mirror-dedupe tuple
-- (which includes the model reference) and the v6 per-slice metrics — the
-- superset that can never false-merge two distinct rows.
CREATE TABLE IF NOT EXISTS public.events (
  owner_id     uuid   NOT NULL REFERENCES public.users(uuid) ON DELETE CASCADE,
  session_uuid text   NOT NULL,
  model_name   text   NOT NULL,
  ts           bigint NOT NULL,
  kind         bigint NOT NULL,
  agent        text,
  in_tok       bigint NOT NULL DEFAULT 0,
  out_tok      bigint NOT NULL DEFAULT 0,
  cache_r      bigint NOT NULL DEFAULT 0,
  cache_w      bigint NOT NULL DEFAULT 0,
  cache_w_1h   bigint NOT NULL DEFAULT 0,
  dur_ms       bigint,
  branch       text,
  commit_sha   text,
  issue_key    text,
  task_size    text,
  note         text,
  api_calls    bigint,
  ctx_tokens   bigint,
  FOREIGN KEY (owner_id, session_uuid)
    REFERENCES public.sessions (owner_id, uuid) ON DELETE CASCADE,
  FOREIGN KEY (owner_id, model_name)
    REFERENCES public.models (owner_id, name) ON DELETE CASCADE,
  UNIQUE NULLS NOT DISTINCT (
    owner_id, session_uuid, model_name, ts, kind, agent,
    in_tok, out_tok, cache_r, cache_w, cache_w_1h, dur_ms,
    branch, commit_sha, issue_key, task_size, note, api_calls, ctx_tokens)
);

-- Supporting indexes for the composite FKs whose columns are not already a
-- leading prefix of a PK/UNIQUE index (so ON DELETE CASCADE and future report
-- reads do not table-scan). Not uniqueness — plain lookup indexes.
CREATE INDEX IF NOT EXISTS idx_sessions_owner_project
  ON public.sessions (owner_id, project_path);
CREATE INDEX IF NOT EXISTS idx_events_owner_model
  ON public.events (owner_id, model_name);
CREATE INDEX IF NOT EXISTS idx_events_owner_ts
  ON public.events (owner_id, ts);

-- ---------------------------------------------------------------------------
-- Privilege floor (beneath RLS): only `authenticated` may touch these tables at
-- all; `anon` and PUBLIC are revoked outright, so an unauthenticated request is
-- refused before RLS is consulted. The secret / bypass (service-role tier) role
-- is never granted here.
-- ---------------------------------------------------------------------------
REVOKE ALL ON public.users, public.projects, public.models,
              public.pricing, public.sessions, public.events
  FROM PUBLIC, anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.users, public.projects, public.models,
  public.pricing, public.sessions, public.events
  TO authenticated;

-- ---------------------------------------------------------------------------
-- Row-Level Security — ENABLE + FORCE + one default-deny policy per table.
-- Each policy is scoped TO authenticated and gated on auth.uid(); no policy is
-- ever granted to anon/public and none uses USING (true).
-- ---------------------------------------------------------------------------

-- users: the row whose uuid is the caller's own auth.uid().
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_owner_all ON public.users
  FOR ALL
  TO authenticated
  USING (auth.uid() = uuid)
  WITH CHECK (auth.uid() = uuid);

-- projects: own rows only.
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects FORCE ROW LEVEL SECURITY;
CREATE POLICY projects_owner_all ON public.projects
  FOR ALL
  TO authenticated
  USING (auth.uid() = owner_id)
  WITH CHECK (auth.uid() = owner_id);

-- models: own rows only.
ALTER TABLE public.models ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.models FORCE ROW LEVEL SECURITY;
CREATE POLICY models_owner_all ON public.models
  FOR ALL
  TO authenticated
  USING (auth.uid() = owner_id)
  WITH CHECK (auth.uid() = owner_id);

-- pricing: own rows only.
ALTER TABLE public.pricing ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pricing FORCE ROW LEVEL SECURITY;
CREATE POLICY pricing_owner_all ON public.pricing
  FOR ALL
  TO authenticated
  USING (auth.uid() = owner_id)
  WITH CHECK (auth.uid() = owner_id);

-- sessions: own rows only.
ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY sessions_owner_all ON public.sessions
  FOR ALL
  TO authenticated
  USING (auth.uid() = owner_id)
  WITH CHECK (auth.uid() = owner_id);

-- events: own rows only.
ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.events FORCE ROW LEVEL SECURITY;
CREATE POLICY events_owner_all ON public.events
  FOR ALL
  TO authenticated
  USING (auth.uid() = owner_id)
  WITH CHECK (auth.uid() = owner_id);
