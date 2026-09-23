import datetime
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import pricing_update

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pricing_golden

FIXTURE = (pathlib.Path(__file__).resolve().parent
           / "fixtures" / "pricing-page.html")
# The current published page: sentence-case headers, Fable/Mythos 5.1 at the
# 0.025x cache-read tier, Sonnet 5 unconditional, no `starting`/`through` rows.
CURRENT_FIXTURE = (pathlib.Path(__file__).resolve().parent
                   / "fixtures" / "pricing-page-current.html")
# The validator's repro page: Sonnet 5 unconditional AND `starting` Sep 1.
INCREASE_FIXTURE = (pathlib.Path(__file__).resolve().parent
                    / "fixtures" / "pricing-page-increase.html")
# An expired `through` intro footnote next to the post-intro rate.
EXPIRED_FIXTURE = (pathlib.Path(__file__).resolve().parent
                   / "fixtures" / "pricing-page-expired-intro.html")
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
    def test_family_prefix_uses_newest_versions_in_force_rate(self):
        """The family default takes the in-force rate of the family's NEWEST
        version, whether that version is listed unconditionally or only
        conditionally.

        Changed from the earlier rule (family default = the newest
        UNCONDITIONALLY-listed version, which on this fixture was Sonnet 4.6
        at $3): that rule let a page listing the newest version only
        conditionally (Sonnet 5 `through Aug 31` $2 / `starting Sep 1` $3)
        price unlisted successors at an OLDER version's rate, and gave a
        conditional-only family no family row at all — while the same
        in-force state with an extra unconditional listing of the newest
        version yielded a different family default."""
        _, cands = fixture_candidates()
        sonnet = next(c for c in cands if c["prefix"] == "claude-sonnet-")
        # Sonnet 5's in-force intro rate on 2026-08-06, not Sonnet 4.6's $3
        self.assertEqual(sonnet["rates"]["in_usd"], 2.0)
        self.assertEqual(sonnet["effective_from"], _epoch(TODAY))

    def test_conditional_only_family_still_gets_a_family_row(self):
        # V4: `mythos 5 through 2026-10-01` is the family's only listing ->
        # `claude-mythos-` minted at its in-force $10, so an unlisted
        # `claude-mythos-6` is estimated at $10 instead of unpriced.
        rates = {"in_usd": 10.0, "out_usd": 50.0, "cache_r_usd": 1.0,
                 "cache_w_usd": 12.5, "cache_w_1h_usd": 20.0}
        entries = [{"family": "mythos", "version": "5", "rates": rates,
                    "condition": ("through", datetime.date(2026, 10, 1))}]
        today = datetime.date(2026, 9, 15)
        cands = pricing_update.build_candidates(entries, today)
        fam = next(c for c in cands if c["prefix"] == "claude-mythos-")
        self.assertEqual((fam["rates"], fam["effective_from"]),
                         (rates, _epoch(today)))
        conn = capture.connect(pathlib.Path(tempfile.mkdtemp()) / "u.db")
        try:
            conn.execute("DELETE FROM pricing")   # no seed: the V4 shape
            pricing_update.apply(
                conn, pricing_update.plan(conn, cands), "test")
            r = price_lookup(conn, "claude-mythos-6",
                             _epoch(datetime.date(2026, 9, 20)))
            self.assertEqual(r["in_usd"], 10.0)
            self.assertTrue(capture.is_estimated("claude-mythos-6",
                                                 "claude-mythos-"))
        finally:
            conn.close()

    def test_family_default_same_with_or_without_extra_unconditional_row(self):
        # V4b / V4c: the newest version (Sonnet 5) is in its intro; listing it
        # ALSO unconditionally at the post-intro rate changes nothing about
        # the in-force state, so the family default must not change either.
        def r(i, o):
            return {"in_usd": i, "out_usd": o, "cache_r_usd": i / 10,
                    "cache_w_usd": i * 1.25, "cache_w_1h_usd": i * 2}
        intro = {"family": "sonnet", "version": "5", "rates": r(2, 10),
                 "condition": ("through", datetime.date(2026, 8, 31))}
        v46 = {"family": "sonnet", "version": "4.6", "rates": r(3, 15),
               "condition": None}
        v4b = [intro,
               {"family": "sonnet", "version": "5", "rates": r(3, 15),
                "condition": ("starting", datetime.date(2026, 9, 1))}, v46]
        v4c = [intro,
               {"family": "sonnet", "version": "5", "rates": r(3, 15),
                "condition": None}, v46]
        fams = []
        for entries in (v4b, v4c):
            cands = pricing_update.build_candidates(entries, TODAY)
            fams.append(next((c["rates"], c["effective_from"]) for c in cands
                             if c["prefix"] == "claude-sonnet-"))
        self.assertEqual(fams[0], fams[1])
        self.assertEqual(fams[0], (r(2, 10), _epoch(TODAY)))

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
        # Same-rate versions (Opus 4.8 etc.) now get their OWN row too (AOS-133:
        # every listed version is minted), at exactly the family rate — so it
        # changes no cost, only whether their events count as estimated.
        opus_fam = next(c for c in cands if c["prefix"] == "claude-opus-")
        o48 = next(c for c in cands if c["prefix"] == "claude-opus-4-8")
        self.assertEqual(o48["rates"], opus_fam["rates"])
        self.assertEqual(o48["effective_from"], opus_fam["effective_from"])

    def test_every_listed_version_gets_its_own_row(self):
        # Each unconditional version read off the page — the family's newest
        # one included — mints its specific prefix(es) at its own rates, dated
        # today like the family row. The bare family prefix stays the fallback
        # for versions the page does not list.
        for entries, cands, today in (
                (*fixture_candidates(), TODAY),
                (*current_candidates(), TODAY_CUR)):
            by = {(c["prefix"], c["effective_from"]): c for c in cands}
            for e in entries:
                if e["condition"] is not None:
                    continue
                for p in pricing_update.specific_prefixes(e["family"],
                                                          e["version"]):
                    c = by.get((p, _epoch(today)))
                    self.assertIsNotNone(c, p)
                    self.assertEqual(c["rates"], e["rates"], p)
                    self.assertFalse(capture.is_family_default(p), p)
            # the family row carries the in-force rate of the family's
            # newest (first-listed) version: the rate its own prefix resolves
            # to from today on (its latest-dated candidate)
            for fam in {e["family"] for e in entries}:
                newest = next(e for e in entries if e["family"] == fam)
                fam_row = by[(f"claude-{fam}-", _epoch(today))]
                for p in pricing_update.specific_prefixes(fam,
                                                          newest["version"]):
                    own = max((c for c in cands if c["prefix"] == p),
                              key=lambda c: c["effective_from"])
                    self.assertEqual(own["rates"], fam_row["rates"], p)

    def test_in_force_through_wins_regardless_of_row_order(self):
        # F4: a page listing the same version both unconditionally and with an
        # in-force `through` intro rate: the intro rate is what is charged now,
        # and it wins in EITHER page order (the unconditional rate is not
        # minted at all while the version has an in-force conditional).
        rates_a = {"in_usd": 2.0, "out_usd": 10.0, "cache_r_usd": 0.2,
                   "cache_w_usd": 2.5, "cache_w_1h_usd": 4.0}
        rates_b = dict(rates_a, in_usd=3.0)
        uncond = {"family": "sonnet", "version": "5", "rates": rates_b,
                  "condition": None}
        intro = {"family": "sonnet", "version": "5", "rates": rates_a,
                 "condition": ("through", TODAY + datetime.timedelta(days=9))}
        for entries in ([uncond, intro], [intro, uncond]):
            cands = pricing_update.build_candidates(entries, TODAY)
            s5 = [c for c in cands if c["prefix"] == "claude-sonnet-5"]
            self.assertEqual(len(s5), 1)
            self.assertEqual(s5[0]["rates"], rates_a)
            self.assertEqual(s5[0]["effective_from"], _epoch(TODAY))
            fam = next(c for c in cands if c["prefix"] == "claude-sonnet-")
            self.assertEqual(fam["rates"], rates_a)

    def test_first_unconditional_row_for_a_version_wins(self):
        # F4: two unconditional rows for one version (e.g. a long-context row
        # listed after the standard one) -> the FIRST listed row is the
        # version's own rate; the later one is dropped by the dedup.
        std = {"in_usd": 3.0, "out_usd": 15.0, "cache_r_usd": 0.3,
               "cache_w_usd": 3.75, "cache_w_1h_usd": 6.0}
        long_ctx = dict(std, in_usd=6.0, out_usd=22.5)
        entries = [
            {"family": "sonnet", "version": "4.5", "rates": std,
             "condition": None},
            {"family": "sonnet", "version": "4.5", "rates": long_ctx,
             "condition": None},
        ]
        cands = pricing_update.build_candidates(entries, TODAY)
        s45 = [c for c in cands if c["prefix"] == "claude-sonnet-4-5"]
        self.assertEqual([c["rates"] for c in s45], [std])
        entries.reverse()
        cands = pricing_update.build_candidates(entries, TODAY)
        s45 = [c for c in cands if c["prefix"] == "claude-sonnet-4-5"]
        self.assertEqual([c["rates"] for c in s45], [long_ctx])

    def test_family_alias_prefixes_never_collide(self):
        _, cands = fixture_candidates()
        keys = [(c["prefix"], c["effective_from"]) for c in cands]
        self.assertEqual(len(keys), len(set(keys)))

    def test_in_force_predicate(self):
        d = datetime.date(2026, 9, 1)
        f = pricing_update.in_force
        self.assertTrue(f(("through", d), d))              # last intro day
        self.assertFalse(f(("through", d), d + datetime.timedelta(days=1)))
        self.assertTrue(f(("starting", d), d))             # first new day
        self.assertFalse(f(("starting", d), d - datetime.timedelta(days=1)))
        self.assertFalse(f(None, d))


