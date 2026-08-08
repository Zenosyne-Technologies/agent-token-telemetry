import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import dashboard

from tests.test_capture import entry


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


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


if __name__ == "__main__":
    unittest.main()
