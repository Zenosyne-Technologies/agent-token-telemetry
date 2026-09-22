#!/usr/bin/env python3
"""Pure migration primitives shared by the telemetry migration commands.

Stdlib only, non-interactive (no prompts, no ``input``): this is a library of
building blocks the two migration COMMANDS consume — Command A (local project
logs -> the central file DB, a later phase) and Command B (central file DB ->
a remote host, a later phase). Prompting, keep-local decisions and collection
switching are the command's job, never this module's.

Three primitives, all operating on local SQLite connections the caller supplies:

  compat_report(src, dst, kit_version)  pure inspection, NO side effects — the
      version/structure/users-table diff the compatibility safeguard reads
      before a migration proceeds (req 12).
  retro_link(conn, uuid, project_id=None)  bind previously-anonymous sessions to
      a now-known user uuid — ``UPDATE sessions SET owner_id`` (req 13),
      idempotent (a second run affects 0 rows).
  ensure_users_row(conn, uuid, name)  the non-interactive auto-migrate primitive
      (req 12's "auto-migrate the users table" clause): reach v7 via
      ``capture.migrate`` then idempotently upsert the ``users`` row.

Every SQL value is bound as a parameter, never string-built. ``compat_report``
never mutates and never throws on a pre-v7 or malformed DB — it REPORTS the
diff, it does not fix it. ``ensure_users_row`` is the only primitive that
mutates, and only the destination connection it is given.
"""
from dataclasses import dataclass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture

# The tables mirrored across every telemetry store — the ones a migration copies
# and whose structure the safeguard compares. Reference/identity tables included
# so a column added by a later schema version can never be silently dropped.
MIRRORED_TABLES = ("events", "sessions", "projects", "cursors", "models",
                   "pricing", "users")


@dataclass(frozen=True)
class TableDiff:
    """A per-table column asymmetry between the source and destination DBs.

    :param table: the table name.
    :param only_in_src: columns present in the source but missing at the
        destination (a migration into ``dst`` would drop these).
    :param only_in_dst: columns present at the destination but missing in the
        source (the destination is ahead for this table).
    """

    table: str
    only_in_src: tuple
    only_in_dst: tuple


@dataclass(frozen=True)
class CompatReport:
    """The structured result of :func:`compat_report` — pure inspection.

    :param kit_version: the schema version the running kit expects (passed
        through so the safeguard can report it alongside the DBs).
    :param src_version: the source DB's ``PRAGMA user_version`` (``None`` if it
        could not be read).
    :param dst_version: the destination DB's ``PRAGMA user_version`` (``None`` if
        it could not be read).
    :param versions_match: whether the two DBs report the same ``user_version``.
    :param src_matches_kit: whether the source DB is at the kit's version.
    :param dst_matches_kit: whether the destination DB is at the kit's version.
    :param users_at_dst: whether the ``users`` table exists at the destination —
        the presence the safeguard turns into a prompt+auto-migrate when False.
    :param table_diffs: a tuple of :class:`TableDiff`, one per mirrored table
        whose columns differ (empty when structures match).
    """

    kit_version: int
    src_version: object
    dst_version: object
    versions_match: bool
    src_matches_kit: bool
    dst_matches_kit: bool
    users_at_dst: bool
    table_diffs: tuple

    @property
    def has_structure_diff(self):
        """True when any mirrored table's columns differ between the DBs."""
        return bool(self.table_diffs)

    @property
    def compatible(self):
        """True when a migration can proceed without a safeguard intervention:
        versions match, the destination has the ``users`` table, and no mirrored
        table's structure differs. The safeguard reads this."""
        return (self.versions_match and self.users_at_dst
                and not self.table_diffs)


def _user_version(conn):
    """``PRAGMA user_version`` for ``conn``, or ``None`` on any failure — a
    malformed DB reports "unknown", it never raises out of inspection."""
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        return None


