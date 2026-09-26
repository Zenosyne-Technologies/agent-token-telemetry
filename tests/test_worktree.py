"""Linked git worktrees belong to their MAIN repository's project.

Real git (``git init`` / ``git worktree add`` / ``git submodule add``) and real
SQLite throughout. Every fixture tree lives under a SYMLINKED alias of its temp
dir, so the stored spelling of a path differs from its realpath — the project
key's spelling rule (the main repo's own sessions' spelling, never a duplicate
row) is observable only that way.
"""
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import report

from tests.test_capture import entry, write_jsonl

GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
       "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def git(cwd, *args):
    """Run git in ``cwd`` (fixture setup only; raises on failure)."""
    return subprocess.run(GIT + ["-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(path, name=None):
    """A real repo with one commit; ``name`` writes the kit's PROJECT-INFO."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    if name:
        (path / ".marvin").mkdir()
        (path / ".marvin" / "PROJECT-INFO.md").write_text(
            f"---\nproject: {name}\n---\n")
    (path / "README").write_text("x\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


class Fixture(unittest.TestCase):
    """Temp tree reached through a symlinked alias (``self.base``)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        real = pathlib.Path(self.tmp.name) / "real"
        real.mkdir()
        self.base = pathlib.Path(self.tmp.name) / "alias"
        self.base.symlink_to(real)
        self.db = pathlib.Path(self.tmp.name) / "telemetry" / "usage.db"
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.db)
        self.addCleanup(os.environ.pop, "TOKEN_TELEMETRY_DB", None)
        self._stdin = sys.stdin
        self.addCleanup(setattr, sys, "stdin", self._stdin)

    def main_repo(self, marker="central\n", name="ecool-erp"):
        """``<base>/ecool`` with the kit name; the opt-in marker ONLY here."""
        m = make_repo(self.base / "ecool", name)
        if marker is not None:
            (m / ".claude").mkdir(exist_ok=True)
            (m / ".claude" / "telemetry").write_text(marker)
        return m

    def claude_worktree(self, main, name="marvin-info-command-f726ab",
                        branch="feat-x"):
        wt = main / ".claude" / "worktrees" / name
        git(main, "worktree", "add", "-q", "-b", branch, str(wt))
        return wt

    def capture(self, cwd, session="sess-1", transcript=None):
        transcript = transcript or (pathlib.Path(self.tmp.name) / f"{session}.jsonl")
        if not transcript.exists():
            write_jsonl(transcript, [entry()])
        sys.stdin = io.StringIO(json.dumps({
            "session_id": session, "transcript_path": str(transcript),
            "cwd": str(cwd), "hook_event_name": "Stop"}))
        capture.main()

    def rows(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def projects(self):
        return self.rows("SELECT path, name FROM projects ORDER BY id")


class TestMainRepoRoot(Fixture):
    """``main_repo_root``: the structural boundary (F2) and spelling (F1)."""

    def test_claude_worktree_resolves_to_main_spelling(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        # Exactly the prefix spelling (through the alias), not the realpath.
        self.assertEqual(str(capture.main_repo_root(wt)), str(m))
        self.assertNotEqual(str(m), os.path.realpath(m))

    def test_external_worktree_resolves_to_realpath(self):
        m = self.main_repo()
        ext = self.base / "ext-wt"
        git(m, "worktree", "add", "-q", "-b", "ext", str(ext))
        self.assertEqual(str(capture.main_repo_root(ext)), os.path.realpath(m))

    def test_plain_checkout_and_plain_dir_are_none(self):
        m = self.main_repo()
        plain = self.base / "plain"
        plain.mkdir()
        self.assertIsNone(capture.main_repo_root(m))
        self.assertIsNone(capture.main_repo_root(plain))
        self.assertEqual(capture.find_project_root(plain), plain)

    def test_submodule_is_its_own_project(self):
        lib_src = make_repo(self.base / "lib-src")
        m = self.main_repo()
        git(m, "submodule", "add", "-q", str(lib_src), "lib")
        sub = m / "lib"
        self.assertTrue((sub / ".git").is_file())
        self.assertIn("/modules/", (sub / ".git").read_text())
        self.assertIsNone(capture.main_repo_root(sub))

    def test_bare_repo_worktree_is_not_resolved(self):
        src = make_repo(self.base / "src")
        bare = self.base / "bare.git"
        subprocess.run(GIT + ["clone", "-q", "--bare", str(src), str(bare)],
                       check=True, capture_output=True)
        wt = self.base / "bare-wt"
        git(bare, "worktree", "add", "-q", "-b", "w", str(wt))
        # Common dir is `bare.git`, not `.git` -> today's behaviour.
        self.assertIsNone(capture.main_repo_root(wt))

    # -- malformed pointer files: each falls back (None) -----------------
    def fake_checkout(self, content):
        d = self.base / "fake"
        d.mkdir(exist_ok=True)
        (d / ".git").write_bytes(content)
        return d

    def gitdir_line(self, wt):
        return (wt / ".git").read_bytes()

    def test_oversized_gitfile_falls_back(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        good = self.gitdir_line(wt).rstrip(b"\n")
        # Same valid pointer, padded with trailing spaces past the cap (the
        # parser strips them, so ONLY the size guard can reject it).
        (wt / ".git").write_bytes(good + b" " * capture.GITFILE_MAX_BYTES + b"\n")
        self.assertIsNone(capture.main_repo_root(wt))

    def test_multiline_gitfile_falls_back(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        # A second (blank) line: the parser strips the gitdir value, so ONLY
        # the single-line rule can reject it.
        (wt / ".git").write_bytes(self.gitdir_line(wt) + b"\n")
        self.assertIsNone(capture.main_repo_root(wt))

    def test_gitdir_outside_worktrees_falls_back(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        # A structurally valid pointer whose gitdir is not `<common>/worktrees/`
        # (commondir still resolves to `.git`) — only that check rejects it.
        (m / ".git" / "worktrees").rename(m / ".git" / "elsewhere")
        (wt / ".git").write_text(
            f"gitdir: {m / '.git' / 'elsewhere' / wt.name}\n")
        self.assertIsNone(capture.main_repo_root(wt))

    def test_non_gitdir_line_falls_back(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        line = self.gitdir_line(wt).replace(b"gitdir:", b"notgit:")
        (wt / ".git").write_bytes(line)
        self.assertIsNone(capture.main_repo_root(wt))

    def test_symlinked_gitfile_falls_back(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        real_file = self.base / "pointer"
        real_file.write_bytes(self.gitdir_line(wt))
        (wt / ".git").unlink()
        (wt / ".git").symlink_to(real_file)
        self.assertIsNone(capture.main_repo_root(wt))

    def test_commondir_mismatch_falls_back(self):
        m = self.main_repo()
        other = make_repo(self.base / "other")
        wt = self.claude_worktree(m)
        gitdir = m / ".git" / "worktrees" / wt.name
        (gitdir / "commondir").write_text(os.path.realpath(other / ".git") + "\n")
        self.assertIsNone(capture.main_repo_root(wt))

    def test_oversized_commondir_falls_back(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        gitdir = m / ".git" / "worktrees" / wt.name
        (gitdir / "commondir").write_text(
            "../.." + " " * capture.GITFILE_MAX_BYTES + "\n")
        self.assertIsNone(capture.main_repo_root(wt))

    def test_malformed_gitfile_capture_keeps_todays_behaviour(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        (wt / ".git").write_bytes(b"gitdir: " + b"x" * 5000 + b"\n")
        (wt / ".claude").mkdir(exist_ok=True)
        (wt / ".claude" / "telemetry").write_text("central\n")
        self.capture(wt)
        self.assertEqual([p for p, _ in self.projects()], [str(wt)])


class TestWorktreeCapture(Fixture):
    """End-to-end ``capture.main()`` from inside a worktree."""

    def test_worktree_session_records_under_main_with_kit_name(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        # The name must come from the MAIN repo's PROJECT-INFO, not the
        # worktree's checked-out copy.
        (wt / ".marvin" / "PROJECT-INFO.md").unlink()
        self.capture(wt)
        self.assertEqual(self.projects(), [(str(m), "ecool-erp")])

    def test_branch_and_sha_come_from_the_worktree(self):
        m = self.main_repo()
        wt = self.claude_worktree(m, branch="feat-x")
        (wt / "f").write_text("y\n")
        git(wt, "add", "f")
        git(wt, "commit", "-q", "-m", "AOS-7: worktree commit")
        self.capture(wt)
        self.assertEqual(
            self.rows("SELECT branch, commit_sha, issue_key FROM events"),
            [("feat-x", git(wt, "rev-parse", "--short", "HEAD"), "AOS-7")])

    def test_marker_only_in_main_repo_is_captured(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        self.assertFalse((wt / ".claude" / "telemetry").exists())
        self.assertTrue(capture.is_enabled(wt))
        self.capture(wt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events"), [(1,)])

    def test_no_marker_anywhere_captures_nothing(self):
        m = self.main_repo(marker=None)
        wt = self.claude_worktree(m)
        self.assertFalse(capture.is_enabled(wt))
        self.capture(wt)
        self.assertFalse(self.db.exists())

    def test_main_and_worktree_sessions_share_one_row(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        self.capture(m, session="main-s")
        self.capture(wt, session="wt-s")
        self.assertEqual([p for p, _ in self.projects()], [str(m)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions"), [(2,)])

    def test_external_worktree_reuses_existing_row_spelling(self):
        m = self.main_repo()
        self.capture(m, session="main-s")
        ext = self.base / "ext-wt"
        git(m, "worktree", "add", "-q", "-b", "ext", str(ext))
        self.capture(ext, session="ext-s")
        # realpath-equal lookup keeps the main row's (alias) spelling.
        self.assertEqual([p for p, _ in self.projects()], [str(m)])

    def test_main_project_mode_mirrors_at_main_root(self):
        m = self.main_repo(marker="project\n")
        wt = self.claude_worktree(m)
        self.capture(wt)
        self.assertTrue(capture.mirror_db_path(m).exists())
        self.assertFalse(capture.mirror_db_path(wt).exists())
        self.assertEqual(self.rows("SELECT mirror_path FROM projects"),
                         [(str(capture.mirror_db_path(m)),)])

    def test_worktree_marker_wins_over_main(self):
        m = self.main_repo(marker="project\n")
        wt = self.claude_worktree(m)
        (wt / ".claude").mkdir(exist_ok=True)
        (wt / ".claude" / "telemetry").write_text("central\n")
        self.assertEqual(capture.storage_mode_for(wt, m), "central")
        self.capture(wt)
        self.assertFalse(capture.mirror_db_path(m).exists())

    def test_sidecar_read_from_worktree_first(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        (m / ".claude" / "telemetry-context.json").write_text(
            json.dumps({"issue_key": "AOS-1"}))
        (wt / ".claude").mkdir(exist_ok=True)
        (wt / ".claude" / "telemetry-context.json").write_text(
            json.dumps({"issue_key": "AOS-2"}))
        self.capture(wt)
        self.assertEqual(self.rows("SELECT issue_key FROM events"), [("AOS-2",)])

    def test_sidecar_falls_back_to_main(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        (m / ".claude" / "telemetry-context.json").write_text(
            json.dumps({"issue_key": "AOS-1"}))
        self.capture(wt)
        self.assertEqual(self.rows("SELECT issue_key FROM events"), [("AOS-1",)])

    def test_submodule_session_stays_its_own_project(self):
        lib_src = make_repo(self.base / "lib-src")
        m = self.main_repo()
        git(m, "submodule", "add", "-q", str(lib_src), "lib")
        sub = m / "lib"
        (sub / ".claude").mkdir()
        (sub / ".claude" / "telemetry").write_text("central\n")
        self.capture(sub)
        self.assertEqual([p for p, _ in self.projects()], [str(sub)])

    def test_report_info_uses_main_project_key(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        self.capture(wt)
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        d = report.fetch_info(conn, self.db, str(wt))
        self.assertEqual(d["root"], str(m))
        self.assertEqual(d["events_here"], 1)


class TestWorktreeFold(Fixture):
    """The one-time v8 data step, on DB copies built at v7."""

    def v7_db(self, rows):
        """A v7 DB with ``rows`` = [(path, name, n_sessions)], stamped 7."""
        conn = capture.connect(self.db)
        for i, (path, name, n) in enumerate(rows):
            pid = conn.execute("INSERT INTO projects(path, name) VALUES (?,?)",
                               (str(path), name)).lastrowid
            for j in range(n):
                conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?,?)",
                             (f"s{i}-{j}", pid))
        conn.commit()
        conn.execute("PRAGMA user_version=7")
        conn.close()

    def migrate(self):
        conn = capture.connect(self.db)
        conn.close()

    def sessions_by_project(self):
        return dict(self.rows(
            "SELECT p.path, COUNT(s.id) FROM projects p"
            " LEFT JOIN sessions s ON s.project_id=p.id GROUP BY p.id"))

    def test_deleted_worktree_row_folds_by_path_convention(self):
        m = self.main_repo()
        gone = m / ".claude" / "worktrees" / "marvin-info-command-f726ab"
        self.v7_db([(m, "ecool-erp", 3), (gone, None, 1)])
        self.migrate()
        self.assertEqual(self.sessions_by_project(), {str(m): 4})
        self.assertEqual(self.rows("PRAGMA user_version"), [(8,)])

    def test_nested_worktree_name_folds(self):
        m = self.main_repo()
        gone = m / ".claude" / "worktrees" / "group" / "name"
        self.v7_db([(m, None, 1), (gone, None, 1)])
        self.migrate()
        self.assertEqual(self.sessions_by_project(), {str(m): 2})

    def test_existing_worktree_row_folds_by_resolution(self):
        m = self.main_repo()
        ext = self.base / "ext-wt"
        git(m, "worktree", "add", "-q", "-b", "ext", str(ext))
        self.v7_db([(m, "ecool-erp", 1), (ext, None, 2)])
        self.migrate()
        self.assertEqual(self.sessions_by_project(), {str(m): 3})

    def test_main_row_created_when_absent(self):
        m = self.main_repo()
        wt = self.claude_worktree(m)
        self.v7_db([(wt, "ecool-erp", 2)])
        self.migrate()
        self.assertEqual(self.projects(), [(str(m), "ecool-erp")])
        self.assertEqual(self.sessions_by_project(), {str(m): 2})

    def test_name_copied_only_when_main_has_none(self):
        m = self.main_repo()
        a = m / ".claude" / "worktrees" / "a"
        self.v7_db([(m, "keep-me", 1), (a, "wt-name", 1)])
        self.migrate()
        self.assertEqual(self.projects(), [(str(m), "keep-me")])

    def test_name_copied_into_nameless_main(self):
        m = self.main_repo()
        a = m / ".claude" / "worktrees" / "a"
        self.v7_db([(m, None, 1), (a, "wt-name", 1)])
        self.migrate()
        self.assertEqual(self.projects(), [(str(m), "wt-name")])

    def test_mirror_pair_moves_only_into_an_unconfigured_main(self):
        m = self.main_repo()
        a = m / ".claude" / "worktrees" / "a"
        b = self.base / "other"
        make_repo(b)
        bw = b / ".claude" / "worktrees" / "b"
        self.v7_db([(m, None, 1), (a, None, 1), (b, None, 1), (bw, None, 1)])
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE projects SET mirror_path='wt-a', mirror_last_at=5"
                     " WHERE path=?", (str(a),))
        conn.execute("UPDATE projects SET mirror_path='main-b', mirror_last_at=1"
                     " WHERE path=?", (str(b),))
        conn.execute("UPDATE projects SET mirror_path='wt-b', mirror_last_at=9"
                     " WHERE path=?", (str(bw),))
        conn.commit()
        conn.close()
        self.migrate()
        self.assertEqual(
            self.rows("SELECT path, mirror_path, mirror_last_at FROM projects"
                      " ORDER BY id"),
            [(str(m), "wt-a", 5), (str(b), "main-b", 1)])

    def test_rerun_is_a_noop(self):
        m = self.main_repo()
        gone = m / ".claude" / "worktrees" / "x"
        self.v7_db([(m, None, 1), (gone, None, 1)])
        self.migrate()
        before = self.rows("SELECT * FROM projects ORDER BY id")
        conn = capture.connect(self.db)
        self.assertEqual(capture.fold_worktree_projects(conn), [])
        conn.close()
        self.migrate()
        self.assertEqual(self.rows("SELECT * FROM projects ORDER BY id"), before)

    def test_fold_runs_once_per_db(self):
        m = self.main_repo()
        self.v7_db([(m, None, 1)])
        self.migrate()  # now v8
        gone = m / ".claude" / "worktrees" / "later"
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO projects(path) VALUES (?)", (str(gone),))
        conn.commit()
        self.assertTrue(capture.migrate_v8(conn))
        conn.close()
        # One-time data step: a v8 DB is never folded again.
        self.assertEqual(len(self.projects()), 2)

    def test_unrelated_deeper_match_is_not_folded(self):
        # `<M>` = `<base>/vendor` is neither a project row nor a git repo.
        odd = self.base / "vendor" / ".claude" / "worktrees" / "thing"
        odd.mkdir(parents=True)
        # ...and a trailing-component-only path with no name segment.
        m = self.main_repo()
        bare_component = str(m) + "/.claude/worktrees/"
        self.v7_db([(m, None, 1), (odd, None, 1), (bare_component, None, 1)])
        self.migrate()
        self.assertEqual(self.sessions_by_project(),
                         {str(m): 1, str(odd): 1, bare_component: 1})

    def test_convention_needs_row_or_git_dir_at_prefix(self):
        # `<M>` exists as a plain directory (no .git) and has no row -> left.
        plain = self.base / "plain"
        plain.mkdir()
        gone = plain / ".claude" / "worktrees" / "w"
        self.v7_db([(gone, None, 1)])
        self.migrate()
        self.assertEqual(self.sessions_by_project(), {str(gone): 1})

    def test_convention_accepts_realpath_equal_row(self):
        # Main folder deleted too; the row alone (other spelling) anchors it.
        real_main = os.path.realpath(self.base) + "/gone-main"
        gone = str(self.base) + "/gone-main/.claude/worktrees/w"
        self.v7_db([(real_main, "n", 1), (gone, None, 1)])
        self.migrate()
        self.assertEqual(self.sessions_by_project(), {real_main: 2})

    def test_submodule_row_is_not_folded(self):
        lib_src = make_repo(self.base / "lib-src")
        m = self.main_repo()
        git(m, "submodule", "add", "-q", str(lib_src), "lib")
        self.v7_db([(m, None, 1), (m / "lib", None, 1)])
        self.migrate()
        self.assertEqual(self.sessions_by_project(),
                         {str(m): 1, str(m / "lib"): 1})

    def test_failed_fold_rolls_back_and_retries(self):
        m = self.main_repo()
        gone = m / ".claude" / "worktrees" / "x"
        self.v7_db([(m, None, 1), (gone, None, 1)])
        real = capture.fold_worktree_projects

        def fold_then_fail(conn):
            real(conn)
            raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(capture, "fold_worktree_projects", fold_then_fail):
            self.migrate()
        self.assertEqual(self.rows("PRAGMA user_version"), [(7,)])
        self.assertEqual(self.sessions_by_project(), {str(m): 1, str(gone): 1})
        self.migrate()
        self.assertEqual(self.sessions_by_project(), {str(m): 2})

    def test_events_and_audit_log_untouched(self):
        m = self.main_repo()
        gone = m / ".claude" / "worktrees" / "x"
        self.v7_db([(m, None, 0), (gone, None, 1)])
        conn = sqlite3.connect(self.db)
        mid = conn.execute("INSERT INTO models(name) VALUES ('m')").lastrowid
        conn.execute("INSERT INTO events(ts, session_id, kind, model_id)"
                     " VALUES (1, 1, 0, ?)", (mid,))
        conn.execute("INSERT INTO audit_log(ts, action, project) VALUES"
                     " (1, 'export', ?)", (str(gone),))
        conn.commit()
        conn.close()
        self.migrate()
        self.assertEqual(self.rows(
            "SELECT p.path FROM events e JOIN sessions s ON s.id=e.session_id"
            " JOIN projects p ON p.id=s.project_id"), [(str(m),)])
        self.assertEqual(self.rows("SELECT project FROM audit_log"),
                         [(str(gone),)])


if __name__ == "__main__":
    unittest.main()
