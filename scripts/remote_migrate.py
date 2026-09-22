#!/usr/bin/env python3
"""Command B worker — migrate the central file DB to the remote host (AOS-104 P8).

SECURITY-GATED. Stdlib only, non-interactive: this is the deterministic worker
the ``/token-telemetry:migrate-to-remote`` command drives, exactly as
``manage.py`` backs the local migration commands. Every remote request goes
through :class:`supabase_backend.SupabaseBackend` — the one TLS-verified,
bounded-timeout, header-only-secret, no-service_role transport — and every SQL
value is bound, never interpolated.

The subcommands split check-from-do so the command never flips the collection
pointer before a verified full upload:

  login                  acquire+persist an Auth session (email/password read
                         from STDIN as JSON, never argv/logs); reconcile identity
  preflight              verify config + session + remote reachability and that
                         the remote schema (supabase/schema.sql) is applied
  local-counts           local per-table row totals (no network) for display
  migrate                sync_users FIRST, then FK-ordered chunked owner-stamped
                         upload, then remote-vs-local COUNT validation — prints
                         counts_match; NEVER flips the pointer
  set-backend            flip active_backend to supabase (only after a verified
                         migrate) or back to local (the reversible rollback)

The core transformation (memo §4b): the LOCAL SQLite schema carries ``owner_id``
only on ``sessions``; the REMOTE schema requires it on projects/models/pricing/
sessions/events. So every uploaded row is stamped with the migrating user's
remote owner id (the reconciled ``auth.uid()``), and events carry the natural
keys (``session_uuid`` / ``model_name``) derived from the local joins. Cursors
are NEVER uploaded — they are authoritative and stay local.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture
import migrate_lib
import settings
import supabase_backend

# The remote tables whose per-owner row counts are validated before the pointer
# flip. `users` is excluded on purpose: under RLS the caller can only ever see
# their OWN users row, so a cross-user count is meaningless — its parent row is
# instead guaranteed present by sync_users running first.
VALIDATED_TABLES = ("projects", "models", "pricing", "sessions", "events")
# Every remote table probed for schema presence in preflight (FK order).
EXPECTED_TABLES = ("users", "projects", "models", "pricing", "sessions",
                   "events")
# The `events` value columns copied verbatim (owner_id/session_uuid/model_name
# are derived separately); order is irrelevant — rows are dicts.
EVENT_VALUE_COLS = ("ts", "kind", "agent", "in_tok", "out_tok", "cache_r",
                    "cache_w", "cache_w_1h", "dur_ms", "branch", "commit_sha",
                    "issue_key", "task_size", "note", "api_calls", "ctx_tokens")


def fail(msg):
    print(msg, file=sys.stderr)
    return 1


def _backend():
    """The active-config Supabase backend, or ``None`` when unconfigured.

    Built straight from the ``supabase`` settings block (not gated on
    ``active_backend``, which is still ``local`` until the very last step of a
    migration), so preflight/login/migrate all run before the pointer flip."""
    cfg = settings.supabase_config()
    if cfg is None:
        return None
    b = supabase_backend.SupabaseBackend(cfg)
    b.open()
    return b


def _audit(db, action, detail):
    """Append one local audit row for the migration operation (req 8)."""
    conn = capture.connect(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO audit_log(ts, action, project, detail)"
                " VALUES (strftime('%s','now'), ?, ?, ?)",
                (action, "central->remote", detail))
    finally:
        conn.close()


# --- login ------------------------------------------------------------------
def do_login(backend):
    """Acquire and persist an Auth session; reconcile identity.

    Reads ``{"email", "password"}`` from STDIN (never argv, so the password
    cannot leak into a process listing) and hands them to
    :meth:`SupabaseBackend.login`, which sends them in the request BODY over
    verified TLS and persists ONLY the returned tokens (mode 0600). Prints the
    Auth uid — never a token or the password."""
    try:
        creds = json.load(sys.stdin)
    except Exception:
        return fail("login requires {\"email\",\"password\"} JSON on stdin")
    email, password = creds.get("email"), creds.get("password")
    if not (email and password):
        return fail("login requires both email and password")
    try:
        result = backend.login(email, password)
    except urllib.error.HTTPError as e:
        return fail(f"login rejected by remote (HTTP {e.code})")
    except Exception:
        return fail("login failed: remote unreachable or invalid response")
    print(f"login_ok=yes uid={result.get('uid')}")
    return 0


# --- preflight --------------------------------------------------------------
def do_preflight(backend):
    """Verify the remote is ready for a migration and report ``key=value`` lines.

    Checks config, a usable Auth session, remote reachability, and — the crux,
    since the remote has no ``PRAGMA user_version`` — that the expected tables
    exist by probing each with a lightweight owner-scoped count. A missing table
    means ``supabase/schema.sql`` has not been applied; the command aborts with
    guidance rather than uploading into a half-provisioned remote."""
    print(f"configured={'yes' if settings.supabase_config() else 'no'}")
    print(f"identity_set={'yes' if settings.current_user() else 'no'}")
    try:
        backend._access_token()
        session_ok = True
    except Exception:
        session_ok = False
    print(f"session={'yes' if session_ok else 'no'}")
    if not session_ok:
        print("reachable=unknown")
        print("schema_present=no")
        return 0
    reachable = True
    missing = []
    for t in EXPECTED_TABLES:
        try:
            backend.count_rows(t)
        except urllib.error.HTTPError:
            missing.append(t)          # reachable, but this table is absent
        except Exception:
            reachable = False
            break
    print(f"reachable={'yes' if reachable else 'no'}")
    present = reachable and not missing
    print(f"schema_present={'yes' if present else 'no'}")
    if missing:
        print(f"missing_tables={','.join(missing)}")
    return 0


# --- local counts -----------------------------------------------------------
def _local_counts(conn):
    counts = {}
    for t in ("users",) + VALIDATED_TABLES:
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return counts


def do_local_counts(db):
    """Print local per-table row totals (no network) so the command can show the
    user what will move before anything is uploaded."""
    if not Path(db).exists():
        return fail(f"central DB does not exist: {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        counts = _local_counts(conn)
    finally:
        conn.close()
    for t in ("users",) + VALIDATED_TABLES:
        print(f"local_{t}={counts[t]}")
    return 0


# --- migrate (the do) -------------------------------------------------------
def _read_upload_rows(conn, owner):
    """Build the FK-ordered, owner-stamped upload payload from the local DB.

    The local schema carries ``owner_id`` only on ``sessions``; here every
    owner-scoped remote row is stamped with ``owner`` (the migrating user's
    remote id), and events are given their natural keys (``session_uuid`` from
    the session join, ``model_name`` from the model join). Cursors are never
    read. Returns an ordered list of ``(table, rows, on_conflict)`` tuples."""
    projects = [
        {"owner_id": owner, "path": path, "name": name,
         "mirror_path": mp, "mirror_last_at": mla}
        for path, name, mp, mla in conn.execute(
            "SELECT path, name, mirror_path, mirror_last_at FROM projects")]
    models = [{"owner_id": owner, "name": name}
              for (name,) in conn.execute("SELECT name FROM models")]
    pricing = [
        {"owner_id": owner, "provider": prov, "model_prefix": pref,
         "model_version": ver, "in_usd": iu, "out_usd": ou, "cache_r_usd": cr,
         "cache_w_usd": cw, "cache_w_1h_usd": cw1, "effective_from": eff,
         "source": src}
        for (prov, pref, ver, iu, ou, cr, cw, cw1, eff, src) in conn.execute(
            "SELECT provider, model_prefix, model_version, in_usd, out_usd,"
            " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from, source"
            " FROM pricing")]
    sessions = [
        {"owner_id": owner, "uuid": uuid, "project_path": path}
        for uuid, path in conn.execute(
            "SELECT s.uuid, p.path FROM sessions s"
            " JOIN projects p ON s.project_id = p.id")]
    events = []
    ev_cols = ", ".join(f"e.{c}" for c in EVENT_VALUE_COLS)
    for row in conn.execute(
            f"SELECT s.uuid, m.name, {ev_cols} FROM events e"
            " JOIN sessions s ON e.session_id = s.id"
            " JOIN models m ON e.model_id = m.id"):
        session_uuid, model_name = row[0], row[1]
        values = dict(zip(EVENT_VALUE_COLS, row[2:]))
        events.append({"owner_id": owner, "session_uuid": session_uuid,
                       "model_name": model_name, **values})
    C = supabase_backend.UPSERT_ON_CONFLICT
    return [("projects", projects, C["projects"]),
            ("models", models, C["models"]),
            ("pricing", pricing, C["pricing"]),
            ("sessions", sessions, C["sessions"]),
            ("events", events, C["events"])]


def _sync_users(conn, backend, owner):
    """Upsert the migrating user's remote ``users`` row FIRST (req 14).

    The remote users row must exist before any owner-scoped FK row references
    it, and its ``uuid`` must equal ``owner`` (== ``auth.uid()``) — which is what
    RLS ``WITH CHECK (auth.uid() = uuid)`` permits the caller to write. Name and
    ``created_at`` come from the local ``users`` row when present (keyed by the
    LOCAL uuid), else from settings + now. Merge-duplicates upsert on ``uuid``,
    so a re-run converges."""
    local = settings.current_user() or {}
    name = local.get("full_name")
    created = None
    local_uuid = local.get("uuid")
    if local_uuid:
        row = conn.execute(
            "SELECT name, created_at FROM users WHERE uuid = ?",
            (local_uuid,)).fetchone()
        if row:
            name = name or row[0]
            created = row[1]
    if not name:
        raise RuntimeError(
            "no full name on file; run register-user before migrating")
    backend.push_rows(
        "users",
        [{"uuid": owner, "name": name,
          "created_at": created if created is not None else int(time.time())}],
        supabase_backend.UPSERT_ON_CONFLICT["users"])


def do_migrate(db, backend):
    """Sync users first, upload FK-ordered + owner-stamped, then count-validate.

    Never flips the collection pointer: on a verified full upload it prints
    ``counts_match=yes`` and exits 0 so the command can flip separately; on any
    upload failure or count mismatch it prints the reason, exits non-zero, and
    the local DB stays authoritative with the pointer unflipped (copy-then-switch
    rollback). Fully re-runnable — every remote write is an idempotent upsert."""
    if not Path(db).exists():
        return fail(f"central DB does not exist: {db}")
    owner = backend.remote_owner_id()
    if not owner:
        return fail("no remote owner id (login first)")
    conn = capture.connect(db)  # schema owner: guarantees v7 columns exist
    try:
        # Retro-link any still-anonymous local sessions to the local uuid so the
        # local store stays consistent (upload stamps the remote owner anyway).
        local_uuid = settings.current_owner_id()
        if local_uuid:
            migrate_lib.retro_link(conn, local_uuid)
        try:
            _sync_users(conn, backend, owner)          # req 14: users FIRST
            uploads = _read_upload_rows(conn, owner)
            sent = {}
            for table, rows, on_conflict in uploads:
                sent[table] = backend.push_rows(table, rows, on_conflict)
        except urllib.error.HTTPError as e:
            return fail(f"upload_ok=no reason=remote rejected a chunk "
                        f"(HTTP {e.code}); pointer NOT flipped")
        except Exception as e:
            return fail(f"upload_ok=no reason={type(e).__name__}; "
                        "pointer NOT flipped, local DB unchanged")
        print("upload_ok=yes")
        local = _local_counts(conn)
    finally:
        conn.close()

    # Count validation (req: verify remote == local per table BEFORE any flip).
    match = True
    remote = {}
    for t in VALIDATED_TABLES:
        try:
            remote[t] = backend.count_rows(t)
        except Exception:
            return fail(f"counts_match=no reason=could not read remote count "
                        f"for {t}; pointer NOT flipped")
        print(f"local_{t}={local[t]} remote_{t}={remote[t]}")
        if local[t] != remote[t]:
            match = False
    print(f"counts_match={'yes' if match else 'no'}")
    _audit(db, "migrate-to-remote",
           f"uploaded {sent}; counts_match={'yes' if match else 'no'}")
    return 0 if match else fail(
        "count validation failed; pointer NOT flipped, local DB authoritative")


# --- set-backend (the flip / rollback) --------------------------------------
def do_set_backend(db, backend_name):
    """Flip the collection pointer (``active_backend``) — the actual switch.

    Writes ``active_backend`` to ``settings.json`` mode 0600 via
    :func:`settings.set_active_backend`. ``supabase`` is the forward switch (run
    only by the command after ``migrate`` reported ``counts_match=yes``);
    ``local`` is the reversible rollback. Atomic — a pointer move, never a data
    move — and audited locally."""
    if backend_name not in ("supabase", "local"):
        return fail(f"unknown backend: {backend_name}")
    settings.set_active_backend(backend_name)
    if Path(db).exists():
        _audit(db, "collection-switch", f"active_backend={backend_name}")
    print(f"active_backend={backend_name}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="remote_migrate.py")
    ap.add_argument("command", choices=[
        "login", "preflight", "local-counts", "migrate", "set-backend"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--backend", default=None)
    a = ap.parse_args(argv)
    db = a.db or str(capture.db_path())

    if a.command == "local-counts":
        return do_local_counts(db)
    if a.command == "set-backend":
        if a.backend is None:
            return fail("set-backend requires --backend supabase|local")
        return do_set_backend(db, a.backend)

    backend = _backend()
    if backend is None:
        return fail("supabase backend is not configured "
                    "(no url in settings.json); configure it first")
    try:
        if a.command == "login":
            return do_login(backend)
        if a.command == "preflight":
            return do_preflight(backend)
        if a.command == "migrate":
            return do_migrate(db, backend)
    finally:
        backend.close()
    return fail("unreachable")


if __name__ == "__main__":
    sys.exit(main())
