---
doc: Remote Read Parity
type: handbook
status: active
summary: How reports read from the remote (Supabase/Postgres) backend with the same numbers as local SQLite — the SECURITY INVOKER reports.sql RPC/views (incl. report_model_pricing, the one Postgres definition of the estimated flag and the no-own-price footer's source), the client read_for_report mapping, report.py routing on active_backend, the by_model/by_tier name tie-break proven insertion-order- and collation-independent, and the dual-dialect drift risk guarded by the cross-dialect golden test and the estimated-flag three-way parity test (SQLite reference in CI, Postgres-gated equivalence, maintainer live step).
keywords: [read-parity, reports, supabase, postgres, rpc, security-invoker, rls, golden-test, dual-dialect, drift, pricing, windows, tiers, estimated, family-default, ancestor-row, tie-break, collation]
level: project
audience: developer
module: storage
sources: [supabase/reports.sql, scripts/supabase_backend.py, scripts/report.py, scripts/storage.py, scripts/capture.py, tests/test_report_parity.py, tests/test_family_default.py]
related: ["[[supabase-backend]]", "[[rls-remote-schema]]", "[[storage-backend]]", "[[capture-pipeline]]", "[[pricing-updates]]"]
created: 2026-09-22
updated: 2026-09-23
---

# Remote Read Parity

`/token-stats`, `/project-stats` and `/info` must show the same numbers whether a
project's telemetry lives in the local SQLite store or the central remote store.
`scripts/report.py` is the **reference dialect** (SQLite). The remote store is
Postgres behind PostgREST, which does not run arbitrary SQL — it exposes tables,
views and RPC. So read parity is achieved by reproducing each report's aggregation
**server-side in Postgres** (`supabase/reports.sql`), called over the same
TLS-verified transport as the writes.

The architect chose server-side parity (Postgres views/RPC) over client-side
re-aggregation. That choice authors the same query in two SQL dialects, and two
dialects of one query can silently drift — so it is **mandatory** to pair it with
a cross-dialect golden test. That test is the drift tripwire; keeping the two
dialects in lockstep is a maintenance obligation this page exists to make explicit.

## The three pieces

**1. `supabase/reports.sql` — server-side aggregation.** Two views and three
functions, hand-written and reviewed:

- `report_model_pricing` — a view at (model, matching pricing row) grain: every
  `pricing` row whose `model_prefix` is a prefix of a model's name, at every
  `effective_from`, carrying that row's rates and the **one Postgres definition**
  of the `estimated` flag (family-default prefix `^claude-[a-z]+-$`, or an
  ancestor-row point-release match) — the twin of `capture.is_estimated` /
  `capture.estimated_sql`. Both consumers read the flag from here rather than
  recomputing it: `report_priced_events` (below) and `report_token_stats`'s
  `models_without_own_price` (a model name with no row here whose `estimated` is
  false has no own price).
- `report_priced_events` — a view at event grain that resolves each event's price
  at the rate in force at its own timestamp (longest `model_prefix` match with
  `effective_from <= ts`, tiebreak latest `effective_from`) by joining
  `report_model_pricing` LATERALly and passing its `estimated` column straight
  through, the single Postgres expression of `report.py`'s `rate_subquery()`.
  `report.py` runs one correlated subquery per rate column, all with identical
  `WHERE`/`ORDER`/`LIMIT`, so they resolve to the same pricing row; the view's
  `LEFT JOIN LATERAL ... LIMIT 1` reads that one row's columns once — equivalent
  given a unique best match. The resolved `estimated` is true when the row is a
  family default or an ancestor row (see [[pricing-updates]] and
  `docs/TELEMETRY-CONTRACT.md` §"Own price vs estimate"), false for the
  model's own row, NULL when unpriced — the Postgres twin of
  `capture.is_estimated` / `capture.estimated_sql`.
- `report_project_stats()` → the `/project-stats` array, now including
  `estimated_events` per project (events priced at a family-default or ancestor
  row; `NULL`/false both count 0, mirroring `report.py`'s `SUM(CASE WHEN
  estimated = 1 ...)`); `report_token_stats()` → the `/token-stats` object
  (today/week windows, by project/agent/model/kind/tier/issue), now also
  carrying `estimated_by_model`, `events_by_model`, `unpriced_by_model` (all
  keyed by model name over the same 7-day backlog-excluded window as
  `by_model`) and `models_without_own_price` (all-time, sorted `COLLATE "C"` to
  match SQLite's byte order) — the twins of `report.py`'s keys of the same
  names, read only through the SECURITY INVOKER view and RLS-scoped tables, so
  they widen nothing a caller can see; `report_info(p_project_path)` → the
  DB-derived `/info` block.

Each returns `jsonb` the client maps 1:1 into the shape `report.py`'s matching
`fetch_*` returns, so the **same `render_*` functions** produce the markdown.

**2. Client `read_for_report` (`scripts/supabase_backend.py`).** The read seam the
storage interface (`scripts/storage.py`) defines and P3 deferred. It calls
`POST /rest/v1/rpc/<fn>` through the one TLS-verified `_request` site (publishable
key + per-user Auth Bearer in headers, never in the URL) and maps the JSON into
the `fetch_*` shape. `LocalSqliteBackend.read_for_report` is the local sibling —
it just runs `report.py`'s SQL. Capability: `server_side_aggregation=True`.

**3. `report.py` routing.** When `storage.remote_backend_if_active()` returns a
backend (i.e. `active_backend=supabase`), `main()` renders the three aggregation
reports from `remote.read_for_report(...)`; the local path is **byte-for-byte
unchanged** for the default `local` backend, and `--scope` / `storage-status`
always stay local (they describe the local project/outbox store). A remote read
failure degrades to a one-line message, never a stack trace.

## Security: SECURITY INVOKER, RLS-respecting (read this before editing reports.sql)

Every function is **`SECURITY INVOKER`** (stated explicitly) and each view sets
**`security_invoker = true`**. They therefore run with the **caller's** privileges
and RLS, so the owner-scoped policies in `schema.sql` apply and a caller sees only
their **own** rows (own-rows-only). None is `SECURITY DEFINER` — that would run as
the object owner and **bypass the caller's RLS**, turning a per-user report into a
cross-user leak. A Postgres view is the sharp edge here: **without**
`security_invoker = true` a view evaluates RLS as its owner (definer-like), so that
flag is load-bearing on both `report_model_pricing` and `report_priced_events`, not
cosmetic. No `service_role` / secret / bypass key is referenced anywhere; reports
run under the same per-user Auth JWT as the writes, and EXECUTE/SELECT is granted
to `authenticated` only (`anon`/PUBLIC revoked, on both views).

## The golden test (`tests/test_report_parity.py`) — the drift guard

A canonical seed corpus (events/sessions/projects/models/pricing spanning today /
this-week / outside-week / backlog windows, tiers, cache-TTL splits, NULLs, an
unpriced model, a model with only estimated (family-default/ancestor) pricing —
proving `models_without_own_price` and the estimated-event counts agree —
prefix-shadowing and `effective_from` supersession) is defined once and loaded
identically into both dialects. The by_model/by_tier tie-break is proven
separately (see "Known cross-dialect nuances" below) since it needs fresh,
purpose-built databases rather than the shared corpus. Three layers:

- **SQLite reference (always, in CI).** The corpus runs through `report.py`'s
  `fetch_*`; known values are asserted. This pins the reference.
- **Mocked client read (always, in CI).** `read_for_report` is driven against a
  mocked RPC whose JSON is built from the SQLite reference — proving the client
  mapping + routing reconstruct the exact shape and render identically, with no
  network, creds or Postgres.
- **Postgres-gated equivalence (integration).** When a local Postgres toolchain
  (`psql`/`createdb`/`dropdb`) is present, a throwaway database gets `schema.sql` +
  `reports.sql`, the same corpus, and each report function is invoked; its output,
  mapped and rendered by the **same** client code, must **equal** the SQLite
  reference. It **skips cleanly** when no Postgres is available and adds no pip
  dependency (subprocess `psql` only).

**Be honest about what CI proves.** CI proves the SQLite reference and the mocked
client. The dual-dialect equivalence runs only where a Postgres is present. The
`schema.sql` RLS enforcement itself (a user truly cannot read another user's rows;
anon reads nothing) is **not** provable in this stdlib suite — it is the
maintainer's live two-user check (`rls-remote-schema.md`).

## The estimated flag's three-way parity (`tests/test_family_default.py`)

The "own price vs estimate" definitions (family default row, ancestor row,
estimated event — see [[pricing-updates]]) are implemented three times, once
per dialect that has to agree on every input: the Python predicates
(`capture.is_family_default`, `capture.is_ancestor_row`,
`capture.is_estimated`), their SQLite boolean-expression twins
(`capture.family_default_sql`, `capture.ancestor_row_sql`,
`capture.estimated_sql`, used by both `report.py` and `dashboard.py`), and the
Postgres expression inlined in `report_priced_events.estimated` above.
`tests/test_family_default.py` is a second, narrower golden test than
`test_report_parity.py`'s: it checks the family-default and ancestor-row
predicates in isolation, across a curated positive/negative case list plus a
randomized sweep of structurally tricky inputs (digits, dashes, case, `LIKE`
wildcards, non-ASCII), asserting the Python result, the SQLite expression
(run against a real `sqlite3` connection) and the Postgres expression (parsed
out of `reports.sql` and run against a real Postgres) all agree. It shares
`test_report_parity.py`'s Postgres-gated pattern — skips cleanly when
`psql`/`createdb`/`dropdb` aren't on `PATH`, and needs the same libpq
connection env (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`) when a
server is reachable. CI provides one via a throwaway `postgres:16-alpine`
service container on port 5433 (`.github/workflows/checks.yml`) so both this
test and `TestPostgresEquivalence` actually run instead of skipping; a
maintainer running locally needs the same client tools and a reachable
Postgres, `TZ=UTC` for date alignment with the SQLite reference, and gets a
clean skip with none of that in place.

