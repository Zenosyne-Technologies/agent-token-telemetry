---
description: Enable token telemetry capture for the current project
allowed-tools: Bash(mkdir:*), Bash(printf:*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(cat:*), Bash(ls:*), Read, Write, Edit, AskUserQuestion
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
   remove; the script itself skips silently when the DB is absent or predates schema 3):

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" clear-mirror-meta --project "<root>"
   ```

7. **Project name** — reports show a human name per project (schema v5,
   `projects.name`). Determine it in this order:
   - `<root>/.docs/PROJECT-INFO.md` frontmatter `project:` key (the
     agent-operating-kit document) — if present and not an unresolved
     `{{PLACEHOLDER}}`, use it WITHOUT asking; capture also keeps this synced
     on every turn, so nothing more is needed — skip the registration below.
   - Otherwise ask the user for a short project name (AskUserQuestion, free
     text via Other; offer the directory basename as the default option) and
     register it — the script creates the projects row if this project has
     never captured (capture will not overwrite a registered name unless a
     kit document appears):

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" register-name --project "<root>" --name "<name>"
   ```

8. **Identity (who this usage belongs to)** — telemetry can attribute new
   sessions to a person (schema v7, `users` + `sessions.owner_id`). Identity is
   stored **centrally**, once per machine, in `~/.claude/telemetry/settings.json`
   (a `{"user": {"uuid": …, "full_name": …}, "active_backend": "local"}` file
   written mode `0600`) — **never in the repo**, so a name cannot be committed.
   The uuid is minted once and stays stable; capture only *reads* this file to
   stamp `owner_id` on new sessions and never prompts.
   - First read the current identity: `cat ~/.claude/telemetry/settings.json`
     (adjust the directory to `$TOKEN_TELEMETRY_DB`'s if that override is set;
     the file may not exist yet).
   - **A `full_name` is already present** → identity is set; do NOT re-prompt.
     State that usage is attributed to the name on file and that they can update
     it by re-running enable and choosing to change it (offer that choice via
     AskUserQuestion; on "keep", skip the write).
   - **No identity yet, or the user chose to update** → ask for their full name
     (AskUserQuestion, free text via Other), then run:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" register-user --name "<full name>"
   ```

   This mints the uuid if absent (stable thereafter — a re-run never re-mints),
   writes `settings.json` (0600), and upserts the `users` row. The name is
   **PII**: it lives only in that 0600 file and the local DB — never echo it into
   a commit, an issue, a URL, or a log. Skipping this step entirely is fine —
   capture then leaves `owner_id` NULL (pre-identity) and works exactly as before.

9. Tell the user: telemetry is enabled for this project. **Restart warning — always state it**: capture hooks load at Claude Code session start, so if the token-telemetry plugin was installed during THIS session (or this is the first enable after installing), nothing is recorded until Claude Code restarts — restart now to start capturing. Every completed turn and
   subagent is recorded (no tokens are consumed by capture). The marker file can be
   committed to enable it for the whole team. Use `/token-telemetry:info` to check
   status and `/token-telemetry:disable` to turn it off.

10. **Storage backend (local vs remote)** — by default telemetry is stored
    **locally** (the SQLite DB above; the central/project choice in step 2 is only
    where that local file lives). If instead the usage should go to a shared
    **remote (Supabase)** database — the only remote option today, more may follow
    — run `/token-telemetry:enable-remote`, which configures the remote, logs in,
    and either migrates the existing local data or starts fresh. The switch is
    reversible and does not change the per-project capture set up here.
