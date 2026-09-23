-- ===========================================================================
-- token-telemetry — remote read parity: server-side report aggregation (P9)
-- SECURITY-GATED. This file is the Postgres HALF of a DUAL-DIALECT contract.
-- ===========================================================================
--
-- The client renders `/token-stats`, `/project-stats` and `/info` from the exact
-- same aggregations whether the data lives in the local SQLite store or the
-- remote Supabase (Postgres) store. `scripts/report.py` is the REFERENCE dialect
-- (SQLite); the views + functions below reproduce its pricing resolution, its time
-- windows, its tier mapping and its cost math in Postgres, returning `jsonb` the
-- client maps 1:1 into the same Python shape `report.py`'s `fetch_*` returns.
--
-- The two dialects MUST be kept in lockstep. `tests/test_report_parity.py` is the
-- drift tripwire: it computes each report from the SQLite path (asserted in CI)
-- and, when a Postgres is reachable, asserts the functions here return the SAME
-- result over an identical seed corpus. A maintainer runs the same equivalence
-- against their own Supabase (developer handbook `remote-read-parity.md`).
--
-- SECURITY MODEL — the load-bearing invariant a reviewer must confirm
-- ---------------------------------------------------------------------------
-- Every function and view below is **SECURITY INVOKER** (functions state it
-- explicitly; each view sets `security_invoker = true`). They therefore run with
-- the PRIVILEGES AND RLS OF THE CALLER, so the owner-scoped Row-Level Security in
-- schema.sql applies and a caller sees ONLY THEIR OWN rows (own-rows-only). None
-- of them is `SECURITY DEFINER` — that would run as the object owner and BYPASS
-- the caller's RLS, turning a per-user report into a cross-user data leak. A
-- Postgres VIEW is especially dangerous here: WITHOUT `security_invoker = true` a
-- view evaluates RLS as its OWNER (definer-like), so the flags below are mandatory,
-- not cosmetic. No `service_role` / secret / bypass key is referenced anywhere;
-- reports run under the same per-user Auth JWT as the writes.
--
-- PRIVILEGE FLOOR — EXECUTE/SELECT is granted to `authenticated` only; `anon` and
-- PUBLIC are revoked, so an unauthenticated request cannot invoke a report even
-- before RLS is consulted (matches schema.sql's DML floor).
--
-- Re-runnable: drop-then-create, so re-applying this file is safe.
-- ===========================================================================

-- Functions depend on the views; drop them first, then the views (dependent
-- first), then recreate.
DROP FUNCTION IF EXISTS public.report_project_stats();
DROP FUNCTION IF EXISTS public.report_token_stats();
DROP FUNCTION IF EXISTS public.report_info(text);
DROP VIEW IF EXISTS public.report_priced_events;
DROP VIEW IF EXISTS public.report_model_pricing;

-- ---------------------------------------------------------------------------
-- report_model_pricing — one row per (model, pricing row whose `model_prefix` is
-- a prefix of the model name), at every `effective_from`, carrying the row's
-- rates and the ONE Postgres definition of the `estimated` flag. Both consumers
-- read the flag from here: report_priced_events (for the row each event
-- resolves to) and report_token_stats' models_without_own_price (a model with
-- no row here whose `estimated` is false has no own price).
--
-- `security_invoker = true` (MANDATORY): the view evaluates RLS as the CALLER, so
-- models/pricing are filtered to the caller's own owner_id.
-- ---------------------------------------------------------------------------
CREATE VIEW public.report_model_pricing
  WITH (security_invoker = true) AS