## Running the equivalence against your own Supabase (maintainer)

1. Apply `supabase/schema.sql` then `supabase/reports.sql` in the Supabase SQL
   editor (both are re-runnable).
2. Point a client at your project (`active_backend=supabase`, publishable key in
   its env var, `login`), then run `/token-telemetry:token-stats`,
   `/token-telemetry:project-stats`, `/token-telemetry:info`.
3. Compare against the same reports over your local SQLite (`active_backend=local`)
   for a project whose data you migrated. They must match. A divergence means the
   two dialects have drifted — reconcile `reports.sql` to `report.py`.

## Known cross-dialect nuances (kept inside the shared envelope)

- **Timezone.** `/token-stats`' "today" is the reader's local day; Postgres uses
  `date_trunc('day', now())` and SQLite `strftime(...,'localtime','start of day')`.
  They agree only when the Postgres **session TimeZone equals the machine's local
  zone**. The golden test pins both to UTC. The 7-day window is pure arithmetic and
  tz-independent.
- **`LIKE` case-sensitivity.** SQLite `LIKE` is ASCII case-insensitive; Postgres
  `LIKE` is case-sensitive. Model identifiers are lowercase ASCII, so the two
  agree; the corpus stays within that envelope.
- **Pricing ties.** A pricing tie on `(prefix length, effective_from)` is ambiguous
  in **both** dialects (SQLite could even mix columns across rows); the pricing
  UNIQUE key and curated data avoid it.
