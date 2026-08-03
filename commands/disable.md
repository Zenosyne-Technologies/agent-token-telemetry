---
description: Disable token telemetry capture for the current project
---

Disable token telemetry for this project:

1. Find the project root: the git root of the current directory, else the current directory.
2. Run: `rm -f <root>/.claude/telemetry` (also check `./.claude/telemetry` if cwd differs from root).
3. Tell the user: telemetry capture is disabled for this project. Existing recorded data in `~/.claude/telemetry/usage.db` is untouched.