class TestInForceMinting(unittest.TestCase):
    """F1 + AOS-140: only the rate in force today is minted, per version."""

    def test_arrived_increase_governs_over_the_unconditional_rate(self):
        # Validator repro (pricing-page-increase.html): Sonnet 5 listed
        # unconditionally at $3 AND `starting September 1, 2026` at $4. Run
        # after Sep 1 -> the $4 increase governs; the $3 pre-increase rate is
        # NOT minted dated today (it would override the increase).
        today = datetime.date(2026, 9, 15)
        entries = pricing_update.parse_models(INCREASE_FIXTURE.read_text())
        cands = pricing_update.build_candidates(entries, today)
        s5 = [c for c in cands if c["prefix"] == "claude-sonnet-5"]
        self.assertEqual([(c["rates"]["in_usd"], c["effective_from"])
                          for c in s5],
                         [(4.0, _epoch(datetime.date(2026, 9, 1)))])
        fam = next(c for c in cands if c["prefix"] == "claude-sonnet-")
        self.assertEqual(fam["rates"]["in_usd"], 4.0)
        conn = _db_with(cands)
        try:
            for ts in (_epoch(datetime.date(2026, 9, 2)),
                       _epoch(today) + 3600, _epoch(today) + 30 * 86400):
                r = price_lookup(conn, "claude-sonnet-5", ts)
                self.assertEqual((r["in_usd"], r["out_usd"]), (4.0, 20.0))
            r = price_lookup(conn, "claude-sonnet-4-5", _epoch(today) + 3600)
            self.assertEqual(r["in_usd"], 3.0)   # other versions untouched
        finally:
            conn.close()

    def test_before_the_increase_the_unconditional_rate_is_minted(self):
        entries = pricing_update.parse_models(INCREASE_FIXTURE.read_text())
        cands = pricing_update.build_candidates(entries,
                                                datetime.date(2026, 8, 15))
        s5 = [c for c in cands if c["prefix"] == "claude-sonnet-5"]
        self.assertEqual([c["rates"]["in_usd"] for c in s5], [3.0])

    def test_expired_intro_is_never_minted_post_intro_rate_governs(self):
        # AOS-140 (pricing-page-expired-intro.html): the page still carries
        # "through August 31, 2026" next to the unconditional post-intro rate.
        # Run after the date -> no $2 row anywhere, the $3 rate governs.
        today = datetime.date(2026, 9, 15)
        entries = pricing_update.parse_models(EXPIRED_FIXTURE.read_text())
        cands = pricing_update.build_candidates(entries, today)
        self.assertFalse(any(c["rates"]["in_usd"] == 2.0 for c in cands))
        s5 = [c for c in cands if c["prefix"] == "claude-sonnet-5"]
        self.assertEqual([(c["rates"]["in_usd"], c["effective_from"])
                          for c in s5], [(3.0, _epoch(today))])
        # ...while the intro is still in force, the intro rate is minted and
        # the post-intro unconditional rate is not.
        cands = pricing_update.build_candidates(
            entries, datetime.date(2026, 8, 31))
        s5 = [c for c in cands if c["prefix"] == "claude-sonnet-5"]
        self.assertEqual([c["rates"]["in_usd"] for c in s5], [2.0])

    def test_rerun_after_intro_expiry_stops_the_intro_rate(self):
        # Same DB, two runs of the original page: on 2026-08-06 the intro rate
        # is minted; on 2026-09-15 the stale "through August 31" footnote must
        # not re-assert it — the Sep-1 increase prices events from then on.
        entries = pricing_update.parse_models(FIXTURE.read_text())
        conn = _db_with(pricing_update.build_candidates(entries, TODAY))
        try:
            later = datetime.date(2026, 9, 15)
            cands = pricing_update.build_candidates(entries, later)
            self.assertFalse(any(
                c["prefix"] == "claude-sonnet-5" and c["rates"]["in_usd"] == 2.0
                for c in cands))
            pricing_update.apply(conn, pricing_update.plan(conn, cands), "t")
            self.assertEqual(price_lookup(
                conn, "claude-sonnet-5", _epoch(TODAY) + 3600)["in_usd"], 2.0)
            for ts in (_epoch(datetime.date(2026, 9, 1)) + 1,
                       _epoch(later) + 3600):
                self.assertEqual(price_lookup(
                    conn, "claude-sonnet-5", ts)["in_usd"], 3.0)
        finally:
            conn.close()


