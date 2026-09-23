"""AOS-143 parser bounds (`scripts/pricing_update.py`): pricing history is
insert-only (docs/TELEMETRY-CONTRACT.md §Pricing table), so a bad row minted
from the parsed pricing page is permanent. These tests bound what a single
page/run can mint: a `starting <d>` row cannot be minted more than a year
back or before a prefix's latest recorded rate (warned, not fatal); a
non-finite or absurd rate refuses the WHOLE run; more than 500 candidate rows
refuses the WHOLE run; and no raw, unsanitized page text ever reaches an
error message. Fixture DBs and in-test HTML only — never the network."""
import contextlib
import datetime
import hashlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import pricing_update

RATES = {"in_usd": 2.0, "out_usd": 10.0, "cache_r_usd": 0.2,
         "cache_w_usd": 2.5, "cache_w_1h_usd": 4.0}
INCREASE_FIXTURE = (pathlib.Path(__file__).resolve().parent / "fixtures"
                    / "pricing-page-increase.html")


def _epoch(d):
    return int(datetime.datetime.combine(
        d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())


def _entry(family, version, condition=None, rates=None):
    return {"family": family, "version": version,
            "rates": dict(rates or RATES), "condition": condition}


def _table_html(rows):
    """A minimal valid pricing table: the real header the page ships, plus
    one ``<td>`` row per ``(name, in, w5, w1h, cr, out)`` tuple in ``rows``."""
    header = ("<tr><th>Model</th><th>Base input tokens</th>"
              "<th>5m cache writes</th><th>1h cache writes</th>"
              "<th>Cache hits and refreshes</th><th>Output tokens</th></tr>")
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<html><body><table>{header}{body}</table></body></html>"


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
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()


class TestBackdatedStarting(Base):
    """Fix 1: an in-force `starting <d>` row is normally minted dated `d`
    however far back — refused (warned, not fatal) when `d` is more than a
    year before today, or earlier than the prefix's latest recorded rate."""

    def test_1970_starting_page_mints_nothing_and_warns(self):
        today = datetime.date(2026, 9, 23)
        entries = [_entry("sonnet", "5",
                          ("starting", datetime.date(1970, 1, 1)))]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertIn("BACKDATED-STARTING WARNING", report)
        self.assertIn("Claude Sonnet 5", report)
        self.assertIn("1970-01-01", report)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])
        self.assertIn("0 row(s) inserted", report)

    def test_starting_earlier_than_prefixs_latest_row_refused(self):
        today = datetime.date(2026, 9, 23)
        self.f.price("claude-sonnet-5", RATES, _epoch(datetime.date(2026, 9, 1)))
        before = self.f.pricing_rows("claude-sonnet-5")
        entries = [_entry("sonnet", "5",
                          ("starting", datetime.date(2026, 8, 15)),
                          dict(RATES, in_usd=9.0))]
        report = pricing_update.run_update(self.f.conn, entries, today, "t")
        self.assertIn("BACKDATED-STARTING WARNING", report)
        self.assertIn("Claude Sonnet 5", report)
        self.assertIn("2026-08-15", report)
        # the existing Sep-1 row is untouched: insert-only, and nothing else
        # was minted for the prefix
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), before)

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

    def test_filter_function_drops_only_the_offending_entry(self):
        today = datetime.date(2026, 9, 23)
        keep = _entry("opus", "5")
        drop = _entry("sonnet", "5", ("starting", datetime.date(1970, 1, 1)))
        filtered, warnings = pricing_update.filter_backdated_starting(
            self.f.conn, [keep, drop], today)
        self.assertEqual(filtered, [keep])
        self.assertEqual(len(warnings), 1)
        fam, ver, date, reason = warnings[0]
        self.assertEqual((fam, ver, date),
                         ("sonnet", "5", datetime.date(1970, 1, 1)))
        self.assertIn("365", reason)

    def test_future_starting_is_left_alone_not_bound_checked(self):
        # A future `starting` is never minted anyway (in_force) — the bound
        # check does not apply to it (no premature warning either).
        today = datetime.date(2026, 9, 23)
        entries = [_entry("sonnet", "5",
                          ("starting", datetime.date(2030, 1, 1)))]
        filtered, warnings = pricing_update.filter_backdated_starting(
            self.f.conn, entries, today)
        self.assertEqual(filtered, entries)
        self.assertEqual(warnings, [])


