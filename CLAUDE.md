# agent-token-telemetry — orchestrator core rules

Greenfield repo (stack not yet chosen — record it here on first commit). Telemetry for AI-agent token usage: collection, storage, reporting. Shell: plain zsh on macOS, no env preamble required yet. No dev stack/ports yet — record the command + ports here when one exists. Long-form docs live in `docs/`. Tracker: JIRA, project **AgentOS** (key AOS) at zenosyne.atlassian.net — modern scrum template (Epic/Story/Task/Bug/Idea); current work: epic AOS-1. (Migrated from Linear ZEN 2026-08-03.)

## Model-tier dispatch (MANDATORY)

Orchestrator (Claude Fable 5 · `claude-fable-5`) plans, decomposes, briefs, sequences, verifies — never bulk-implements. Route to the cheapest capable tier:

- **Orchestrator inline**: architecture/ADRs, security-critical design, irreversible ops, QA sign-off, brief authoring, conflict resolution.
- **Claude Opus 4.8 subagent** (`claude-opus-4-8`): only after two Sonnet failures, or cross-cutting debugging with no clear repro (state why in the brief).
- **Claude Sonnet 5 subagent** (`claude-sonnet-5`, default): features, fixes, validators, tests, QA sweeps, imports, docs.
- **Claude Haiku 4.5 subagent** (`claude-haiku-4-5-20251001`, "ponytail"): mechanical zero-discretion micro-tasks → `docs/agents/ponytail.md`.

Escalate after two failures; de-escalate when work turns mechanical.

## Task lifecycle (per tracker task)

build (worker) → **validate** (fresh agents, never the builder → `docs/agents/validation-agent.md`) → **document** (worker → `docs/agents/documentation-agent.md`) → close the tracker issue with commit refs.

## Rules cascade

Keep context lean: load a reference ONLY when performing that activity, and cite it in the sub-agent brief instead of inlining its content.

- Writing any agent brief → `docs/agents/briefing.md`
- Validating done work (BA + security personas, E2E script) → `docs/agents/validation-agent.md`
- Documenting after a done task → `docs/agents/documentation-agent.md`
- Creating/updating tracker issues → `docs/agents/ticket-filing.md` (defers to the in-tracker "Issue Intake & Triage Guide")
- Micro-tasks → `docs/agents/ponytail.md`

## Standing rules

- **Attribution: none.** Commits, PRs, docs, and code comments carry NO AI attribution of any kind.
- Integration-verify at the real boundary: cold-boot the composed/dev stack for milestone-sized work; API-level checks (curl) are NOT browser E2E — browser-smoke any web-facing change.
- Real bugs → `docs/issue-log.md` AND the tracker per `docs/agents/ticket-filing.md`.
- Conventions that bite: (none yet — grow this list with project-specific hard-won rules, each with the incident reference that earned it.)
