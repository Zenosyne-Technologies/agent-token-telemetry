---
description: Delete one project's telemetry from the central DB, with an export-first option and a typed confirmation
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(ls:*), Bash(cat:*), Read
---

Remove **one** project's rows from the central telemetry DB. Deletion is irreversible
and there is no undo in this plugin, so the export route is offered first and the plain
route takes a typed confirmation. All DB work runs through
`${CLAUDE_PLUGIN_ROOT}/scripts/manage.py` — values passed as arguments, never written
into SQL. **Nothing outside the chosen project is ever deleted**; `models` and
`pricing` are shared reference data the script never touches on delete.

If the script reports the central DB does not exist, say so and stop.

### 1. List and select

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" list-projects
```

Show its table and ask which project — one per run. A project not in the table stops
the command.

### 2. Ask the route FIRST, before showing anything else

Two options, asked before any confirmation of the deletion itself:

- **Export first** (keep the data in its own file, then remove it centrally) — read and
  follow `${CLAUDE_PLUGIN_ROOT}/commands/storage-separate.md` from its step 2 with the
  project already chosen, and **stop when it finishes**. That flow already validates
  the export, writes the `export` audit row and offers the post-export deletion; do not
  delete anything here as well.
- **Plain delete** (the data is not wanted anywhere) — continue below.

### 3. Show exactly what will be deleted

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" counts --project "<path>"
```

Report the events/sessions/cursors counts and date span plainly, plus: this is the only
copy unless a project-local mirror exists (`storage-status` shows it; this command
never reads, writes or deletes that file).

### 4. Extra confirmation — typed, not a yes

Ask the user to **type the project's basename** (the last path segment) to confirm.
Compare exactly. Anything else — a `yes`, a near miss, an empty answer — cancels the
command with nothing written.

### 5. Delete

One transaction, children before parents, with the audit row inside it — a failure
part-way rolls the whole thing back, so a half-deleted project cannot survive:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" delete --project "<path>" --action delete --detail "<N events, M sessions, K cursors>"
```

The `audit_log` row outlives the project it describes; audit history is never deleted.

### 6. Report

Deleted counts, the audit row written, and two notes:

- Space is **not** reclaimed until someone runs `VACUUM` on the central DB — say it,
  never run it automatically (it rewrites the whole file and locks it meanwhile).
- If the project is still opted in, capture starts recording it again on the next turn
  from a fresh cursor — telemetry is not disabled by deleting data. Point at
  `/token-telemetry:disable` when that is what the user actually wanted.
