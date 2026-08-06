---
description: Show where telemetry data lives — central DB size and state, plus every project's events and mirror
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py":*)
---

Run this single command and output its stdout **verbatim** — it is already
finished markdown (central DB table, per-project mirror table, error-log and
audit-trail notes). Add no commentary; explain only if the user asks a
follow-up question. If the script fails, show its stderr.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" storage-status
```

The report is deterministic and read-only (the DB opens `mode=ro`; file sizes
include `-wal`/`-shm` siblings). To carve a project out into its own file use
`/token-telemetry:storage-separate`; to remove one, `/token-telemetry:storage-delete`.