SELECT
  m.owner_id        AS owner_id,
  m.name            AS model_name,
  pr.model_prefix   AS model_prefix,
  pr.in_usd         AS in_usd,
  pr.out_usd        AS out_usd,
  pr.cache_r_usd    AS cache_r_usd,
  pr.cache_w_usd    AS cache_w_usd,
  pr.cache_w_1h_usd AS cache_w_1h_usd,
  pr.effective_from AS effective_from,
  -- true when pricing this model at this row is an ESTIMATE: the row is a
  -- FAMILY DEFAULT row (a bare `claude-<family>-` prefix, the family's fallback
  -- rate) or an ANCESTOR row (R = the model name minus the row's prefix opens
  -- with a point-release segment `-<1-2 digits>`: an unlisted point release
  -- priced at its nearest listed ancestor's row); false for the model's own
  -- row. Twin of capture.is_estimated / capture.estimated_sql.
  (pr.model_prefix OPERATOR(pg_catalog.~) '^claude-[a-z]+-$'
   OR pg_catalog.substr(m.name,
        pg_catalog.length(pr.model_prefix) OPERATOR(pg_catalog.+) 1)
      OPERATOR(pg_catalog.~) '^-[0-9]{1,2}(-|$)') AS estimated
FROM public.models m
JOIN public.pricing pr
  ON pr.owner_id = m.owner_id
 AND m.name LIKE pr.model_prefix || '%';

GRANT SELECT ON public.report_model_pricing TO authenticated;
REVOKE ALL ON public.report_model_pricing FROM PUBLIC, anon;

-- ---------------------------------------------------------------------------
-- report_priced_events — one row per event, priced at the rate in force at the
-- event's OWN timestamp. This is the single Postgres definition of the pricing
-- resolution that `report.py`'s `rate_subquery()` expresses in SQLite: for each
-- event, the pricing row whose `model_prefix` is a prefix of the model name and
-- whose `effective_from <= ts`, choosing the LONGEST prefix then the LATEST
-- `effective_from`. `report.py` runs one correlated subquery per rate column, all
-- with identical WHERE/ORDER/LIMIT, so they all resolve to the SAME pricing row;
-- the LATERAL join below picks that one row once (from report_model_pricing, the
-- model's matching rows) and reads every column from it — equivalent given a
-- unique best match (the pricing UNIQUE key + curated data guarantee it; a
-- prefix/effective_from tie would be ambiguous in BOTH dialects).
--
-- `security_invoker = true` (MANDATORY): the view evaluates RLS as the CALLER, so
-- events/models/sessions/pricing are all filtered to the caller's own owner_id.
-- ---------------------------------------------------------------------------
CREATE VIEW public.report_priced_events
  WITH (security_invoker = true) AS
SELECT
  e.owner_id       AS owner_id,
  e.session_uuid   AS session_uuid,
  s.project_path   AS project_path,
  m.name           AS model_name,
  e.ts             AS ts,
  e.kind           AS kind,
  e.agent          AS agent,
  e.note           AS note,
  e.issue_key      AS issue_key,
  e.in_tok         AS in_tok,
  e.out_tok        AS out_tok,
  e.cache_r        AS cache_r,
  e.cache_w        AS cache_w,
  e.cache_w_1h     AS cache_w_1h,
  pr.in_usd        AS in_usd,
  pr.out_usd       AS out_usd,
  pr.cache_r_usd   AS cache_r_usd,
  pr.cache_w_usd   AS cache_w_usd,
  pr.cache_w_1h_usd AS cache_w_1h_usd,
  pr.effective_from AS rate_from,
  -- the resolved row's report_model_pricing.estimated: true = the event's cost
  -- is an ESTIMATE, false = the model's own row, NULL = unpriced.
  pr.estimated     AS estimated
FROM public.events e
JOIN public.models m
  ON m.owner_id = e.owner_id AND m.name = e.model_name
JOIN public.sessions s
  ON s.owner_id = e.owner_id AND s.uuid = e.session_uuid
LEFT JOIN LATERAL (
  SELECT mp.in_usd, mp.out_usd, mp.cache_r_usd, mp.cache_w_usd,
         mp.cache_w_1h_usd, mp.effective_from, mp.model_prefix, mp.estimated
  FROM public.report_model_pricing mp
  WHERE mp.owner_id = e.owner_id
    AND mp.model_name = m.name
    AND mp.effective_from <= e.ts
  ORDER BY length(mp.model_prefix) DESC, mp.effective_from DESC
  LIMIT 1
) pr ON true;

GRANT SELECT ON public.report_priced_events TO authenticated;
REVOKE ALL ON public.report_priced_events FROM PUBLIC, anon;

-- ---------------------------------------------------------------------------
-- report_project_stats() — the `/project-stats` all-time per-project rollup,
-- ordered by estimated cost then output, as a jsonb array of objects. Mirrors
-- `report.py.fetch_project_stats`: projects -> sessions -> events (LEFT JOINs, so
-- a project or session with no events still appears), cost components split into
-- classic (uncached in/out) and cached (read/write, with the 1h cache-write slice
-- priced at its own rate and falling back to the 5m rate). The basename fallback
-- for the display name stays in the CLIENT renderer, so raw path + name are
-- returned here exactly as the SQLite fetch leaves them. `estimated_events`
-- counts the project's events priced at an estimate (the view's `estimated`).
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.report_project_stats()
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
  SELECT coalesce(
    jsonb_agg(to_jsonb(t) ORDER BY
      (t.classic_in + t.classic_out + t.cached_r + t.cached_w) DESC,
      t.output DESC),
    '[]'::jsonb)
  FROM (
    SELECT
      p.path AS path,
      p.name AS name,
      count(DISTINCT s.uuid)                                   AS sessions,
      count(pe.ts)                                             AS events,
      coalesce(sum(pe.in_tok), 0)                              AS input,
      coalesce(sum(pe.out_tok), 0)                             AS output,
      coalesce(sum(pe.cache_r), 0)                             AS cache_read,
      coalesce(sum(pe.cache_w), 0)                             AS cache_write,
      coalesce(sum(pe.in_tok * coalesce(pe.in_usd, 0)), 0)
        / 1000000.0                                            AS classic_in,
      coalesce(sum(pe.out_tok * coalesce(pe.out_usd, 0)), 0)
        / 1000000.0                                            AS classic_out,
      coalesce(sum(pe.cache_r * coalesce(pe.cache_r_usd, 0)), 0)
        / 1000000.0                                            AS cached_r,
      coalesce(sum((pe.cache_w - pe.cache_w_1h) * coalesce(pe.cache_w_usd, 0)
            + pe.cache_w_1h * coalesce(pe.cache_w_1h_usd, pe.cache_w_usd, 0)), 0)
            / 1000000.0                                        AS cached_w,
      max(pe.rate_from)                                        AS rate_from,
      sum(CASE WHEN pe.ts IS NOT NULL AND pe.rate_from IS NULL
               THEN 1 ELSE 0 END)                              AS unpriced_events,
      CASE WHEN min(pe.ts) IS NULL THEN NULL
           ELSE (to_timestamp(min(pe.ts))::date)::text END     AS first_seen,
      CASE WHEN max(pe.ts) IS NULL THEN NULL
           ELSE (to_timestamp(max(pe.ts))::date)::text END     AS last_activity,
      -- events priced at an ESTIMATE (view column `estimated`: family default
      -- or ancestor row); NULL (unpriced) and false both count 0, exactly as
      -- report.py's SUM(CASE WHEN estimated = 1 ...).
      pg_catalog.sum(CASE WHEN pe.estimated THEN 1 ELSE 0 END)
                                                               AS estimated_events
    FROM public.projects p
    LEFT JOIN public.sessions s
      ON s.owner_id = p.owner_id AND s.project_path = p.path
    LEFT JOIN public.report_priced_events pe
      ON pe.owner_id = s.owner_id AND pe.session_uuid = s.uuid
    GROUP BY p.path, p.name
  ) t;
$$;

REVOKE ALL ON FUNCTION public.report_project_stats() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.report_project_stats() TO authenticated;

-- ---------------------------------------------------------------------------
-- report_token_stats() — every breakdown the `/token-stats` report shows, as one
-- jsonb object. Mirrors `report.py.fetch_token_stats` window-for-window:
--   * "today" = the reader's LOCAL day (local midnight, as a UTC epoch), matching
--     SQLite's `strftime('%s','now','localtime','start of day','utc')`. This
--     assumes the Postgres session TimeZone equals the machine's local zone (the
--     one cross-dialect tz caveat — see the handbook).
--   * windowed totals/by_agent/by_model/by_kind/by_tier exclude first-capture
--     backlog roll-ups (`note = 'backlog-capture'`); "by_project" does NOT (it
--     mirrors report.py, which omits that filter there); "by_issue" is all-time.
--   * every ORDER BY is `sum(out_tok) DESC`, exactly as report.py; the seed corpus
--     avoids ties on that key so the two dialects cannot order a tie differently.
-- Tiers are ROLE-based, mirroring the kit's token-economics.md "Tier mapping"
-- verbatim (docs/TELEMETRY-CONTRACT.md restates it): the main session (kind=0,
-- agent NULL) is 'orchestrator'; named marvin:* personas route to their own
-- tier (marvin:developer/marvin:researcher/marvin:validator-* -> heavy;
-- marvin:escalation-* -> ladder; marvin:developer-small/marvin:documenter ->
-- small; marvin:ponytail -> micro); any OTHER agent falls back to its model's
-- claude-fable/opus/sonnet/haiku prefix (ladder/heavy/small/micro), 'unknown'
-- for no match. The role split is visible in by_tier (and by_rung), NOT in
-- by_model: that stays ONE row per model, as before role tiering, but its
-- tier column now lists every tier the model actually served that window,
-- comma-joined in the kit's display order (orchestrator, heavy, ladder,
-- small, micro, 'unknown' last) — e.g. a model used both as the main session
-- and as a marvin:developer subagent reads 'orchestrator, heavy'. by_rung
-- breaks the 'ladder' rows of by_tier down by escalation rung (high/xhigh/
-- max/frontier), read only from the named marvin:escalation-<rung> persona; a
-- ladder row with no such name (the model-prefix fallback, or an
-- unrecognized marvin:escalation-* suffix) is grouped under v_rung_fallback
-- ('no rung (fallback)') instead of dropped, so by_rung's rows always sum to
-- by_tier's ladder total.
-- The tier and rung rules are written ONCE, in the `tagged` CTE at the top of
-- the function body: by_model, by_tier and by_rung all read its `tier`/`rung`
-- columns, so the three breakdowns cannot classify the same event differently
-- (tests/test_report_parity.py pins that the CASE appears exactly once here).
-- Raw path+name are returned for by_project (basename stays client-side).
-- The estimate figures (estimated_by_model, events_by_model, unpriced_by_model,
-- models_without_own_price) mirror report.py's keys of the same names; they
-- read only through the SECURITY INVOKER view and RLS-scoped tables, so they
-- widen nothing a caller can see.
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.report_token_stats()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
DECLARE
  -- Local midnight today, as a UTC epoch (== SQLite 'localtime','start of day').
  v_today bigint := extract(epoch FROM date_trunc('day', now()))::bigint;
  -- now - 7 days, in seconds (pure arithmetic == SQLite '-7 days').
  v_week  bigint := extract(epoch FROM now())::bigint - 7 * 86400;
  -- by_rung's catch-all label for a ladder row with no named rung — mirrors
  -- report.py's RUNG_FALLBACK_LABEL verbatim.
  v_rung_fallback text := 'no rung (fallback)';
  result  jsonb;
