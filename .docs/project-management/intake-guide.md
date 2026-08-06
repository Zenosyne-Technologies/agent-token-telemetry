# Issue Intake & Triage Guide — AgentOS (AOS)

Authoritative filing workflow for Jira project **AgentOS** (site
zenosyne.atlassian.net, key AOS). Formerly lived in the description of seed
issue AOS-11 (Confluence is unreachable for this app token); this repo doc is
now the authoritative home — the tracker issue points here. On label DEFINITIONS
the versioned registry `.docs/agents/label-syntax.md` wins; this guide owns the
filing workflow.

## Label taxonomy

**Type** (one required per item): `type:feature` · `type:bug` ·
`type:change-request` · `type:investigation` · `type:tech-debt`

**Area** (one required per item): `area:collection` · `area:storage` ·
`area:reporting` · `area:infra` · `area:docs`

**Severity** (defects only): `sev1-critical` (data loss, security, app
unusable) · `sev2-high` (feature broken, no workaround) · `sev3-medium`
(workaround exists, or cosmetic-but-functional) · `sev4-low` (polish)

**Size** (stories/tasks, at planning time; mirrored to Story Points per
tracker-config): `size:xs` · `size:s` · `size:m` · `size:l` · `size:xl`

**Origin** (one required per item): `origin:user-request` ·
`origin:architect-request` · `origin:agent-qa` · `origin:agent-dev` ·
`origin:roadmap`

## Hierarchy

The kit targets 4 levels: **milestone → epic → story → task**. Epic = native
Jira Epic; Story = native Story; Task/Sub-task = implementation step.
**Milestones are virtual**: a `milestone:<slug>` label on every epic of the
milestone, queried via JQL `labels = "milestone:<slug>"` — encoded ONLY in that
label so it converts losslessly to a release (fixVersion) if the connector
gains release creation.

## Filing rules

- Labels follow `.docs/agents/label-syntax.md` (versioned registry). EVERY item
  created or edited gets one label per required dimension; agents touching an
  unlabeled item backfill from its description.
- Native issue type **Bug** for `type:bug` (fall back to Task if unavailable).
- Labels are canonical; mirror native Priority from sev (sev1→Highest,
  sev2→High, sev3→Medium, sev4→Low; JSM Impact likewise if present). On
  conflict, the label wins.
- File in project AOS, status To Do (this workflow has no Backlog status).
- ALWAYS search for duplicates (JQL) before filing.
- Description template: `## Repro / ## Expected / ## Actual / ## Evidence /
  ## Suspected cause / ## Refs`. Change requests: current vs desired behavior
  plus acceptance criteria. Feature/story items: `## Scope / ## DoD`.
- QA sweeps: one tracking issue titled "QA sweep — <scope> <date>"; findings
  filed as separate linked issues.
