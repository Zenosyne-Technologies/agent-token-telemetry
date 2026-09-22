"""Tests for Command B — central file DB -> remote (Supabase) migration (P8).

SECURITY-GATED. Every test runs against a MOCKED HTTP layer: a stateful fake
Supabase (``FakeSupabase``) stands in for ``urllib.request.urlopen``, applying
PostgREST-style upserts to an in-memory store and answering ``count=exact``
probes via a ``Content-Range`` header. No live network, no real Supabase URL,
email or key ever touches the suite — the fakes are obvious
(``https://example.supabase.co``, ``user@example.com``).

Coverage (the DoD's proofs): sync-users-FIRST ordering, ``owner_id`` stamping on
every table, FK upload order, idempotent re-run (no duplicates), the
count-validation gate, that migrate NEVER flips the pointer, rollback on failure
(no flip, local unchanged), flip-back-to-local, cursors never uploaded, the
write-path self-heal users row, and that the local DB is never touched.
"""
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from collections import defaultdict
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import migrate_lib
import remote_migrate
import settings
import supabase_backend

from tests.test_capture import entry

URL = "https://example.supabase.co"
KEY_ENV = "TOKEN_TELEMETRY_SUPABASE_KEY_TESTVAR"
KEY_VALUE = "SENTINEL_PUBLISHABLE_KEY_zzz"
AUTH_UID = "auth-uid-777"


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self._headers = headers or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def getheaders(self):
        return self._headers


class FakeSupabase:
    """A stateful stand-in for ``urllib.request.urlopen``.

    Applies merge/ignore-duplicate upserts to an in-memory ``tables`` store and
    answers count probes, so idempotency and count-validation are exercised
    end-to-end. ``fail_tables`` raise on POST (transport failure); ``drop_tables``
    accept the POST but silently store nothing (forces a count mismatch)."""

    def __init__(self):
        self.tables = defaultdict(list)
        self.calls = []
        self.fail_tables = set()
        self.drop_tables = set()

    def __call__(self, req, timeout=None, context=None):
        url = req.full_url
        method = req.get_method()
        headers = {k.lower(): v for k, v in req.header_items()}
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append({"url": url, "method": method, "headers": headers,
                           "body": body})
        if "/auth/v1/token" in url:
            return FakeResponse(200, json.dumps({
                "access_token": "acc-tok", "refresh_token": "ref-tok",
                "expires_in": 3600, "user": {"id": AUTH_UID}}).encode())
        table = url.split("/rest/v1/")[1].split("?")[0]
        query = url.split("?", 1)[1] if "?" in url else ""
        if method == "GET":
            n = len(self.tables[table])
            return FakeResponse(200, b"[]",
                                headers=[("Content-Range", f"0-0/{n}")])
        # POST (upsert)
        if table in self.fail_tables:
            raise urllib.error.HTTPError(url, 500, "boom", None, None)
        if table not in self.drop_tables:
            on_conflict = ""
            for part in query.split("&"):
                if part.startswith("on_conflict="):
                    on_conflict = part[len("on_conflict="):]
            merge = "resolution=merge-duplicates" in headers.get("prefer", "")
            self._upsert(table, body or [], on_conflict, merge)
        return FakeResponse(201, b"")

    def _upsert(self, table, rows, on_conflict, merge):
        keycols = on_conflict.split(",") if on_conflict else []
        store = self.tables[table]
        for row in rows:
            key = tuple(row.get(c) for c in keycols)
            idx = next((i for i, r in enumerate(store)
                        if tuple(r.get(c) for c in keycols) == key), None)
            if idx is None:
                store.append(row)
            elif merge:
                store[idx] = row

    # helpers
    def rest_posts(self):
        return [c for c in self.calls
                if "/rest/v1/" in c["url"] and c["method"] == "POST"]

    def post_tables(self):
        return [c["url"].split("/rest/v1/")[1].split("?")[0]
                for c in self.rest_posts()]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "telemetry"
        self.db = str(self.dir / "usage.db")
        self._prev_db = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = self.db
        self._prev_key = os.environ.get(KEY_ENV)
        os.environ[KEY_ENV] = KEY_VALUE
        self._prev_sess = os.environ.get(supabase_backend.SESSION_ENV)
        os.environ.pop(supabase_backend.SESSION_ENV, None)
        self._stdin = sys.stdin

    def tearDown(self):
        sys.stdin = self._stdin
        for name, prev in (("TOKEN_TELEMETRY_DB", self._prev_db),
                           (KEY_ENV, self._prev_key),
                           (supabase_backend.SESSION_ENV, self._prev_sess)):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        self.tmp.cleanup()

    def seed_local(self, sessions=(("s1", "/proj"),), events_per=1):
        """A small central DB: identity + users row + projects/models/sessions/
        events + the pricing seed. Returns the local uuid."""
        settings.ensure_identity("Ada Lovelace")
        settings.set_supabase_config(URL, KEY_ENV)
        uid = settings.current_owner_id()
        conn = capture.connect(self.db)
        try:
            migrate_lib.ensure_users_row(conn, uid, "Ada Lovelace")
            for i, (suid, proj) in enumerate(sessions):
                groups = capture.aggregate([
                    entry(model="claude-sonnet-5", inp=100 + i, out=50,
                          cr=10, cw=5, mid=f"m{i}",
                          ts="2026-09-20T10:00:00.000Z")])
                with conn:
                    capture.insert_events(conn, proj, suid, 0, None, groups,
                                          branch="main", commit_sha="abc",
                                          owner_id=uid)
        finally:
            conn.close()
        return uid

    def login(self, fake):
        """Drive a login (against ``fake``) so credentials.json + reconciled
        auth_uid are on file, exactly as the command's login step does before
        migrate."""
        sys.stdin = io.StringIO(json.dumps(
            {"email": "user@example.com", "password": "hunter2"}))
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(remote_migrate.main(["login"]), 0)

    def local_counts(self):
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return remote_migrate._local_counts(conn)
        finally:
            conn.close()


