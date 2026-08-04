#!/usr/bin/env python3
"""Token telemetry capture hook for Claude Code (Stop / SubagentStop).

Stdlib only. Reads hook JSON on stdin, appends compact usage rows to the
central SQLite DB. Must never break a session: always exits 0.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path


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
    activity recorded in the same transcript."""
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
            "in": 0, "out": 0, "cr": 0, "cw": 0, "first": None, "last": None,
        })
        g["in"] += usage.get("input_tokens") or 0
        g["out"] += usage.get("output_tokens") or 0
        g["cr"] += usage.get("cache_read_input_tokens") or 0
        g["cw"] += usage.get("cache_creation_input_tokens") or 0
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
  effective_from INTEGER NOT NULL,
  source         TEXT,
  UNIQUE(provider, model_prefix, model_version, effective_from));
"""

# USD per 1M tokens: input, output, cache read, cache write. Prefixes (not full
# model names) so a new dated release prices correctly on longest-prefix match.
# effective_from 0: the seed applies to all history until a dated row supersedes it.
PRICING_SEED = [
    ("anthropic", "claude-fable-", 10.0, 50.0, 1.00, 12.50),
    ("anthropic", "claude-opus-", 5.0, 25.0, 0.50, 6.25),
    ("anthropic", "claude-sonnet-", 3.0, 15.0, 0.30, 3.75),
    ("anthropic", "claude-haiku-", 1.0, 5.0, 0.10, 1.25),
]
SEED_SOURCE = "seed-v0.2.0"

V2_COLUMNS = ("issue_key", "task_size", "note")


def migrate(conn):
    """SCHEMA is the v1 baseline; later versions are deltas applied here.
    Every statement is individually idempotent rather than wrapped in one
    transaction, so a migrating process never blocks a peer's capture."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    for col in V2_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # duplicate column (peer process) - or a transient failure,
            # which the post-condition check below catches
    # Never stamp a version the schema does not actually have: the except above
    # cannot tell "already added" from "database is locked"/"disk full", and a
    # premature stamp would strand the DB without the columns forever.
    if not set(V2_COLUMNS) <= {r[1] for r in conn.execute("PRAGMA table_info(events)")}:
        return  # next connect retries
    conn.executescript(PRICING_SCHEMA)
    # Gate on the seed rows, not the table: CREATE TABLE autocommits, so a
    # failure before the INSERT can leave an empty table that must still get
    # seeded. OR IGNORE covers two processes seeding concurrently.
    if not conn.execute("SELECT 1 FROM pricing WHERE source=? LIMIT 1",
                        (SEED_SOURCE,)).fetchone():
        conn.executemany(
            "INSERT OR IGNORE INTO pricing(provider, model_prefix, in_usd,"
            " out_usd, cache_r_usd, cache_w_usd, effective_from, source)"
            " VALUES (?,?,?,?,?,?,0,?)",
            [(*row, SEED_SOURCE) for row in PRICING_SEED])
    # Commit the data before stamping the version: a crash in between leaves
    # user_version < 2, and the next connect simply migrates again. Also leaves
    # no open transaction for main()'s BEGIN IMMEDIATE to trip over.
    conn.commit()
    conn.execute("PRAGMA user_version=2")


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


def record(conn, project, session_uuid, kind_hint, agent, groups,
           transcript, new_offset, branch=None, commit_sha=None,
           issue_key=None, task_size=None, note=None):
    with conn:
        project_id = get_or_create(conn, "projects", "path", project)
        row = conn.execute(
            "SELECT id FROM sessions WHERE uuid=?", (session_uuid,)).fetchone()
        session_id = row[0] if row else conn.execute(
            "INSERT INTO sessions(uuid, project_id) VALUES (?,?)",
            (session_uuid, project_id)).lastrowid
        for (model, side), g in groups.items():
            model_id = get_or_create(conn, "models", "name", model)
            kind = 1 if (kind_hint or side) else 0
            dur = (int((g["last"] - g["first"]) * 1000)
                   if g["first"] is not None else None)
            ts = int(g["last"]) if g["last"] is not None else int(time.time())
            conn.execute(
                "INSERT INTO events(ts, session_id, kind, agent, model_id,"
                " in_tok, out_tok, cache_r, cache_w, dur_ms, branch, commit_sha,"
                " issue_key, task_size, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, session_id, kind, agent, model_id,
                 g["in"], g["out"], g["cr"], g["cw"], dur, branch, commit_sha,
                 issue_key, task_size, note))
        conn.execute(
            "INSERT INTO cursors(transcript, offset, session_id) VALUES (?,?,?)"
            " ON CONFLICT(transcript) DO UPDATE SET offset=excluded.offset",
            (str(transcript), new_offset, session_id))


def find_project_root(cwd):
    p = Path(cwd)
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
    return p


def is_enabled(cwd):
    root = find_project_root(cwd)
    return ((Path(cwd) / ".claude" / "telemetry").exists()
            or (root / ".claude" / "telemetry").exists())


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


ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]+-\d+):")


def issue_key_from_git(cwd):
    """Fallback when no sidecar is present: tracker keys lead commit subjects
    by convention, so the last commit names the task in flight."""
    subject = git(cwd, "log", "-1", "--format=%s")
    m = ISSUE_KEY_RE.match(subject) if subject else None
    return m.group(1) if m else None


def read_sidecar(root):
    """`.claude/telemetry-context.json`, rewritten by the agent on task switch.
    Any problem (absent, unreadable, malformed) is a silent None - enrichment
    is never worth failing a capture over."""
    try:
        with open(Path(root) / ".claude" / "telemetry-context.json") as f:
            ctx = json.load(f)
        return ctx if isinstance(ctx, dict) else None
    except Exception:
        return None


def sidecar_text(value):
    """Sidecar values are agent-written JSON and can be any type. Binding a
    dict/list raises sqlite3.ProgrammingError, which would take capture offline
    for as long as the bad file exists - so only scalars survive."""
    return str(value) if isinstance(value, (str, int, float)) else None


def log_error():
    try:
        log = db_path().parent / "error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(f"--- {datetime.now().isoformat()}\n{traceback.format_exc()}\n")
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
        root = find_project_root(cwd)
        ctx = read_sidecar(root) or {}
        issue_key = sidecar_text(ctx.get("issue_key")) or issue_key_from_git(cwd)
        conn = connect(db_path())
        try:
            # Take the write lock up front so concurrent hook firings on the
            # same transcript serialize instead of racing the cursor
            # read/aggregate/insert sequence (double-count or dropped events).
            # sqlite3.connect(..., timeout=5) in connect() busy-waits for the
            # lock; record()'s `with conn:` commits this transaction on exit.
            conn.execute("BEGIN IMMEDIATE")
            offset = get_offset(conn, transcript)
            entries, new_offset = read_new_entries(transcript, offset)
            groups = aggregate(entries)
            if not groups and new_offset == offset:
                conn.rollback()
                return
            kind_hint = 1 if hook.get("hook_event_name") == "SubagentStop" else 0
            agent = hook.get("agent_type") or hook.get("agent_name")
            record(conn, str(root),
                   hook.get("session_id") or "unknown", kind_hint, agent,
                   groups, transcript, new_offset, branch, sha,
                   issue_key, sidecar_text(ctx.get("size")),
                   sidecar_text(ctx.get("summary")))
        finally:
            conn.close()
    except Exception:
        if enabled:
            log_error()


if __name__ == "__main__":
    main()
    sys.exit(0)
