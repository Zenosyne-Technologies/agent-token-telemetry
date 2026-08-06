---
description: Restart the token-telemetry dashboard server without opening a new tab
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py":*)
---

Restart the dashboard's background server **in place** — it rebinds the same
localhost port the open dashboard tab is already polling, so that tab's
"connection lost" overlay reconnects on its own within a few seconds. This does
**not** open a second browser tab. Run the single command and report its stdout
verbatim.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py" restart
```

Use this when the dashboard shows "Connection lost" (e.g. the server hit its
idle timeout or was stopped). If no dashboard tab is open, prefer
`/token-telemetry:dashboard`, which also opens the browser.
