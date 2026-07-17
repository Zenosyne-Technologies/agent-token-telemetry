# Token Telemetry Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code plugin that captures per-turn and per-subagent token usage into a central SQLite DB via Stop/SubagentStop hooks (zero model-token overhead), with a `/token-stats` reporting command.

**Architecture:** Hooks pipe session JSON to `scripts/capture.py`, which incrementally parses the transcript JSONL (byte-offset cursor = dedup), aggregates `usage` fields per model, and inserts compact rows into `~/.claude/telemetry/usage.db` (lookup tables, WAL). Reporting is a slash command that runs prepared sqlite3 queries with an inline pricing map.

**Tech Stack:** Python 3 stdlib only (`sqlite3`, `json`), JSON plugin manifests, markdown slash commands, `unittest` for tests.

**Spec:** docs/superpowers/specs/2026-07-17-token-telemetry-plugin-design.md

## Global Constraints

- Capture path uses Python stdlib ONLY — no pip dependencies, ever.
- `capture.py` must ALWAYS exit 0; failures append to `~/.claude/telemetry/error.log`, never break a session.
- Opt-in gate: no `.claude/telemetry` marker at cwd or its git root → exit immediately, no DB writes.
- DB path resolves at runtime from `TOKEN_TELEMETRY_DB` env var, default `~/.claude/telemetry/usage.db` (env override exists for tests).
- Cost is never stored in the DB — derived at query time only.
- Installed files never reference plugin-cache paths; use `${CLAUDE_PLUGIN_ROOT}` in hooks.json.
- Commits: no AI attribution (repo policy in CLAUDE.md / .claude/settings.json).
- Run tests with: `python3 -m unittest tests.test_capture -v` from the repo root.

---

### Task 1: Plugin scaffold (manifest, marketplace, hooks wiring)

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `.claude-plugin/marketplace.json`
- Create: `hooks/hooks.json`

**Interfaces:**
- Produces: hook registration invoking `${CLAUDE_PLUGIN_ROOT}/scripts/capture.py` on `Stop` and `SubagentStop` (Task 4 provides that script; the path is fixed here).

- [ ] **Step 1: Write `.claude-plugin/plugin.json`**

```json
{
  "name": "token-telemetry",
  "version": "0.1.0",
  "description": "Zero-token-overhead token usage telemetry: Stop/SubagentStop hooks record per-turn usage into a central SQLite DB; /token-stats reports.",
  "author": { "name": "Zenosyne" }
}
```

- [ ] **Step 2: Write `.claude-plugin/marketplace.json`** (lets the repo be added as a local marketplace: `claude plugin marketplace add /Users/spike/Dev/agent-token-telemetry`)

```json
{
  "name": "agent-token-telemetry",
  "owner": { "name": "Zenosyne" },
  "plugins": [
    {
      "name": "token-telemetry",
      "source": "./",
      "description": "Token usage telemetry for Claude Code sessions"
    }
  ]
}
```

- [ ] **Step 3: Write `hooks/hooks.json`** (plugin wrapper format — `hooks` key is required)

```json
{
  "description": "Capture per-turn and per-subagent token usage into the central telemetry DB",
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/capture.py\"",
            "timeout": 10
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/capture.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 4: Validate all three JSON files**

Run: `python3 -m json.tool .claude-plugin/plugin.json && python3 -m json.tool .claude-plugin/marketplace.json && python3 -m json.tool hooks/hooks.json`
Expected: each file echoed back pretty-printed, exit 0.

- [ ] **Step 5: Commit**

```bash
git add .claude-plugin hooks
git commit -m "Scaffold token-telemetry plugin: manifest, marketplace, hook wiring"
```

---

### Task 2: Transcript parsing and aggregation (`capture.py` core)

**Files:**
- Create: `scripts/capture.py` (parsing half)
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: `read_new_entries(path, offset) -> (list[dict], int)` — parses complete JSONL lines from byte `offset`, returns entries + new offset; never consumes a partial trailing line. `aggregate(entries) -> dict[(model, sidechain), dict]` — sums usage per (model, isSidechain) group with keys `in`, `out`, `cr`, `cw`, `first`, `last` (epoch floats or None). `parse_ts(iso_str) -> float`.

- [ ] **Step 1: Write the failing tests**

Create empty `tests/__init__.py`, then `tests/test_capture.py`:

```python
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture


def entry(model="claude-sonnet-5", inp=100, out=50, cr=10, cw=5,
          side=False, ts="2026-07-17T10:00:00.000Z", typ="assistant"):
    return {
        "type": typ,
        "isSidechain": side,
        "timestamp": ts,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw,
        }},
    }


def write_jsonl(path, entries, trailing_partial=None):
    with open(path, "wb") as f:
        for e in entries:
            f.write(json.dumps(e).encode() + b"\n")
        if trailing_partial is not None:
            f.write(trailing_partial)


class TestReadNewEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_all_from_zero(self):
        write_jsonl(self.path, [entry(), entry()])
        entries, offset = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(offset, self.path.stat().st_size)

    def test_incremental_read_from_offset(self):
        write_jsonl(self.path, [entry()])
        _, offset1 = capture.read_new_entries(self.path, 0)
        with open(self.path, "ab") as f:
            f.write(json.dumps(entry(inp=7)).encode() + b"\n")
        entries, offset2 = capture.read_new_entries(self.path, offset1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"]["usage"]["input_tokens"], 7)
        self.assertEqual(offset2, self.path.stat().st_size)

    def test_partial_trailing_line_not_consumed(self):
        write_jsonl(self.path, [entry()], trailing_partial=b'{"type":"assist')
        entries, offset = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 1)
        # offset stops after the last complete line
        self.assertEqual(offset, self.path.stat().st_size - len(b'{"type":"assist'))

    def test_malformed_lines_skipped(self):
        with open(self.path, "wb") as f:
            f.write(b"not json at all\n")
            f.write(json.dumps(entry()).encode() + b"\n")
        entries, _ = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 1)


