# Ponytail — micro-model task profile

For mechanical, low-blast-radius work where the orchestrator (or a worker agent) already made every decision. The micro-model agent executes; it does not judge.

Eligible: filing/updating tracker issues from prepared payloads (per `ticket-filing.md`); label/metadata/data entry; log greps with an exact pattern → count/extract report; lint/format-only fixes; single-file edits with the exact diff described; doc typo passes.

Not eligible: anything needing judgment, multi-file edits, security-adjacent code, user-visible copywriting from scratch.

Brief template (≤15 lines + payload):
```
Task: <one sentence>.
Tools: <exact tool names; ONE tool-search call if deferred>.
Input: <the prepared payload, verbatim>.
Do: <numbered mechanical steps, incl. list-before-create idempotency>.
FINAL MESSAGE: <exact format>. Nothing else.
```

If the agent would need to ask a question, the task was mis-tiered — pull it back to the default worker.
