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
import re
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
          "claude-haiku-5", "gpt-4o", "claude-3-5-haiku-20241022",
          "claude-opus-5-5", "claude-haiku-4-5-20251001"]

# (provider, model_prefix, model_version, in, out, cache_r, cache_w, cache_w_1h,
#  effective_from). Two 'claude-sonnet-' rows (seed@0 and dated@NOW-20d) exercise
# effective_from supersession; 'claude-sonnet-4-5-' (longer) exercises longest-
# prefix shadowing; gpt-4o has NO matching prefix (unpriced); 'claude-3-5-haiku'
# is a legacy-scheme OWN row (not a family default — digits after `claude-`).
# Estimated-flag coverage: the effective_from=0 seed rows and the dated bare
# 'claude-sonnet-' row are family defaults; 'claude-sonnet-4-5-' and
# 'claude-3-5-haiku' are own rows. 'claude-opus-5' is an ANCESTOR row for the
# unlisted successor 'claude-opus-5-5' (R = '-5' -> estimated) and
# 'claude-haiku-4-5' is the OWN row of the date snapshot
# 'claude-haiku-4-5-20251001' (R = '-20251001' -> not estimated).
PRICING = [
    ("anthropic", "claude-sonnet-", "", 3.0, 15.0, 0.30, 3.75, 6.0, 0),
    ("anthropic", "claude-sonnet-", "v2", 4.0, 20.0, 0.40, 5.0, 8.0, NOW - 20 * D),
    ("anthropic", "claude-sonnet-4-5-", "", 3.5, 17.5, 0.35, 4.375, 7.0, NOW - 10 * D),
    ("anthropic", "claude-opus-", "", 5.0, 25.0, 0.50, 6.25, 10.0, 0),
    ("anthropic", "claude-haiku-", "", 1.0, 5.0, 0.10, 1.25, 2.0, 0),
    ("anthropic", "claude-3-5-haiku", "", 0.8, 4.0, 0.08, 1.0, 1.6, NOW - 40 * D),
    ("anthropic", "claude-opus-5", "", 5.5, 27.5, 0.55, 6.875, 11.0, NOW - 40 * D),
    ("anthropic", "claude-haiku-4-5", "", 1.1, 5.5, 0.11, 1.375, 2.2, NOW - 40 * D),
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
    # legacy-prefix model priced by its OWN row -> not estimated
    ("s-b1", "claude-3-5-haiku-20241022", NOW - 4 * D, 0, None, 7000, 1500, 0, 0, 0, None, None),
    # the shadowing model BEFORE its own row takes effect (NOW-10d): resolves to
    # the dated bare 'claude-sonnet-' row -> estimated, though the model HAS an
    # own row (so it is not a "model without own price")
    ("s-b1", "claude-sonnet-4-5-8", NOW - 12 * D, 1, "dev",  6000,  2500,     0,    0,    0, None,    None),
    # unlisted successor priced by its nearest listed ancestor's row
    # ('claude-opus-5', R = '-5') -> estimated; the model has no own price
    ("s-b1", "claude-opus-5-5",     NOW - 5 * D, 0, None,   9000,  3000,   900,  400,  100, None,    None),
    # date snapshot priced by its version's OWN row ('claude-haiku-4-5',
    # R = '-20251001') -> not estimated
    ("s-b1", "claude-haiku-4-5-20251001", NOW - 5 * D, 1, "doc", 4000, 800, 0, 0, 0, None, None),
]

# The estimated flag every event must resolve to, in BOTH dialects — keyed
# (session, model, ts); None = unpriced.
EXPECTED_ESTIMATED = {
    ("s-a1", "claude-sonnet-5", NOW): True,            # dated bare family row
    ("s-a1", "claude-opus-4-8", NOW): True,            # effective_from=0 seed
    ("s-a2", "claude-sonnet-4-5-8", NOW - 3 * D): False,   # own (shadowing) row
    ("s-a2", "claude-haiku-5", NOW - 3 * D): True,     # seed
    ("s-b1", "claude-sonnet-5", NOW - 30 * D): True,   # seed (dated row later)
    ("s-b1", "gpt-4o", NOW - 3 * D): None,             # unpriced
    ("s-b1", "claude-sonnet-5", NOW - 2 * D): True,    # backlog, dated family
    ("s-b1", "claude-3-5-haiku-20241022", NOW - 4 * D): False,  # legacy own row
    ("s-b1", "claude-sonnet-4-5-8", NOW - 12 * D): True,   # own row not yet in force
    ("s-b1", "claude-opus-5-5", NOW - 5 * D): True,    # ancestor row
    ("s-b1", "claude-haiku-4-5-20251001", NOW - 5 * D): False,  # snapshot own
}
EXPECTED_WITHOUT_OWN_PRICE = ["claude-haiku-5", "claude-opus-4-8",
                              "claude-opus-5-5", "claude-sonnet-5", "gpt-4o"]


