#!/usr/bin/env python3
"""Supabase remote storage backend — the write path (AOS-104 P6). SECURITY-GATED.

Stdlib only. A :class:`~storage.StorageBackend` sibling to
:class:`~storage.LocalSqliteBackend` that writes one firing's events to a
Supabase project over its PostgREST REST API, authenticated per-user with a
Supabase Auth (GoTrue) JWT and Row-Level Security keyed on ``auth.uid()`` (the
RLS policies themselves are P7). It obeys the same iron rule as capture: **never
break a session** — every remote error is swallowed, the event is retained in a
local outbox, and control returns normally.

Security model (design memo §1/§6, architect decisions §9):
  - **Transport:** PostgREST at ``<url>/rest/v1/`` and GoTrue at ``<url>/auth/v1/``
    over ``urllib.request`` with a **certificate-verified default TLS context**
    (:func:`ssl.create_default_context`). Verification is NEVER disabled and only
    ``https://`` is used. Auth is header-based (``apikey`` + ``Authorization:
    Bearer``); **no secret ever enters a URL, a query string, a log, or a commit.**
  - **Keys:** the low-privilege **publishable** key is read from the env var whose
    NAME lives in settings (never the value). The sensitive **Auth session/refresh
    token** lives in a mode-0600 ``credentials.json`` under the telemetry dir (env
    → keychain → file precedence). The **secret / bypass key (the service-role
    tier) is never read, stored, or supported** anywhere.
  - **Identity:** own-rows-only. Remote rows carry ``owner_id`` = the reconciled
    Supabase ``auth.uid()`` (hybrid identity — memo §5 decision 6).

Reads and the remote schema/RLS are later phases (P7/P9): this backend is
write-only and honest about it (``server_side_aggregation=False``,
``open_ro()`` → ``None``).
"""
import json
import os
import ssl
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

import capture
import settings
import storage

# One bounded per-request timeout so a hung remote can never stall the hook. The
# push aborts to the outbox on the FIRST failing request, so the worst case a
# session pays for an unreachable remote is a single timeout, not one per table.
REMOTE_TIMEOUT = 5.0
# Refresh the Auth JWT when it is within this many seconds of expiry.
REFRESH_SKEW = 60
# At most this many backlogged firings are re-sent per hook firing, bounding the
# catch-up cost after the remote comes back (mirrors capture.SUBAGENT_BATCH).
OUTBOX_DRAIN_BATCH = 50
# Env var that may inject an Auth access token directly (highest precedence, for
# CI/testing) ahead of the keychain and the 0600 file. Holds a token, not a key.
SESSION_ENV = "TOKEN_TELEMETRY_SUPABASE_SESSION"


def credentials_path():
    """Absolute path to the mode-0600 ``credentials.json`` (Auth tokens) beside
    the usage DB — a sibling of ``settings.json``, never inside a repo."""
    return settings.telemetry_dir() / "credentials.json"


