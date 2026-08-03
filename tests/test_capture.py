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
