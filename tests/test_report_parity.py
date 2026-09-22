"""Cross-dialect report parity — the drift guard for AOS-104 P9.

The client renders `/token-stats`, `/project-stats` and `/info` from the SAME
aggregations whether the data is local (SQLite, `scripts/report.py`, the REFERENCE
dialect) or remote (Postgres, `supabase/reports.sql`, invoked over PostgREST RPC).
Two dialects of the same query can silently drift; this file is the tripwire.

Three layers, from always-on to environment-gated:

  1. **SQLite reference (always, in CI).** A canonical seed corpus is loaded into a
     real SQLite DB and run through `report.py`'s `fetch_*`; known values are
     asserted. This pins the reference the Postgres side must match.

  2. **Mocked client read (always, in CI).** `SupabaseBackend.read_for_report` is
     driven against a MOCKED PostgREST RPC whose JSON is built from the SQLite
     reference in the shape `reports.sql` documents. It proves the client mapping
     + routing reconstruct the exact `fetch_*` shape and render identically — with
     no network, no real creds, no Postgres.

  3. **Postgres-gated equivalence (integration).** When a local Postgres toolchain
     is present, a throwaway database gets `schema.sql` + `reports.sql`, the SAME
     corpus, and the report functions are invoked; their output, mapped by the
     SAME client mapper and rendered by the SAME renderer, must EQUAL the SQLite
     reference. SKIPS cleanly when no Postgres is available — it never fails CI for
     lack of one, and adds no pip dependency (psql via subprocess only). The
     maintainer runs the same equivalence against their live Supabase (handbook).
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import report
import settings
import storage
import supabase_backend

REPO = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_SQL = (REPO / "supabase" / "schema.sql").read_text()
REPORTS_SQL = (REPO / "supabase" / "reports.sql").read_text()

# ---------------------------------------------------------------------------
# The canonical seed corpus — ONE definition, loaded identically into SQLite
# (integer-keyed) and Postgres (owner/natural-keyed). It spans multiple windows
# (today / this-week / outside-week / backlog), tiers, cache types, NULLs, an
# unpriced model, prefix-shadowing and effective_from supersession.
# ---------------------------------------------------------------------------
OWNER = "11111111-1111-4111-8111-111111111111"
NOW = int(time.time())
D = 86400

# (path, name)
PROJECTS = [
    ("/home/user/alpha", "Alpha"),
    ("/home/user/beta", None),       # no registered name -> basename fallback
    ("/home/user/zeta", "Zeta"),     # no sessions/events at all (LEFT-JOIN zero path)
]

MODELS = ["claude-sonnet-5", "claude-sonnet-4-5-8", "claude-opus-4-8",
          "claude-haiku-5", "gpt-4o"]

# (provider, model_prefix, model_version, in, out, cache_r, cache_w, cache_w_1h,
#  effective_from). Two 'claude-sonnet-' rows (seed@0 and dated@NOW-20d) exercise
# effective_from supersession; 'claude-sonnet-4-5-' (longer) exercises longest-
# prefix shadowing; gpt-4o has NO matching prefix (unpriced).
PRICING = [
    ("anthropic", "claude-sonnet-", "", 3.0, 15.0, 0.30, 3.75, 6.0, 0),
    ("anthropic", "claude-sonnet-", "v2", 4.0, 20.0, 0.40, 5.0, 8.0, NOW - 20 * D),
    ("anthropic", "claude-sonnet-4-5-", "", 3.5, 17.5, 0.35, 4.375, 7.0, NOW - 10 * D),
    ("anthropic", "claude-opus-", "", 5.0, 25.0, 0.50, 6.25, 10.0, 0),
    ("anthropic", "claude-haiku-", "", 1.0, 5.0, 0.10, 1.25, 2.0, 0),
]

# (uuid, project_path)
SESSIONS = [("s-a1", "/home/user/alpha"), ("s-a2", "/home/user/alpha"),
            ("s-b1", "/home/user/beta")]

# (session_uuid, model_name, ts, kind, agent, in, out, cr, cw, cw1h,
#  issue_key, note)
EVENTS = [
    ("s-a1", "claude-sonnet-5",     NOW,        0, None,  100000, 50000, 10000, 5000, 5000, "AOS-1", None),
    ("s-a1", "claude-opus-4-8",     NOW,        1, "dev",  20000,  8000,  2000, 1000,    0, "AOS-1", None),
    ("s-a2", "claude-sonnet-4-5-8", NOW - 3 * D, 0, None,  40000, 30000,  4000, 2000, 1000, "AOS-2", None),
    ("s-a2", "claude-haiku-5",      NOW - 3 * D, 1, "doc",  5000,  1000,   500,    0,    0, None,    None),
    ("s-b1", "claude-sonnet-5",     NOW - 30 * D, 0, None, 200000, 90000,     0,    0,    0, "AOS-3", None),
    ("s-b1", "gpt-4o",              NOW - 3 * D, 0, None,   1000,  2000,     0,    0,    0, None,    None),
    ("s-b1", "claude-sonnet-5",     NOW - 2 * D, 0, None,   3000,  3000,     0,    0,    0, None, "backlog-capture"),
]


def build_sqlite_corpus(path):
    """Load the corpus into a fresh v7 SQLite DB (integer-keyed), returning a
    read-only connection. The seed pricing `capture.connect` installs is cleared
    so the corpus pricing is authoritative and matches the remote exactly."""
    conn = capture.connect(path)
    conn.execute("DELETE FROM pricing")
    conn.execute("INSERT INTO users(uuid, name, created_at) VALUES (?,?,?)",
                 (OWNER, "Tester", NOW))
    pid = {}
    for p, name in PROJECTS:
        pid[p] = conn.execute(
            "INSERT INTO projects(path, name) VALUES (?,?)", (p, name)).lastrowid
    mid = {}
    for m in MODELS:
        mid[m] = conn.execute(
            "INSERT INTO models(name) VALUES (?)", (m,)).lastrowid
    for row in PRICING:
        conn.execute(
            "INSERT INTO pricing(provider, model_prefix, model_version, in_usd,"
            " out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
            " source) VALUES (?,?,?,?,?,?,?,?,?,?)", (*row, "test"))
    sid = {}
    for uuid, ppath in SESSIONS:
        sid[uuid] = conn.execute(
            "INSERT INTO sessions(uuid, project_id, owner_id) VALUES (?,?,?)",
            (uuid, pid[ppath], OWNER)).lastrowid
    for (su, model, ts, kind, agent, itok, otok, cr, cw, cw1h,
         issue, note) in EVENTS:
        conn.execute(
            "INSERT INTO events(ts, session_id, kind, agent, model_id, in_tok,"
            " out_tok, cache_r, cache_w, cache_w_1h, dur_ms, branch, commit_sha,"
            " issue_key, task_size, note, api_calls, ctx_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, sid[su], kind, agent, mid[model], itok, otok, cr, cw, cw1h,
             None, None, None, issue, None, note, None, None))
    conn.commit()
    conn.close()
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _sql_lit(v):
    """A safe SQL literal for the corpus loader (test-only, fixed corpus)."""
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def pg_corpus_sql():
    """The corpus as owner/natural-keyed Postgres INSERTs (schema.sql shape)."""
    out = [f"INSERT INTO public.users(uuid, name, created_at) VALUES "
           f"({_sql_lit(OWNER)}, 'Tester', {NOW});"]
    for p, name in PROJECTS:
        out.append(f"INSERT INTO public.projects(owner_id, path, name) VALUES "
                   f"({_sql_lit(OWNER)}, {_sql_lit(p)}, {_sql_lit(name)});")
    for m in MODELS:
        out.append(f"INSERT INTO public.models(owner_id, name) VALUES "
                   f"({_sql_lit(OWNER)}, {_sql_lit(m)});")
    for (prov, prefix, ver, i, o, cr, cw, cw1h, eff) in PRICING:
        out.append(
            "INSERT INTO public.pricing(owner_id, provider, model_prefix,"
            " model_version, in_usd, out_usd, cache_r_usd, cache_w_usd,"
            " cache_w_1h_usd, effective_from, source) VALUES ("
            f"{_sql_lit(OWNER)}, {_sql_lit(prov)}, {_sql_lit(prefix)},"
            f" {_sql_lit(ver)}, {i}, {o}, {cr}, {cw}, {cw1h}, {eff}, 'test');")
    for uuid, ppath in SESSIONS:
        out.append(
            "INSERT INTO public.sessions(owner_id, uuid, project_path) VALUES "
            f"({_sql_lit(OWNER)}, {_sql_lit(uuid)}, {_sql_lit(ppath)});")
    for (su, model, ts, kind, agent, itok, otok, cr, cw, cw1h,
         issue, note) in EVENTS:
        out.append(
            "INSERT INTO public.events(owner_id, session_uuid, model_name, ts,"
            " kind, agent, in_tok, out_tok, cache_r, cache_w, cache_w_1h,"
            " issue_key, note) VALUES ("
            f"{_sql_lit(OWNER)}, {_sql_lit(su)}, {_sql_lit(model)}, {ts},"
            f" {kind}, {_sql_lit(agent)}, {itok}, {otok}, {cr}, {cw}, {cw1h},"
            f" {_sql_lit(issue)}, {_sql_lit(note)});")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Bridge: turn the SQLite reference fetch output into the JSON shape reports.sql
# emits. Used by the ALWAYS-ON mocked test to feed read_for_report a faithful RPC
# response without a live Postgres (so the mapping is exercised over real corpus
# values, not hand-picked ones).
# ---------------------------------------------------------------------------
def project_stats_to_json(rows):
    return [dict(r) for r in rows]


def token_stats_to_json(d):
    return {
        "today": list(d["today"]), "week": list(d["week"]),
        "backlog_excluded": d["backlog_excluded"],
        # by_project JSON carries raw [path, name, ...]; the client applies the
        # basename fallback. The reference fetch already resolved the label, so
        # re-emit it as both path and name=None to round-trip the same label.
        "by_project": [[r[0], None, r[1], r[2], r[3], r[4], r[5]]
                       for r in d["by_project"]],
        "by_agent": [list(r) for r in d["by_agent"]],
        "by_model": [list(r) for r in d["by_model"]],
        "by_kind": [list(r) for r in d["by_kind"]],
        "by_tier": [list(r) for r in d["by_tier"]],
        "by_issue": [list(r) for r in d["by_issue"]],
    }


def info_to_json(central):
    c = dict(central)
    c.pop("schema", None)  # schema has no remote analogue
    return c


class TestSqliteReference(unittest.TestCase):
    """Layer 1 — the reference the Postgres side must match, asserted in CI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = build_sqlite_corpus(
            pathlib.Path(self.tmp.name) / "usage.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_project_stats_reference(self):
        md = report.render_project_stats(report.fetch_project_stats(self.conn))
        self.assertIn("| Alpha |", md)
        self.assertIn("| beta |", md)     # basename fallback (name is NULL)
        self.assertIn("| Zeta |", md)     # zero-event project still appears
        # zero-event project renders em-dashes for the cost cells...
        self.assertIn("| Zeta | 0 | 0 |", md)
        self.assertIn("| — | — | — |", md)
        # ...and beta's one unpriced (gpt-4o) event is surfaced, not hidden.
        self.assertIn("1 of 3 events unpriced", md)

    def test_token_stats_reference_today_window(self):
        d = report.fetch_token_stats(self.conn)
        # today = the two NOW events (100k+20k in, 50k+8k out), backlog excluded.
        self.assertEqual(d["today"], (120000, 58000, 12000, 6000, 2))
        self.assertEqual(d["backlog_excluded"], 1)

    def test_token_stats_reference_prefix_and_tiers(self):
        md = report.render_token_stats(report.fetch_token_stats(self.conn))
        self.assertIn("| claude-sonnet-4-5-8 | small |", md)  # longest-prefix tier
        self.assertIn("| gpt-4o | unknown |", md)             # unpriced model tier
        self.assertIn("unpriced", md)                         # gpt-4o cost cell
        self.assertIn("| AOS-3 |", md)                        # all-time by-issue

    def test_info_reference_counts(self):
        c = report.fetch_info_central(self.conn, "/home/user/alpha")
        self.assertEqual(c["events"], len(EVENTS))
        self.assertEqual(c["projects"], len(PROJECTS))
        self.assertEqual(c["pricing_rows"], len(PRICING))
        self.assertEqual(c["events_here"], 4)   # alpha's four events

    def test_local_backend_read_for_report_matches_fetch(self):
        # The local sibling of the read seam runs the same SQL as report.py.
        db = self.tmp.name + "/usage.db"
        b = storage.LocalSqliteBackend(db)
        self.assertEqual(b.read_for_report("project-stats"),
                         report.fetch_project_stats(self.conn))
        self.assertEqual(b.read_for_report("token-stats"),
                         report.fetch_token_stats(self.conn))
        self.assertEqual(b.read_for_report("info", project_path="/home/user/alpha"),
                         report.fetch_info_central(self.conn, "/home/user/alpha"))
        # a store that does not exist reads as None, never raises
        self.assertIsNone(
            storage.LocalSqliteBackend(self.tmp.name + "/absent.db")
            .read_for_report("info"))


# ---------------------------------------------------------------------------
# Layer 2 — mocked client read (always). No network, no Postgres, no real creds.
# ---------------------------------------------------------------------------
URL = "https://example.supabase.co"
KEY_ENV = "TOKEN_TELEMETRY_SUPABASE_KEY_PARITYVAR"


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return 200

    status = 200


class TestMockedClientRead(unittest.TestCase):
    """The RPC JSON (built from the SQLite reference) maps back to the exact
    fetch_* shape and renders identically to the local path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(
            pathlib.Path(self.tmp.name) / "telemetry" / "usage.db")
        self._prev_key = os.environ.get(KEY_ENV)
        os.environ[KEY_ENV] = "SENTINEL_KEY"
        self._prev_sess = os.environ.get(supabase_backend.SESSION_ENV)
        os.environ[supabase_backend.SESSION_ENV] = "SENTINEL_TOKEN"
        self.ref = build_sqlite_corpus(
            pathlib.Path(self.tmp.name) / "ref.db")
        self.json = {
            "report_project_stats": project_stats_to_json(
                report.fetch_project_stats(self.ref)),
            "report_token_stats": token_stats_to_json(
                report.fetch_token_stats(self.ref)),
            "report_info": info_to_json(
                report.fetch_info_central(self.ref, "/home/user/alpha")),
        }

    def tearDown(self):
        self.ref.close()
        for name, prev in (("TOKEN_TELEMETRY_DB", self._prev_db),
                           (KEY_ENV, self._prev_key),
                           (supabase_backend.SESSION_ENV, self._prev_sess)):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        self.tmp.cleanup()

    def responder(self, req, timeout=None, context=None):
        for fn, payload in self.json.items():
            if req.full_url.endswith(f"/rpc/{fn}"):
                return _FakeResp(json.dumps(payload).encode())
        return _FakeResp(b"null")

    def backend(self):
        return supabase_backend.SupabaseBackend(
            {"url": URL, "publishable_key_env": KEY_ENV})

    def test_read_for_report_roundtrips_project_stats(self):
        b = self.backend()
        with mock.patch("urllib.request.urlopen", self.responder):
            got = b.read_for_report("project-stats")
        self.assertEqual(report.render_project_stats(got),
                         report.render_project_stats(
                             report.fetch_project_stats(self.ref)))

    def test_read_for_report_roundtrips_token_stats(self):
        b = self.backend()
        with mock.patch("urllib.request.urlopen", self.responder):
            got = b.read_for_report("token-stats")
        self.assertEqual(report.render_token_stats(got),
                         report.render_token_stats(
                             report.fetch_token_stats(self.ref)))

    def test_read_for_report_maps_info(self):
        b = self.backend()
        with mock.patch("urllib.request.urlopen", self.responder):
            got = b.read_for_report("info", project_path="/home/user/alpha")
        ref = report.fetch_info_central(self.ref, "/home/user/alpha")
        for k in ("events", "projects", "pricing_rows", "latest_rate_from",
                  "first_day", "last_day", "events_here"):
            self.assertEqual(got[k], ref[k], k)
        self.assertEqual(got["schema"], supabase_backend.REMOTE_SCHEMA_SHAPE)

    def test_rpc_uses_auth_headers_and_no_secret_in_url(self):
        calls = []

        def rec(req, timeout=None, context=None):
            calls.append({"url": req.full_url,
                          "headers": {k.lower(): v
                                      for k, v in req.header_items()}})
            return self.responder(req, timeout, context)

        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.read_for_report("token-stats")
        c = calls[0]
        self.assertTrue(c["url"].startswith("https://"))
        self.assertTrue(c["url"].endswith("/rest/v1/rpc/report_token_stats"))
        self.assertEqual(c["headers"]["apikey"], "SENTINEL_KEY")
        self.assertEqual(c["headers"]["authorization"], "Bearer SENTINEL_TOKEN")
        self.assertNotIn("SENTINEL_KEY", c["url"])
        self.assertNotIn("SENTINEL_TOKEN", c["url"])


class TestReportRouting(unittest.TestCase):
    """report.py routes the three aggregation reports to the remote backend when
    active_backend=supabase, and leaves the local path untouched otherwise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self._prev_db = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.root / "telemetry" / "usage.db")
        self._prev_key = os.environ.get(KEY_ENV)
        os.environ[KEY_ENV] = "SENTINEL_KEY"
        self._prev_sess = os.environ.get(supabase_backend.SESSION_ENV)
        os.environ[supabase_backend.SESSION_ENV] = "SENTINEL_TOKEN"
        self.ref = build_sqlite_corpus(self.root / "ref.db")
        self.json = {
            "report_project_stats": project_stats_to_json(
                report.fetch_project_stats(self.ref)),
            "report_token_stats": token_stats_to_json(
                report.fetch_token_stats(self.ref)),
            "report_info": info_to_json(
                report.fetch_info_central(self.ref, str(self.root))),
        }

    def tearDown(self):
        self.ref.close()
        for name, prev in (("TOKEN_TELEMETRY_DB", self._prev_db),
                           (KEY_ENV, self._prev_key),
                           (supabase_backend.SESSION_ENV, self._prev_sess)):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        self.tmp.cleanup()

    def responder(self, req, timeout=None, context=None):
        for fn, payload in self.json.items():
            if req.full_url.endswith(f"/rpc/{fn}"):
                return _FakeResp(json.dumps(payload).encode())
        return _FakeResp(b"null")

    def run_report(self, argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report.main(argv)
        return out.getvalue()

    def test_supabase_active_routes_token_stats_to_remote(self):
        settings.ensure_identity("Tester")
        settings.set_supabase_config(URL, KEY_ENV)
        settings.set_active_backend("supabase")
        with mock.patch("urllib.request.urlopen", self.responder):
            out = self.run_report(["token-stats", "--cwd", str(self.root)])
        # the remote (corpus) view, not the empty local store
        self.assertIn("| claude-sonnet-4-5-8 | small |", out)
        self.assertIn("**By tier (7 days)**", out)

    def test_local_default_does_not_touch_network(self):
        settings.ensure_identity("Tester")   # active_backend defaults to local
        calls = []

        def rec(req, timeout=None, context=None):
            calls.append(req.full_url)
            return self.responder(req, timeout, context)

        with mock.patch("urllib.request.urlopen", rec):
            out = self.run_report(["token-stats", "--cwd", str(self.root)])
        self.assertEqual(calls, [])   # local path never hits the network
        # local store is empty -> the standard no-telemetry message
        self.assertIn("No telemetry has been recorded yet", out)

    def test_remote_read_failure_degrades_without_stacktrace(self):
        settings.ensure_identity("Tester")
        settings.set_supabase_config(URL, KEY_ENV)
        settings.set_active_backend("supabase")

        def boom(req, timeout=None, context=None):
            import urllib.error
            raise urllib.error.URLError("mock unreachable")

        with mock.patch("urllib.request.urlopen", boom):
            out = self.run_report(["token-stats", "--cwd", str(self.root)])
        self.assertIn("remote telemetry backend could not be read", out)


# ---------------------------------------------------------------------------
# Layer 3 — Postgres-gated equivalence (integration). Skips cleanly with no PG.
# ---------------------------------------------------------------------------
PG_TOOLS = all(shutil.which(t) for t in ("psql", "createdb", "dropdb"))
PG_REASON = ("no local Postgres toolchain (psql/createdb/dropdb) — the "
             "SQLite<->Postgres equivalence is a maintainer/CI-with-Postgres "
             "step; the SQLite reference and mocked client read still run")

# Shims so the Supabase-flavoured schema.sql applies to a plain Postgres: the
# `auth.uid()` function and the `authenticated`/`anon` roles it references. The
# test connects as a superuser, which BYPASSES RLS, so the SECURITY INVOKER
# functions see every corpus row (== report.py, which has no RLS) — the RLS
# ENFORCEMENT itself is the maintainer's live two-user check, not this one.
PG_SHIM = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE
  AS $$ SELECT NULL::uuid $$;
DO $$ BEGIN CREATE ROLE authenticated; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE anon; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
SET TIME ZONE 'UTC';
"""


@unittest.skipUnless(PG_TOOLS, PG_REASON)
class TestPostgresEquivalence(unittest.TestCase):
    """Apply schema.sql + reports.sql to a throwaway Postgres, load the SAME
    corpus, invoke the report functions, and assert their output (mapped +
    rendered by the SAME client code) EQUALS the SQLite reference."""

    @classmethod
    def setUpClass(cls):
        cls.dbname = f"tt_golden_{os.getpid()}"
        env = os.environ.copy()
        try:
            subprocess.run(["createdb", cls.dbname], check=True,
                           capture_output=True, env=env, timeout=30)
        except Exception as exc:   # no reachable server / no permission
            raise unittest.SkipTest(f"createdb unavailable: {exc}")
        # UTC for BOTH dialects so date(ts,'localtime') aligns across dialects.
        cls._prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        script = "\n".join([PG_SHIM, SCHEMA_SQL, REPORTS_SQL, pg_corpus_sql()])
        cls._run_sql_file(cls.dbname, script)

    @classmethod
    def tearDownClass(cls):
        if cls._prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._prev_tz
        time.tzset()
        subprocess.run(["dropdb", "--if-exists", cls.dbname],
                       capture_output=True, env=os.environ.copy(), timeout=30)

    @classmethod
    def _run_sql_file(cls, dbname, sql):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write(sql)
            path = f.name
        try:
            r = subprocess.run(
                ["psql", "-v", "ON_ERROR_STOP=1", "-q", "-d", dbname, "-f", path],
                capture_output=True, text=True, env=os.environ.copy(), timeout=60)
            if r.returncode != 0:
                raise AssertionError(f"psql apply failed:\n{r.stderr}")
        finally:
            os.unlink(path)

    def _rpc(self, sql):
        r = subprocess.run(
            ["psql", "-X", "-A", "-t", "-d", self.dbname, "-c", sql],
            capture_output=True, text=True, env=os.environ.copy(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip())

    def sqlite_ref(self):
        tmp = tempfile.mkdtemp()
        return build_sqlite_corpus(pathlib.Path(tmp) / "usage.db")

    def test_project_stats_equivalent(self):
        pg = supabase_backend._map_project_stats(
            self._rpc("SELECT public.report_project_stats();"))
        ref = report.fetch_project_stats(self.sqlite_ref())
        self.assertEqual(report.render_project_stats(pg),
                         report.render_project_stats(ref))

    def test_token_stats_equivalent(self):
        pg = supabase_backend._map_token_stats(
            self._rpc("SELECT public.report_token_stats();"))
        ref = report.fetch_token_stats(self.sqlite_ref())
        self.assertEqual(report.render_token_stats(pg),
                         report.render_token_stats(ref))

    def test_info_equivalent(self):
        pg = supabase_backend._map_info(
            self._rpc("SELECT public.report_info('/home/user/alpha');"))
        ref = report.fetch_info_central(self.sqlite_ref(), "/home/user/alpha")
        for k in ("events", "projects", "pricing_rows", "latest_rate_from",
                  "first_day", "last_day", "events_here"):
            self.assertEqual(pg[k], ref[k], k)


if __name__ == "__main__":
    unittest.main()
