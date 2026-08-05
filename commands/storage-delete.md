---
description: Delete one project's telemetry from the central DB, with an export-first option and a typed confirmation
allowed-tools: Bash(python3:*), Bash(sqlite3:*), Bash(ls:*), Bash(cat:*), Read
---

Remove **one** project's rows from the central telemetry DB. Deletion is irreversible and
there is no undo in this plugin, so the export route is offered first and the plain route
takes a typed confirmation. **Nothing outside the chosen project is ever deleted.**

Central DB: `~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB` when set. If it does
not exist, say so and stop.

### 1. List and select

```sql
SELECT p.id, p.path, COUNT(e.rowid) AS events
FROM projects p
LEFT JOIN sessions s ON s.project_id = p.id
LEFT JOIN events e ON e.session_id = s.id
GROUP BY p.id, p.path ORDER BY events DESC;
```

Numbered table (`#`, id, project, events); ask which project — one per run. A project not
in the table stops the command.

### 2. Ask the route FIRST, before showing anything else

Two options, asked before any confirmation of the deletion itself:

- **Export first** (keep the data in its own file, then remove it centrally) — read and
  follow `${CLAUDE_PLUGIN_ROOT}/commands/storage-separate.md` from its step 2 with the
  project already chosen, and **stop when it finishes**. That flow already validates the export, writes the `export`
  audit row and offers the post-export deletion; do not delete anything here as well.
- **Plain delete** (the data is not wanted anywhere) — continue below.

### 3. Show exactly what will be deleted

```sql
SELECT
  (SELECT COUNT(*) FROM events WHERE session_id IN
     (SELECT id FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project))) AS events,
  (SELECT COUNT(*) FROM sessions
     WHERE project_id = (SELECT id FROM projects WHERE path = :project)) AS sessions,
  (SELECT COUNT(*) FROM cursors WHERE session_id IN
     (SELECT id FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project))) AS cursors,
  (SELECT MIN(date(ts,'unixepoch')) || ' .. ' || MAX(date(ts,'unixepoch')) FROM events
     WHERE session_id IN (SELECT id FROM sessions
                          WHERE project_id = (SELECT id FROM projects WHERE path = :project))) AS span;
```

Report the four numbers plainly, plus: this is the only copy unless a project-local
mirror exists (`projects.mirror_path` for that row — say where it is; this command never
reads, writes or deletes that file).

### 4. Extra confirmation — typed, not a yes

Ask the user to **type the project's basename** (the last path segment) to confirm.
Compare exactly. Anything else — a `yes`, a near miss, an empty answer — cancels the
command with nothing written.

### 5. Delete, transactionally, with the audit row inside the transaction

Run it with the project path passed as an argument, never interpolated into SQL — the
same `python3 - "<args>" <<'PY'` style `${CLAUDE_PLUGIN_ROOT}/commands/storage-separate.md`
step 3 uses:

```sql
BEGIN;
DELETE FROM events WHERE session_id IN
  (SELECT id FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project));
DELETE FROM cursors WHERE session_id IN
  (SELECT id FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project));
DELETE FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project);
DELETE FROM projects WHERE path = :project;
INSERT INTO audit_log(ts, action, project, detail)
VALUES (strftime('%s','now'), 'delete', :project, :detail);
COMMIT;
```

`detail` carries the counts from step 3, e.g. `3 events, 2 sessions, 2 cursors`. Two
separate guarantees, do not conflate them: the **order** (children before parents) means
no statement ever leaves a row pointing at a deleted parent, so the DB is consistent at
every step even to a concurrent reader; the **single transaction** means a failure
part-way rolls the whole thing back, so a half-deleted project cannot survive the
command. `models` and `pricing` are shared reference data — **never**
delete from them. The `audit_log` row outlives the project it describes; audit history is
never deleted here.

### 6. Report

Deleted counts, the audit row written, and two notes:

- Space is **not** reclaimed until someone runs `sqlite3 <central-db> 'VACUUM;'` — say
  it, never run it automatically (it rewrites the whole file and locks it meanwhile).
- If the project is still opted in, capture starts recording it again on the next turn
  from a fresh cursor — telemetry is not disabled by deleting data. Point at
  `/token-telemetry:disable` when that is what the user actually wanted.