def read_credentials():
    """Parse ``credentials.json`` and return the stored Auth session dict.

    :returns: the session dict, or ``{}`` on ANY problem (absent, unreadable,
        corrupt, non-object). Never raises: read on the hook path.
    """
    try:
        with open(credentials_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_credentials(session):
    """Persist the Auth ``session`` to ``credentials.json`` atomically, mode 0600.

    Same discipline as :func:`settings.write_settings`: a per-process temp file
    opened 0600 from creation, swapped in with :func:`os.replace`, and the final
    mode forced to 0600. This file holds the ONLY on-disk secret (the access /
    refresh token); it is never logged and never committed.

    :param session: the ``{"access_token", "refresh_token", "expires_at",
        "uid"}`` dict to persist.
    :returns: the :class:`~pathlib.Path` written.
    :raises OSError: only on a genuine filesystem failure.
    """
    d = settings.telemetry_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = credentials_path()
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(session, f, indent=2)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


class SupabaseBackend(storage.StorageBackend):
    """A write-only remote backend over Supabase PostgREST + Auth.

    Constructed from a validated ``supabase`` config block
    (:func:`settings.supabase_config`). Holds no secrets in memory beyond the
    lifetime of a single request; the publishable key is read from env by name
    and the Auth token from the 0600 credentials file each time it is needed.
    """

    def __init__(self, config, settings_snapshot=None, outbox_path=None):
        """:param config: ``{"url", "publishable_key_env"}`` from settings.
        :param settings_snapshot: an already-read settings dict (optional) so the
            hot path need not re-read ``settings.json``.
        :param outbox_path: override the local outbox DB location (tests)."""
        self.base = str(config["url"]).rstrip("/")
        self.key_env = (config.get("publishable_key_env")
                        or settings.DEFAULT_SUPABASE_KEY_ENV)
        self._settings = settings_snapshot
        self._outbox_path = (Path(outbox_path) if outbox_path is not None
                             else settings.telemetry_dir() / "outbox.db")
        self._outbox = None

    # --- lifecycle ---
    def open(self):
        """Open the local outbox store; validate no network write here. Tolerant
        by design — never raises on the hot path, so capture's guarded remote
        hook can always proceed to :meth:`write_events`. Returns ``self``."""
        try:
            self._outbox = self._open_outbox()
        except Exception:
            self._outbox = None
        return self

    def open_ro(self):
        """Remote reads are a later phase (P9): there is no local read-only SQL
        connection to hand back, so this is honestly ``None``."""
        return None

    def close(self):
        """Close the local outbox connection if one is open."""
        if self._outbox is not None:
            try:
                self._outbox.close()
            finally:
                self._outbox = None

    def capabilities(self):
        """Honest flags: RLS gives per-user isolation and PostgREST upserts, but
        this phase is write-only (reads/aggregation are P9) and cursors stay
        local and authoritative (never remote)."""
        return storage.Caps(
            server_side_aggregation=False, owns_cursors=False, multi_user=True,
            supports_upsert=True, writable=True)

    # --- schema / compatibility ---
    def schema_version(self):
        """The remote schema is provisioned and versioned out-of-band (P7); this
        backend does not manage or read it, so the version is unknown here."""
        return None

    def ensure_schema(self):
        """No-op: the remote schema + RLS policies are created in P7 (reviewed
        SQL), never by a laptop-side capture."""
        return None

    # --- cursors: NEVER remote (memo §4c / decision 7) ---
    def cursor_get(self, transcript):
        """Cursors are authoritative and LOCAL even when events go remote, so a
        remote backend owns none. Capture keeps reading/writing cursors through
        the local backend; this raises if ever called by mistake."""
        raise NotImplementedError(
            "cursors stay in the local store; SupabaseBackend owns none")

    def cursor_set(self, transcript, offset, session_id):
        raise NotImplementedError(
            "cursors stay in the local store; SupabaseBackend owns none")

    # --- identity ---
    def remote_owner_id(self, local_owner_id=None):
        """The owner id stamped on remote rows: the reconciled Supabase
        ``auth.uid()`` when known (what RLS matches), else the local uuid passed
        in, else the central identity's uuid. Hybrid identity (memo §5)."""
        s = self._settings if self._settings is not None else settings.read_settings()
        user = s.get("user") if isinstance(s, dict) else None
        if isinstance(user, dict):
            auth_uid = user.get("auth_uid")
            if isinstance(auth_uid, str) and auth_uid:
                return auth_uid
        if local_owner_id:
            return local_owner_id
        return settings.current_owner_id(s)

    def login(self, email, password):
        """Acquire an Auth session with the password grant and persist it.

        **Grant choice (flagged design decision):** the OAuth2 *password grant*
        (``POST /auth/v1/token?grant_type=password``) is used because this is a
        first-party CLI with no browser to run a redirect flow through; the
        email/password travel in the request BODY over verified TLS, never in the
        URL, and are never stored — only the returned tokens are. This method is
        the token-acquisition entry the interactive login command (P11) calls; it
        never prompts. On success it reconciles the local uuid with the Auth uid.

        :param email: the user's Supabase Auth email.
        :param password: the user's Supabase Auth password.
        :returns: ``{"uid", "expires_at"}`` — NEVER the tokens themselves.
        :raises RuntimeError: if the publishable key env var is unset.
        :raises urllib.error.URLError / HTTPError: on a transport/auth failure.
        """
        url = f"{self.base}/auth/v1/token?grant_type=password"
        _status, raw = self._request(
            "POST", url, self._auth_headers(), {"email": email,
                                                "password": password})
        data = json.loads(raw)
        session = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_at": (data.get("expires_at")
                           or int(time.time() + (data.get("expires_in") or 3600))),
            "uid": (data.get("user") or {}).get("id"),
        }
        write_credentials(session)
        settings.record_auth_identity(session["uid"])
        # Refresh the in-memory snapshot so a subsequent write in the same
        # process picks up the reconciled auth_uid without re-reading the file.
        self._settings = settings.read_settings()
        return {"uid": session["uid"], "expires_at": session["expires_at"]}

    # --- write path ---
    def write_events(self, project, session_uuid, kind_hint, agent, groups,
                     branch=None, commit_sha=None, issue_key=None,
                     task_size=None, note=None, first_capture=False,
                     owner_id=None):
        """Push one firing's rows to Supabase; NEVER raise, NEVER block a session.

        First re-sends any backlog (offline outbox), then upserts this firing's
        projects/models/sessions (FK resolution) and inserts its events over
        PostgREST. On ANY remote/transport error the firing is retained in the
        local outbox and re-sent on a later call — same swallow-and-log discipline
        as the mirror write. Returns the session uuid (cursors are local, so the
        caller does not use this to key a remote cursor)."""
        payload = self._build_payload(
            project, session_uuid, kind_hint, agent, groups, branch, commit_sha,
            issue_key, task_size, note, first_capture, owner_id)
        try:
            self._drain_outbox()
        except Exception:
            pass  # backlog re-send is best-effort; the current firing still tries
        if payload["rows"]:
            try:
                self._push(payload)
            except Exception:
                self._enqueue(payload)
                capture.log_error("supabase write deferred to outbox")
        return session_uuid

    def _build_payload(self, project, session_uuid, kind_hint, agent, groups,
                       branch, commit_sha, issue_key, task_size, note,
                       first_capture, owner_id):
        """Build the JSON-serializable logical write for one firing — safe to
        queue in the outbox and replay verbatim. Event values come from the
        shared :func:`capture.derive_event_fields`, so local and remote rows
        cannot drift."""
        derived = capture.derive_event_fields(
            kind_hint, agent, groups, branch, commit_sha, issue_key, task_size,
            note, first_capture)
        return {
            "project": project,
            "session_uuid": session_uuid,
            "owner_id": self.remote_owner_id(owner_id),
            "rows": [{"model": model, "fields": fields}
                     for model, _kind, fields in derived],
        }

    def _push(self, payload):
        """Perform the full FK-ordered upsert for one firing. Raises on any
        transport/HTTP error so :meth:`write_events` can route it to the outbox.

        Remote rows are addressed by NATURAL KEY (path / name / uuid) rather than
        local integer ids, which are meaningless across databases; the P7 schema
        resolves the foreign references. ``owner_id`` is carried on both
        ``sessions`` and ``events`` so RLS on ``auth.uid()`` can gate either."""
        token = self._access_token()
        owner = payload["owner_id"]
        project = payload["project"]
        headers = self._rest_headers(token)
        # 1. projects (upsert on natural key `path`)
        self._rest("projects", [{"path": project}], headers, on_conflict="path")
        # 2. models (upsert on `name`), one row per distinct model this firing
        models = sorted({r["model"] for r in payload["rows"]})
        self._rest("models", [{"name": m} for m in models], headers,
                   on_conflict="name")
        # 3. sessions (upsert on `uuid`, carrying owner_id + project reference)
        self._rest("sessions", [{"uuid": payload["session_uuid"],
                                 "project_path": project, "owner_id": owner}],
                   headers, on_conflict="uuid")
        # 4. events (insert; no natural key exists, so a re-send relies on the
        #    outbox draining exactly once — a landed firing is dropped from it)
        event_rows = []
        for r in payload["rows"]:
            row = dict(r["fields"])
            row["session_uuid"] = payload["session_uuid"]
            row["model_name"] = r["model"]
            row["owner_id"] = owner
            event_rows.append(row)
        self._rest("events", event_rows, headers)

    # --- transport (single urlopen site) ---
    def _auth_headers(self):
        """Headers for a GoTrue call: the publishable key as ``apikey`` plus a
        JSON content type. No Authorization bearer yet (this IS how we get one)."""
        return {"apikey": self._publishable_key(),
                "Content-Type": "application/json"}

    def _rest_headers(self, token):
        """Headers for a PostgREST write: publishable key + the per-user Auth
        Bearer token + upsert preference. Secrets live ONLY in headers."""
        return {"apikey": self._publishable_key(),
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal"}

    def _publishable_key(self):
        """The low-privilege publishable key, read from the env var NAMED in
        settings. Never persisted, never logged. A secret / bypass (service-role
        tier) key is never accepted here."""
        key = os.environ.get(self.key_env)
        if not key:
            raise RuntimeError(
                f"publishable key env var {self.key_env} is not set")
        return key

    def _rest(self, table, rows, headers, on_conflict=None):
        """POST ``rows`` to ``/rest/v1/<table>``. ``on_conflict`` selects the
        upsert key (a non-secret query param). No-ops on an empty ``rows``."""
        if not rows:
            return
        url = f"{self.base}/rest/v1/{table}"
        if on_conflict:
            url += f"?on_conflict={on_conflict}"
        self._request("POST", url, headers, rows)

    def _request(self, method, url, headers, body):
        """The ONE place a network request is made — so TLS, timeout, and the
        no-secret-in-URL rule are enforced in exactly one auditable spot.

        Uses a certificate-verified default TLS context (NEVER an unverified
        one) and a bounded timeout. Only ``https://`` is permitted. Raises
        :class:`urllib.error.URLError` / :class:`~urllib.error.HTTPError` /
        :class:`TimeoutError` on failure — the caller decides what to swallow."""
        if not url.lower().startswith("https://"):
            raise RuntimeError("refusing a non-HTTPS Supabase request")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for name, value in headers.items():
            req.add_header(name, value)
        # Default context verifies the certificate chain and the hostname;
        # verification is never turned off (no unverified context, no disabled
        # certificate mode) — that is enforced by a static test in the suite.
        context = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT,
                                    context=context) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return status, resp.read()

    # --- Auth session (GoTrue) ---
    def _load_session(self):
        """Resolve the Auth session by precedence: env → keychain → 0600 file.
        Returns the session dict, or ``{}`` when none is available."""
        token = os.environ.get(SESSION_ENV)
        if token:
            return {"access_token": token, "refresh_token": None,
                    "expires_at": None}
        kc = self._keychain_session()
        if kc:
            return kc
        return read_credentials()

    def _keychain_session(self):
        """OS-keychain session lookup. A documented extension point: no keychain
        is trivially portable across platforms, so the default is ``None`` (fall
        through to the 0600 file). A platform integration slots in here without
        touching the precedence contract."""
        return None

    def _access_token(self):
        """A currently-valid Auth access token, refreshing when near expiry.

        :raises RuntimeError: when no session is available (no login yet) — the
            caller routes the firing to the outbox until the user logs in.
        """
        session = self._load_session()
        token = session.get("access_token")
        if not token:
            raise RuntimeError("no Supabase Auth session (login required)")
        expires_at = session.get("expires_at")
        refresh = session.get("refresh_token")
        if (expires_at is not None and refresh
                and time.time() > expires_at - REFRESH_SKEW):
            session = self._refresh(refresh)
            token = session["access_token"]
        return token

    def _refresh(self, refresh_token):
        """Exchange a refresh token for a fresh Auth session and persist it.
        The refresh token travels in the BODY, never the URL."""
        url = f"{self.base}/auth/v1/token?grant_type=refresh_token"
        _status, raw = self._request(
            "POST", url, self._auth_headers(), {"refresh_token": refresh_token})
        data = json.loads(raw)
        session = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh_token,
            "expires_at": (data.get("expires_at")
                           or int(time.time() + (data.get("expires_in") or 3600))),
            "uid": (data.get("user") or {}).get("id"),
        }
        write_credentials(session)
        return session

    # --- offline outbox (local, durable) ---
    def _open_outbox(self):
        """Open (creating if needed) the local outbox DB. The outbox is a local
        queue of firings that failed to reach the remote — cursors are NEVER put
        here; only replayable event payloads are."""
        self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._outbox_path, timeout=5)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS outbox("
            " id INTEGER PRIMARY KEY, enqueued_at INTEGER NOT NULL,"
            " payload TEXT NOT NULL)")
        conn.commit()
        return conn

    def _outbox_conn(self):
        if self._outbox is None:
            self._outbox = self._open_outbox()
        return self._outbox

    def _enqueue(self, payload):
        """Retain a firing that failed to reach the remote, for a later re-send."""
        conn = self._outbox_conn()
        with conn:
            conn.execute(
                "INSERT INTO outbox(enqueued_at, payload) VALUES (?,?)",
                (int(time.time()), json.dumps(payload)))

    def _drain_outbox(self):
        """Re-send backlogged firings oldest-first, deleting each as it lands.
        Stops at the FIRST failure (the remote is still down) so a persistent
        outage costs one request, not one per queued firing. Bounded per call."""
        conn = self._outbox_conn()
        rows = conn.execute(
            "SELECT id, payload FROM outbox ORDER BY id LIMIT ?",
            (OUTBOX_DRAIN_BATCH,)).fetchall()
        for row_id, raw in rows:
            try:
                self._push(json.loads(raw))
            except Exception:
                break  # remote still unreachable; leave this and the rest queued
            with conn:
                conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))

    def outbox_count(self):
        """How many firings are currently queued for re-send (0 when drained)."""
        conn = self._outbox_conn()
        return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


def active_backend(settings_dict=None):
    """Return a :class:`SupabaseBackend` iff the active backend is ``supabase``
    and a usable config is present, else ``None`` — the guard capture's remote
    hook branches on. Total and never-raising: a malformed/absent settings file
    yields ``None`` and the default local path is untouched.

    :param settings_dict: an already-read settings dict (optional), to avoid a
        second file read on the hot path.
    """
    try:
        s = (settings_dict if settings_dict is not None
             else settings.read_settings())
        if not isinstance(s, dict) or s.get("active_backend") != "supabase":
            return None
        cfg = settings.supabase_config(s)
        if cfg is None:
            return None
        return SupabaseBackend(cfg, settings_snapshot=s)
    except Exception:
        return None
