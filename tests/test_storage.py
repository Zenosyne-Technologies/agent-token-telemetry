"""Characterization tests for the storage backend seam (AOS-104 P3).

These lock the P3 acceptance bar: the `StorageBackend` interface exists,
`LocalSqliteBackend` implements it, and routing capture's write unit / cursors /
read-only accessor through the backend produces **byte-for-byte identical** DB
rows and report output versus the pre-seam path of calling the `capture`
functions directly. A regression that changes what the seam writes or reads
fails here.
"""
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import report
import storage

from tests.test_capture import entry


def dump(conn, table):
    """Every column of every row, rowid-ordered — the exact stored bytes."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid").fetchall()


def run_report(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = report.main(argv)
    return rc, out.getvalue()


class TestInterface(unittest.TestCase):
    def test_local_is_a_storage_backend(self):
        self.assertTrue(
            issubclass(storage.LocalSqliteBackend, storage.StorageBackend))

    def test_abstract_base_cannot_instantiate(self):
        with self.assertRaises(TypeError):
            storage.StorageBackend()

    def test_capabilities_flags_are_honest(self):
        caps = storage.LocalSqliteBackend("/nonexistent.db").capabilities()
        self.assertEqual(
            (caps.server_side_aggregation, caps.owns_cursors, caps.multi_user,
             caps.supports_upsert, caps.writable),
            (True, True, False, True, True))


class TestByteForByte(unittest.TestCase):
    """Same input, two write paths — legacy `capture.*` calls vs the backend —
    must land identical rows in every table, and identical report output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _groups(self):
        # Fixed ts + message id => aggregate() is fully deterministic, so any
        # divergence between the two DBs is the seam's doing, not timing.
        return capture.aggregate([
            entry(model="claude-sonnet-5", inp=100000, out=50000, cr=10000,
                  cw=5000, mid="m1", cw1h=2000,
                  ts="2026-09-20T10:00:00.000Z"),
            entry(model="claude-opus-5", inp=2000, out=800, mid="m2",
                  ts="2026-09-20T10:05:00.000Z"),
        ])

    def _seed_legacy(self, db):
        conn = capture.connect(db)
        with conn:
            sid = capture.insert_events(conn, "/proj", "s1", 0, None,
                                        self._groups(), "main", "abc123",
                                        "AOS-104", "m", None, owner_id="u-1")
            capture.write_cursor(conn, "/t.jsonl", 4096, sid)
        conn.close()

    def _seed_backend(self, db):
        b = storage.LocalSqliteBackend(db).open()
        try:
            with b.conn:
                sid = b.write_events("/proj", "s1", 0, None, self._groups(),
                                     branch="main", commit_sha="abc123",
                                     issue_key="AOS-104", task_size="m",
                                     note=None, owner_id="u-1")
                b.cursor_set("/t.jsonl", 4096, sid)
        finally:
            b.close()

    def test_rows_identical_across_all_tables(self):
        legacy, seam = self.dir / "legacy.db", self.dir / "seam.db"
        self._seed_legacy(legacy)
        self._seed_backend(seam)
        lc = capture.connect(legacy)
        sc = capture.connect(seam)
        try:
            for table in ("events", "cursors", "sessions", "projects",
                          "models"):
                self.assertEqual(dump(lc, table), dump(sc, table),
                                 f"{table} diverged between legacy and seam")
        finally:
            lc.close()
            sc.close()

    def test_cursor_get_round_trips_through_backend(self):
        db = self.dir / "cur.db"
        self._seed_backend(db)
        b = storage.LocalSqliteBackend(db).open()
        try:
            self.assertEqual(b.cursor_get("/t.jsonl"), 4096)
            self.assertEqual(b.cursor_get("/never-seen.jsonl"), 0)
        finally:
            b.close()

    def test_schema_version_matches_capture(self):
        db = self.dir / "sv.db"
        b = storage.LocalSqliteBackend(db).open()
        try:
            self.assertEqual(b.schema_version(), capture.SCHEMA_VERSION)
        finally:
            b.close()

    def test_report_output_identical_through_the_seam(self):
        legacy, seam = self.dir / "legacy.db", self.dir / "seam.db"
        self._seed_legacy(legacy)
        self._seed_backend(seam)
        # project-stats is all-time (no rolling window), so it is deterministic
        # given the fixed-ts rows; open_ro now routes through the backend.
        _, legacy_out = run_report(["project-stats", "--db", str(legacy)])
        _, seam_out = run_report(["project-stats", "--db", str(seam)])
        self.assertEqual(legacy_out, seam_out)
        # renders the path basename, proving real rows flowed through the seam.
        self.assertIn("| proj |", legacy_out)


class TestOpenRo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_db_is_none(self):
        self.assertIsNone(report.open_ro(self.dir / "nope.db"))
        self.assertIsNone(
            storage.LocalSqliteBackend(self.dir / "nope.db").open_ro())

    def test_present_db_opens_read_only(self):
        db = self.dir / "usage.db"
        capture.connect(db).close()
        conn = report.open_ro(db)
        self.assertIsNotNone(conn)
        try:
            # read works; write is refused by the mode=ro connection.
            conn.execute("SELECT COUNT(*) FROM events").fetchone()
            with self.assertRaises(Exception):
                conn.execute(
                    "INSERT INTO projects(path) VALUES ('/x')")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
