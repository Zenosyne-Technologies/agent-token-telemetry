"""AOS-143 parser bounds (`scripts/pricing_update.py`): pricing history is
insert-only (docs/TELEMETRY-CONTRACT.md §Pricing table), so a bad row minted
from the parsed pricing page is permanent. These tests bound what a single
page/run can mint: a `starting <d>` row cannot be minted more than a year
back (a per-row refusal, warned unless already recorded, not fatal); a
non-finite or absurd rate refuses the WHOLE run; more than 500 candidate rows
refuses the WHOLE run; a malformed/obfuscated numeric token (round 1 and
round 2) refuses the WHOLE run; and no raw, unsanitized page text ever
reaches an error message. Fixture DBs and in-test HTML only, with one
deliberate exception: `TestFetchHangAndSizeBounds` (round 2, hang/DoS fix)
runs a real local stdlib `http.server` on loopback, since the bound under
test is about genuine socket/timeout behaviour a mocked `urlopen` cannot
exercise — it never reaches an external host."""
import contextlib
import datetime
import hashlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import pricing_update

RATES = {"in_usd": 2.0, "out_usd": 10.0, "cache_r_usd": 0.2,
         "cache_w_usd": 2.5, "cache_w_1h_usd": 4.0}
INCREASE_FIXTURE = (pathlib.Path(__file__).resolve().parent / "fixtures"
                    / "pricing-page-increase.html")
# The exact rates INCREASE_FIXTURE's `starting September 1, 2026` row parses
# to (its "Claude Sonnet 5 (starting ...)" row: $4 / $5 / $8 / $0.40 / $20 per
# MTok) — used to pre-seed a DB that already recorded the real increase, so
# the steady-state tests below exercise a re-run that changes nothing.
INCREASE_RATES = {"in_usd": 4.0, "out_usd": 20.0, "cache_r_usd": 0.4,
                  "cache_w_usd": 5.0, "cache_w_1h_usd": 8.0}