BEGIN
  -- `tagged` — every event in the by_model/by_tier/by_rung window (7 days,
  -- backlog excluded), with its ROLE tier and, for a ladder-tier event ONLY,
  -- its escalation rung (NULL for every other tier, so by_rung's
  -- `rung IS NOT NULL` is exactly by_tier's ladder rows). This is the ONE
  -- Postgres definition of both rules (twin of report.py's
  -- tier_case()/rung_case(), which fetch_token_stats likewise splices once
  -- into its own shared CTE); the three aggregates below only ever read
  -- t.tier / t.rung, never restate a CASE.
  WITH tagged AS (
    SELECT tt.*,
           CASE WHEN tt.tier = 'ladder' THEN coalesce(CASE
             WHEN tt.agent = 'marvin:escalation-high'     THEN 'high'
             WHEN tt.agent = 'marvin:escalation-xhigh'    THEN 'xhigh'
             WHEN tt.agent = 'marvin:escalation-max'      THEN 'max'
             WHEN tt.agent = 'marvin:escalation-frontier' THEN 'frontier'
           END, v_rung_fallback) END AS rung
    FROM (
      SELECT pe.*,
             CASE
               WHEN pe.kind = 0 AND pe.agent IS NULL THEN 'orchestrator'
               WHEN pe.agent IN ('marvin:developer', 'marvin:researcher')
                    OR pe.agent LIKE 'marvin:validator-%' THEN 'heavy'
               WHEN pe.agent LIKE 'marvin:escalation-%' THEN 'ladder'
               WHEN pe.agent IN ('marvin:developer-small', 'marvin:documenter')
                    THEN 'small'
               WHEN pe.agent = 'marvin:ponytail' THEN 'micro'
               WHEN pe.model_name LIKE 'claude-opus-%'   THEN 'heavy'
               WHEN pe.model_name LIKE 'claude-sonnet-%' THEN 'small'
               WHEN pe.model_name LIKE 'claude-haiku-%'  THEN 'micro'
               WHEN pe.model_name LIKE 'claude-fable-%'  THEN 'ladder'
               ELSE 'unknown' END AS tier
      FROM public.report_priced_events pe
      WHERE pe.ts >= v_week AND coalesce(pe.note, '') <> 'backlog-capture'
    ) tt
  )
  SELECT jsonb_build_object(
    'today', (
      SELECT jsonb_build_array(
               coalesce(sum(in_tok), 0), coalesce(sum(out_tok), 0),
               coalesce(sum(cache_r), 0), coalesce(sum(cache_w), 0), count(*))
      FROM public.events
      WHERE ts >= v_today AND coalesce(note, '') <> 'backlog-capture'),
    'week', (
      SELECT jsonb_build_array(
               coalesce(sum(in_tok), 0), coalesce(sum(out_tok), 0),
               coalesce(sum(cache_r), 0), coalesce(sum(cache_w), 0), count(*))
      FROM public.events
      WHERE ts >= v_week AND coalesce(note, '') <> 'backlog-capture'),
    'backlog_excluded', (
      SELECT count(*) FROM public.events
      WHERE ts >= v_week AND coalesce(note, '') = 'backlog-capture'),
    'by_project', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(path, name, i, o, cr, cw, n) ORDER BY o DESC),
             '[]'::jsonb)
      FROM (
        SELECT p.path AS path, p.name AS name,
               sum(e.in_tok) AS i, sum(e.out_tok) AS o,
               sum(e.cache_r) AS cr, sum(e.cache_w) AS cw, count(*) AS n
        FROM public.events e
        JOIN public.sessions s
          ON s.owner_id = e.owner_id AND s.uuid = e.session_uuid
        JOIN public.projects p
          ON p.owner_id = s.owner_id AND p.path = s.project_path
        WHERE e.ts >= v_week
        GROUP BY p.path, p.name
      ) q),
    'by_agent', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(label, i, o, n) ORDER BY o DESC), '[]'::jsonb)
      FROM (
        SELECT coalesce(agent,
                 CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END) AS label,
               sum(in_tok) AS i, sum(out_tok) AS o, count(*) AS n
        FROM public.events
        WHERE ts >= v_week AND coalesce(note, '') <> 'backlog-capture'
        GROUP BY 1
      ) q),
    -- by_model is ONE row per model (F1): unlike by_tier, it stays
    -- model-keyed, but a model that served more than one role-tier this
    -- window lists them ALL, comma-joined in the kit's display order
    -- (orchestrator, heavy, ladder, small, micro, unknown last) via the
    -- INNER per-(model,tier) grouping's `string_agg(... ORDER BY tier_rank)`
    -- — a documented Postgres aggregate-ORDER-BY extension. `tier_rank` is a
    -- rank over the tier TEXT only (mirrors report.py's tier_rank_case), so
    -- it cannot itself drift from a role-rule edit to the `tagged` CTE's CASE.
    'by_model', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(model_name, tier_list, i, o, cost, rate_from)
               ORDER BY o DESC, model_name COLLATE pg_catalog."C"), '[]'::jsonb)
      FROM (
        SELECT model_name,
               string_agg(tier, ', ' ORDER BY
                 CASE tier
                   WHEN 'orchestrator' THEN 0 WHEN 'heavy' THEN 1
                   WHEN 'ladder' THEN 2 WHEN 'small' THEN 3
                   WHEN 'micro' THEN 4 ELSE 5 END) AS tier_list,
               sum(i) AS i, sum(o) AS o,
               round(sum(cost)::numeric, 4) AS cost,
               max(rate_from) AS rate_from
        FROM (
          SELECT t.model_name AS model_name, t.tier AS tier,
                 sum(t.in_tok) AS i, sum(t.out_tok) AS o,
                 sum(
                   t.in_tok  * coalesce(t.in_usd, 0)
                 + t.out_tok * coalesce(t.out_usd, 0)
                 + t.cache_r * coalesce(t.cache_r_usd, 0)
                 + (t.cache_w - t.cache_w_1h) * coalesce(t.cache_w_usd, 0)
                 + t.cache_w_1h
                     * coalesce(t.cache_w_1h_usd, t.cache_w_usd, 0)
                 ) / 1000000.0 AS cost,
                 max(t.rate_from) AS rate_from
          FROM tagged t
          GROUP BY t.model_name, t.tier
        ) per_model_tier
        GROUP BY model_name
      ) q),
    'by_kind', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(label, i, o, pct) ORDER BY srt), '[]'::jsonb)
      FROM (
        SELECT kind AS srt,
               CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END AS label,
               sum(in_tok) AS i, sum(out_tok) AS o,
               round(100.0 * sum(cache_r)
                     / nullif(sum(in_tok) + sum(cache_r), 0), 1) AS pct
        FROM public.events
        WHERE ts >= v_week AND coalesce(note, '') <> 'backlog-capture'
        GROUP BY kind
      ) q),
    'by_tier', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(tier, i, o, n)
               ORDER BY o DESC, tier COLLATE pg_catalog."C"), '[]'::jsonb)
      FROM (
        SELECT t.tier AS tier,
               sum(t.in_tok) AS i, sum(t.out_tok) AS o, count(*) AS n
        FROM tagged t
        GROUP BY t.tier
      ) q),
    -- Ladder rungs, broken out from the 'ladder' rows of by_tier above: high /
    -- xhigh / max / frontier, read only from the named marvin:escalation-*
    -- persona (never inferred from model or effort). A ladder row with no
    -- such name (the model-prefix fallback, or an unrecognized
    -- marvin:escalation-* suffix) is COALESCEd into v_rung_fallback rather
    -- than dropped, so by_rung's rows always sum to by_tier's ladder total —
    -- mirrors report.py's RUNG_FALLBACK_LABEL verbatim (report_token_stats'
    -- DECLARE block); it only appears when at least one such row exists.
    'by_rung', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(rung, i, o, n)
               ORDER BY o DESC, rung COLLATE pg_catalog."C"), '[]'::jsonb)
      FROM (
        SELECT t.rung AS rung, pg_catalog.sum(t.in_tok) AS i,
               pg_catalog.sum(t.out_tok) AS o, pg_catalog.count(*) AS n
        FROM tagged t
        WHERE t.rung IS NOT NULL   -- set for, and only for, ladder-tier rows
        GROUP BY t.rung
      ) q),
    'by_issue', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(issue_key, i, o, cr, cw, n) ORDER BY o DESC),
             '[]'::jsonb)
      FROM (
        SELECT issue_key, sum(in_tok) AS i, sum(out_tok) AS o,
               sum(cache_r) AS cr, sum(cache_w) AS cw, count(*) AS n
        FROM public.events
        WHERE issue_key IS NOT NULL
        GROUP BY issue_key
      ) q),
    -- Estimate figures, same 7-day backlog-excluded window as by_model (twin of
    -- report.py's estimated_by_model / events_by_model / unpriced_by_model):
    -- model -> count, models with a zero count omitted except in
    -- events_by_model, which lists every by_model name.
    'estimated_by_model', (
      SELECT coalesce(
               pg_catalog.jsonb_object_agg(q.model_name, q.n),
               '{}'::pg_catalog.jsonb)
      FROM (
        SELECT pe.model_name AS model_name, pg_catalog.count(*) AS n
        FROM public.report_priced_events pe
        WHERE pe.ts >= v_week AND coalesce(pe.note, '') <> 'backlog-capture'
          AND pe.estimated
        GROUP BY pe.model_name
      ) q),
    'events_by_model', (
      SELECT coalesce(
               pg_catalog.jsonb_object_agg(q.model_name, q.n),
               '{}'::pg_catalog.jsonb)
      FROM (
        SELECT pe.model_name AS model_name, pg_catalog.count(*) AS n
        FROM public.report_priced_events pe
        WHERE pe.ts >= v_week AND coalesce(pe.note, '') <> 'backlog-capture'
        GROUP BY pe.model_name
      ) q),
    'unpriced_by_model', (
      SELECT coalesce(
               pg_catalog.jsonb_object_agg(q.model_name, q.n),
               '{}'::pg_catalog.jsonb)
      FROM (
        SELECT pe.model_name AS model_name, pg_catalog.count(*) AS n
        FROM public.report_priced_events pe
        WHERE pe.ts >= v_week AND coalesce(pe.note, '') <> 'backlog-capture'
          AND pe.rate_from IS NULL
        GROUP BY pe.model_name
      ) q),
    -- Models WITHOUT OWN PRICE, all-time (twin of report.py's
    -- fetch_models_without_own_price): a model with at least one NON-ZERO-TOKEN
    -- event (a model whose events are ALL zero-token — e.g. a synthetic
    -- bookkeeping model with no input/output/cache tokens — has nothing to
    -- price and is excluded) and no pricing row matching it (any
    -- effective_from) that is neither a family default row nor an ancestor row
    -- for it, i.e. no report_model_pricing row with `estimated` false. Sorted
    -- by byte order (COLLATE "C") to match SQLite's BINARY ORDER BY. RLS scopes
    -- every table to the caller.
    'models_without_own_price', (
      SELECT coalesce(
               pg_catalog.jsonb_agg(q.name ORDER BY q.name COLLATE pg_catalog."C"),
               '[]'::pg_catalog.jsonb)
      FROM (
        SELECT DISTINCT m.name AS name
        FROM public.models m
        WHERE EXISTS (
                SELECT 1 FROM public.events e
                WHERE e.owner_id = m.owner_id AND e.model_name = m.name
                  AND (e.in_tok <> 0 OR e.out_tok <> 0 OR e.cache_r <> 0
                       OR e.cache_w <> 0))
          AND NOT EXISTS (
                SELECT 1 FROM public.report_model_pricing mp
                WHERE mp.owner_id = m.owner_id AND mp.model_name = m.name
                  AND NOT mp.estimated)
      ) q)
  ) INTO result;
  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.report_token_stats() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.report_token_stats() TO authenticated;

