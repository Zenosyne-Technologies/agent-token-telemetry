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

The remote schema/RLS is P7. Read parity is P9 (this file's ``read_for_report``):
the reports are aggregated SERVER-SIDE by hand-written, ``SECURITY INVOKER``
Postgres functions in ``supabase/reports.sql`` (so RLS applies and a caller sees
only their OWN rows), invoked over the same TLS-verified PostgREST transport as
the writes (``POST /rest/v1/rpc/<fn>``). ``server_side_aggregation`` is therefore
``True``; ``open_ro()`` stays ``None`` because there is no LOCAL read-only SQL
connection — the aggregation runs remotely, not over a handed-back cursor.
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
# Rows per bulk-migration upsert request (P8). Chunked so one HTTP body stays
# bounded and a resumed migration re-sends at most one chunk; every chunk is an
# idempotent merge-duplicates upsert, so re-running converges with no duplicates.
MIGRATE_CHUNK = 500
# Env var that may inject an Auth access token directly (highest precedence, for
# CI/testing) ahead of the keychain and the 0600 file. Holds a token, not a key.
SESSION_ENV = "TOKEN_TELEMETRY_SUPABASE_SESSION"

# The `events` upsert conflict target — the full row identity, matching the
# `UNIQUE NULLS NOT DISTINCT (...)` constraint in supabase/schema.sql exactly.
# Every column named here is present on the event rows _push builds, so a
# re-drained outbox firing MERGES rather than duplicating (the P6 finding). It is
# the union of the mirror-dedupe tuple (which includes the model reference) and
# the v6 per-slice metrics, prefixed by owner_id — the superset that can never
# false-merge two genuinely-distinct events.
EVENTS_ON_CONFLICT = (
    "owner_id,session_uuid,model_name,ts,kind,agent,"
    "in_tok,out_tok,cache_r,cache_w,cache_w_1h,dur_ms,"
    "branch,commit_sha,issue_key,task_size,note,api_calls,ctx_tokens")

# Per-table upsert conflict targets — each MUST match a UNIQUE/PRIMARY KEY
# constraint on the corresponding table in supabase/schema.sql. Every target is
# owner-scoped so a re-send is idempotent per owner (own-rows-only). tests/
# test_schema_sql.py verifies the schema carries a matching constraint for each.
UPSERT_ON_CONFLICT = {
    "users": "uuid",
    "projects": "owner_id,path",
    "models": "owner_id,name",
    "pricing": "owner_id,provider,model_prefix,model_version,effective_from",
    "sessions": "owner_id,uuid",
    "events": EVENTS_ON_CONFLICT,
}


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
        # Self-heal latch (req 7): the current user's `users` row is upserted at
        # most once per process before its first FK-referencing push, so a user
        # who enabled remote without a full migration still has a valid parent
        # row and events do not loop in the outbox on the users FK. Reset to
        # False so a failed attempt retries on the next firing.
        self._user_synced = False

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

    # --- read path (P9): server-side aggregation via the reports.sql RPC ---
    def read_for_report(self, report, project_path=None):
        """Return one report's aggregated data by calling its ``SECURITY INVOKER``
        Postgres function over PostgREST RPC, mapped into the EXACT Python shape
        ``report.py``'s matching ``fetch_*`` returns — so the shared ``render_*``
        renders remote and local identically (the cross-dialect golden test
        guards that the two SQL dialects agree on this shape).

        The function is ``SECURITY INVOKER`` and runs under the caller's Auth JWT,
        so RLS restricts every row to the caller's OWN data (own-rows-only). Raises
        on any transport/HTTP/auth error (unlike the never-break-a-session write
        path): a report command is not the capture hook, and ``report.py`` renders
        a friendly degradation message rather than a stack trace."""
        if report == "project-stats":
            return _map_project_stats(self._rpc("report_project_stats"))
        if report == "token-stats":
            return _map_token_stats(self._rpc("report_token_stats"))
        if report == "info":
            return _map_info(self._rpc("report_info",
                                       {"p_project_path": project_path}))
        raise ValueError(f"unknown report {report!r}")

    def _rpc(self, fn, params=None):
        """Invoke a ``reports.sql`` function via ``POST /rest/v1/rpc/<fn>`` and
        return its parsed JSON result. Auth is header-based (publishable key +
        the per-user Auth Bearer), so the JWT — and thus ``auth.uid()`` for the
        RLS the function honors — is the caller's; no secret ever enters the URL.
        Reuses the single TLS-verified :meth:`_request` site."""
        token = self._access_token()
        headers = {"apikey": self._publishable_key(),
                   "Authorization": f"Bearer {token}",
                   "Content-Type": "application/json",
                   "Accept": "application/json"}
        url = f"{self.base}/rest/v1/rpc/{fn}"
        _status, raw, _hdrs = self._request("POST", url, headers, params or {})
        return json.loads(raw) if raw else None

    def close(self):
        """Close the local outbox connection if one is open."""
        if self._outbox is not None:
            try:
                self._outbox.close()
            finally:
                self._outbox = None

    def capabilities(self):
        """Honest flags: RLS gives per-user isolation, PostgREST upserts, and —
        as of P9 — server-side report aggregation via the ``reports.sql`` RPC
        (``server_side_aggregation=True``). Cursors stay local and authoritative
        (never remote), so this backend owns none."""
        return storage.Caps(
            server_side_aggregation=True, owns_cursors=False, multi_user=True,
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
        _status, raw, _hdrs = self._request(
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
        resolves the foreign references. ``owner_id`` is carried on EVERY table so
        the RLS `auth.uid() = owner_id` gate applies uniformly (own-rows-only),
        and every upsert's ``on_conflict`` is the OWNER-SCOPED unique key from
        supabase/schema.sql (:data:`UPSERT_ON_CONFLICT`), so a re-drained outbox
        firing merges instead of duplicating — including ``events``, keyed on the
        full-row-identity constraint (:data:`EVENTS_ON_CONFLICT`)."""
        token = self._access_token()
        owner = payload["owner_id"]
        project = payload["project"]
        headers = self._rest_headers(token)
        # 0. self-heal (req 7): ensure the owner's `users` row exists before any
        #    FK-referencing row, once per process. A fresh-enabled user (no full
        #    migration) otherwise has no parent row and every event loops in the
        #    outbox on the users FK.
        self._ensure_user_row(headers, owner)
        # 1. projects (upsert on the owner-scoped natural key `(owner_id, path)`)
        self._rest("projects", [{"owner_id": owner, "path": project}], headers,
                   on_conflict=UPSERT_ON_CONFLICT["projects"])
        # 2. models (upsert on `(owner_id, name)`), one row per distinct model
        models = sorted({r["model"] for r in payload["rows"]})
        self._rest("models", [{"owner_id": owner, "name": m} for m in models],
                   headers, on_conflict=UPSERT_ON_CONFLICT["models"])
        # 3. sessions (upsert on `(owner_id, uuid)`, carrying project reference)
        self._rest("sessions", [{"uuid": payload["session_uuid"],
                                 "project_path": project, "owner_id": owner}],
                   headers, on_conflict=UPSERT_ON_CONFLICT["sessions"])
        # 4. events (upsert on the full-row-identity key so a re-sent firing
        #    merges rather than duplicating — NULLS NOT DISTINCT on the remote
        #    side collapses rows whose nullable columns are NULL).
        event_rows = []
        for r in payload["rows"]:
            row = dict(r["fields"])
            row["session_uuid"] = payload["session_uuid"]
            row["model_name"] = r["model"]
            row["owner_id"] = owner
            event_rows.append(row)
        self._rest("events", event_rows, headers,
                   on_conflict=UPSERT_ON_CONFLICT["events"])

    def _ensure_user_row(self, headers, owner):
        """Self-heal the owner's remote ``users`` row before FK-referencing rows.

        Idempotent and at most once per process (:attr:`_user_synced`): a user
        who enabled the remote backend without running the full migration still
        gets a valid parent row, so events do not loop in the outbox on the
        ``users`` foreign key (the P7 finding). The row is inserted with
        ``resolution=ignore-duplicates`` so an existing row — and its real
        ``created_at`` written by the migration — is never clobbered by this
        best-effort now-stamp. Name/uuid come from settings; when no full name is
        on file the row cannot satisfy ``users.name NOT NULL``, so this no-ops
        (the latch stays False and a later firing retries once a name is set).

        :param headers: the REST headers already built for this push (its Auth
            bearer is reused; only the ``Prefer`` resolution is overridden).
        :param owner: the ``owner_id`` stamped on this firing — the users row's
            ``uuid`` (== ``auth.uid()`` after login, what RLS matches).
        """
        if self._user_synced:
            return
        s = self._settings if self._settings is not None else settings.read_settings()
        user = s.get("user") if isinstance(s, dict) else None
        name = user.get("full_name") if isinstance(user, dict) else None
        if not name:
            return
        uheaders = dict(headers)
        uheaders["Prefer"] = "resolution=ignore-duplicates,return=minimal"
        self._rest("users", [{"uuid": owner, "name": name,
                              "created_at": int(time.time())}], uheaders,
                   on_conflict=UPSERT_ON_CONFLICT["users"])
        self._user_synced = True

    # --- bulk migration (P8): FK-ordered chunked upserts + count validation.
    #     These DELIBERATELY raise on any transport/HTTP error (unlike the
    #     never-break-a-session write path) so the migration command aborts and
    #     leaves the collection pointer unflipped and the local DB authoritative.
    def push_rows(self, table, rows, on_conflict, chunk=None):
        """Upsert ``rows`` into remote ``table`` in idempotent chunks.

        Every chunk is a ``Prefer: resolution=merge-duplicates`` upsert on
        ``on_conflict`` (an owner-scoped unique key), so a resumed or re-run
        migration merges instead of duplicating. All I/O goes through the one
        TLS-verified :meth:`_request` site. Raises on the FIRST failing chunk so
        the caller can abort without flipping the pointer.

        :param table: the remote table name.
        :param rows: a list of JSON-serializable row dicts (already owner-stamped
            by the caller for every owner-scoped table).
        :param on_conflict: the upsert conflict target (an owner-scoped unique
            key from :data:`UPSERT_ON_CONFLICT`).
        :param chunk: rows per request (defaults to :data:`MIGRATE_CHUNK`).
        :returns: the number of rows sent.
        :raises urllib.error.URLError / HTTPError / TimeoutError: on any failure.
        """
        if not rows:
            return 0
        size = chunk or MIGRATE_CHUNK
        sent = 0
        for i in range(0, len(rows), size):
            batch = rows[i:i + size]
            # Re-derive the bearer per chunk so a long upload refreshes a token
            # nearing expiry rather than failing mid-run.
            headers = self._rest_headers(self._access_token())
            self._rest(table, batch, headers, on_conflict=on_conflict)
            sent += len(batch)
        return sent

    def count_rows(self, table):
        """Return the number of remote rows in ``table`` visible to the caller.

        Under RLS every authenticated request sees only the caller's OWN rows, so
        this is the owner-scoped count the migration validates against the local
        totals before the pointer flip. Uses ``Prefer: count=exact`` and reads the
        ``Content-Range`` response header (``0-0/<total>``); no table data is
        transferred beyond a single probe row. Raises on any transport/HTTP error
        (e.g. a missing table on an unprovisioned remote) so preflight and
        validation can surface it.

        :param table: the remote table to count.
        :returns: the owner-scoped row count as an ``int``.
        :raises urllib.error.URLError / HTTPError / TimeoutError: on any failure.
        """
        headers = dict(self._rest_headers(self._access_token()))
        headers["Prefer"] = "count=exact"
        headers["Range-Unit"] = "items"
        headers["Range"] = "0-0"
        url = f"{self.base}/rest/v1/{table}"
        _status, _body, resp_headers = self._request("GET", url, headers)
        cr = resp_headers.get("content-range") or ""
        total = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        return int(total) if total.isdigit() else 0

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

    def _request(self, method, url, headers, body=None):
        """The ONE place a network request is made — so TLS, timeout, and the
        no-secret-in-URL rule are enforced in exactly one auditable spot.

        Uses a certificate-verified default TLS context (NEVER an unverified
        one) and a bounded timeout. Only ``https://`` is permitted. Raises
        :class:`urllib.error.URLError` / :class:`~urllib.error.HTTPError` /
        :class:`TimeoutError` on failure — the caller decides what to swallow.

        :returns: ``(status, body_bytes, response_headers)`` where the headers
            are a lower-cased-key dict (used by :meth:`count_rows` to read the
            ``Content-Range`` total); an empty dict when they cannot be read."""
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
            body_bytes = resp.read()
            try:
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            except Exception:
                resp_headers = {}
            return status, body_bytes, resp_headers

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
        _status, raw, _hdrs = self._request(
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


# The remote schema mirrors the SQLite v7 shape (supabase/schema.sql). The remote
# has no PRAGMA user_version, so `/info`'s schema line reflects that modeled shape
# — a store property, NOT a cross-dialect aggregation, so the golden test compares
# every other central field but excludes this one.
REMOTE_SCHEMA_SHAPE = 7


def _i(v):
    """Coerce an RPC JSON number to ``int`` (JSON may hand back a float for an
    integral value), preserving ``None``. Keeps the mapped shape's types equal to
    what SQLite's ``fetch_*`` returns so ``render_*`` output cannot drift."""
    return None if v is None else int(v)


def _f(v):
    """Coerce an RPC JSON number to ``float`` (SQLite returns floats for cost /
    percentage columns), preserving ``None``."""
    return None if v is None else float(v)


def _map_project_stats(data):
    """Map ``report_project_stats``'s JSON array into the list-of-dicts
    :func:`report.fetch_project_stats` returns (keyed by ``report.STATS_KEYS``).
    The RPC returns raw ``path``/``name``; the basename fallback stays in the
    renderer, exactly as the local path leaves it. ``estimated_events`` is
    ``None`` (not reported) while the RPC does not return it — the key keeps
    the shape identical to the local fetch."""
    out = []
    for r in (data or []):
        out.append({
            "path": r.get("path"), "name": r.get("name"),
            "sessions": _i(r.get("sessions")), "events": _i(r.get("events")),
            "input": _i(r.get("input")), "output": _i(r.get("output")),
            "cache_read": _i(r.get("cache_read")),
            "cache_write": _i(r.get("cache_write")),
            "classic_in": _f(r.get("classic_in")),
            "classic_out": _f(r.get("classic_out")),
            "cached_r": _f(r.get("cached_r")), "cached_w": _f(r.get("cached_w")),
            "rate_from": _i(r.get("rate_from")),
            "unpriced_events": _i(r.get("unpriced_events")),
            "first_seen": r.get("first_seen"),
            "last_activity": r.get("last_activity"),
            "estimated_events": _i(r.get("estimated_events"))})
    return out


def _map_token_stats(data):
    """Map ``report_token_stats``'s JSON object into the dict
    :func:`report.fetch_token_stats` returns. Tuples/positions and column types
    match the SQLite path so the shared renderer is source-agnostic. The
    ``by_project`` basename fallback is applied here, exactly as the SQLite fetch
    does in Python (``nm or Path(path).name``). ``estimated_by_model`` and
    ``models_without_own_price`` are ``None`` (not reported) while the RPC does
    not return them — the keys keep the shape identical to the local fetch."""
    data = data or {}

    def toks(v):
        v = v or [0, 0, 0, 0, 0]
        return (_i(v[0]), _i(v[1]), _i(v[2]), _i(v[3]), _i(v[4]))

    by_project = []
    for path, nm, *rest in (data.get("by_project") or []):
        label = nm or Path(path).name
        by_project.append((label, _i(rest[0]), _i(rest[1]), _i(rest[2]),
                           _i(rest[3]), _i(rest[4])))
    return {
        "today": toks(data.get("today")),
        "week": toks(data.get("week")),
        "backlog_excluded": _i(data.get("backlog_excluded")) or 0,
        "by_project": by_project,
        "by_agent": [(a, _i(i), _i(o), _i(n))
                     for a, i, o, n in (data.get("by_agent") or [])],
        "by_model": [(nm, tier, _i(i), _i(o), _f(cost), _i(rf))
                     for nm, tier, i, o, cost, rf
                     in (data.get("by_model") or [])],
        "by_kind": [(k, _i(i), _i(o), _f(pct))
                    for k, i, o, pct in (data.get("by_kind") or [])],
        "by_tier": [(t, _i(i), _i(o), _i(n))
                    for t, i, o, n in (data.get("by_tier") or [])],
        "by_issue": [(k, _i(i), _i(o), _i(cr), _i(cw), _i(n))
                     for k, i, o, cr, cw, n in (data.get("by_issue") or [])],
        "estimated_by_model": (
            None if data.get("estimated_by_model") is None
            else {k: _i(v) for k, v in data["estimated_by_model"].items()}),
        "models_without_own_price": data.get("models_without_own_price"),
    }


def _map_info(data):
    """Map ``report_info``'s JSON object into the dict
    :func:`report.fetch_info_central` returns (``schema`` set from the modeled
    remote shape — the remote has no ``user_version``)."""
    data = data or {}
    return {
        "schema": REMOTE_SCHEMA_SHAPE,
        "events": _i(data.get("events")) or 0,
        "first_day": data.get("first_day"), "last_day": data.get("last_day"),
        "projects": _i(data.get("projects")) or 0,
        "pricing_rows": _i(data.get("pricing_rows")) or 0,
        "latest_rate_from": _i(data.get("latest_rate_from")),
        "events_here": _i(data.get("events_here")) or 0,
    }


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
