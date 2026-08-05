---
description: Disable token telemetry capture for the current project
allowed-tools: Bash(rm:*), Bash(sqlite3:*), Bash(ls:*), Read
---

Disable token telemetry for this project:

1. Find the project root: the git root of the current directory, else the current directory.
2. Run: `rm -f <root>/.claude/telemetry` (also check `./.claude/telemetry` if cwd differs from root).
3. Clear the mirror metadata on the central `projects` row — no further capture will
   write a project-level copy, so leaving it set would have
   `/token-telemetry:storage-status` report a mirror that is no longer maintained. The
   mirror *file* is left in place; it is the user's data. Skip silently if the central DB
   (`~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB`) does not exist or predates
   schema 3:

   ```sql
   UPDATE projects SET mirror_path = NULL, mirror_last_at = NULL WHERE path = :root;
   ```

4. Tell the user: telemetry capture is disabled for this project. Existing recorded data in `~/.claude/telemetry/usage.db` is untouched (remove it per project with `/token-telemetry:storage-delete`), and any project-local `.claude/telemetry-usage.db` is left where it is.
