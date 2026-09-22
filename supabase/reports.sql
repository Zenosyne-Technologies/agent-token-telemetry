-- ===========================================================================
-- token-telemetry — remote read parity: server-side report aggregation (P9)
-- SECURITY-GATED. This file is the Postgres HALF of a DUAL-DIALECT contract.
-- ===========================================================================
--
-- The client renders `/token-stats`, `/project-stats` and `/info` from the exact
-- same aggregations whether the data lives in the local SQLite store or the
-- remote Supabase (Postgres) store. `scripts/report.py` is the REFERENCE dialect
-- (SQLite); the view + functions below reproduce its pricing resolution, its time
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
-- Every function and the view below is **SECURITY INVOKER** (functions state it
-- explicitly; the view sets `security_invoker = true`). They therefore run with
-- the PRIVILEGES AND RLS OF THE CALLER, so the owner-scoped Row-Level Security in
-- schema.sql applies and a caller sees ONLY THEIR OWN rows (own-rows-only). None
-- of them is `SECURITY DEFINER` — that would run as the object owner and BYPASS
-- the caller's RLS, turning a per-user report into a cross-user data leak. A
-- Postgres VIEW is especially dangerous here: WITHOUT `security_invoker = true` a
-- view evaluates RLS as its OWNER (definer-like), so the flag below is mandatory,
-- not cosmetic. No `service_role` / secret / bypass key is referenced anywhere;
-- reports run under the same per-user Auth JWT as the writes.
--
-- PRIVILEGE FLOOR — EXECUTE/SELECT is granted to `authenticated` only; `anon` and
-- PUBLIC are revoked, so an unauthenticated request cannot invoke a report even
-- before RLS is consulted (matches schema.sql's DML floor).
--
-- Re-runnable: drop-then-create, so re-applying this file is safe.
-- ===========================================================================

-- Functions depend on the view; drop them first, then the view, then recreate.
DROP FUNCTION IF EXISTS public.report_project_stats();
DROP FUNCTION IF EXISTS public.report_token_stats();
DROP FUNCTION IF EXISTS public.report_info(text);
DROP VIEW IF EXISTS public.report_priced_events;

-- ---------------------------------------------------------------------------
-- report_priced_events — one row per event, priced at the rate in force at the
-- event's OWN timestamp. This is the single Postgres definition of the pricing
-- resolution that `report.py`'s `rate_subquery()` expresses in SQLite: for each
-- event, the pricing row whose `model_prefix` is a prefix of the model name and
-- whose `effective_from <= ts`, choosing the LONGEST prefix then the LATEST
-- `effective_from`. `report.py` runs one correlated subquery per rate column, all
-- with identical WHERE/ORDER/LIMIT, so they all resolve to the SAME pricing row;
-- the LATERAL join below picks that one row once and reads every column from it —
-- equivalent given a unique best match (the pricing UNIQUE key + curated data
-- guarantee it; a prefix/effective_from tie would be ambiguous in BOTH dialects).
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
  pr.effective_from AS rate_from
FROM public.events e
JOIN public.models m
  ON m.owner_id = e.owner_id AND m.name = e.model_name
JOIN public.sessions s
  ON s.owner_id = e.owner_id AND s.uuid = e.session_uuid
LEFT JOIN LATERAL (
  SELECT p.in_usd, p.out_usd, p.cache_r_usd, p.cache_w_usd,
         p.cache_w_1h_usd, p.effective_from
  FROM public.pricing p
  WHERE p.owner_id = e.owner_id
    AND m.name LIKE p.model_prefix || '%'
    AND p.effective_from <= e.ts
  ORDER BY length(p.model_prefix) DESC, p.effective_from DESC
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
-- returned here exactly as the SQLite fetch leaves them.
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.report_project_stats()
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY INVOKER
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
           ELSE (to_timestamp(max(pe.ts))::date)::text END     AS last_activity
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
-- Tiers map claude-fable/opus/sonnet/haiku prefixes to orchestrator/heavy/small/
-- micro. Raw path+name are returned for by_project (basename stays client-side).
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.report_token_stats()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
AS $$
DECLARE
  -- Local midnight today, as a UTC epoch (== SQLite 'localtime','start of day').
  v_today bigint := extract(epoch FROM date_trunc('day', now()))::bigint;
  -- now - 7 days, in seconds (pure arithmetic == SQLite '-7 days').
  v_week  bigint := extract(epoch FROM now())::bigint - 7 * 86400;
  result  jsonb;
BEGIN
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
    'by_model', (
      SELECT coalesce(jsonb_agg(
               jsonb_build_array(model_name, tier, i, o, cost, rate_from)
               ORDER BY o DESC), '[]'::jsonb)
      FROM (
        SELECT model_name,
               CASE
                 WHEN model_name LIKE 'claude-fable-%'  THEN 'orchestrator'
                 WHEN model_name LIKE 'claude-opus-%'   THEN 'heavy'
                 WHEN model_name LIKE 'claude-sonnet-%' THEN 'small'
                 WHEN model_name LIKE 'claude-haiku-%'  THEN 'micro'
                 ELSE 'unknown' END AS tier,
               i, o, cost, rate_from
        FROM (
          SELECT pe.model_name AS model_name,
                 sum(pe.in_tok) AS i, sum(pe.out_tok) AS o,
                 round((sum(
                   pe.in_tok  * coalesce(pe.in_usd, 0)
                 + pe.out_tok * coalesce(pe.out_usd, 0)
                 + pe.cache_r * coalesce(pe.cache_r_usd, 0)
                 + (pe.cache_w - pe.cache_w_1h) * coalesce(pe.cache_w_usd, 0)
                 + pe.cache_w_1h
                     * coalesce(pe.cache_w_1h_usd, pe.cache_w_usd, 0)
                 ) / 1000000.0)::numeric, 4) AS cost,
                 max(pe.rate_from) AS rate_from
          FROM public.report_priced_events pe
          WHERE pe.ts >= v_week AND coalesce(pe.note, '') <> 'backlog-capture'
          GROUP BY pe.model_name
        ) m
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
               jsonb_build_array(tier, i, o, n) ORDER BY o DESC), '[]'::jsonb)
      FROM (
        SELECT CASE
                 WHEN pe.model_name LIKE 'claude-fable-%'  THEN 'orchestrator'
                 WHEN pe.model_name LIKE 'claude-opus-%'   THEN 'heavy'
                 WHEN pe.model_name LIKE 'claude-sonnet-%' THEN 'small'
                 WHEN pe.model_name LIKE 'claude-haiku-%'  THEN 'micro'
                 ELSE 'unknown' END AS tier,
               sum(pe.in_tok) AS i, sum(pe.out_tok) AS o, count(*) AS n
        FROM public.report_priced_events pe
        WHERE pe.ts >= v_week AND coalesce(pe.note, '') <> 'backlog-capture'
        GROUP BY 1
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
