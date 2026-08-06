---
description: Report telemetry plugin version, this project's opt-in and storage mode, and DB state
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py":*)
---

Run this single command and output its stdout **verbatim** — it is already
finished markdown (status table plus, when warranted, a restart diagnosis and a
seed-pricing note). Add no commentary and no recommendations of your own;
explain only if the user asks a follow-up question. If the script fails, show
its stderr.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" info --cwd "$(pwd)"
```

The report is deterministic and read-only (the DB is opened `mode=ro` and the
script never creates, migrates or modifies anything).
