---
doc: Pricing Updates
type: handbook
status: active
summary: How `pricing_update.py` refreshes the `pricing` table from Anthropic's published pricing page — case-insensitive table/column detection, the never-mint-a-future-dated-row rule, and the two narrow exceptions to the pricing table's immutability contract.
keywords: [pricing, pricing-update, parse_models, build_candidates, immutability, effective_from]
level: project
audience: developer
module: pricing-update
sources: [scripts/pricing_update.py, commands/pricing-update.md, docs/TELEMETRY-CONTRACT.md]
related: ["[[capture-pipeline]]"]
created: 2026-09-22
updated: 2026-09-22
---

# Pricing Updates

`scripts/pricing_update.py` (run by `/token-telemetry:pricing-update`, whose
command surface is `commands/pricing-update.md`) fetches Anthropic's published
pricing page, parses its model pricing table, and diffs the result against the
`pricing` table's existing rows. Two functions carry the logic that matters to
a maintainer: `parse_models()` turns the raw HTML into rate entries, and
`build_candidates()` turns those entries into the dated rows the script
inserts.

## Case-insensitive table and column detection

`parse_models()` finds the pricing table on the page by a predicate over its
header row, and then locates each of its five columns (`in`, `w5`, `w1h`,
`cr`, `out`) the same way — both match by lower-casing the header cell before
comparing it against the needle (`"base input tokens"`, `"5m cache"`, `"1h
cache"`, `"cache hits"`, `"output"`). Anthropic's page has shipped the same
columns under both Title Case ("Base Input Tokens") and sentence case ("Base
input tokens"); a case-sensitive match failed to find the table at all the
moment the casing changed on a page that was otherwise unchanged, and the
script exited 2 (its fetch/parse-failure code) with nothing actually wrong
with the data.

The guard stays strict where it matters: `if set(col) != {"in", "w5", "w1h",
"cr", "out"}: raise ValueError(...)` still fires when a column is genuinely
absent from the header. Case-insensitivity only widens what counts as a
match for a column that IS there — it never suppresses the check that all
five were found.

## Never mint a future-dated row

Each conditional row `parse_models()` extracts is either `through <date>` (an
introductory rate currently in force) or `starting <date>` (a scheduled
increase Anthropic has announced but that has not taken effect yet).
`build_candidates()` dates a `through` row `effective_from = today`, but a
`starting` row is skipped outright — `if kind == "starting" and date > today:
continue` — until a run of the script happens on or after that date.

The reason is what `effective_from` means to every consumer: it is a claim
that the rate was actually in force from that date forward. Minting a
`starting`-dated row before its date arrives would record a forecast as if it
were a charged rate, and if Anthropic later moved or withdrew that date, every
query in between would already have priced events against a rate that never
took effect. Recording the row only once its date has actually arrived means
every row this script writes started life as an already-effective rate, never
a prediction.

## Immutability and its two DELETE exceptions

`docs/TELEMETRY-CONTRACT.md`'s "Pricing table" section is authoritative on
the pricing table's shape and its default immutability (`INSERT OR IGNORE`
only, rows never `UPDATE`d or `DELETE`d) — this page does not restate it.

The one addition worth a maintainer's attention: a pricing row may be
`DELETE`d in exactly two narrow cases — (1) it is future-dated and not yet in
effect (`effective_from > now`), or (2) it recorded a `starting <date>`
forecast, written before its date, that the publisher subsequently withdrew.
Both share the same justification: neither row ever priced a real charge, so
removing it corrects the table rather than rewriting history. Every row whose
`effective_from <= now` — one that was, at some point, the rate actually
charged — stays immutable regardless.

Because `build_candidates()` never mints a `starting` row ahead of its date
(above), case (2) cannot arise from this script's own normal run — it is only
reachable through the manual fallback that records a forecast by hand before
this script would have. See `docs/TELEMETRY-CONTRACT.md` for the exact
`effective_from`/`now` boundary and the `effective_from = 0` seed-row caveat.
