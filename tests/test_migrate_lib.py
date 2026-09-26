"""Tests for the migration primitives library (AOS-104 P5).

These lock the P5 acceptance bar: the three primitives are pure/idempotent as
specified. `compat_report` inspects without mutating and never throws on a
pre-v7 or malformed DB; `retro_link` scopes, globals and is idempotent (second
run = 0 rows); `ensure_users_row` upserts idempotently (created_at preserved on
update) and auto-migrates a genuine pre-v7 DB to v7 before inserting.
"""
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import migrate_lib

from tests.test_capture import build_v6_db


def v7_db(path):
    """A fresh, correctly-stamped v7 store (schema owner = capture.connect)."""
    conn = capture.connect(path)
    return conn


class TestCompatReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_two_matching_v7_dbs_are_compatible(self):
        src = v7_db(self.dir / "src.db")
        dst = v7_db(self.dir / "dst.db")
        self.addCleanup(src.close)
        self.addCleanup(dst.close)
        rep = migrate_lib.compat_report(src, dst, capture.SCHEMA_VERSION)
        self.assertEqual((rep.src_version, rep.dst_version),
                         (capture.SCHEMA_VERSION, capture.SCHEMA_VERSION))
        self.assertTrue(rep.versions_match)
        self.assertTrue(rep.src_matches_kit and rep.dst_matches_kit)
        self.assertTrue(rep.users_at_dst)
        self.assertEqual(rep.table_diffs, ())
        self.assertFalse(rep.has_structure_diff)
        self.assertTrue(rep.compatible)

    def test_version_mismatch_is_reported(self):
        src = v7_db(self.dir / "src.db")
        dst = v7_db(self.dir / "dst.db")
        self.addCleanup(src.close)
        self.addCleanup(dst.close)
        # Same shape, only the stamp differs — isolates the version check from
        # structure/users diffs.
        dst.execute("PRAGMA user_version=6")
        rep = migrate_lib.compat_report(src, dst, capture.SCHEMA_VERSION)
        self.assertEqual((rep.src_version, rep.dst_version),
                         (capture.SCHEMA_VERSION, 6))
        self.assertFalse(rep.versions_match)
        self.assertTrue(rep.src_matches_kit)
        self.assertFalse(rep.dst_matches_kit)
        self.assertTrue(rep.users_at_dst)  # still same shape
        self.assertFalse(rep.compatible)

    def test_structure_diff_surfaces_a_column_in_one_not_the_other(self):
        src = v7_db(self.dir / "src.db")
        dst = v7_db(self.dir / "dst.db")
        self.addCleanup(src.close)
        self.addCleanup(dst.close)
        src.execute("ALTER TABLE events ADD COLUMN extra_metric INTEGER")
        rep = migrate_lib.compat_report(src, dst, capture.SCHEMA_VERSION)
        self.assertTrue(rep.versions_match)  # ALTER does not bump user_version
        self.assertTrue(rep.has_structure_diff)
        events_diff = [d for d in rep.table_diffs if d.table == "events"]
        self.assertEqual(len(events_diff), 1)
        self.assertEqual(events_diff[0].only_in_src, ("extra_metric",))
        self.assertEqual(events_diff[0].only_in_dst, ())
        self.assertFalse(rep.compatible)

    def test_destination_missing_users_table_does_not_throw(self):
        src = v7_db(self.dir / "src.db")
        self.addCleanup(src.close)
        # A genuine pre-v7 destination: no users table, no owner_id column.
        build_v6_db(self.dir / "dst.db")
        dst = sqlite3.connect(self.dir / "dst.db")
        self.addCleanup(dst.close)
        rep = migrate_lib.compat_report(src, dst, capture.SCHEMA_VERSION)
        self.assertFalse(rep.users_at_dst)
        self.assertEqual((rep.src_version, rep.dst_version),
                         (capture.SCHEMA_VERSION, 6))
        self.assertFalse(rep.versions_match)
        self.assertFalse(rep.compatible)
        # The users-table asymmetry shows up as a structure diff too.
        self.assertIn("users", {d.table for d in rep.table_diffs})

    def test_report_never_mutates_either_connection(self):
        src = v7_db(self.dir / "src.db")
        dst = v7_db(self.dir / "dst.db")
        self.addCleanup(src.close)
        self.addCleanup(dst.close)
        migrate_lib.compat_report(src, dst, capture.SCHEMA_VERSION)
        # user_version unchanged, users table still empty — no side effects.
        self.assertEqual(dst.execute("PRAGMA user_version").fetchone()[0],
                         capture.SCHEMA_VERSION)
        self.assertEqual(
            dst.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)


class TestRetroLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = capture.connect(pathlib.Path(self.tmp.name) / "usage.db")
        self.addCleanup(self.conn.close)
        with self.conn:
            self.conn.execute("INSERT INTO projects(path) VALUES ('/a'), ('/b')")
            self.conn.execute(
                "INSERT INTO users(uuid, name, created_at)"
                " VALUES ('owner-x', 'X', 1)")
            # project 1 (/a): two anonymous + one already-owned.
            self.conn.execute(
                "INSERT INTO sessions(uuid, project_id, owner_id) VALUES"
                " ('a1', 1, NULL), ('a2', 1, NULL), ('a3', 1, 'owner-x')")
            # project 2 (/b): one anonymous.
            self.conn.execute(
                "INSERT INTO sessions(uuid, project_id, owner_id)"
                " VALUES ('b1', 2, NULL)")

    def owners(self):
        return dict(self.conn.execute(
            "SELECT uuid, owner_id FROM sessions ORDER BY uuid").fetchall())

    def test_scoped_then_global_then_idempotent(self):
        # Scoped to project 1: only /a's two anonymous rows link; /b untouched.
        n = migrate_lib.retro_link(self.conn, "me-uuid", project_id=1)
        self.assertEqual(n, 2)
        self.assertEqual(self.owners(), {
            "a1": "me-uuid", "a2": "me-uuid", "a3": "owner-x", "b1": None})
        # Global: only the remaining anonymous row (/b) links; owned row is
        # never re-stamped.
        n = migrate_lib.retro_link(self.conn, "me-uuid")
        self.assertEqual(n, 1)
        self.assertEqual(self.owners(), {
            "a1": "me-uuid", "a2": "me-uuid", "a3": "owner-x", "b1": "me-uuid"})
        # Idempotent: nothing is NULL anymore, so a second global run is a no-op.
        self.assertEqual(migrate_lib.retro_link(self.conn, "me-uuid"), 0)

    def test_binds_uuid_as_argument(self):
        # A uuid containing SQL metacharacters is stored verbatim, not executed.
        hostile = "u'); DROP TABLE sessions;--"
        n = migrate_lib.retro_link(self.conn, hostile)
        self.assertEqual(n, 3)  # a1, a2, b1
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions WHERE owner_id=?",
                              (hostile,)).fetchone()[0], 3)


class TestEnsureUsersRow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_upsert_is_idempotent_and_preserves_created_at(self):
        conn = capture.connect(self.dir / "usage.db")
        self.addCleanup(conn.close)
        migrate_lib.ensure_users_row(conn, "u1", "Ada Lovelace")
        self.assertEqual(
            conn.execute("SELECT name FROM users WHERE uuid='u1'").fetchone()[0],
            "Ada Lovelace")
        # Pin created_at to a known sentinel to prove the update never rewrites it.
        with conn:
            conn.execute("UPDATE users SET created_at=12345 WHERE uuid='u1'")
        migrate_lib.ensure_users_row(conn, "u1", "Ada L.")
        name, created = conn.execute(
            "SELECT name, created_at FROM users WHERE uuid='u1'").fetchone()
        self.assertEqual(name, "Ada L.")            # name updated in place
        self.assertEqual(created, 12345)            # created_at preserved
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_auto_migrates_a_pre_v7_db_then_inserts(self):
        build_v6_db(self.dir / "usage.db")
        conn = sqlite3.connect(self.dir / "usage.db")
        self.addCleanup(conn.close)
        # Precondition: genuinely pre-v7 — no users table, no owner_id column.
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertFalse(migrate_lib._has_table(conn, "users"))
        self.assertNotIn(
            "owner_id", {r[1] for r in conn.execute("PRAGMA table_info(sessions)")})

        migrate_lib.ensure_users_row(conn, "u1", "Grace Hopper")

        # The store reached the current version and the row landed.
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         capture.SCHEMA_VERSION)
        self.assertTrue(migrate_lib._has_table(conn, "users"))
        self.assertIn(
            "owner_id", {r[1] for r in conn.execute("PRAGMA table_info(sessions)")})
        self.assertEqual(
            conn.execute("SELECT name FROM users WHERE uuid='u1'").fetchone()[0],
            "Grace Hopper")
        # The pre-existing v6 event row is untouched by the migrate.
        self.assertEqual(conn.execute("SELECT ts FROM events").fetchall(), [(99,)])


if __name__ == "__main__":
    unittest.main()
