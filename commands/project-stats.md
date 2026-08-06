---
description: One table of per-project token usage, cost and activity dates from the central DB
allowed-tools: Bash(python3:*)
---

Run this single command and output its stdout **verbatim** — it is already
finished markdown. Add no commentary, no summary, no reformatting; explain only
if the user asks a follow-up question. If the script fails, show its stderr.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" project-stats
```

The report is deterministic and read-only (the DB is opened `mode=ro`); every
event prices at the rate in force at its own timestamp, per
`docs/TELEMETRY-CONTRACT.md`. For where the data physically lives use
`/token-telemetry:storage-status`; for time-windowed and per-model/agent/tier
breakdowns use `/token-telemetry:token-stats`.
