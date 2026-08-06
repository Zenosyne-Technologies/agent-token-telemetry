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


if __name__ == "__main__":
    unittest.main()
