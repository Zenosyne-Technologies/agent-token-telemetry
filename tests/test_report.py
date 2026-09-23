import contextlib
import datetime
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

    def test_project_stats_strips_bidi_controls_and_line_separators(self):
        self.seed_db()
        hostile = "evil‮SPOOFED⁦pop next end"
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET name=? WHERE path='/proj'",
                     (hostile,))
        conn.commit()
        conn.close()
        _, out = run(["project-stats", "--db", str(self.db)])
        table_lines = [l for l in out.splitlines() if l.startswith("|")]
        # header + alignment row + exactly one data row: structure intact
        self.assertEqual(len(table_lines), 3)
        data_row = table_lines[2]
        self.assertNotIn("‮", data_row)  # RIGHT-TO-LEFT OVERRIDE
        self.assertNotIn("⁦", data_row)  # LEFT-TO-RIGHT ISOLATE
        self.assertNotIn(" ", data_row)  # LINE SEPARATOR
        self.assertNotIn(" ", data_row)  # PARAGRAPH SEPARATOR
        self.assertIn("evilSPOOFEDpop next end", data_row)

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

    def test_by_model_tie_breaks_on_model_name(self):
        # When two models tie on SUM(out_tok) (the sort key),
        # the order must be deterministic — byte order on the model name —
        # so the local and remote (Postgres) dialects agree on ties.
        conn = capture.connect(self.db)
        now = int(time.time())
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
        groups = capture.aggregate([
            entry(model="claude-opus-2", inp=100, out=1000, mid="m1", ts=ts),
            entry(model="claude-opus-1", inp=100, out=1000, mid="m2", ts=ts),
        ])
        with conn:
            capture.insert_events(conn, "/proj", "s1", 0, None, groups)
        conn.close()
        _, out = run(["token-stats", "--db", str(self.db)])
        self.assertLess(out.index("claude-opus-1"), out.index("claude-opus-2"))

    def test_by_tier_tie_breaks_on_tier_name(self):
        # Same rule for by_tier — a tie on SUM(out_tok) between
        # two different tiers breaks on the tier label's byte order.
        conn = capture.connect(self.db)
        now = int(time.time())
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
        groups = capture.aggregate([
            entry(model="claude-sonnet-1", inp=100, out=500, mid="m1", ts=ts),
            entry(model="claude-opus-1", inp=100, out=500, mid="m2", ts=ts),
        ])
        with conn:
            capture.insert_events(conn, "/proj", "s1", 0, None, groups)
        conn.close()
        _, out = run(["token-stats", "--db", str(self.db)])
        section = out.split("**By tier (7 days)**", 1)[1]
        # tie on out_tok -> 'heavy' before 'small' (byte order)
        self.assertLess(section.index("heavy"), section.index("small"))

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

    def test_scoped_output_is_the_rollup_alone_without_price_footer(self):
        # commands/token-stats.md: with --scope the output is the Scoped
        # rollup ALONE — it replaces the normal report, so the own-price
        # footer (which closes the normal report) does not appear, and the
        # rollup's cost line is the last line.
        self.seed_event(issue_key="AOS-79")
        _, full = run(["token-stats", "--db", str(self.db),
                       "--cwd", str(self.dir)])
        # non-vacuous: the same DB DOES produce a footer without --scope
        self.assertTrue(full.rstrip("\n").splitlines()[-1].startswith(
            "No own published price for `claude-sonnet-5`"))
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertNotIn("No own published price", out)
        lines = out.rstrip("\n").splitlines()
        self.assertEqual(lines[1], "**Scoped rollup**")
        self.assertTrue(lines[-1].startswith("**"), lines[-1])
        self.assertIn(" events, 100,000 input / 50,000 output tokens",
                      lines[-1])

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

    def test_scoped_rollup_flags_estimated_cost(self):
        # a fresh DB prices at the effective_from=0 family-default seed, so
        # the one covered event is estimated: the whole figure is an estimate
        self.seed_event(issue_key="AOS-79")
        _, out = run(["token-stats", "--scope", "AOS-79", "--db", str(self.db),
                      "--cwd", str(self.dir)])
        self.assertIn("100,000 input / 50,000 output tokens; the whole figure"
                      " is an estimate (every event at an estimated rate).",
                      out)


# The exact qualifier phrases every report renders (docs/TELEMETRY-CONTRACT.md
# §Pricing table — "Own price vs estimate").
ALL_EST = "the whole figure is an estimate (every event at an estimated rate)"
RATE = 1_699_963_200   # 2023-11-14 12:00 UTC
DAY = datetime.date.fromtimestamp(RATE).isoformat()   # local, as rendered


