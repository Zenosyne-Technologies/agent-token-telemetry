# token-telemetry

Claude Code plugin that records per-turn and per-subagent token usage into a
central SQLite database — with **zero model-token overhead** (capture runs in
Stop/SubagentStop hooks, outside the model loop).

v0.7.2 completes the **permission-hardening pass** (AOS-10): no command grants
`Bash(sqlite3:*)` or bare `Bash(python3:*)` any more. Write-side bookkeeping
(export, delete, audit, mirror-meta, name registration) moved into
`scripts/manage.py` (argv-passed values, never interpolated into SQL; table
copies introspect common columns so exports survive schema growth), and
`storage-status` joined the read-only script reports. Every command's grant now
pins to its exact script invocation.

v0.7.0 (schema v5) adds **project names and a richer project table**:
`projects.name` is stamped by capture from the kit's `.docs/PROJECT-INFO.md`
(`project:` frontmatter key) or asked for at enable time; reports fall back to
the path basename. `/token-telemetry:project-stats` now shows cache read/write
counters and splits the (bold) estimated cost into classic and cached price
columns, each formatted `total (input / output)`. `/token-telemetry:token-stats`
joins the script-generated family in `scripts/report.py`.

v0.6.0 makes the deterministic reports **script-generated**: `/token-telemetry:info`,
`/token-telemetry:project-stats` and `/token-telemetry:pricing-update` now just run
`scripts/report.py` / `scripts/pricing_update.py` and echo the finished markdown —
near-zero model tokens, sub-second runtime, and read-only report access (`mode=ro`).
The pricing update fetches and parses the official pricing page itself (LLM flow
remains only as fallback if the page layout changes). Fetch/render are split behind
a single backend seam (`open_ro()` / `capture.connect()`) so future server-hosted
databases plug in without touching queries or formatting.

v0.5.0 (schema v4) prices **5-minute and 1-hour cache writes separately** — they
bill at 1.25× and 2× the input rate respectively, so 1h-heavy sessions were
under-costed. `events.cache_w` stays the TTL-agnostic total (every existing
query keeps working); new `events.cache_w_1h` carries the 1-hour portion and
`pricing.cache_w_1h_usd` its rate (NULL on pre-v4 rows → cost queries fall back
to the 5m rate, which is exactly the old estimate). The seed and
`/token-telemetry:pricing-update` both carry the 1h rates.

v0.4.3 fixes a **capture multiple-counting bug**: Claude Code writes one
transcript line per content block, all repeating the same `message.id` and
usage object, and capture summed every line — inflating token counts (and
therefore cost estimates) 2–3× on tool-heavy turns. Usage is now counted once
per `message.id` (last line wins — snapshots are cumulative). Rows captured by
older versions are overstated; there is no correction factor stored, so
delete-and-recapture is the only clean remedy for affected projects.

v0.4.0 adds **storage management**: `/token-telemetry:storage-status` shows where
every project's data actually lives (central DB size including `-wal`/`-shm`,
per-project event counts, whether a project-level copy is configured and whether
its file is reachable from this machine), `/token-telemetry:storage-separate`
carves one project out into its own self-contained SQLite file — validated
against the central counts before it offers to remove anything — and
`/token-telemetry:storage-delete` removes one project's data with an
export-first route and a typed confirmation. Every removal and export is
recorded in a new append-only `audit_log` table (schema v3), and the central
`projects` row now records each project's mirror path and last mirrored event.

v0.3.0 adds a **storage choice**. `/token-telemetry:enable` now asks where the
data should live: *central only* (the default, unchanged behavior) or *project
folder* — which keeps writing the central DB **and** mirrors the same event rows
into `<root>/.claude/telemetry-usage.db`, so usage history travels with the repo
or the team share. The disclosure is deliberate and always stated at enable time:
project mode is a **dual write**, not a redirect — a central copy is still kept
for retention and cross-project stats. The central DB stays authoritative (it
alone tracks transcript cursors); the mirror is best effort and any failure of it
is logged and dropped rather than costing you the capture. `/token-telemetry:info`
reports the whole picture: plugin version, this project's opt-in and storage mode,
sidecar presence, and both DBs' schema version, event counts and pricing state.

v0.2.0 added pricing-as-data (a versioned, effective-dated `pricing` table instead
of a hardcoded map) and kit-aware columns (`issue_key`, `task_size`, `note`) so
usage can be sliced by tracker issue, task size, and milestone — the seam the
**agent-operating-kit** consumes for cost-aware reporting. Install both from the
same marketplace for the full suite; each degrades gracefully without the other.

The kit-side joins cost nothing extra to capture because they ride the kit's own
conventions: `milestone/<slug>` branches make the by-milestone breakdown a plain
GROUP BY on the `branch` column; `<KEY>:`-prefixed commit messages give the
by-issue fallback when no sidecar was present; model-name prefixes map to the
kit's dispatch tiers (orchestrator / heavy / small / micro). This repo is itself
operated under the kit (Jira project AOS, `.docs/` cascade, issue-key commits) —
the telemetry dogfoods the conventions it joins against.

