---
description: Enable token telemetry capture for the current project
allowed-tools: Bash(mkdir:*), Bash(printf:*), Bash(sqlite3:*), Bash(cat:*), Bash(ls:*), Read, Write, Edit, AskUserQuestion
---

Enable token telemetry for this project:

1. Find the project root: the git root of the current directory, else the current directory.
2. Ask the user where the data should be stored (AskUserQuestion, two options):
   - **Central only** (default) — events go to `~/.claude/telemetry/usage.db` only.
   - **Project folder** — events go to the central DB *and* a project-local copy at
     `<root>/.claude/telemetry-usage.db`, so the data travels with the repo.
3. Run `mkdir -p <root>/.claude`, then write the chosen mode as the marker's **first
   line** — `central` or `project`. Anything else in the file (or an empty file, as
   older versions wrote) is read as `central`.
   - Marker absent or empty → `printf 'project\n' > <root>/.claude/telemetry` (or
     `central`).
   - Marker already has content → **read it, replace only line 1, write it back**,
     keeping every later line verbatim. Lines after the first are free-form notes the
     contract promises to preserve; never truncate the file to write the mode.
4. **Project mode only** — add `.claude/telemetry-usage.db*` to `<root>/.gitignore`
   (append it if the line is not already there; the `*` also covers the `-wal`/`-shm`
   files). Then tell the user it is git-ignored by default, and that committing it
   instead is a valid team choice if they want shared usage history in the repo — in
   which case they should drop that line.
5. **Project mode only** — always state plainly: a central copy is still kept at
   `~/.claude/telemetry/usage.db` for retention and cross-project stats. The project
   copy is a best-effort mirror; the central DB is authoritative.
6. **Central mode only** — clear any stale mirror metadata on the central `projects` row,
   so `/token-telemetry:storage-status` stops reporting a project-level copy this project
   no longer writes (the mirror *file* is left alone — it is the user's data to keep or
   remove). Skip silently if the DB does not exist or predates schema 3; this is
   bookkeeping, never a reason to fail the command:

   ```sql
   UPDATE projects SET mirror_path = NULL, mirror_last_at = NULL WHERE path = :root;
   ```

7. Tell the user: telemetry is enabled for this project. **Restart warning — always state it**: capture hooks load at Claude Code session start, so if the token-telemetry plugin was installed during THIS session (or this is the first enable after installing), nothing is recorded until Claude Code restarts — restart now to start capturing. Every completed turn and
   subagent is recorded (no tokens are consumed by capture). The marker file can be
   committed to enable it for the whole team. Use `/token-telemetry:info` to check
   status and `/token-telemetry:disable` to turn it off.
