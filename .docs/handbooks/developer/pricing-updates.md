---
doc: Pricing Updates
type: handbook
status: active
summary: How `pricing_update.py` refreshes the `pricing` table from Anthropic's published pricing page — case-insensitive table/column detection, minting only the rate in force today per listed version (with a stale-price warning for an expired intro with nothing else listed), the own-price-vs-estimate definitions, and the two narrow exceptions to the pricing table's immutability contract.
keywords: [pricing, pricing-update, parse_models, build_candidates, immutability, effective_from, in-force, estimated, family-default, ancestor-row, stale-price-warning]
level: project
audience: developer
module: pricing-update
sources: [scripts/pricing_update.py, commands/pricing-update.md, docs/TELEMETRY-CONTRACT.md, tests/pricing_golden.py]
related: ["[[capture-pipeline]]", "[[remote-read-parity]]"]
created: 2026-09-22
updated: 2026-09-23
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

## Mint only the rate in force today, per listed version

`build_candidates()` plans a specific prefix (`claude-<family>-<version>`, dots
as dashes, or the legacy alias prefixes) for **every** model version the page
lists, not only versions whose rate differs from the family default. For each
version it mints only the rate actually **in force on the run date**
(`in_force()`): an in-force `through <d>` intro (`d >= today`) is dated today;
an arrived `starting <d>` increase (`d <= today`) is dated `d`, same as
before; and the version's unconditional rate is minted, dated today, **only
when no conditional is in force for it** — otherwise a stale unconditional row
would override the rate actually charged. When a version lists several
unconditional rows (e.g. a long-context row), the first one on the page wins.
The `claude-<family>-` family default row takes the in-force rate of the
family's newest (first-listed) version, whether that version's own rate comes
from an unconditional or a conditional row — a family whose newest version is
mid-intro or mid-increase still gets an accurate family row.

**Stale intro, no other rate (`stale_intros()`).** When a version's *only*
listing is a `through <d>` intro that has already expired — no unconditional
row, no in-force `starting` — the page asserts no rate in force today.
Nothing is minted for that version, nor for its family default when it is the
newest (its events keep whatever rate was last recorded), and the run report
prints a `STALE-PRICE WARNING` line naming the family, version and the
intro's end date (`commands/pricing-update.md` carries the exact wording).
Before this rule, an expired intro with nothing else listed was silently
re-minted at today's date on every run — a real bug, since it kept
re-asserting a rate that was no longer being charged.

See `docs/TELEMETRY-CONTRACT.md` §Pricing table ("`pricing-update` mints only
the rate in force today" / "Known limitation — expired intro with no other
rate") for the exact rules; this section only orients a maintainer to the
functions that implement them (`in_force`, `build_candidates`,
`stale_intros`, `render`).

## Own price vs estimate

Every version now getting its own row means an event can price at that
model's **own** row, at its family's **default** row, or — for an unlisted
point release such as `claude-opus-5-5` while the page lists `claude-opus-5`
— at its nearest listed **ancestor's** row. The latter two are flagged
**estimated**: a reasonable stand-in cost, not necessarily that model's
published rate. `docs/TELEMETRY-CONTRACT.md` §Pricing table ("Own price vs
estimate") is authoritative on the four defined terms (family default row,
ancestor row, estimated event, model without own price) and their three
parallel implementations — Python (`capture.is_family_default` /
`is_ancestor_row` / `is_estimated`), SQLite (`capture.family_default_sql` /
`ancestor_row_sql` / `estimated_sql`) and the equivalent expression in
`supabase/reports.sql` — this page does not restate it.

The flag is exposed today by `report.py` (`fetch_project_stats`'s
`estimated_events`, `fetch_token_stats`'s `estimated_by_model` and
`models_without_own_price`), `dashboard.py` (`/api/data`: each event's and
group's `estimated`, `kpis.estimatedEvents`, `modelsWithoutOwnPrice`), and
`report_priced_events.estimated` on the remote (see [[remote-read-parity]]) —
but nothing renders it in a report or the dashboard UI yet; that is later
work, not landed here.

## Testing: the golden no-cost-change test

`tests/pricing_golden.py` records, per (page fixture or inline entry list,
run date), the pricing row every event in a name/timestamp matrix resolved to
under `build_candidates()` **before** this change — generated once, from
commit `4f8c215`, via `python3 tests/pricing_golden.py --generate` (rerun
only when a scenario is added; the baseline commit never moves).
`tests/test_pricing_update.py::TestGoldenPreAos133` replays the same
scenarios through the current code and fails on any resolved-row difference
that is not on its reviewed `ALLOWED_DIFFS` allow-list. Every allow-list entry
names a reason code (`ALLOWED_REASONS`, `a`–`e`): (a) an unlisted successor
now priced — and flagged estimated — at its nearest ancestor; (b) an in-force
`through` wins regardless of page order; (c) the first unconditional row for
a version wins over a later same-version row; (d) only the rate in force
today is minted (the bug fix above); (e) a legacy-alias version that used to
go unpriced now gets its own row. A diff outside that list fails the test —
the allow-list is the reviewed record of every place this change is allowed
to move a cost, and nothing else. See [[remote-read-parity]] for the
Postgres-gated tests that guard the estimated flag's SQLite/Postgres
expressions against their Python definition.

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
