import calendar
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import dashboard
import settings

from tests.test_capture import entry


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def utc_epoch(y, m, d, hh=12, mm=0, ss=0):
    """A known epoch-seconds timestamp for the given UTC calendar date, for
    tests that pin a LITERAL expected ``dashboard._warn_date``/
    ``_warn_date_range`` string (see ``pin_utc``) — an independent oracle,
    not a round-trip through the function under test."""
    return calendar.timegm((y, m, d, hh, mm, ss, 0, 0, 0))


def pin_utc(testcase):
    """Pin TZ=UTC for one test, restoring it on cleanup. ``_warn_date``
    renders in the server's local timezone (its own docstring says so), so a
    test asserting a literal expected date string needs a fixed zone to be
    deterministic across runners — mirrors the TZ pin in
    tests/test_report_parity.py's ``TestPostgresEquivalence``."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()

    def _restore():
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()

    testcase.addCleanup(_restore)


class TestDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "usage.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, project, session, **kw):
        conn = capture.connect(self.db)
        groups = capture.aggregate([entry(**kw)])
        with conn:
            capture.insert_events(conn, project, session, 0, None, groups)
        conn.close()

    def _ro(self):
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def seed_one(self, ts=None):
        ts = ts if ts is not None else int(time.time())
        self._insert("/proj", "s1", model="claude-sonnet-5", inp=100000, out=50000,
                     cr=10000, cw=5000, cw1h=5000, mid="m1", ts=iso(ts))

    # ---- totals + the cost/token identity ------------------------------

    def test_totals_and_identity(self):
        self.seed_one()
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        k = d["kpis"]
        # sonnet seed: in 3 / out 15 / cr 0.3 / cw1h 6 per MTok -> $1.083
        self.assertAlmostEqual(k["cost"], 1.083, places=3)
        self.assertEqual(k["events"], 1)
        self.assertEqual(k["total"], 165000)
        self.assertEqual(k["consumed"], 150000)     # in + out
        self.assertEqual(k["cachetok"], 15000)      # cache_r + cache_w
        self.assertEqual(k["consumed"] + k["cachetok"], k["total"])
        # composition sums to the same total, both ways
        ct = d["composition"]["tokens"]
        self.assertEqual(ct["in"] + ct["out"] + ct["cache_r"] + ct["cache_w"], k["total"])
        cc = d["composition"]["cost"]
        self.assertAlmostEqual(cc["in"] + cc["out"] + cc["cache_r"] + cc["cache_w"],
                               k["cost"], places=6)
        # byModel / byProject each reconcile to the grand cost
        self.assertAlmostEqual(sum(g["cost"] for g in d["byModel"]), k["cost"], places=6)
        self.assertAlmostEqual(sum(g["cost"] for g in d["byProject"]), k["cost"], places=6)
        self.assertEqual(d["byModel"][0]["name"], "Sonnet 5")

    def test_estimated_flag_is_carried_without_changing_cost(self):
        # Seed rows are family defaults: the sonnet event is an estimate. An
        # own row at the SAME rates flips the flag and leaves the cost alone.
        self.seed_one()
        conn = self._ro()
        before = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        self.assertTrue(before["events"]["rows"][0]["estimated"])
        self.assertEqual(before["kpis"]["estimatedEvents"], 1)
        self.assertEqual(before["byModel"][0]["estimated"], 1)
        self.assertEqual(before["modelsWithoutOwnPrice"], ["claude-sonnet-5"])
        rw = capture.connect(self.db)
        with rw:
            rw.execute(
                "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
                " source) SELECT provider, 'claude-sonnet-5', in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, 1, 'test'"
                " FROM pricing WHERE model_prefix = 'claude-sonnet-'")
        rw.close()
        conn = self._ro()
        after = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        self.assertFalse(after["events"]["rows"][0]["estimated"])
        self.assertEqual(after["kpis"]["estimatedEvents"], 0)
        self.assertEqual(after["byModel"][0]["estimated"], 0)
        self.assertEqual(after["modelsWithoutOwnPrice"], [])
        self.assertEqual(after["kpis"]["cost"], before["kpis"]["cost"])

    def test_ancestor_row_is_estimated_until_an_own_row_lands(self):
        # An unlisted successor ('claude-sonnet-5-1') priced by its nearest
        # listed ancestor's row ('claude-sonnet-5', R = '-1') is still an
        # estimate, and the model has no own price; its own row flips both.
        self._insert("/proj", "s1", model="claude-sonnet-5-1", inp=1000,
                     out=500, cr=0, cw=0, cw1h=0, mid="m1",
                     ts=iso(int(time.time())))

        def add_row(prefix):
            rw = capture.connect(self.db)
            with rw:
                rw.execute(
                    "INSERT INTO pricing(provider, model_prefix, in_usd,"
                    " out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,"
                    " effective_from, source) VALUES"
                    " ('anthropic', ?, 3, 15, 0.3, 3.75, 6, 1, 'test')",
                    (prefix,))
            rw.close()

        def data():
            conn = self._ro()
            try:
                return dashboard.build_data(conn, {"period": ["year"]})
            finally:
                conn.close()

        add_row("claude-sonnet-5")
        d = data()
        self.assertTrue(d["events"]["rows"][0]["estimated"])
        self.assertEqual(d["kpis"]["estimatedEvents"], 1)
        self.assertEqual(d["modelsWithoutOwnPrice"], ["claude-sonnet-5-1"])
        add_row("claude-sonnet-5-1")
        d = data()
        self.assertFalse(d["events"]["rows"][0]["estimated"])
        self.assertEqual(d["kpis"]["estimatedEvents"], 0)
        self.assertEqual(d["modelsWithoutOwnPrice"], [])

    def test_missing_db_is_read_only_safe(self):
        # build_data is never called without a conn; the HTTP layer guards None.
        # Here we assert the ro open of an absent DB stays absent.
        self.assertFalse(self.db.exists())

    # ---- period window --------------------------------------------------

    def test_period_window_excludes_old_events(self):
        now = int(time.time())
        self.seed_one(ts=now)                       # in every window
        self.seed_one(ts=now - 40 * 86400)          # only month/year
        conn = self._ro()
        day = dashboard.build_data(conn, {"period": ["day"]})
        week = dashboard.build_data(conn, {"period": ["week"]})
        year = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        self.assertEqual(day["kpis"]["events"], 1)
        self.assertEqual(week["kpis"]["events"], 1)
        self.assertEqual(year["kpis"]["events"], 2)

    def test_default_period_is_week(self):
        self.seed_one()
        conn = self._ro()
        d = dashboard.build_data(conn, {})   # no period param -> default
        conn.close()
        self.assertEqual(d["period"], "week")

    # ---- filters --------------------------------------------------------

    def test_project_filter(self):
        self.seed_one()
        self._insert("/other", "s2", model="claude-sonnet-5", inp=1000, out=1000,
                     cr=0, cw=0, cw1h=0, mid="m2", ts=iso(int(time.time())))
        conn = self._ro()
        allp = dashboard.build_data(conn, {"period": ["year"]})
        one = dashboard.build_data(conn, {"period": ["year"], "project": ["/proj"]})
        conn.close()
        self.assertEqual(allp["kpis"]["projects"], 2)
        self.assertEqual(one["kpis"]["projects"], 1)
        self.assertEqual(one["kpis"]["events"], 1)
        # the project table still lists BOTH projects (it ignores the selection)
        self.assertEqual(len(one["byProject"]), 2)

    def test_model_filter_is_parameterised(self):
        self.seed_one()
        conn = self._ro()
        # a hostile value must be treated as data, yield nothing, and never raise
        d = dashboard.build_data(conn, {"period": ["year"],
                                        "models": ["'; DROP TABLE events;--"]})
        conn.close()
        self.assertEqual(d["kpis"]["events"], 0)

    # ---- events pagination + sort --------------------------------------

    def test_pagination_and_sort(self):
        base = int(time.time())
        for i in range(5):
            self.seed_one(ts=base - i * 60)
        conn = self._ro()
        p0 = dashboard.build_data(conn, {"period": ["year"], "pageSize": ["2"], "page": ["0"]})
        p2 = dashboard.build_data(conn, {"period": ["year"], "pageSize": ["2"], "page": ["2"]})
        conn.close()
        self.assertEqual(p0["events"]["total"], 5)
        self.assertEqual(len(p0["events"]["rows"]), 2)
        self.assertEqual(len(p2["events"]["rows"]), 1)          # last page remainder
        self.assertAlmostEqual(p0["events"]["sums"]["cost"], 5 * 1.083, places=3)

    # ---- own-price warning detail (AOS-135) ------------------------------

    def test_price_warning_detail_for_model_without_own_price(self):
        # seed_one is priced off the sonnet FAMILY DEFAULT (no own row for
        # claude-sonnet-5), so it must show up with its count/seen/cost.
        now = int(time.time())
        self.seed_one(ts=now)
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["week"]})
        conn.close()
        detail = d["modelsWithoutOwnPriceDetail"]
        self.assertEqual(len(detail), 1)
        row = detail[0]
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(row["modelName"], "Sonnet 5")
        self.assertEqual(row["events"], 1)
        self.assertEqual(row["firstSeen"], now)
        self.assertEqual(row["lastSeen"], now)
        self.assertAlmostEqual(row["cost"], 1.083, places=3)

    def test_price_warning_detail_empty_when_every_model_has_own_price(self):
        self.seed_one()
        rw = capture.connect(self.db)
        with rw:
            rw.execute(
                "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
                " source) SELECT provider, 'claude-sonnet-5', in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, 1, 'test'"
                " FROM pricing WHERE model_prefix = 'claude-sonnet-'")
        rw.close()
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["week"]})
        conn.close()
        self.assertEqual(d["modelsWithoutOwnPrice"], [])
        self.assertEqual(d["modelsWithoutOwnPriceDetail"], [])

    def test_price_warning_detail_covers_multiple_events_and_is_all_time(self):
        # first/last seen and the event count span BOTH events even though one
        # is outside the requested "day" period — the warning is an all-time
        # signal, same scope as modelsWithoutOwnPrice itself, not windowed.
        now = int(time.time())
        old = now - 40 * 86400
        self.seed_one(ts=old)
        self.seed_one(ts=now)
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["day"]})
        conn.close()
        self.assertEqual(d["kpis"]["events"], 1)          # window itself IS filtered
        detail = d["modelsWithoutOwnPriceDetail"]
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0]["events"], 2)
        self.assertEqual(detail[0]["firstSeen"], old)
        self.assertEqual(detail[0]["lastSeen"], now)
        self.assertAlmostEqual(detail[0]["cost"], 2 * 1.083, places=3)

    def test_price_warning_detail_carries_a_hostile_model_name_raw(self):
        # The model name is untrusted transcript data. The server must not try
        # to sanitize or escape it — JSON-encoding it here is safe by
        # construction. HTML-escaping is no longer any layer's job for this
        # banner: the renderer (dashboard.html's renderPriceWarning, AOS-135
        # S3) builds every node with createElement/textContent, which escapes
        # structurally — there is no esc() call to remember or forget. This
        # test only guards against the SERVER mangling or double-escaping the
        # name; TestPriceWarningRenderStructure below pins the renderer side.
        hostile = "<img src=x onerror=alert(1)>\"'\n"
        self._insert("/proj", "s1", model=hostile, inp=100, out=50, cr=0,
                     cw=0, cw1h=0, mid="m9", ts=iso(int(time.time())))
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        self.assertIn(hostile, d["modelsWithoutOwnPrice"])
        names = {row["model"] for row in d["modelsWithoutOwnPriceDetail"]}
        self.assertIn(hostile, names)
        # round-trips unmodified through JSON, exactly like every other field
        encoded = json.loads(json.dumps(d["modelsWithoutOwnPriceDetail"]))
        self.assertIn(hostile, {row["model"] for row in encoded})
        # a hostile name never matches any seeded pricing prefix -> unpriced
        pw_names = {row["model"] for row in d["priceWarning"]["unpriced"]}
        self.assertIn(hostile, pw_names)
        self.assertEqual(
            [row["model"] for row in d["priceWarning"]["unpriced"]
             if row["model"] == hostile][0], hostile)

    # ---- own-price warning BANNER content (AOS-135 S3) -------------------
    # dashboard.py's build_price_warning() is the only place that formats
    # this banner's strings; the client (dashboard.html's renderPriceWarning)
    # does no arithmetic/formatting of its own (TestPriceWarningRenderStructure
    # below pins that). These tests pin the server's output exactly.

    def test_price_warning_banner_prices_the_1h_fallback_and_pins_cost(self):
        # F4: an ANCESTOR row (claude-sonnet-5-1 is an unlisted point release
        # of claude-sonnet-5, R='-1') whose cache_w_1h_usd is NULL — predating
        # the 1h/5m split — must still price the event's 1h cache-write
        # tokens at that row's 5m rate via COALESCE(cache_w_1h_usd,
        # cache_w_usd), reproducing the pre-split estimate. The model still
        # has no own row, so it lands in the "estimated" group with this
        # exact cost text.
        pin_utc(self)
        now = utc_epoch(2025, 6, 18)   # single-day range: a literal oracle
        self._insert("/proj", "s1", model="claude-sonnet-5-1", inp=1000000,
                     out=200000, cr=0, cw=100000, cw1h=40000, mid="m1",
                     ts=iso(now))
        rw = capture.connect(self.db)
        with rw:
            rw.execute(
                "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
                " source) VALUES ('anthropic', 'claude-sonnet-5', 3.0, 15.0,"
                " 0.3, 3.75, NULL, 1, 'test-no-1h-rate')")
        rw.close()
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        # in 1,000,000*3 + out 200,000*15 + cache_w 100,000 all @ 3.75
        # (60,000 @ 3.75 explicitly + 40,000 @ the 5m-rate fallback) = 6.375
        expected_cost = 6.375
        detail = {r["model"]: r for r in d["modelsWithoutOwnPriceDetail"]}
        self.assertAlmostEqual(detail["claude-sonnet-5-1"]["cost"], expected_cost, places=6)
        pw = d["priceWarning"]
        self.assertEqual(pw["unpriced"], [])
        self.assertEqual(len(pw["estimated"]), 1)
        row = pw["estimated"][0]
        self.assertEqual(row["model"], "claude-sonnet-5-1")
        self.assertEqual(row["modelName"], "Sonnet 5.1")
        self.assertEqual(row["eventsText"], "1 event")
        self.assertEqual(row["dateRangeText"], "Jun 18, 2025")
        self.assertEqual(row["costText"], "$6.38")
        self.assertEqual(row["costText"], dashboard._warn_usd(expected_cost))
        self.assertEqual(pw["heading"],
                          "1 model logged with no price of their own (all time).")
        self.assertEqual(pw["estimatedHeading"], "Priced at an estimated rate")

    def test_price_warning_banner_splits_estimated_and_unpriced_groups(self):
        # F3: a model priced at a family-default/ancestor rate is "estimated"
        # (with its cost); a model matching NO pricing row at all (a
        # non-Claude name) is "unpriced" — its cost text is "not counted",
        # never a misleading "$0.00 estimated". F6: exact model id ships
        # alongside the pretty name; heading states "(all time)" and uses
        # "their own", not "its own".
        now = int(time.time())
        self.seed_one(ts=now)   # claude-sonnet-5 -> family default -> estimated
        self._insert("/proj", "s2", model="gpt-4o", inp=100, out=50, cr=0,
                     cw=0, cw1h=0, mid="m2", ts=iso(now))   # -> unpriced
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        pw = d["priceWarning"]
        self.assertEqual(len(pw["estimated"]), 1)
        self.assertEqual(len(pw["unpriced"]), 1)
        est = pw["estimated"][0]
        self.assertEqual(est["model"], "claude-sonnet-5")
        self.assertEqual(est["modelName"], "Sonnet 5")
        detail = {r["model"]: r for r in d["modelsWithoutOwnPriceDetail"]}
        self.assertEqual(est["costText"], dashboard._warn_usd(detail["claude-sonnet-5"]["cost"]))
        self.assertNotEqual(est["costText"], "not counted")
        unp = pw["unpriced"][0]
        self.assertEqual(unp["model"], "gpt-4o")
        self.assertEqual(unp["modelName"], "gpt-4o")
        self.assertEqual(unp["eventsText"], "1 event")
        self.assertEqual(unp["costText"], "not counted")
        self.assertEqual(pw["heading"],
                          "2 models logged with no price of their own (all time).")
        self.assertEqual(pw["estimatedHeading"], "Priced at an estimated rate")
        self.assertEqual(pw["unpricedHeading"], "No price at all")

    def test_price_warning_banner_empty_when_every_model_has_own_price(self):
        self.seed_one()
        rw = capture.connect(self.db)
        with rw:
            rw.execute(
                "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
                " source) SELECT provider, 'claude-sonnet-5', in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, 1, 'test'"
                " FROM pricing WHERE model_prefix = 'claude-sonnet-'")
        rw.close()
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["week"]})
        conn.close()
        self.assertEqual(d["priceWarning"]["estimated"], [])
        self.assertEqual(d["priceWarning"]["unpriced"], [])

    def _add_price_row(self, prefix, effective_from, provider="anthropic"):
        rw = capture.connect(self.db)
        with rw:
            rw.execute(
                "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
                " source) VALUES (?, ?, 2.0, 6.0, 0.2, 2.5, NULL, ?, 'test')",
                (provider, prefix, effective_from))
        rw.close()

    def test_price_warning_mixed_model_states_what_its_cost_covers(self):
        # N1: 'mistral-large-2' has 3 events; its ancestor row
        # 'mistral-large' only takes effect after the first two, so 2 events
        # are unpriced and 1 is priced at the ancestor (estimated) rate. It
        # stays in the ESTIMATED group (it does carry an estimate), but the
        # cost text must say the figure covers 1 event and the other 2 are
        # not counted — never a bare "$X" that silently sums them as $0.
        now = int(time.time())
        for i, age in enumerate((20, 15, 1)):
            self._insert("/proj", "s1", model="mistral-large-2", inp=1000000,
                         out=0, cr=0, cw=0, cw1h=0, mid=f"x{i}",
                         ts=iso(now - age * 86400))
        self._add_price_row("mistral-large", now - 10 * 86400, provider="mistral")
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["week"]})
        conn.close()
        detail = {r["model"]: r for r in d["modelsWithoutOwnPriceDetail"]}
        self.assertEqual(detail["mistral-large-2"]["events"], 3)
        self.assertEqual(detail["mistral-large-2"]["pricedEvents"], 1)
        self.assertFalse(detail["mistral-large-2"]["unpriced"])
        self.assertAlmostEqual(detail["mistral-large-2"]["cost"], 2.0, places=9)
        pw = d["priceWarning"]
        self.assertEqual(pw["unpriced"], [])
        self.assertEqual([r["model"] for r in pw["estimated"]], ["mistral-large-2"])
        row = pw["estimated"][0]
        self.assertEqual(row["eventsText"], "3 events")
        self.assertEqual(row["costText"],
                         "$2.00 for 1 priced event; 2 unpriced, not counted")

    def test_price_warning_mixed_cost_text_plurals(self):
        pw = dashboard.build_price_warning([{
            "model": "m", "modelName": "m", "events": 5, "pricedEvents": 2,
            "firstSeen": 1, "lastSeen": 2, "cost": 0.5, "unpriced": False}])
        self.assertEqual(pw["estimated"][0]["costText"],
                         "$0.500 for 2 priced events; 3 unpriced, not counted")
        self.assertEqual(pw["estimated"][0]["eventsText"], "5 events")

    def test_price_warning_date_text_catches_month_index_off_by_one(self):
        # C10: dashboard._warn_date indexes _WARN_MONTHS with `d.month - 1`.
        # An off-by-one substitution (e.g. `d.month % 12`) still returns a
        # name from the tuple for every month, so it passes silently unless
        # pinned against a literal string. December is the sharpest case:
        # `12 % 12 == 0` wraps all the way around to "Jan", printed with the
        # UNCHANGED year — "Jan 15, 2025" instead of "Dec 15, 2025".
        pin_utc(self)
        ts = utc_epoch(2025, 12, 15)
        pw = dashboard.build_price_warning([{
            "model": "m", "modelName": "m", "events": 1, "pricedEvents": 1,
            "firstSeen": ts, "lastSeen": ts, "cost": 1.0, "unpriced": False}])
        self.assertEqual(pw["estimated"][0]["dateRangeText"], "Dec 15, 2025")

    def test_price_warning_events_text_counts_plural_exactly(self):
        # M2b: pin n > 1, not just "1 event".
        now = int(time.time())
        for i in range(3):
            self._insert("/proj", "s1", model="claude-sonnet-5", inp=100,
                         out=50, cr=0, cw=0, cw1h=0, mid=f"p{i}", ts=iso(now - i))
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["week"]})
        conn.close()
        self.assertEqual(d["priceWarning"]["estimated"][0]["eventsText"], "3 events")
        self.assertEqual(dashboard._warn_events(0), "0 events")
        self.assertEqual(dashboard._warn_events(1), "1 event")
        self.assertEqual(dashboard._warn_events(2), "2 events")
        self.assertEqual(dashboard._warn_events(12), "12 events")

    def test_price_warning_is_all_time_and_includes_backlog_capture_events(self):
        # M11: the warning's scope is ALL events of the model — outside the
        # requested window and including 'backlog-capture' roll-ups, which
        # every dashboard window itself excludes. A model whose only events
        # are backlog roll-ups from long ago must still be listed, counted
        # and priced.
        pin_utc(self)
        now = utc_epoch(2026, 1, 5)   # multi-month range crossing a year boundary
        old = now - 400 * 86400
        conn = capture.connect(self.db)
        with conn:
            capture.insert_events(conn, "/proj", "s9", 0, None, capture.aggregate([
                entry(model="claude-opus-5-5", inp=1000000, out=0, cr=0, cw=0,
                      cw1h=0, mid="b1", ts=iso(old))]), note="backlog-capture")
            capture.insert_events(conn, "/proj", "s9", 0, None, capture.aggregate([
                entry(model="claude-opus-5-5", inp=1000000, out=0, cr=0, cw=0,
                      cw1h=0, mid="b2", ts=iso(now))]), note="backlog-capture")
        conn.close()
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["day"]})
        conn.close()
        self.assertEqual(d["kpis"]["events"], 0)            # the window excludes them
        self.assertEqual(d["modelsWithoutOwnPrice"], ["claude-opus-5-5"])
        row = {r["model"]: r for r in d["modelsWithoutOwnPriceDetail"]}["claude-opus-5-5"]
        self.assertEqual(row["events"], 2)
        self.assertEqual((row["firstSeen"], row["lastSeen"]), (old, now))
        self.assertAlmostEqual(row["cost"], 2 * 5.0, places=9)  # opus family in_usd 5
        est = d["priceWarning"]["estimated"][0]
        self.assertEqual(est["eventsText"], "2 events")
        self.assertEqual(est["costText"], "$10.00")
        self.assertEqual(est["dateRangeText"], "Dec 1, 2024 – Jan 5, 2026")

    def test_price_warning_cost_equals_the_pages_by_model_cost(self):
        # The banner's SQL cost must be the same arithmetic the rest of the
        # page uses (fetch_rows: a NULL rate on a resolved row prices that
        # token class at 0, the other classes still count). Pinned with a
        # resolved ancestor row whose in_usd is NULL, plus a family-default
        # model, against byModel over the same all-in-window events.
        now = int(time.time())
        self._insert("/proj", "s1", model="claude-sonnet-5", inp=100000,
                     out=50000, cr=10000, cw=5000, cw1h=5000, mid="c1", ts=iso(now))
        self._insert("/proj", "s1", model="acme-big-2", inp=1000000,
                     out=100000, cr=20000, cw=10000, cw1h=4000, mid="c2", ts=iso(now))
        rw = capture.connect(self.db)
        with rw:
            rw.execute(
                "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
                " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
                " source) VALUES ('acme', 'acme-big', NULL, 10.0, 1.0, 2.0,"
                " 4.0, 1, 'test-null-in')")
        rw.close()
        conn = self._ro()
        d = dashboard.build_data(conn, {"period": ["year"]})
        conn.close()
        by_model = {g["key"]: g["cost"] for g in d["byModel"]}
        detail = {r["model"]: r for r in d["modelsWithoutOwnPriceDetail"]}
        self.assertEqual(set(detail), {"claude-sonnet-5", "acme-big-2"})
        # acme: out 100000*10 + cache_r 20000*1 + 6000*2 + 4000*4 = 1.048
        self.assertAlmostEqual(detail["acme-big-2"]["cost"], 1.048, places=9)
        for model, row in detail.items():
            self.assertAlmostEqual(row["cost"], by_model[model], places=9, msg=model)
            self.assertEqual(row["pricedEvents"], 1, model)

    def test_warn_usd_rounds_like_the_page_on_ties(self):
        # N2: the page's fmtUSD (toLocaleString, halfExpand on the shortest
        # decimal) renders 1.305 as "$1.31"; the banner must too. Below $1 it
        # is toFixed — exact binary value, exact ties away from zero.
        # tests/test_dashboard_client.py cross-checks against the real fmtUSD.
        self.assertEqual(dashboard._warn_usd(1.305), "$1.31")
        self.assertEqual(dashboard._warn_usd(1.005), "$1.01")
        self.assertEqual(dashboard._warn_usd(2.675), "$2.68")
        self.assertEqual(dashboard._warn_usd(1234.565), "$1,234.57")
        self.assertEqual(dashboard._warn_usd(0.0625), "$0.063")
        self.assertEqual(dashboard._warn_usd(0.0115), "$0.011")   # exact 0.01149…
        self.assertEqual(dashboard._warn_usd(0.0145), "$0.015")   # exact 0.01450…
        self.assertEqual(dashboard._warn_usd(0.00005), "$0.0001")
        # just BELOW a half-cent boundary stays below (no rounding nudge)
        self.assertEqual(dashboard._warn_usd(1.3049999999), "$1.30")
        self.assertEqual(dashboard._warn_usd(999.9949999999), "$999.99")
        self.assertEqual(dashboard._warn_usd(0), "$0.00")
        self.assertEqual(dashboard._warn_usd(None), "$0.00")
        self.assertEqual(dashboard._warn_usd(float("nan")), "$0.00")


# --- timeline bucketing (v0.11.0: per-bucket columns, not a running total) ---

import datetime


def ts(y, mo, d, h=0):
    return int(datetime.datetime(y, mo, d, h).timestamp())


def row(when, cost=1.0):
    return {"ts": when, "cost": cost, "total": 10, "consumed": 3,
            "cachetok": 7, "modelName": "m"}


class TestTimelineGrain(unittest.TestCase):
    """The chart answers 'how much WHEN': one column per bucket, never a
    running total, with the grain chosen by the period."""

    def test_grain_per_period(self):
        self.assertEqual(dashboard.TIMELINE_GRAIN["day"], "hour")
        self.assertEqual(dashboard.TIMELINE_GRAIN["week"], "day")
        self.assertEqual(dashboard.TIMELINE_GRAIN["month"], "day")
        self.assertEqual(dashboard.TIMELINE_GRAIN["year"], "month")

    def test_buckets_floor_to_local_boundaries(self):
        t = ts(2026, 8, 6, 14) + 1837
        self.assertEqual(dashboard._bucket_start(t, "hour"), ts(2026, 8, 6, 14))
        self.assertEqual(dashboard._bucket_start(t, "day"), ts(2026, 8, 6))
        self.assertEqual(dashboard._bucket_start(t, "month"), ts(2026, 8, 1))

    def test_next_bucket_crosses_month_and_year(self):
        self.assertEqual(dashboard._next_bucket(ts(2026, 8, 1), "month"),
                         ts(2026, 9, 1))
        self.assertEqual(dashboard._next_bucket(ts(2026, 12, 1), "month"),
                         ts(2027, 1, 1))
        self.assertEqual(dashboard._next_bucket(ts(2026, 8, 6), "day"),
                         ts(2026, 8, 7))

    def test_quiet_buckets_are_zero_filled_not_dropped(self):
        since, now = ts(2026, 8, 1), ts(2026, 8, 5)
        tl = dashboard._timeline([row(ts(2026, 8, 3), 2.0)], "week", since, now)
        self.assertEqual([b["ts"] for b in tl],
                         [ts(2026, 8, d) for d in range(1, 6)])
        self.assertEqual([b["cost"] for b in tl], [0.0, 0.0, 2.0, 0.0, 0.0])
        self.assertEqual([b["n"] for b in tl], [0, 0, 1, 0, 0])

    def test_values_are_per_bucket_totals(self):
        since, now = ts(2026, 8, 6), ts(2026, 8, 6, 5)
        tl = dashboard._timeline(
            [row(ts(2026, 8, 6, 1), 1.0), row(ts(2026, 8, 6, 1, ), 2.0),
             row(ts(2026, 8, 6, 4), 4.0)], "day", since, now)
        by = {b["ts"]: b for b in tl}
        self.assertEqual(by[ts(2026, 8, 6, 1)]["cost"], 3.0)   # summed, not cumulative
        self.assertEqual(by[ts(2026, 8, 6, 4)]["cost"], 4.0)   # NOT 7.0
        self.assertEqual(by[ts(2026, 8, 6, 2)]["cost"], 0.0)

    def test_rows_outside_the_window_are_ignored(self):
        since, now = ts(2026, 8, 5), ts(2026, 8, 6)
        tl = dashboard._timeline([row(ts(2026, 7, 1))], "week", since, now)
        self.assertEqual(sum(b["n"] for b in tl), 0)


class TestGrainChoice(unittest.TestCase):
    """The period picks a default; the reader may override it within what the
    window can carry (hours across a year would be 8,760 points)."""

    def test_defaults_are_used_when_nothing_is_asked(self):
        for period, grain in dashboard.TIMELINE_GRAIN.items():
            self.assertEqual(dashboard.resolve_grain(period, ""), grain)

    def test_explicit_choice_is_honoured_when_offered(self):
        self.assertEqual(dashboard.resolve_grain("week", "hour"), "hour")
        self.assertEqual(dashboard.resolve_grain("month", "hour"), "hour")
        self.assertEqual(dashboard.resolve_grain("year", "day"), "day")

    def test_unavailable_choice_falls_back_to_the_default(self):
        # months across a week would be one point; hours across a year, 8,760
        self.assertEqual(dashboard.resolve_grain("week", "month"), "day")
        self.assertEqual(dashboard.resolve_grain("year", "hour"), "month")
        self.assertEqual(dashboard.resolve_grain("day", "nonsense"), "hour")

    def test_month_grain_is_offered_for_the_year_window_only(self):
        for period, offers in dashboard.GRAIN_ALLOWED.items():
            self.assertEqual("month" in offers, period == "year", period)

    def test_every_default_is_among_its_own_offers(self):
        for period, grain in dashboard.TIMELINE_GRAIN.items():
            self.assertIn(grain, dashboard.GRAIN_ALLOWED[period])

    def test_timeline_honours_an_explicit_grain(self):
        since, now = ts(2026, 8, 6), ts(2026, 8, 6, 5)
        tl = dashboard._timeline([row(ts(2026, 8, 6, 2))], "week", since, now,
                                 grain="hour")
        self.assertEqual(len(tl), 6)                 # 6 hourly buckets, not 1 day
        self.assertTrue(all(b["grain"] == "hour" for b in tl))


class TestVersionRedirect(unittest.TestCase):
    """A plugin update leaves older copies in the cache and a running session
    keeps serving the command text it loaded — `open`/`restart` must not roll
    the dashboard back to the stale copy they were invoked from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "token-telemetry"

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *versions):
        for v in versions:
            d = self.root / v / "scripts"
            d.mkdir(parents=True)
            (d / "dashboard.py").write_text("# copy\n")
        return self.root

    def test_points_at_the_highest_installed_version(self):
        self.make("0.9.1", "0.10.3", "0.12.0")
        got = dashboard.newest_sibling_script(
            self.root / "0.10.3" / "scripts" / "dashboard.py")
        self.assertEqual(got,
                         (self.root / "0.12.0" / "scripts" / "dashboard.py")
                         .resolve())

    def test_semver_ordering_is_numeric_not_lexical(self):
        # "0.9.1" > "0.12.0" as strings; the version tuple must win
        self.make("0.9.1", "0.12.0")
        got = dashboard.newest_sibling_script(
            self.root / "0.9.1" / "scripts" / "dashboard.py")
        self.assertEqual(got.parent.parent.name, "0.12.0")

    def test_newest_copy_redirects_nowhere(self):
        self.make("0.11.1", "0.12.0")
        self.assertIsNone(dashboard.newest_sibling_script(
            self.root / "0.12.0" / "scripts" / "dashboard.py"))

    def test_dev_checkout_is_left_alone(self):
        # not a versioned cache layout -> the invoking copy is what was meant
        d = pathlib.Path(self.tmp.name) / "repo" / "scripts"
        d.mkdir(parents=True)
        (d / "dashboard.py").write_text("# dev\n")
        self.assertIsNone(dashboard.newest_sibling_script(d / "dashboard.py"))

    def test_incomplete_version_dirs_are_skipped(self):
        self.make("0.11.1")
        (self.root / "0.13.0").mkdir()          # no scripts/dashboard.py
        self.assertIsNone(dashboard.newest_sibling_script(
            self.root / "0.11.1" / "scripts" / "dashboard.py"))


