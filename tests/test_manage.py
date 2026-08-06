import contextlib
import io
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import manage

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


if __name__ == "__main__":
    unittest.main()
