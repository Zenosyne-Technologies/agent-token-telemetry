import contextlib
import io
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import report

from tests.test_capture import entry


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = report.main(argv)
    return rc, out.getvalue()


class TestReportScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.db = self.dir / "usage.db"

    def tearDown(self):
        self.tmp.cleanup()

    def seed_db(self):
        conn = capture.connect(self.db)
        now = int(time.time())
        groups = capture.aggregate([
            entry(model="claude-sonnet-5", inp=100000, out=50000, cr=10000,
                  cw=5000, mid="m1", cw1h=5000,
                  ts=time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                   time.gmtime(now))),
        ])
        with conn:
            capture.insert_events(conn, "/proj", "s1", 0, None, groups)
        conn.close()

    def test_project_stats_missing_db(self):
        rc, out = run(["project-stats", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        self.assertIn("No telemetry has been recorded yet", out)
        self.assertFalse(self.db.exists())  # read-only: never creates the DB

    def test_project_stats_renders_markdown_row(self):
        self.seed_db()
        rc, out = run(["project-stats", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        self.assertIn("| project | sessions | events |", out)
        self.assertIn("| proj |", out)   # basename fallback, no name stamped
        self.assertIn("100,000", out)    # thousands separator
        self.assertIn("(today)", out)
        # fresh DB prices at the seed -> the closing hint must appear
        self.assertIn("undated seed", out)
        self.assertIn("pricing-update", out)

    def test_project_stats_shows_registered_name(self):
        self.seed_db()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET name='My Project' WHERE path='/proj'")
        conn.commit()
        conn.close()
        _, out = run(["project-stats", "--db", str(self.db)])
        self.assertIn("| My Project |", out)

    def test_project_stats_prices_1h_writes_at_1h_rate(self):
        self.seed_db()
        _, out = run(["project-stats", "--db", str(self.db)])
        # sonnet seed per MTok: in 3, out 15, cr 0.3, cw1h 6; all writes 1h.
        # 100k in + 50k out + 10k cr + 5k cw1h = 0.3+0.75+0.003+0.03 = 1.083
        self.assertIn("**$1.08**", out)   # est. cost total, bold, 2 decimals

    def test_project_stats_cost_split_adds_up(self):
        self.seed_db()
        _, out = run(["project-stats", "--db", str(self.db)])
        # classic 1.05 (0.30 in / 0.75 out); cached 0.033 (0.003 r / 0.03 w).
        # Max two decimals, trailing zeros cut, tiny-but-nonzero -> <$0.01.
        self.assertIn("$1.05 ($0.3 / $0.75)", out)
        self.assertIn("$0.03 (<$0.01 / $0.03)", out)
        # cache token counters are columns now
        self.assertIn("| cache read | cache write |", out.splitlines()[0])
        # rates dates no longer appear in the cost cells
        self.assertNotIn("(rates ", out)

    def test_fmt_usd_rounding_rules(self):
        self.assertEqual(report.fmt_usd(25.6747), "$25.67")
        self.assertEqual(report.fmt_usd(1.50), "$1.5")
        self.assertEqual(report.fmt_usd(25.00), "$25")
        self.assertEqual(report.fmt_usd(0.0003), "<$0.01")
        self.assertEqual(report.fmt_usd(0), "$0")

    def test_token_stats_renders_sections(self):
        self.seed_db()
        rc, out = run(["token-stats", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        self.assertIn("**Today:", out)
        self.assertIn("**By project (7 days)**", out)
        self.assertIn("**By model (7 days)**", out)
        self.assertIn("**By tier (7 days)**", out)
        self.assertIn("claude-sonnet-5", out)
        self.assertIn("| claude-sonnet-5 | small |", out)  # tier in by-model
        self.assertIn("small", out)          # tier mapping
        self.assertIn("No milestone-branch", out)
        self.assertIn("No issue-tagged", out)

    def test_token_stats_missing_db(self):
        rc, out = run(["token-stats", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        self.assertIn("No telemetry has been recorded yet", out)
        self.assertFalse(self.db.exists())

    def test_info_reports_off_project_and_missing_db(self):
        rc, out = run(["info", "--db", str(self.db), "--cwd", str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertIn("telemetry **off**", out)
        self.assertIn("does not exist", out)
        self.assertFalse(self.db.exists())

    def test_info_enabled_project_with_zero_events_gets_restart_hint(self):
        (self.dir / ".claude").mkdir()
        (self.dir / ".claude" / "telemetry").write_text("central\n")
        self.seed_db()  # events exist, but for /proj — not for this root
        rc, out = run(["info", "--db", str(self.db), "--cwd", str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertIn("telemetry **enabled**", out)
        self.assertIn("central storage", out)
        self.assertNotIn("(default)", out)  # marker names the mode explicitly
        self.assertIn("Restart Claude Code", out)

    def test_info_counts_this_projects_events(self):
        (self.dir / ".claude").mkdir()
        (self.dir / ".claude" / "telemetry").write_text("central\n")
        conn = capture.connect(self.db)
        groups = capture.aggregate([entry(mid="m1")])
        with conn:
            capture.insert_events(conn, str(self.dir), "s1", 0, None, groups)
        conn.close()
        _, out = run(["info", "--db", str(self.db), "--cwd", str(self.dir)])
        self.assertIn("| this project | 1 events", out)
        self.assertNotIn("Restart Claude Code", out)

    def test_info_future_dated_rate_is_not_latest(self):
        self.seed_db()
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
            " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from, source)"
            " VALUES ('anthropic','claude-sonnet-5',3,15,0.3,3.75,6,"
            " strftime('%s','now','+30 days'),'test')")
        conn.commit()
        conn.close()
        _, out = run(["info", "--db", str(self.db), "--cwd", str(self.dir)])
        # the only in-force rows are the seed; the future row must not surface
        self.assertIn("seed rates (undated)", out)

    def test_project_stats_handles_pre_v4_db(self):
        # A DB the new capture has not migrated yet (columns absent).
        conn = capture.connect(self.db)
        conn.execute("PRAGMA user_version=3")
        conn.commit()
        conn.close()
        # simulate the missing columns by rebuilding without them
        raw = sqlite3.connect(self.db)
        raw.executescript("""
            CREATE TABLE ev2 AS SELECT ts, session_id, kind, agent, model_id,
              in_tok, out_tok, cache_r, cache_w, dur_ms, branch, commit_sha,
              issue_key, task_size, note FROM events;
            DROP TABLE events; ALTER TABLE ev2 RENAME TO events;
            CREATE TABLE pr2 AS SELECT provider, model_prefix, model_version,
              in_usd, out_usd, cache_r_usd, cache_w_usd, effective_from, source
              FROM pricing;
            DROP TABLE pricing; ALTER TABLE pr2 RENAME TO pricing;
        """)
        raw.commit()
        raw.close()
        rc, out = run(["project-stats", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        self.assertIn("| project |", out)


if __name__ == "__main__":
    unittest.main()