def _epoch(d):
    return int(datetime.datetime.combine(
        d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())


def _entry(family, version, condition=None, rates=None):
    return {"family": family, "version": version,
            "rates": dict(rates or RATES), "condition": condition}


# Row tuples passed to _table_html are always in this SEMANTIC order,
# whatever the layout; each layout then emits the cells in its own column
# order, so a two-row page genuinely exercises the column mapping.
ROW_KEYS = ("name", "in", "w5", "w1h", "cr", "out")
# The two rate-table layouts pricing_update recognizes (AOS-151): the older
# single-row header, and the live page's two-row header (a column-group row
# above the per-column row, verbatim cell texts from the 2026-09-23 capture).
LAYOUTS = ("single-row", "two-row")
_LAYOUT_ORDER = {"single-row": ("name", "in", "w5", "w1h", "cr", "out"),
                 "two-row": ("name", "in", "out", "w5", "w1h", "cr")}
_LAYOUT_HEADER = {
    "single-row": {"name": "Model", "in": "Base input tokens",
                   "w5": "5m cache writes", "w1h": "1h cache writes",
                   "cr": "Cache hits and refreshes", "out": "Output tokens"},
    "two-row": {"name": "Name", "in": "Input", "out": "Output",
                "w5": "5m writes", "w1h": "1h writes",
                "cr": "Hits and refreshes"},
}
_TWO_ROW_GROUP = ('<tr><th scope="colgroup">Model</th>'
                  '<th scope="colgroup" colSpan="2">Base tokens</th>'
                  '<th scope="colgroup" colSpan="3">Prompt caching</th></tr>')


def _table_html(rows, layout="single-row", header=None):
    """A minimal valid pricing table in ``layout`` (one of :data:`LAYOUTS`):
    the header the page ships in that layout, plus one ``<td>`` row per
    ``(name, in, w5, w1h, cr, out)`` tuple in ``rows`` (a shorter tuple
    omits its trailing cells). ``header`` overrides per-column header texts
    by key; a ``None`` value drops that column from the header and every
    row."""
    head = dict(_LAYOUT_HEADER[layout], **(header or {}))
    order = [k for k in _LAYOUT_ORDER[layout] if head[k] is not None]
    head_html = "<tr>" + "".join(f"<th>{head[k]}</th>" for k in order) + "</tr>"
    if layout == "two-row":
        head_html = _TWO_ROW_GROUP + head_html
    body = ""
    for r in rows:
        cells = dict(zip(ROW_KEYS, r))
        body += ("<tr>" + "".join(f"<td>{cells[k]}</td>"
                                  for k in order if k in cells) + "</tr>")
    return f"<html><body><table>{head_html}{body}</table></body></html>"


class Fixture:
    """A fresh telemetry DB (seed rows kept, no events/projects needed)."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "usage.db"
        self.conn = capture.connect(self.path)

    def price(self, prefix, rates, eff, source="test"):
        self.conn.execute(
            "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
            " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from,"
            " source) VALUES ('anthropic',?,?,?,?,?,?,?,?)",
            (prefix, rates["in_usd"], rates["out_usd"], rates["cache_r_usd"],
             rates["cache_w_usd"], rates["cache_w_1h_usd"], eff, source))
        self.conn.commit()

    def pricing_rows(self, prefix=None):
        q = ("SELECT model_prefix, in_usd, out_usd, cache_r_usd, cache_w_usd,"
             " cache_w_1h_usd, effective_from, source FROM pricing")
        if prefix is None:
            return self.conn.execute(
                q + " ORDER BY model_prefix, effective_from").fetchall()
        return self.conn.execute(
            q + " WHERE model_prefix=? ORDER BY effective_from",
            (prefix,)).fetchall()

    def digest(self):
        """Hash of the DB file(s) on disk — proves a refused run wrote
        nothing, including through WAL."""
        h = hashlib.sha256()
        for suffix in ("", "-wal"):
            p = pathlib.Path(str(self.path) + suffix)
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()

    def write_html(self, html, name="page.html"):
        p = pathlib.Path(self.tmp.name) / name
        p.write_text(html)
        return p

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pricing_update.main(["--db", str(self.path), *args])
        return code, out.getvalue(), err.getvalue()

    def close(self):
        self.conn.close()
        self.tmp.cleanup()


class Base(unittest.TestCase):
    # The page layout every self._html() call renders. Each HTML-parsing
    # class runs once per layout: this base value, plus a generated
    # `<Class>TwoRowLayout` twin at the bottom of the module (AOS-151).
    LAYOUT = "single-row"

    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def _html(self, rows, header=None):
        return _table_html(rows, self.LAYOUT, header)


class TestBackdatedStarting(Base):
    """Fix 1 (AOS-143, corrected): an in-force `starting <d>` row is
    normally minted dated `d` however far back — refused (warned, not
    fatal, INSERT-only) when `d` is more than a year before today. The
    "earlier than the prefix's latest recorded rate" rule was removed
    entirely: in steady state the DB already holds later rows for a prefix
    once a real increase has landed, and that rule refused every
    legitimate increase on the very next scheduled run after it arrived."""

    def test_1970_starting_page_mints_nothing_and_warns(self):
        today = datetime.date(2026, 9, 23)
        entries = [_entry("sonnet", "5",
                          ("starting", datetime.date(1970, 1, 1)))]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertIn("BACKDATED-STARTING WARNING", report)
        self.assertIn("Claude Sonnet 5", report)
        self.assertIn("1970-01-01", report)
        self.assertIn("a starting row dated 1970-01-01 is more than 365"
                      " days old and was not recorded; the rows already"
                      " recorded for Claude Sonnet 5 are unchanged", report)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])
        self.assertIn("0 row(s) inserted", report)

    def test_starting_before_prefixs_latest_row_still_mints_within_365d(self):
        # The removed rule: a starting date within the 365-day window still
        # mints even though the DB already holds a LATER row for the same
        # prefix (e.g. the page corrected a previously-announced date
        # backward) — INSERT OR IGNORE, never UPDATE/DELETE, so the later
        # row stays exactly as recorded.
        today = datetime.date(2026, 9, 23)
        later_eff = _epoch(datetime.date(2026, 9, 15))
        self.f.price("claude-sonnet-5", RATES, later_eff)
        before = self.f.pricing_rows("claude-sonnet-5")
        entries = [_entry("sonnet", "5",
                          ("starting", datetime.date(2026, 8, 15)),
                          dict(RATES, in_usd=9.0))]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertNotIn("BACKDATED-STARTING WARNING", report)
        rows = self.f.pricing_rows("claude-sonnet-5")
        self.assertEqual(len(rows), 2)
        self.assertIn(_epoch(datetime.date(2026, 8, 15)),
                     [r[6] for r in rows])
        # the existing later row is untouched: insert-only
        self.assertIn(before[0], rows)

    def test_arrived_increase_within_a_year_still_mints_regression(self):
        # Regression: filter_backdated_starting must not swallow a
        # legitimate, recent scheduled increase. Same repro page as
        # test_pricing_update.TestInForceMinting, run end-to-end through
        # run_update this time (Sonnet 5 `starting September 1, 2026`,
        # 14 days before the run date — nowhere near the 365-day cutoff and
        # no earlier recorded row for the prefix exists yet).
        today = datetime.date(2026, 9, 15)
        entries = pricing_update.parse_models(INCREASE_FIXTURE.read_text())
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertNotIn("BACKDATED-STARTING WARNING", report)
        rows = self.f.pricing_rows("claude-sonnet-5")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 4.0)                 # in_usd
        self.assertEqual(rows[0][6], _epoch(datetime.date(2026, 9, 1)))

    def test_steady_state_366_days_later_mints_nothing_no_base_reversion(self):
        # F1 regression: the increase already minted the $4 rows on
        # 2026-09-01; a year-plus later the page is UNCHANGED (still shows
        # base $3 + `starting September 1, 2026` $4). The starting row is
        # now >365 days old and refused — but in-force status still comes
        # from the page (F1), so the version stays "conditioned": the $3
        # base rate must NOT be minted (that would silently revert the real
        # increase), and the family default must not mint either, since the
        # newest version's in-force rate this run is the refused row. The
        # refused row is already recorded with identical rates, so the run
        # is a no-op for it and prints no BACKDATED-STARTING WARNING.
        arrived = datetime.date(2026, 9, 1)
        self.f.price("claude-sonnet-5", INCREASE_RATES, _epoch(arrived))
        self.f.price("claude-sonnet-", INCREASE_RATES, _epoch(arrived))
        before_5 = self.f.pricing_rows("claude-sonnet-5")
        before_fam = self.f.pricing_rows("claude-sonnet-")
        today = datetime.date(2027, 9, 2)   # 366 days after the increase
        entries = pricing_update.parse_models(INCREASE_FIXTURE.read_text())
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertNotIn("BACKDATED-STARTING WARNING", report)
        # nothing changed for the refused version or its family default: the
        # pre-increase $3 rate was never (re-)minted, and the already-
        # recorded $4 rows are untouched. (The fixture's third, unconditional
        # row, Sonnet 4.5, mints normally either way — unrelated to F1.)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), before_5)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-"), before_fam)
        table_rows = [ln for ln in report.splitlines() if ln.startswith("| `")]
        self.assertFalse(any(ln.startswith("| `claude-sonnet-5`")
                             or ln.startswith("| `claude-sonnet-`")
                             for ln in table_rows))

    def test_steady_state_within_365_days_still_mints_the_increase(self):
        # Same steady-state DB, but the run happens within the 365-day
        # window (the increase is 200 days old, not 366): it mints
        # normally, and INSERT OR IGNORE makes the re-run of an
        # already-recorded date a no-op.
        arrived = datetime.date(2026, 9, 1)
        self.f.price("claude-sonnet-5", INCREASE_RATES, _epoch(arrived))
        self.f.price("claude-sonnet-", INCREASE_RATES, _epoch(arrived))
        today = arrived + datetime.timedelta(days=200)
        entries = pricing_update.parse_models(INCREASE_FIXTURE.read_text())
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertNotIn("BACKDATED-STARTING WARNING", report)
        # the increase is already recorded at its own date: re-running mints
        # nothing new for it or its family default (INSERT OR IGNORE is a
        # no-op); only the fixture's unrelated Sonnet 4.5 row is new.
        rows = self.f.pricing_rows("claude-sonnet-5")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 4.0)
        self.assertEqual(rows[0][6], _epoch(arrived))
        fam_rows = [r for r in self.f.pricing_rows("claude-sonnet-")
                   if r[6] != 0]              # exclude the seed row
        self.assertEqual(len(fam_rows), 1)
        self.assertEqual(fam_rows[0][6], _epoch(arrived))

    def test_filter_function_flags_only_the_offending_entry(self):
        today = datetime.date(2026, 9, 23)
        keep = _entry("opus", "5")
        drop = _entry("sonnet", "5", ("starting", datetime.date(1970, 1, 1)))
        refused, warnings = pricing_update.filter_backdated_starting(
            [keep, drop], today)
        self.assertEqual(refused, {pricing_update.row_key(drop)})
        self.assertEqual(
            refused, {("sonnet", "5", ("starting", datetime.date(1970, 1, 1)))})
        self.assertEqual(warnings, [("sonnet", "5", datetime.date(1970, 1, 1),
                                     drop["rates"])])

    def test_future_starting_is_left_alone_not_bound_checked(self):
        # A future `starting` is never minted anyway (in_force) — the bound
        # check does not apply to it (no premature warning either).
        today = datetime.date(2026, 9, 23)
        entries = [_entry("sonnet", "5",
                          ("starting", datetime.date(2030, 1, 1)))]
        refused, warnings = pricing_update.filter_backdated_starting(
            entries, today)
        self.assertEqual(refused, set())
        self.assertEqual(warnings, [])

    def test_exactly_365_days_back_mints_366_is_refused(self):
        # Boundary: the cutoff is "more than 365 days before today", i.e.
        # `date < cutoff`, not `<=` — exactly 365 days back still mints.
        today = datetime.date(2026, 9, 23)
        d364 = today - datetime.timedelta(days=364)
        d365 = today - datetime.timedelta(days=365)
        d366 = today - datetime.timedelta(days=366)
        for d, expect_refused in ((d364, False), (d365, False), (d366, True)):
            with self.subTest(days_back=(today - d).days):
                entries = [_entry("sonnet", "5", ("starting", d))]
                refused, warnings = pricing_update.filter_backdated_starting(
                    entries, today)
                self.assertEqual(bool(refused), expect_refused)
                self.assertEqual(bool(warnings), expect_refused)


def _rates(i):
    """A full rate dict scaled from input rate ``i`` (same shape the page
    parses to)."""
    return {"in_usd": float(i), "out_usd": i * 5.0, "cache_r_usd": i / 10,
            "cache_w_usd": i * 1.25, "cache_w_1h_usd": i * 2.0}


class TestPerRowRefusal(Base):
    """AOS-143 correction (G1/G2/INFO): the 365-day bound is decided PER
    ROW, and only for `starting` rows. In-force status still comes from the
    unfiltered page; each candidate row is then skipped only if it is itself
    a `starting` row dated more than 365 days back. A version whose page
    carries an old increase footnote AND a newer arrived increase mints the
    newer one; an in-force intro (`through`) row is never refused; and an
    old row already recorded with identical rates is a silent no-op."""

    TODAY = datetime.date(2026, 9, 23)
    OLD = TODAY - datetime.timedelta(days=400)        # 2025-08-19
    RECENT = datetime.date(2026, 9, 1)                # 22 days back
    INTRO_END = datetime.date(2026, 12, 31)

    def _resolve(self, prefix):
        row = self.f.conn.execute(
            "SELECT in_usd, effective_from FROM pricing WHERE model_prefix=?"
            " AND effective_from<=? ORDER BY effective_from DESC LIMIT 1",
            (prefix, _epoch(self.TODAY))).fetchone()
        return row and (row[0], row[1])

    def _two_increases(self):
        return [_entry("sonnet", "5", None, _rates(3)),
                _entry("sonnet", "5", ("starting", self.OLD), _rates(4)),
                _entry("sonnet", "5", ("starting", self.RECENT), _rates(5))]

    def _warnings(self, report):
        return [ln for ln in report.splitlines()
                if ln.startswith("BACKDATED-STARTING WARNING")]

    def test_recent_increase_mints_beside_an_old_refused_one_recorded_db(self):
        # val143b C1: the DB already holds the old $4 increase; the page
        # keeps its footnote and adds the next increase ($5, Sep 1 2026).
        # Main mints $5 @Sep 1 on the version and $5 @today on the family —
        # so must this branch; the stale $4 must not keep governing.
        self.f.price("claude-sonnet-5", _rates(4), _epoch(self.OLD))
        self.f.price("claude-sonnet-", _rates(4), _epoch(self.OLD))
        report = pricing_update.run_update(
            self.f.conn, self._two_increases(), self.TODAY, "t")
        self.assertEqual(self._resolve("claude-sonnet-5"),
                         (5.0, _epoch(self.RECENT)))
        self.assertEqual(self._resolve("claude-sonnet-"),
                         (5.0, _epoch(self.TODAY)))
        self.assertIn("2 row(s) inserted", report)
        # the old row is already recorded with identical rates: no warning
        self.assertEqual(self._warnings(report), [])

    def test_recent_increase_mints_beside_an_old_refused_one_fresh_db(self):
        # val143b C2: fresh DB. Only the old row is refused; the base $3 stays
        # suppressed (a conditional is in force for the version).
        report = pricing_update.run_update(
            self.f.conn, self._two_increases(), self.TODAY, "t")
        self.assertEqual(
            [(r[1], r[6]) for r in self.f.pricing_rows("claude-sonnet-5")],
            [(5.0, _epoch(self.RECENT))])
        self.assertEqual(self._resolve("claude-sonnet-"),
                         (5.0, _epoch(self.TODAY)))
        warns = self._warnings(report)
        self.assertEqual(len(warns), 1)
        self.assertIn(
            f"a starting row dated {self.OLD.isoformat()} is more than 365"
            " days old and was not recorded; the rows already recorded for"
            " Claude Sonnet 5 are unchanged", warns[0])
        self.assertNotIn(self.RECENT.isoformat(), warns[0])

    def test_build_candidates_skips_only_the_refused_row(self):
        refused, _ = pricing_update.filter_backdated_starting(
            self._two_increases(), self.TODAY)
        cands = pricing_update.build_candidates(
            self._two_increases(), self.TODAY, refused)
        own = [(c["rates"]["in_usd"], c["effective_from"]) for c in cands
               if c["prefix"] == "claude-sonnet-5"]
        self.assertEqual(own, [(5.0, _epoch(self.RECENT))])

    def test_in_force_intro_row_is_never_refused(self):
        # val143b B1/B2 (pins mutant V26): an in-force `through` intro on a
        # version that also lists a starting row 400 days back. Only the
        # starting row is refused; the intro mints today on the version and
        # the family, and the base $3 stays suppressed.
        for with_base in (False, True):
            with self.subTest(with_base=with_base):
                self.tearDown()
                self.setUp()
                entries = ([_entry("sonnet", "5", None, _rates(3))]
                           if with_base else [])
                entries += [
                    _entry("sonnet", "5", ("through", self.INTRO_END),
                           _rates(2)),
                    _entry("sonnet", "5", ("starting", self.OLD), _rates(4))]
                report = pricing_update.run_update(
                    self.f.conn, entries, self.TODAY, "t")
                self.assertEqual(
                    [(r[1], r[6])
                     for r in self.f.pricing_rows("claude-sonnet-5")],
                    [(2.0, _epoch(self.TODAY))])
                self.assertEqual(self._resolve("claude-sonnet-"),
                                 (2.0, _epoch(self.TODAY)))
                self.assertEqual(len(self._warnings(report)), 1)

    def test_old_through_row_is_not_bound_checked(self):
        # The bound applies to `starting` rows only: a `through` row dated
        # far back (an intro that ended long ago) is never refused/warned.
        old_intro = self.TODAY - datetime.timedelta(days=900)
        entries = [_entry("sonnet", "5", None, _rates(3)),
                   _entry("sonnet", "5", ("through", old_intro), _rates(2))]
        refused, warnings = pricing_update.filter_backdated_starting(
            entries, self.TODAY)
        self.assertEqual((refused, warnings), (set(), []))
        report = pricing_update.run_update(
            self.f.conn, entries, self.TODAY, "t")
        self.assertEqual(self._warnings(report), [])
        self.assertEqual(self._resolve("claude-sonnet-5"),
                         (3.0, _epoch(self.TODAY)))

    def test_weekly_rerun_over_already_recorded_old_row_prints_no_warning(self):
        # Steady state: the increase was recorded when it arrived; a year+
        # later the page still carries it. Every weekly re-run is a no-op for
        # that row — no warning, since it IS recorded.
        self.f.price("claude-sonnet-5", _rates(4), _epoch(self.OLD))
        entries = [_entry("sonnet", "5", None, _rates(3)),
                   _entry("sonnet", "5", ("starting", self.OLD), _rates(4))]
        for week in range(3):
            today = self.TODAY + datetime.timedelta(days=7 * week)
            report = pricing_update.run_update(self.f.conn, entries, today,
                                               "t")
            self.assertEqual(self._warnings(report), [], today)
            self.assertIn("0 row(s) inserted", report)

    def test_old_row_recorded_with_different_rates_still_warns(self):
        # Only an IDENTICAL recorded row (same prefix, date and rates) is a
        # silent no-op; a differing one is genuinely not recorded -> warns.
        self.f.price("claude-sonnet-5", _rates(7), _epoch(self.OLD))
        entries = [_entry("sonnet", "5", ("starting", self.OLD), _rates(4))]
        report = pricing_update.run_update(
            self.f.conn, entries, self.TODAY, "t")
        self.assertEqual(len(self._warnings(report)), 1)


class TestValueBounds(Base):
    """Fix 2: money() is permissive about magnitude on purpose — a
    non-finite or absurd rate refuses the WHOLE run, atomically, before any
    write, with EXIT_BOUNDS_REFUSED (never the fetch-failure or other-error
    code — AOS-143 correction, F1). AOS-143 correction, F2: a malformed
    numeric token — a thousands separator, more than one decimal point, or
    any exponent marker (complete or dangling after a bare `.`) — is never
    truncated to its leading digits; it refuses the whole run instead. An
    in_usd/out_usd under MIN_INOUT_RATE_USD is refused too (cache rates keep
    no lower bound)."""

    def _page(self, in_cell, out_cell="$10 / MTok"):
        return self._html([("Claude Sonnet 5", in_cell, "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok", out_cell)])

    def test_400_digit_rate_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$" + "9" * 400 + " / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_over_10000_per_mtok_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$15000 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_exactly_10000_per_mtok_is_accepted(self):
        # Boundary: the cap is "> 10000", not ">= 10000".
        path = self.f.write_html(self._page("$10000 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5")[0][1], 10000.0)

    def test_parse_models_raises_directly_for_non_finite(self):
        with self.assertRaises(pricing_update.PricingRefused) as ctx:
            pricing_update.parse_models(self._page("$" + "9" * 400))
        self.assertIn("out of bounds", str(ctx.exception))

    def test_exponent_notation_refuses_whole_run_not_mantissa(self):
        # $1e309 must never mint $1 (truncated mantissa) — it is malformed
        # and refuses the whole run, digest unchanged.
        before = self.f.digest()
        path = self.f.write_html(self._page("$1e309 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_uppercase_signed_exponent_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$1E+5 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_thousands_separator_refuses_whole_run(self):
        # AOS-143 correction, F2: "$1,500" used to mint "$1" (truncated at
        # the comma) — the comma makes the whole token malformed instead.
        before = self.f.digest()
        path = self.f.write_html(self._page("$1,500 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_two_decimal_points_refuses_whole_run(self):
        # AOS-143 correction, F2: "$4.00.00" used to mint "$4".
        before = self.f.digest()
        path = self.f.write_html(self._page("$4.00.00 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_dangling_exponent_refuses_whole_run(self):
        # AOS-143 correction, F2: "$1.e3" used to mint "$1" — a bare "."
        # with no digits after it, immediately followed by an exponent
        # marker, is just as malformed as a complete exponent form.
        before = self.f.digest()
        path = self.f.write_html(self._page("$1.e3 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_money_rejects_malformed_numbers_accepts_legit_ones(self):
        for bad in ("$1e309", "$1E+5 / MTok", "$5e-2", "$1,500", "$4.00.00",
                    "$1.e3"):
            with self.subTest(cell=bad):
                with self.assertRaises(pricing_update.PricingRefused):
                    pricing_update.money(bad)
        for text, expect in (("$2.50 / MTok", 2.5), ("$3", 3.0),
                             ("$3.75", 3.75), ("$0.30", 0.30),
                             ("$0.08", 0.08)):
            with self.subTest(cell=text):
                self.assertEqual(pricing_update.money(text), expect)

    def test_zero_in_usd_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$0 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_zero_out_usd_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$2 / MTok", out_cell="$0 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_below_floor_in_usd_refuses_whole_run(self):
        # AOS-143 correction, F4: below MIN_INOUT_RATE_USD is refused even
        # though it is not zero or negative — the old bound was "<= 0" only.
        before = self.f.digest()
        path = self.f.write_html(self._page("$0.0000001 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_at_floor_in_usd_is_accepted(self):
        # Boundary: the floor is "< 0.01", not "<= 0.01".
        path = self.f.write_html(self._page("$0.01 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5")[0][1], 0.01)

    def test_zero_cache_rate_is_unaffected(self):
        # Cache rates keep no lower bound: a legitimate $0 cache-read/write
        # rate is accepted, unlike in_usd/out_usd.
        path = self.f.write_html(self._html([
            ("Claude Sonnet 5", "$2 / MTok", "$0 / MTok", "$0 / MTok",
             "$0 / MTok", "$10 / MTok")]))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0)
        row = self.f.pricing_rows("claude-sonnet-5")[0]
        self.assertEqual((row[3], row[4], row[5]), (0.0, 0.0, 0.0))


class TestRowCap(Base):
    """Fix 3: more than MAX_CANDIDATES candidate rows refuses the whole run
    atomically — nothing planned or applied."""

    def test_501_candidate_rows_refused_atomically(self):
        today = datetime.date(2026, 9, 23)
        entries = [_entry("sonnet", str(1000 + i)) for i in range(501)]
        before = self.f.pricing_rows()
        with self.assertRaises(pricing_update.PricingRefused) as ctx:
            pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertIn("500", str(ctx.exception))
        self.assertEqual(self.f.pricing_rows(), before)

    def test_500_candidate_rows_is_not_refused(self):
        today = datetime.date(2026, 9, 23)
        # 499 distinct versions + 1 family row = 500, exactly at the cap.
        entries = [_entry("sonnet", str(1000 + i)) for i in range(499)]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertIn("row(s) inserted", report)


class TestWarningCap(Base):
    """AOS-143 correction, F3 sev4-low: BACKDATED-STARTING (and the other
    per-row STALE-PRICE / FUTURE-RATE) warnings are not candidate rows, so
    the MAX_CANDIDATES cap does not bound them — a page with many refused
    `starting` rows could otherwise print thousands of warning lines into
    the report the command echoes verbatim into the agent's context. Each
    category is capped at WARNING_CAP lines plus one "... and N more"
    summary line."""

    def test_backdated_starting_warnings_capped_with_summary_line(self):
        today = datetime.date(2026, 9, 23)
        old = today - datetime.timedelta(days=400)
        n = pricing_update.WARNING_CAP + 5
        entries = [_entry("sonnet", str(1000 + i), ("starting", old))
                  for i in range(n)]
        before = self.f.pricing_rows()
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        warn_lines = [ln for ln in report.splitlines()
                     if ln.startswith("BACKDATED-STARTING WARNING")]
        self.assertEqual(len(warn_lines), pricing_update.WARNING_CAP)
        self.assertIn(f"… and {n - pricing_update.WARNING_CAP} more"
                     " BACKDATED-STARTING warning(s)", report)
        # nothing was minted for any of the refused rows, capped or not
        self.assertEqual(self.f.pricing_rows(), before)

    def test_stale_price_warnings_capped_with_summary_line(self):
        today = datetime.date(2026, 9, 23)
        ended = today - datetime.timedelta(days=10)
        n = pricing_update.WARNING_CAP + 3
        entries = [_entry("sonnet", str(1000 + i), ("through", ended))
                  for i in range(n)]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        warn_lines = [ln for ln in report.splitlines()
                     if ln.startswith("STALE-PRICE WARNING")]
        self.assertEqual(len(warn_lines), pricing_update.WARNING_CAP)
        self.assertIn(f"… and {n - pricing_update.WARNING_CAP} more"
                     " STALE-PRICE warning(s)", report)

    def test_under_cap_warnings_print_no_summary_line(self):
        today = datetime.date(2026, 9, 23)
        old = today - datetime.timedelta(days=400)
        entries = [_entry("sonnet", "5", ("starting", old))]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertIn("BACKDATED-STARTING WARNING", report)
        self.assertNotIn("more BACKDATED-STARTING warning(s)", report)


class TestErrorSanitization(Base):
    """Fix 4: no raw, unsanitized page text ever reaches a printed error."""

    def test_safe_error_text_strips_esc_cr_and_bidi(self):
        raw = "Claude Sonnet 5\x1b[31mevil\r‮reordered⁦x"
        clean = pricing_update._safe_error_text(raw)
        self.assertNotIn("\x1b", clean)
        self.assertNotIn("\r", clean)
        self.assertNotIn("‮", clean)
        self.assertNotIn("⁦", clean)
        self.assertIn("evil", clean)
        self.assertIn("reordered", clean)

    def test_safe_error_text_strips_additional_bidi_control_chars(self):
        # AOS-143 correction, F5: U+200E LRM, U+200F RLM and U+061C ALM carry
        # the Bidi_Control property but were missing from the original
        # range-only strip set.
        raw = "A‎B‏C؜D"
        clean = pricing_update._safe_error_text(raw)
        self.assertNotIn("‎", clean)
        self.assertNotIn("‏", clean)
        self.assertNotIn("؜", clean)
        self.assertEqual(clean, "ABCD")

    def test_safe_error_text_caps_length(self):
        clean = pricing_update._safe_error_text("x" * 1000)
        self.assertLessEqual(len(clean), 201)

    def test_safe_error_text_strips_c1_controls(self):
        # AOS-143 correction: U+009B (the 8-bit form of CSI — a terminal
        # escape sequence without a 7-bit ESC byte) and U+0085 (NEL) are C1
        # controls (U+0080-U+009F), previously left un-stripped.
        raw = "A\x9b31mB\x85C"
        clean = pricing_update._safe_error_text(raw)
        self.assertNotIn("\x9b", clean)
        self.assertNotIn("\x85", clean)
        self.assertIn("A", clean)
        self.assertIn("31mB", clean)
        self.assertIn("C", clean)

    def test_header_error_message_has_no_raw_control_or_bidi_chars(self):
        # The "output" column is renamed away, so the header guard raises;
        # the message must carry the escape/bidi characters stripped.
        in_text = _LAYOUT_HEADER[self.LAYOUT]["in"] + "\x1b[31m"
        html = self._html([("Claude Sonnet 5", "$2 / MTok", "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok")],
                          header={"in": in_text, "out": "Weird‮Column"})
        with self.assertRaises(ValueError) as ctx:
            pricing_update.parse_models(html)
        msg = str(ctx.exception)
        self.assertIn("unexpected pricing table header", msg)
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("‮", msg)

    def test_header_error_message_is_length_capped(self):
        # str(<list of cell strings>) has no length limit on its own — a
        # header row padded with one very long cell must still print a
        # short, bounded message.
        in_text = _LAYOUT_HEADER[self.LAYOUT]["in"] + "z" * 5000
        html = self._html([("Claude Sonnet 5", "$2 / MTok", "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok")],
                          header={"in": in_text, "out": None})  # no output
        with self.assertRaises(ValueError) as ctx:
            pricing_update.parse_models(html)
        self.assertIn("unexpected pricing table header", str(ctx.exception))
        self.assertLess(len(str(ctx.exception)), 300)

    def test_rate_cell_error_message_has_no_raw_control_or_bidi_chars(self):
        name_cell = "Claude Sonnet 5\x1b[31mevil‮reordered"
        html = self._html([(name_cell, "not a dollar amount",
                            "$2.50 / MTok", "$4 / MTok", "$0.20 / MTok",
                            "$10 / MTok")])
        with self.assertRaises(ValueError) as ctx:
            pricing_update.parse_models(html)
        msg = str(ctx.exception)
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("‮", msg)
        self.assertIn("evil", msg)          # sanitized, not deleted outright
        self.assertIn("reordered", msg)

    def test_cli_stderr_carries_no_raw_control_chars_on_parse_failure(self):
        html = self._html([("Claude Sonnet 5\x1b[31m", "nope",
                            "$2.50 / MTok", "$4 / MTok", "$0.20 / MTok",
                            "$10 / MTok")])
        path = self.f.write_html(html)
        code, out, err = self.f.cli("--html", str(path))
        # A structural parse failure (no dollar amount at all in the "in"
        # cell) is the "other errors" outcome, distinct from a fetch failure
        # and from a bounds refusal (AOS-143 correction, F1).
        self.assertEqual(code, pricing_update.EXIT_OTHER_ERROR)
        self.assertNotIn("\x1b", err)


class TestFetchVsParseVsBoundsExitCodes(Base):
    """AOS-143 correction, F1: the three failure modes a plain refresh run
    can end in are distinguished by exit code, so the command's manual
    fallback — which has none of the parser's bounds — can be wired to run
    ONLY for a genuine fetch failure, never for a page the script refused or
    could not structurally parse."""

    def test_simulated_fetch_failure_returns_the_fetch_code(self):
        # No --html: main() takes the real network branch. urlopen is
        # patched to fail the way a down host or a bad connection would,
        # without touching the network.
        import unittest.mock
        import urllib.error

        before = self.f.pricing_rows()
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch(
                "pricing_update.urllib.request.urlopen",
                side_effect=urllib.error.URLError("simulated connection"
                                                  " failure")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = pricing_update.main(["--db", str(self.f.path)])
        self.assertEqual(code, pricing_update.EXIT_FETCH_FAILED)
        self.assertIn("pricing page fetch failed", err.getvalue())
        self.assertEqual(self.f.pricing_rows(), before)

    def test_fetch_failure_error_text_is_sanitized(self):
        # F2/F3 (pre-existing, corrected here): a raw HTTP status line can
        # carry attacker- or MITM-controlled terminal escape sequences —
        # main() must sanitize it with the same sanitizer used for
        # page-derived error text before printing it.
        import unittest.mock

        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch(
                "pricing_update.urllib.request.urlopen",
                side_effect=OSError("HTTP/1.1 500 Oops\x1b]0;pwned\x07"
                                    "\x1b[2Jbad")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = pricing_update.main(["--db", str(self.f.path)])
        self.assertEqual(code, pricing_update.EXIT_FETCH_FAILED)
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("\x07", err.getvalue())
        self.assertIn("Oops", err.getvalue())

    def test_bounds_refusal_and_other_error_never_use_the_fetch_code(self):
        bounds_path = self.f.write_html(self._html([
            ("Claude Sonnet 5", "$0 / MTok", "$2.50 / MTok", "$4 / MTok",
             "$0.20 / MTok", "$10 / MTok")]), name="bounds.html")
        other_path = self.f.write_html(self._html([
            ("Claude Sonnet 5", "nope", "$2.50 / MTok", "$4 / MTok",
             "$0.20 / MTok", "$10 / MTok")]), name="other.html")
        bounds_code, _, _ = self.f.cli("--html", str(bounds_path))
        other_code, _, _ = self.f.cli("--html", str(other_path))
        self.assertEqual(bounds_code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(other_code, pricing_update.EXIT_OTHER_ERROR)
        self.assertNotEqual(bounds_code, pricing_update.EXIT_FETCH_FAILED)
        self.assertNotEqual(other_code, pricing_update.EXIT_FETCH_FAILED)


class TestFutureOnlyNewestWarning(Base):
    """LOW (AOS-133 validation): a family's newest version listed only with
    a FUTURE `starting` date mints no family row — warn that the family
    default keeps its last recorded rate, rather than staying silent."""

    def test_future_starting_only_newest_warns(self):
        today = datetime.date(2026, 9, 15)
        entries = [
            _entry("sonnet", "5", ("starting", datetime.date(2026, 10, 1))),
            _entry("sonnet", "4.6", None, dict(RATES, in_usd=3.0)),
        ]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertIn("FUTURE-RATE WARNING", report)
        self.assertIn("Claude Sonnet 5", report)
        self.assertIn("2026-10-01", report)
        # only the seed row remains — no new claude-sonnet- row minted
        fam_rows = self.f.pricing_rows("claude-sonnet-")
        self.assertEqual([r for r in fam_rows if r[6] != 0], [])
        # the older, unconditioned version still mints normally
        self.assertEqual(len(self.f.pricing_rows("claude-sonnet-4-6")), 1)

    def test_no_warning_once_the_family_has_any_in_force_rate(self):
        today = datetime.date(2026, 9, 15)
        entries = [
            _entry("sonnet", "5", None, dict(RATES, in_usd=3.0)),
        ]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertNotIn("FUTURE-RATE WARNING", report)


class TestStrictNumberToken(Base):
    """AOS-143 correction, round 2, F6: the round-1 token class
    (`[0-9,.eE+-]`) still let a separator, magnitude suffix, or written-out
    exponent it did not recognize truncate a cell to its leading digits
    instead of refusing it — e.g. `$1 500` (a thousands-grouping space, ASCII
    or one of four Unicode variants) minted `1`, silently underpricing by
    3+ orders of magnitude. The token class now also captures every one of
    those characters, so the WHOLE run fails the strict all-digits check and
    refuses, instead of stopping short and keeping only the well-formed
    prefix. `e`/`E`/`+`/`-` stay in the class from round 1 — dropping them
    would stop capturing `e309` in `$1e309`, silently reintroducing the very
    truncation-to-`$1` bug round 1 fixed."""

    def _page(self, in_cell):
        return self._html([("Claude Sonnet 5", in_cell, "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok", "$10 / MTok")])

    def test_thousands_grouping_space_variants_refuse_whole_run(self):
        # ASCII space, thin space (U+2009), narrow no-break space (U+202F),
        # no-break space (U+00A0), figure space (U+2007) — every one of them
        # is a run character, so "$1<space>500" is captured whole and fails
        # the strict digits-only check, rather than truncating to "1".
        for space in (" ", " ", " ", " ", " "):
            with self.subTest(space=repr(space)):
                before = self.f.digest()
                path = self.f.write_html(self._page(f"$1{space}500 / MTok"))
                code, out, err = self.f.cli("--html", str(path))
                self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
                self.assertIn("REFUSED", out)
                self.assertEqual(self.f.digest(), before)
                self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_apostrophe_thousands_separator_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$1'500 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_underscore_thousands_separator_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$1_500 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_k_magnitude_suffix_refuses_whole_run(self):
        # Previously truncated to "1" (a 1000x underprice) instead of
        # refusing the cell.
        before = self.f.digest()
        path = self.f.write_html(self._page("$1k / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_m_magnitude_suffix_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$1M / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_written_out_exponent_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$1x10^6 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_leading_minus_sign_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$-4 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_leading_plus_sign_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$+4 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_non_ascii_digit_is_still_the_safe_unparseable_path(self):
        # A non-ASCII digit (Arabic-Indic ONE, U+0661) is outside the token
        # class entirely, so no run is captured at all — money() returns
        # None (not a refusal), and parse_models raises its pre-existing
        # generic "unparseable rate cell" error (EXIT_OTHER_ERROR), exactly
        # as it did before this fix. Still safe (STOP either way), and
        # unchanged by round 2.
        self.assertIsNone(pricing_update.money("$١"))
        before = self.f.digest()
        path = self.f.write_html(self._page("$١"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_OTHER_ERROR)
        self.assertEqual(self.f.digest(), before)

    def test_exponent_and_legit_forms_still_behave_as_round_one_fixed(self):
        # Regression guard: e/E/+/- stay in the token class from round 1 —
        # exponent notation must keep refusing the whole run, never
        # silently truncate to the leading mantissa digit(s).
        for bad in ("$1e309", "$1E+5", "$5e-2", "$1,500", "$4.00.00",
                    "$1.e3"):
            with self.subTest(cell=bad):
                with self.assertRaises(pricing_update.PricingRefused):
                    pricing_update.money(bad)
        for text, expect in (("$3", 3.0), ("$3.75", 3.75), ("$0.30", 0.30),
                             ("$5 / MTok", 5.0), ("$5/MTok", 5.0),
                             ("$1/MTok", 1.0)):
            with self.subTest(cell=text):
                self.assertEqual(pricing_update.money(text), expect)

    def test_committed_fixture_pages_still_parse_under_the_stricter_rule(self):
        # The stricter token class must not refuse any real, currently
        # published rate cell — every committed fixture page still parses
        # end to end.
        fixtures_dir = pathlib.Path(__file__).resolve().parent / "fixtures"
        for name in ("pricing-page-two-row-header-2026-09-23.html",
                     "pricing-page-current.html", "pricing-page-increase.html",
                     "pricing-page-expired-intro.html", "pricing-page.html"):
            with self.subTest(fixture=name):
                html = (fixtures_dir / name).read_text()
                entries = pricing_update.parse_models(html)
                self.assertTrue(entries)


class TestTrailingTruncationLookalikes(Base):
    """AOS-143 round 4, N1: the token class's captured run can still stop
    ONE CHARACTER TOO EARLY when the very next character is a separator or
    digit it does not recognize — e.g. a fullwidth comma (U+FF0C) or an
    Arabic-Indic digit are outside :data:`pricing_update._MONEY_TOKEN_RE`'s
    class entirely, so ``$1，500`` and ``$3٠٠`` each match only their leading
    digit, which then passes :data:`pricing_update._STRICT_NUMBER_RE`
    unmodified — silently minting `1` or `3` instead of refusing an
    obviously-truncated cell. :func:`pricing_update._rate_token_truncated`
    checks the character immediately after the matched run for exactly this
    shape (a Unicode digit, a separator lookalike, or a dash directly
    followed by a digit) and refuses the whole cell too."""

    def _page(self, in_cell):
        return self._html([("Claude Sonnet 5", in_cell, "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok", "$10 / MTok")])

    def test_fullwidth_comma_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$1，500 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_arabic_indic_digit_run_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$3٠٠ / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_en_dash_range_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$3–4 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_repeated_decimal_point_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$3.5.1 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_money_unit_refusals_and_controls(self):
        for bad in ("$1，500", "$3٠٠", "$3–4", "$3.5.1"):
            with self.subTest(cell=bad):
                with self.assertRaises(pricing_update.PricingRefused):
                    pricing_update.money(bad + " / MTok")
        for text, expect in (("$3 / MTok", 3.0), ("$3/MTok", 3.0)):
            with self.subTest(cell=text):
                self.assertEqual(pricing_update.money(text), expect)


class TestFetchHangAndSizeBounds(Base):
    """AOS-143 correction, round 2 (hang/DoS fix): `urlopen(timeout=N)` only
    bounds each individual socket operation, not the fetch as a whole — a
    server that keeps the connection open and trickles a byte through just
    before each such operation would time out could hang the fetch
    indefinitely, and `resp.read()` has no cap of its own on response body
    size. `_fetch_page` now enforces one real wall-clock deadline across
    connect + the whole read (a background thread joined with a timeout,
    since urllib gives no other way to bound a slow-but-technically-alive
    read) and a hard cap on total body size, exceeding either -> a fetch
    failure (EXIT_FETCH_FAILED) with a sanitized message. These run a real
    local stdlib `http.server` in a background thread — no mocked
    `urlopen` — so the bound is exercised against genuine socket behaviour."""

    @staticmethod
    def _serve(handler_cls):
        import http.server
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread

    def test_trickle_server_times_out_instead_of_hanging(self):
        import http.server

        class TrickleHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                # One byte every 0.3s — always faster than the per-socket-
                # operation `timeout=` below (1s), so a naive per-op-only
                # timeout would never trip; bounded to ~2.1s so the server
                # thread self-terminates for test teardown.
                for _ in range(7):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    time.sleep(0.3)

            def log_message(self, *a, **k):
                pass

        httpd, thread = self._serve(TrickleHandler)
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/pricing"
            started = time.monotonic()
            with unittest.mock.patch("pricing_update.URL", url), \
                    unittest.mock.patch("pricing_update.FETCH_TIMEOUT_S", 1.0):
                code, out, err = self.f.cli()
            elapsed = time.monotonic() - started
            self.assertEqual(code, pricing_update.EXIT_FETCH_FAILED)
            self.assertIn("fetch failed", err)
            # Must return at ~the patched deadline, not hang for the whole
            # trickle (~2.1s) or the module-default 30s.
            self.assertLess(elapsed, 2.0)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_oversized_body_is_rejected_without_buffering_it_all(self):
        import http.server

        body = b"x" * 50000

        class OversizedHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a, **k):
                pass

        httpd, thread = self._serve(OversizedHandler)
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/pricing"
            with unittest.mock.patch("pricing_update.URL", url), \
                    unittest.mock.patch("pricing_update.FETCH_MAX_BYTES", 1024):
                code, out, err = self.f.cli()
            self.assertEqual(code, pricing_update.EXIT_FETCH_FAILED)
            self.assertIn("fetch failed", err)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_normal_sized_fast_response_is_unaffected(self):
        import http.server

        page = self._html([("Claude Sonnet 5", "$2 / MTok", "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok", "$10 / MTok")])
        body = page.encode()

        class FastHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a, **k):
                pass

        httpd, thread = self._serve(FastHandler)
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/pricing"
            with unittest.mock.patch("pricing_update.URL", url):
                code, out, err = self.f.cli()
            self.assertEqual(code, 0)
            self.assertEqual(self.f.pricing_rows("claude-sonnet-5")[0][1], 2.0)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestHtmlFileReadBounds(Base):
    """AOS-143 round 4, F2: ``--html`` is reached only through the command's
    fallback, which now requires an explicit Claude Code permission prompt
    (round 4, F1) — but once approved, the file it reads used to be
    unbounded: ``Path.read_text()`` had no size cap (unlike the network
    fetch's :data:`pricing_update.FETCH_MAX_BYTES`) and could block forever
    reading a FIFO. :func:`pricing_update._read_html_file` now applies the
    same 5MB cap and refuses non-regular files (``os.stat`` +
    ``stat.S_ISREG``) BEFORE ever opening them, so a FIFO cannot block the
    process at all — mapped to :data:`pricing_update.EXIT_OTHER_ERROR`, not
    the fallback-eligible :data:`pricing_update.EXIT_FETCH_FAILED`, since
    the file IS there but is not a trustworthy source."""

    SCRIPT = (pathlib.Path(__file__).resolve().parent.parent / "scripts"
              / "pricing_update.py")

    def test_oversized_html_file_is_refused_db_unchanged(self):
        # Padded with an otherwise-valid table so the cap is what refuses
        # this, not incidental garbage content: without the size check, this
        # file would read, parse, and mint a real row (a weaker version of
        # this test that used pure padding would still "pass" against a
        # reverted fix, since an unbounded read_text() of non-HTML garbage
        # also ends up EXIT_OTHER_ERROR via a parse failure).
        before = (self.f.digest(), self.f.pricing_rows())
        page = self._page("$2 / MTok")
        pad_len = pricing_update.FETCH_MAX_BYTES + 1 - len(page)
        padded = page + f"<!--{'x' * pad_len}-->"
        self.assertGreater(len(padded), pricing_update.FETCH_MAX_BYTES)
        big = pathlib.Path(self.f.tmp.name) / "big.html"
        big.write_text(padded)
        code, out, err = self.f.cli("--html", str(big))
        self.assertEqual(code, pricing_update.EXIT_OTHER_ERROR, err)
        self.assertIn("exceeded", err)
        self.assertEqual((self.f.digest(), self.f.pricing_rows()), before)

    def _page(self, in_cell):
        return self._html([("Claude Sonnet 5", in_cell, "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok", "$10 / MTok")])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_fifo_html_path_is_refused_promptly_not_blocked(self):
        fifo = pathlib.Path(self.f.tmp.name) / "page.fifo"
        os.mkfifo(fifo)
        env = dict(os.environ, TOKEN_TELEMETRY_DB=str(self.f.path))
        before = self.f.digest()
        try:
            result = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--html", str(fifo)],
                env=env, capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("--html on a FIFO blocked instead of being refused"
                      " promptly as a non-regular file")
        self.assertEqual(result.returncode, pricing_update.EXIT_OTHER_ERROR,
                         result.stderr)
        self.assertEqual(self.f.digest(), before)


class TestDbErrorExitCode(Base):
    """AOS-143 correction, round 2, INFO: a DB error (e.g. an unopenable or
    unwritable DB) must map to the documented EXIT_OTHER_ERROR (3), not an
    uncaught traceback that exits 1 like a bound refusal."""

    def test_unopenable_db_returns_other_error_code_not_a_traceback(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            # A directory, not a file, at the DB path: Path.exists() is
            # True (passing main()'s "no DB yet" guard), but
            # sqlite3.connect() on a directory always fails.
            db_dir = pathlib.Path(tmp.name) / "not-a-file.db"
            db_dir.mkdir()
            html_path = pathlib.Path(tmp.name) / "page.html"
            html_path.write_text(self._html([
                ("Claude Sonnet 5", "$2 / MTok", "$2.50 / MTok",
                 "$4 / MTok", "$0.20 / MTok", "$10 / MTok")]))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = pricing_update.main(
                    ["--db", str(db_dir), "--html", str(html_path)])
            self.assertEqual(code, pricing_update.EXIT_OTHER_ERROR)
            self.assertIn("pricing DB error", err.getvalue())
        finally:
            tmp.cleanup()


def _page_date(d):
    """``d`` the way the page writes a condition date (``August 1, 2026``)."""
    return f"{d:%B} {d.day}, {d.year}"


class TestPageDrivenRunBounds(Base):
    """AOS-151: the row cap, the 365-day `starting` rule and the warning cap
    are enforced in :func:`pricing_update.run_update`, after parsing — these
    drive them from a real ``--html`` page through ``main()`` instead of
    hand-built entries, so they run once per page layout (see the generated
    ``TwoRowLayout`` twin) and no layout's parse path can bypass them."""

    SONNET = ("$2 / MTok", "$2.50 / MTok", "$4 / MTok", "$0.20 / MTok",
              "$10 / MTok")

    @staticmethod
    def _today():
        return datetime.datetime.now(tz=datetime.timezone.utc).date()

    def test_501_candidate_rows_from_a_page_refuse_the_whole_run(self):
        # 500 unconditional versions -> 500 specific + 1 family row = 501.
        rows = [(f"Claude Sonnet {1000 + i}", *self.SONNET)
                for i in range(500)]
        before = self.f.digest()
        path = self.f.write_html(self._html(rows))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, pricing_update.EXIT_BOUNDS_REFUSED, out + err)
        self.assertIn("500", out)
        self.assertEqual(self.f.digest(), before)

    def test_500_candidate_rows_from_a_page_are_not_refused(self):
        rows = [(f"Claude Sonnet {1000 + i}", *self.SONNET)
                for i in range(499)]
        path = self.f.write_html(self._html(rows))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.f.pricing_rows("claude-sonnet-1000")), 1)

    def test_starting_row_over_365_days_back_is_refused_and_warned(self):
        old = self._today() - datetime.timedelta(days=400)
        rows = [(f"Claude Sonnet 5 (starting {_page_date(old)})",
                 "$4 / MTok", "$5 / MTok", "$8 / MTok", "$0.40 / MTok",
                 "$20 / MTok")]
        path = self.f.write_html(self._html(rows))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0, out + err)
        self.assertIn("BACKDATED-STARTING WARNING", out)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_starting_row_within_365_days_mints_dated_its_start(self):
        recent = self._today() - datetime.timedelta(days=30)
        rows = [(f"Claude Sonnet 5 (starting {_page_date(recent)})",
                 "$4 / MTok", "$5 / MTok", "$8 / MTok", "$0.40 / MTok",
                 "$20 / MTok")]
        path = self.f.write_html(self._html(rows))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0, out + err)
        got = self.f.pricing_rows("claude-sonnet-5")
        self.assertEqual([(r[1], r[6]) for r in got], [(4.0, _epoch(recent))])

    def test_backdated_warnings_from_a_page_are_capped(self):
        old = self._today() - datetime.timedelta(days=400)
        n = pricing_update.WARNING_CAP + 4
        rows = [(f"Claude Sonnet {1000 + i} (starting {_page_date(old)})",
                 *self.SONNET) for i in range(n)]
        path = self.f.write_html(self._html(rows))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0, out + err)
        lines = [ln for ln in out.splitlines()
                 if ln.startswith("BACKDATED-STARTING WARNING")]
        self.assertEqual(len(lines), pricing_update.WARNING_CAP)
        self.assertIn(f"… and {n - pricing_update.WARNING_CAP} more"
                      " BACKDATED-STARTING warning(s)", out)


