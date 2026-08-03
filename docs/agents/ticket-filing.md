# Filing tracker issues

Tracker: **JIRA project AgentOS (key AOS)** at zenosyne.atlassian.net (modern scrum template). Until a JIRA/Confluence intake guide exists, THIS file is authoritative. (The former Linear "Issue Intake & Triage Guide" is superseded — Linear is no longer the tracker as of 2026-08-03.)

Non-negotiables:
- Project AOS; new issues → To Do (backlog); attach to the relevant Epic via parent (current work: AOS-1).
- Type mapping (was `type:*` labels in Linear): bug → **Bug** · change request/feature → **Story** (or **Task** for non-user-facing work) · investigation → **Task** · tech-debt → **Task**; exploratory proposals → **Idea**.
- Severity → **priority**: sev1 data-loss/security/app-unusable → Highest · sev2 feature broken, no workaround → High · sev3 workaround exists or cosmetic-functional → Medium · sev4 polish → Low.
- Provenance + area as JIRA **labels**: `found-by-agent-qa` | `found-by-owner`, plus one of `area-collection` | `area-storage` | `area-reporting` | `area-infra` | `area-docs`.
- Search for duplicates BEFORE filing (JQL on project = AOS).
- Description template: `## Repro / ## Expected / ## Actual / ## Evidence / ## Suspected cause / ## Refs`. Stories: current vs desired behavior + acceptance criteria.
- QA sweeps: one tracking Task ("QA sweep — <scope> <date>"), findings filed as linked issues.
- **Milestones = Epics** (JIRA releases are not usable via the current MCP — only Epic/Story/Task structure is). One epic per milestone-sized outcome, milestone name in the epic title (current: AOS-1 "v0.1 MVP"). **Stories** carry the user-facing outcomes and acceptance criteria; **Tasks** carry implementation work. Story and Task are the same hierarchy level, so tasks sit beside stories under the epic and are wired with `Blocks` links: task blocks its story; verification tasks are blocked by the stories they verify. Epic done = all stories accepted + milestone-sized integration verification (per CLAUDE.md standing rules).

Filing with fully-prepared content is ponytail (micro-model) work; drafting content from raw findings is default-worker work.
