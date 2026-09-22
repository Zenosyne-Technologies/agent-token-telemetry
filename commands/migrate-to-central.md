---
description: Import one project's local telemetry mirror (or a storage-separate export) into the central DB
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(ls:*), Bash(cat:*), Bash(git rev-parse:*), Read, Write, Edit, AskUserQuestion
---

Import **one** project's LOCAL telemetry — its project-folder mirror at
`<root>/.claude/telemetry-usage.db`, or a `/storage-separate` export file — INTO
the central telemetry DB. This is the inverse of `/storage-separate`: it brings
local logs *home* so central reporting sees them. **Local→local, no network.**
All DB work runs through `${CLAUDE_PLUGIN_ROOT}/scripts/manage.py` — values are
passed as arguments, never written into SQL. Re-running is always safe: the
import deduplicates on the full row tuple, so a second run adds nothing.

### 1. Locate the source

The default source is this project's mirror. Find the project root — the git
root of the current directory (`git rev-parse --show-toplevel`), else the current
directory — and use `<root>/.claude/telemetry-usage.db`. If the user named an
explicit export/DB path instead, use that. Confirm the file exists (`ls -l`); if
it does not, say so and stop. The project **path** to import is the project root
(central rows are keyed on it).

### 2. Show what will move

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" counts --project "<root>" --db "<source-db>"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" counts --project "<root>"
```

Report `source N events / M sessions` and the current central figures, so the
user sees the delta before anything is written. (`counts` for the central DB
reads `0` for a project it has never seen — that is the normal first-import case.)

### 3. Compatibility safeguard

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" import-check --source "<source-db>" --project "<root>"
```

Read its `key=value` lines:

- **`blocking_diff=yes`** → the source carries a column the central DB cannot
  accept; importing would silently drop data. **Stop here** and report the
  `structure_diff` line — do not import. (This is the source being on a *newer*
  schema than this kit; upgrade the plugin first.)
- **`identity_set=no`** → no central identity is on file yet. Ask the user for
  their **full name** (AskUserQuestion, free text via Other) and pass it as
  `--name` in step 4. This establishes the identity (mints the uuid, writes the
  mode-0600 `settings.json`) and writes the `users` row, so the imported
  sessions can be attributed. The name is **PII** — never echo it into a commit,
  an issue, a URL or a log.
- **`identity_set=yes`** → identity already exists; **no `--name` needed**. The
  import reuses the name on file (a pre-v7 central's `users` table is
  auto-migrated and its row rewritten from that identity automatically).

### 4. Import

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" import-project --source "<source-db>" --project "<root>" [--name "<full name>"]
```

Pass `--name` **only** when step 3 reported `identity_set=no`. The import copies
projects → models → pricing → sessions → events in one transaction (children
after parents), remapping foreign keys through the natural keys so the source's
ids never collide with the central's, deduped on the full row tuple. It then
retro-links the imported project's previously-anonymous sessions to the user.
**Cursors are never imported** — they are central-authoritative, and importing a
mirror's would corrupt the central read state. On any failure the whole import
rolls back and the central DB is left untouched. The `import-project` audit row
(source + moved counts) is written inside that transaction.

### 5. Keep a copy?

Ask plainly whether to **keep the local source file**. Default is **keep** — it
is the safer choice and the import having landed centrally is already a complete
outcome. This command **never deletes** the source; only remove it if the user
explicitly says to, and even then leave it to them to delete (`rm`), stating the
path.

### 6. Collection-switch warning

State plainly, regardless of the keep choice:

> Active collection is unchanged by this import. If this project is in **project**
> mode, capture still writes both the central DB *and* the local mirror on every
> turn — the mirror keeps growing. This import copied a point-in-time snapshot; it
> did not switch collection.

Then offer the switch: *turn this project to central-only, so the local mirror
becomes backup-only and is no longer written to?* Default **no**.

- **On an explicit yes** — set the project marker to central. Read
  `<root>/.claude/telemetry`; if it is absent or empty, write `central\n`;
  otherwise **replace only its first line** with `central`, keeping every later
  line verbatim (the contract preserves free-form notes after line 1). Then run
  `clear-mirror-meta --project "<root>"` so `/storage-status` stops reporting a
  mirror this project no longer writes (the mirror *file* is left alone — the
  user's data to keep or remove). Note the marker change takes effect on the next
  Claude Code session (capture hooks load at start).
- **On no** — leave the marker exactly as it is; the warning above stands.

### 7. Report

Source path and the counts that moved (events/sessions), the central totals now,
whether the source file was kept, whether the project was switched to
central-only, and the `import-project` audit row written. Re-running this command
is safe and converges to zero new rows.
