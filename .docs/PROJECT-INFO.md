---
project: agent-token-telemetry
description: Claude Code plugin recording per-turn/per-subagent token usage telemetry into a central SQLite database, zero model-token overhead (capture runs in Stop/SubagentStop hooks).
owner: Emprove Services Kft.
pm_tool: jira
tracker_coordinates: site zenosyne.atlassian.net, project AgentOS (key AOS)
project_key: AOS
hierarchy_levels: 3/4 (virtual milestones — epics carry a milestone:<slug> label)
intake_guide_url: https://zenosyne.atlassian.net/browse/AOS-11
stack: Python 3 (stdlib only) + JSON manifests + markdown slash commands
dev_command: none (no dev stack/ports); tests: python3 -m unittest tests.test_capture -v
docs_location: docs/ (long-form docs, issue log, superpowers specs/plans); .docs/agents/ (kit rules cascade)
kit_version: 0.11.0
label_syntax_version: 1.2.0
---

# agent-token-telemetry — project information

Meta overview for foreign agents, agentic OS frameworks, and reporting tools. The YAML frontmatter above is the machine contract and the source of truth for facts; this body is the human overview. Any agent that changes a fact below updates the frontmatter in the same change. Facts only — operating rules live in `CLAUDE.md` and `.docs/agents/`.

- Repository layout: single repo — `scripts/capture.py` (capture), `commands/` (slash commands: enable/disable/token-stats), `hooks/hooks.json` (Stop/SubagentStop wiring), `tests/` (unittest), `docs/` (long-form docs, issue log, superpowers specs/plans), `.docs/agents/` (kit rules cascade).
- Hierarchy details, virtual milestones, severity/size native mappings: `.docs/agents/tracker-config.md`
- Label registry: `.docs/agents/label-syntax.md` · Filing rules: `.docs/agents/ticket-filing.md`
- Operating rules: `CLAUDE.md` + the `.docs/agents/` rules cascade
