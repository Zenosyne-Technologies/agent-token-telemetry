import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture


def entry(model="claude-sonnet-5", inp=100, out=50, cr=10, cw=5,
          side=False, ts="2026-07-17T10:00:00.000Z", typ="assistant"):
    return {
        "type": typ,
        "isSidechain": side,
        "timestamp": ts,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw,
        }},
    }


def write_jsonl(path, entries, trailing_partial=None):
    with open(path, "wb") as f:
        for e in entries:
            f.write(json.dumps(e).encode() + b"\n")
        if trailing_partial is not None:
            f.write(trailing_partial)


class TestReadNewEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_all_from_zero(self):
        write_jsonl(self.path, [entry(), entry()])
        entries, offset = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(offset, self.path.stat().st_size)

    def test_incremental_read_from_offset(self):
        write_jsonl(self.path, [entry()])
        _, offset1 = capture.read_new_entries(self.path, 0)
        with open(self.path, "ab") as f:
            f.write(json.dumps(entry(inp=7)).encode() + b"\n")
        entries, offset2 = capture.read_new_entries(self.path, offset1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"]["usage"]["input_tokens"], 7)
        self.assertEqual(offset2, self.path.stat().st_size)

    def test_partial_trailing_line_not_consumed(self):
        write_jsonl(self.path, [entry()], trailing_partial=b'{"type":"assist')
        entries, offset = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 1)
        # offset stops after the last complete line
        self.assertEqual(offset, self.path.stat().st_size - len(b'{"type":"assist'))

    def test_malformed_lines_skipped(self):
        with open(self.path, "wb") as f:
            f.write(b"not json at all\n")
            f.write(json.dumps(entry()).encode() + b"\n")
        entries, _ = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 1)


class TestAggregate(unittest.TestCase):
    def test_sums_single_model(self):
        groups = capture.aggregate([entry(inp=100, out=50), entry(inp=10, out=5)])
        g = groups[("claude-sonnet-5", 0)]
        self.assertEqual(g["in"], 110)
        self.assertEqual(g["out"], 55)
        self.assertEqual(g["cr"], 20)
        self.assertEqual(g["cw"], 10)

    def test_groups_by_model_and_sidechain(self):
        groups = capture.aggregate([
            entry(model="claude-sonnet-5"),
            entry(model="claude-haiku-4-5", side=True),
        ])
        self.assertIn(("claude-sonnet-5", 0), groups)
        self.assertIn(("claude-haiku-4-5", 1), groups)

    def test_non_assistant_and_no_usage_skipped(self):
        no_usage = {"type": "assistant", "message": {"model": "m"}}
        groups = capture.aggregate([entry(typ="user"), no_usage])
        self.assertEqual(groups, {})

    def test_first_last_timestamps(self):
        groups = capture.aggregate([
            entry(ts="2026-07-17T10:00:00.000Z"),
            entry(ts="2026-07-17T10:00:30.000Z"),
        ])
        g = groups[("claude-sonnet-5", 0)]
        self.assertAlmostEqual(g["last"] - g["first"], 30.0)


class TestDbLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "sub" / "usage.db"
        self.conn = capture.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_created_and_idempotent(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"projects", "models", "sessions", "events", "cursors"}, tables)
        capture.connect(self.db).close()  # second connect must not raise

    def test_get_or_create_dedupes(self):
        a = capture.get_or_create(self.conn, "models", "name", "claude-sonnet-5")
        b = capture.get_or_create(self.conn, "models", "name", "claude-sonnet-5")
        self.assertEqual(a, b)

    def test_record_inserts_events_and_cursor(self):
        groups = capture.aggregate([entry(), entry(model="claude-haiku-4-5", side=True)])
        capture.record(self.conn, "/proj", "sess-1", 0, None, groups, "/t.jsonl", 500)
        rows = self.conn.execute(
            "SELECT kind, in_tok, out_tok FROM events ORDER BY kind").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 0)   # main turn
        self.assertEqual(rows[1][0], 1)   # sidechain -> subagent
        self.assertEqual(capture.get_offset(self.conn, "/t.jsonl"), 500)

    def test_cursor_upsert_advances(self):
        groups = capture.aggregate([entry()])
        capture.record(self.conn, "/proj", "sess-1", 0, None, groups, "/t.jsonl", 100)
        capture.record(self.conn, "/proj", "sess-1", 0, None, groups, "/t.jsonl", 200)
        self.assertEqual(capture.get_offset(self.conn, "/t.jsonl"), 200)

    def test_unknown_transcript_offset_is_zero(self):
        self.assertEqual(capture.get_offset(self.conn, "/never-seen.jsonl"), 0)

    def test_subagent_kind_hint(self):
        groups = capture.aggregate([entry()])  # not sidechain
        capture.record(self.conn, "/proj", "sess-1", 1, "explorer", groups, "/t2.jsonl", 50)
        kind, agent = self.conn.execute(
            "SELECT kind, agent FROM events WHERE session_id="
            "(SELECT id FROM sessions WHERE uuid='sess-1') ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(kind, 1)
        self.assertEqual(agent, "explorer")


import io
import os
import sqlite3 as _sqlite3
import subprocess


# The v0.1.0 shape, frozen here so the migration path is tested against the
# schema real DBs in the field were created with.
V1_SCHEMA = """
CREATE TABLE projects(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL);
CREATE TABLE models  (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE sessions(id INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL,
  project_id INTEGER NOT NULL REFERENCES projects(id));
CREATE TABLE events(
  ts         INTEGER NOT NULL,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  kind       INTEGER NOT NULL,
  agent      TEXT,
  model_id   INTEGER NOT NULL REFERENCES models(id),
  in_tok     INTEGER NOT NULL DEFAULT 0,
  out_tok    INTEGER NOT NULL DEFAULT 0,
  cache_r    INTEGER NOT NULL DEFAULT 0,
  cache_w    INTEGER NOT NULL DEFAULT 0,
  dur_ms     INTEGER,
  branch     TEXT,
  commit_sha TEXT);
CREATE TABLE cursors(transcript TEXT PRIMARY KEY,
  offset INTEGER NOT NULL, session_id INTEGER NOT NULL);
"""


class AlterBlockedConnection(_sqlite3.Connection):
    """ALTER TABLE failing for a transient reason (locked DB, disk full) rather
    than the benign duplicate-column case the migration expects to swallow."""

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("ALTER"):
            raise _sqlite3.OperationalError("database is locked")
        return super().execute(sql, *args)


class TestSchemaV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "usage.db"

    def tearDown(self):
        self.tmp.cleanup()

    def columns(self, conn, table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    def test_fresh_db_is_user_version_2(self):
        conn = capture.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertLessEqual({"issue_key", "task_size", "note"},
                             self.columns(conn, "events"))

    def test_pricing_table_seeded(self):
        conn = capture.connect(self.db)
        self.addCleanup(conn.close)
        rows = conn.execute(
            "SELECT provider, model_prefix, in_usd, out_usd, cache_r_usd,"
            " cache_w_usd, effective_from, source FROM pricing").fetchall()
        self.assertGreaterEqual(len(rows), 4)
        prefixes = {r[1] for r in rows}
        for tier in ("fable", "opus", "sonnet", "haiku"):
            self.assertTrue(any(tier in p for p in prefixes), tier)
        for r in rows:
            self.assertEqual(r[0], "anthropic")
            # Seed rates apply to all history until superseded by a dated row.
            self.assertEqual(r[6], 0)
            self.assertEqual(r[7], "seed-v0.2.0")

    def test_reconnect_does_not_reseed(self):
        capture.connect(self.db).close()
        before = self._pricing_count()
        capture.connect(self.db).close()
        self.assertEqual(self._pricing_count(), before)
        # Re-run the migration itself over an already-seeded table: the seed
        # gate must key on the seed rows, not on the table's existence.
        conn = _sqlite3.connect(self.db)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        migrated = capture.connect(self.db)
        self.addCleanup(migrated.close)
        self.assertEqual(self._pricing_count(), before)
        self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_empty_pricing_table_is_seeded(self):
        # A crash between CREATE TABLE (autocommitted) and the seed INSERT
        # leaves an empty pricing table; the next connect must still seed it.
        conn = _sqlite3.connect(self.db)
        conn.executescript(V1_SCHEMA)
        conn.executescript(capture.PRICING_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        migrated = capture.connect(self.db)
        self.addCleanup(migrated.close)
        self.assertGreaterEqual(self._pricing_count(), 4)

    def test_alter_failure_withholds_stamp_and_retries(self):
        conn = _sqlite3.connect(self.db)
        conn.executescript(V1_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

        blocked = _sqlite3.connect(self.db, factory=AlterBlockedConnection)
        capture.migrate(blocked)
        # Stamping v2 here would strand the DB: the columns are missing and no
        # later connect would ever add them.
        self.assertEqual(blocked.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertNotIn("issue_key", self.columns(blocked, "events"))
        blocked.close()

        retried = capture.connect(self.db)  # transient cause gone
        self.addCleanup(retried.close)
        self.assertEqual(retried.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertLessEqual({"issue_key", "task_size", "note"},
                             self.columns(retried, "events"))

    def _pricing_count(self):
        conn = _sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM pricing").fetchone()[0]
        finally:
            conn.close()

    def test_migrates_v1_db_without_touching_rows(self):
        conn = _sqlite3.connect(self.db)
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO projects(path) VALUES ('/proj')")
        conn.execute("INSERT INTO models(name) VALUES ('claude-sonnet-5')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES ('s1', 1)")
        conn.execute(
            "INSERT INTO events(ts, session_id, kind, agent, model_id, in_tok,"
            " out_tok, cache_r, cache_w, dur_ms, branch, commit_sha)"
            " VALUES (99, 1, 0, 'legacy', 1, 11, 22, 33, 44, 55, 'main', 'abc123')")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

        migrated = capture.connect(self.db)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertLessEqual({"issue_key", "task_size", "note"},
                             self.columns(migrated, "events"))
        self.assertGreaterEqual(migrated.execute(
            "SELECT COUNT(*) FROM pricing").fetchone()[0], 4)
        row = migrated.execute(
            "SELECT ts, agent, in_tok, out_tok, cache_r, cache_w, dur_ms,"
            " branch, commit_sha, issue_key, task_size, note FROM events").fetchall()
        self.assertEqual(row, [(99, "legacy", 11, 22, 33, 44, 55, "main",
                                "abc123", None, None, None)])


class TestIssueKeyRegex(unittest.TestCase):
    def key(self, subject):
        m = capture.ISSUE_KEY_RE.match(subject)
        return m.group(1) if m else None

    def test_matches_keys_including_single_char_projects(self):
        self.assertEqual(self.key("A-1: single letter project"), "A-1")
        self.assertEqual(self.key("AOS-42: multi letter project"), "AOS-42")
        self.assertEqual(self.key("A1-2: digits in the key"), "A1-2")

    def test_rejects_non_key_prefixes(self):
        for subject in ("feat: add a thing", "WIP: stuff", "a-1: lowercase",
                        "feat(AOS-42): scoped conventional commit",
                        "fix AOS-42: not a prefix"):
            self.assertIsNone(self.key(subject), subject)


class TestStrandedMigration(unittest.TestCase):
    """A DB stamped user_version=2 without the v2 columns (a stamp that landed
    before the ALTERs, or a rollback) must heal on the next connect instead of
    failing every capture forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "usage.db"
        conn = _sqlite3.connect(self.db)
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO projects(path) VALUES ('/proj')")
        conn.execute("INSERT INTO models(name) VALUES ('claude-sonnet-5')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES ('s1', 1)")
        conn.execute("PRAGMA user_version=2")  # stranded: stamped, no columns
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_stranded_db_heals_on_next_connect(self):
        conn = capture.connect(self.db)
        self.addCleanup(conn.close)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        self.assertLessEqual({"issue_key", "task_size", "note"}, cols)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        # The seed gate is re-checked too: pricing was never created before.
        self.assertGreaterEqual(
            conn.execute("SELECT COUNT(*) FROM pricing").fetchone()[0], 4)
        # And capture works end to end against the healed DB.
        capture.record(conn, "/proj", "s1", 0, None, capture.aggregate([entry()]),
                       "/t.jsonl", 10, issue_key="AOS-12")
        self.assertEqual(
            conn.execute("SELECT issue_key FROM events").fetchall(), [("AOS-12",)])


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.proj = self.root / "proj"
        (self.proj / ".claude").mkdir(parents=True)
        self.transcript = self.root / "sess.jsonl"
        self.db = self.root / "telemetry" / "usage.db"
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.db)
        self._stdin = sys.stdin

    def tearDown(self):
        sys.stdin = self._stdin
        os.environ.pop("TOKEN_TELEMETRY_DB", None)
        self.tmp.cleanup()

    def run_main(self, session_id="sess-1", event="Stop"):
        sys.stdin = io.StringIO(json.dumps({
            "session_id": session_id,
            "transcript_path": str(self.transcript),
            "cwd": str(self.proj),
            "hook_event_name": event,
        }))
        capture.main()

    def count_events(self):
        if not self.db.exists():
            return 0
        conn = _sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()

    def test_no_marker_no_db(self):
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertFalse(self.db.exists())

    def test_marker_records_event(self):
        (self.proj / ".claude" / "telemetry").touch()
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.count_events(), 1)

    def test_refire_is_noop(self):
        (self.proj / ".claude" / "telemetry").touch()
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.run_main()  # nothing new appended
        self.assertEqual(self.count_events(), 1)

    def test_incremental_second_turn(self):
        (self.proj / ".claude" / "telemetry").touch()
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        with open(self.transcript, "ab") as f:
            f.write(json.dumps(entry(inp=1)).encode() + b"\n")
        self.run_main()
        self.assertEqual(self.count_events(), 2)

    def test_malformed_stdin_never_raises(self):
        sys.stdin = io.StringIO("this is not json")
        capture.main()  # must not raise
        # Pre-opt-in failures must write nothing to disk - not even the
        # telemetry directory itself.
        self.assertFalse(self.db.parent.exists())

    def test_missing_transcript_never_raises(self):
        (self.proj / ".claude" / "telemetry").touch()
        sys.stdin = io.StringIO(json.dumps({
            "session_id": "s", "transcript_path": str(self.root / "nope.jsonl"),
            "cwd": str(self.proj), "hook_event_name": "Stop",
        }))
        capture.main()  # must not raise
        self.assertEqual(self.count_events(), 0)

    def write_sidecar(self, text):
        (self.proj / ".claude" / "telemetry-context.json").write_text(text)

    def init_git_repo(self, subject):
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
               "-C", str(self.proj)]
        subprocess.run(git + ["init", "-q"], check=True, capture_output=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", subject],
                       check=True, capture_output=True)

    def event_context(self):
        conn = _sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT issue_key, task_size, note FROM events").fetchall()
        finally:
            conn.close()

    def test_sidecar_fields_recorded(self):
        (self.proj / ".claude" / "telemetry").touch()
        self.write_sidecar(json.dumps({
            "issue_key": "AOS-42", "project": "p", "size": "m",
            "summary": "Wire the sidecar"}))
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.event_context(),
                         [("AOS-42", "m", "Wire the sidecar")])

    def test_issue_key_falls_back_to_commit_subject(self):
        (self.proj / ".claude" / "telemetry").touch()
        self.init_git_repo("AOS-7: teach capture the fallback")
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.event_context(), [("AOS-7", None, None)])

    def test_issue_key_null_without_sidecar_or_key_prefix(self):
        (self.proj / ".claude" / "telemetry").touch()
        self.init_git_repo("no key in this subject")
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.event_context(), [(None, None, None)])

    def test_non_scalar_sidecar_values_ignored(self):
        (self.proj / ".claude" / "telemetry").touch()
        self.write_sidecar(json.dumps({
            "issue_key": {"nested": "AOS-9"}, "size": ["m"],
            "summary": {"text": "no"}}))
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        # Binding a dict/list raises sqlite3.ProgrammingError - that would take
        # capture offline for as long as the bad sidecar exists.
        self.assertEqual(self.count_events(), 1)
        self.assertEqual(self.event_context(), [(None, None, None)])
        self.assertFalse((self.db.parent / "error.log").exists())

    def test_numeric_sidecar_values_coerced_to_text(self):
        (self.proj / ".claude" / "telemetry").touch()
        self.write_sidecar(json.dumps({"issue_key": "AOS-3", "size": 2,
                                       "summary": 1.5}))
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.event_context(), [("AOS-3", "2", "1.5")])

    def test_malformed_sidecar_ignored(self):
        (self.proj / ".claude" / "telemetry").touch()
        self.write_sidecar("{not json,,,")
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.count_events(), 1)
        self.assertEqual(self.event_context(), [(None, None, None)])
        self.assertFalse((self.db.parent / "error.log").exists())

    def test_oversized_sidecar_ignored(self):
        # A sidecar this large is not agent-written context; parsing it would
        # burn time and memory on every hook firing, so it is skipped unread.
        (self.proj / ".claude" / "telemetry").touch()
        self.write_sidecar(json.dumps({"issue_key": "AOS-99", "size": "m",
                                       "summary": "x" * 70000}))
        self.assertGreater(
            (self.proj / ".claude" / "telemetry-context.json").stat().st_size,
            65536)
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.count_events(), 1)
        self.assertEqual(self.event_context(), [(None, None, None)])
        self.assertFalse((self.db.parent / "error.log").exists())

    def test_error_after_optin_still_logged(self):
        (self.proj / ".claude" / "telemetry").touch()
        write_jsonl(self.transcript, [entry()])
        # Force a failure after the opt-in gate: pre-create the DB path as a
        # directory so sqlite3.connect() raises.
        self.db.mkdir(parents=True)
        self.run_main()  # must not raise
        self.assertTrue((self.db.parent / "error.log").exists())


class TestConcurrency(unittest.TestCase):
    """Regression test for the get_offset/get_or_create race: parallel hook
    firings against the same DB must neither double-count nor drop events."""

    SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "capture.py"
    NPROCS = 8

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.proj = self.root / "proj"
        (self.proj / ".claude").mkdir(parents=True)
        (self.proj / ".claude" / "telemetry").touch()
        self.db = self.root / "telemetry" / "usage.db"
        self.env = dict(os.environ)
        self.env["TOKEN_TELEMETRY_DB"] = str(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def count_events(self):
        conn = _sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()

    def test_parallel_firings_no_double_count_no_drop(self):
        model = "claude-never-before-seen-concurrency-model"
        hook_files = []
        for i in range(self.NPROCS):
            transcript = self.root / f"transcript-{i}.jsonl"
            write_jsonl(transcript, [entry(model=model)])
            hook_path = self.root / f"hook-{i}.json"
            hook_path.write_text(json.dumps({
                "session_id": f"sess-{i}",
                "transcript_path": str(transcript),
                "cwd": str(self.proj),
                "hook_event_name": "SubagentStop",
            }))
            hook_files.append(hook_path)

        # Launch all processes first, feeding each its hook JSON from its own
        # file (not a pipe) so starting them isn't gated on stdin writes -
        # this keeps the firings genuinely simultaneous.
        procs = []
        opened = []
        for hook_path in hook_files:
            f = open(hook_path, "r")
            opened.append(f)
            procs.append(subprocess.Popen(
                [sys.executable, str(self.SCRIPT)],
                stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.env, text=True))

        results = [p.communicate(timeout=60) for p in procs]
        returncodes = [p.returncode for p in procs]
        for f in opened:
            f.close()

        for rc, (out, err) in zip(returncodes, results):
            self.assertEqual(rc, 0, f"stdout={out!r} stderr={err!r}")

        self.assertEqual(self.count_events(), self.NPROCS)
        self.assertFalse((self.db.parent / "error.log").exists())


if __name__ == "__main__":
    unittest.main()
