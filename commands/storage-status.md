---
description: Show where telemetry data lives — central DB size and state, plus every project's events and mirror
allowed-tools: Bash(sqlite3:*), Bash(ls:*), Bash(cat:*), Read
---

Report where telemetry data is stored, across every project the central DB knows about.
**Read-only — never create, migrate, move or delete a DB, a marker or a row here.** If
something is absent, say so and move on. (To carve a project out into its own file use
`/token-telemetry:storage-separate`; to remove one, `/token-telemetry:storage-delete`.)

1. **Central DB** — `~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB` when set.
   `ls -l` the file **and its `-wal`/`-shm` siblings** (`ls -l <db> <db>-wal <db>-shm`);
   the reported size is their sum, since unchecked-pointed WAL can hold a large share of
   the data. If the DB itself does not exist, report "no telemetry recorded yet" (enable
   with `/token-telemetry:enable`) and stop.

   ```sql
   PRAGMA user_version;
   SELECT COUNT(*) AS events, MIN(date(ts,'unixepoch')) AS first_day,
          MAX(date(ts,'unixepoch')) AS last_day FROM events;
   ```

2. **Per project** — one row per `projects` row, whatever its storage mode:

   ```sql
   SELECT p.path AS project, COUNT(e.rowid) AS events, p.mirror_path AS mirror_path,
          CASE WHEN p.mirror_last_at IS NULL THEN ''
               ELSE datetime(p.mirror_last_at,'unixepoch') END AS mirror_last_at
   FROM projects p
   LEFT JOIN sessions s ON s.project_id = p.id
   LEFT JOIN events e ON e.session_id = s.id
   GROUP BY p.id, p.path ORDER BY events DESC;
   ```

   If step 1 reported `user_version` below 3, this DB predates the mirror columns: run
   the same query without the two `p.mirror_*` expressions, render the mirror columns as
   `—`, and add one line saying the DB upgrades to schema 3 on its next captured turn.

3. **Mirror files** — for each project whose `mirror_path` is set, `ls -l <mirror_path>
   <mirror_path>-wal <mirror_path>-shm` and report the summed size. When the path does
   not exist on this machine, report **"not accessible on this machine"** — that is the
   normal reading for a project that lives on another checkout, an unmounted volume or a
   teammate's machine, and it is **not** an error. An empty `mirror_path` simply means
   central-only storage for that project.

4. **Render** exactly these two tables, in this order.

   ```markdown
   ### Central DB

   | path | size (incl. -wal/-shm) | events | schema |
   |---|---|---|---|
   | `<db path>` | 12.4 MB | 8,431 | v3 |

   ### Projects

   | project | events | mirror? | mirror size | last mirrored |
   |---|---|---|---|---|
   | `/Users/me/dev/foo` | 5,102 | yes | 3.1 MB | 2026-08-05 14:03 (2 days ago) |
   | `/Users/me/dev/bar` | 3,329 | no | — | — |
   | `/Volumes/ext/baz` | 812 | yes | not accessible on this machine | 2026-06-02 (2 months ago) |
   ```

   Humanize `mirror_last_at` as the timestamp plus a relative age in parentheses.

5. **`mirror_last_at` is configured state, not a write receipt.** It is stamped in the
   central transaction *before* the project-local write is attempted, so it says "the
   last captured turn for this project was configured to write a mirror" — never "the
   mirror write succeeded". A recent `mirror_last_at` with a missing or stale mirror file
   is exactly the signal that mirror writes are failing: say so, and point at
   `~/.claude/telemetry/error.log` (`ls -l` it and report size + last-modified when it
   exists), where every swallowed mirror failure is logged.

6. If `audit_log` has any rows, close with the last five storage-management actions —
   nothing more:

   ```sql
   SELECT datetime(ts,'unixepoch') AS at, action, project, detail
   FROM audit_log ORDER BY ts DESC LIMIT 5;
   ```

Keep the whole report to the two tables plus at most three lines of notes.
