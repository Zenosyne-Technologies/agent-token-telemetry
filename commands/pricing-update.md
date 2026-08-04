---
description: Refresh the pricing table from Anthropic's currently published rates
allowed-tools: Bash(sqlite3:*), WebFetch
---

Keep the telemetry DB's `pricing` table current. There is no official pricing API —
read Anthropic's published pricing/docs pages carefully and do not guess a number that
isn't stated.

1. List the model families actually in use: `sqlite3 ~/.claude/telemetry/usage.db
   "SELECT DISTINCT name FROM models;"`. If the DB does not exist, tell the user and
   stop.
2. Fetch Anthropic's current published per-million-token pricing (input, output, cache
   read, cache write) for each family present — the models/pricing pages on
   anthropic.com and docs.anthropic.com. Note the source URL you read each rate from.
3. For each family, compare the fetched rates to the row with the greatest
   `effective_from` for that `provider`+`model_prefix` in the `pricing` table:
   `sqlite3 -header -column ~/.claude/telemetry/usage.db "SELECT * FROM pricing WHERE
   provider='anthropic' AND model_prefix=? ORDER BY effective_from DESC LIMIT 1;"`.
4. On any rate change, **INSERT a new row** with today's date as `effective_from`
   (`strftime('%s','now','start of day')`) and the source URL — **never UPDATE or
   DELETE** an existing pricing row; history must stay queryable exactly as it was
   priced at the time. Use `INSERT OR IGNORE` (unique key is
   `provider, model_prefix, model_version, effective_from`) so a same-day rerun is a
   no-op.
5. A model family with no matching prefix on the pricing pages is unpriced — report it
   as unpriced, do not fabricate a rate or reuse a different family's number.
6. Report a table: model prefix, old rate → new rate (or "unchanged", or "unpriced"),
   source URL, effective date. Nothing else.
