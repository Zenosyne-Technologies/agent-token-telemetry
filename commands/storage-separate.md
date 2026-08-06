---
description: Export one project's telemetry into its own SQLite file, then optionally remove it from the central DB
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py":*), Bash(ls:*), Bash(cat:*), Read
---

Carve **one** project out of the central telemetry DB into a self-contained file, and
only then offer to remove it centrally. Interactive and stepwise: the user picks the
project, sees validated counts, and confirms the deletion separately. All DB work runs
through `${CLAUDE_PLUGIN_ROOT}/scripts/manage.py` — values are passed as arguments,
never written into SQL, and nothing outside the chosen project is ever exported or
deleted (the shared `models`/`pricing` reference tables are copied, never deleted, so
the export prices itself standalone).

If the script reports the central DB does not exist, say so and stop.

### 1. List the projects

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" list-projects
```

Show its table and ask which project to carve out — one per run. A project not in the
table stops the command.

### 2. Pick the export path

`<central-dir>/<slug>-<YYYY-MM-DD>.db`, where `<slug>` is the **basename** of the
project path, kebab-cased: lowercased, every run of non-alphanumeric characters
replaced by `-`, leading/trailing `-` stripped (empty result → `project`). Date is
today. **Never overwrite an existing file** — the script hard-refuses too; if the name
is taken (`ls -l`), append `-2`, then `-3`, until free, and report the final path.

### 3. Export

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" export --project "<path>" --out "<export-db>"
```

The export is created through the plugin's own `connect()`/`migrate()` (current schema
+ pricing seed); table copies use the columns common to both DBs, so a newer schema
never silently drops data.

### 4. Validate before anything is deleted

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" counts --project "<path>"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" counts --project "<path>" --db "<export-db>"
```

Report `export N events / M sessions vs central N events / M sessions`. **If either
pair differs, stop here**: report the mismatch, keep the export file for inspection,
and do not offer the deletion.

### 5. Record the export

Only after validation passes — the audit trail of what left the central store lives in
the central store:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" audit --action export --project "<path>" --detail "<export filename; N events, M sessions>"
```

### 6. Then — and only then — offer the deletion

Ask plainly: *delete this project's rows from the central DB now?* Default is **no**;
the export standing on its own is already a complete outcome. State that the export
file is the only copy afterwards, and that this touches nothing else — no other
project's rows, and not the project's own `.claude/telemetry-usage.db` mirror, which
this command never reads or writes.

On yes (one transaction, children before parents, audit row inside it):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manage.py" delete --project "<path>" --action delete-after-export --detail "<counts>"
```

Deleting a project's cursors means a still-live transcript is re-read from offset 0 on
the next capture; that is correct (the rows are gone) and worth one line to the user if
the project is still in use.

### 7. Report

Export path and size, validated counts, whether central rows were deleted, and the two
audit actions written. Then note that the deleted space is **not** reclaimed until
someone runs `VACUUM` on the central DB — say it, do not run it (it rewrites the whole
file and can take a while).
