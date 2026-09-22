---
doc: Migration Commands
type: handbook
status: active
summary: The telemetry migration path — the pure primitives in migrate_lib.py (compat_report, retro_link, ensure_users_row) and how Command A (manage.py import-check/import-project) consumes them to import a project's local mirror into the central DB, with its foreign-key remap, single-transaction full-tuple dedupe, cursors-never-imported invariant, and retro-link.
keywords: [migration, import, migrate_lib, compat_report, retro_link, ensure_users_row, dedupe, foreign-key-remap, cursors, command-a]
level: project
audience: developer
module: migration
sources: [scripts/migrate_lib.py, scripts/manage.py, commands/migrate-to-central.md, docs/TELEMETRY-CONTRACT.md]
related: ["[[identity-model]]", "[[storage-backend]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Migration Commands

Telemetry can move between stores. The first such path is **Command A** —
importing a project's local mirror (or a `/storage-separate` export) *into* the
central DB. It is local→local and needs no network. This page covers the shared
primitives and Command A's design; the remote path (central→remote) is a later
phase built on the same primitives.

## The primitives — `scripts/migrate_lib.py`

Three pure, stdlib-only building blocks operate on SQLite connections the caller
supplies. They hold **no** policy: prompting, keep-local decisions and collection
switching are the *command's* job, never the library's.

- **`compat_report(src, dst, kit_version)`** — pure inspection, no writes. Reads
  each DB's `PRAGMA user_version`, diffs the columns of every mirrored table, and
  reports whether the destination has the `users` table. It never throws on a
  pre-v7 or malformed DB — it *reports* the diff, it does not fix it. Its
  `CompatReport.table_diffs` carry, per table, the columns `only_in_src` and
  `only_in_dst`.
- **`retro_link(conn, uuid, project_id=None)`** — binds previously-anonymous
  sessions (`owner_id IS NULL`) to a now-known user, optionally scoped to one
  project. Idempotent: a second run matches nothing and updates 0 rows. The uuid
  is bound, never interpolated.
- **`ensure_users_row(conn, uuid, name)`** — the auto-migrate primitive. Brings a
  pre-v7 destination to v7 via `capture.migrate` (creating the `users` table and
  `sessions.owner_id`), then idempotently upserts the `users` row keyed on uuid
  (`created_at` is never rewritten on update). The name is PII — bound, never
  logged.

## Command A — `manage.py import-check` / `import-project`

Two subcommands mirror the split every storage command uses: a read-only
preflight the prompt reads, and the one write action. `commands/migrate-to-central.md`
orchestrates them; all values pass as arguments, never SQL.

### `import-check` — the safeguard the prompt reads

Read-only. It opens the source read-only, opens central read-only when it
exists, runs `compat_report`, and prints machine-parseable `key=value` lines:
`blocking_diff`, `users_at_dst`, `identity_set`, `compatible`, plus the version
and structure-diff detail. The command branches on these to decide whether to
abort and whether to prompt for a name.

### `import-project` — the import

The inverse of `export`: it ATTACHes the source and copies rows *in* rather than
carving them *out*. The design points that matter:

- **Compat abort (req 12).** Before any write it runs `compat_report` and refuses
  when any table has `only_in_src` columns — the source is on a *newer* schema and
  importing would silently drop that data. `only_in_dst` columns are harmless
  (the source simply has no value; the copy leaves them NULL, matching the
  additive-schema contract). A pre-v7 central instead is auto-migrated: the
  schema-owning `capture.connect` raises it to the current version, and the
  `users` row is (re)written from the identity — from `--name` when the command
  prompted for it, else from the central `settings.json` already on file.

- **Foreign-key remap through natural keys.** `export` copies ids verbatim
  because its destination is empty. Import cannot: central is populated, so the
  source's synthetic ids would collide. Every FK is therefore remapped through a
  natural key — project by `path`, session by `uuid`, model by `name`. Parents
  are inserted first (`INSERT OR IGNORE` on those unique keys); the central
  project id is read back and bound into the sessions insert; events join
  `src.sessions→sessions` (on uuid) and `src.models→models` (on name) to pick up
  the *central* `session_id` and `model_id`. Column lists come from
  `common_column_list()` introspection (shared columns only, `id` excluded), so a
  schema delta never drops or invents a column.

- **Full-tuple dedupe (contract "Duplicates").** Events have no natural key, so
  the insert appends `NOT EXISTS (… WHERE ev.<col> IS e.<col> …)` over every
  remapped column, null-safe via SQLite's `IS`. A re-run therefore adds nothing —
  the whole command converges.

- **One transaction, audit inside it.** The projects/models/pricing/sessions/
  events inserts and the `import-project` audit row (source + moved counts) run
  inside a single `with central:` block. Any failure rolls the whole import back
  and leaves central untouched — there is no half-imported project.

- **Cursors are never imported.** Cursors are central-authoritative and drive
  what is read from a transcript. A mirror keeps none and an export's are foreign;
  importing either could make central re-read or skip transcript bytes. The import
  simply never references the `cursors` table.

- **Retro-link after commit.** Once the import commits, `retro_link(central, uuid,
  project_id=<imported project>)` binds that project's still-anonymous sessions to
  the user. It is a separate, idempotent transaction — scoped to the imported
  project, so other projects' anonymous sessions stay anonymous.

## What the command owns, not the script

Locating the source, the keep-a-copy choice (default keep, never deletes), the
collection-switch warning, and the optional marker flip to central-only all live
in `commands/migrate-to-central.md`. The subcommands stay non-interactive so their
permission grant pins to `manage.py` alone — the same discipline as every other
storage command.
