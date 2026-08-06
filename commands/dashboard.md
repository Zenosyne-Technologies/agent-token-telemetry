---
description: Open the interactive token-telemetry dashboard in your browser
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py":*)
---

Launch the local dashboard and open it in the default browser. Run the single
command below and report its stdout verbatim (it prints the URL and the
background pid). The server is a read-only, localhost-only web app that queries
`usage.db` directly — every KPI, breakdown and cost total is computed
server-side, so the browser only ever receives aggregates and one page of
events. Filter by period (day / week / month / year — default week), model,
agent and project; the page auto-refreshes every 5 minutes.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py" open
```

The `open` subcommand spawns the server as a detached background process (so
this command returns immediately) and reattaches instead of starting a
duplicate if one is already running. The server **self-terminates after ~11
minutes with no requests** — while a dashboard tab is open it stays alive via
the 5-minute auto-refresh, and once every tab is closed it shuts down and
cleans up on its own. To stop it sooner:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py" stop
```

Read-only throughout: the DB is opened `mode=ro`, every filter value is passed
as a bound SQL parameter (never interpolated), and each event prices at the
rate in force at its own timestamp per `docs/TELEMETRY-CONTRACT.md` — the same
resolution `report.py` uses, so the dashboard's numbers match `/token-stats`.