class TestLogin(Base):
    def test_login_reads_stdin_and_never_persists_password(self):
        self.seed_local()
        fake = FakeSupabase()
        sys.stdin = io.StringIO(json.dumps(
            {"email": "user@example.com", "password": "hunter2"}))
        with mock.patch("urllib.request.urlopen", fake):
            rc = remote_migrate.main(["login"])
        self.assertEqual(rc, 0)
        # password only in the request body, never in any URL or in settings
        for c in fake.calls:
            self.assertNotIn("hunter2", c["url"])
        self.assertNotIn("hunter2", settings.settings_path().read_text())
        login = next(c for c in fake.calls if "grant_type=password" in c["url"])
        self.assertEqual(login["body"]["password"], "hunter2")
        # identity reconciled to the auth uid
        self.assertEqual(settings.current_user()["auth_uid"], AUTH_UID)


class TestSyncUsersFirstAndFkOrder(Base):
    def test_users_pushed_first_then_fk_order(self):
        self.seed_local()
        fake = FakeSupabase()
        self.login(fake)
        with mock.patch("urllib.request.urlopen", fake):
            rc = remote_migrate.main(["migrate"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            fake.post_tables(),
            ["users", "projects", "models", "pricing", "sessions", "events"])

    def test_no_cursors_ever_uploaded(self):
        self.seed_local()
        fake = FakeSupabase()
        self.login(fake)
        with mock.patch("urllib.request.urlopen", fake):
            remote_migrate.main(["migrate"])
        for c in fake.calls:
            self.assertNotIn("cursors", c["url"])
        self.assertEqual(fake.tables.get("cursors", []), [])


class TestOwnerStamping(Base):
    def test_every_uploaded_row_carries_the_owner(self):
        self.seed_local(sessions=(("s1", "/proj"), ("s2", "/proj2")))
        fake = FakeSupabase()
        self.login(fake)
        with mock.patch("urllib.request.urlopen", fake):
            remote_migrate.main(["migrate"])
        # users row uuid == owner (auth uid); every owner-scoped table stamped
        self.assertEqual(fake.tables["users"][0]["uuid"], AUTH_UID)
        for table in ("projects", "models", "pricing", "sessions", "events"):
            self.assertTrue(fake.tables[table])
            for row in fake.tables[table]:
                self.assertEqual(row["owner_id"], AUTH_UID, f"{table} owner")

    def test_events_carry_natural_keys_from_local_joins(self):
        self.seed_local()
        fake = FakeSupabase()
        self.login(fake)
        with mock.patch("urllib.request.urlopen", fake):
            remote_migrate.main(["migrate"])
        ev = fake.tables["events"][0]
        self.assertEqual(ev["session_uuid"], "s1")
        self.assertEqual(ev["model_name"], "claude-sonnet-5")
        self.assertEqual(ev["owner_id"], AUTH_UID)
        # a natural-key session row, not a local integer id
        sess = fake.tables["sessions"][0]
        self.assertEqual(sess["uuid"], "s1")
        self.assertEqual(sess["project_path"], "/proj")


class TestCountValidationGate(Base):
    def test_counts_match_and_exit_zero_on_full_upload(self):
        self.seed_local(sessions=(("s1", "/proj"), ("s2", "/proj2")))
        fake = FakeSupabase()
        self.login(fake)
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("sys.stdout", out):
            rc = remote_migrate.main(["migrate"])
        self.assertEqual(rc, 0)
        self.assertIn("counts_match=yes", out.getvalue())
        local = self.local_counts()
        for t in remote_migrate.VALIDATED_TABLES:
            self.assertEqual(len(fake.tables[t]), local[t])

    def test_count_mismatch_fails_and_does_not_flip(self):
        self.seed_local()
        fake = FakeSupabase()
        fake.drop_tables = {"events"}       # accepted but stored nowhere
        self.login(fake)
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("sys.stdout", out):
            rc = remote_migrate.main(["migrate"])
        self.assertEqual(rc, 1)
        self.assertIn("counts_match=no", out.getvalue())
        # pointer NOT flipped
        self.assertEqual(settings.read_settings().get("active_backend"), "local")


class TestIdempotentRerun(Base):
    def test_second_run_adds_no_duplicates(self):
        self.seed_local(sessions=(("s1", "/proj"), ("s2", "/proj")))
        fake = FakeSupabase()
        self.login(fake)
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(remote_migrate.main(["migrate"]), 0)
            first = {t: len(fake.tables[t]) for t in fake.tables}
            self.assertEqual(remote_migrate.main(["migrate"]), 0)
        for t, n in first.items():
            self.assertEqual(len(fake.tables[t]), n, f"{t} duplicated on re-run")


class TestRollbackNoFlip(Base):
    def test_upload_failure_aborts_without_flip_and_leaves_local(self):
        self.seed_local()
        fake = FakeSupabase()
        fake.fail_tables = {"events"}       # transport error mid-upload
        self.login(fake)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = remote_migrate.main(["migrate"])
        self.assertEqual(rc, 1)
        self.assertIn("upload_ok=no", err.getvalue())
        # pointer unflipped, local DB intact and authoritative
        self.assertEqual(settings.read_settings().get("active_backend"), "local")
        self.assertTrue(pathlib.Path(self.db).exists())
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        finally:
            conn.close()


class TestPointerFlipReversible(Base):
    def test_migrate_never_flips_the_pointer_itself(self):
        self.seed_local()
        fake = FakeSupabase()
        self.login(fake)
        with mock.patch("urllib.request.urlopen", fake):
            remote_migrate.main(["migrate"])
        # migrate validated but did NOT flip — that is a separate explicit step
        self.assertEqual(settings.read_settings().get("active_backend"), "local")

    def test_set_backend_flips_and_is_reversible(self):
        self.seed_local()
        self.assertEqual(
            remote_migrate.main(["set-backend", "--backend", "supabase"]), 0)
        self.assertEqual(settings.read_settings()["active_backend"], "supabase")
        # reversible rollback to local
        self.assertEqual(
            remote_migrate.main(["set-backend", "--backend", "local"]), 0)
        self.assertEqual(settings.read_settings()["active_backend"], "local")

    def test_flip_writes_settings_mode_0600(self):
        self.seed_local()
        remote_migrate.main(["set-backend", "--backend", "supabase"])
        import stat
        self.assertEqual(
            stat.S_IMODE(settings.settings_path().stat().st_mode), 0o600)


class TestLocalUntouched(Base):
    def test_migrate_never_deletes_or_mutates_local_rows(self):
        uid = self.seed_local()
        before = self.local_counts()
        fake = FakeSupabase()
        self.login(fake)
        with mock.patch("urllib.request.urlopen", fake):
            remote_migrate.main(["migrate"])
        self.assertTrue(pathlib.Path(self.db).exists())
        self.assertEqual(self.local_counts(), before)


class TestPreflight(Base):
    def test_reports_schema_present_when_all_tables_probe(self):
        self.seed_local()
        fake = FakeSupabase()
        self.login(fake)
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("sys.stdout", out):
            rc = remote_migrate.main(["preflight"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("schema_present=yes", text)
        self.assertIn("reachable=yes", text)
        self.assertIn("session=yes", text)

    def test_reports_missing_table_when_schema_not_applied(self):
        self.seed_local()

        class MissingEvents(FakeSupabase):
            def __call__(self, req, timeout=None, context=None):
                url = req.full_url
                if url.endswith("/rest/v1/events") and req.get_method() == "GET":
                    raise urllib.error.HTTPError(url, 404, "no table", None, None)
                return super().__call__(req, timeout=timeout, context=context)

        fake = MissingEvents()
        self.login(fake)
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("sys.stdout", out):
            remote_migrate.main(["preflight"])
        text = out.getvalue()
        self.assertIn("schema_present=no", text)
        self.assertIn("missing_tables=events", text)


class TestSelfHealUsersRow(Base):
    """The write path (not the migration) upserts the current user's users row
    before its first FK-referencing push — so a fresh-enabled user with no full
    migration still has a valid parent row and events do not loop in the outbox
    on the users FK."""

    def test_write_path_upserts_users_row_first_once(self):
        settings.ensure_identity("Grace Hopper")
        settings.set_supabase_config(URL, KEY_ENV)
        os.environ[supabase_backend.SESSION_ENV] = "acc-tok"
        fake = FakeSupabase()
        b = supabase_backend.SupabaseBackend({"url": URL,
                                              "publishable_key_env": KEY_ENV})
        b.open()
        groups = capture.aggregate([
            entry(model="claude-sonnet-5", inp=100, out=50, cr=10, cw=5,
                  mid="m1", ts="2026-09-20T10:00:00.000Z")])
        with mock.patch("urllib.request.urlopen", fake):
            b.write_events("/proj", "s1", 0, None, groups, owner_id="local-uuid")
            b.write_events("/proj", "s2", 0, None, groups, owner_id="local-uuid")
        # users upsert leads the FIRST firing's REST posts...
        self.assertEqual(fake.post_tables()[0], "users")
        self.assertEqual(fake.tables["users"][0]["name"], "Grace Hopper")
        # ...and is not repeated on the second firing (once per process)
        self.assertEqual(fake.post_tables().count("users"), 1)

    def test_no_users_row_without_a_full_name_on_file(self):
        # owner id but no full_name -> cannot satisfy users.name NOT NULL; skip
        settings.write_settings({"user": {"uuid": "u1"},
                                 "supabase": {"url": URL,
                                              "publishable_key_env": KEY_ENV}})
        os.environ[supabase_backend.SESSION_ENV] = "acc-tok"
        fake = FakeSupabase()
        b = supabase_backend.SupabaseBackend({"url": URL,
                                              "publishable_key_env": KEY_ENV})
        b.open()
        groups = capture.aggregate([
            entry(model="claude-sonnet-5", inp=100, out=50, cr=10, cw=5,
                  mid="m1", ts="2026-09-20T10:00:00.000Z")])
        with mock.patch("urllib.request.urlopen", fake):
            b.write_events("/proj", "s1", 0, None, groups, owner_id="u1")
        self.assertNotIn("users", fake.post_tables())


if __name__ == "__main__":
    unittest.main()
