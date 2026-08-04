# Agent brief discipline

Every sub-agent brief includes, in this order:

1. **Env preamble**: the project's shell prefix/env requirements, container naming + ports if used (remove containers when done).
2. **Exact scope**: file paths and *sections* to read — never "explore the repo". Scoped commands (per-package filters, targeted test files); full suite only for the agent that owns the whole tree at commit time.
3. **DoD**: the task's definition of done, copied from the tracker issue — verifiable statements the agent works to; the FINAL MESSAGE reports each DoD item met or missed.
4. **Security surface**: the task's security-sensitive surfaces (auth, input-validation boundaries, data exposure, secrets touched, dependency changes), citing `security.md`; no secrets in code, commits, comments, or reports — ever.
5. **Ownership boundary** (concurrent agents): the paths this agent owns; selective `git add <paths>` only, never `-A`.
6. **Branch + autocommit**: the milestone branch to work on (`milestone/<slug>`); the agent commits its own scoped work itself (atomic commits, selective add), messages starting with the tracker issue key the brief names (`<KEY>: …`) before its final message — work is never left uncommitted, and the agent never asks permission to commit.
7. **Idempotency** (create/import tasks): list-before-create, skip existing — retries and resumes become free.
8. **"Work synchronously, no sub-agents."**
9. **FINAL MESSAGE spec**: machine-consumed exact format. The orchestrator parses only that — never reads transcripts (context blowout).
10. **Attribution policy** as configured in the core rules.

**No mid-run policy changes.** Agents rightly treat instructions that reverse their original brief mid-run as possible prompt-injection and may refuse. Put policy in the original brief; if policy changes while an agent runs, let it finish per its brief and reconcile afterward (amend the commit, correct the record).

Ponytail (micro-model) briefs: ≤15 lines + prepared payload, one task, exact input → exact output, zero discretion. If it needs clarification, re-tier to the small worker.
