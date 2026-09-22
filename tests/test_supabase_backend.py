"""Tests for the Supabase remote write backend (AOS-104 P6). SECURITY-GATED.

Every test runs against a MOCKED HTTP layer — `urllib.request.urlopen` is
monkeypatched, so no live network, no real Supabase URL/email/key ever touches
the suite. Fakes are obvious (`https://example.supabase.co`, `user@example.com`).

Coverage: successful upsert write, remote-failure → outbox + capture still
exits 0, offline-outbox re-send, token refresh, login/identity reconcile, and
the security invariants the validator will hunt — TLS verification always on,
no secret in any URL, no secret in any log, publishable key read from env by
name, and no service_role/secret-key or non-stdlib dependency anywhere.
"""
import io
import json
import os
import pathlib
import sqlite3
import ssl
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import settings
import storage
import supabase_backend

from tests.test_capture import entry, write_jsonl

URL = "https://example.supabase.co"
KEY_ENV = "TOKEN_TELEMETRY_SUPABASE_KEY_TESTVAR"
KEY_VALUE = "SENTINEL_PUBLISHABLE_KEY_zzz"
ACCESS_TOKEN = "SENTINEL_ACCESS_TOKEN_zzz"


class FakeResponse:
    """A minimal urlopen return value: a context manager exposing status/read."""

    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


class Recorder:
    """A stand-in for `urllib.request.urlopen` that records every request and
    delegates the response (or an exception) to a swappable responder."""

    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    def __call__(self, req, timeout=None, context=None):
        headers = {k.lower(): v for k, v in req.header_items()}
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append({
            "url": req.full_url, "method": req.get_method(),
            "headers": headers, "body": body,
            "timeout": timeout, "context": context})
        return self.responder(req.full_url, body)

    def urls(self):
        return [c["url"] for c in self.calls]

    def rest_calls(self):
        return [c for c in self.calls if "/rest/v1/" in c["url"]]

    def call_to(self, needle):
        for c in self.calls:
            if needle in c["url"]:
                return c
        return None


def ok_responder(url, body):
    """2xx for every call; token endpoints return a fresh session."""
    if "/auth/v1/token" in url:
        return FakeResponse(200, json.dumps({
            "access_token": "refreshed-token", "refresh_token": "r2",
            "expires_in": 3600, "user": {"id": "auth-uid-xyz"}}).encode())
    return FakeResponse(201, b"")


def boom_responder(url, body):
    """Every call fails at the transport layer (remote unreachable)."""
    import urllib.error
    raise urllib.error.URLError("mock: remote unreachable")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "telemetry"
        self._prev_db = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.dir / "usage.db")
        self._prev_key = os.environ.get(KEY_ENV)
        os.environ[KEY_ENV] = KEY_VALUE
        self._prev_sess = os.environ.get(supabase_backend.SESSION_ENV)

    def tearDown(self):
        for name, prev in (("TOKEN_TELEMETRY_DB", self._prev_db),
                           (KEY_ENV, self._prev_key),
                           (supabase_backend.SESSION_ENV, self._prev_sess)):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        self.tmp.cleanup()

    def cfg(self):
        return {"url": URL, "publishable_key_env": KEY_ENV}

    def backend(self):
        b = supabase_backend.SupabaseBackend(self.cfg())
        b.open()
        return b

    def groups(self):
        return capture.aggregate([
            entry(model="claude-sonnet-5", inp=100000, out=50000, cr=10000,
                  cw=5000, mid="m1", ts="2026-09-20T10:00:00.000Z")])

    def with_session_env(self, token=ACCESS_TOKEN):
        os.environ[supabase_backend.SESSION_ENV] = token


class TestContract(Base):
    def test_is_a_storage_backend(self):
        self.assertTrue(
            issubclass(supabase_backend.SupabaseBackend, storage.StorageBackend))

    def test_capabilities_are_honest(self):
        caps = self.backend().capabilities()
        # P9 delivers server-side aggregation via the reports.sql RPC.
        self.assertEqual(
            (caps.server_side_aggregation, caps.owns_cursors, caps.multi_user,
             caps.supports_upsert, caps.writable),
            (True, False, True, True, True))

    def test_schema_is_deferred_and_no_local_read_connection(self):
        b = self.backend()
        # Aggregation runs REMOTELY via RPC (P9), so there is still no local
        # read-only SQL connection to hand back.
        self.assertIsNone(b.open_ro())
        self.assertIsNone(b.schema_version())  # remote schema is provisioned P7
        self.assertIsNone(b.ensure_schema())

    def test_cursor_methods_refuse(self):
        b = self.backend()
        with self.assertRaises(NotImplementedError):
            b.cursor_get("/t.jsonl")
        with self.assertRaises(NotImplementedError):
            b.cursor_set("/t.jsonl", 10, 1)