class TestStaleIntroWarning(unittest.TestCase):
    """AOS-140 known limitation: a version whose ONLY listing is an expired
    `through` intro has no known in-force rate — nothing is minted and the
    run report prints a STALE-PRICE WARNING instead."""

    RATES2 = {"in_usd": 2.0, "out_usd": 10.0, "cache_r_usd": 0.2,
              "cache_w_usd": 2.5, "cache_w_1h_usd": 4.0}
    RATES3 = {"in_usd": 3.0, "out_usd": 15.0, "cache_r_usd": 0.3,
              "cache_w_usd": 3.75, "cache_w_1h_usd": 6.0}
    WARNING = ("STALE-PRICE WARNING: Claude Sonnet 5 (`claude-sonnet-5`) —"
               " its introductory rate ended 2026-08-31 and the page lists no"
               " rate in force after it; nothing was minted, so events for it"
               " keep the last recorded rate until the page publishes a"
               " post-intro rate.")

    def entries(self):
        return [{"family": "sonnet", "version": "5", "rates": self.RATES2,
                 "condition": ("through", datetime.date(2026, 8, 31))},
                {"family": "sonnet", "version": "4.6", "rates": self.RATES3,
                 "condition": None}]

    def test_second_run_after_expiry_warns_and_mints_nothing(self):
        # V5a: run on 08-15 (intro in force) then 09-15 (intro expired, no
        # post-intro rate on the page).
        conn = capture.connect(pathlib.Path(tempfile.mkdtemp()) / "u.db")
        try:
            first = pricing_update.run_update(
                conn, self.entries(), datetime.date(2026, 8, 15), "t")
            self.assertNotIn("STALE-PRICE WARNING", first)
            rows = ("SELECT * FROM pricing"
                    " ORDER BY model_prefix, effective_from")
            before = conn.execute(rows).fetchall()
            second = pricing_update.run_update(
                conn, self.entries(), datetime.date(2026, 9, 15), "t")
            self.assertIn(self.WARNING, second.splitlines())
            self.assertEqual(second.count("STALE-PRICE WARNING"), 1)
            # no new row minted; nothing else changes
            self.assertEqual(conn.execute(rows).fetchall(), before)
            self.assertIn("0 row(s) inserted", second)
            table = [ln for ln in second.splitlines()
                     if ln.startswith("| `")]
            self.assertEqual(table, [
                "| `claude-sonnet-4-6` | 3 / 15 / 0.3 / 3.75 / 6 |"
                " 2026-09-15 | unchanged |"])
            # events keep the last recorded (intro) rate
            ts = _epoch(datetime.date(2026, 9, 20))
            self.assertEqual(price_lookup(conn, "claude-sonnet-5", ts),
                             self.RATES2)
        finally:
            conn.close()

    def test_no_warning_when_another_rate_is_in_force(self):
        today = datetime.date(2026, 9, 15)
        for extra in ({"family": "sonnet", "version": "5",
                       "rates": self.RATES3, "condition": None},
                      {"family": "sonnet", "version": "5",
                       "rates": self.RATES3,
                       "condition": ("starting", datetime.date(2026, 9, 1))}):
            self.assertEqual(pricing_update.stale_intros(
                self.entries() + [extra], today), [])
        self.assertEqual(
            pricing_update.stale_intros(self.entries(), today),
            [("sonnet", "5", datetime.date(2026, 8, 31))])
        # while the intro is still in force there is nothing stale
        self.assertEqual(pricing_update.stale_intros(
            self.entries(), datetime.date(2026, 8, 31)), [])


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