class TestLayoutHelper(unittest.TestCase):
    """Guards on the parametrization itself: each layout the helper renders
    must reach the parser through THAT layout's header path (a two-row page
    silently parsed as single-row would make the twin classes prove
    nothing), and map every rate to the right column."""

    DISTINCT = ("Claude Sonnet 5", "$1.10 / MTok", "$2.20 / MTok",
                "$3.30 / MTok", "$0.40 / MTok", "$5.50 / MTok")

    def test_each_layout_maps_every_rate_to_its_own_column(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                entries = pricing_update.parse_models(
                    _table_html([self.DISTINCT], layout))
                self.assertEqual(entries[0]["rates"], {
                    "in_usd": 1.1, "cache_w_usd": 2.2,
                    "cache_w_1h_usd": 3.3, "cache_r_usd": 0.4,
                    "out_usd": 5.5})

    def test_two_row_page_is_located_by_the_two_row_path(self):
        html = _table_html([self.DISTINCT], "two-row")
        self.assertNotIn(pricing_update._SINGLE_ROW_MARKER, html.lower())
        tc = pricing_update.TableCollector()
        tc.feed(html)
        header, body, needles = pricing_update._locate_rate_table(tc.tables)
        self.assertIs(needles, pricing_update._TWO_ROW_NEEDLES)
        self.assertEqual(header[0], "Name")
        self.assertEqual(len(body), 1)

    def test_two_row_group_row_without_prompt_caching_is_not_the_table(self):
        # The batch table has a group row too ("Model | Batch tokens") —
        # only the group row carrying BOTH markers is the rate table.
        html = _table_html([self.DISTINCT], "two-row").replace(
            "Prompt caching", "Batch")
        with self.assertRaises(ValueError) as ctx:
            pricing_update.parse_models(html)
        self.assertIn("not found", str(ctx.exception))

    def test_ambiguous_two_row_header_is_refused(self):
        # Two cells matching one needle must refuse, never pick either.
        html = _table_html([self.DISTINCT], "two-row",
                           header={"cr": "Input hits"})
        with self.assertRaises(ValueError):
            pricing_update.parse_models(html)

    def test_every_html_building_class_has_a_two_row_twin(self):
        import inspect
        missing = [cls.__name__ for cls in Base.__subclasses__()
                   if "self._html(" in inspect.getsource(cls)
                   and cls.LAYOUT == "single-row"
                   and f"{cls.__name__}TwoRowLayout" not in globals()]
        self.assertEqual(missing, [])


class TestTwoRowLiveFixture(Base):
    """AOS-151: the committed capture of the live page (2026-09-23), parsed
    and applied end to end through ``--html`` on a fresh DB."""

    FIXTURE = (pathlib.Path(__file__).resolve().parent / "fixtures"
               / "pricing-page-two-row-header-2026-09-23.html")

    def test_bare_refresh_of_the_live_capture_records_every_listed_model(self):
        code, out, err = self.f.cli("--html", str(self.FIXTURE))
        self.assertEqual(code, 0, out + err)
        expect = {
            "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
            "claude-opus-5-5": (4.0, 20.0, 0.2, 5.0, 8.0),
            "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5, 4.0),
            "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25, 2.0),
            "claude-mythos-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
            "claude-opus-4-1": (15.0, 75.0, 1.5, 18.75, 30.0),
            "claude-3-5-haiku": (0.8, 4.0, 0.08, 1.0, 1.6),
            # family defaults follow each family's first-listed (newest)
            "claude-opus-": (4.0, 20.0, 0.2, 5.0, 8.0),
            "claude-sonnet-": (2.0, 10.0, 0.2, 2.5, 4.0),
        }
        for prefix, rates in expect.items():
            with self.subTest(prefix=prefix):
                latest = self.f.pricing_rows(prefix)[-1]
                self.assertEqual(tuple(latest[1:6]), rates)

    def test_fast_mode_and_batch_tables_are_not_parsed_as_rates(self):
        entries = pricing_update.parse_models(self.FIXTURE.read_text())
        self.assertEqual(len(entries), 18)
        opus55 = [e for e in entries
                  if (e["family"], e["version"]) == ("opus", "5.5")]
        # $8/$40 (fast mode) and $2/$10 (batch) must not appear.
        self.assertEqual([e["rates"]["in_usd"] for e in opus55], [4.0])


# Every class that renders a page through self._html() runs a second time
# against the live page's two-row header (AOS-151), so no AOS-143 bound —
# strict token, value bounds, sanitization, exit codes, fetch/file bounds,
# row cap, 365-day rule, warning cap — can be skipped by that parse path.
# TestLayoutHelper.test_every_html_building_class_has_a_two_row_twin fails
# if a new such class is added without being listed here.
for _cls in (TestValueBounds, TestErrorSanitization,
             TestFetchVsParseVsBoundsExitCodes, TestStrictNumberToken,
             TestTrailingTruncationLookalikes, TestFetchHangAndSizeBounds,
             TestHtmlFileReadBounds, TestDbErrorExitCode,
             TestPageDrivenRunBounds):
    _twin = f"{_cls.__name__}TwoRowLayout"
    globals()[_twin] = type(_twin, (_cls,), {
        "LAYOUT": "two-row", "__module__": __name__,
        "__doc__": f"{_cls.__name__}, against the two-row page header."})
del _cls, _twin


if __name__ == "__main__":
    unittest.main()
