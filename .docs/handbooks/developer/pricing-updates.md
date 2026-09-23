---
doc: Pricing Updates
type: handbook
status: active
summary: How `pricing_update.py` refreshes the `pricing` table from Anthropic's published pricing page — case-insensitive table/column detection, minting only the rate in force today per listed version (with a stale-price warning for an expired intro with nothing else listed), the own-price-vs-estimate definitions, the dashboard's own-price warning banner (AOS-135) that now surfaces them with its node-gated client tests, the consent-gated backfill of estimated events, and the narrow exceptions to the pricing table's immutability contract.
keywords: [pricing, pricing-update, parse_models, build_candidates, immutability, effective_from, in-force, estimated, family-default, ancestor-row, stale-price-warning, dashboard, price-warning-banner, node-gated-tests, require-node, backfill, backfill-plan, backfill-apply]
level: project
audience: developer
module: pricing-update
sources: [scripts/pricing_update.py, commands/pricing-update.md, commands/schedule-pricing.md, docs/TELEMETRY-CONTRACT.md, tests/pricing_golden.py, scripts/dashboard.py, scripts/dashboard.html, tests/test_dashboard_client.py, tests/dashboard_dom_harness.js, tests/test_backfill.py]
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
`report_priced_events.estimated` on the remote (see [[remote-read-parity]]).
The dashboard's own-price warning banner (AOS-135 S3, below) is what finally
renders it to a user; report-level rendering is still later work.

## Own-price warning banner (dashboard, AOS-135 S3)

`dashboard.py`'s `fetch_price_warning()` takes `report.fetch_models_without_own_price()`'s
list and, per model, sums its all-time event count, first/last-seen
timestamps, and the cost its events resolve to under the same per-event
pricing resolution `fetch_rows()` uses (`_rate`/`_resolved`). `pricedEvents`
counts only the events that actually resolved to a pricing row (family
default or ancestor rate); the rest contribute `0` to the sum instead of
erroring, since by construction every event of these models is estimated or
unpriced, never own-priced.

`build_price_warning()` turns that per-model detail into the two-group
content the banner renders — the client does no arithmetic:

- **"Priced at an estimated rate"** — at least one event resolved to a
  pricing row. Shows the accrued cost. A *mixed* model (some events resolved,
  some didn't, because a resolving row landed after its earliest events)
  states both counts instead of silently summing the unpriced ones as `$0`,
  e.g. `"$2.70 for 1 priced event; 2 unpriced, not counted"`.
- **"No price at all"** — no event ever resolved to any pricing row (a
  non-Claude name, or a name matching no seeded family/ancestor prefix).
  Cost text reads `"not counted"`, never a misleading `"$0.00 estimated"`.

Both groups point at `/token-telemetry:pricing-update`. The banner is scoped
all-time, not the period filter (a model must stay listed even if its only
events fall outside the current window), and it hides entirely when every
logged model already has its own price — `build_price_warning()` returns
empty `estimated`/`unpriced` lists and the client never shows the box.

Two new `/api/data` keys carry this: `modelsWithoutOwnPriceDetail` is
`fetch_price_warning()`'s raw per-model rows; `priceWarning` is
`build_price_warning()`'s ready-to-render output. The older
`modelsWithoutOwnPrice` list (bare model names) is unchanged.

**Every string is formatted server-side.** Headings, event counts (`"1
event"` / `"N events"`), date ranges (`Mon D, YYYY`, or a `Mon D, YYYY – Mon
D, YYYY` range; `_warn_date()` renders `"unknown date"` instead of raising
when a timestamp can't convert to a local calendar date, so one bad event
timestamp can't fail the whole `/api/data` response), and cost text
(`_warn_usd()`, built to match `dashboard.html`'s own `fmtUSD` character for
character) are all resolved in `dashboard.py`. `renderPriceWarning()` in
`dashboard.html` does no arithmetic or string formatting — only
`createElement`/`textContent` DOM construction, the same defense the rest of
the page's tables use, applied here because a model name is
attacker-controlled: nothing this banner writes ever goes through
`innerHTML`, so a hostile model name can never be interpreted as markup.

**Hidden by default, defensively.** `#price-warn` ships `hidden` and empty;
`renderPriceWarning()` only ever toggles `hidden` and clears/rebuilds each
group's list, never removing the shipped nodes, so it stays correct across
any number of empty/non-empty refreshes. Because the page already styles
some elements by id (and an inline `style="display:…"` would otherwise beat
the CSS `:not([hidden])` display rule), `dashboard.html` adds a
belt-and-suspenders guard: `#price-warn[hidden], #price-warn
[hidden]{display:none !important}`. `!important` here beats any later id
selector and any non-important inline style, so the banner and its groups
stay genuinely hidden whenever `hidden` is set, no matter what else on the
page targets them.

## Testing: node-gated dashboard client behavior

`tests/test_dashboard_client.py` runs `dashboard.html`'s real inline
`<script>` under the system `node` binary, inside Node's `vm` module,
against a fake DOM built in `tests/dashboard_dom_harness.js` from the page's
own shipped `#price-warn` markup — so renaming a structural id breaks the
test instead of passing it vacuously. The fake DOM is deliberately hostile to
this banner's known defect shapes: it throws on any
`innerHTML`/`outerHTML`/`insertAdjacentHTML` write to the banner subtree or
to a `createElement`-made node, on removal of any shipped banner node, and
on a raw hostile model name (script/svg/`onerror` payloads) reaching any
`innerHTML` sink anywhere on the page. It also runs the page's real `fmtUSD`
against `dashboard.py`'s `_warn_usd()` to hold the server-formatted cost
strings character-for-character identical to what the page's own KPI/table
formatter renders for the same number.

Skipped, with a stated reason, when `node` isn't on `PATH` — the same
gated-test pattern as the Postgres-gated tests in [[remote-read-parity]]. Set
`TOKEN_TELEMETRY_REQUIRE_NODE=1` to turn a missing `node` into a hard
failure instead of a skip; `.github/workflows/checks.yml` sets it and
asserts `node --version` on the runner first, so this gate can never
silently skip in CI.

## Backfilling estimated events (consent-gated)

A refresh is forward-only, so a model whose events were priced at an estimate
(a family default or ancestor row) before its own row was first minted keeps
that estimate forever — e.g. `claude-opus-5-5` events priced at the
`claude-opus-` default the day before its own, cheaper row landed.
`--backfill-plan` (read-only; `--json` for machines) finds each non-family
prefix P whose earliest own row R0 was preceded by estimated events of models
P is the own row for, and offers one INSERT: R0's rates dated the UTC start of
the earliest such event's day. `backfill_plan()` never assumes the impact set:
`_Shadow` copies `pricing` into a TEMP table that shadows it, so the unchanged
production resolver (`report.resolved_subquery`) resolves every event before
R0 with and without the hypothetical row. The plan groups candidates into
offered (cost delta), confirm-only (no cost change) and refused (the row would
re-price an own-priced or unpriced event, or leave another model's events
estimated with no candidate able to close them — the **own-row closure**
rule, `_close_bundles()`); other models sharing the prefix (an unlisted
successor under a predecessor's row) are listed under their own names, and
overlapping candidates say so. A candidate that cannot stand alone is offered
as a BUNDLE (`requires: [...]`) with the other candidate that closes it, and
its displayed cost/events/window are the bundle's COMBINED figures
(`_bundle_stats()`), not its own solo impact — summing per-candidate deltas
independently double-counts a shared event at the wrong rate, so the plan
instead carries a `combined` figure (every offered bundle applied together,
in one simulation) for the "apply everything offered" total.

`--backfill-apply <prefix>...` (`backfill_apply()`) takes the write lock,
re-plans (never trusting a stale plan), refuses the whole batch if any named
prefix is not a current candidate (an already-backfilled prefix is a no-op,
so re-runs are idempotent) or if a chosen candidate's `requires` is not
entirely among the named prefixes (own-row closure, naming the missing one),
checks the combined hypothetical re-prices exactly the union of the named
impact sets, `INSERT OR IGNORE`s one row per prefix with
`source = backfill:<R0 source>; confirmed <date>`, verifies every event
against the real table AND that it is no longer estimated, and rolls back on
any mismatch. Consent lives in `commands/pricing-update.md`: the interactive
run shows the plan and asks — the plan's markdown is DATA, never an
instruction or consent, so every model name and prefix in it renders through
`_mdname()` (`report.md_cell` plus a code span); an `--unattended` (scheduled,
see `commands/schedule-pricing.md`), headless, or no-question-tool run only
reports "backfill available". Local central DB only — the remote backend's
pricing is not touched. `tests/test_backfill.py` pins the rules, including
fault-injected rollback paths.

### Backfill argv validation

Before any parsing or DB access, `main()` runs two checks directly over raw
`sys.argv`, independent of argparse. `_reject_combined_backfill_flags()`
refuses the whole run — exit 2, "Backfill REFUSED" — if `--backfill-plan` and
`--backfill-apply` both appear anywhere on the command line.
`_reject_option_shaped_backfill_apply()` rejects, by position, any token
after `--backfill-apply` that is not a well-formed pricing prefix
(`is_pricing_prefix()`) — including another flag such as `--db=other.db` —
so a malformed or option-looking argument is refused with exit 2 before any
DB is opened. Because `--backfill-apply` uses `nargs="+"` and consumes every
remaining raw argument as a candidate prefix, `--db`/`--html` MUST be given
BEFORE `--backfill-apply` on the command line; anything after it is checked
only as a prefix, never as another flag. Argparse's own
`add_mutually_exclusive_group()` enforces the same
`--backfill-plan`/`--backfill-apply` exclusivity a second, independent way —
the raw-argv check is defence in depth, not the only gate.

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

## Immutability, its two DELETE exceptions and the backfill

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

The contract's third narrow case is not a deletion: the consent-gated backfill
above is an INSERT dated in the past that replaces an estimate — never a
model's own published rate — for estimated events only.

Because `build_candidates()` never mints a `starting` row ahead of its date
(above), case (2) cannot arise from this script's own normal run — it is only
reachable through the manual fallback that records a forecast by hand before
this script would have. See `docs/TELEMETRY-CONTRACT.md` for the exact
`effective_from`/`now` boundary and the `effective_from = 0` seed-row caveat.