# --------------------------------------------------------------------------
# Golden cost test against the PRE-AOS-133 rules (tests/pricing_golden.py).
# Every (scenario, model, timestamp) must resolve to exactly the rates the
# pre-change code produced, except the reviewed differences below. A
# prefix-only change at identical rates is accepted ONLY when the old row was
# a family default and the new row is the model's OWN row (not estimated) —
# the AOS-133 intent (every listed version gets its own row), no cost change.
# --------------------------------------------------------------------------
ALLOWED_REASONS = {
    "a": "unlisted successor now prices at its nearest listed ancestor's own"
         " row, flagged estimated (F2; forward-only: minted rows are dated"
         " today, so past events keep their rate)",
    "b": "an in-force `through` intro rate wins regardless of page row order"
         " (F4; the pre-change code let an earlier unconditional row win)",
    "c": "the FIRST unconditional row for a version wins over a later"
         " same-version row such as a long-context row (F4; the pre-change"
         " code minted only rows differing from the family rate, so the later"
         " row won)",
    "d": "F1/AOS-140: only the rate in force today is minted — an expired"
         " `through` intro rate is never re-asserted, an arrived `starting`"
         " increase is not overridden by the pre-increase rate, and the family"
         " default takes the newest version's in-force rate",
    "e": "a listed legacy-alias version (Haiku 3.5 as its family's newest) was"
         " unpriced because its alias prefix was never minted; AOS-133 mints"
         " every listed version's own row",
}
_INC, _EXP, _PG = ("increase@2026-09-15 after starting",
                   "expired-intro@2026-09-15 intro expired",
                   "page@2026-09-15 intro expired, increase arrived")
