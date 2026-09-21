import datetime
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import pricing_update

FIXTURE = (pathlib.Path(__file__).resolve().parent
           / "fixtures" / "pricing-page.html")
# The current published page: sentence-case headers, Fable/Mythos 5.1 at the
# 0.025x cache-read tier, Sonnet 5 unconditional, no `starting`/`through` rows.
CURRENT_FIXTURE = (pathlib.Path(__file__).resolve().parent
                   / "fixtures" / "pricing-page-current.html")
TODAY = datetime.date(2026, 8, 6)
TODAY_CUR = datetime.date(2026, 9, 21)


def fixture_candidates():
    entries = pricing_update.parse_models(FIXTURE.read_text())
    return entries, pricing_update.build_candidates(entries, TODAY)


def current_candidates():
    entries = pricing_update.parse_models(CURRENT_FIXTURE.read_text())
    return entries, pricing_update.build_candidates(entries, TODAY_CUR)


def price_lookup(conn, name, ts):
    """The contract's rate rule: the row with the longest `model_prefix` that
    is a prefix of `name`, restricted to `effective_from <= ts`, and among
    those the greatest `effective_from`. Returns the rate dict or None."""
    row = conn.execute(
        "SELECT in_usd, out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd"
        " FROM pricing WHERE provider='anthropic' AND ? LIKE model_prefix||'%'"
        " AND effective_from <= ?"
        " ORDER BY length(model_prefix) DESC, effective_from DESC LIMIT 1",
        (name, ts)).fetchone()
    return dict(zip(pricing_update.RATE_KEYS, row)) if row else None


class TestParser(unittest.TestCase):
    def test_parses_every_published_family(self):
        entries, _ = fixture_candidates()
        self.assertEqual({e["family"] for e in entries},
                         {"fable", "mythos", "opus", "sonnet", "haiku"})

    def test_rates_read_from_the_right_columns(self):
        entries, _ = fixture_candidates()
        fable = next(e for e in entries if e["family"] == "fable")
        self.assertEqual(fable["rates"], {
            "in_usd": 10.0, "out_usd": 50.0, "cache_r_usd": 1.0,
            "cache_w_usd": 12.5, "cache_w_1h_usd": 20.0})

    def test_conditional_rows_carry_their_dates(self):
        entries, _ = fixture_candidates()
        conds = [e["condition"] for e in entries
                 if e["family"] == "sonnet" and e["condition"]]
        self.assertIn(("starting", datetime.date(2026, 9, 1)), conds)
        self.assertTrue(any(k == "through" for k, _ in conds))

    def test_unparseable_page_raises(self):
        with self.assertRaises(ValueError):
            pricing_update.parse_models("<html><body>no tables</body></html>")


