---
doc: Pricing Updates
type: handbook
status: active
summary: How `pricing_update.py` refreshes the `pricing` table from Anthropic's published pricing page — case-insensitive table/column detection across the live page's two-row header and the older single-row one, rendered-text cell reading with a strict model-version boundary and skip-and-warn for width-mismatched model rows (AOS-151), minting only the rate in force today per listed version (with warnings for an expired intro or a future-only-newest version), the own-price-vs-estimate definitions, the dashboard's own-price warning banner (AOS-135) that now surfaces them with its node-gated client tests, the consent-gated backfill of estimated events, the narrow exceptions to the pricing table's immutability contract, and the AOS-143 parser bounds (backdated-starting refusal, malformed-token rejection extended in round 2 to separator/suffix/notation characters, rate-magnitude/floor and row-count caps, capped per-row warnings, three distinct exit codes so the command's fallback can never bypass a bound, a wall-clock fetch deadline and body-size cap closing a hang/DoS gap, an unopenable-DB error mapped to its documented exit code, sanitized error and fetch-failure text, and round 2's F1b fix that closed the fallback itself — it now only ever re-fetches the page and re-runs this same script, never on an unattended/scheduled run) that keep a hostile or broken page from minting permanent bad rows.
keywords: [pricing, pricing-update, parse_models, build_candidates, immutability, effective_from, in-force, estimated, family-default, ancestor-row, stale-price-warning, future-rate-warning, backdated-starting-warning, parser-bounds, max-rate-usd, min-inout-rate-usd, max-candidates, warning-cap, exit-bounds-refused, exit-fetch-failed, exit-other-error, dashboard, price-warning-banner, node-gated-tests, require-node, backfill, backfill-plan, backfill-apply, f1b, fetch-timeout-s, fetch-max-bytes, strict-number-token, rendered-text, version-boundary, skipped-row-warning, colspan]
level: project
audience: developer
module: pricing-update
sources: [scripts/pricing_update.py, commands/pricing-update.md, commands/schedule-pricing.md, docs/TELEMETRY-CONTRACT.md, tests/pricing_golden.py, tests/test_pricing_bounds.py, scripts/dashboard.py, scripts/dashboard.html, tests/test_dashboard_client.py, tests/dashboard_dom_harness.js, tests/test_backfill.py, scripts/backfill_summary.py, tests/test_backfill_banner.py]
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

## Table and column detection

`parse_models()` hands the page's tables to `_locate_rate_table()`, which
takes the first table, in page order, matching one of two header layouts, and
then to `_map_rate_columns()`, which finds each of the five rate columns
(`in`, `out`, `w5`, `w1h`, `cr`) in that layout's header row. All header text
is lower-cased before comparing, since Anthropic's page has shipped the same
columns in both Title Case and sentence case.

- **Two-row header** (the live page since at least 2026-09-23, fixture
  `tests/fixtures/pricing-page-two-row-header-2026-09-23.html`): a
  column-group row (`Model` | `Base tokens` | `Prompt caching`) above the
  per-column row (`Name` | `Input` | `Output` | `5m writes` | `1h writes` |
  `Hits and refreshes`). The table is recognized by the group row carrying
  both `"base tokens"` and `"prompt caching"`; the columns are mapped from
  the SECOND row, which is the one aligned with the data cells. The page's
  batch table (`Model` | `Batch tokens`) and fast-mode table (`Model` |
  `Input` | `Output`) carry neither marker pair and are never read as rates.
- **Single-row header** (the page before 2026-09, and the older fixtures):
  one header row recognized by a `"base input tokens"` cell, mapped with the
  needles `"base input"`, `"output"`, `"5m cache"`, `"1h cache"`, `"cache
  hits"`. Kept so an `--html` save of the older page still parses.

When no table matches either layout the script exits with its
structural-parse-failure code (`EXIT_OTHER_ERROR` — a "table not found" is a
parse failure, not a fetch one); that is how AOS-151 surfaced, when the live
page moved to the two-row header and every bare refresh exited 3.