_EXP8 = "expired-intro@2026-08-15 intro in force"
_P806, _P831 = ("page@2026-08-06 intro in force, increase future",
                "page@2026-08-31 intro last day")
# Published rate sets (in, out, cache-read, 5m-write, 1h-write USD per MTok),
# each as read off the fixture page / scenario entry that mints it.
FABLE_5 = MYTHOS_5 = (10.0, 50.0, 1.0, 12.5, 20.0)   # pricing-page*.html
OPUS_5 = (5.0, 25.0, 0.5, 6.25, 10.0)                # pricing-page*.html
SONNET_3 = (3.0, 15.0, 0.3, 3.75, 6.0)   # Sonnet 4/4.5 and post-intro 5 $3
SONNET_2 = (2.0, 10.0, 0.2, 2.5, 4.0)    # Sonnet 5 intro / current page $2
SONNET_4 = (4.0, 20.0, 0.4, 5.0, 8.0)    # increase page: Sonnet 5 from Sep 1
# (scenario, model) -> (reason, *expected resolved row): the FULL tuple
# model_prefix, in_usd, out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,
# effective_from (ISO date, UTC midnight) every differing event of that model
# in that scenario must resolve to.
ALLOWED_DIFFS = {
    ("D uncond (other rate) before in-force through", "claude-opus-4-1"):
        ("b", "claude-opus-4-1", 10.0, 50.0, 1.0, 12.5, 20.0, "2026-09-15"),
    ("E two uncond rows for one version", "claude-sonnet-4-5"):
        ("c", "claude-sonnet-4-5", *SONNET_3, "2026-09-15"),
    ("K Haiku 3.5 legacy alias newest", "claude-3-5-haiku-20241022"):
        ("e", "claude-3-5-haiku", 0.8, 4.0, 0.08, 1.0, 1.6, "2026-09-15"),
    # F1: the arrived $4 increase governs the family default too
    (_INC, "claude-sonnet-4-20250514"):
        ("d", "claude-sonnet-", *SONNET_4, "2026-09-15"),
    (_INC, "claude-sonnet-4-6"): ("d", "claude-sonnet-", *SONNET_4,
                                  "2026-09-15"),
    (_INC, "claude-sonnet-4-7"): ("d", "claude-sonnet-", *SONNET_4,
                                  "2026-09-15"),
    # AOS-140: the expired $2 intro is not re-asserted; post-intro $3 governs
    # (the unconditional row dated today on the expired-intro page; the
    # arrived Sep-1 `starting` row on the original page)
    (_EXP, "claude-sonnet-5"): ("d", "claude-sonnet-5", *SONNET_3,
                                "2026-09-15"),
    (_EXP, "claude-sonnet-5-1"): ("d", "claude-sonnet-5", *SONNET_3,
                                  "2026-09-15"),
    (_EXP, "claude-sonnet-5-20260101"): ("d", "claude-sonnet-5", *SONNET_3,
                                         "2026-09-15"),
    (_PG, "claude-sonnet-5"): ("d", "claude-sonnet-5", *SONNET_3,
                               "2026-09-01"),
    (_PG, "claude-sonnet-5-1"): ("d", "claude-sonnet-5", *SONNET_3,
                                 "2026-09-01"),
    (_PG, "claude-sonnet-5-20260101"): ("d", "claude-sonnet-5", *SONNET_3,
                                        "2026-09-01"),
    # F1: while the intro is in force, the newest version's in-force rate is
    # the family default
    (_EXP8, "claude-sonnet-4-20250514"): ("d", "claude-sonnet-", *SONNET_2,
                                          "2026-08-15"),
    (_EXP8, "claude-sonnet-4-6"): ("d", "claude-sonnet-", *SONNET_2,
                                   "2026-08-15"),
    (_EXP8, "claude-sonnet-4-7"): ("d", "claude-sonnet-", *SONNET_2,
                                   "2026-08-15"),
}
# F2 (a): unlisted successors -> nearest listed ancestor's own row (dated the
# run date), identical rates here
for _sc, _model, _prefix, _rates, _eff in [
        (sc, m, p, r, day)
        for sc, day in ((_P806, "2026-08-06"), (_P831, "2026-08-31"),
                        (_PG, "2026-09-15"))
        for m, p, r in (
            ("claude-fable-5-1-20260901", "claude-fable-5", FABLE_5),
            ("claude-fable-5-1-x", "claude-fable-5", FABLE_5),
            ("claude-mythos-5-1-x", "claude-mythos-5", MYTHOS_5),
            ("claude-opus-5-5", "claude-opus-5", OPUS_5),
            ("claude-opus-5-5-20261001", "claude-opus-5", OPUS_5),
            ("claude-sonnet-4-7", "claude-sonnet-4", SONNET_3))] + [
        (sc, m, "claude-opus-5", OPUS_5, day)
        for sc, day in (("current@2026-09-21", "2026-09-21"),
                        (_EXP8, "2026-08-15"), (_EXP, "2026-09-15"))
        for m in ("claude-opus-5-5", "claude-opus-5-5-20261001")] + [
        ("current@2026-09-21", "claude-sonnet-5-1", "claude-sonnet-5",
         SONNET_2, "2026-09-21"),
        ("increase@2026-08-15 before starting", "claude-sonnet-5-1",
         "claude-sonnet-5", SONNET_3, "2026-08-15")]:
    ALLOWED_DIFFS[(_sc, _model)] = ("a", _prefix, *_rates, _eff)


