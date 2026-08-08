import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import dashboard


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


if __name__ == "__main__":
    unittest.main()