The column guard is strict: every needle must match exactly ONE header cell,
no two rate keys may share a column, and no rate may sit in column 0 (the
model name every row is identified by) — otherwise `ValueError`
("unexpected pricing table header", with the header text sanitized). Only the
header's position and wording differ between layouts: every data row below
either header goes through the same row loop, so the strict rate token, the
value bounds, and the sanitization below apply identically.
`tests/test_pricing_bounds.py` runs every class that builds a page twice,
once per layout (the generated `...TwoRowLayout` twins), and fails if a new
page-building class is added without a twin.

## Reading a data row: cell text, model name, row width (AOS-151)

**Cell text is the text a browser renders.** `TableCollector` builds every
cell's text as follows:

- An HTML comment contributes nothing and is never a boundary. The live page
  is React SSR and splits text nodes with `<!-- -->`, so
  `4<!-- -->.<!-- -->5` reads `4.5`.
- An inline element (`span`, `a`, `b`, `strong`, `i`, `em`, `sup`, `code`, …)
  concatenates with its neighbours with no inserted space, so `4<b>.5</b>`
  reads `4.5`.
- A block-level element (`div`, `p`, `li`, headings, table parts, …, the list
  is `_BLOCK_TAGS`), a `<br>`, and a flex/grid item insert one boundary space,
  so `Claude Sonnet 5<br>1M-token` reads `Claude Sonnet 5 1M-token`.
- Whitespace then collapses to single spaces.

A flex or grid container blockifies its children (CSS Display §2.7), and that
is how the live page separates a model name from its tagline. The name cell is
`<div class="flex min-w-0 flex-col"><a>Claude Opus 5.5</a><span>For …</span></div>`:
two inline elements with no whitespace between them, on separate lines only
because their parent is a flex column. A retired model's name sits in a
`<span class="inline-flex">` beside a badge button whose icon is a
private-use glyph (U+E0F0). The page's CSS is Tailwind, so a container is
recognized by an exact, unprefixed `flex`/`inline-flex`/`grid`/`inline-grid`
class token or an inline `style` declaring `display: [inline-]flex|grid`. A
responsive variant such as `md:flex` is conditional and is ignored. No other
CSS is modelled. An `<a>` + `<span>` outside any flex container reads as one
run of text (`Claude Sonnet 51M-token`), which the version boundary below then
refuses.

**Model name and version.** `_MODEL_NAME_RE` matches
`Claude <Family> <version>`, where the version is at most two `.`-separated
components of at most three digits each. `_version_boundary_ok` then requires
the character right after the version to be end-of-text or whitespace
(`str.isspace`). Anything else refuses the whole run (`PricingRefused`, exit
1, nothing written):

- a letter or a digit of any script
- `.`, `_`, `-`, `,`, `/`, `%` or other ASCII punctuation
- a separator lookalike (`٫`, `．`, `․`, `·`, `–`, `‐`, `＿`, …)
- an invisible format or combining character (U+200B, U+2060, U+FEFF, U+00AD,
  U+200E, U+0301, …)

No punctuation is allowed. In every committed page capture under
`tests/fixtures/`, a rate-table model name is followed only by end-of-cell or
whitespace.

**Row width.** Rates are read by cell index, so a model row must occupy the
header's columns cell for cell: its cell count and its effective width (each
cell's `colspan`, parsed and clamped to 1..1000 the way a browser does it,
summed) must both equal the header's cell count. A model row that does not
(an extra cell, a short row, a merged "Contact sales" cell, a colspan'd
"Claude Opus 4 and earlier" heading) is **skipped**:

- its rates are never read, so they are never shifted into other columns
- every other model on the page is still recorded
- the report prints a `SKIPPED-ROW WARNING` naming the model, its rate-table
  row number and its sanitized name-cell text; these lines are capped at
  `WARNING_CAP` like the other warning categories

