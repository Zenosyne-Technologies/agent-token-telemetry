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
TODAY = datetime.date(2026, 8, 6)


def fixture_candidates():
    entries = pricing_update.parse_models(FIXTURE.read_text())
    return entries, pricing_update.build_candidates(entries, TODAY)


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

    def test_conditional_rows_get_specific_prefix_and_dates(self):
        _, cands = fixture_candidates()
        s5 = sorted((c for c in cands if c["prefix"] == "claude-sonnet-5"),
                    key=lambda c: c["effective_from"])
        self.assertEqual(len(s5), 2)
        self.assertEqual(s5[0]["rates"]["in_usd"], 2.0)  # intro, today
        sep1 = datetime.datetime.fromtimestamp(
            s5[1]["effective_from"], tz=datetime.timezone.utc).date()
        self.assertEqual(sep1, datetime.date(2026, 9, 1))
        self.assertEqual(s5[1]["rates"]["in_usd"], 3.0)

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


if __name__ == "__main__":
    unittest.main()