def sqlite_estimated_by_event(conn):
    """Per-event estimated flag from report.py's resolver, keyed like
    EXPECTED_ESTIMATED (0/1 normalized to bool, NULL kept as None)."""
    rows = conn.execute(
        f"SELECT s.uuid, m.name, e.ts, {report.estimated_subquery()}"
        " FROM events e JOIN models m ON m.id = e.model_id"
        " JOIN sessions s ON s.id = e.session_id").fetchall()
    return {(u, n, ts): (None if f is None else bool(f))
            for u, n, ts, f in rows}


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
        "estimated_by_model": d["estimated_by_model"],
        "events_by_model": d["events_by_model"],
        "unpriced_by_model": d["unpriced_by_model"],
        "models_without_own_price": d["models_without_own_price"],
    }


ESTIMATE_KEYS = ("estimated_by_model", "events_by_model", "unpriced_by_model",
                 "models_without_own_price")


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
        self.assertIn("1 of 7 events unpriced", md)

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

    def test_estimated_per_event_reference(self):
        self.assertEqual(sqlite_estimated_by_event(self.conn),
                         EXPECTED_ESTIMATED)

    def test_estimated_counts_reference(self):
        rows = {r["path"]: r for r in report.fetch_project_stats(self.conn)}
        self.assertEqual(rows["/home/user/alpha"]["estimated_events"], 3)
        self.assertEqual(rows["/home/user/beta"]["estimated_events"], 4)
        self.assertEqual(rows["/home/user/zeta"]["estimated_events"], 0)
        d = report.fetch_token_stats(self.conn)
        # 7-day, backlog-excluded window, like by_model; zero counts omitted
        self.assertEqual(d["estimated_by_model"], {
            "claude-sonnet-5": 1, "claude-opus-4-8": 1, "claude-haiku-5": 1,
            "claude-opus-5-5": 1})
        self.assertEqual(d["models_without_own_price"],
                         EXPECTED_WITHOUT_OWN_PRICE)
        # the per-model event / unpriced counts the cost qualifiers divide by
        self.assertEqual(d["events_by_model"], {
            "claude-sonnet-5": 1, "claude-opus-4-8": 1,
            "claude-sonnet-4-5-8": 1, "claude-haiku-5": 1, "gpt-4o": 1,
            "claude-3-5-haiku-20241022": 1, "claude-opus-5-5": 1,
            "claude-haiku-4-5-20251001": 1})
        self.assertEqual(d["unpriced_by_model"], {"gpt-4o": 1})
        # the scoped-rollup summer counts the same events
        rowids = [r[0] for r in self.conn.execute("SELECT rowid FROM events")]
        s = report.priced_sum_for_rowids(self.conn, rowids)
        self.assertEqual(s["estimated"], sum(
            1 for v in EXPECTED_ESTIMATED.values() if v))
        self.assertEqual(report.priced_sum_for_rowids(self.conn, [])
                         ["estimated"], 0)

    def test_estimate_rendered_reference(self):
        # the rendered qualifiers the Postgres side must reproduce byte-for-byte
        ps = report.render_project_stats(report.fetch_project_stats(self.conn))
        self.assertIn("— 3 of 4 events at an estimated rate |", ps)   # Alpha
        self.assertIn("— 1 of 7 events unpriced, 4 of 7 events at an"
                      " estimated rate |", ps)                         # beta
        ts = report.render_token_stats(report.fetch_token_stats(self.conn))
        # the ancestor-priced unlisted successor: its whole figure estimated
        self.assertIn("| claude-opus-5-5 | heavy | 9,000 | 3,000 | $0.14 —"
                      " the whole figure is an estimate (every event at an"
                      " estimated rate) |", ts)
        # the snapshot priced by its own row: no qualifier
        self.assertIn("| claude-haiku-4-5-20251001 | micro | 4,000 | 800 |"
                      " $0.01 |", ts)
        self.assertIn("; 1 of 8 events unpriced, 4 of 8 events at an"
                      " estimated rate.**", ts)
        self.assertEqual(
            ts.splitlines()[-1],
            "No own published price for `claude-haiku-5`, `claude-opus-4-8`,"
            " `claude-opus-5-5`, `claude-sonnet-5`, `gpt-4o` — their cost is"
            " an estimate (family default or nearest listed ancestor rate) or"
            " unpriced; `/token-telemetry:pricing-update` refreshes the"
            " pricing table.")

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
        # the mapped shape carries the same keys (estimated_events included)
        self.assertEqual(got, report.fetch_project_stats(self.ref))
        self.assertEqual(report.render_project_stats(got),
                         report.render_project_stats(
                             report.fetch_project_stats(self.ref)))

    def test_read_for_report_roundtrips_token_stats(self):
        b = self.backend()
        with mock.patch("urllib.request.urlopen", self.responder):
            got = b.read_for_report("token-stats")
        ref = report.fetch_token_stats(self.ref)
        for k in ESTIMATE_KEYS:
            self.assertEqual(got[k], ref[k], k)
        self.assertEqual(set(got), set(ref))   # identical key set
        self.assertEqual(report.render_token_stats(got),
                         report.render_token_stats(
                             report.fetch_token_stats(self.ref)))

    def test_remote_predating_estimate_figures_renders_without_them(self):
        # a remote whose reports.sql predates the figures: keys map to None,
        # the render says nothing about estimates and still succeeds
        old_ts = {k: v for k, v in self.json["report_token_stats"].items()
                  if k not in ESTIMATE_KEYS}
        ts = supabase_backend._map_token_stats(old_ts)
        for k in ESTIMATE_KEYS:
            self.assertIsNone(ts[k], k)
        md = report.render_token_stats(ts)
        self.assertNotIn("estimated rate", md)
        self.assertNotIn("No own published price", md)
        old_ps = [{k: v for k, v in r.items() if k != "estimated_events"}
                  for r in self.json["report_project_stats"]]
        ps = supabase_backend._map_project_stats(old_ps)
        self.assertTrue(all(r["estimated_events"] is None for r in ps))
        self.assertNotIn("estimated rate", report.render_project_stats(ps))

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
        # the estimate figure itself, per project — not merely its rendering
        self.assertEqual({r["path"]: r["estimated_events"] for r in pg},
                         {r["path"]: r["estimated_events"] for r in ref})
        # non-vacuous: the compared render really carries the qualifiers
        self.assertIn("4 of 7 events at an estimated rate",
                      report.render_project_stats(pg))

    def test_token_stats_equivalent(self):
        pg = supabase_backend._map_token_stats(
            self._rpc("SELECT public.report_token_stats();"))
        ref = report.fetch_token_stats(self.sqlite_ref())
        self.assertEqual(report.render_token_stats(pg),
                         report.render_token_stats(ref))
        for k in ESTIMATE_KEYS:
            self.assertEqual(pg[k], ref[k], k)
        self.assertEqual(pg["models_without_own_price"],
                         EXPECTED_WITHOUT_OWN_PRICE)   # incl. claude-opus-5-5
        self.assertEqual(pg["estimated_by_model"]["claude-opus-5-5"], 1)
        md = report.render_token_stats(pg)
        self.assertIn("No own published price for", md)
        self.assertIn("4 of 8 events at an estimated rate", md)

    def test_reapplying_reports_sql_is_safe(self):
        # drop-then-create: applying the file again over itself must succeed
        # and leave the report functions answering identically
        before = self._rpc("SELECT public.report_token_stats();")
        self._run_sql_file(self.dbname, REPORTS_SQL)
        self.assertEqual(self._rpc("SELECT public.report_token_stats();"),
                         before)

    def test_estimated_per_event_equivalent(self):
        # The view's `estimated` must resolve from the SAME pricing row as
        # report.py's resolver, event for event — seed rows, a dated bare
        # family row, an own row, the legacy 'claude-3-5-haiku' own row, the
        # shadowing own row before and after it takes effect, and an unpriced
        # model.
        rows = self._rpc(
            "SELECT coalesce(json_agg(json_build_array(session_uuid,"
            " model_name, ts, estimated)), '[]'::json)"
            " FROM public.report_priced_events;")
        pg = {(u, n, ts): f for u, n, ts, f in rows}
        self.assertEqual(pg, sqlite_estimated_by_event(self.sqlite_ref()))
        self.assertEqual(pg, EXPECTED_ESTIMATED)

    def test_info_equivalent(self):
        pg = supabase_backend._map_info(
            self._rpc("SELECT public.report_info('/home/user/alpha');"))
        ref = report.fetch_info_central(self.sqlite_ref(), "/home/user/alpha")
        for k in ("events", "projects", "pricing_rows", "latest_rate_from",
                  "first_day", "last_day", "events_here"):
            self.assertEqual(pg[k], ref[k], k)


