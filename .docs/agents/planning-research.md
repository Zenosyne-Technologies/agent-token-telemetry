# Planning research (plan validation + solution research)

Applies when planning sizes a task `size:l` or `size:xl` (per `label-syntax.md`). Tasks `size:m` and below get NO dedicated research pass — build them directly.

For each qualifying task, the planner dispatches two research passes, in order, BEFORE the build brief:

1. **Plan-validation research** — a fresh agent adversarially checks the plan against the actual codebase: hidden dependencies, breaking-change surface, wrong assumptions, missing acceptance criteria, sequencing risks.
2. **Solution research** — after validation findings are reconciled into the plan: research implementation approaches — viable options with trade-offs, a recommended approach with reasons, and references (code, docs, prior art).

Both passes search the CODE and the PM TOOL: `git log`/`git blame` the touched files and methods for issue keys in earlier commits (commit messages start with their key), fetch those issues, and read their comments — prior findings and solutions often answer current questions. Cite the relevant issues in Refs.

Tier routing (mandatory — by the task's `size:` label):

| Size | Researcher tier |
|---|---|
| `size:xl` (very complex) | Claude Opus 4.8 (claude-opus-4-8) |
| `size:l` (mid complexity) | Claude Opus 4.8 (claude-opus-4-8) |
| `size:m` and below | no research pass |

Reporting — findings live where the plan lives, both passes alike:
- Issue-tracked plan → a comment on the issue: `## Findings / ## Risks / ## Recommendation / ## Refs`.
- Otherwise → an md doc at `docs/research/<issue-key-or-slug>-{validation|solution}.md`, linked from the tracker issue if one exists.

Briefs follow `briefing.md`; FINAL MESSAGE (machine-consumed): verdict (`plan-ok` | `plan-gaps: <n>` for validation; `recommendation: <one line>` for solution) + the comment URL or doc path. The planner reconciles findings into the plan before dispatching the build — research that isn't folded back in is waste.
