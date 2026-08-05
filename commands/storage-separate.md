---
description: Export one project's telemetry into its own SQLite file, then optionally remove it from the central DB
allowed-tools: Bash(python3:*), Bash(sqlite3:*), Bash(ls:*), Bash(cat:*), Read
---

Carve **one** project out of the central telemetry DB into a self-contained file, and
only then offer to remove it centrally. Interactive and stepwise: the user picks the
project, sees validated counts, and confirms the deletion separately. **Nothing outside
the chosen project is ever read into the export or deleted from the central DB** — except
the shared `models` and `pricing` reference tables, which are copied (never deleted) so
the export can price itself standalone.

Central DB: `~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB` when set. If it does
not exist, say so and stop.

### 1. List the projects

```sql
SELECT p.id, p.path, COUNT(e.rowid) AS events
FROM projects p
LEFT JOIN sessions s ON s.project_id = p.id
LEFT JOIN events e ON e.session_id = s.id
GROUP BY p.id, p.path ORDER BY events DESC;
```

Render as a numbered table (`#`, id, project, events) and ask which one to carve out —
one project per run. If the user names a project that is not in the table, stop.

### 2. Pick the export path

`<central-dir>/<slug>-<YYYY-MM-DD>.db`, where `<slug>` is the **basename** of the project
path, kebab-cased: lowercased, every run of non-alphanumeric characters replaced by `-`,
leading/trailing `-` stripped (empty result → `project`). Date is today, `date +%F`.

**Refuse to write over an existing file.** If the name is taken, append `-2`, then `-3`,
until free — a same-day second export must never overwrite the first. Check with `ls -l`
before writing and report the final path.

### 3. Export

Run exactly this — the export DB is created through the plugin's own
`connect()`/`migrate()`, so it lands on the current schema (`user_version = 3`) with the
pricing seed, and every value is passed as an argument, never interpolated into SQL:

```bash
python3 - "$CLAUDE_PLUGIN_ROOT" "<central-db>" "<export-db>" "<project-path>" <<'PY'
import os, sys
plugin, central, export, project = sys.argv[1:5]
# Hard refusal, not just the step-2 name check: connect() would happily open an
# existing DB and add a second project's rows to it, silently turning someone
# else's export into a two-project file.
if os.path.exists(export):
    sys.exit(f"export path exists, refusing: {export}")
sys.path.insert(0, plugin + "/scripts")
import capture

EVENTS = ("ts, session_id, kind, agent, model_id, in_tok, out_tok, cache_r,"
          " cache_w, dur_ms, branch, commit_sha, issue_key, task_size, note")
PRICING = ("provider, model_prefix, model_version, in_usd, out_usd,"
           " cache_r_usd, cache_w_usd, effective_from, source")
PID = "(SELECT id FROM src.projects WHERE path = ?)"
SESSIONS = f"(SELECT id FROM src.sessions WHERE project_id = {PID})"

conn = capture.connect(export)
conn.execute("ATTACH DATABASE ? AS src", (central,))
with conn:
    # Full reference tables: the export must price itself with no central DB.
    conn.execute("INSERT OR IGNORE INTO models(id, name) SELECT id, name FROM src.models")
    conn.execute(f"INSERT OR IGNORE INTO pricing({PRICING}) SELECT {PRICING} FROM src.pricing")
    # ids are copied verbatim so events/sessions keep their foreign keys.
    conn.execute("INSERT INTO projects(id, path, mirror_path, mirror_last_at)"
                 " SELECT id, path, mirror_path, mirror_last_at"
                 " FROM src.projects WHERE path = ?", (project,))
    conn.execute("INSERT INTO sessions(id, uuid, project_id)"
                 f" SELECT id, uuid, project_id FROM src.sessions WHERE project_id = {PID}",
                 (project,))
    conn.execute(f"INSERT INTO events({EVENTS}) SELECT {EVENTS} FROM src.events"
                 f" WHERE session_id IN {SESSIONS}", (project,))
    conn.execute("INSERT INTO cursors(transcript, offset, session_id)"
                 " SELECT transcript, offset, session_id FROM src.cursors"
                 f" WHERE session_id IN {SESSIONS}", (project,))
conn.execute("DETACH DATABASE src")
conn.close()
PY
```

### 4. Validate before anything is deleted

Count events and sessions for that project in **both** DBs and show both numbers:

```sql
-- run against the export (no WHERE needed: it holds one project) and against
-- the central DB with the project bound
SELECT COUNT(*) FROM events
WHERE session_id IN (SELECT id FROM sessions
                     WHERE project_id = (SELECT id FROM projects WHERE path = :project));
SELECT COUNT(*) FROM sessions
WHERE project_id = (SELECT id FROM projects WHERE path = :project);
```

Report `export N events / M sessions vs central N events / M sessions`. **If either pair
differs, stop here**: report the mismatch, keep the export file for inspection, and do
not offer the deletion.

### 5. Record the export

Only after validation passes, and **against the central DB** — the audit trail of what
left the central store lives in the central store, not in the export:

```sql
INSERT INTO audit_log(ts, action, project, detail)
VALUES (strftime('%s','now'), 'export', :project, :detail);
```

`detail` is the export **filename plus the validated counts**, e.g.
`alpha-2026-08-06.db; 3 events, 2 sessions`.

### 6. Then — and only then — offer the deletion

Ask plainly: *delete this project's rows from the central DB now?* Default is **no**; the
export standing on its own is already a complete outcome. State that the export file is
the only copy afterwards, and that this touches nothing else — no other project's rows,
and not the project's own `.claude/telemetry-usage.db` mirror, which is a separate file
this command never reads or writes.

On yes, run one transaction against the central DB, children before parents, with the
audit row inside it (same argv-passed style as step 3 — never interpolate the path into
SQL):

```sql
BEGIN;
DELETE FROM events WHERE session_id IN
  (SELECT id FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project));
DELETE FROM cursors WHERE session_id IN
  (SELECT id FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project));
DELETE FROM sessions WHERE project_id = (SELECT id FROM projects WHERE path = :project);
DELETE FROM projects WHERE path = :project;
INSERT INTO audit_log(ts, action, project, detail)
VALUES (strftime('%s','now'), 'delete-after-export', :project, :detail);
COMMIT;
```

`models` and `pricing` are shared reference data — **never** delete from them. Deleting a
project's cursors means a still-live transcript would be re-read from offset 0 on the
next capture; that is correct (the rows are gone) and worth one line to the user if the
project is still in use.

### 7. Report

Export path and size, validated counts, whether central rows were deleted, and the two
audit actions written. Then note that the deleted space is **not** reclaimed until
someone runs `sqlite3 <central-db> 'VACUUM;'` — say it, do not run it (VACUUM rewrites
the whole file and can take a while on a large DB).
