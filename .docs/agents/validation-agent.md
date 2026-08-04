# Validation agents

Done work is validated by FRESH agents that did not build it — briefed explicitly as validators, adversarial by default ("your job is to falsify the claim of done"). Two perspectives, two agents (heavy worker tier), run in SEQUENCE — completion first, security only after completion passes; security review never runs on work that is not done:

## Stage 1 — Completion validator (business-analyst persona), FIRST after build

Persona: a skeptical BA representing the end user and the acceptance criteria.
- Verify the issue's DoD and every AC against actual behavior, not code intent — each DoD item gets an explicit pass/fail in the verdict.
- Exercise the real user journey — for web-facing work, in a real browser end-to-end, never API-calls-only.
- Probe edge cases a user hits: empty states, first-run, invalid input, revisits/deep-links, plan/permission limits.
- Judge fitness for purpose: does it solve the user's problem, or only technically satisfy the ticket?
- Verdict FAIL → back to the builder; Stage 2 does not run.

## Stage 2 — Security validator (application-security persona), only after Stage 1 passes

Persona: an application security analyst reviewing the change surface.
- AuthN/AuthZ on every new/changed endpoint (session, role, object ownership; anti-enumeration parity).
- Input validation at the boundary; injection on raw query paths; SSRF on any outbound fetch.
- Secrets/PII in logs and error bodies; rate limiting on mutating/enumerable routes; audit coverage of state changes.
- Data exposure: response projections (no hashes/tokens/internal URLs), privacy modes honored.

## E2E script (when available)

When the project has a scripted E2E suite (Playwright or equivalent): validators RUN it, wait for results, and record them in the tracker — pass → comment on the task; fail → open a bug sub-issue per `ticket-filing.md`. Until it exists, the validator drives the browser directly (browser tools) per the BA persona — API-level checks are never browser E2E.

## Reporting

Verdict PASS/FAIL + severity-ranked findings. Real defects: file per `ticket-filing.md` AND the project's issue log. Validators never fix — they report.

## Milestone validation

At milestone close, validation is led by the orchestrator (Claude Fable 5) working WITH Claude Sonnet 5 sub-agents: the orchestrator plans the sweep (integration boundaries, cross-task user journeys, DoD roll-up across the milestone's issues) and dispatches the checks; sub-agents gather the evidence, the orchestrator judges it and signs off before the milestone branch merges. Task-level stages are not re-run — milestone validation tests the composition.

## Why fresh agents

Builders validate their own mental model, not the artifact. Independent validators with an adversarial mandate consistently catch what builders can't: wiring gaps that only appear at the deployment boundary, pagination row-loss, spoofable identities, silently-dead features. Beyond the milestone sweep above, reserve extra orchestrator-level review at TASK level for security-critical invariants only (crypto, deletion, money paths).
