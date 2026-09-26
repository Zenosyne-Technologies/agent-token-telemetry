#!/usr/bin/env python3
"""Token telemetry capture hook for Claude Code (Stop / SubagentStop).

Stdlib only. Reads hook JSON on stdin, appends compact usage rows to the
central SQLite DB. Must never break a session: always exits 0.
"""
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import settings
import storage


def db_path():
    return Path(os.environ.get("TOKEN_TELEMETRY_DB",
                               "~/.claude/telemetry/usage.db")).expanduser()


def parse_ts(iso_str):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()


def read_new_entries(path, offset):
    """Parse complete JSONL lines from byte offset; never consume a partial
    trailing line (transcripts are append-only and may be mid-write)."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end == -1:
        return [], offset
    consumed = data[:end + 1]
    entries = []
    for line in consumed.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries, offset + len(consumed)


def aggregate(entries):
    """Sum usage per (model, sidechain) group. Sidechain entries are subagent
    activity recorded in the same transcript.

    One API call is written as one transcript line PER CONTENT BLOCK, each
    line repeating the same message.id with a usage snapshot — so usage is
    counted once per id, last line wins (snapshots are cumulative; the last
    carries the call's final totals). Lines without an id sum individually."""
    groups = {}
    for e in entries:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        model = msg.get("model") or "unknown"
        side = 1 if e.get("isSidechain") else 0
        g = groups.setdefault((model, side), {
            "in": 0, "out": 0, "cr": 0, "cw": 0, "cw1h": 0,
            "first": None, "last": None,
            "_by_id": {}, "_no_id": [],
        })
        mid = msg.get("id")
        if mid:
            g["_by_id"][mid] = usage
        else:
            g["_no_id"].append(usage)
        ts = e.get("timestamp")
        if ts:
            try:
                t = parse_ts(ts)
            except ValueError:
                continue
            if g["first"] is None or t < g["first"]:
                g["first"] = t
            if g["last"] is None or t > g["last"]:
                g["last"] = t
    for g in groups.values():
        usages = list(g.pop("_by_id").values()) + g.pop("_no_id")
        # Per-slice agent metrics: one usage snapshot per API call after the
        # message.id dedupe, and the LAST call's input side is the context
        # size when the slice ended (what Claude Code's own token gauge shows).
        g["calls"] = len(usages)
        g["ctx"] = 0
        for usage in usages:
            g["in"] += usage.get("input_tokens") or 0
            g["out"] += usage.get("output_tokens") or 0
            g["cr"] += usage.get("cache_read_input_tokens") or 0
            # cw stays the TTL-agnostic total; cw1h carries the 1-hour portion
            # (billed at 2x input vs 1.25x for 5m) when the API splits it out.
            cc = usage.get("cache_creation") or {}
            cw = usage.get("cache_creation_input_tokens")
            if cw is None:
                cw = ((cc.get("ephemeral_5m_input_tokens") or 0)
                      + (cc.get("ephemeral_1h_input_tokens") or 0))
            g["cw"] += cw or 0
            g["cw1h"] += cc.get("ephemeral_1h_input_tokens") or 0
            g["ctx"] = ((usage.get("input_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                        + (cw or 0))
    return groups


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS models  (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL,
  project_id INTEGER NOT NULL REFERENCES projects(id));
CREATE TABLE IF NOT EXISTS events(
  ts         INTEGER NOT NULL,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  kind       INTEGER NOT NULL,
  agent      TEXT,
  model_id   INTEGER NOT NULL REFERENCES models(id),
  in_tok     INTEGER NOT NULL DEFAULT 0,
  out_tok    INTEGER NOT NULL DEFAULT 0,
  cache_r    INTEGER NOT NULL DEFAULT 0,
  cache_w    INTEGER NOT NULL DEFAULT 0,
  dur_ms     INTEGER,
  branch     TEXT,
  commit_sha TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE TABLE IF NOT EXISTS cursors(transcript TEXT PRIMARY KEY,
  offset INTEGER NOT NULL, session_id INTEGER NOT NULL);
"""

PRICING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pricing(
  provider       TEXT NOT NULL,
  model_prefix   TEXT NOT NULL,
  model_version  TEXT NOT NULL DEFAULT '',
  in_usd         REAL,
  out_usd        REAL,
  cache_r_usd    REAL,
  cache_w_usd    REAL,
  cache_w_1h_usd REAL,
  effective_from INTEGER NOT NULL,
  source         TEXT,
  UNIQUE(provider, model_prefix, model_version, effective_from));
"""

# USD per 1M tokens: input, output, cache read, 5m cache write (1.25x input),
# 1h cache write (2x input). Prefixes (not full model names) so a new dated
# release prices correctly on longest-prefix match. effective_from 0: the seed
# applies to all history until a dated row supersedes it.
PRICING_SEED = [
    ("anthropic", "claude-fable-", 10.0, 50.0, 1.00, 12.50, 20.0),
    ("anthropic", "claude-opus-", 5.0, 25.0, 0.50, 6.25, 10.0),
    ("anthropic", "claude-sonnet-", 3.0, 15.0, 0.30, 3.75, 6.0),
    ("anthropic", "claude-haiku-", 1.0, 5.0, 0.10, 1.25, 2.0),
]
SEED_SOURCE = "seed-v0.2.0"

# A "family default" pricing row is one whose model_prefix is a bare family
# prefix — `claude-<lowercase letters>-` and nothing else (the seed rows above,
# and every dated `claude-<family>-` row pricing-update writes). It is the
# fallback rate for any model of that family without a row of its own, so an
# event that resolves to one prices at an ESTIMATE. See
# docs/TELEMETRY-CONTRACT.md §Pricing table. Postgres twin:
# `model_prefix ~ '^claude-[a-z]+-$'` (supabase/reports.sql).
_FAMILY_DEFAULT_RE = re.compile(r"claude-[a-z]+-")


def is_family_default(prefix):
    """Whether a pricing row's ``model_prefix`` is a bare family prefix.

    Exactly the regex ``^claude-[a-z]+-$`` (ASCII lowercase letters only, at
    least one, whole string): ``claude-opus-`` and ``claude-fable-`` are family
    defaults; ``claude-opus-5-5``, ``claude-opus-4-2025``, ``claude-3-5-haiku``
    and ``claude-sonnet-4`` are not.

    :param prefix: a ``pricing.model_prefix`` value (``None`` is not one).
    :returns: ``True`` when the prefix is a family default, else ``False``.
    """
    return isinstance(prefix, str) and \
        _FAMILY_DEFAULT_RE.fullmatch(prefix) is not None


def family_default_sql(col):
    """SQLite boolean expression (1/0, NULL for a NULL ``col``) equivalent to
    :func:`is_family_default` on the text column/expression ``col``.

    A single ``GLOB 'claude-[a-z]*-'`` is NOT equivalent — its ``*`` admits
    digits and dashes (``claude-3-5-haiku-``, ``claude-opus-4-``). This checks
    the structure instead: the value starts with ``claude-``, ends with ``-``,
    has at least one character between them, and that middle contains no
    character outside ``a-z``. GLOB is case-sensitive and compares code
    points, matching the regex. Equivalence is pinned by tests over a
    positive/negative list plus a randomized sweep.

    :param col: a trusted SQL expression (a column reference), never user input.
    :returns: the SQL fragment, parenthesized.
    """
    return (f"({col} GLOB 'claude-?*-'"
            f" AND substr({col}, 8, length({col}) - 8) NOT GLOB '*[^a-z]*')")


# An "ancestor row" is a pricing row that resolves for a model only because
# the model is an unlisted POINT RELEASE of the row's version: the remainder R
# (the model name with the row's model_prefix removed from its start) opens
# with a 1–2 digit point-release segment — `^-[0-9]{1,2}(-|$)`. E.g. prefix
# `claude-opus-5` for model `claude-opus-5-5` (R = `-5`). A date snapshot is
# NOT an ancestor: `claude-haiku-4-5` for `claude-haiku-4-5-20251001`
# (R = `-20251001`), `claude-opus-4-2025` for `claude-opus-4-20250514`
# (R = `0514`). An event resolved to an ancestor row prices at its nearest
# listed ancestor's rate — an ESTIMATE, like a family default. Postgres twin
# in supabase/reports.sql (`~ '^-[0-9]{1,2}(-|$)'`).
_ANCESTOR_REMAINDER_RE = re.compile(r"-[0-9]{1,2}(?:-|\Z)")


def is_ancestor_row(model_name, prefix):
    """Whether ``prefix`` prices ``model_name`` only as its nearest listed
    ancestor (the model is an unlisted point release of that row's version).

    R = ``model_name`` with ``len(prefix)`` leading characters removed (the
    resolver guarantees the prefix matches); the row is an ancestor row when R
    matches ``^-[0-9]{1,2}(-|$)`` (ASCII digits, ``$`` = end of string).

    :param model_name: the event's model name.
    :param prefix: the resolved row's ``model_prefix``.
    :returns: ``True`` for an ancestor row; ``False`` otherwise, including
        when either argument is ``None``.
    """
    if not isinstance(model_name, str) or not isinstance(prefix, str):
        return False
    return _ANCESTOR_REMAINDER_RE.match(model_name[len(prefix):]) is not None


def ancestor_row_sql(name_col, prefix_col):
    """SQLite boolean expression (1/0, NULL when either input is NULL)
    equivalent to :func:`is_ancestor_row`.

    SQLite has no regex, so ``^-[0-9]{1,2}(-|$)`` is spelled as the four
    structural GLOB shapes it admits — R is exactly ``-D``, ``-D-…``, ``-DD``
    or ``-DD-…`` (D an ASCII digit; GLOB's ``*`` matches any tail, newlines
    included; ``[0-9]`` compares code points). Equivalence is pinned by tests
    over named edge cases plus a randomized sweep.

    :param name_col: trusted SQL expression for the model name.
    :param prefix_col: trusted SQL expression for the row's ``model_prefix``.
    :returns: the SQL fragment, parenthesized.
    """
    r = f"substr({name_col}, length({prefix_col}) + 1)"
    return (f"({r} GLOB '-[0-9]' OR {r} GLOB '-[0-9]-*'"
            f" OR {r} GLOB '-[0-9][0-9]' OR {r} GLOB '-[0-9][0-9]-*')")


def is_estimated(model_name, prefix):
    """Whether an event of ``model_name`` resolved to the pricing row
    ``prefix`` prices at an ESTIMATE: the row is a family default row
    (:func:`is_family_default`) or an ancestor row (:func:`is_ancestor_row`).
    docs/TELEMETRY-CONTRACT.md §Pricing table.

    :param model_name: the event's model name.
    :param prefix: the resolved row's ``model_prefix`` (``None`` = unpriced).
    :returns: ``True`` when estimated, else ``False``.
    """
    return is_family_default(prefix) or is_ancestor_row(model_name, prefix)


def estimated_sql(name_col, prefix_col):
    """SQLite boolean expression equivalent to :func:`is_estimated` — 1/0 for
    a priced event, NULL when ``prefix_col`` is NULL (unpriced).

    :param name_col: trusted SQL expression for the model name.
    :param prefix_col: trusted SQL expression for the row's ``model_prefix``.
    :returns: the SQL fragment, parenthesized.
    """
    return (f"({family_default_sql(prefix_col)}"
            f" OR {ancestor_row_sql(name_col, prefix_col)})")

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log(
  ts      INTEGER NOT NULL,
  action  TEXT NOT NULL,
  project TEXT NOT NULL,
  detail  TEXT);
"""

# v7: identity foundation — the `users` table and a nullable
# `sessions.owner_id` REFERENCES users(uuid). Additive and inert: this phase
# only lays the schema down. NULL owner_id = pre-identity, never backfilled
# except by a later retro-link step.
USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(uuid TEXT PRIMARY KEY, name TEXT NOT NULL,
  created_at INTEGER NOT NULL);
"""

V2_COLUMNS = ("issue_key", "task_size", "note")
# v3: where a project-level copy of this project's events lives, and the event
# timestamp of the last capture that was configured to write one.
V3_PROJECT_COLUMNS = (("mirror_path", "TEXT"), ("mirror_last_at", "INTEGER"))
MIRROR_META = {col for col, _ in V3_PROJECT_COLUMNS}
# v4: cache writes split by TTL. events.cache_w stays the total; cache_w_1h is
# the 1-hour portion (5m portion = cache_w - cache_w_1h). pricing gains the 1h
# write rate; NULL there means unknown and cost queries fall back to cache_w_usd.
V4_COLUMNS = (("events", "cache_w_1h", "INTEGER NOT NULL DEFAULT 0"),
              ("pricing", "cache_w_1h_usd", "REAL"))
# v5: human project name on `projects` — stamped by capture from the kit's
# .docs/PROJECT-INFO.md (`project:` frontmatter key) or registered at enable
# time; NULL means unknown and reports fall back to the path basename.
V5_COLUMNS = (("projects", "name", "TEXT"),)
# v6: per-event agent metrics — how many API calls the slice contains and the
# context size (input side of the LAST call: input + cache read + cache write)
# when it ended. NULL on pre-v6 rows = unknown, never backfilled.
V6_COLUMNS = (("events", "api_calls", "INTEGER"),
              ("events", "ctx_tokens", "INTEGER"))
# v7: `sessions.owner_id` — nullable FK to users(uuid). NULL = pre-identity
# (rows recorded before the identity feature), never backfilled except by a
# later retro-link step. The `users` table itself lives in USERS_SCHEMA above.
V7_COLUMNS = (("sessions", "owner_id", "TEXT REFERENCES users(uuid)"),)
# v8: a DATA step, no shape change — the one-time fold of linked-worktree
# project rows into their main repository's row (see migrate_v8).
SCHEMA_VERSION = 8


def table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def event_columns(conn):
    return table_columns(conn, "events")


def has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def migrate(conn):
    """SCHEMA is the v1 baseline; later versions are deltas applied here, in
    order, each hop gated on its own post-condition. Every statement is
    individually idempotent rather than wrapped in one transaction, so a
    migrating process never blocks a peer's capture."""
    # The version stamp alone is not proof the schema matches it: a DB stamped
    # for a version it does not actually have (an older build that stamped too
    # early, a restored/edited file) would otherwise fail every capture forever.
    # A few extra PRAGMA reads on the fast path buy that DB a self-heal.
    if (conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION
            and set(V2_COLUMNS) <= event_columns(conn)
            and MIRROR_META | {"name"} <= table_columns(conn, "projects")
            and has_table(conn, "audit_log")
            and "cache_w_1h" in event_columns(conn)
            and "cache_w_1h_usd" in table_columns(conn, "pricing")
            and {"api_calls", "ctx_tokens"} <= event_columns(conn)
            and has_table(conn, "users")
            and "owner_id" in table_columns(conn, "sessions")):
        return
    # Hops run in sequence and each returns whether its shape actually landed:
    # v3 must never be attempted - let alone stamped - on a DB that failed v2.
    if (migrate_v2(conn) and migrate_v3(conn) and migrate_v4(conn)
            and migrate_v5(conn) and migrate_v6(conn) and migrate_v7(conn)):
        migrate_v8(conn)


def migrate_v2(conn):
    """v1 -> v2: the three kit-aware `events` columns and the `pricing` table."""
    for col in V2_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # duplicate column (peer process) - or a transient failure,
            # which the post-condition check below catches
    # Never stamp a version the schema does not actually have: the except above
    # cannot tell "already added" from "database is locked"/"disk full", and a
    # premature stamp would strand the DB without the columns forever.
    if not set(V2_COLUMNS) <= event_columns(conn):
        return False  # next connect retries
    conn.executescript(PRICING_SCHEMA)
    # Gate on the seed rows, not the table: CREATE TABLE autocommits, so a
    # failure before the INSERT can leave an empty table that must still get
    # seeded. OR IGNORE covers two processes seeding concurrently.
    if not conn.execute("SELECT 1 FROM pricing WHERE source=? LIMIT 1",
                        (SEED_SOURCE,)).fetchone():
        conn.executemany(
            "INSERT OR IGNORE INTO pricing(provider, model_prefix, in_usd,"
            " out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,"
            " effective_from, source)"
            " VALUES (?,?,?,?,?,?,?,0,?)",
            [(*row, SEED_SOURCE) for row in PRICING_SEED])
    # Commit the data before stamping the version: a crash in between leaves
    # user_version < 2, and the next connect simply migrates again. Also leaves
    # no open transaction for main()'s BEGIN IMMEDIATE to trip over.
    conn.commit()
    conn.execute("PRAGMA user_version=2")
    return True


def migrate_v3(conn):
    """v2 -> v3: mirror metadata on `projects` and the `audit_log` table the
    storage-management commands (`/storage-separate`, `/storage-delete`) append
    to. Same discipline as v2: idempotent ALTERs, post-condition verified before
    the version stamp."""
    for col, coltype in V3_PROJECT_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # duplicate column, or a transient failure the check catches
    conn.executescript(AUDIT_SCHEMA)
    if not (MIRROR_META <= table_columns(conn, "projects")
            and has_table(conn, "audit_log")):
        return False  # next connect retries; the stamp stays at 2
    conn.commit()
    conn.execute("PRAGMA user_version=3")
    return True


def migrate_v4(conn):
    """v3 -> v4: cache writes split by TTL — `events.cache_w_1h` (the 1-hour
    portion; `cache_w` stays the TTL-agnostic total, so every pre-v4 query
    keeps working) and `pricing.cache_w_1h_usd` (NULL = unknown; cost queries
    fall back to `cache_w_usd`, which is exactly the pre-v4 estimate). Same
    discipline: idempotent ALTERs, post-condition verified before the stamp.
    A fresh DB's pricing table is created with the column already (the CREATE
    lives in PRICING_SCHEMA), so its ALTER lands in the except arm here."""
    for table, col, coltype in V4_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # duplicate column, or a transient failure the check catches
    if not ("cache_w_1h" in event_columns(conn)
            and "cache_w_1h_usd" in table_columns(conn, "pricing")):
        return False  # next connect retries; the stamp stays at 3
    conn.commit()
    conn.execute("PRAGMA user_version=4")
    return True


def migrate_v5(conn):
    """v4 -> v5: `projects.name` — the human project name, filled by capture
    from the kit's PROJECT-INFO or registered at enable time; NULL = unknown
    (reports fall back to the path basename). Same discipline as every hop."""
    for table, col, coltype in V5_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # duplicate column, or a transient failure the check catches
    if "name" not in table_columns(conn, "projects"):
        return False  # next connect retries; the stamp stays at 4
    conn.commit()
    conn.execute("PRAGMA user_version=5")
    return True


def migrate_v6(conn):
    """v5 -> v6: per-event agent metrics — `events.api_calls` and
    `events.ctx_tokens` (NULL = unknown on pre-v6 rows). Same discipline."""
    for table, col, coltype in V6_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # duplicate column, or a transient failure the check catches
    if not {"api_calls", "ctx_tokens"} <= event_columns(conn):
        return False  # next connect retries; the stamp stays at 5
    conn.commit()
    conn.execute("PRAGMA user_version=6")
    return True


def migrate_v7(conn):
    """v6 -> v7: identity foundation — the `users` table and a nullable
    `sessions.owner_id` REFERENCES users(uuid) (NULL = pre-identity, never
    backfilled except by a later retro-link step). Additive and inert: nothing
    here mints uuids or stamps owner_id yet. Same discipline: idempotent
    CREATE/ALTER, post-condition verified before the stamp."""
    conn.executescript(USERS_SCHEMA)
    for table, col, coltype in V7_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # duplicate column, or a transient failure the check catches
    if not (has_table(conn, "users")
            and "owner_id" in table_columns(conn, "sessions")):
        return False  # next connect retries; the stamp stays at 6
    conn.commit()
    conn.execute("PRAGMA user_version=7")
    return True


def worktree_fold_target(path, other_paths):
    """Where a ``projects`` row recorded for a linked worktree belongs: the
    main repository path to fold it into, or None to leave the row alone.

    Two rules, in order:

    1. **Path convention** (works after the worktree folder was deleted).
       ``<M>`` is the prefix before the FIRST :data:`WORKTREE_COMPONENT` in
       ``path``, followed by at least one non-empty name segment. Accepted only
       when ``<M>`` is realpath-equal to another row's path, or ``<M>`` is an
       existing directory holding a ``.git`` directory. Anything else (e.g. an
       unrelated path that merely contains the component deeper down) falls
       through to rule 2 and is otherwise left alone.
    2. **Resolution** — ``path`` still exists and :func:`main_repo_root`
       resolves it to a different directory.

    :param path: the row's stored path.
    :param other_paths: every OTHER row's stored path.
    :returns: the main-root path string, or None.
    """
    i = path.find(WORKTREE_COMPONENT)
    if i > 0 and any(path[i + len(WORKTREE_COMPONENT):].split("/")):
        main = path[:i]
        real = os.path.realpath(main)
        if (any(os.path.realpath(p) == real for p in other_paths)
                or (os.path.isdir(main)
                    and os.path.isdir(os.path.join(main, ".git")))):
            return main
    if os.path.isdir(path):
        main = main_repo_root(path)
        if (main is not None
                and os.path.realpath(main) != os.path.realpath(path)):
            return str(main)
    return None


def fold_worktree_projects(conn):
    """Fold every linked-worktree ``projects`` row into its main repository's
    row (:func:`worktree_fold_target`). Caller owns the transaction.

    Per folded row: ``sessions.project_id`` is reassigned (the only table keyed
    by project id — events hang off sessions, cursors off transcripts); the
    main row takes the worktree's ``name`` when it has none, and its
    ``mirror_path``/``mirror_last_at`` pair when it has no mirror configured
    (a main row's own pair is never overwritten); the worktree row is deleted.
    A main row that does not exist yet is created with the target spelling.
    ``audit_log.project`` is free text, a historical record — untouched.
    Idempotent: a second run finds no worktree rows.

    :param conn: an open DB connection inside a write transaction.
    :returns: list of ``(worktree_path, main_path)`` pairs folded.
    """
    cols = table_columns(conn, "projects")
    folded = []
    for (pid, path) in conn.execute(
            "SELECT id, path FROM projects ORDER BY id").fetchall():
        others = [p for (p,) in conn.execute(
            "SELECT path FROM projects WHERE id<>?", (pid,))]
        target = worktree_fold_target(path, others)
        if target is None:
            continue
        dest_path = canonical_project_path(conn, target)
        row = conn.execute("SELECT id FROM projects WHERE path=?",
                           (dest_path,)).fetchone()
        dest = row[0] if row else conn.execute(
            "INSERT INTO projects(path) VALUES (?)", (dest_path,)).lastrowid
        if dest == pid:
            continue
        conn.execute("UPDATE sessions SET project_id=? WHERE project_id=?",
                     (dest, pid))
        if "name" in cols:
            conn.execute(
                "UPDATE projects SET name=(SELECT name FROM projects WHERE id=?)"
                " WHERE id=? AND COALESCE(name,'')=''", (pid, dest))
        if MIRROR_META <= cols:
            conn.execute(
                "UPDATE projects SET"
                " mirror_path=(SELECT mirror_path FROM projects WHERE id=?),"
                " mirror_last_at=(SELECT mirror_last_at FROM projects WHERE id=?)"
                " WHERE id=? AND mirror_path IS NULL", (pid, pid, dest))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        folded.append((path, dest_path))
    return folded


def migrate_v8(conn):
    """v7 -> v8: a one-time DATA step — fold linked-worktree project rows into
    their main repository (:func:`fold_worktree_projects`). No shape change.

    One ``BEGIN IMMEDIATE`` transaction holds the fold AND the version stamp
    (``user_version`` is transactional), so a crash leaves both or neither;
    the version is re-read under the lock, so a peer that folded first makes
    this a no-op. Any failure rolls back and returns False — the next connect
    retries, and capture is never failed over it.

    :param conn: an open DB connection with no transaction in progress.
    :returns: True once the DB is at v8.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            fold_worktree_projects(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    # The WAL switch needs momentary exclusive access; under a fresh-DB
    # stampede it can raise "database is locked" despite the busy timeout.
    # WAL is persistent per-database, so one winner is enough - retry
    # briefly, then proceed either way (journal mode never affects
    # correctness, only concurrency performance).
    for _ in range(20):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError:
            time.sleep(0.05)
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def get_or_create(conn, table, column, value):
    row = conn.execute(
        f"SELECT id FROM {table} WHERE {column}=?", (value,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        f"INSERT INTO {table}({column}) VALUES (?)", (value,)).lastrowid


def get_offset(conn, transcript):
    row = conn.execute(
        "SELECT offset FROM cursors WHERE transcript=?", (str(transcript),)).fetchone()
    return row[0] if row else 0


def write_cursor(conn, transcript, offset, session_id):
    """Upsert the per-transcript read cursor — the single home for the cursor
    write the central write path performs (from both `record` and
    `sweep_subagents`). `LocalSqliteBackend.cursor_set` delegates here so the
    seam and the direct callers share one statement and cannot drift."""
    conn.execute(
        "INSERT INTO cursors(transcript, offset, session_id) VALUES (?,?,?)"
        " ON CONFLICT(transcript) DO UPDATE SET offset=excluded.offset",
        (str(transcript), offset, session_id))


# A first capture whose aggregated span exceeds this is a roll-up of
# pre-telemetry history, not a per-turn delta — marked so windowed reports
# and the dashboard can keep it out of day/week figures (all-time totals
# include it). The event's dur_ms carries the actual span.
BACKLOG_SPAN_S = 86400
BACKLOG_NOTE = "backlog-capture"


def derive_event_fields(kind_hint, agent, groups, branch=None, commit_sha=None,
                        issue_key=None, task_size=None, note=None,
                        first_capture=False):
    """The pure per-group event-field derivation shared by every storage
    backend — the SINGLE source of truth for how a `(model, sidechain)` group
    becomes one event row, with NO storage side effects.

    `insert_events` (local SQLite) and the remote backend both call this, so the
    two write paths cannot drift into recording different rows (the split-
    responsibility defect class). It returns a list of ``(model, kind, fields)``
    where ``fields`` is a scalar-only dict — JSON-serializable, so a remote
    backend can queue it in an offline outbox unchanged. Column-to-storage
    mapping (model name → local `model_id`, session → `session_id`) stays with
    each backend; only the value derivation lives here.

    `first_capture` marks rows whose span exceeds BACKLOG_SPAN_S as backlog
    roll-ups — but never over a real sidecar note. The sub-agent `agent` label
    belongs to sub-agent rows only (kind=1): a main-loop row must never wear the
    name of whatever sub-agent happened to trigger the firing.

    :returns: ``[(model, kind, fields_dict), ...]`` in ``groups`` iteration order.
    """
    rows = []
    for (model, side), g in groups.items():
        kind = 1 if (kind_hint or side) else 0
        row_agent = agent if kind else None
        row_note = note
        if (row_note is None and first_capture and g["first"] is not None
                and g["last"] - g["first"] > BACKLOG_SPAN_S):
            row_note = BACKLOG_NOTE
        dur = (int((g["last"] - g["first"]) * 1000)
               if g["first"] is not None else None)
        ts = int(g["last"]) if g["last"] is not None else int(time.time())
        rows.append((model, kind, {
            "ts": ts, "kind": kind, "agent": row_agent,
            "in_tok": g["in"], "out_tok": g["out"], "cache_r": g["cr"],
            "cache_w": g["cw"], "cache_w_1h": g.get("cw1h", 0), "dur_ms": dur,
            "branch": branch, "commit_sha": commit_sha, "issue_key": issue_key,
            "task_size": task_size, "note": row_note,
            "api_calls": g.get("calls"), "ctx_tokens": g.get("ctx")}))
    return rows


def insert_events(conn, project, session_uuid, kind_hint, agent, groups,
                  branch=None, commit_sha=None, issue_key=None,
                  task_size=None, note=None, first_capture=False,
                  owner_id=None):
    """One event row per (model, sidechain) group. Caller owns the transaction.
    Returns the session id (the cursor row, written only in the central DB,
    needs it). Row values come from :func:`derive_event_fields`, the derivation
    shared with the remote backend.

    `owner_id` (a user uuid from central settings, or None) is stamped ONLY when
    this call CREATES the session row — an existing session, and every pre-v7 DB
    that has no `owner_id` column, is left exactly as before (NULL = pre-identity,
    never backfilled here). Reading the column set first means an identity-less
    capture and a stranded pre-v7 DB both take the unchanged two-column INSERT."""
    project_id = get_or_create(conn, "projects", "path", project)
    row = conn.execute(
        "SELECT id FROM sessions WHERE uuid=?", (session_uuid,)).fetchone()
    if row:
        session_id = row[0]
    elif owner_id is not None and "owner_id" in table_columns(conn, "sessions"):
        session_id = conn.execute(
            "INSERT INTO sessions(uuid, project_id, owner_id) VALUES (?,?,?)",
            (session_uuid, project_id, owner_id)).lastrowid
    else:
        session_id = conn.execute(
            "INSERT INTO sessions(uuid, project_id) VALUES (?,?)",
            (session_uuid, project_id)).lastrowid
    for model, _kind, f in derive_event_fields(
            kind_hint, agent, groups, branch, commit_sha, issue_key,
            task_size, note, first_capture):
        model_id = get_or_create(conn, "models", "name", model)
        conn.execute(
            "INSERT INTO events(ts, session_id, kind, agent, model_id,"
            " in_tok, out_tok, cache_r, cache_w, cache_w_1h, dur_ms, branch,"
            " commit_sha, issue_key, task_size, note, api_calls, ctx_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["ts"], session_id, f["kind"], f["agent"], model_id,
             f["in_tok"], f["out_tok"], f["cache_r"], f["cache_w"],
             f["cache_w_1h"], f["dur_ms"], f["branch"], f["commit_sha"],
             f["issue_key"], f["task_size"], f["note"],
             f["api_calls"], f["ctx_tokens"]))
    return session_id


def latest_event_ts(groups):
    stamps = [g["last"] for g in groups.values() if g["last"] is not None]
    return int(max(stamps)) if stamps else int(time.time())


def stamp_mirror_meta(conn, project, mirror_path, ts):
    """Record on the central `projects` row that this project keeps a
    project-level copy, and how recent the capture that expected one was.

    This is **configured state, not a write receipt**: it is stamped inside the
    central transaction, before the mirror write is even attempted, and stays
    stamped when that write later fails. The central DB must always know a
    project-level copy exists - a reader that inferred storage mode from a
    successful mirror write would report project mode as central exactly when
    the mirror is broken and needs looking at. `mirror_last_at` therefore answers
    "when was the last capture destined for this mirror", and only
    `/token-telemetry:storage-status`'s file check answers "did it land"."""
    # A DB whose v3 hop has not landed yet (post-condition withheld the stamp)
    # has no such columns; the metadata is never worth failing the capture over.
    if MIRROR_META <= table_columns(conn, "projects"):
        conn.execute(
            "UPDATE projects SET mirror_path=?, mirror_last_at=? WHERE path=?",
            (str(mirror_path), ts, str(project)))


def record(conn, project, session_uuid, kind_hint, agent, groups,
           transcript, new_offset, branch=None, commit_sha=None,
           issue_key=None, task_size=None, note=None, mirror_path=None,
           first_capture=False, owner_id=None):
    with conn:
        session_id = insert_events(conn, project, session_uuid, kind_hint,
                                   agent, groups, branch, commit_sha,
                                   issue_key, task_size, note,
                                   first_capture=first_capture,
                                   owner_id=owner_id)
        if mirror_path is not None:
            stamp_mirror_meta(conn, project, mirror_path,
                              latest_event_ts(groups))
        write_cursor(conn, transcript, new_offset, session_id)


# At most this many sub-agent transcript files advance per hook firing, so a
# large backlog (the first sweep after upgrading, or a subagent-heavy session)
# cannot blow the hook timeout — the remainder lands on subsequent firings.
SUBAGENT_BATCH = 40


def subagents_dir(transcript):
    """The harness writes each sub-agent's transcript to
    `<dir>/<session-id>/subagents/agent-<id>.jsonl` beside the main transcript;
    the SubagentStop hook itself only carries the MAIN transcript path."""
    p = Path(transcript)
    return p.parent / p.stem / "subagents"


def subagent_label(jsonl_path):
    """agentType from the sibling `.meta.json` (e.g. 'marvin:developer').
    None on any problem — the hook payload's agent field is the fallback."""
    try:
        with open(jsonl_path.with_name(jsonl_path.stem + ".meta.json")) as f:
            label = json.load(f).get("agentType")
        return label if isinstance(label, str) and label else None
    except Exception:
        return None


def sweep_subagents(conn, project, session_uuid, transcript, meta,
                    hook_agent, owner_id=None):
    """Capture new usage from the session's sub-agent transcript files —
    per-file cursors, one kind=1 event batch per file, labeled from its
    meta.json. Bounded by SUBAGENT_BATCH per firing. Wraps its own BEGIN
    IMMEDIATE (the caller's main-transcript transaction has already
    committed). Returns the inserted event-arg tuples for mirroring."""
    d = subagents_dir(transcript)
    try:
        files = sorted(p for p in d.iterdir() if p.suffix == ".jsonl")
    except OSError:
        return []  # no subagents directory — nothing to sweep
    inserted = []
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        for f in files:
            if len(inserted) >= SUBAGENT_BATCH:
                break
            offset = get_offset(conn, f)
            try:
                if f.stat().st_size <= offset:
                    continue  # nothing new — skip without opening the file
            except OSError:
                continue
            entries, new_offset = read_new_entries(f, offset)
            groups = aggregate(entries)
            if not groups and new_offset == offset:
                continue
            event_args = (project, session_uuid, 1,
                          subagent_label(f) or hook_agent, groups)
            session_id = insert_events(conn, *event_args, *meta,
                                       first_capture=(offset == 0),
                                       owner_id=owner_id)
            write_cursor(conn, f, new_offset, session_id)
            if groups:
                inserted.append((event_args, offset == 0))
    return inserted


def mirror_events(root, *args, **kwargs):
    """Copy the rows just committed centrally into the project-local DB, so the
    data travels with the repo. The central DB stays authoritative: it alone
    tracks cursors, so nothing here can change what gets read from a transcript.
    No cursor is written here - a retried capture can therefore duplicate rows in
    the mirror; they are identical rows, so consumers dedupe on the full tuple.
    Must be called only after the central connection is closed: this opens a
    second DB and must never do so while holding the central write lock."""
    path = mirror_db_path(root)
    # The mirror path lives inside the repo, so it can arrive as a committed
    # symlink - which would point SQLite's writes at any file on the machine.
    # Refuse rather than resolve: nothing here is worth writing through a link.
    if path.is_symlink():
        raise RuntimeError(f"mirror path is a symlink - refused: {path}")
    # The mirror is a second storage backend on the project-local file; open it
    # through the same seam as the central write so both share one code path.
    backend = storage.LocalSqliteBackend(path)
    backend.open()
    try:
        # Same lock discipline as the central write, for the same reason:
        # parallel firings share this file, and a deferred transaction lets
        # get_or_create's SELECT-then-INSERT race - the losing writer's rows are
        # then dropped by the swallow-all-mirror-errors rule. Measured: 9/10
        # rows landing in 2 of 3 ten-way trials without this.
        backend.conn.execute("BEGIN IMMEDIATE")
        with backend.conn:
            backend.write_events(*args, **kwargs)
    finally:
        backend.close()


def find_project_root(cwd):
    """The session's own checkout root: the nearest ancestor of ``cwd`` that
    holds a ``.git`` entry (directory OR file), else ``cwd`` itself.

    Inside a linked git worktree this is the WORKTREE folder (its ``.git`` is a
    pointer file) — branch/sha and the commit-subject issue key are read from
    here. The project key is :func:`main_repo_root` of this, when it has one.

    :param cwd: the hook's working directory.
    :returns: a :class:`Path`.
    """
    p = Path(cwd)
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
    return p


# Cap on the two pointer files a linked worktree carries — its `.git` file
# (`gitdir: <path>`) and the gitdir's `commondir` — so a hostile or runaway file
# can never cost the hook path more than one small read. Git writes both as a
# single short line.
GITFILE_MAX_BYTES = 4096
# Claude Code creates its worktrees at `<main>/.claude/worktrees/<name>`. The
# prefix before the FIRST occurrence of this component is how the main repo's
# own sessions already spell the project path (F1), and it is the only rule the
# one-time fold can apply to a worktree folder that has since been deleted.
WORKTREE_COMPONENT = "/.claude/worktrees/"


def _read_pointer_line(path):
    """One bounded pointer line from ``path``, or None.

    Accepts only a regular file (a symlink is refused by ``O_NOFOLLOW``; a
    FIFO cannot block the hook thanks to ``O_NONBLOCK``; the ``fstat`` check
    then rejects anything not regular, with no lstat/open race) of at most
    :data:`GITFILE_MAX_BYTES`, UTF-8, holding exactly one non-empty line (one
    trailing newline allowed).

    :param path: the file to read.
    :returns: the line without its newline, or None on any problem.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        # One byte past the cap is enough to tell "too big" — the read itself
        # is the bound, so a file growing after an fstat cannot slip past it.
        data = os.read(fd, GITFILE_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > GITFILE_MAX_BYTES:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    if not text or "\n" in text or "\r" in text:
        return None
    return text


def main_repo_root(checkout):
    """The MAIN repository root of a linked git worktree, or None.

    Pure filesystem reads (no git subprocess — capture has a hard latency
    budget), every read bounded. Accepted only when ALL of these hold,
    otherwise None (the caller keeps today's behaviour — the checkout itself is
    the project):

    * ``<checkout>/.git`` is a regular file (not a symlink, not a directory) of
      at most :data:`GITFILE_MAX_BYTES` holding a single ``gitdir:`` line;
    * that gitdir, resolved, is a directory named ``<common>/worktrees/<id>``
      whose ``commondir`` file (same bound) resolves to ``<common>``;
    * ``<common>`` is a directory named ``.git``, and its parent is a directory.

    A submodule's ``.git`` file points at ``.git/modules/…`` and fails the
    ``worktrees/`` check, so a submodule stays its own project; a plain
    checkout (``.git`` directory), a bare repo and a non-git directory have no
    pointer file and return None.

    Spelling (the project key must match the main repo's own sessions): when
    ``checkout`` is ``<M>/.claude/worktrees/<…>`` and ``<M>`` resolves to the
    same directory, the result is ``<M>`` exactly as spelled; otherwise it is
    the realpath of ``<common>``'s parent.

    :param checkout: a checkout root, as :func:`find_project_root` returns it.
    :returns: a :class:`Path`, or None. Never raises.
    """
    try:
        checkout = Path(checkout)
        line = _read_pointer_line(checkout / ".git")
        if line is None or not line.startswith("gitdir:"):
            return None
        target = line[len("gitdir:"):].strip()
        if not target:
            return None
        gitdir = Path(os.path.realpath(os.path.join(checkout, target)))
        if not gitdir.is_dir() or gitdir.parent.name != "worktrees":
            return None
        common = gitdir.parent.parent
        common_line = _read_pointer_line(gitdir / "commondir")
        if common_line is None:
            return None
        resolved_common = os.path.realpath(
            os.path.join(gitdir, common_line.strip()))
        if resolved_common != str(common):
            return None
        if common.name != ".git" or not common.is_dir():
            return None
        main = common.parent
        if not main.is_dir():
            return None
        spelled = str(checkout)
        i = spelled.find(WORKTREE_COMPONENT)
        if i > 0 and os.path.realpath(spelled[:i]) == str(main):
            return Path(spelled[:i])
        return main
    except Exception:
        return None


def canonical_project_path(conn, path):
    """The stored spelling of the ``projects`` row for the directory ``path``
    names, so a worktree session never creates a duplicate row for its repo.

    An exact string match wins; otherwise the first row whose path is
    realpath-equal to ``path`` is returned with ITS stored spelling; with no
    such row, ``path`` itself (the caller's insert then creates it).

    :param conn: an open DB connection with a ``projects`` table.
    :param path: the candidate project path (string).
    :returns: the path string to key the project by.
    """
    path = str(path)
    if conn.execute("SELECT 1 FROM projects WHERE path=?",
                    (path,)).fetchone():
        return path
    real = os.path.realpath(path)
    for (stored,) in conn.execute("SELECT path FROM projects ORDER BY id"):
        if os.path.realpath(stored) == real:
            return stored
    return path


def is_enabled(cwd):
    """Whether this project opted in: a ``.claude/telemetry`` marker in
    ``cwd``, in the checkout root, or — inside a linked worktree — in the main
    repository root (the main repo's opt-in covers its worktrees).

    :param cwd: the hook's working directory.
    :returns: bool.
    """
    root = find_project_root(cwd)
    if ((Path(cwd) / ".claude" / "telemetry").exists()
            or (root / ".claude" / "telemetry").exists()):
        return True
    main = main_repo_root(root)
    return main is not None and (main / ".claude" / "telemetry").exists()


STORAGE_CENTRAL = "central"
STORAGE_PROJECT = "project"
MARKER_MAX_BYTES = 4096


def read_storage_mode(root):
    """The marker's first line selects storage: `project` adds the local mirror,
    anything else means central-only. Central is the default in every ambiguous
    case - absent, empty, oversized, undecodable or unrecognized content - so a
    marker written by an older version (empty file) keeps behaving as it did,
    and a corrupted one degrades to the mode that always works."""
    try:
        with open(Path(root) / ".claude" / "telemetry", "rb") as f:
            head = f.read(MARKER_MAX_BYTES)
        first = head.split(b"\n", 1)[0].decode("utf-8").strip().lower()
        return STORAGE_PROJECT if first == STORAGE_PROJECT else STORAGE_CENTRAL
    except Exception:
        return STORAGE_CENTRAL


def storage_mode_for(checkout, main=None):
    """Storage mode for a session: the checkout's own marker decides when it
    has one; inside a linked worktree without one, the main repo's marker
    does (mirrors :func:`is_enabled`'s dual check).

    :param checkout: the checkout root (:func:`find_project_root`).
    :param main: :func:`main_repo_root` of it, or None.
    :returns: :data:`STORAGE_PROJECT` or :data:`STORAGE_CENTRAL`.
    """
    if main is None or os.path.lexists(Path(checkout) / ".claude" / "telemetry"):
        return read_storage_mode(checkout)
    return read_storage_mode(main)


def mirror_db_path(root):
    return Path(root) / ".claude" / "telemetry-usage.db"


def git(cwd, *args):
    try:
        # A hostile repo's tracked .git/config can set core.fsmonitor or
        # core.hooksPath to run arbitrary programs when git invokes them.
        # These -c overrides beat repo config and neutralize that; do not
        # remove them. Every git call in this script goes through here.
        out = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=2)
        return out.stdout.strip() or None
    except Exception:
        return None


def git_meta(cwd):
    return (git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
            git(cwd, "rev-parse", "--short", "HEAD"))


# Anchored and closed by ':' so only a leading tracker key matches - single-letter
# project keys included ("A-1:"), while "feat:", "WIP: ...", "feat(AOS-42):" and
# any lowercase prefix do not.
ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]*-\d+):")


def issue_key_from_git(cwd):
    """Fallback when no sidecar is present: tracker keys lead commit subjects
    by convention, so the last commit names the task in flight."""
    subject = git(cwd, "log", "-1", "--format=%s")
    m = ISSUE_KEY_RE.match(subject) if subject else None
    return m.group(1) if m else None


PROJECT_INFO_MAX_BYTES = 65536
PROJECT_NAME_RE = re.compile(r"^project:\s*(.+?)\s*$", re.MULTILINE)


PROJECT_INFO_LOCATIONS = (".marvin", ".docs", "docs")


def project_name_from_kit(root):
    """The human project name from the agent-operating-kit's PROJECT-INFO
    frontmatter (`project:` key). Looks in a three-location ladder, in order:
    `.marvin/` (kit >=v0.21), `.docs/` (kit v0.15-0.20), `docs/` (kit <v0.15) —
    the kit's PROJECT-INFO.md has moved across versions, so a project on an
    older kit still resolves. The first location whose PROJECT-INFO.md exists
    wins; that file alone decides the result and never falls through to the
    next location, even if it turns out invalid. None when the kit is not
    installed, the file is unreadable/oversized, or the value is an
    unresolved {{PLACEHOLDER}} — a missing name is never worth failing or
    slowing a capture over. A repo-controlled symlink at any ladder location
    (e.g. `.marvin/PROJECT-INFO.md` -> /etc/passwd) must not read outside the
    repo root: each candidate is resolved and required to stay under the
    resolved root before it is opened; one that escapes is treated as invalid
    at that location, same as any other bad file — no fall-through."""
    try:
        root_resolved = Path(root).resolve()
        for loc in PROJECT_INFO_LOCATIONS:
            path = Path(root) / loc / "PROJECT-INFO.md"
            if path.is_file():
                resolved = path.resolve()
                try:
                    within_root = resolved.is_relative_to(root_resolved)
                except AttributeError:
                    try:
                        resolved.relative_to(root_resolved)
                        within_root = True
                    except ValueError:
                        within_root = False
                if not within_root:
                    return None
                if resolved.stat().st_size > PROJECT_INFO_MAX_BYTES:
                    return None
                m = PROJECT_NAME_RE.search(resolved.read_text(errors="replace"))
                if not m:
                    return None
                name = m.group(1).strip().strip("'\"")
                return name if name and "{{" not in name else None
        return None
    except Exception:
        return None


def stamp_project_name(conn, project, name):
    """Keep `projects.name` matched to the kit's source of truth. Overwrites a
    stale or enable-registered name when the kit document says otherwise; a DB
    whose v5 hop has not landed simply skips (never worth failing capture)."""
    if name and "name" in table_columns(conn, "projects"):
        with conn:
            conn.execute(
                "UPDATE projects SET name=? WHERE path=?"
                " AND COALESCE(name,'') <> ?",
                (name, str(project), name))


SIDECAR_MAX_BYTES = 65536


def read_sidecar(root):
    """`.claude/telemetry-context.json`, rewritten by the agent on task switch.
    Any problem (absent, unreadable, malformed, implausibly large) is a silent
    None - enrichment is never worth failing a capture over. The size check
    comes first: a runaway file is not agent-written context, and parsing it
    would cost time and memory on every hook firing."""
    try:
        path = Path(root) / ".claude" / "telemetry-context.json"
        if path.stat().st_size > SIDECAR_MAX_BYTES:
            return None
        with open(path) as f:
            ctx = json.load(f)
        return ctx if isinstance(ctx, dict) else None
    except Exception:
        return None


def sidecar_text(value):
    """Sidecar values are agent-written JSON and can be any type. Binding a
    dict/list raises sqlite3.ProgrammingError, which would take capture offline
    for as long as the bad file exists - so only scalars survive."""
    return str(value) if isinstance(value, (str, int, float)) else None


def log_error(context=None):
    """Always the CENTRAL telemetry directory, whatever the storage mode - one
    place to look. `context` names the failing step, since a swallowed mirror
    failure is otherwise indistinguishable from a failed capture."""
    try:
        log = db_path().parent / "error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            header = f"--- {datetime.now().isoformat()}"
            if context:
                header += f" {context}"
            f.write(f"{header}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def main():
    # No disk writes of any kind - including error.log - are allowed before
    # opt-in is positively established. `enabled` must be set before the
    # first statement that can raise, so a malformed-stdin or unresolvable-cwd
    # failure (which happens pre-gate) exits silently instead of logging.
    enabled = False
    try:
        hook = json.load(sys.stdin)
        cwd = hook.get("cwd") or os.getcwd()
        enabled = is_enabled(cwd)
        if not enabled:
            return
        transcript = hook.get("transcript_path")
        if not transcript or not os.path.exists(transcript):
            return
        # Enrichment shells out to git (up to ~2s per call) and has no
        # dependency on cursor/DB state, so all of it must run before the write
        # lock is taken below — otherwise it would hold that lock for the
        # duration of the subprocess calls, and a peer process's own BEGIN
        # IMMEDIATE could exceed connect()'s 5s busy-wait and drop its event.
        branch, sha = git_meta(cwd)
        # `root` is the session's own checkout; inside a linked worktree the
        # project is the MAIN repository (`main_root`), which keys the row,
        # names it, and owns the storage mode's mirror. Branch/sha and the
        # commit-subject issue key stay with the checkout (cwd) above.
        root = find_project_root(cwd)
        main_root = main_repo_root(root)
        key_root = main_root or root
        kit_name = project_name_from_kit(key_root)
        # The kit writes the sidecar into the checkout the session runs in;
        # a worktree without one falls back to the main repo's.
        ctx = (read_sidecar(root)
               or (read_sidecar(main_root) if main_root is not None else None)
               or {})
        issue_key = sidecar_text(ctx.get("issue_key")) or issue_key_from_git(cwd)
        # Read once, before the write lock: the central transaction stamps the
        # mirror metadata (below) and the same decision gates the mirror write.
        project_mode = storage_mode_for(root, main_root) == STORAGE_PROJECT
        # The owning user's uuid from central settings.json, read here (with the
        # rest of the enrichment, above the lock) so the hook path never blocks
        # on identity. None = no identity set yet = pre-identity; capture then
        # behaves byte-for-byte as before. read_settings() swallows every
        # settings-read error internally, so a malformed file never reaches here.
        # The one read is shared with the remote-backend gate below.
        settings_snapshot = settings.read_settings()
        owner_id = settings.current_owner_id(settings_snapshot)
        backend = storage.LocalSqliteBackend(db_path())
        backend.open()
        conn = backend.conn
        project = str(key_root)
        if main_root is not None:
            # Before the write lock: reuse the main repo's existing row under
            # its stored spelling (realpath-equal), never a duplicate.
            project = canonical_project_path(conn, project)
        mirror_batch = []
        try:
            # Take the write lock up front so concurrent hook firings on the
            # same transcript serialize instead of racing the cursor
            # read/aggregate/insert sequence (double-count or dropped events).
            # sqlite3.connect(..., timeout=5) in connect() busy-waits for the
            # lock; record()'s `with conn:` commits this transaction on exit.
            conn.execute("BEGIN IMMEDIATE")
            offset = backend.cursor_get(transcript)
            entries, new_offset = read_new_entries(transcript, offset)
            groups = aggregate(entries)
            # The MAIN transcript always holds main-loop work (kind 0): the
            # SubagentStop payload names an agent but points at the main
            # transcript — sub-agent usage lives in its own file and is swept
            # below. Old-harness inline sidechain lines still yield kind=1
            # groups via their side flag, labeled with the payload agent.
            agent = hook.get("agent_type") or hook.get("agent_name")
            meta = (branch, sha, issue_key, sidecar_text(ctx.get("size")),
                    sidecar_text(ctx.get("summary")))
            if groups or new_offset != offset:
                # Built once and shared with the mirror call below, so the two
                # writes cannot drift into recording different rows.
                event_args = (project, hook.get("session_id") or "unknown",
                              0, agent, groups)
                # Gated on `groups` like the mirror write: a turn with no usage
                # entries mirrors nothing, so there is no event timestamp to
                # stamp — `latest_event_ts` would fall back to `now` and record
                # a mirror that was never written for an event that does not
                # exist.
                record(conn, *event_args, transcript, new_offset, *meta,
                       mirror_path=(mirror_db_path(key_root)
                                    if project_mode and groups else None),
                       first_capture=(offset == 0), owner_id=owner_id)
                if groups:
                    mirror_batch.append(((*event_args, *meta), offset == 0))
            else:
                conn.rollback()
            stamp_project_name(conn, project, kit_name)
            for swept, fc in sweep_subagents(
                    conn, project, hook.get("session_id") or "unknown",
                    transcript, meta, agent, owner_id):
                mirror_batch.append(((*swept, *meta), fc))
        finally:
            backend.close()
        # Only now, with the central transaction committed and its connection
        # closed, is the project-local copy attempted - and any failure of it is
        # logged and dropped: the mirror exists for retention and portability,
        # never at the cost of the authoritative write or the session.
        if mirror_batch and project_mode:
            for args, fc in mirror_batch:
                try:
                    mirror_events(key_root, *args, first_capture=fc,
                                  owner_id=owner_id)
                except Exception:
                    log_error(
                        f"mirror write failed: {mirror_db_path(key_root)}")
        # Optional remote backend (AOS-104 P6, guarded). When — and ONLY when —
        # the active backend is `supabase`, ALSO push the rows just committed
        # centrally to the remote store, after the central commit and outside
        # every local lock, exactly like the mirror. The default `local` backend
        # makes `remote_backend_if_active` return None, so this whole block is a
        # single dict lookup and the local write path is byte-for-byte unchanged.
        # The backend swallows remote failures internally (retaining events in a
        # local outbox); the try/except here is defense in depth so a session is
        # never broken. The active-backend DISPATCH proper (routing the sole
        # write to the remote) is P8; this is the additive write-through only.
        if mirror_batch:
            remote = storage.remote_backend_if_active(settings_snapshot)
            if remote is not None:
                try:
                    remote.open()
                    for args, fc in mirror_batch:
                        remote.write_events(*args, first_capture=fc,
                                            owner_id=owner_id)
                except Exception:
                    log_error("remote write failed")
                finally:
                    try:
                        remote.close()
                    except Exception:
                        pass
    except Exception:
        if enabled:
            log_error()


if __name__ == "__main__":
    main()
    sys.exit(0)