## Install

```
claude plugin marketplace add Zenosyne-Technologies/emprove-marketplace
claude plugin install token-telemetry@emprove
```

**Restart Claude Code after installing — this is not optional**: capture hooks load only at session start, so sessions before a restart are silently not recorded (the most common "why is nothing logged?" cause; `/token-telemetry:info` diagnoses it). Standalone (no marketplace):

```
claude plugin marketplace add /path/to/agent-token-telemetry
claude plugin install token-telemetry@agent-token-telemetry
```

## Use

- `/token-telemetry:enable` — opt this project in (writes the `.claude/telemetry`
  marker, whose first line records the chosen storage mode — `central` or
  `project`; committable for team-wide opt-in)
- `/token-telemetry:disable` — opt out
- `/token-telemetry:info` — read-only status: plugin version, opt-in + storage
  mode + sidecar for this project, central DB (schema version, events, projects,
  pricing rows and their as-of date) and, in project mode, the mirror DB
- `/token-telemetry:storage-status` — read-only map of where data lives: central
  DB path/size/schema/event count, then one row per project with its event
  count, whether a project-level copy is configured, that file's size (or "not
  accessible on this machine") and when it was last mirrored
- `/token-telemetry:storage-separate` — export one project into
  `<central-dir>/<slug>-<date>.db` (same schema, full `models`/`pricing` so it
  prices itself standalone), validate its counts against the central DB, then
  optionally delete that project's rows centrally — both steps audited
- `/token-telemetry:storage-delete` — delete one project's data; asks whether to
  export first, shows the exact counts, and requires the project's basename
  typed back before writing. Never VACUUMs for you; it tells you to
- `/token-telemetry:project-stats` — one all-time table, a row per project:
  sessions, events, input/output tokens, estimated cost (priced per event from
  the `pricing` table), first seen and last activity, ordered by cost
- `/token-telemetry:token-stats` — totals, per-project/model/agent/milestone/
  tier/issue breakdown, cache hit rate, cost estimates priced from the
  `pricing` table
- `/token-telemetry:pricing-update` — agent-driven refresh: fetches Anthropic's
  currently published per-model pricing and inserts new effective-dated rows on
  change (history is never mutated — a rate change is always a new row)
- `/token-telemetry:schedule-pricing` — registers `pricing-update` as a weekly
  background scheduled agent where the host supports it, else prints the
  manual-cadence advice

Data lives in `~/.claude/telemetry/usage.db` (SQLite, WAL) — plus
`<root>/.claude/telemetry-usage.db` in project mode. `/token-telemetry:token-stats`
always reads the central DB; the mirror carries the same schema, so the same
queries run against it when you open it directly with sqlite3/DuckDB/Grafana. Capture never breaks a session — capture errors go to
`~/.claude/telemetry/error.log` only for opted-in projects (mirror failures land
there too, labelled); failures before the opt-in check exit silently. The mirror
keeps no cursors, so it can hold duplicate rows if the central DB is ever reset
while the project copy is kept — they are identical rows; see
`docs/TELEMETRY-CONTRACT.md` for the dedupe hint. The central DB also records
each project's mirror path and last mirrored event — stamped before the mirror
write is attempted, so it reports **configured state, not a landed write**: a
recent timestamp with a missing file is exactly how a broken mirror shows up in
`/token-telemetry:storage-status`. Cost is never stored — it is derived at query
time from the `pricing` table (see `docs/TELEMETRY-CONTRACT.md` for the exact
rate-resolution rule and the stability promise on consumed columns). A fresh DB
seeds the four tier rates at `effective_from = 0` — reports label those as
"seed rates (undated)"; run `/token-telemetry:pricing-update` once to replace
them with dated rows from Anthropic's published pricing. Rate changes are always
new effective-dated rows, so historical events keep pricing at the rate that was
in force when they happened.

When a kit-managed project has telemetry enabled, the kit writes
`.claude/telemetry-context.json` at tracker-task start/switch; capture stamps
`issue_key`/`task_size`/`note` from it onto events, enabling per-issue and
per-milestone cost breakdowns and the kit's cost-per-issue closing comment.

## Design

See `docs/superpowers/specs/2026-07-17-token-telemetry-plugin-design.md` (v0.1.0
capture design), `docs/TELEMETRY-CONTRACT.md` (the v0.2.0 stability contract:
consumed columns, pricing table shape, sidecar spec, `PRAGMA user_version`
discipline), and — kit side — the integration design in the agent-operating-kit
repo: `docs/superpowers/specs/2026-08-04-cost-telemetry-integration-v0.12.0-design.md`
with its `templates/docs/agents/token-economics.md` contract mirror.

## Tests

```
python3 -m unittest tests.test_capture -v
```
