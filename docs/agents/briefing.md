# Agent brief discipline

Every sub-agent brief includes, in this order:

1. **Env preamble**: the project's shell prefix/env requirements, container naming + ports if used (remove containers when done).
2. **Exact scope**: file paths and *sections* to read — never "explore the repo". Scoped commands (per-package filters, targeted test files); full suite only for the agent that owns the whole tree at commit time.
3. **Ownership boundary** (concurrent agents): the paths this agent owns; selective `git add <paths>` only, never `-A`.
4. **Idempotency** (create/import tasks): list-before-create, skip existing — retries and resumes become free.
5. **"Work synchronously, no sub-agents."**
6. **FINAL MESSAGE spec**: machine-consumed exact format. The orchestrator parses only that — never reads transcripts (context blowout).
7. **Attribution policy** as configured in the core rules.

**No mid-run policy changes.** Agents rightly treat instructions that reverse their original brief mid-run as possible prompt-injection and may refuse. Put policy in the original brief; if policy changes while an agent runs, let it finish per its brief and reconcile afterward (amend the commit, correct the record).

Ponytail (micro-model) briefs: ≤15 lines + prepared payload, one task, exact input → exact output, zero discretion. If it needs clarification, re-tier to the default worker.