def stats_row(events=10, unpriced=0, estimated=0, **kw):
    """A fetch_project_stats-shaped row with a priced, dated figure."""
    r = {"path": "/p/alpha", "name": "Alpha", "sessions": 1, "events": events,
         "input": 1000, "output": 500, "cache_read": 0, "cache_write": 0,
         "classic_in": 1.0, "classic_out": 2.0, "cached_r": 0.0,
         "cached_w": 0.0, "rate_from": RATE,
         "unpriced_events": unpriced, "first_seen": None,
         "last_activity": None, "estimated_events": estimated}
    r.update(kw)
    return r


def token_data(events=10, unpriced=0, estimated=0, without=None,
               model="claude-opus-5-5"):
    """A fetch_token_stats-shaped dict with one priced, dated model."""
    return {
        "today": (0, 0, 0, 0, 0), "week": (1000, 500, 0, 0, events),
        "backlog_excluded": 0, "by_project": [], "by_agent": [],
        "by_model": [(model, "heavy", 1000, 500, 3.0, RATE)],
        "by_kind": [], "by_tier": [], "by_issue": [],
        "estimated_by_model": {model: estimated} if estimated else {},
        "events_by_model": {model: events},
        "unpriced_by_model": {model: unpriced} if unpriced else {},
        "models_without_own_price": without if without is not None else [],
    }


def model_cell(md, model="claude-opus-5-5"):
    line = next(l for l in md.splitlines() if l.startswith(f"| {model} |"))
    return line.split(" | ")[-1].rstrip(" |")


