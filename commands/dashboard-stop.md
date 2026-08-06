---
description: Stop the backgrounded token-telemetry dashboard server
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py":*)
---

Deliberately stop the dashboard's background server and clear its runtime
marker. Run the single command and report its stdout verbatim.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py" stop
```

The server also self-terminates after ~11 minutes with no open dashboard, so
this is only needed to free the port immediately. Any open dashboard tab will
show "Connection lost" and can be revived later with
`/token-telemetry:dashboard-restart` or `/token-telemetry:dashboard`.
