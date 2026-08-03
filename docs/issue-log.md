# Issue log

Real bugs found during development are recorded here AND filed in the tracker per `docs/agents/ticket-filing.md`.

| Date | Issue (tracker ref) | Severity | Summary | Status |
|------|---------------------|----------|---------|--------|
| 2026-08-03 | AOS-1 (epic; found by security validation pre-merge) | sev1 | `git_meta` ran `git -C <cwd>` honoring repo-controlled `core.fsmonitor`/`core.hooksPath` → arbitrary code execution on hook fire in hostile repos. Hardened with `-c` overrides; lookup moved outside the DB write lock. Commits fa8bbfe, 7c844ec. | Fixed pre-merge |
| 2026-08-03 | AOS-1 (epic; found by final review + BA validation pre-merge) | sev2 | Concurrent hook processes could double-count (cursor read outside txn) or silently lose events (get_or_create UNIQUE race; reproduced 11/12). Serialized capture with `BEGIN IMMEDIATE`; 8-process regression test added. Follow-up: fresh-DB WAL-switch stampede could still drop an event (`PRAGMA journal_mode=WAL` → "database is locked" bypassing busy handler) — bounded retry added, proven 30/30. Commits fa8bbfe, a865274. | Fixed |
