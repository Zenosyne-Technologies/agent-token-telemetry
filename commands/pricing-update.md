---
description: Refresh the pricing table from Anthropic's currently published rates
allowed-tools: Bash(sqlite3:*), WebFetch
---

Keep the telemetry DB's `pricing` table current. There is no official pricing API —
read Anthropic's published pricing page carefully and do not guess a number that
isn't stated.

1. Confirm the DB exists (`~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB`);
   if not, tell the user and stop.
2. Fetch Anthropic's current published per-million-token pricing (input, output, cache
   read, cache write) from **https://platform.claude.com/docs/en/about-claude/pricing**
   (follow its links for older-model rates when listed elsewhere). Collect rates for
   **ALL model families published there** — not just models a project has used; the
   table prices future usage too. Note the exact source URL per rate.
3. For each fetched family, compare against the row with the greatest `effective_from`
   for that `provider`+`model_prefix`: `sqlite3 -header -column
   ~/.claude/telemetry/usage.db "SELECT * FROM pricing WHERE provider='anthropic' AND
   model_prefix=? ORDER BY effective_from DESC LIMIT 1;"` (no row → it is a new family).
4. INSERT a new row (today as `effective_from`, `strftime('%s','now','start of day')`,
   plus the source URL) when: the rate changed, OR the family has no row yet, OR the
   family's only rows are the undated seed (`effective_from = 0`) — the first run after
   install must replace the seed with dated rows **regardless of whether the numbers
   differ**. **Never UPDATE or DELETE** an existing pricing row; history must stay
   queryable exactly as it was priced at the time. Use `INSERT OR IGNORE` (unique key
   `provider, model_prefix, model_version, effective_from`) so a same-day rerun is a
   no-op.
5. A model family that exists in the DB's `models` table but has no matching prefix on
   the pricing page is unpriced — report it as unpriced, do not fabricate a rate or
   reuse a different family's number.
6. Report a table: model prefix, old rate → new rate (or "unchanged", "new", or
   "unpriced"), source URL, effective date. Nothing else.
