---
doc: Handbook index
type: reference
status: active
summary: The table of contents for one handbook audience — every page in this folder is registered here or it does not exist.
keywords: [handbook, index, pages, audience]
level: project
created: 2026-09-21
updated: 2026-09-22
---

# Handbook Index

**Belongs here**: pages describing the product AS IT CURRENTLY IS, in this audience's voice — one page per logical unit, each registered in the table below with a one-line description. A page not listed here doesn't exist. Page format, audience voice, and the mandatory discovery pass before creating or amending anything are the rules of this handbooks area itself.

**Does NOT belong here**: what CHANGED (the tracker and commit history own that), framework defaults and stock conventions, rules written for agents rather than humans (→ `../../information/`), plans and research (→ `../../plans/`, `../../researches/`), and secrets or internal-only URLs — handbooks are a shareable surface.

`sources` is the discovery key: it names the code paths a page documents, so a changed path finds its page with one grep.

| item | sources | what it covers | status | updated |
|---|---|---|---|---|
| [[reading-token-stats]] | `commands/token-stats.md`, `commands/project-stats.md`, `scripts/report.py` | What `/token-stats` shows, the all-time per-project table from `/project-stats`, scoped rollups by issue-key set and their three empty states, seed rates (undated), cache hit rate | active | 2026-09-21 |
| [[migrating-local-logs-to-central]] | `commands/migrate-to-central.md`, `scripts/manage.py` | Importing a project's local mirror (or a storage-separate export) into the central DB with `/migrate-to-central`, the keep-a-copy default, what switching active collection means, and why re-running is safe | active | 2026-09-22 |
| [[migrating-to-remote]] | `commands/migrate-to-remote.md`, `scripts/remote_migrate.py`, `scripts/supabase_backend.py` | Uploading the whole central DB to the remote Supabase project with `/migrate-to-remote` — login, the readiness check, the count-verified upload, the keep-local default, and that switching collection is reversible and re-running safe | active | 2026-09-22 |
