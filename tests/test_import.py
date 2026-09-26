"""Tests for Command A — import a project's LOCAL mirror into the central DB
(AOS-104 P4, ``manage.py import-check`` / ``import-project``).

These lock the DoD: children-after-parents import with foreign keys remapped
through natural keys (so the source's ids never collide with a populated
central), full-tuple dedupe (re-run adds nothing), cursors never imported,
retro-link scoped to the imported project, the audit row written inside the
import transaction, rollback on an injected failure, the compat safeguard's
abort on an incompatible diff, and its prompt-driven auto-migrate of a pre-v7
central's ``users`` table.
"""
import contextlib
import io
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import manage
import settings

from tests.test_capture import entry, build_v6_db


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = manage.main(argv)
    return rc, out.getvalue()


class ImportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        # settings.json (identity) lives beside the central DB, keyed off the
        # env — point it at the temp dir so no test touches the real ~/.claude.
        self.central = self.dir / "telemetry" / "usage.db"
        self._prev = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.central)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev is None:
            os.environ.pop("TOKEN_TELEMETRY_DB", None)
        else:
            os.environ["TOKEN_TELEMETRY_DB"] = self._prev

    def mirror(self, path, project="/proj", sessions=(("s1", "m1"),),
               model="claude-sonnet-5"):
        """A project-local mirror DB (built through the schema owner, so v7)."""
        conn = capture.connect(path)
        for i, (sid, mid) in enumerate(sessions):
            capture.record(conn, project, sid, 0, None,
                           capture.aggregate([entry(mid=mid, model=model)]),
                           f"/t{i}.jsonl", 10)
        conn.close()
        return path

    def central_conn(self):
        return sqlite3.connect(self.central)

    def count(self, table, conn=None):
        c = conn or self.central_conn()
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            if conn is None:
                c.close()


class TestHappyPath(ImportBase):
    def test_imports_children_after_parents_with_audit_and_retrolink(self):
        src = self.mirror(self.dir / "m.db",
                          sessions=(("s1", "m1"), ("s2", "m2")))
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(src), "--project", "/proj",
                       "--name", "Ada Lovelace"])
        self.assertEqual(rc, 0)
        self.assertIn("+2 events", out)
        conn = self.central_conn()
        self.addCleanup(conn.close)
        self.assertEqual(self.count("events", conn), 2)
        self.assertEqual(self.count("sessions", conn), 2)
        # every imported session bound to the user (retro-link).
        owners = conn.execute("SELECT owner_id FROM sessions").fetchall()
        self.assertTrue(all(o[0] is not None for o in owners))
        # the audit row landed with the moved counts (inside the txn).
        audit = conn.execute(
            "SELECT action, project, detail FROM audit_log").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][0], "import-project")
        self.assertEqual(audit[0][1], "/proj")
        self.assertIn("+2 events", audit[0][2])

    def test_name_is_never_echoed(self):
        src = self.mirror(self.dir / "m.db")
        _, out = run(["import-project", "--db", str(self.central),
                      "--source", str(src), "--project", "/proj",
                      "--name", "Ada Lovelace"])
        self.assertNotIn("Ada Lovelace", out)

    def test_missing_source_fails_cleanly(self):
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(self.dir / "nope.db"),
                       "--project", "/proj", "--name", "Ada"])
        self.assertEqual(rc, 1)
        self.assertIn("source DB does not exist", out)

    def test_unknown_project_in_source_fails(self):
        src = self.mirror(self.dir / "m.db")
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(src), "--project", "/absent",
                       "--name", "Ada"])
        self.assertEqual(rc, 1)
        self.assertIn("project not found in source", out)


class TestIdempotencyAndDedupe(ImportBase):
    def test_rerun_adds_nothing(self):
        src = self.mirror(self.dir / "m.db",
                          sessions=(("s1", "m1"), ("s2", "m2")))
        run(["import-project", "--db", str(self.central), "--source", str(src),
             "--project", "/proj", "--name", "Ada"])
        self.assertEqual(self.count("events"), 2)
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(src), "--project", "/proj"])
        self.assertEqual(rc, 0)
        self.assertIn("+0 events", out)
        self.assertEqual(self.count("events"), 2)   # full-tuple dedupe

    def test_partial_dedupe_imports_only_the_new_rows(self):
        src = self.mirror(self.dir / "m.db", sessions=(("s1", "m1"),))
        run(["import-project", "--db", str(self.central), "--source", str(src),
             "--project", "/proj", "--name", "Ada"])
        self.assertEqual(self.count("events"), 1)
        # add a second session to the SAME source, re-import: only the new row.
        conn = capture.connect(src)
        capture.record(conn, "/proj", "s2", 0, None,
                       capture.aggregate([entry(mid="m2")]), "/t2.jsonl", 10)
        conn.close()
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(src), "--project", "/proj"])
        self.assertEqual(rc, 0)
        self.assertIn("+1 events", out)
        self.assertEqual(self.count("events"), 2)
        self.assertEqual(self.count("sessions"), 2)


