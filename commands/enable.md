---
description: Enable token telemetry capture for the current project
---

Enable token telemetry for this project:

1. Find the project root: the git root of the current directory, else the current directory.
2. Run: `mkdir -p <root>/.claude && touch <root>/.claude/telemetry`
3. Tell the user: telemetry is enabled for this project. Every completed turn and subagent will be recorded to `~/.claude/telemetry/usage.db` (no tokens are consumed by capture). The marker file can be committed to enable it for the whole team. Use `/token-telemetry:disable` to turn it off.
