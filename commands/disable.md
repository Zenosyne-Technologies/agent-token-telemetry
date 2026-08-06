---
description: Disable token telemetry capture for the current project
allowed-tools: Bash(rm:*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(ls:*), Read
---

Disable token telemetry for this project:

1. Find the project root: the git root of the current directory, else the current directory.
2. Run: `rm -f <root>/.claude/telemetry` (also check `./.claude/telemetry` if cwd differs from root).
3. Clear the mirror metadata on the central `projects` row — no further capture will
   write a project-level copy, so leaving it set would have
   `/token-telemetry:storage-status` report a mirror that is no longer maintained. The
   mirror *file* is left in place; it is the user's data. The script skips silently if
   the central DB does not exist or predates schema 3:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" clear-mirror-meta --project "<root>"
   ```

4. Tell the user: telemetry capture is disabled for this project. Existing recorded data in `~/.claude/telemetry/usage.db` is untouched (remove it per project with `/token-telemetry:storage-delete`), and any project-local `.claude/telemetry-usage.db` is left where it is.