class TestGoldenPreAos133(unittest.TestCase):
    """Per-event resolved rates of the current build_candidates vs the golden
    recorded from the pre-AOS-133 code (tests/pricing_golden.py). Fails on any
    cost difference outside the reviewed allow-list above, on an allow-listed
    event resolving to anything but its expected row, and on a stale
    allow-list entry."""

    maxDiff = None   # print every differing event on failure

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(pricing_golden.GOLDEN.read_text())
        cls.current = pricing_golden.run_scenarios(
            pricing_update.build_candidates, pricing_update.parse_models)

    def test_every_scenario_is_in_the_golden(self):
        self.assertEqual(set(self.golden["scenarios"]),
                         {s[0] for s in pricing_golden.SCENARIOS})
        for name, _src, _today, names in pricing_golden.SCENARIOS:
            self.assertEqual(set(self.golden["scenarios"][name]), set(names))

    def test_costs_match_the_pre_aos133_golden(self):
        unexpected, wrong, used = [], [], set()
        for sc, by_model in sorted(self.golden["scenarios"].items()):
            for model, by_ts in sorted(by_model.items()):
                for label, old in sorted(by_ts.items()):
                    new = self.current[sc][model][label]
                    if new == old:
                        continue
                    if (old is not None and new is not None
                            and old[1:] == new[1:]
                            and capture.is_family_default(old[0])
                            and not capture.is_estimated(model, new[0])):
                        continue  # own row minted at the same rate
                    line = f"{sc} | {model} | ts {label}: {old} -> {new}"
                    allowed = ALLOWED_DIFFS.get((sc, model))
                    if allowed is None:
                        unexpected.append(line)
                        continue
                    used.add((sc, model))
                    code, *expected, eff = allowed
                    expected.append(_epoch(datetime.date.fromisoformat(eff)))
                    # the FULL resolved row: prefix, all five rates and
                    # effective_from — a cache-rate or date drift on an
                    # allow-listed event is a failure, not an accepted diff
                    if (new is None or new != expected
                            or (code == "a"
                                and not capture.is_estimated(model, new[0]))):
                        wrong.append(f"[{code}] {line} (expected {expected})")
        # subTests: every failing category is reported, so a mutation that
        # breaks an allow-listed event is visible even when it also breaks
        # other events
        with self.subTest("unexpected"):
            self.assertEqual(unexpected, [],
                             "cost differs from the pre-AOS-133 golden")
        with self.subTest("allow-listed"):
            self.assertEqual(
                wrong, [], "allow-listed event resolved to an unexpected row")
        with self.subTest("stale"):
            self.assertEqual(set(ALLOWED_DIFFS) - used, set(),
                             "stale allow-list entries (no longer differ)")

    def test_every_allowed_diff_names_a_reason(self):
        for key, (code, *_rest) in ALLOWED_DIFFS.items():
            self.assertIn(code, ALLOWED_REASONS, key)

    def test_golden_reproduces_from_the_baseline_commit(self):
        # Provenance: the committed golden equals a fresh run of the
        # pre-change build_candidates loaded from git. Skips where the
        # baseline commit is not in the local history (a shallow clone).
        try:
            old = pricing_golden.load_baseline_module()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            self.skipTest(f"baseline commit unavailable: {exc}")
        self.assertEqual(
            pricing_golden.run_scenarios(old.build_candidates,
                                         old.parse_models),
            self.golden["scenarios"])


def _db_with(cands):
    """A fresh DB (seed rows kept) with `cands` planned and applied."""
    conn = capture.connect(pathlib.Path(tempfile.mkdtemp()) / "u.db")
    pricing_update.apply(conn, pricing_update.plan(conn, cands), "test")
    return conn


def _epoch(d):
    return int(datetime.datetime.combine(
        d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())


if __name__ == "__main__":
    unittest.main()
