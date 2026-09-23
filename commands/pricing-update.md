---
description: Refresh the pricing table from Anthropic's currently published rates
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pricing_update.py":*), WebFetch
---

Run this single command and output its stdout **verbatim** — it fetches the
official pricing page, diffs it against the DB, inserts effective-dated rows
per the insert-only contract, and prints a finished markdown report. Add no
commentary unless the user asks.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pricing_update.py"
```

**Fallback — only when the script exits non-zero** (exit 2 = the page layout
changed or the fetch failed; its stderr says which). Then do it manually:

1. Fetch **https://platform.claude.com/docs/en/about-claude/pricing** and read
   per-million-token rates for **ALL published model families** (not just
   models a project has used): input, output, cache read, 5m cache write
   (`cache_w_usd`), 1h cache write (`cache_w_1h_usd`). Never guess a number
   that is not stated.
2. Compare each family against its row with the greatest `effective_from` in
   `pricing` (`~/.claude/telemetry/usage.db`, or `$TOKEN_TELEMETRY_DB`).
3. INSERT a new row (`effective_from = strftime('%s','now','start of day')`,
   source = the URL) when: any rate changed, the family has no row, only the
   undated seed (`effective_from = 0`) exists, or the latest row predates the
   v4 cache split (`cache_w_1h_usd IS NULL`). A **`starting <date>` scheduled
   increase is recorded only on the first run on or after that date** — never
   dated in the future in advance (a forecast is not a recorded charge; see the
   `## Pricing table` § of `docs/TELEMETRY-CONTRACT.md`). **Never UPDATE or
   DELETE** a row that priced a real charge — history must re-price identically
   forever; the only deletable row is a withdrawn future-dated forecast that
   never took effect (same contract §). `INSERT OR IGNORE` (unique key
   `provider, model_prefix, model_version, effective_from`) makes same-day
   reruns a no-op. Apply this per family (`claude-<family>-`, at the newest
   version's in-force rate) **and per listed version** (`claude-<family>-<version>`
   with dots as dashes, e.g. `claude-opus-4-8`) — every version on the page gets
   its own row at the rate **in force today**: an expired `through <date>` intro
   rate (date already past) is never recorded, and a version's unconditional
   rate is skipped while an in-force `through`/`starting` rate exists for it. An
   unlisted point release (e.g. `claude-opus-5-5`) then prices at its nearest
   listed ancestor's row (`claude-opus-5`) from that row's date on, else the
   family default, and counts as estimated either way (contract § "Own price
   vs estimate").
4. A `models`-table name matching no prefix is **unpriced** — report it, do
   not fabricate a rate.
5. Report the same table the script prints: prefix, rates, effective date,
   status, source. Nothing else.