def _columns(conn, table):
    """The column-name set of ``table`` (empty when the table is absent or the
    read fails). ``table`` comes only from :data:`MIRRORED_TABLES`."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def _has_table(conn, name):
    """Whether a table named ``name`` exists in ``conn`` (False on any failure)."""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    except Exception:
        return False


def compat_report(src_conn, dst_conn, kit_version):
    """Inspect two local SQLite stores for migration compatibility — no writes.

    Reads each DB's ``PRAGMA user_version`` and whether they agree, diffs the
    columns of every mirrored table (:data:`MIRRORED_TABLES`) so a column present
    in one but not the other surfaces, and checks whether the destination has the
    ``users`` table. This is exactly what the compatibility safeguard (req 12)
    reads before deciding to proceed, prompt, or auto-migrate. Pure inspection:
    it mutates neither connection and never throws on a pre-v7 or otherwise
    malformed DB — it REPORTS the diff, it does not fix it.

    :param src_conn: an open :mod:`sqlite3` connection to the migration source.
    :param dst_conn: an open :mod:`sqlite3` connection to the destination.
    :param kit_version: the schema version the running kit expects (e.g.
        :data:`capture.SCHEMA_VERSION`); reported, and compared to each DB.
    :returns: a :class:`CompatReport`.
    """
    src_ver = _user_version(src_conn)
    dst_ver = _user_version(dst_conn)
    diffs = []
    for table in MIRRORED_TABLES:
        src_cols = _columns(src_conn, table)
        dst_cols = _columns(dst_conn, table)
        only_src = tuple(sorted(src_cols - dst_cols))
        only_dst = tuple(sorted(dst_cols - src_cols))
        if only_src or only_dst:
            diffs.append(TableDiff(table, only_src, only_dst))
    return CompatReport(
        kit_version=kit_version,
        src_version=src_ver,
        dst_version=dst_ver,
        versions_match=(src_ver is not None and src_ver == dst_ver),
        src_matches_kit=(src_ver == kit_version),
        dst_matches_kit=(dst_ver == kit_version),
        users_at_dst=_has_table(dst_conn, "users"),
        table_diffs=tuple(diffs))


def retro_link(conn, uuid, project_id=None):
    """Bind previously-anonymous sessions to a now-known user uuid (req 13).

    Runs ``UPDATE sessions SET owner_id=? WHERE owner_id IS NULL``, optionally
    scoped to a single ``project_id``, committing the update atomically. Only
    pre-identity rows (``owner_id IS NULL``) are touched, so this is idempotent:
    a second run matches nothing and affects 0 rows. The uuid is bound as a
    parameter, never interpolated into the SQL. Requires the v7 shape (the
    ``sessions.owner_id`` column) — call :func:`ensure_users_row` or
    ``capture.migrate`` first on a pre-v7 destination.

    :param conn: an open read-write connection to a v7 (or later) store.
    :param uuid: the user uuid to stamp onto the anonymous sessions.
    :param project_id: when given, restrict the update to that project's
        sessions; ``None`` retro-links every anonymous session in the store.
    :returns: the number of session rows updated.
    """
    with conn:
        if project_id is None:
            cur = conn.execute(
                "UPDATE sessions SET owner_id=? WHERE owner_id IS NULL",
                (uuid,))
        else:
            cur = conn.execute(
                "UPDATE sessions SET owner_id=? WHERE owner_id IS NULL"
                " AND project_id=?", (uuid, project_id))
        return cur.rowcount


def ensure_users_row(conn, uuid, name):
    """Ensure the destination can hold identity, then upsert the user row.

    The non-interactive half of req 12's auto-migrate clause: first bring the
    store to v7 via :func:`capture.migrate` (so a pre-v7 destination gains the
    ``users`` table and ``sessions.owner_id``), then idempotently upsert
    ``users(uuid, name, created_at)`` keyed on uuid — a new uuid inserts with
    ``created_at`` = now, an existing one only updates ``name`` (``created_at``
    is never rewritten). The NAME is supplied by the caller (the command prompts,
    not this library); it is bound as a parameter, never interpolated, and never
    echoed here.

    :param conn: an open read-write connection to the destination store; it must
        already carry capture's baseline tables (``capture.migrate`` applies the
        version deltas, not the v1 baseline).
    :param uuid: the user uuid to upsert.
    :param name: the user's full name (PII — bound as an argument, not logged).
    :returns: ``None``.
    """
    capture.migrate(conn)
    with conn:
        conn.execute(
            "INSERT INTO users(uuid, name, created_at)"
            " VALUES (?, ?, strftime('%s','now'))"
            " ON CONFLICT(uuid) DO UPDATE SET name=excluded.name",
            (uuid, name))