-- ---------------------------------------------------------------------------
-- report_info(p_project_path) — the DB-derived portion of `/info` (the local
-- filesystem portion stays local in the client). Mirrors
-- `report.py.fetch_info_central`: overall event count + local-date span, project
-- and pricing-row counts, the latest rate already IN FORCE (a future-dated row
-- must not masquerade as current), and THIS project's event count. `schema`
-- (SQLite's PRAGMA user_version) is a store property, not an aggregation, and has
-- no remote analogue, so it is NOT returned — the client stamps the modeled
-- remote shape and the golden test excludes it from the equality.
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.report_info(p_project_path text)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
  SELECT jsonb_build_object(
    'events', (SELECT count(*) FROM public.events),
    'first_day', (SELECT CASE WHEN min(ts) IS NULL THEN NULL
                    ELSE (to_timestamp(min(ts))::date)::text END
                  FROM public.events),
    'last_day', (SELECT CASE WHEN max(ts) IS NULL THEN NULL
                    ELSE (to_timestamp(max(ts))::date)::text END
                 FROM public.events),
    'projects', (SELECT count(*) FROM public.projects),
    'pricing_rows', (SELECT count(*) FROM public.pricing),
    'latest_rate_from', (
      SELECT max(CASE WHEN effective_from <= extract(epoch FROM now())::bigint
                      THEN effective_from END)
      FROM public.pricing),
    'events_here', (
      SELECT count(*)
      FROM public.events e
      JOIN public.sessions s
        ON s.owner_id = e.owner_id AND s.uuid = e.session_uuid
      WHERE s.project_path = p_project_path)
  );
$$;

REVOKE ALL ON FUNCTION public.report_info(text) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.report_info(text) TO authenticated;