class TestEstimateFlags(unittest.TestCase):
    """Reports flag cost figures priced at a family default or an
    ancestor row (ESTIMATED), distinct from unpriced events, and footer the
    models without an own price."""

    # ---- project-stats cost cell
    def est_cell(self, **kw):
        md = report.render_project_stats([stats_row(**kw)])
        return md.splitlines()[2].split(" | ")[7]

    def test_project_stats_none_estimated_says_nothing(self):
        self.assertEqual(self.est_cell(estimated=0), "**$3** ($1 / $2)")

    def test_project_stats_some_estimated(self):
        self.assertEqual(self.est_cell(estimated=3),
                         "**$3** ($1 / $2) — 3 of 10 events at an estimated"
                         " rate")

    def test_project_stats_all_estimated(self):
        self.assertEqual(self.est_cell(estimated=10),
                         f"**$3** ($1 / $2) — {ALL_EST}")

    def test_project_stats_estimated_and_unpriced_stay_distinct(self):
        self.assertEqual(self.est_cell(unpriced=2, estimated=5),
                         "**$3** ($1 / $2) — 2 of 10 events unpriced,"
                         " 5 of 10 events at an estimated rate")

    def test_project_stats_unpriced_only_is_unchanged(self):
        self.assertEqual(self.est_cell(unpriced=2),
                         "**$3** ($1 / $2) — 2 of 10 events unpriced")

    def test_project_stats_not_reported_says_nothing(self):
        # a remote whose reports.sql predates the figure maps it to None
        self.assertEqual(self.est_cell(estimated=None), "**$3** ($1 / $2)")

    # ---- token-stats by-model cell + headline
    def test_token_stats_none_estimated_says_nothing(self):
        md = report.render_token_stats(token_data(estimated=0))
        self.assertEqual(model_cell(md), "$3")
        self.assertNotIn("estimated rate", md)
        self.assertIn(f"(rates as of {DAY}).**", md)

    def test_token_stats_some_estimated(self):
        md = report.render_token_stats(token_data(estimated=4))
        self.assertEqual(model_cell(md),
                         "$3 — 4 of 10 events at an estimated rate")
        self.assertIn(f"(rates as of {DAY}); 4 of 10 events at an"
                      " estimated rate.**", md)

    def test_token_stats_all_estimated(self):
        md = report.render_token_stats(token_data(estimated=10))
        self.assertEqual(model_cell(md), f"$3 — {ALL_EST}")
        self.assertIn(f"(rates as of {DAY}); {ALL_EST}.**", md)

    def test_token_stats_estimated_and_unpriced_stay_distinct(self):
        md = report.render_token_stats(token_data(unpriced=1, estimated=6))
        self.assertEqual(model_cell(md),
                         "$3 — 1 of 10 events unpriced, 6 of 10 events at an"
                         " estimated rate")

    def test_token_stats_unpriced_only_says_nothing_without_an_estimate(self):
        # The "U of M events unpriced" note in token-stats
        # (headline and per-model cell) stays silent when NOTHING in the
        # same figure is also estimated (N == 0) — unpriced-only carries no
        # note here, unlike project-stats, which is unconditional and unaffected.
        md = report.render_token_stats(token_data(unpriced=1, estimated=0))
        self.assertEqual(model_cell(md), "$3")
        self.assertNotIn("unpriced", md)

    def test_token_stats_headline_sums_across_models(self):
        d = token_data(estimated=2)
        d["by_model"].append(("gpt-4o", "unknown", 10, 20, 0.0, None))
        d["events_by_model"]["gpt-4o"] = 3
        d["unpriced_by_model"]["gpt-4o"] = 3
        md = report.render_token_stats(d)
        self.assertIn("; 3 of 13 events unpriced, 2 of 13 events at an"
                      " estimated rate.**", md)
        self.assertEqual(model_cell(md, "gpt-4o"), "unpriced")

    def test_token_stats_not_reported_says_nothing(self):
        d = token_data(estimated=4)
        for k in ("estimated_by_model", "events_by_model",
                  "unpriced_by_model", "models_without_own_price"):
            d[k] = None
        md = report.render_token_stats(d)
        self.assertEqual(model_cell(md), "$3")
        self.assertNotIn("estimated", md)
        self.assertNotIn("No own published price", md)

    # ---- fetch_models_without_own_price
    def test_models_without_own_price_requires_an_event(self):
        # A model row with NO events at all must not appear —
        # models_without_own_price names models with at least one event that
        # resolves to an estimate or nothing, not every unpriced name.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = pathlib.Path(tmp.name) / "usage.db"
        conn = capture.connect(db)
        now = int(time.time())
        groups = capture.aggregate([
            entry(model="claude-sonnet-5", inp=100, out=50, mid="m1",
                  ts=time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                   time.gmtime(now)))])
        with conn:
            capture.insert_events(conn, "/proj", "s1", 0, None, groups)
        conn.execute("INSERT INTO models(name) VALUES ('claude-ghost-1')")
        conn.commit()
        without = report.fetch_models_without_own_price(conn)
        conn.close()
        # claude-sonnet-5 has an event and prices only at the seed family
        # default -> no own row -> included.
        self.assertIn("claude-sonnet-5", without)
        # claude-ghost-1 has no events at all -> excluded, despite also
        # having no matching pricing row.
        self.assertNotIn("claude-ghost-1", without)

    def test_models_without_own_price_excludes_all_zero_token_models(self):
        # A model whose every event is zero-token (e.g. a
        # synthetic bookkeeping entry) has nothing to price and is excluded
        # from the footer, even though it has events and no own price.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = pathlib.Path(tmp.name) / "usage.db"
        conn = capture.connect(db)
        now = int(time.time())
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
        groups = capture.aggregate([
            entry(model="<synthetic>", inp=0, out=0, cr=0, cw=0, mid="m1",
                  ts=ts)])
        with conn:
            capture.insert_events(conn, "/proj", "s1", 0, None, groups)
        conn.close()
        conn = capture.connect(db)
        without = report.fetch_models_without_own_price(conn)
        conn.close()
        self.assertNotIn("<synthetic>", without)

    def test_models_without_own_price_counts_any_single_token_column(self):
        # "zero-token" means EVERY token column is zero: a model whose only
        # non-zero column is input, output, cache read or cache write still
        # has something to price and stays in the list (each column alone).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = pathlib.Path(tmp.name) / "usage.db"
        conn = capture.connect(db)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time()))
        groups = capture.aggregate([
            entry(model="only-input", inp=7, out=0, cr=0, cw=0, mid="m1",
                  ts=ts),
            entry(model="only-output", inp=0, out=7, cr=0, cw=0, mid="m2",
                  ts=ts),
            entry(model="only-cache-read", inp=0, out=0, cr=7, cw=0,
                  mid="m3", ts=ts),
            entry(model="only-cache-write", inp=0, out=0, cr=0, cw=7,
                  mid="m4", ts=ts),
            entry(model="<synthetic>", inp=0, out=0, cr=0, cw=0, mid="m5",
                  ts=ts)])
        with conn:
            capture.insert_events(conn, "/proj", "s1", 0, None, groups)
        # guard the fixture itself: each model's single column really landed
        self.assertEqual(sorted(conn.execute(
            "SELECT m.name, e.in_tok, e.out_tok, e.cache_r, e.cache_w"
            " FROM events e JOIN models m ON m.id = e.model_id").fetchall()), [
            ("<synthetic>", 0, 0, 0, 0), ("only-cache-read", 0, 0, 7, 0),
            ("only-cache-write", 0, 0, 0, 7), ("only-input", 7, 0, 0, 0),
            ("only-output", 0, 7, 0, 0)])
        without = report.fetch_models_without_own_price(conn)
        conn.close()
        self.assertEqual(without, ["only-cache-read", "only-cache-write",
                                   "only-input", "only-output"])

    # ---- footer
    def test_footer_absent_when_every_model_has_own_price(self):
        md = report.render_token_stats(token_data(without=[]))
        self.assertNotIn("No own published price", md)
        self.assertNotIn("pricing-update", md)

    def test_footer_names_models_and_points_to_pricing_update(self):
        md = report.render_token_stats(token_data(
            estimated=10, without=["claude-opus-5-5", "gpt-4o"]))
        last = md.splitlines()[-1]
        self.assertEqual(
            last, "No own published price for `claude-opus-5-5`, `gpt-4o` —"
            " their cost is an estimate (family default or nearest listed"
            " ancestor rate) or unpriced; `/token-telemetry:pricing-update`"
            " refreshes the pricing table.")

    def test_footer_uses_singular_pronoun_for_one_model(self):
        # a single named model reads oddly with the plural "their cost" —
        # the footer must use "its cost" when there is exactly one.
        md = report.render_token_stats(token_data(
            estimated=10, without=["claude-opus-5-5"]))
        last = md.splitlines()[-1]
        self.assertEqual(
            last, "No own published price for `claude-opus-5-5` — its cost"
            " is an estimate (family default or nearest listed ancestor"
            " rate) or unpriced; `/token-telemetry:pricing-update` refreshes"
            " the pricing table.")
        self.assertNotIn("their cost", last)

    def test_footer_sanitizes_hostile_model_name(self):
        hostile = ("evil|name\n# Heading\r\n| a | b |\t**bold** `tick`"
                   " [x](http://evil.example)\x1b[31m‮")
        md = report.render_token_stats(token_data(without=[hostile]))
        footer = [l for l in md.splitlines()
                  if l.startswith("No own published price for")]
        self.assertEqual(len(footer), 1)         # one line: newlines folded
        line = footer[0]
        # nothing after the footer line: the name injected no extra lines
        self.assertEqual(md.splitlines()[-1], line)
        self.assertNotIn("\n# Heading", md)
        # every pipe is escaped, so no table row/column can be forged
        self.assertIsNone(re.search(r"(?<!\\)\|", line))
        # the only backticks are the code-span pair around the name and the
        # pair around the command: the name cannot break out of its span
        self.assertEqual(line.count("`"), 4)
        name_span = line.split("`")[1]
        self.assertEqual(name_span, report.md_cell(hostile))
        self.assertIn("**bold**", name_span)     # inert inside the code span
        # control bytes and bidi overrides are gone
        self.assertTrue(all(ord(ch) >= 0x20 and ord(ch) != 0x7f
                            for ch in line))
        self.assertNotIn("‮", line)

    # ---- scoped rollup
    def scoped(self, events=10, unpriced=0, estimated=0):
        return report.render_scoped_rollup({
            "state": "full", "keys": ["AOS-1"], "invalid": [], "n": 1, "k": 1,
            "in_tok": 1000, "out_tok": 500, "cache_r": 0, "cache_w": 0,
            "events": events, "cost": 3.0, "rate_from": [RATE],
            "unpriced": unpriced, "estimated": estimated})

    def test_scoped_rollup_unpriced_only_says_nothing_without_an_estimate(self):
        # Same gate as token-stats — the scoped rollup's unpriced
        # note stays silent unless the set also carries an estimated marker.
        md = self.scoped(unpriced=1, estimated=0)
        self.assertIn("**$3** — 10 events, 1,000 input / 500 output tokens.",
                      md)
        self.assertNotIn("unpriced", md)

    def test_scoped_rollup_qualifiers(self):
        base = "**$3** — 10 events, 1,000 input / 500 output tokens"
        self.assertIn(base + ".", self.scoped())
        self.assertIn(base + "; 3 of 10 events at an estimated rate.",
                      self.scoped(estimated=3))
        self.assertIn(base + f"; {ALL_EST}.", self.scoped(estimated=10))
        self.assertIn(base + "; 1 of 10 events unpriced, 4 of 10 events at an"
                      " estimated rate.", self.scoped(unpriced=1, estimated=4))

    # ---- end to end over a real DB
    def test_seed_priced_db_flags_estimate_and_footers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = pathlib.Path(tmp.name) / "usage.db"
        conn = capture.connect(db)
        now = int(time.time())
        groups = capture.aggregate([
            entry(model="claude-sonnet-5", inp=1000, out=500, mid="m1",
                  ts=time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                   time.gmtime(now)))])
        with conn:
            capture.insert_events(conn, "/proj", "s1", 0, None, groups)
        conn.close()
        _, ps = run(["project-stats", "--db", str(db)])
        self.assertIn(f"— {ALL_EST} |", ps)
        _, ts = run(["token-stats", "--db", str(db)])
        self.assertIn(f"(seed rates) — {ALL_EST} |", ts)
        self.assertIn("No own published price for `claude-sonnet-5` — its"
                      " cost is an estimate", ts)


if __name__ == "__main__":
    unittest.main()
