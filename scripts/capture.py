#!/usr/bin/env python3
"""Token telemetry capture hook for Claude Code (Stop / SubagentStop).

Stdlib only. Reads hook JSON on stdin, appends compact usage rows to the
central SQLite DB. Must never break a session: always exits 0.
"""
import json
import os
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
           transcript, new_offset, branch=None, commit_sha=None):
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
                " in_tok, out_tok, cache_r, cache_w, dur_ms, branch, commit_sha)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, session_id, kind, agent, model_id,
                 g["in"], g["out"], g["cr"], g["cw"], dur, branch, commit_sha))
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


def git_meta(cwd):
    def run(*args):
        try:
            # A hostile repo's tracked .git/config can set core.fsmonitor or
            # core.hooksPath to run arbitrary programs when git invokes them.
            # These -c overrides beat repo config and neutralize that; do not
            # remove them.
            out = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                 "-C", str(cwd), *args],
                capture_output=True, text=True, timeout=2)
            return out.stdout.strip() or None
        except Exception:
            return None
    return run("rev-parse", "--abbrev-ref", "HEAD"), run("rev-parse", "--short", "HEAD")


def log_error():
    try:
        log = db_path().parent / "error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(f"--- {datetime.now().isoformat()}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def main():
    try:
        hook = json.load(sys.stdin)
        cwd = hook.get("cwd") or os.getcwd()
        if not is_enabled(cwd):
            return
        transcript = hook.get("transcript_path")
        if not transcript or not os.path.exists(transcript):
            return
        # git_meta() shells out to git (up to ~4s across two calls) and has no
        # dependency on cursor/DB state, so it must run before the write lock
        # is taken below — otherwise it would hold that lock for the duration
        # of the subprocess calls, and a peer process's own BEGIN IMMEDIATE
        # could exceed connect()'s 5s busy-wait and drop its event.
        branch, sha = git_meta(cwd)
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
            record(conn, str(find_project_root(cwd)),
                   hook.get("session_id") or "unknown", kind_hint, agent,
                   groups, transcript, new_offset, branch, sha)
        finally:
            conn.close()
    except Exception:
        log_error()


if __name__ == "__main__":
    main()
    sys.exit(0)
