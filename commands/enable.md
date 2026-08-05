---
description: Enable token telemetry capture for the current project
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
6. Tell the user: telemetry is enabled for this project. Every completed turn and
   subagent is recorded (no tokens are consumed by capture). The marker file can be
   committed to enable it for the whole team. Use `/token-telemetry:info` to check
   status and `/token-telemetry:disable` to turn it off.
