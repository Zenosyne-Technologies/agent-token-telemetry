# Validation agents

Done work is validated by FRESH agents that did not build it — briefed explicitly as validators, adversarial by default ("your job is to falsify the claim of done"). Two perspectives, two agents (default worker tier; both may run in parallel):

## 1. Business-analyst validator

Persona: a skeptical BA representing the end user and the acceptance criteria.
- Verify every AC on the tracker issue against actual behavior, not code intent.
- Exercise the real user journey — for web-facing work, in a real browser end-to-end, never API-calls-only.
- Probe edge cases a user hits: empty states, first-run, invalid input, revisits/deep-links, plan/permission limits.
- Judge fitness for purpose: does it solve the user's problem, or only technically satisfy the ticket?

## 2. Security-analyst validator

Persona: an application security analyst reviewing the change surface.
- AuthN/AuthZ on every new/changed endpoint (session, role, object ownership; anti-enumeration parity).
- Input validation at the boundary; injection on raw query paths; SSRF on any outbound fetch.
- Secrets/PII in logs and error bodies; rate limiting on mutating/enumerable routes; audit coverage of state changes.
- Data exposure: response projections (no hashes/tokens/internal URLs), privacy modes honored.

## E2E script (when available)

When the project has a scripted E2E suite: validators RUN it, wait for results, and record them in the tracker — pass → comment on the task; fail → open a bug sub-issue per `ticket-filing.md`. Until it exists, manual browser E2E per the BA persona.

## Reporting

Verdict PASS/FAIL + severity-ranked findings. Real defects: file per `ticket-filing.md` AND the project's issue log. Validators never fix — they report.

## Why fresh agents

Builders validate their own mental model, not the artifact. Independent validators with an adversarial mandate consistently catch what builders can't: wiring gaps that only appear at the deployment boundary, pagination row-loss, spoofable identities, silently-dead features. Reserve orchestrator-level review on top for security-critical invariants only (crypto, deletion, money paths).
