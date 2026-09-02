import contextlib
import io
import pathlib
import re
import sqlite3
import subprocess
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

    def test_project_stats_escapes_hostile_project_name(self):
        self.seed_db()
        hostile = "evil | name ` with\nnewline junk"
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET name=? WHERE path='/proj'",
                     (hostile,))
        conn.commit()
        conn.close()
        _, out = run(["project-stats", "--db", str(self.db)])
        table_lines = [l for l in out.splitlines() if l.startswith("|")]
        # header + alignment row + exactly one data row: structure intact
        self.assertEqual(len(table_lines), 3)
        # column count matches between header and the hostile data row —
        # an unescaped "|" in the name would have added a spurious column
        header_cols = len(re.split(r"(?<!\\)\|", table_lines[0]))
        row_cols = len(re.split(r"(?<!\\)\|", table_lines[2]))
        self.assertEqual(header_cols, row_cols)
        # raw hostile characters never appear unescaped in the output
        self.assertNotIn("evil | name", out)
        self.assertNotIn("` with", out)
        self.assertNotIn("with\nnewline", out)
        # sanitized form is present instead
        self.assertIn("evil \\| name ' with newline junk", out)

    def test_project_stats_strips_ansi_control_bytes(self):
        self.seed_db()
        hostile = "\x01\x1b[31mRedText\x1b[0m normal"
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET name=? WHERE path='/proj'",
                     (hostile,))
        conn.commit()
        conn.close()
        _, out = run(["project-stats", "--db", str(self.db)])
        table_lines = [l for l in out.splitlines() if l.startswith("|")]
        # header + alignment row + exactly one data row: structure intact
        self.assertEqual(len(table_lines), 3)
        # no byte below 0x20 (space) and no DEL (0x7f) in the rendered row
        # (the raw string still uses "\n" as the normal line separator)
        data_row = table_lines[2]
        self.assertTrue(all(ord(ch) >= 0x20 and ord(ch) != 0x7f
                            for ch in data_row))
        # the sanitized text survives (with the escape bytes gone)
        self.assertIn("RedText", data_row)

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
        self.assertIn("No issue-tagged", out)

    def test_token_stats_never_groups_by_branch(self):
        self.seed_db()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE events SET branch='milestone/foo'")
        conn.commit()
        conn.close()
        _, out = run(["token-stats", "--db", str(self.db)])
        self.assertNotIn("milestone", out.lower())
        self.assertNotIn("By milestone", out)
        # events.branch must no longer be used as a grouping key anywhere
        src = pathlib.Path(report.__file__).read_text()
        self.assertNotIn("branch LIKE", src)
        self.assertNotIn("GROUP BY branch", src)
        self.assertNotIn("by_milestone", src)

    def test_storage_status_renders_tables(self):
        self.seed_db()
        rc, out = run(["storage-status", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        self.assertIn("### Central DB", out)
        self.assertIn("### Projects", out)
        self.assertIn("`/proj`", out)
        self.assertIn("| no | — | — |", out)   # no mirror configured

    def test_storage_status_shows_configured_mirror(self):
        self.seed_db()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET mirror_path='/nowhere/m.db',"
                     " mirror_last_at=strftime('%s','now') WHERE path='/proj'")
        conn.commit()
        conn.close()
        _, out = run(["storage-status", "--db", str(self.db)])
        self.assertIn("not accessible on this machine", out)
        self.assertIn("configured state, not a write receipt", out)

    def test_token_stats_excludes_backlog_rollups_with_notice(self):
        self.seed_db()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE events SET note='backlog-capture'")
        conn.commit()
        conn.close()
        rc, out = run(["token-stats", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        # the only event is a roll-up -> windowed totals are zero...
        self.assertIn("| today | 0 | 0 | 0 | 0 | 0 |", out)
        # ...and the exclusion is stated, never silent
        self.assertIn("1 backlog roll-up event(s)", out)
        self.assertIn("excluded from the windowed figures", out)

    def test_project_stats_all_time_keeps_backlog_rollups(self):
        self.seed_db()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE events SET note='backlog-capture'")
        conn.commit()
        conn.close()
        _, out = run(["project-stats", "--db", str(self.db)])
        self.assertIn("100,000", out)   # all-time view still counts it

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


class TestScopedRollup(unittest.TestCase):
    """AOS-79: caller-supplied issue-key-set scoping, replacing the
    `branch LIKE 'milestone/%'` grouping gitflow (kit v0.22.0) makes match
    nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.db = self.dir / "usage.db"

    def tearDown(self):
        self.tmp.cleanup()

    def seed_event(self, issue_key=None, commit_sha=None, out_tok=50000):
        conn = capture.connect(self.db)
        now = int(time.time())
        groups = capture.aggregate([
            entry(model="claude-sonnet-5", inp=100000, out=out_tok, cr=10000,
                  cw=5000, mid="m1", cw1h=5000,
                  ts=time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                   time.gmtime(now))),
        ])
        with conn:
            capture.insert_events(conn, str(self.dir), "s1", 0, None, groups,
                                  commit_sha=commit_sha, issue_key=issue_key)
        conn.close()

    def init_git_repo(self):
        for args in (["git", "init", "-q"],
                     ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "Test"]):
            subprocess.run(args, cwd=self.dir, check=True,
                           capture_output=True)

    def commit(self, subject):
        (self.dir / "f.txt").write_text(subject)
        subprocess.run(["git", "add", "-A"], cwd=self.dir, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", subject], cwd=self.dir,
                       check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=self.dir, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit_with_body(self, subject, body):
        (self.dir / "f.txt").write_text(subject + body)
        subprocess.run(["git", "add", "-A"], cwd=self.dir, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", subject, "-m", body],
                       cwd=self.dir, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=self.dir, check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_empty_key_set_fails_resolution(self):
        _, out = run(["token-stats", "--scope", " , ,", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("scope resolution failed — empty key set", out)
        self.assertNotIn("$", out)   # no figure

    def test_invalid_keys_are_named_and_rejected(self):
        _, out = run(["token-stats", "--scope", "not valid!,also-bad-",
                      "--db", str(self.db), "--cwd", str(self.dir)])
        self.assertIn("Rejected invalid scope key(s)", out)
        self.assertIn("notvalid", out)   # sanitized echo (space/! stripped)
        self.assertIn("also-bad-", out)
        self.assertIn("scope resolution failed — empty key set", out)

    def test_telemetry_absent_when_db_missing(self):
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("telemetry absent", out)
        self.assertNotIn("$", out)

    def test_telemetry_absent_when_project_has_no_events(self):
        # DB exists (seeded for an unrelated project path) but nothing for
        # this project's cwd.
        conn = capture.connect(self.db)
        groups = capture.aggregate([entry(mid="m1")])
        with conn:
            capture.insert_events(conn, "/some/other/project", "s1", 0, None,
                                  groups)
        conn.close()
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("telemetry absent", out)

    def test_broken_scope_when_no_key_has_rows(self):
        self.seed_event(issue_key="AOS-1")   # some events, but not AOS-79
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("0 of 1 scoped issues have telemetry rows"
                      " (broken scope until proven otherwise)", out)
        self.assertNotIn("$", out)

    def test_scoped_rollup_via_issue_key(self):
        self.seed_event(issue_key="AOS-79")
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("**Scoped rollup**", out)
        self.assertIn("1 events", out)
        self.assertIn("100,000 input / 50,000 output", out)
        self.assertNotIn("of 1 issues have rows", out)   # full coverage

    def test_scoped_rollup_falls_back_to_commit_sha(self):
        self.init_git_repo()
        sha = self.commit("AOS-79: fix the thing")
        self.seed_event(issue_key=None, commit_sha=sha)   # untagged row
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("**Scoped rollup**", out)
        self.assertIn("1 events", out)
        self.assertNotIn("broken scope", out)

    def test_scoped_rollup_partial_coverage_sums_across_set(self):
        self.seed_event(issue_key="AOS-79", out_tok=50000)
        # AOS-80 has no tagged rows and no matching commit -> uncovered
        _, out = run(["token-stats", "--scope", "AOS-79,AOS-80",
                      "--db", str(self.db), "--cwd", str(self.dir)])
        self.assertIn("1 of 2 issues have rows.", out)
        self.assertIn("50,000 output", out)   # sum is just the covered key

    def test_commit_sha_fallback_matches_subject_only_not_body_paragraph(self):
        # Security fix: --grep matches anywhere in the full commit message,
        # not just the subject; a key mentioned only in a later body
        # paragraph must never be attributed to that key's rollup.
        self.init_git_repo()
        good_sha = self.commit("AOS-79: real fix")
        bad_sha = self.commit_with_body(
            "Unrelated change", "AOS-79: mentioned only in the body")
        shas = report.commits_for_key(self.dir, "AOS-79")
        self.assertIn(good_sha, shas)
        self.assertNotIn(bad_sha, shas)

    def test_commit_sha_fallback_pipeline_ignores_body_only_mention(self):
        self.init_git_repo()
        bad_sha = self.commit_with_body(
            "Unrelated change", "AOS-79: mentioned only in the body")
        self.seed_event(issue_key=None, commit_sha=bad_sha)
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("0 of 1 scoped issues have telemetry rows"
                      " (broken scope until proven otherwise)", out)

    def test_invalid_scope_key_echo_is_sanitized(self):
        # Security fix: a rejected --scope token is echoed into markdown —
        # it must not carry backticks/newlines/pipes into the rendered text.
        evil = "bad`key\nwith|pipe"
        _, out = run(["token-stats", "--scope", evil, "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("Rejected invalid scope key(s)", out)
        self.assertNotIn(evil, out)
        segment = (out.split("Rejected invalid scope key(s):")[1]
                  .split("(must match")[0])
        self.assertNotIn("\n", segment)
        self.assertNotIn("|", segment)
        # exactly one backtick pair wraps the sanitized token — no breakout
        self.assertEqual(segment.count("`"), 2)

    def test_invalid_scope_key_echo_is_length_capped(self):
        long_tok = "!" * 100 + "1"  # all invalid chars but the trailing "1"
        _, out = run(["token-stats", "--scope", long_tok, "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("`1`", out)

    def test_sanitize_invalid_echo_helper(self):
        self.assertEqual(report.sanitize_invalid_echo("bad`key\nwith|pipe"),
                         "badkeywithpipe")
        self.assertEqual(report.sanitize_invalid_echo("```\n\n"),
                         "(unprintable)")
        self.assertEqual(report.sanitize_invalid_echo("a" * 50),
                         "a" * 32 + "…")


if __name__ == "__main__":
    unittest.main()