The order of checks is: model name, then version boundary, then width. So a row
with no model name at all (the live page's "Additional models" divider) is
passed over without a warning, and a malformed version still refuses the run
whatever the row's width. If every model row is skipped, the run ends with
"no model rows" (`EXIT_OTHER_ERROR`), and the message gives the skipped count.
A header holding a merged cell raises "unexpected pricing table header"
(`EXIT_OTHER_ERROR`), because every rate would otherwise map against the wrong
column.

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

**A family's newest version listed only with a future `starting` date
(`future_only_newest()`).** Distinct from the stale-intro case above: the
newest version's only listing is a `starting <d>` increase that has not
arrived yet (`d > today`), so no rate is in force for it and, as with a stale
intro, no family default is minted either. This is not a page bug — the
increase just hasn't happened — but it is easy to mistake for "nothing
changed": the run report prints a `FUTURE-RATE WARNING` line naming the
family, version and the increase's start date, so a reader knows the family
default is intentionally holding its last recorded rate until then. A version
mixing an expired `through` with a future `starting` is reported once, by
`stale_intros()`, not twice.

## Parser bounds (AOS-143, corrected)

Pricing history is insert-only (`docs/TELEMETRY-CONTRACT.md` §Pricing table,
"History is never mutated"), so a bad row minted from the parsed page is
**permanent**. A security review found four ways a hostile or merely broken
page could abuse that: `pricing_update.py` bounds all four before any row
reaches the database.

**Backdated `starting` rows (`filter_backdated_starting()`).** An in-force
`starting <d>` row is normally minted dated `d`, however far back that is —
"starting January 1, 1970" would mint `effective_from = 0`, re-pricing every
event of that prefix back to the epoch. The refusal is **per row**, and only
for `starting` rows:

- `filter_backdated_starting()` names each in-force `starting` row dated more
  than `BACKDATE_MAX_DAYS` (365) days before the run date, by its
  `row_key()` — `(family, version, condition)`, one page row, never a whole
  version. `through` rows and unconditional rows are never named, whatever
  their date; a future `starting` (`d > today`) is never minted anyway
  (`in_force()`).
- `build_candidates()` decides in-force status from the **unfiltered** page,
  exactly as before AOS-143, then skips only the named rows. Every other row
  of the same version still mints: a newer arrived `starting` increase at its
  own date, an in-force `through` intro dated today. A version with any
  in-force conditional, refused or not, keeps its unconditional base rate
  suppressed, so a refusal never reverts a real increase to the base rate.
- The family default takes the newest version's latest surviving in-force
  row. When every in-force row of that version was refused, no family row is
  minted and the family default keeps its last recorded rate.
- `run_update()` drops the warning for a refused row that
  `already_recorded()` finds in the DB — every prefix holds a row at that
  date with identical rates, minted when the increase first arrived — so a
  weekly re-run over an old, recorded increase is silent. Any other refused
  row prints one `BACKDATED-STARTING WARNING` line and the run proceeds: "a
  starting row dated `<d>` is more than 365 days old and was not recorded;
  the rows already recorded for `<version>` are unchanged".

So an arrived increase within 365 days mints at its date even when later rows
already exist for that prefix, and even when the page still lists an older,
refused increase for the same version; `INSERT OR IGNORE` keeps a re-run at
an already-recorded date a no-op. There is no other bound on `starting`
dates: refusing a date earlier than a prefix's latest recorded row would
refuse every legitimate increase on the run after it arrived, because the
increase itself recorded that later row.

**Three distinct exit codes (AOS-143, corrected, F1).** A plain refresh run
ends in `EXIT_BOUNDS_REFUSED` (1, the page violated a bound below — never
falls back), `EXIT_FETCH_FAILED` (2, the page/file could not be read at
all, including the fetch bounds below — the ONLY code
`commands/pricing-update.md`'s fallback runs for, and only on an interactive
run), or `EXIT_OTHER_ERROR` (3, the page read fine but did not structurally
parse, another unexpected error, or a DB error — also never falls back). The
first AOS-143 fix shipped let a bound refusal share `EXIT_FETCH_FAILED`'s
predecessor code with a genuine fetch/layout failure, so the command's own
fallback text — which had none of these bounds — would run for exactly the
pages the bounds exist to refuse; `main()` now catches `PricingRefused`
around `parse_models()` itself (not just around `run_update()`'s row-count
check) so every bound refusal, wherever it is raised, gets the same
non-falling-back code.

**Round 2 (F1b) closed the fallback itself.** Even with the codes above
separated, a hostile server could still force `EXIT_FETCH_FAILED` on demand
and get served, through the agent's own differently-identified fetch, a page
whose bounds the command's PROSE fallback applied more loosely than the
script (a per-row skip instead of refusing the whole page; no family/prefix
check; no cap on an unattended run). The fallback no longer parses or inserts
anything by hand at all: it re-fetches the page, writes it to a file, and
re-runs `pricing_update.py --html <file>`, so every bound in this document
applies through the one implementation; it also never runs on an unattended
or scheduled run — those report and stop on `EXIT_FETCH_FAILED` too. A
related round-2 fix bounds the fetch itself: `_fetch_page()` enforces one
wall-clock deadline across connect + the full read (`FETCH_TIMEOUT_S`, 30s)
in a joined daemon thread — `urlopen`'s own `timeout=` only bounds each
individual socket operation, which a server that trickles data through can
avoid indefinitely — plus a body-size cap (`FETCH_MAX_BYTES`, 5MB) enforced
while reading in chunks, since `resp.read()` has no cap of its own.

**Unbounded rate magnitude, and malformed numeric tokens (`MAX_RATE_USD`,
`MIN_INOUT_RATE_USD`, in `parse_models()`; malformed-token rejection in
`money()` itself, AOS-143 corrected, extended round 2, F6).** `money()`
matches the WHOLE contiguous `$`-prefixed numeric-ish run and requires it —
trailing ASCII spaces trimmed — to be a plain decimal number — ASCII digits
with at most one `.` — raising `PricingRefused` otherwise. Round 1's class
(digits, commas, dots, exponent markers, signs) already refused a thousands
separator (`$1,500`), more than one decimal point (`$4.00.00`), or any
exponent marker, complete (`$1e309`) or dangling after a bare `.`
(`$1.e3`), instead of silently truncating to the leading digits (minting
`$1`). Round 2 found the class still let other separator/suffix/notation
characters through uncaptured, each truncating the same way: an apostrophe
or underscore thousands separator (`$1'500`, `$1_500`), a thousands-grouping
space — ASCII or one of four Unicode variants, thin space U+2009, narrow
no-break space U+202F, no-break space U+00A0, figure space U+2007 —
(`$1 500`), a `k`/`M` magnitude suffix (`$1k`, `$1M`), a written-out exponent
(`$1x10^6`), or a leading sign (`$-4`, `$+4`). Every one of those is now also
a run character, so the WHOLE run is captured and fails the strict check
instead of stopping short. `e`/`E`/`+`/`-` stay in the class from round 1 —
removing them would stop capturing an exponent's tail and reopen the
`$1e309` → `$1` truncation it fixed. A character the class does not
recognize at all (e.g. a non-ASCII digit) still starts no run — `money()`
returns `None`, `parse_models()` raises its pre-existing generic
"unparseable rate cell" error (`EXIT_OTHER_ERROR`), and that is still safe
(the run stops either way). A well-formed number that survives that check is
still bounded in `parse_models()`: a rate cell
with several hundred digits overflows Python's `float()` to `inf` with **no
exception raised**; `parse_models()` rejects any rate that is non-finite,
exceeds `MAX_RATE_USD` ($10,000/MTok), or — for `in_usd`/`out_usd` only,
cache rates keep no lower bound — is under `MIN_INOUT_RATE_USD`
($0.01/MTok; the old bound was "$0 or below" only, so a near-zero rate such
as $0.0000001 still minted). Any of these refuses the **whole run**
atomically (`PricingRefused`, `EXIT_BOUNDS_REFUSED`) before `main()` ever
opens the database connection.

**Unbounded row count (`MAX_CANDIDATES`, in `run_update()`).** A page listing
thousands of rows would mint thousands of candidate pricing rows in one run.
`run_update()` checks `len(build_candidates(...))` against `MAX_CANDIDATES`
(500) **before** calling `plan()`/`apply()` — over the cap raises
`PricingRefused` and nothing is planned or applied. `main()` catches
`PricingRefused`, prints its message, and exits with `EXIT_BOUNDS_REFUSED` —
the same code the rate-bounds checks above use, since both are the script
REFUSING a page bound, a different failure mode from either a fetch or a
structural parse failure.

**Unbounded per-row warning lines (`WARNING_CAP`, in `render()`, AOS-143
corrected).** A STALE-PRICE, BACKDATED-STARTING, FUTURE-RATE or (AOS-151)
SKIPPED-ROW warning is not a candidate row, so `MAX_CANDIDATES` above does not bound how many of
them one run can print, and the command prints stdout "verbatim" into the
agent's context. `_capped_warnings()` renders at most `WARNING_CAP` (20)
lines per category, plus one "... and N more" summary line when there are
more — independently for each of the four categories.

**Raw page text and fetch-error text in printed messages
(`_safe_error_text()`).** The two parse-failure messages that embed page
text — "unexpected pricing table header" and "unparseable rate cell in
row" — used to interpolate the raw cell text (and, for the header, a raw
Python list of raw cells) directly into the exception message that
`main()` prints to stderr on a parse failure; `main()`'s fetch-failure
message now goes through the same sanitizer too (AOS-143 correction — its
exception text can carry an attacker- or MITM-controlled raw HTTP status
line, e.g. `HTTP/1.1 500 Oops\x1b]0;pwned\x07`). `_safe_error_text()`
strips ASCII control characters and DEL (U+0000-U+001F, U+007F), the C1
control range (U+0080-U+009F — including U+0085 NEL and U+009B, the 8-bit
form of CSI, usable to start a terminal escape sequence without a 7-bit
`ESC` byte) and Unicode bidi-control characters — U+200E LRM, U+200F RLM,
U+061C ALM, U+202A-U+202E, U+2066-U+2069 (the first three were missing from
the original strip set, AOS-143 correction) — then caps the result to 200
characters — a header row is a Python list, whose `str()` has no length
limit on its own, so even a single absurdly long or many-celled header
prints a short, bounded message.

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
`--backfill-plan` (reads the DB read-only, writes no DB row, and caches a
plan summary in `backfill-plan.json` next to the DB for the dashboard — see
"Dashboard 'backfill available' line" below; `--json` for machines) finds
each non-family
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