class TestForeignKeyRemap(ImportBase):
    def test_import_into_populated_central_never_collides(self):
        # Central already holds a DIFFERENT project — its rows take ids 1..,
        # so the mirror's identical internal ids must be remapped, not reused.
        pre = capture.connect(self.central)
        capture.record(pre, "/other", "o1", 0, None,
                       capture.aggregate([entry(mid="x", model="claude-haiku-4")]),
                       "/o.jsonl", 10)
        pre.close()
        src = self.mirror(self.dir / "m.db")   # internal project id also 1
        rc, _ = run(["import-project", "--db", str(self.central),
                     "--source", str(src), "--project", "/proj", "--name", "Ada"])
        self.assertEqual(rc, 0)
        conn = self.central_conn()
        self.addCleanup(conn.close)
        # both projects present under distinct ids; both projects' events kept.
        self.assertEqual(
            conn.execute("SELECT path FROM projects ORDER BY path").fetchall(),
            [("/other",), ("/proj",)])
        self.assertEqual(self.count("events", conn), 2)
        # the imported session hangs off the /proj project row, not /other.
        pid = conn.execute(
            "SELECT id FROM projects WHERE path='/proj'").fetchone()[0]
        self.assertEqual(
            conn.execute("SELECT project_id FROM sessions WHERE uuid='s1'"
                         ).fetchone()[0], pid)


class TestRetroLinkScoping(ImportBase):
    def test_only_the_imported_projects_sessions_are_linked(self):
        # A pre-existing, anonymous session under a different project must stay
        # anonymous — retro-link is scoped to the imported project.
        pre = capture.connect(self.central)
        capture.record(pre, "/other", "o1", 0, None,
                       capture.aggregate([entry(mid="x")]), "/o.jsonl", 10)
        pre.close()
        src = self.mirror(self.dir / "m.db")
        run(["import-project", "--db", str(self.central), "--source", str(src),
             "--project", "/proj", "--name", "Ada"])
        conn = self.central_conn()
        self.addCleanup(conn.close)
        owners = dict(conn.execute(
            "SELECT s.uuid, s.owner_id FROM sessions s").fetchall())
        self.assertIsNone(owners["o1"])          # untouched other project
        self.assertIsNotNone(owners["s1"])       # imported project linked