class TestSuccessfulWrite(Base):
    def test_write_upserts_in_fk_order_with_auth_headers(self):
        self.with_session_env()
        rec = Recorder(ok_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups(),
                           branch="main", commit_sha="abc",
                           issue_key="AOS-110", task_size="m",
                           owner_id="local-uuid")
        rests = [c["url"] for c in rec.rest_calls()]
        self.assertEqual(rests, [
            f"{URL}/rest/v1/projects?on_conflict=owner_id,path",
            f"{URL}/rest/v1/models?on_conflict=owner_id,name",
            f"{URL}/rest/v1/sessions?on_conflict=owner_id,uuid",
            f"{URL}/rest/v1/events?on_conflict="
            f"{supabase_backend.EVENTS_ON_CONFLICT}"])
        for c in rec.rest_calls():
            self.assertTrue(c["url"].startswith("https://"))
            self.assertEqual(c["headers"]["apikey"], KEY_VALUE)
            self.assertEqual(c["headers"]["authorization"],
                             f"Bearer {ACCESS_TOKEN}")
            self.assertIn("merge-duplicates", c["headers"]["prefer"])
        self.assertEqual(b.outbox_count(), 0)

    def test_event_and_session_rows_carry_owner_and_fields(self):
        self.with_session_env()
        rec = Recorder(ok_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups(),
                           owner_id="local-uuid")
        # every parent upsert carries owner_id (uniform owner-scoping), so the
        # owner-scoped unique keys and RLS gate apply to all tables, not just
        # sessions/events.
        proj = rec.call_to("/rest/v1/projects")["body"][0]
        self.assertEqual(proj, {"owner_id": "local-uuid", "path": "/proj"})
        model = rec.call_to("/rest/v1/models")["body"][0]
        self.assertEqual(model["owner_id"], "local-uuid")
        sess = rec.call_to("/rest/v1/sessions")["body"][0]
        self.assertEqual(sess["uuid"], "s1")
        self.assertEqual(sess["owner_id"], "local-uuid")
        self.assertEqual(sess["project_path"], "/proj")
        ev = rec.call_to("/rest/v1/events")["body"][0]
        self.assertEqual(ev["session_uuid"], "s1")
        self.assertEqual(ev["model_name"], "claude-sonnet-5")
        self.assertEqual(ev["owner_id"], "local-uuid")
        self.assertEqual(ev["in_tok"], 100000)
        self.assertEqual(ev["out_tok"], 50000)

    def test_bounded_timeout_is_set(self):
        self.with_session_env()
        rec = Recorder(ok_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups())
        for c in rec.calls:
            self.assertEqual(c["timeout"], supabase_backend.REMOTE_TIMEOUT)
            self.assertIsNotNone(c["timeout"])


