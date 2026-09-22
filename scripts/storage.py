#!/usr/bin/env python3
"""Storage backend seam for token-telemetry (P3 of AOS-104).

Stdlib only. Defines the abstract :class:`StorageBackend` — the union of the
storage operations capture's write path and report's read path perform
**today** — and the one concrete implementation, :class:`LocalSqliteBackend`,
which wraps the existing ``capture`` SQLite logic with **no behaviour change**.
It is a home for code that already exists, not a rewrite: every method delegates
to the proven ``capture`` functions (``connect``/``migrate``/``insert_events``/
``get_offset``/``write_cursor``) so the bytes written and read are identical.

A future remote backend (Supabase, a later phase) implements the same interface.
The read path is still SQL-coupled — reports run SQLite-dialect SQL over the
connection :meth:`LocalSqliteBackend.open_ro` returns — so a named,
backend-neutral read abstraction (``read_for_report``) is deliberately **not**
introduced here; it arrives with the remote read-parity phase.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import sqlite3

import capture


@dataclass(frozen=True)
class Caps:
    """Honest capability flags a caller can branch on. A backend reports what it
    can actually do; :class:`LocalSqliteBackend`'s values are fixed constants."""

    server_side_aggregation: bool  # backend can run the report SQL/rollups itself
    owns_cursors: bool             # transcript read-cursor authority lives here
    multi_user: bool               # RLS-isolated per-user rows present
    supports_upsert: bool          # idempotent merge-on-conflict writes
    writable: bool                 # backend accepts writes (reports open read-only)


class StorageBackend(ABC):
    """The storage seam capture and report call instead of ``sqlite3`` directly.

    The method set is exactly the union of what the code does with storage today:
    open/close a read-write session, expose a read-only connection, report and
    ensure the schema version, write one firing's events, and get/set a
    transcript cursor. No speculative remote-only methods live here.
    """

    # --- lifecycle ---
    @abstractmethod
    def open(self):
        """Acquire a read-write session (local: ``connect()`` + ``migrate()``)."""

    @abstractmethod
    def open_ro(self):
        """Return a read-only connection to the store, or ``None`` when absent.
        The ``report.open_ro()`` equivalent — the single read-side seam."""

    @abstractmethod
    def close(self):
        """Release the read-write session opened by :meth:`open`."""

    @abstractmethod
    def capabilities(self):
        """Return the backend's :class:`Caps`."""

    # --- schema / compatibility ---
    @abstractmethod
    def schema_version(self):
        """The store's current schema version (local: ``PRAGMA user_version``)."""

    @abstractmethod
    def ensure_schema(self):
        """Bring the store to the current schema (local: ``migrate()``)."""

    # --- write path (capture) ---
    @abstractmethod
    def write_events(self, project, session_uuid, kind_hint, agent, groups,
                     branch=None, commit_sha=None, issue_key=None,
                     task_size=None, note=None, first_capture=False,
                     owner_id=None):
        """Write one firing's aggregated event rows; return the session id."""

    @abstractmethod
    def cursor_get(self, transcript):
        """The stored byte offset for ``transcript`` (0 when unseen)."""

    @abstractmethod
    def cursor_set(self, transcript, offset, session_id):
        """Upsert the read cursor for ``transcript``."""


class LocalSqliteBackend(StorageBackend):
    """The local SQLite store — today's only backend.

    Wraps a single ``usage.db``-family file (the central DB, or a project mirror,
    or an export). :meth:`open` holds one read-write connection in :attr:`conn`,
    which capture's transaction orchestration (``BEGIN IMMEDIATE`` / ``with
    conn:``) drives exactly as before. Every write/cursor/schema method delegates
    to the corresponding ``capture`` function, so behaviour is byte-for-byte
    identical to calling those functions directly.
    """

    def __init__(self, path):
        """:param path: the SQLite DB file this backend reads and writes."""
        self.path = Path(path)
        self.conn = None

    # --- lifecycle ---
    def open(self):
        """Open the read-write connection via ``capture.connect`` — which sets
        WAL, applies :const:`capture.SCHEMA` and runs the migration ladder.
        Returns ``self`` so callers can ``LocalSqliteBackend(p).open()``."""
        self.conn = capture.connect(self.path)
        return self

    def open_ro(self):
        """A read-only connection to :attr:`path`, or ``None`` when the file does
        not exist — identical to the former ``report.open_ro`` seam. This backend
        can never create or migrate through the read-only handle."""
        if not self.path.exists():
            return None
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    def close(self):
        """Close the read-write connection if one is open."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def capabilities(self):
        """Local SQLite runs the report SQL itself, owns the cursor table, has no
        per-user RLS isolation, upserts via ``ON CONFLICT``, and is writable."""
        return Caps(server_side_aggregation=True, owns_cursors=True,
                    multi_user=False, supports_upsert=True, writable=True)

    # --- schema / compatibility ---
    def schema_version(self):
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def ensure_schema(self):
        capture.migrate(self.conn)

    # --- write path ---
    def write_events(self, project, session_uuid, kind_hint, agent, groups,
                     branch=None, commit_sha=None, issue_key=None,
                     task_size=None, note=None, first_capture=False,
                     owner_id=None):
        """Delegate to ``capture.insert_events`` on the held connection. The
        caller owns the transaction, exactly as before."""
        return capture.insert_events(
            self.conn, project, session_uuid, kind_hint, agent, groups,
            branch, commit_sha, issue_key, task_size, note,
            first_capture=first_capture, owner_id=owner_id)

    def cursor_get(self, transcript):
        return capture.get_offset(self.conn, transcript)

    def cursor_set(self, transcript, offset, session_id):
        capture.write_cursor(self.conn, transcript, offset, session_id)
