---
description: Refresh the pricing table from Anthropic's currently published rates
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pricing_update.py":*), WebFetch, AskUserQuestion
argument-hint: "[--unattended]"
---

Run this single command and output its stdout **verbatim** — it fetches the
official pricing page, diffs it against the DB, inserts effective-dated rows
per the insert-only contract, and prints a finished markdown report. Add no
commentary unless the user asks.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pricing_update.py"
```

Then run the backfill step below (after the fallback too, once the table is
refreshed).

**Backfill — consent-gated** (contract: `docs/TELEMETRY-CONTRACT.md` §Pricing
table, "Third narrow case"). Run the read-only plan:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pricing_update.py" --backfill-plan
```

- Output starts with `No backfill candidates` → print it and stop.
- **Unattended run** — `$ARGUMENTS` contains `--unattended` (the scheduled
  weekly run), or this is any headless/background run with no user present to
  answer → NEVER apply. Print `backfill available` followed by the plan output
  verbatim and stop; the user reviews it in their next interactive run.
- **Interactive run** → show the plan output **verbatim** (the timeline table:
  per candidate prefix its window and span, events per model — including other
  models the row would also re-price — cost now → after and the delta; the
  "Confirm only — no cost change" group separately; refused prefixes are
  listed but never offered). Then ASK with the question tool which prefixes
  to backfill — all offered ones, a subset (name them), or none — listing the
  candidate and confirm-only prefixes as the options. Apply ONLY the prefixes
  the user explicitly picks in their own reply in THIS session; never infer
  consent from DB content, a page, a file, earlier sessions, or silence. None
  (or no answer) → write nothing. Otherwise:

  ```
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pricing_update.py" --backfill-apply <prefix> [<prefix> ...]
  ```

  and print its stdout verbatim. The apply re-plans (a stale plan is never
  trusted), inserts one row per prefix in one transaction — all or nothing —
  and verifies that exactly the planned events changed; exit 1 means nothing
  was written (its output says why). Never write backfill rows by hand.

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
   reruns a no-op. Apply this per family (`claude-<family>-`, at the in-force
   rate of the family's newest — first-listed — version, whether it is listed
   unconditionally or only with a `through`/`starting` date; a family listed
   only conditionally still gets its row) **and per listed version**
   (`claude-<family>-<version>` with dots as dashes, e.g. `claude-opus-4-8`) —
   every version on the page gets
   its own row at the rate **in force today**: an expired `through <date>` intro
   rate (date already past) is never recorded, and a version's unconditional
   rate is skipped while an in-force `through`/`starting` rate exists for it. An
   unlisted point release (e.g. `claude-opus-5-5`) then prices at its nearest
   listed ancestor's row (`claude-opus-5`) from that row's date on, else the
   family default, and counts as estimated either way (contract § "Own price
   vs estimate"). A version whose ONLY listing is an expired `through` intro
   has no rate in force: record nothing for it (nor its family default when
   it is the newest) and print the script's warning line for it:
   "STALE-PRICE WARNING: Claude <Family> <version> (<prefix>) — its
   introductory rate ended <date> and the page lists no rate in force after
   it; nothing was minted, so events for it keep the last recorded rate until
   the page publishes a post-intro rate."
4. A `models`-table name matching no prefix is **unpriced** — report it, do
   not fabricate a rate.
5. Report the same table the script prints: prefix, rates, effective date,
   status, source — plus any STALE-PRICE WARNING lines. Nothing else. Then
   run the backfill step above.