- **`by_model` / `by_tier` ties order by name.** A tie on `SUM(out_tok)` breaks on
  the model name / tier label, byte order (SQLite's default `ORDER BY ... ,
  model_name` / `, 1`; Postgres `ORDER BY o DESC, model_name COLLATE
  pg_catalog."C"` / `, tier COLLATE pg_catalog."C"` — `COLLATE "C"` is what makes
  the Postgres order match SQLite's regardless of the database's own default
  collation). `tests/test_report_parity.py`'s tie-break tests prove this is
  insertion-order-independent (every permutation in `TIE_PERMUTATIONS` is tried,
  each against a fresh DB) and, on the Postgres side, collation-independent too: a
  `reversed_collation=True` fixture builds a throwaway database with an ICU
  default collation that sorts `b`..`z` backwards (`REVERSED_ICU_RULES`), so a
  missing `COLLATE "C"` would order the tied rows backwards there even though it
  passes on a plain database. This needs a Postgres 16+ server built with ICU
  support (the CI workflow's `postgres:16-alpine` service qualifies); a server
  without it skips that specific case cleanly instead of failing.
- **`/info` schema line.** The remote has no `PRAGMA user_version`; the client
  stamps the modeled remote shape (v7) and the golden test excludes `schema` from
  the equality — it is a store property, not an aggregation.
- **Estimated counts are event-grain only on the remote, so far.** `report_priced_events.estimated`
  exists in the view, but `report_project_stats()`/`report_token_stats()` do
  not yet aggregate it — `_map_project_stats`'s `estimated_events` and
  `_map_token_stats`'s `estimated_by_model`/`models_without_own_price` stay
  `None` on the remote path today (they keep the local shape's keys, just
  unpopulated). A future story that adds the aggregation to `reports.sql`
  needs to update those two mapping functions in the same change, or the
  remote reports will keep reporting "unknown" where local reports don't.