class TestValueBounds(Base):
    """Fix 2: money() is permissive about magnitude on purpose — a
    non-finite or absurd rate refuses the WHOLE run, atomically, before any
    write."""

    def _page(self, in_cell):
        return _table_html([("Claude Sonnet 5", in_cell, "$2.50 / MTok",
                            "$4 / MTok", "$0.20 / MTok", "$10 / MTok")])

    def test_400_digit_rate_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$" + "9" * 400 + " / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 2)
        self.assertIn("pricing page fetch/parse failed", err)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_over_10000_per_mtok_refuses_whole_run(self):
        before = self.f.digest()
        path = self.f.write_html(self._page("$15000 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 2)
        self.assertEqual(self.f.digest(), before)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5"), [])

    def test_exactly_10000_per_mtok_is_accepted(self):
        # Boundary: the cap is "> 10000", not ">= 10000".
        path = self.f.write_html(self._page("$10000 / MTok"))
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(self.f.pricing_rows("claude-sonnet-5")[0][1], 10000.0)

    def test_parse_models_raises_directly_for_non_finite(self):
        with self.assertRaises(ValueError) as ctx:
            pricing_update.parse_models(self._page("$" + "9" * 400))
        self.assertIn("out of bounds", str(ctx.exception))


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

    def test_safe_error_text_caps_length(self):
        clean = pricing_update._safe_error_text("x" * 1000)
        self.assertLessEqual(len(clean), 201)

    def test_header_error_message_has_no_raw_control_or_bidi_chars(self):
        header = ("<tr><th>Model</th>"
                  "<th>Base input tokens\x1b[31m</th>"
                  "<th>5m cache writes</th><th>1h cache writes</th>"
                  "<th>Cache hits and refreshes</th>"
                  "<th>Weird‮Column</th></tr>")  # no "output" column
        html = (f"<html><body><table>{header}"
               "<tr><td>Claude Sonnet 5</td><td>$2 / MTok</td>"
               "<td>$2.50 / MTok</td><td>$4 / MTok</td><td>$0.20 / MTok</td>"
               "</tr></table></body></html>")
        with self.assertRaises(ValueError) as ctx:
            pricing_update.parse_models(html)
        msg = str(ctx.exception)
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("‮", msg)

    def test_header_error_message_is_length_capped(self):
        # str(<list of cell strings>) has no length limit on its own — a
        # header row padded with one very long cell must still print a
        # short, bounded message.
        header = ("<tr><th>Model</th>"
                  f"<th>Base input tokens{'z' * 5000}</th>"
                  "<th>5m cache writes</th><th>1h cache writes</th>"
                  "<th>Cache hits and refreshes</th></tr>")  # no "output"
        html = (f"<html><body><table>{header}"
               "<tr><td>Claude Sonnet 5</td><td>$2 / MTok</td>"
               "<td>$2.50 / MTok</td><td>$4 / MTok</td><td>$0.20 / MTok</td>"
               "</tr></table></body></html>")
        with self.assertRaises(ValueError) as ctx:
            pricing_update.parse_models(html)
        self.assertLess(len(str(ctx.exception)), 300)

    def test_rate_cell_error_message_has_no_raw_control_or_bidi_chars(self):
        name_cell = "Claude Sonnet 5\x1b[31mevil‮reordered"
        html = _table_html([(name_cell, "not a dollar amount",
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
        html = _table_html([("Claude Sonnet 5\x1b[31m", "nope",
                            "$2.50 / MTok", "$4 / MTok", "$0.20 / MTok",
                            "$10 / MTok")])
        path = self.f.write_html(html)
        code, out, err = self.f.cli("--html", str(path))
        self.assertEqual(code, 2)
        self.assertNotIn("\x1b", err)


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


if __name__ == "__main__":
    unittest.main()