class TestCandidates(unittest.TestCase):
    def test_family_prefix_uses_newest_unconditional_row(self):
        _, cands = fixture_candidates()
        sonnet = next(c for c in cands if c["prefix"] == "claude-sonnet-")
        # NOT the Sonnet 5 intro rate — the newest unconditional row (4.6)
        self.assertEqual(sonnet["rates"]["in_usd"], 3.0)

    def test_through_row_is_dated_today_future_starting_is_not_minted(self):
        # A `through <d>` intro rate is in force now -> dated today. A
        # `starting <d>` scheduled increase whose date is still in the future
        # must NOT be minted in advance (a forecast is not a recorded charge).
        _, cands = fixture_candidates()
        s5 = sorted((c for c in cands if c["prefix"] == "claude-sonnet-5"),
                    key=lambda c: c["effective_from"])
        self.assertEqual(len(s5), 1)  # only the in-force intro row
        self.assertEqual(s5[0]["rates"]["in_usd"], 2.0)
        today_epoch = int(datetime.datetime.combine(
            TODAY, datetime.time(),
            tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(s5[0]["effective_from"], today_epoch)
        # The Sep-1 $3 forecast row is absent while it is still in the future.
        self.assertFalse(any(c["rates"]["in_usd"] == 3.0 for c in
                             cands if c["prefix"] == "claude-sonnet-5"))

    def test_scheduled_increase_recorded_only_once_its_date_arrives(self):
        # Same page parsed after the `starting` date records the increase at
        # that date -- recorded when it takes effect, never in advance.
        sep1_epoch = int(datetime.datetime.combine(
            datetime.date(2026, 9, 1), datetime.time(),
            tzinfo=datetime.timezone.utc).timestamp())
        entries = pricing_update.parse_models(FIXTURE.read_text())
        on_or_after = pricing_update.build_candidates(
            entries, datetime.date(2026, 9, 15))
        sep1 = next(c for c in on_or_after
                    if c["prefix"] == "claude-sonnet-5"
                    and c["effective_from"] == sep1_epoch)
        self.assertEqual(sep1["rates"]["in_usd"], 3.0)

    def test_retired_models_on_old_pricing_get_specific_prefixes(self):
        _, cands = fixture_candidates()
        prefixes = {c["prefix"] for c in cands}
        self.assertLessEqual({"claude-opus-4-1", "claude-opus-4-0",
                              "claude-opus-4-2025", "claude-3-5-haiku"},
                             prefixes)
        # same-rate versions (Opus 4.8 etc.) must NOT get redundant rows
        self.assertNotIn("claude-opus-4-8", prefixes)

    def test_family_alias_prefixes_never_collide(self):
        _, cands = fixture_candidates()
        keys = [(c["prefix"], c["effective_from"]) for c in cands]
        self.assertEqual(len(keys), len(set(keys)))


class TestPlanAndApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "usage.db"
        self.conn = capture.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_update(self):
        _, cands = fixture_candidates()
        planned = pricing_update.plan(self.conn, cands)
        inserted = pricing_update.apply(self.conn, planned, "test")
        return planned, inserted

    def test_first_run_replaces_seed_and_adds_specifics(self):
        planned, inserted = self.run_update()
        by = {(c["prefix"], c["effective_from"]): c["status"] for c in planned}
        self.assertEqual(inserted, len(planned))  # everything lands
        statuses = {c["prefix"]: c["status"] for c in planned}
        self.assertEqual(statuses["claude-fable-"], "seed replaced")
        self.assertEqual(statuses["claude-opus-4-1"], "new")
        self.assertTrue(all(s != "unchanged" for s in by.values()))

    def test_second_run_is_all_unchanged_no_inserts(self):
        self.run_update()
        planned, inserted = self.run_update()
        self.assertEqual(inserted, 0)
        self.assertTrue(all(c["status"] == "unchanged" for c in planned))

    def test_rate_change_inserts_new_dated_row_keeping_history(self):
        self.run_update()
        before = self.conn.execute(
            "SELECT COUNT(*) FROM pricing").fetchone()[0]
        entries = pricing_update.parse_models(FIXTURE.read_text())
        cands = pricing_update.build_candidates(
            entries, TODAY + datetime.timedelta(days=30))
        fable = next(c for c in cands if c["prefix"] == "claude-fable-")
        fable["rates"] = dict(fable["rates"], in_usd=11.0)
        planned = pricing_update.plan(self.conn, cands)
        pricing_update.apply(self.conn, planned, "test")
        self.assertEqual(next(c["status"] for c in planned
                              if c["prefix"] == "claude-fable-"), "updated")
        rows = self.conn.execute(
            "SELECT in_usd FROM pricing WHERE model_prefix='claude-fable-'"
            " AND effective_from > 0 ORDER BY effective_from").fetchall()
        self.assertEqual([r[0] for r in rows], [10.0, 11.0])  # history kept

    def test_unpriced_models_reported(self):
        self.conn.execute("INSERT INTO models(name) VALUES ('<synthetic>')")
        self.conn.commit()
        self.assertEqual(pricing_update.unpriced_models(self.conn),
                         ["<synthetic>"])


class TestParserCaseInsensitivity(unittest.TestCase):
    def test_sentence_case_headers_parse(self):
        # The current page renders sentence case ("Base input tokens", "Cache
        # hits and refreshes"); the case-sensitive parser missed it (exit 2).
        entries = pricing_update.parse_models(CURRENT_FIXTURE.read_text())
        self.assertTrue(entries)
        self.assertEqual({e["family"] for e in entries},
                         {"fable", "mythos", "opus", "sonnet", "haiku"})

    def test_header_guard_still_raises_on_a_genuinely_absent_column(self):
        # Case-insensitive matching must not weaken the guard: drop the output
        # column entirely and the parser must refuse, not mismap an index.
        html = ("<html><body><table><tr>"
                "<th>Model</th><th>Base input tokens</th>"
                "<th>5m cache writes</th><th>1h cache writes</th>"
                "<th>Cache hits and refreshes</th>"
                "</tr><tr><td>Claude Sonnet 5</td><td>$2 / MTok</td>"
                "<td>$2.50 / MTok</td><td>$4 / MTok</td><td>$0.20 / MTok</td>"
                "</tr></table></body></html>")
        with self.assertRaises(ValueError):
            pricing_update.parse_models(html)


class TestCurrentPage(unittest.TestCase):
    """The current published page: new facts the old fixture cannot exercise."""

    def test_fable_and_mythos_51_use_the_0025x_cache_read_tier(self):
        # First time two versions of one family differ on one rate: 5.1 reads
        # at 0.025x base ($0.25) while Fable/Mythos 5 read at 0.1x ($1).
        entries, _ = current_candidates()
        f51 = next(e for e in entries
                   if e["family"] == "fable" and e["version"] == "5.1")
        f5 = next(e for e in entries
                  if e["family"] == "fable" and e["version"] == "5")
        m51 = next(e for e in entries
                   if e["family"] == "mythos" and e["version"] == "5.1")
        self.assertEqual(f51["rates"]["cache_r_usd"], 0.25)
        self.assertEqual(m51["rates"]["cache_r_usd"], 0.25)
        self.assertEqual(f5["rates"]["cache_r_usd"], 1.0)

    def test_no_conditional_rows_and_sonnet_4_6_prices_3_15(self):
        # Sonnet 5 is unconditional and first in family order, so
        # `claude-sonnet-` becomes $2/$10 and the older 4.6/4.5/4 rows get
        # specific $3/$15 prefixes (the inverse of the old page's structure).
        entries, cands = current_candidates()
        self.assertTrue(all(e["condition"] is None for e in entries))
        sonnet_fam = next(c for c in cands if c["prefix"] == "claude-sonnet-")
        self.assertEqual((sonnet_fam["rates"]["in_usd"],
                          sonnet_fam["rates"]["out_usd"]), (2.0, 10.0))
        for v in ("claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4"):
            c = next(c for c in cands if c["prefix"] == v)
            self.assertEqual((c["rates"]["in_usd"], c["rates"]["out_usd"]),
                             (3.0, 15.0))
        # A sonnet-4-6 model still prices $3/$15 after the run.
        conn = capture.connect(pathlib.Path(tempfile.mkdtemp()) / "u.db")
        try:
            pricing_update.apply(
                conn, pricing_update.plan(conn, cands), "test")
            ts = _epoch(TODAY_CUR) + 3600
            r = price_lookup(conn, "claude-sonnet-4-6-20260101", ts)
            self.assertEqual((r["in_usd"], r["out_usd"]), (3.0, 15.0))
            # ...and the sonnet-5 model prices at the $2/$10 family rate.
            r5 = price_lookup(conn, "claude-sonnet-5", ts)
            self.assertEqual((r5["in_usd"], r5["out_usd"]), (2.0, 10.0))
        finally:
            conn.close()

    def test_prefix_shadowing_fable_51_never_takes_fable_5_rate(self):
        # `claude-fable-5` is a string-prefix of `claude-fable-5-1`; without a
        # dedicated 5.1 row, longest-prefix matching would hand a 5.1 model
        # Fable-5's $1 cache-read rate.
        _, cands = current_candidates()
        f51 = next(c for c in cands if c["prefix"] == "claude-fable-5-1")
        self.assertEqual(f51["rates"]["cache_r_usd"], 0.25)
        conn = capture.connect(pathlib.Path(tempfile.mkdtemp()) / "u.db")
        try:
            pricing_update.apply(
                conn, pricing_update.plan(conn, cands), "test")
            ts = _epoch(TODAY_CUR) + 3600
            r = price_lookup(conn, "claude-fable-5-1-20260901", ts)
            self.assertEqual(r["cache_r_usd"], 0.25)  # NOT 1.0 (Fable-5)
            # The genuine Fable-5 model still reads at $1.
            r5 = price_lookup(conn, "claude-fable-5-20260601", ts)
            self.assertEqual(r5["cache_r_usd"], 1.0)
        finally:
            conn.close()

    def test_acceptance_three_corrected_rows_present(self):
        # The three corrected pricing facts the repair establishes, checked by
        # the contract's own rate lookup on a fresh DB run of the current page:
        # Fable 5.1 and Mythos 5.1 at cache-read $0.25, Sonnet 5 at $2/$10.
        _, cands = current_candidates()
        conn = capture.connect(pathlib.Path(tempfile.mkdtemp()) / "u.db")
        try:
            pricing_update.apply(
                conn, pricing_update.plan(conn, cands), "test")
            ts = _epoch(TODAY_CUR) + 3600
            self.assertEqual(
                price_lookup(conn, "claude-fable-5-1-x", ts),
                {"in_usd": 10.0, "out_usd": 50.0, "cache_r_usd": 0.25,
                 "cache_w_usd": 12.5, "cache_w_1h_usd": 20.0})
            self.assertEqual(
                price_lookup(conn, "claude-mythos-5-1-x", ts),
                {"in_usd": 10.0, "out_usd": 50.0, "cache_r_usd": 0.25,
                 "cache_w_usd": 12.5, "cache_w_1h_usd": 20.0})
            self.assertEqual(
                price_lookup(conn, "claude-sonnet-5", ts),
                {"in_usd": 2.0, "out_usd": 10.0, "cache_r_usd": 0.2,
                 "cache_w_usd": 2.5, "cache_w_1h_usd": 4.0})
        finally:
            conn.close()


def _epoch(d):
    return int(datetime.datetime.combine(
        d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())


if __name__ == "__main__":
    unittest.main()
