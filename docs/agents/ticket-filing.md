# Filing tracker issues

Authoritative rules live in the tracker document **"Issue Intake & Triage Guide"** (https://linear.app/zenosyne/document/issue-intake-and-triage-guide-6f69648c5d81). Every brief that has an agent create or update issues MUST tell the agent to fetch and follow that document.

Non-negotiables (mirror of the guide — the guide wins on conflict):
- Team Zenosyne (ZEN) + project "Agent Token Telemetry"; new issues → Backlog.
- Labels: exactly one `type:*` (bug | change-request | investigation | tech-debt), one `area:*`, one `sev1..sev4`, one `found-by:*` (agent-qa | owner).
- Severity: sev1 data-loss/security/app-unusable · sev2 feature broken, no workaround · sev3 workaround exists or cosmetic-functional · sev4 polish.
- Search for duplicates BEFORE filing.
- Description template: `## Repro / ## Expected / ## Actual / ## Evidence / ## Suspected cause / ## Refs`. Change requests: current vs desired behavior + acceptance criteria.
- QA sweeps: one tracking issue ("QA sweep — <scope> <date>"), findings filed as related issues.

Filing with fully-prepared content is ponytail (micro-model) work; drafting content from raw findings is default-worker work.