class TestCursorsNotImported(ImportBase):
    def test_a_source_with_cursors_never_writes_central_cursors(self):
        # A /storage-separate export carries cursor rows; a mirror does not.
        # Either way, the importer must skip them — central cursors are
        # authoritative and importing foreign cursors could corrupt read state.
        src = self.dir / "export.db"
        exp = capture.connect(src)
        capture.record(exp, "/proj", "s1", 0, None,
                       capture.aggregate([entry(mid="m1")]), "/t.jsonl", 10)
        exp.close()
        # the mirror-build path (capture.record) wrote a cursor row into src.
        self.assertGreater(self.count("cursors", conn=sqlite3.connect(src)), 0)
        rc, _ = run(["import-project", "--db", str(self.central),
                     "--source", str(src), "--project", "/proj", "--name", "Ada"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.count("cursors"), 0)   # none imported


class TestCompatSafeguard(ImportBase):
    def test_incompatible_diff_aborts_without_touching_central(self):
        # Central already has data; a source with a column central lacks must
        # abort the whole import (dropping that column would lose data).
        pre = capture.connect(self.central)
        capture.record(pre, "/keep", "k1", 0, None,
                       capture.aggregate([entry(mid="k")]), "/k.jsonl", 10)
        pre.close()
        before = self.count("events")
        src = self.mirror(self.dir / "newer.db", project="/np")
        conn = capture.connect(src)
        conn.execute("ALTER TABLE events ADD COLUMN extra_metric INTEGER")
        conn.commit()
        conn.close()
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(src), "--project", "/np", "--name", "Ada"])
        self.assertEqual(rc, 1)
        self.assertIn("incompatible", out)
        self.assertIn("extra_metric", out)
        # central untouched: same event count, the new project never created.
        self.assertEqual(self.count("events"), before)
        self.assertEqual(
            self.count("projects",
                       conn=sqlite3.connect(f"file:{self.central}?mode=ro", uri=True)), 1)

    def test_missing_users_table_is_auto_migrated(self):
        # A genuinely pre-v7 central: the import must migrate it to v7 (creating
        # the users table) and write the users row from the supplied name.
        self.central.parent.mkdir(parents=True, exist_ok=True)
        build_v6_db(self.central)
        self.assertEqual(
            self.central_conn().execute("PRAGMA user_version").fetchone()[0], 6)
        src = self.mirror(self.dir / "m.db")
        rc, _ = run(["import-project", "--db", str(self.central),
                     "--source", str(src), "--project", "/proj",
                     "--name", "Grace Hopper"])
        self.assertEqual(rc, 0)
        conn = self.central_conn()
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            capture.SCHEMA_VERSION)
        self.assertEqual(
            conn.execute("SELECT name FROM users").fetchone()[0], "Grace Hopper")
        # the pre-v7 event row survives the migrate.
        self.assertEqual(
            conn.execute("SELECT ts FROM events WHERE ts=99").fetchone()[0], 99)

    def test_no_identity_and_no_name_is_refused(self):
        src = self.mirror(self.dir / "m.db")
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(src), "--project", "/proj"])
        self.assertEqual(rc, 1)
        self.assertIn("no central identity", out)


class TestImportCheck(ImportBase):
    def test_reports_compatible_and_identity_state(self):
        settings.ensure_identity("Ada")
        src = self.mirror(self.dir / "m.db")
        # central exists (identity write does not create the DB) — build it.
        capture.connect(self.central).close()
        rc, out = run(["import-check", "--db", str(self.central),
                       "--source", str(src), "--project", "/proj"])
        self.assertEqual(rc, 0)
        self.assertIn("compatible=yes", out)
        self.assertIn("identity_set=yes", out)
        self.assertIn("blocking_diff=no", out)

    def test_flags_a_blocking_diff(self):
        capture.connect(self.central).close()
        src = self.mirror(self.dir / "newer.db")
        conn = capture.connect(src)
        conn.execute("ALTER TABLE events ADD COLUMN extra_metric INTEGER")
        conn.commit()
        conn.close()
        rc, out = run(["import-check", "--db", str(self.central),
                       "--source", str(src), "--project", "/proj"])
        self.assertEqual(rc, 0)
        self.assertIn("blocking_diff=yes", out)
        self.assertIn("compatible=no", out)


class TestRollback(ImportBase):
    def test_injected_failure_rolls_back_the_whole_import(self):
        settings.ensure_identity("Ada")   # identity set, so no --name needed
        src = self.mirror(self.dir / "m.db",
                          sessions=(("s1", "m1"), ("s2", "m2")))
        original = manage.audit_row

        def boom(*a, **k):
            raise RuntimeError("injected failure")

        manage.audit_row = boom
        self.addCleanup(lambda: setattr(manage, "audit_row", original))
        with self.assertRaises(RuntimeError):
            manage.main(["import-project", "--db", str(self.central),
                         "--source", str(src), "--project", "/proj"])
        # the audit row is written INSIDE the import transaction, so its failure
        # rolls back every inserted event and session — central is untouched.
        self.assertEqual(self.count("events"), 0)
        self.assertEqual(self.count("sessions"), 0)

    def test_converges_after_a_failed_run(self):
        settings.ensure_identity("Ada")
        src = self.mirror(self.dir / "m.db")
        original = manage.audit_row
        manage.audit_row = lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
        try:
            with self.assertRaises(RuntimeError):
                manage.main(["import-project", "--db", str(self.central),
                             "--source", str(src), "--project", "/proj"])
        finally:
            manage.audit_row = original
        # a clean re-run converges: the rolled-back rows import exactly once.
        rc, out = run(["import-project", "--db", str(self.central),
                       "--source", str(src), "--project", "/proj"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.count("events"), 1)


if __name__ == "__main__":
    unittest.main()
