---
description: Report telemetry plugin version, this project's opt-in and storage mode, and DB state
allowed-tools: Bash(sqlite3:*), Bash(cat:*), Bash(ls:*), Read
---

Report the telemetry setup. **Read-only — never create, migrate or modify a DB, a
marker or a config file here.** If something is absent, say so and move on.

1. **Plugin version** — read `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` and
   report its `version` and `name`.

2. **This project** — find the project root (git root of the current directory, else
   the current directory), then report:
   - opt-in: does `<root>/.claude/telemetry` exist? (absent → telemetry is off for
     this project; say so, point at `/token-telemetry:enable`, and skip step 4)
   - storage mode: the marker's first line — `project` (case-insensitive, whitespace
     trimmed) means project mode; **anything else, including an empty file, is
     `central`**. Read it with `cat <root>/.claude/telemetry` and say which mode is in
     force, noting when the mode is a default rather than an explicit line.
   - sidecar: does `<root>/.claude/telemetry-context.json` exist? If so, `cat` it and
     report its `issue_key`/`size` (not the whole file).

3. **Central DB** (`~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB` when set).
   `ls -l` it first — if it does not exist, report "no telemetry recorded yet" and skip
   the queries. Otherwise run with `sqlite3 -header -column`:

   ```sql
   PRAGMA user_version;
   SELECT COUNT(*) AS events, MIN(date(ts,'unixepoch')) AS first_day,
          MAX(date(ts,'unixepoch')) AS last_day FROM events;
   SELECT COUNT(*) AS projects FROM projects;
   SELECT COUNT(*) AS pricing_rows,
          CASE WHEN MAX(effective_from) IS NULL THEN 'none'
               WHEN MAX(effective_from) = 0 THEN 'seed rates (undated)'
               ELSE date(MAX(effective_from),'unixepoch') END AS latest_rates
   FROM pricing;
   ```

   `effective_from = 0` is the seed marker, **never** render it as a date
   (1970-01-01). When `latest_rates` is `seed rates (undated)`, add one line: run
   `/token-telemetry:pricing-update` to replace the seed with dated published rates.
   Also `ls -l ~/.claude/telemetry/error.log` and, if present, report its size and
   last-modified time — capture and mirror failures are swallowed and land there.

4. **Project mirror DB** — only when step 2 found `project` mode. `ls -l
   <root>/.claude/telemetry-usage.db`; if absent, report that no mirror write has
   landed yet (it is created on the first captured turn after enabling). If present,
   run the same `PRAGMA user_version` and event-count/date-range queries against it and
   report them beside the central numbers.

   The central DB is authoritative, so compare the mirror against the central count
   **for this project's path** — passing the root as a parameter, or with any `'` in
   the path doubled (`''`) if you inline it:

   ```sql
   SELECT COUNT(*) AS events_here FROM events e
   JOIN sessions s ON s.id = e.session_id
   JOIN projects p ON p.id = s.project_id
   WHERE p.path = ?;   -- bind the project root; if inlined, double any apostrophe
   ```

   A mirror count **higher** than that is expected, not corruption, and has two
   causes — name whichever fits, and only if you actually observe the gap:
   - the mirror carries no cursors, so rows were re-inserted after the central DB was
     reset, moved or restored while the project-local file was kept;
   - the mirror is committed to the repo and was pulled from teammates, so it carries
     their machines' rows — visible as extra `projects.path` values inside the mirror.
     Check with `SELECT path, COUNT(*) FROM projects p JOIN sessions s ON
     s.project_id = p.id JOIN events e ON e.session_id = s.id GROUP BY path;` against
     the mirror.

**Diagnostic**: when this project is enabled but the central DB has ZERO events for this project's path (or doesn't exist), say the likely cause first: the capture hooks were not loaded when this session started — restart Claude Code after installing the plugin or enabling telemetry; capture begins next session.

Present it as a compact status block: plugin version, project opt-in + mode + sidecar,
central DB line, mirror DB line. No recommendations beyond the two named above.