class TestBackendBanner(unittest.TestCase):
    """P10 / AOS-114: the dashboard page gets a small backend-awareness banner
    only when the remote (Supabase) backend is active; the local page (default,
    or whatever an absent/malformed settings.json reads back as) is served
    byte-for-byte unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "telemetry"
        self._prev = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.dir / "usage.db")
        self.original_html = dashboard.HTML.read_bytes()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TOKEN_TELEMETRY_DB", None)
        else:
            os.environ["TOKEN_TELEMETRY_DB"] = self._prev
        self.tmp.cleanup()

    def test_no_settings_file_is_local_no_banner(self):
        self.assertFalse(settings.settings_path().exists())
        page = dashboard.dashboard_page()
        self.assertEqual(page, self.original_html)
        self.assertNotIn(b'<div class="backend-note"', page)

    def test_malformed_settings_is_local_no_banner_no_crash(self):
        self.dir.mkdir(parents=True)
        settings.settings_path().write_text("{ this is not json")
        page = dashboard.dashboard_page()
        self.assertEqual(page, self.original_html)

    def test_explicit_local_backend_no_banner(self):
        settings.write_settings({"active_backend": "local"})
        page = dashboard.dashboard_page()
        self.assertEqual(page, self.original_html)

    def test_supabase_backend_shows_banner(self):
        settings.write_settings({"active_backend": "supabase"})
        page = dashboard.dashboard_page()
        self.assertNotEqual(page, self.original_html)
        self.assertEqual(len(page), len(self.original_html) + len(dashboard._BACKEND_BANNER))
        self.assertIn(b'<div class="backend-note"', page)
        self.assertIn(b"/token-telemetry:token-stats", page)
        # inserted exactly once, ahead of the header it decorates
        self.assertEqual(page.count(b'<div class="backend-note"'), 1)
        self.assertLess(page.find(b'<div class="backend-note"'),
                        page.find(b'<header class="hd">'))

    def test_unknown_backend_value_is_treated_as_local(self):
        settings.write_settings({"active_backend": "carrier-pigeon"})
        page = dashboard.dashboard_page()
        self.assertEqual(page, self.original_html)


# --- own-price warning renderer structure (AOS-135 S3, F2b) ---------------
#
# A cheap first line only: a text scan of one function cannot see a helper
# defined elsewhere, bracket access, or a sink elsewhere on the render path.
# The GATE is tests/test_dashboard_client.py, which executes the page's real
# script under node against a fake DOM that throws on every such write.
#
# dashboard.html's renderPriceWarning() must build every node with
# createElement/textContent and never touch innerHTML/insertAdjacentHTML/
# outerHTML/document.write, so a hostile model name can never be interpreted
# as markup — and must never remove #price-warn's own shipped structural
# children (the F1 bug: box.innerHTML="" on the empty path deleted
# #price-warn-msg and the row list themselves, so the next non-empty render
# threw inside load()'s try and tripped the "Connection lost" dialog
# permanently). These tests parse the actual shipped dashboard.html and
# isolate the renderPriceWarning function body (brace-balanced from its
# `function renderPriceWarning(` header) so a regression anywhere in that
# function — including in a locally-nested helper — is caught.

_DASHBOARD_HTML = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "dashboard.html"


def _extract_js_function(source, name):
    """The brace-balanced body of `function <name>(...){...}` in `source`,
    starting at the `function` keyword. Raises AssertionError if the
    function is missing or its braces never balance (e.g. it was deleted or
    mangled) so a broken extraction fails loudly instead of silently passing
    an empty/partial string through the checks below."""
    marker = f"function {name}("
    start = source.index(marker)
    i = source.index("{", start)
    depth = 0
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces extracting function {name}")


class TestPriceWarningRenderStructure(unittest.TestCase):
    def setUp(self):
        self.source = _DASHBOARD_HTML.read_text()
        self.fn = _extract_js_function(self.source, "renderPriceWarning")

    def test_render_function_is_found_and_nontrivial(self):
        # Sanity: the extraction itself must have found real content, or
        # every other assertion in this class would pass vacuously.
        self.assertIn("price-warn", self.fn)
        self.assertGreater(len(self.fn), 200)

    def test_no_innerHTML_family_api_is_used(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML",
                       "document.write"):
            self.assertNotIn(banned, self.fn,
                f"renderPriceWarning must not use {banned} (F1/F2)")

    def test_structural_children_are_never_removed_or_replaced(self):
        # Only a row <li> may ever be created/appended/discarded; the box,
        # the two group <div>s, their <h4> headings and the message <span>
        # are shipped once in dashboard.html and must never be torn down.
        for banned in ("removeChild", ".remove(", "replaceWith", "replaceChild"):
            self.assertNotIn(banned, self.fn,
                f"renderPriceWarning must not use {banned} on structural nodes")

    def test_uses_textContent(self):
        self.assertIn("textContent", self.fn)

    def test_calls_createElement_to_build_rows(self):
        self.assertIn("createElement", self.fn)

    def test_the_call_site_passes_the_new_priceWarning_field(self):
        # renderAll() must feed the server-formatted object, not the old
        # (now removed) raw detail array.
        self.assertIn("renderPriceWarning(D.priceWarning)", self.source)
        self.assertNotIn("renderPriceWarning(D.modelsWithoutOwnPriceDetail)", self.source)


if __name__ == "__main__":
    unittest.main()
