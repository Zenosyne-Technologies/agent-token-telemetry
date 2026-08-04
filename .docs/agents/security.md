# Security discipline

Applies to EVERY task that touches auth, input boundaries, data exposure, secrets, or dependencies — cite this file in those briefs.

## Secrets

- Never commit secrets: no keys, tokens, passwords, `.env` files, or live connection strings in code, fixtures, or docs — env wiring plus `.env.example` placeholders only.
- Never paste secrets into the PM tool: issue comments, report snapshots, tracker docs, and PR bodies are shared surfaces — scrub command output and logs before posting (the comment-discipline and reporting rules write agent output there).
- A leaked secret is a sev1 incident: rotate FIRST, then file per `ticket-filing.md`.

## Dependencies

- Adding or upgrading a dependency is never ponytail work: size it `m` or larger, name it in the brief.
- Check advisories before adopting (the stack's native tooling: npm audit / pip-audit / osv-scanner equivalents); pin exact versions.
- Every new or upgraded dependency is listed in the FINAL MESSAGE.

## Surfaces

- Build briefs name the task's security surface (`briefing.md` ingredient) — builders treat listed surfaces as constraints, not commentary.
- Security-critical design (crypto, deletion, money paths, authZ models) stays orchestrator-inline — never delegated.
- Validation is two-stage (`validation-agent.md`): completion first, security second — security review never runs on incomplete work.