class TestNeverBreaksSession(Base):
    def test_remote_failure_goes_to_outbox_and_does_not_raise(self):
        self.with_session_env()
        rec = Recorder(boom_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            # must NOT raise
            b.write_events("/proj", "s1", 0, None, self.groups(),
                           owner_id="local-uuid")
        self.assertEqual(b.outbox_count(), 1)

    def test_no_secret_in_error_log_on_failure(self):
        self.with_session_env()
        rec = Recorder(boom_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups())
        log = self.dir / "error.log"
        self.assertTrue(log.exists())
        text = log.read_text()
        self.assertNotIn(KEY_VALUE, text)
        self.assertNotIn(ACCESS_TOKEN, text)

    def test_missing_publishable_key_defers_without_network(self):
        self.with_session_env()
        os.environ.pop(KEY_ENV, None)  # the named env var is unset
        rec = Recorder(ok_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups())
        self.assertEqual(rec.calls, [])          # no request attempted
        self.assertEqual(b.outbox_count(), 1)    # retained for later


class TestOutboxResend(Base):
    def test_backlog_resends_on_a_later_firing(self):
        self.with_session_env()
        b = self.backend()
        fail = Recorder(boom_responder)
        with mock.patch("urllib.request.urlopen", fail):
            b.write_events("/proj", "s-old", 0, None, self.groups())
        self.assertEqual(b.outbox_count(), 1)
        ok = Recorder(ok_responder)
        with mock.patch("urllib.request.urlopen", ok):
            b.write_events("/proj", "s-new", 0, None, self.groups())
        # both the backlogged and the current firing landed; outbox drained
        self.assertEqual(b.outbox_count(), 0)
        session_bodies = [c["body"][0]["uuid"] for c in ok.calls
                          if "/rest/v1/sessions" in c["url"]]
        self.assertIn("s-old", session_bodies)
        self.assertIn("s-new", session_bodies)


class TestTokenRefresh(Base):
    def test_expired_token_is_refreshed_before_write(self):
        # a credentials file with an already-expired access token + refresh token
        supabase_backend.write_credentials({
            "access_token": "stale-token", "refresh_token": "r1",
            "expires_at": 1, "uid": "auth-uid-xyz"})
        rec = Recorder(ok_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups())
        refresh = rec.call_to("grant_type=refresh_token")
        self.assertIsNotNone(refresh)
        self.assertEqual(refresh["body"], {"refresh_token": "r1"})
        # subsequent REST writes use the refreshed bearer token
        for c in rec.rest_calls():
            self.assertEqual(c["headers"]["authorization"],
                             "Bearer refreshed-token")
        # and the new session is persisted
        self.assertEqual(supabase_backend.read_credentials()["access_token"],
                         "refreshed-token")


class TestLoginAndReconcile(Base):
    def login_responder(self, url, body):
        if "grant_type=password" in url:
            return FakeResponse(200, json.dumps({
                "access_token": "acc-tok-123", "refresh_token": "ref-tok-123",
                "expires_in": 3600, "user": {"id": "auth-uid-777"}}).encode())
        return FakeResponse(201, b"")

    def test_login_stores_tokens_0600_and_reconciles_identity(self):
        settings.ensure_identity("Ada Lovelace")  # local uuid minted
        local_uuid = settings.current_user()["uuid"]
        rec = Recorder(self.login_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            result = b.login("user@example.com", "hunter2")
        # returns identity, never the tokens
        self.assertEqual(result["uid"], "auth-uid-777")
        self.assertNotIn("access_token", result)
        # credentials file: present, 0600, holds the token
        creds = supabase_backend.credentials_path()
        self.assertEqual(stat.S_IMODE(creds.stat().st_mode), 0o600)
        self.assertEqual(supabase_backend.read_credentials()["access_token"],
                         "acc-tok-123")
        # identity reconciled: auth_uid recorded, local uuid preserved
        user = settings.current_user()
        self.assertEqual(user["uuid"], local_uuid)
        self.assertEqual(user["auth_uid"], "auth-uid-777")
        # settings.json (non-secret) never holds the token
        self.assertNotIn("acc-tok-123",
                         settings.settings_path().read_text())

    def test_password_travels_in_body_not_url(self):
        settings.ensure_identity("Ada")
        rec = Recorder(self.login_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.login("user@example.com", "hunter2")
        login = rec.call_to("grant_type=password")
        self.assertNotIn("hunter2", login["url"])
        self.assertNotIn("user@example.com", login["url"])
        self.assertEqual(login["body"]["password"], "hunter2")
        self.assertEqual(login["body"]["email"], "user@example.com")

    def test_remote_rows_prefer_the_auth_uid(self):
        settings.ensure_identity("Ada")
        rec = Recorder(self.login_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.login("user@example.com", "hunter2")
            b.write_events("/proj", "s1", 0, None, self.groups(),
                           owner_id="local-uuid")
        ev = rec.call_to("/rest/v1/events")["body"][0]
        self.assertEqual(ev["owner_id"], "auth-uid-777")  # not local-uuid


class TestSecurityInvariants(Base):
    def test_tls_context_verifies_certificate_and_hostname(self):
        self.with_session_env()
        rec = Recorder(ok_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups())
        self.assertTrue(rec.calls)
        for c in rec.calls:
            ctx = c["context"]
            self.assertIsInstance(ctx, ssl.SSLContext)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)

    def test_no_secret_appears_in_any_url(self):
        self.with_session_env()
        rec = Recorder(ok_responder)
        b = self.backend()
        with mock.patch("urllib.request.urlopen", rec):
            b.write_events("/proj", "s1", 0, None, self.groups())
        for url in rec.urls():
            self.assertNotIn(KEY_VALUE, url)
            self.assertNotIn(ACCESS_TOKEN, url)

    def test_source_has_no_unverified_tls_or_secret_key(self):
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "scripts" / "supabase_backend.py").read_text()
        for forbidden in ("_create_unverified_context", "CERT_NONE",
                          "check_hostname = False", "check_hostname=False",
                          "verify=False", "service_role", "sb_secret",
                          "secret_key"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_source_is_stdlib_only(self):
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "scripts" / "supabase_backend.py").read_text()
        for pkg in ("import requests", "import psycopg", "import supabase",
                    "from supabase", "httpx", "aiohttp"):
            self.assertNotIn(pkg, src, pkg)


class TestFactoryGate(Base):
    def test_none_when_backend_is_local(self):
        settings.ensure_identity("Ada")  # active_backend defaults to local
        self.assertIsNone(supabase_backend.active_backend())
        self.assertIsNone(storage.remote_backend_if_active())

    def test_none_when_supabase_config_missing(self):
        settings.write_settings({"active_backend": "supabase"})
        self.assertIsNone(supabase_backend.active_backend())

    def test_backend_when_active_and_configured(self):
        settings.ensure_identity("Ada")
        settings.set_supabase_config(URL, KEY_ENV)
        settings.set_active_backend("supabase")
        b = supabase_backend.active_backend()
        self.assertIsInstance(b, supabase_backend.SupabaseBackend)
        self.assertIsInstance(storage.remote_backend_if_active(),
                              supabase_backend.SupabaseBackend)


class TestCaptureNeverBrokenByRemote(unittest.TestCase):
    """The guarded capture write-through: with active_backend=supabase and an
    unreachable remote, capture still completes, writes locally, exits 0, and the
    event is retained in the outbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.proj = self.root / "proj"
        (self.proj / ".claude").mkdir(parents=True)
        (self.proj / ".claude" / "telemetry").touch()  # central mode marker
        self.transcript = self.root / "sess.jsonl"
        self.telem = self.root / "telemetry"
        self._prev_db = os.environ.get("TOKEN_TELEMETRY_DB")
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.telem / "usage.db")
        self._prev_key = os.environ.get(KEY_ENV)
        os.environ[KEY_ENV] = KEY_VALUE
        self._prev_sess = os.environ.get(supabase_backend.SESSION_ENV)
        os.environ[supabase_backend.SESSION_ENV] = ACCESS_TOKEN
        self._stdin = sys.stdin
        # settings: opt into the remote backend
        settings.ensure_identity("Ada")
        settings.set_supabase_config(URL, KEY_ENV)
        settings.set_active_backend("supabase")

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

    def run_main(self):
        sys.stdin = io.StringIO(json.dumps({
            "session_id": "sess-1", "transcript_path": str(self.transcript),
            "cwd": str(self.proj), "hook_event_name": "Stop"}))
        capture.main()  # the __main__ block calls sys.exit(0) unconditionally

    def test_capture_completes_locally_and_queues_remote_on_failure(self):
        write_jsonl(self.transcript, [entry()])
        with mock.patch("urllib.request.urlopen", Recorder(boom_responder)):
            self.run_main()  # must not raise
        # local write is authoritative and unaffected
        conn = sqlite3.connect(self.telem / "usage.db")
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        finally:
            conn.close()
        # the remote firing is retained in the local outbox for re-send
        outbox = self.telem / "outbox.db"
        self.assertTrue(outbox.exists())
        oc = sqlite3.connect(outbox)
        try:
            self.assertEqual(
                oc.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        finally:
            oc.close()

    def test_local_default_makes_no_remote_call(self):
        settings.set_active_backend("local")  # flip back to default
        write_jsonl(self.transcript, [entry()])
        rec = Recorder(ok_responder)
        with mock.patch("urllib.request.urlopen", rec):
            self.run_main()
        self.assertEqual(rec.calls, [])  # local path never touches the network


if __name__ == "__main__":
    unittest.main()
