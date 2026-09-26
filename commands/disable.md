---
description: Disable token telemetry capture for the current project
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(ls:*), Read
---

Disable token telemetry for this project:

1. Run the scripted disable. It resolves the project exactly as capture does: inside a
   git worktree, the project is the worktree's **main repository**, so this turns
   capture off for the main checkout **and all its worktrees**. It removes every
   `.claude/telemetry` opt-in marker it finds (main root, each worktree, the current
   checkout and the current directory), then clears the mirror metadata on the central
   `projects` row. The mirror *file* is left in place; it is the user's data. The
   central-DB step skips silently if that DB does not exist or predates schema 3.

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" disable --cwd "<current directory>"
   ```

   It prints one JSON line: `root` (the repository), `worktrees` (its other checkouts)
   and `removed` (the marker files deleted).

2. Tell the user: telemetry capture is disabled for `<root>` — **this applies to
   `<root>` and all its worktrees** (name them from `worktrees` when there are any).
   A marker placed in some other subdirectory by hand is not searched for; if one
   exists, capture from that directory continues. Existing recorded data in
   `~/.claude/telemetry/usage.db` is untouched (remove it per project with
   `/token-telemetry:storage-delete`), and any project-local
   `.claude/telemetry-usage.db` is left where it is.