### Dashboard "backfill available" line (AOS-149)

`scripts/backfill_summary.py` owns a small sidecar, `backfill-plan.json`
beside the dashboard's DB. `main()`'s `--backfill-plan` branch takes
`fingerprint()` before and after `backfill_plan()` and, via
`_cache_plan_summary()`, writes `{version, computedAt, bundles, deltaText,
fingerprint}` only when both match and the DB is the dashboard's
(`is_dashboard_db()`); a successful `--backfill-apply` calls
`_clear_plan_summary()`. Neither touches stdout or the exit code. The writer
is atomic and symlink-safe (`O_EXCL|O_NOFOLLOW` 0600 temp + `os.replace`, a
non-regular target refused). `fingerprint()` hashes every pricing row plus
aggregates of the events before the plan horizon (latest first-row date of
any non-family-default prefix), so it is cheap and ordinary new capture never
invalidates it. `dashboard.backfill_line()` → `backfill_summary.banner_line()`
validates the file strictly (`read()`: size cap, `O_NOFOLLOW`, exact types,
`DELTA_RE`), recomputes the fingerprint, and returns the pre-formatted line or
`None` — never raising; always `None` on the supabase backend. It rides in
`priceWarning.backfill`; `renderPriceWarning()` sets it with `textContent`
into `#price-warn-backfill-msg` and shows the banner for it alone. Contract:
docs/TELEMETRY-CONTRACT.md, "Dashboard plan summary". Tests:
`tests/test_backfill_banner.py` and the node-gated
`TestBackfillLinePageRender` / backfill cases in
`tests/test_dashboard_client.py`.

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
remaining raw argument as a candidate prefix, `--db` MUST be given BEFORE
`--backfill-apply` on the command line (`--html` is refresh-only and is
rejected with either backfill flag); anything after it is checked
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
(above), case (2) cannot arise from this script's own normal run. It is not
reachable through the command's fallback either (AOS-143 correction, round 2,
F1b): the fallback no longer records anything by hand — it re-fetches the page
and re-runs this same script — so case (2) currently has no documented path at
all. See `docs/TELEMETRY-CONTRACT.md` for the exact `effective_from`/`now`
boundary and the `effective_from = 0` seed-row caveat.