class TestAggregate(unittest.TestCase):
    def test_sums_single_model(self):
        groups = capture.aggregate([entry(inp=100, out=50), entry(inp=10, out=5)])
        g = groups[("claude-sonnet-5", 0)]
        self.assertEqual(g["in"], 110)
        self.assertEqual(g["out"], 55)
        self.assertEqual(g["cr"], 20)
        self.assertEqual(g["cw"], 10)

    def test_groups_by_model_and_sidechain(self):
        groups = capture.aggregate([
            entry(model="claude-sonnet-5"),
            entry(model="claude-haiku-4-5", side=True),
        ])
        self.assertIn(("claude-sonnet-5", 0), groups)
        self.assertIn(("claude-haiku-4-5", 1), groups)

    def test_non_assistant_and_no_usage_skipped(self):
        no_usage = {"type": "assistant", "message": {"model": "m"}}
        groups = capture.aggregate([entry(typ="user"), no_usage])
        self.assertEqual(groups, {})

    def test_first_last_timestamps(self):
        groups = capture.aggregate([
            entry(ts="2026-07-17T10:00:00.000Z"),
            entry(ts="2026-07-17T10:00:30.000Z"),
        ])
        g = groups[("claude-sonnet-5", 0)]
        self.assertAlmostEqual(g["last"] - g["first"], 30.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_capture -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'capture'`.

- [ ] **Step 3: Write the parsing half of `scripts/capture.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_capture -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/capture.py tests/
git commit -m "Add transcript parsing and usage aggregation to capture script"
```

---

### Task 3: SQLite layer (schema, lookups, cursor, event insert)

**Files:**
- Modify: `scripts/capture.py` (append DB half)
- Test: `tests/test_capture.py` (append `TestDbLayer`)

**Interfaces:**
- Consumes: `aggregate()` group dicts from Task 2.
- Produces: `connect(path) -> sqlite3.Connection` (creates parent dir + schema, WAL). `get_or_create(conn, table, column, value) -> int`. `get_offset(conn, transcript) -> int`. `record(conn, project, session_uuid, kind_hint, agent, groups, transcript, new_offset) -> None` — one transaction: inserts one `events` row per group and upserts the cursor. `kind_hint` is 1 for SubagentStop, 0 for Stop; a row's `kind` is 1 if `kind_hint` or the group's sidechain flag is set.

- [ ] **Step 1: Append failing tests to `tests/test_capture.py`**

```python
class TestDbLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "sub" / "usage.db"
        self.conn = capture.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_created_and_idempotent(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"projects", "models", "sessions", "events", "cursors"}, tables)
        capture.connect(self.db).close()  # second connect must not raise

    def test_get_or_create_dedupes(self):
        a = capture.get_or_create(self.conn, "models", "name", "claude-sonnet-5")
        b = capture.get_or_create(self.conn, "models", "name", "claude-sonnet-5")
        self.assertEqual(a, b)

    def test_record_inserts_events_and_cursor(self):
        groups = capture.aggregate([entry(), entry(model="claude-haiku-4-5", side=True)])
        capture.record(self.conn, "/proj", "sess-1", 0, None, groups, "/t.jsonl", 500)
        rows = self.conn.execute(
            "SELECT kind, in_tok, out_tok FROM events ORDER BY kind").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 0)   # main turn
        self.assertEqual(rows[1][0], 1)   # sidechain -> subagent
        self.assertEqual(capture.get_offset(self.conn, "/t.jsonl"), 500)

    def test_cursor_upsert_advances(self):
        groups = capture.aggregate([entry()])
        capture.record(self.conn, "/proj", "sess-1", 0, None, groups, "/t.jsonl", 100)
        capture.record(self.conn, "/proj", "sess-1", 0, None, groups, "/t.jsonl", 200)
        self.assertEqual(capture.get_offset(self.conn, "/t.jsonl"), 200)

    def test_unknown_transcript_offset_is_zero(self):
        self.assertEqual(capture.get_offset(self.conn, "/never-seen.jsonl"), 0)

    def test_subagent_kind_hint(self):
        groups = capture.aggregate([entry()])  # not sidechain
        capture.record(self.conn, "/proj", "sess-1", 1, "explorer", groups, "/t2.jsonl", 50)
        kind, agent = self.conn.execute(
            "SELECT kind, agent FROM events WHERE session_id="
            "(SELECT id FROM sessions WHERE uuid='sess-1') ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(kind, 1)
        self.assertEqual(agent, "explorer")
```

- [ ] **Step 2: Run tests to verify the new class fails**

Run: `python3 -m unittest tests.test_capture.TestDbLayer -v`
Expected: FAIL — `AttributeError: module 'capture' has no attribute 'connect'`.

- [ ] **Step 3: Append the DB half to `scripts/capture.py`**

```python
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
    conn.execute("PRAGMA journal_mode=WAL")
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
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest tests.test_capture -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/capture.py tests/test_capture.py
git commit -m "Add SQLite layer: schema, lookup tables, cursor upsert, event insert"
```

---

### Task 4: Hook entrypoint (`main`), opt-in gate, git meta, error logging

**Files:**
- Modify: `scripts/capture.py` (append entrypoint half; make executable)
- Test: `tests/test_capture.py` (append `TestMain`)

**Interfaces:**
- Consumes: everything from Tasks 2–3.
- Produces: `find_project_root(cwd) -> Path` (nearest ancestor with `.git`, else cwd). `is_enabled(cwd) -> bool` (`.claude/telemetry` marker at cwd or its project root). `git_meta(cwd) -> (branch|None, short_sha|None)`. `main() -> None` (reads hook JSON from `sys.stdin`, never raises).

- [ ] **Step 1: Append failing tests to `tests/test_capture.py`**

```python
import io
import os
import sqlite3 as _sqlite3


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.proj = self.root / "proj"
        (self.proj / ".claude").mkdir(parents=True)
        self.transcript = self.root / "sess.jsonl"
        self.db = self.root / "telemetry" / "usage.db"
        os.environ["TOKEN_TELEMETRY_DB"] = str(self.db)
        self._stdin = sys.stdin

    def tearDown(self):
        sys.stdin = self._stdin
        os.environ.pop("TOKEN_TELEMETRY_DB", None)
        self.tmp.cleanup()

    def run_main(self, session_id="sess-1", event="Stop"):
        sys.stdin = io.StringIO(json.dumps({
            "session_id": session_id,
            "transcript_path": str(self.transcript),
            "cwd": str(self.proj),
            "hook_event_name": event,
        }))
        capture.main()

    def count_events(self):
        if not self.db.exists():
            return 0
        conn = _sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()

    def test_no_marker_no_db(self):
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertFalse(self.db.exists())

    def test_marker_records_event(self):
        (self.proj / ".claude" / "telemetry").touch()
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.assertEqual(self.count_events(), 1)

    def test_refire_is_noop(self):
        (self.proj / ".claude" / "telemetry").touch()
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        self.run_main()  # nothing new appended
        self.assertEqual(self.count_events(), 1)

    def test_incremental_second_turn(self):
        (self.proj / ".claude" / "telemetry").touch()
        write_jsonl(self.transcript, [entry()])
        self.run_main()
        with open(self.transcript, "ab") as f:
            f.write(json.dumps(entry(inp=1)).encode() + b"\n")
        self.run_main()
        self.assertEqual(self.count_events(), 2)

    def test_malformed_stdin_never_raises(self):
        sys.stdin = io.StringIO("this is not json")
        capture.main()  # must not raise

    def test_missing_transcript_never_raises(self):
        (self.proj / ".claude" / "telemetry").touch()
        sys.stdin = io.StringIO(json.dumps({
            "session_id": "s", "transcript_path": str(self.root / "nope.jsonl"),
            "cwd": str(self.proj), "hook_event_name": "Stop",
        }))
        capture.main()  # must not raise
        self.assertEqual(self.count_events(), 0)
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `python3 -m unittest tests.test_capture.TestMain -v`
Expected: FAIL — `AttributeError: module 'capture' has no attribute 'main'`.

- [ ] **Step 3: Append the entrypoint half to `scripts/capture.py`**

```python
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
            out = subprocess.run(["git", "-C", str(cwd), *args],
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
        conn = connect(db_path())
        try:
            offset = get_offset(conn, transcript)
            entries, new_offset = read_new_entries(transcript, offset)
            groups = aggregate(entries)
            if not groups and new_offset == offset:
                return
            branch, sha = git_meta(cwd)
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
```

- [ ] **Step 4: Run the full suite and make the script executable**

Run: `python3 -m unittest tests.test_capture -v` — Expected: all PASS.
Run: `chmod +x scripts/capture.py`

- [ ] **Step 5: Smoke-test as the hook will invoke it**

```bash
echo '{"session_id":"smoke","transcript_path":"/nonexistent","cwd":"'$PWD'","hook_event_name":"Stop"}' | python3 scripts/capture.py; echo "exit: $?"
```
Expected: `exit: 0` (no marker in this repo yet → silent no-op).

- [ ] **Step 6: Commit**

```bash
git add scripts/capture.py tests/test_capture.py
git commit -m "Add hook entrypoint: opt-in gate, git meta, error logging, always-exit-0"
```

---

### Task 5: Slash commands (enable, disable, token-stats)

**Files:**
- Create: `commands/enable.md`
- Create: `commands/disable.md`
- Create: `commands/token-stats.md`

**Interfaces:**
- Consumes: DB schema from Task 3 (query column names must match exactly: `events.ts/kind/agent/model_id/in_tok/out_tok/cache_r/cache_w`, `models.name`, `projects.path`, `sessions.project_id`).

- [ ] **Step 1: Write `commands/enable.md`**

```markdown
---
description: Enable token telemetry capture for the current project
---

Enable token telemetry for this project:

1. Find the project root: the git root of the current directory, else the current directory.
2. Run: `mkdir -p <root>/.claude && touch <root>/.claude/telemetry`
3. Tell the user: telemetry is enabled for this project. Every completed turn and subagent will be recorded to `~/.claude/telemetry/usage.db` (no tokens are consumed by capture). The marker file can be committed to enable it for the whole team. Use `/token-telemetry:disable` to turn it off.
```

- [ ] **Step 2: Write `commands/disable.md`**

```markdown
---
description: Disable token telemetry capture for the current project
---

Disable token telemetry for this project:

1. Find the project root: the git root of the current directory, else the current directory.
2. Run: `rm -f <root>/.claude/telemetry` (also check `./.claude/telemetry` if cwd differs from root).
3. Tell the user: telemetry capture is disabled for this project. Existing recorded data in `~/.claude/telemetry/usage.db` is untouched.
```

- [ ] **Step 3: Write `commands/token-stats.md`**

````markdown
---
description: Show token usage and cost statistics from the telemetry DB
allowed-tools: Bash(sqlite3:*)
---

Report token usage from `~/.claude/telemetry/usage.db`. If the file does not exist, tell the user no telemetry has been recorded yet (enable with `/token-telemetry:enable`) and stop.

Run these queries with `sqlite3 -header -column ~/.claude/telemetry/usage.db "<SQL>"`:

Totals (today and last 7 days):

```sql
SELECT CASE WHEN ts >= strftime('%s','now','start of day') THEN 'today' ELSE 'last 7d' END AS period,
       SUM(in_tok) AS input, SUM(out_tok) AS output,
       SUM(cache_r) AS cache_read, SUM(cache_w) AS cache_write,
       COUNT(*) AS events
FROM events WHERE ts >= strftime('%s','now','-7 days')
GROUP BY period ORDER BY period DESC;
```

By project (last 7 days):

```sql
SELECT p.path, SUM(e.in_tok) AS input, SUM(e.out_tok) AS output,
       SUM(e.cache_r) AS cache_read, COUNT(*) AS events
FROM events e JOIN sessions s ON s.id = e.session_id
JOIN projects p ON p.id = s.project_id
WHERE e.ts >= strftime('%s','now','-7 days')
GROUP BY p.path ORDER BY output DESC;
```

By model (last 7 days):

```sql
SELECT m.name, SUM(e.in_tok) AS input, SUM(e.out_tok) AS output,
       SUM(e.cache_r) AS cache_read, SUM(e.cache_w) AS cache_write
FROM events e JOIN models m ON m.id = e.model_id
WHERE e.ts >= strftime('%s','now','-7 days')
GROUP BY m.name ORDER BY output DESC;
```

Main vs subagent split and cache hit rate (last 7 days):

```sql
SELECT CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END AS kind,
       SUM(in_tok) AS input, SUM(out_tok) AS output,
       ROUND(100.0 * SUM(cache_r) / NULLIF(SUM(in_tok) + SUM(cache_r), 0), 1) AS cache_hit_pct
FROM events WHERE ts >= strftime('%s','now','-7 days') GROUP BY kind;
```

Then estimate cost for the by-model results using this pricing map (USD per million tokens; cache read ≈ 0.1× input, cache write ≈ 1.25× input). These are estimates — flag unknown models as unpriced:

| model prefix | input | output | cache read | cache write |
|---|---|---|---|---|
| claude-fable-5 | 10.00 | 50.00 | 1.00 | 12.50 |
| claude-opus-4 (any) | 5.00 | 25.00 | 0.50 | 6.25 |
| claude-sonnet | 3.00 | 15.00 | 0.30 | 3.75 |
| claude-haiku | 1.00 | 5.00 | 0.10 | 1.25 |

Cost per model = (input × in$ + output × out$ + cache_read × cr$ + cache_write × cw$) / 1,000,000.

Present: a short headline (today's totals + estimated 7-day cost), then the breakdown tables. Keep it compact.
````

- [ ] **Step 4: Verify the SQL against a scratch DB**

```bash
TOKEN_TELEMETRY_DB=/tmp/tt-scratch.db python3 - <<'EOF'
import sys; sys.path.insert(0, "scripts")
import capture, time
conn = capture.connect(capture.db_path())
groups = {("claude-sonnet-5", 0): {"in": 1000, "out": 500, "cr": 200, "cw": 100,
                                    "first": time.time() - 5, "last": time.time()}}
capture.record(conn, "/tmp/proj", "verify-sess", 0, None, groups, "/tmp/t.jsonl", 10)
conn.close()
EOF
sqlite3 -header -column /tmp/tt-scratch.db "SELECT m.name, SUM(e.in_tok) AS input, SUM(e.out_tok) AS output FROM events e JOIN models m ON m.id=e.model_id GROUP BY m.name;"
rm -f /tmp/tt-scratch.db*
```
Expected: one row, `claude-sonnet-5 | 1000 | 500`. Run each of the four command queries the same way; all must execute without error.

- [ ] **Step 5: Commit**

```bash
git add commands
git commit -m "Add enable/disable/token-stats slash commands with pricing map"
```

---

### Task 6: README, project docs, and real-session verification

**Files:**
- Create: `README.md`
- Modify: `CLAUDE.md` (facts paragraph: stack is now known)

**Interfaces:**
- Consumes: everything; documents the installed surface.

- [ ] **Step 1: Write `README.md`**

```markdown
# token-telemetry

Claude Code plugin that records per-turn and per-subagent token usage into a
central SQLite database — with **zero model-token overhead** (capture runs in
Stop/SubagentStop hooks, outside the model loop).

## Install

```
claude plugin marketplace add /path/to/agent-token-telemetry
claude plugin install token-telemetry@agent-token-telemetry
```

Restart Claude Code (hooks load at session start).

## Use

- `/token-telemetry:enable` — opt this project in (creates `.claude/telemetry`
  marker; committable for team-wide opt-in)
- `/token-telemetry:disable` — opt out
- `/token-telemetry:token-stats` — totals, per-project/model/agent breakdown,
  cache hit rate, cost estimates

Data lives in `~/.claude/telemetry/usage.db` (SQLite, WAL). Query it directly
with sqlite3/DuckDB/Grafana. Capture errors go to `~/.claude/telemetry/error.log`
and never break a session. Cost is never stored — it is derived at query time
from the pricing map in `commands/token-stats.md`.

## Design

See `docs/superpowers/specs/2026-07-17-token-telemetry-plugin-design.md`.

## Tests

```
python3 -m unittest tests.test_capture -v
```
```

- [ ] **Step 2: Update the CLAUDE.md facts paragraph**

In `CLAUDE.md`, replace the first sentence of the facts paragraph ("Greenfield repo (stack not yet chosen — record it here on first commit).") with: "Claude Code plugin (`token-telemetry`): Python 3 stdlib capture script (`scripts/capture.py`), JSON manifests, markdown slash commands; tests via `python3 -m unittest tests.test_capture -v`."

- [ ] **Step 3: Run the full test suite one final time**

Run: `python3 -m unittest tests.test_capture -v`
Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Add README and record stack in CLAUDE.md"
```

- [ ] **Step 5: Real-session E2E (requires user)**

This cannot be verified from inside the implementing session (hooks load at session start). Instruct the user:

1. `claude plugin marketplace add /Users/spike/Dev/agent-token-telemetry`
2. `claude plugin install token-telemetry@agent-token-telemetry`
3. In this repo, run `/token-telemetry:enable`, restart Claude Code, send any message, wait for the turn to finish.
4. Verify: `sqlite3 ~/.claude/telemetry/usage.db "SELECT COUNT(*) FROM events;"` returns ≥ 1, and `/token-telemetry:token-stats` renders.
5. Check `~/.claude/telemetry/error.log` does not exist (or is empty).

Record the outcome on the tracker issue ZEN-120 per `docs/agents/ticket-filing.md`.
