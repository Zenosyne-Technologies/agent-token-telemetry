---
description: Show token usage and cost statistics from the telemetry DB
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py":*)
---

Run this single command and output its stdout **verbatim** — it is already
finished markdown (headline, then windowed and per-project/model/agent/tier/
milestone/issue breakdowns). Add no commentary and no reformatting; explain
only if the user asks a follow-up question. If the script fails, show its
stderr.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" token-stats
```

The report is deterministic and read-only (the DB opens `mode=ro`); every event
prices at the rate in force at its own timestamp per
`docs/TELEMETRY-CONTRACT.md`. For a single specific issue including pre-v2
rows, the kit's per-issue recipe (issue_key union commit-sha git-log fallback)
in that contract still applies.