OWNER2 = "22222222-2222-4222-8222-222222222222"

# auth.uid() driven by a session setting, so the test can act as a given user
# under the `authenticated` role — where the schema.sql RLS policies APPLY
# (unlike the superuser connection above, which bypasses them).
PG_RLS_SHIM = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE
  AS $$ SELECT nullif(current_setting('test.uid', true), '')::uuid $$;
DO $$ BEGIN CREATE ROLE authenticated; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE anon; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
GRANT USAGE ON SCHEMA auth TO authenticated;
SET TIME ZONE 'UTC';
"""


def pg_intruder_sql():
    """A second user's rows: a project, a model with NO own price and an
    event in the 7-day window, plus an OWN pricing row for the first user's
    ancestor-priced 'claude-opus-5-5'. If any estimate figure leaked across
    owners, the first user's report would change (an extra model without own
    price, or 'claude-opus-5-5' losing its estimate)."""
    o = _sql_lit(OWNER2)
    return "\n".join([
        f"INSERT INTO public.users(uuid, name, created_at) VALUES ({o}, 'Other', {NOW});",
        f"INSERT INTO public.projects(owner_id, path, name) VALUES ({o}, '/home/user/secret', 'Secret');",
        f"INSERT INTO public.models(owner_id, name) VALUES ({o}, 'claude-intruder-9');",
        f"INSERT INTO public.models(owner_id, name) VALUES ({o}, 'claude-opus-5-5');",
        "INSERT INTO public.pricing(owner_id, provider, model_prefix, model_version,"
        " in_usd, out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,"
        f" effective_from, source) VALUES ({o}, 'anthropic', 'claude-opus-5-5',"
        " '', 9, 9, 9, 9, 9, 0, 'test');",
        f"INSERT INTO public.sessions(owner_id, uuid, project_path) VALUES ({o}, 's-x1', '/home/user/secret');",
        "INSERT INTO public.events(owner_id, session_uuid, model_name, ts, kind,"
        " agent, in_tok, out_tok, cache_r, cache_w, cache_w_1h, issue_key, note)"
        f" VALUES ({o}, 's-x1', 'claude-intruder-9', {NOW - D}, 0, NULL, 777,"
        " 999999, 0, 0, 0, NULL, NULL);",
        "INSERT INTO public.events(owner_id, session_uuid, model_name, ts, kind,"
        " agent, in_tok, out_tok, cache_r, cache_w, cache_w_1h, issue_key, note)"
        f" VALUES ({o}, 's-x1', 'claude-opus-5-5', {NOW - D}, 0, NULL, 5, 5,"
        " 0, 0, 0, NULL, NULL);",
    ])


@unittest.skipUnless(PG_TOOLS, PG_REASON)
class TestPostgresEstimateRls(unittest.TestCase):
    """The estimate figures read only through RLS: acting as one user under
    the `authenticated` role, the report functions return exactly the SQLite
    reference for that user's corpus, and another user's rows (models without
    own price, own pricing rows, events) never reach it."""

    @classmethod
    def setUpClass(cls):
        cls.dbname = f"tt_rls_{os.getpid()}"
        try:
            subprocess.run(["createdb", cls.dbname], check=True,
                           capture_output=True, env=os.environ.copy(),
                           timeout=30)
        except Exception as exc:
            raise unittest.SkipTest(f"createdb unavailable: {exc}")
        cls._prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        TestPostgresEquivalence._run_sql_file(cls.dbname, "\n".join(
            [PG_RLS_SHIM, SCHEMA_SQL, REPORTS_SQL, pg_corpus_sql(),
             pg_intruder_sql()]))

    @classmethod
    def tearDownClass(cls):
        if cls._prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._prev_tz
        time.tzset()
        subprocess.run(["dropdb", "--if-exists", cls.dbname],
                       capture_output=True, env=os.environ.copy(), timeout=30)

    def _rpc_as(self, uid, sql):
        r = subprocess.run(
            ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-d", self.dbname, "-c", "SET ROLE authenticated",
             "-c", f"SET test.uid = '{uid}'", "-c", sql],
            capture_output=True, text=True, env=os.environ.copy(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip())

    def sqlite_ref(self):
        tmp = tempfile.mkdtemp()
        return build_sqlite_corpus(pathlib.Path(tmp) / "usage.db")

    def test_owner_sees_only_own_estimate_figures(self):
        ref_conn = self.sqlite_ref()
        ts = supabase_backend._map_token_stats(
            self._rpc_as(OWNER, "SELECT public.report_token_stats();"))
        ref = report.fetch_token_stats(ref_conn)
        for k in ESTIMATE_KEYS:
            self.assertEqual(ts[k], ref[k], k)
        self.assertNotIn("claude-intruder-9", ts["models_without_own_price"])
        self.assertIn("claude-opus-5-5", ts["models_without_own_price"])
        self.assertEqual(report.render_token_stats(ts),
                         report.render_token_stats(ref))
        ps = supabase_backend._map_project_stats(
            self._rpc_as(OWNER, "SELECT public.report_project_stats();"))
        self.assertEqual(report.render_project_stats(ps),
                         report.render_project_stats(
                             report.fetch_project_stats(ref_conn)))

    def test_other_owner_sees_only_theirs(self):
        ts = supabase_backend._map_token_stats(
            self._rpc_as(OWNER2, "SELECT public.report_token_stats();"))
        # their own 'claude-opus-5-5' row is an own price for THEM only
        self.assertEqual(ts["models_without_own_price"], ["claude-intruder-9"])
        self.assertEqual(ts["estimated_by_model"], {})
        self.assertEqual(ts["events_by_model"],
                         {"claude-intruder-9": 1, "claude-opus-5-5": 1})
        self.assertEqual(ts["unpriced_by_model"], {"claude-intruder-9": 1})
        ps = supabase_backend._map_project_stats(
            self._rpc_as(OWNER2, "SELECT public.report_project_stats();"))
        self.assertEqual([r["path"] for r in ps], ["/home/user/secret"])
        self.assertEqual(ps[0]["estimated_events"], 0)

    def test_no_identity_sees_nothing(self):
        ts = supabase_backend._map_token_stats(
            self._rpc_as("", "SELECT public.report_token_stats();"))
        for k in ESTIMATE_KEYS:
            self.assertFalse(ts[k], k)


class TestReportsSecurityHardening(unittest.TestCase):
    """Always-on structural guards on supabase/reports.sql (no Postgres needed).
    They pin the Supabase security posture its linter checks on a real instance
    (verified live 2026-09-22): every report function is SECURITY INVOKER — never
    DEFINER, which would run as the object owner and BYPASS the caller's RLS — and
    pins search_path (the function_search_path_mutable advisor) so a hijacked
    search_path cannot resolve an unqualified name to an attacker's object."""

    def test_no_security_definer_in_code(self):
        # a DEFINER function would run as the owner and bypass the caller's RLS.
        # Strip SQL comment lines first — the header prose legitimately explains
        # why DEFINER is avoided, and that mention must not trip this guard.
        code = "\n".join(l for l in REPORTS_SQL.splitlines()
                         if not l.lstrip().startswith("--"))
        self.assertNotIn("SECURITY DEFINER", code)

    def test_every_report_function_is_invoker_and_pins_search_path(self):
        # the exact INVOKER+pinned-search_path pairing appears once per function
        # (3); the header's prose mention of "SECURITY INVOKER" won't match this.
        self.assertEqual(
            REPORTS_SQL.count("SECURITY INVOKER\nSET search_path = ''"), 3)

    def test_every_view_is_security_invoker_and_authenticated_only(self):
        # a view without security_invoker evaluates RLS as its OWNER; each
        # view is created with the flag and granted to `authenticated` only
        views = re.findall(r"CREATE VIEW (public\.\w+)\n  WITH "
                           r"\(security_invoker = true\) AS", REPORTS_SQL)
        self.assertEqual(len(views), REPORTS_SQL.count("CREATE VIEW"))
        self.assertEqual(sorted(views), ["public.report_model_pricing",
                                         "public.report_priced_events"])
        for v in views:
            self.assertIn(f"GRANT SELECT ON {v} TO authenticated;",
                          REPORTS_SQL)
            self.assertIn(f"REVOKE ALL ON {v} FROM PUBLIC, anon;",
                          REPORTS_SQL)
            self.assertIn(f"DROP VIEW IF EXISTS {v};", REPORTS_SQL)


if __name__ == "__main__":
    unittest.main()
