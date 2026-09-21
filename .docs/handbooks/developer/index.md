---
doc: Handbook index
type: reference
status: active
summary: The table of contents for one handbook audience — every page in this folder is registered here or it does not exist.
keywords: [handbook, index, pages, audience]
level: project
created: 2026-09-21
updated: 2026-09-21
---

# Handbook Index

**Belongs here**: pages describing the product AS IT CURRENTLY IS, in this audience's voice — one page per logical unit, each registered in the table below with a one-line description. A page not listed here doesn't exist. Page format, audience voice, and the mandatory discovery pass before creating or amending anything are the rules of this handbooks area itself.

**Does NOT belong here**: what CHANGED (the tracker and commit history own that), framework defaults and stock conventions, rules written for agents rather than humans (→ `../../information/`), plans and research (→ `../../plans/`, `../../researches/`), and secrets or internal-only URLs — handbooks are a shareable surface.

`sources` is the discovery key: it names the code paths a page documents, so a changed path finds its page with one grep.

| item | sources | what it covers | status | updated |
|---|---|---|---|---|
| [[capture-pipeline]] | `scripts/capture.py`, `hooks/hooks.json`, `docs/TELEMETRY-CONTRACT.md` | Stop/SubagentStop capture hook: never-break-a-session guarantee, cursor/offset transcript tailing, lock ordering, migration post-conditions and the version hop chain, storage modes and central authority, mirror metadata semantics, sidecar attribution, project-name ladder resolution, pricing-at-query-time | active | 2026-09-21 |
