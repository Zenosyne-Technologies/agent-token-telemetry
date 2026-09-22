import contextlib
import io
import json
import os
import pathlib
import sqlite3
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import manage
import settings

from tests.test_capture import entry


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = manage.main(argv)
    return rc, out.getvalue()


class TestManage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.db = self.dir / "usage.db"

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self):
        conn = capture.connect(self.db)
        capture.record(conn, "/proj", "s1", 0, None,
                       capture.aggregate([entry(mid="m1")]), "/t.jsonl", 10)
        conn.close()

    def test_register_name_creates_row_before_any_capture(self):
        rc, _ = run(["register-name", "--db", str(self.db),
                     "--project", "/fresh", "--name", "Fresh One"])
        self.assertEqual(rc, 0)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT name FROM projects WHERE path='/fresh'").fetchone()[0],
            "Fresh One")
        conn.close()

    def test_clear_mirror_meta_without_db_is_a_silent_noop(self):
        rc, out = run(["clear-mirror-meta", "--db", str(self.db),
                       "--project", "/proj"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing to clear", out)
        self.assertFalse(self.db.exists())  # never creates the DB

    def test_clear_mirror_meta_clears_only_the_target(self):
        conn = capture.connect(self.db)
        conn.execute("INSERT INTO projects(path, mirror_path, mirror_last_at)"
                     " VALUES ('/a', '/a/m.db', 5), ('/b', '/b/m.db', 6)")
        conn.commit()
        conn.close()
        rc, _ = run(["clear-mirror-meta", "--db", str(self.db),
                     "--project", "/a"])
        self.assertEqual(rc, 0)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT path, mirror_path FROM projects ORDER BY path").fetchall(),
            [("/a", None), ("/b", "/b/m.db")])
        conn.close()

    def test_export_refuses_existing_target(self):
        self.seed()
        target = self.dir / "out.db"
        target.write_text("something")
        rc, out = run(["export", "--db", str(self.db),
                       "--project", "/proj", "--out", str(target)])
        self.assertEqual(rc, 1)
        self.assertIn("refusing", out)
        self.assertEqual(target.read_text(), "something")

    def test_export_preserves_project_name(self):
        # common-column introspection must carry v5's projects.name — the old
        # hardcoded column list silently dropped it.
        self.seed()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET name='Named' WHERE path='/proj'")
        conn.commit()
        conn.close()
        target = self.dir / "out.db"
        rc, _ = run(["export", "--db", str(self.db),
                     "--project", "/proj", "--out", str(target)])
        self.assertEqual(rc, 0)
        conn = sqlite3.connect(target)
        self.assertEqual(conn.execute(
            "SELECT name FROM projects WHERE path='/proj'").fetchone()[0],
            "Named")
        conn.close()

    def test_counts_reports_all_four_figures(self):
        self.seed()
        rc, out = run(["counts", "--db", str(self.db), "--project", "/proj"])
        self.assertEqual(rc, 0)
        self.assertIn("events=1", out)
        self.assertIn("sessions=1", out)
        self.assertIn("cursors=1", out)
        self.assertIn("span=", out)

    def test_delete_rejects_unknown_action(self):
        self.seed()
        rc, out = run(["delete", "--db", str(self.db), "--project", "/proj",
                       "--action", "purge", "--detail", "x"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown delete action", out)

    def test_missing_required_argument_fails_cleanly(self):
        rc, out = run(["export", "--db", str(self.db), "--project", "/proj"])
        self.assertEqual(rc, 1)
        self.assertIn("requires --out", out)

    def test_list_projects_renders_names(self):
        self.seed()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET name='Listed' WHERE path='/proj'")
        conn.commit()
        conn.close()
        rc, out = run(["list-projects", "--db", str(self.db)])
        self.assertEqual(rc, 0)
        self.assertIn("| `/proj` | Listed | 1 |", out)


class TestRegisterUser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "telemetry"
        self.db = self.dir / "usage.db"
        # register-user writes settings.json beside the DB, keyed off the env.
        self._prev = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.db)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TOKEN_TELEMETRY_DB", None)
        else:
            os.environ["TOKEN_TELEMETRY_DB"] = self._prev
        self.tmp.cleanup()

    def users(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT uuid, name, created_at FROM users").fetchall()
        finally:
            conn.close()

    def test_mints_uuid_writes_settings_0600_and_upserts_users(self):
        rc, out = run(["register-user", "--db", str(self.db), "--name", "Ada"])
        self.assertEqual(rc, 0)
        self.assertIn("minted", out)
        # settings.json written 0600 with the minted uuid
        sp = settings.settings_path()
        self.assertEqual(stat.S_IMODE(sp.stat().st_mode), 0o600)
        uid = json.loads(sp.read_text())["user"]["uuid"]
        # the users row exists, keyed on that uuid
        rows = self.users()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], uid)
        self.assertEqual(rows[0][1], "Ada")

    def test_the_name_is_never_echoed_to_stdout(self):
        # PII discipline: the full name must not land in command output.
        _, out = run(["register-user", "--db", str(self.db),
                      "--name", "Ada Lovelace"])
        self.assertNotIn("Ada Lovelace", out)

    def test_rerun_keeps_uuid_and_created_at_updates_name(self):
        run(["register-user", "--db", str(self.db), "--name", "Ada"])
        first = self.users()[0]
        rc, out = run(["register-user", "--db", str(self.db),
                       "--name", "Ada L."])
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)  # uuid not re-minted
        second = self.users()[0]
        self.assertEqual(len(self.users()), 1)          # still one row (upsert)
        self.assertEqual(second[0], first[0])           # same uuid
        self.assertEqual(second[2], first[2])           # created_at preserved
        self.assertEqual(second[1], "Ada L.")           # name updated

    def test_requires_a_name(self):
        rc, out = run(["register-user", "--db", str(self.db)])
        self.assertEqual(rc, 1)
        self.assertIn("requires --name", out)


if __name__ == "__main__":
    unittest.main()
