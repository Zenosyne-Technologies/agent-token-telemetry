"""Consent-gated pricing backfill (`pricing_update.py --backfill-plan` /
`--backfill-apply`): docs/TELEMETRY-CONTRACT.md §Pricing table, "History is
never mutated", case 3. Fixture DBs only."""
import contextlib
import datetime
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import pricing_update

TODAY = datetime.date(2026, 9, 23)
D = int(datetime.datetime(2026, 9, 22, tzinfo=datetime.timezone.utc).timestamp())
DAY = 86400
SRC = "https://example.test/pricing"
FAM_OPUS = (5.0, 25.0, 0.5, 6.25, 10.0)
OPUS_55 = (4.0, 20.0, 0.2, 5.0, 8.0)
# 1M input, 100k output, 1M cache read, no cache write: cost = in + out/10 + cr
TOK = (1_000_000, 100_000, 1_000_000, 0, 0)


def ev_cost(rates):
    return rates[0] + rates[1] / 10 + rates[2]


class Fixture:
    """A fresh telemetry DB (seed rows kept) with pricing rows and events."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "usage.db"
        self.conn = capture.connect(self.path)
        self.conn.execute("INSERT INTO projects(path) VALUES ('/p')")
        self.conn.execute("INSERT INTO sessions(uuid, project_id) VALUES ('s', 1)")
        self.conn.commit()

    def price(self, prefix, rates, eff, source=SRC):
        self.conn.execute(
            "INSERT INTO pricing(provider, model_prefix, in_usd, out_usd,"
            " cache_r_usd, cache_w_usd, cache_w_1h_usd, effective_from, source)"
            " VALUES ('anthropic',?,?,?,?,?,?,?,?)", (prefix, *rates, eff, source))
        self.conn.commit()

    def event(self, model, ts, tok=TOK):
        mid = capture.get_or_create(self.conn, "models", "name", model)
        rid = self.conn.execute(
            "INSERT INTO events(ts, session_id, kind, model_id, in_tok, out_tok,"
            " cache_r, cache_w, cache_w_1h) VALUES (?,1,0,?,?,?,?,?,?)",
            (ts, mid, *tok)).lastrowid
        self.conn.commit()
        return rid

    def resolved(self):
        """{event rowid: (model_prefix, effective_from)} — the contract's
        resolution, written independently of the code under test."""
        return {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT e.rowid,"
            " (SELECT model_prefix FROM pricing p WHERE m.name LIKE"
            "  p.model_prefix || '%' AND p.effective_from <= e.ts ORDER BY"
            "  length(p.model_prefix) DESC, p.effective_from DESC LIMIT 1),"
            " (SELECT effective_from FROM pricing p WHERE m.name LIKE"
            "  p.model_prefix || '%' AND p.effective_from <= e.ts ORDER BY"
            "  length(p.model_prefix) DESC, p.effective_from DESC LIMIT 1)"
            " FROM events e JOIN models m ON m.id = e.model_id")}

    def pricing_rows(self):
        return self.conn.execute(
            "SELECT * FROM pricing ORDER BY model_prefix, effective_from"
        ).fetchall()

    def digest(self):
        """Hash of the DB file(s) on disk."""
        h = hashlib.sha256()
        for suffix in ("", "-wal"):
            p = pathlib.Path(str(self.path) + suffix)
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()

    def plan(self):
        return pricing_update.backfill_plan(self.conn, TODAY)

    def apply(self, *prefixes):
        return pricing_update.backfill_apply(self.conn, list(prefixes), TODAY)

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pricing_update.main(["--db", str(self.path), *args])
        return code, out.getvalue()

    def close(self):
        self.conn.close()
        self.tmp.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def only(self, plan, group="candidates"):
        others = [g for g in ("candidates", "confirm_only", "refused")
                  if g != group]
        for g in others:
            self.assertEqual(plan[g], [], g)
        self.assertEqual(len(plan[group]), 1, plan[group])
        return plan[group][0]


class TestOpus55Case(Base):
    """The real incident: family-priced events on day D, own row dated D+1."""

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        self.early = [self.f.event("claude-opus-5-5", D + 3600),
                      self.f.event("claude-opus-5-5", D + 7200)]
        self.late = self.f.event("claude-opus-5-5", D + DAY + 60)

    def test_one_candidate_with_window_counts_and_costs(self):
        c = self.only(self.f.plan())
        self.assertEqual(c["prefix"], "claude-opus-5-5")
        self.assertEqual(c["backfill_from"], "2026-09-22")
        self.assertEqual(c["window"], {"first": "2026-09-22",
                                       "last": "2026-09-22", "span": "1 day"})
        self.assertEqual([(m["model"], m["events"]) for m in c["models"]],
                         [("claude-opus-5-5", 2)])
        self.assertAlmostEqual(c["cost_now"], 2 * ev_cost(FAM_OPUS))
        self.assertAlmostEqual(c["cost_after"], 2 * ev_cost(OPUS_55))
        self.assertAlmostEqual(c["delta"],
                               2 * (ev_cost(OPUS_55) - ev_cost(FAM_OPUS)))
        self.assertEqual(sorted(c["impact"]), sorted(self.early))
        self.assertIsNone(c["refused"])

    def test_plan_is_read_only_byte_identical(self):
        self.f.conn.close()
        before = self.f.digest()
        code, out = self.f.cli("--backfill-plan")
        self.assertEqual(code, 0)
        self.assertIn("claude-opus-5-5", out)
        code, _ = self.f.cli("--backfill-plan", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.f.digest(), before)
        self.f.conn = capture.connect(self.f.path)

    def test_plan_function_leaves_no_temp_copy_and_no_rows(self):
        rows = self.f.pricing_rows()
        self.f.plan()
        self.assertEqual(self.f.pricing_rows(), rows)
        self.assertIsNone(self.f.conn.execute(
            "SELECT name FROM sqlite_temp_master WHERE name='pricing'"
        ).fetchone())

    def test_apply_inserts_one_row_and_changes_exactly_the_impact_set(self):
        unrelated = self.f.event("claude-sonnet-5", D + 100)
        rows_before = self.f.pricing_rows()
        res_before = self.f.resolved()
        impact = set(self.only(self.f.plan())["impact"])
        report = self.f.apply("claude-opus-5-5")
        rows_after = self.f.pricing_rows()
        self.assertEqual(len(rows_after), len(rows_before) + 1)
        new = [r for r in rows_after if r not in rows_before]
        self.assertEqual(len(new), 1)
        (provider, prefix, version, *rates, eff, source) = new[0]
        self.assertEqual((provider, prefix, version, tuple(rates), eff),
                         ("anthropic", "claude-opus-5-5", "", OPUS_55, D))
        self.assertEqual(source, f"backfill:{SRC}; confirmed 2026-09-23")
        res_after = self.f.resolved()
        changed = {ev for ev in res_after if res_after[ev] != res_before[ev]}
        self.assertEqual(changed, impact)
        for ev in impact:
            self.assertEqual(res_after[ev], ("claude-opus-5-5", D))
        self.assertEqual(res_after[unrelated], res_before[unrelated])
        self.assertEqual(res_after[self.late], res_before[self.late])
        self.assertIn("Verified: 2 event(s)", report)

    def test_idempotent_reapply(self):
        self.f.apply("claude-opus-5-5")
        rows, res = self.f.pricing_rows(), self.f.resolved()
        report = self.f.apply("claude-opus-5-5")
        self.assertIn("already backfilled", report)
        self.assertEqual(self.f.pricing_rows(), rows)
        self.assertEqual(self.f.resolved(), res)
        plan = self.f.plan()
        self.assertEqual(plan, {"candidates": [], "confirm_only": [],
                                "refused": [], "combined": None})

    def test_json_shape(self):
        code, out = self.f.cli("--backfill-plan", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data),
                         {"candidates", "confirm_only", "refused", "combined"})
        c = data["candidates"][0]
        self.assertEqual(set(c), {
            "prefix", "provider", "model_version", "r0", "backfill_from",
            "window", "models", "events", "cost_now", "cost_after", "delta",
            "overlaps", "requires", "refused", "impact_events"})
        self.assertEqual(set(c["r0"]), {"effective_from", "source", "rates"})
        self.assertEqual(set(c["window"]), {"first", "last", "span"})
        self.assertEqual(set(c["models"][0]), {
            "model", "events", "cost_now", "cost_after", "delta", "first",
            "last"})
        self.assertEqual(c["impact_events"], 2)
        self.assertEqual(c["r0"]["effective_from"], "2026-09-23")

    def test_markdown_table(self):
        code, out = self.f.cli("--backfill-plan")
        self.assertEqual(code, 0)
        self.assertIn("Backfill available — 1 candidate", out)
        self.assertIn("| `claude-opus-5-5` | 2026-09-22 (1 day) |", out)
        self.assertIn("$16.00 → $12.40", out)
        self.assertIn("**-$3.60**", out)

    def test_cli_apply(self):
        code, out = self.f.cli("--backfill-apply", "claude-opus-5-5")
        self.assertEqual(code, 0, out)
        self.assertIn("**-$3.60**", out)


class TestAncestorCase(Base):
    """Successor events priced by a predecessor's own row (an ancestor row)."""

    def test_successor_priced_by_predecessor_is_a_candidate(self):
        self.f.price("claude-opus-5", FAM_OPUS, D - 10 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        ev = self.f.event("claude-opus-5-5", D + 60)
        self.assertEqual(self.f.resolved()[ev], ("claude-opus-5", D - 10 * DAY))
        c = self.only(self.f.plan())
        self.assertEqual((c["prefix"], c["backfill_from"], c["impact"]),
                         ("claude-opus-5-5", "2026-09-22", [ev]))
        self.assertAlmostEqual(c["delta"], ev_cost(OPUS_55) - ev_cost(FAM_OPUS))
        self.f.apply("claude-opus-5-5")
        self.assertEqual(self.f.resolved()[ev], ("claude-opus-5-5", D))


class TestSharedPrefix(Base):
    """A backdated predecessor row also re-prices a successor's estimated
    events — the successor is listed under its own name."""

    FAM = (6.0, 30.0, 0.6, 7.5, 12.0)

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-", self.FAM, D - 30 * DAY)
        self.f.price("claude-opus-5", FAM_OPUS, D + 5 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + 10 * DAY)
        self.e5 = self.f.event("claude-opus-5", D + 60)
        self.e55 = self.f.event("claude-opus-5-5", D + 2 * DAY)

    def test_successor_listed_under_its_own_name(self):
        plan = self.f.plan()
        by = {c["prefix"]: c for c in plan["candidates"]}
        self.assertEqual(set(by), {"claude-opus-5", "claude-opus-5-5"})
        c5 = by["claude-opus-5"]
        self.assertEqual(c5["backfill_from"], "2026-09-22")
        self.assertEqual({m["model"]: m["events"] for m in c5["models"]},
                         {"claude-opus-5": 1, "claude-opus-5-5": 1})
        self.assertEqual(sorted(c5["impact"]), sorted([self.e5, self.e55]))
        self.assertEqual(c5["window"], {"first": "2026-09-22",
                                        "last": "2026-09-24",
                                        "span": "3 days"})
        self.assertEqual(c5["overlaps"], ["claude-opus-5-5"])
        c55 = by["claude-opus-5-5"]
        self.assertEqual((c55["backfill_from"], c55["impact"]),
                         ("2026-09-24", [self.e55]))

    def test_stale_plan_new_own_row_between_plan_and_apply(self):
        stale = {c["prefix"]: c for c in self.f.plan()["candidates"]}
        self.assertIn(self.e55, stale["claude-opus-5"]["impact"])
        # the successor's own (backfilled) row arrives before the apply
        self.f.apply("claude-opus-5-5")
        before = self.f.resolved()
        report = self.f.apply("claude-opus-5")
        after = self.f.resolved()
        self.assertEqual(after[self.e5], ("claude-opus-5", D))
        self.assertEqual(after[self.e55], before[self.e55])
        self.assertEqual(after[self.e55], ("claude-opus-5-5", D + 2 * DAY))
        self.assertIn("Verified: 1 event(s)", report)

    def test_stale_plan_prefix_no_longer_a_candidate_is_refused(self):
        self.assertTrue(self.f.plan()["candidates"])
        # an own row for claude-opus-5 dated before its events arrives
        self.f.price("claude-opus-5", FAM_OPUS, D - DAY, source="manual")
        rows = self.f.pricing_rows()
        with self.assertRaises(pricing_update.BackfillRefused):
            self.f.apply("claude-opus-5")
        self.assertEqual(self.f.pricing_rows(), rows)

    def test_applying_both_prices_shared_event_at_the_longest_row(self):
        self.f.apply("claude-opus-5", "claude-opus-5-5")
        res = self.f.resolved()
        self.assertEqual(res[self.e5], ("claude-opus-5", D))
        self.assertEqual(res[self.e55], ("claude-opus-5-5", D + 2 * DAY))

    def test_all_or_nothing_with_an_invalid_prefix(self):
        rows, res = self.f.pricing_rows(), self.f.resolved()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-opus-5", "claude-opus-5-5", "claude-nope-1")
        self.assertIn("claude-nope-1", cm.exception.args[0])
        self.assertEqual(self.f.pricing_rows(), rows)
        self.assertEqual(self.f.resolved(), res)
        code, out = self.f.cli("--backfill-apply", "claude-opus-5",
                               "claude-nope-1")
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.f.pricing_rows(), rows)


class TestRefusal(Base):
    """An own-priced event would change -> refused, never offered."""

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-5", FAM_OPUS, D - 10 * DAY)
        self.f.price("claude-opus-5-2", OPUS_55, D + DAY)
        # estimated: ancestor row claude-opus-5 for claude-opus-5-2
        self.est = self.f.event("claude-opus-5-2", D + 60)
        # OWN-priced: a date snapshot of Opus 5 that claude-opus-5-2 prefixes
        self.own = self.f.event("claude-opus-5-20260601", D + 120)

    def test_refused_and_not_offered(self):
        c = self.only(self.f.plan(), "refused")
        self.assertEqual(c["prefix"], "claude-opus-5-2")
        self.assertIn("claude-opus-5-20260601 (1)", c["refused"])
        _, out = self.f.cli("--backfill-plan")
        self.assertIn("Refused", out)
        self.assertNotIn("Backfill available", out)

    def test_apply_writes_nothing(self):
        rows, res = self.f.pricing_rows(), self.f.resolved()
        with self.assertRaises(pricing_update.BackfillRefused):
            self.f.apply("claude-opus-5-2")
        self.assertEqual(self.f.pricing_rows(), rows)
        self.assertEqual(self.f.resolved(), res)


class TestUnpricedRefusal(Base):
    def test_previously_unpriced_event_in_the_window_refuses(self):
        # fixture-only: drop the seed so an early event is genuinely unpriced
        self.f.conn.execute("DELETE FROM pricing WHERE model_prefix ="
                            " 'claude-opus-'")
        self.f.price("claude-opus-", FAM_OPUS, D + 3600)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        self.f.event("claude-opus-5-5", D + 100)      # unpriced
        self.f.event("claude-opus-5-5", D + 7200)     # estimated (family)
        c = self.only(self.f.plan(), "refused")
        self.assertIn("previously unpriced events: claude-opus-5-5 (1)",
                      c["refused"])


class TestZeroDelta(Base):
    def test_equal_rates_grouped_as_confirm_only(self):
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-4-8", FAM_OPUS, D + DAY)
        self.f.event("claude-opus-4-8", D + 60)
        c = self.only(self.f.plan(), "confirm_only")
        self.assertEqual(c["prefix"], "claude-opus-4-8")
        self.assertAlmostEqual(c["delta"], 0.0)
        _, out = self.f.cli("--backfill-plan")
        self.assertIn("Confirm only — no cost change", out)


class TestNoCandidates(Base):
    def test_family_only_model_and_own_priced_history_offer_nothing(self):
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5", FAM_OPUS, D - 5 * DAY)
        self.f.event("claude-haiku-4-5-20251001", D)   # no own row at all
        self.f.event("claude-opus-5", D)                # own-priced
        self.assertEqual(self.f.plan(), {"candidates": [], "confirm_only": [],
                                         "refused": [], "combined": None})
        _, out = self.f.cli("--backfill-plan")
        self.assertIn("No backfill candidates", out)


class TestTriggerNeedsOwnRow(Base):
    def test_predecessor_row_is_not_offered_for_successor_only_events(self):
        # only the successor has estimated events before the predecessor's
        # row: backfilling claude-opus-5 would swap one estimate for another
        # (an ancestor rate), so only the successor's own prefix is offered
        self.f.price("claude-opus-", (6.0, 30.0, 0.6, 7.5, 12.0), D - 30 * DAY)
        self.f.price("claude-opus-5", FAM_OPUS, D + 5 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + 10 * DAY)
        self.f.event("claude-opus-5-5", D + 60)
        c = self.only(self.f.plan())
        self.assertEqual(c["prefix"], "claude-opus-5-5")


class TestSafetyNets(Base):
    """Fault injection: the guards that are unreachable by construction still
    refuse / roll back when their invariant breaks."""

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        self.ev = self.f.event("claude-opus-5-5", D + 60)
        self.f.event("claude-opus-5-5", D + 120)

    def patch(self, obj, name, fn):
        orig = getattr(obj, name)
        setattr(obj, name, fn(orig))
        self.addCleanup(setattr, obj, name, orig)

    def test_verification_mismatch_rolls_back(self):
        ev = self.ev

        def wrap(orig):
            def resolve_main(sh):
                out = orig(sh)
                out[ev] = None   # pretend one event resolved elsewhere
                return out
            return resolve_main
        self.patch(pricing_update._Shadow, "resolve_main", wrap)
        rows = self.f.pricing_rows()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-opus-5-5")
        self.assertIn("ROLLED BACK", cm.exception.args[0])
        self.assertEqual(self.f.pricing_rows(), rows)

    def test_combined_set_mismatch_refuses(self):
        def wrap(orig):
            def plan(sh, today):
                cands = orig(sh, today)
                for c in cands:
                    c["impact"] = c["impact"][:1]   # a truncated impact set
                return cands
            return plan
        self.patch(pricing_update, "_plan_candidates", wrap)
        rows = self.f.pricing_rows()
        with self.assertRaises(pricing_update.BackfillRefused):
            self.f.apply("claude-opus-5-5")
        self.assertEqual(self.f.pricing_rows(), rows)

    def test_event_resolving_to_another_row_refuses_the_candidate(self):
        def wrap(orig):
            def resolve(sh, before=None):
                return {ev: (999999 if rid == -1 else rid)
                        for ev, rid in orig(sh, before).items()}
            return resolve
        self.patch(pricing_update._Shadow, "resolve", wrap)
        c = self.only(self.f.plan(), "refused")
        self.assertIn("other than the backfill row", c["refused"])


class TestCrossBundleSafetyNet(Base):
    """Fault injection isolating the resolved-row mismatch check from the
    F2 est=0 check below it: a resolution wrongly landing on another,
    UNRELATED bundle member's own row still passes ``_is_own`` (it does not
    verify the row's prefix actually matches the event's model — only that
    it is not a family/ancestor row) but must still be caught as a
    mismatch against the planned row, so the wrong-resolution check alone
    (independent of the est=0 one) still rolls back."""

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        self.opus_ev = self.f.event("claude-opus-5-5", D + 60)
        self.f.price("claude-sonnet-", (6.0, 30.0, 0.6, 7.5, 12.0), D - 30 * DAY)
        self.f.price("claude-sonnet-4-100", (2.0, 10.0, 0.2, 2.5, 4.0), D + DAY)
        self.sonnet_ev = self.f.event("claude-sonnet-4-100", D + 60)

    def patch(self, obj, name, fn):
        orig = getattr(obj, name)
        setattr(obj, name, fn(orig))
        self.addCleanup(setattr, obj, name, orig)

    def test_wrong_resolution_to_another_bundle_members_row_rolls_back(self):
        opus_ev, sonnet_ev = self.opus_ev, self.sonnet_ev

        def wrap(orig):
            def resolve_main(sh):
                out = orig(sh)
                out[opus_ev], out[sonnet_ev] = out[sonnet_ev], out[opus_ev]
                return out
            return resolve_main
        self.patch(pricing_update._Shadow, "resolve_main", wrap)
        rows = self.f.pricing_rows()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-opus-5-5", "claude-sonnet-4-100")
        self.assertIn("ROLLED BACK", cm.exception.args[0])
        self.assertIn("resolved differently", cm.exception.args[0])
        self.assertEqual(self.f.pricing_rows(), rows)


class TestHumanSpan(unittest.TestCase):
    def test_spans(self):
        d = datetime.date(2026, 9, 1)
        span = pricing_update.human_span
        self.assertEqual(span(d, d), "1 day")
        self.assertEqual(span(d, d + datetime.timedelta(days=2)), "3 days")
        self.assertEqual(span(d, d + datetime.timedelta(days=20)), "3 weeks")
        self.assertEqual(span(d, d + datetime.timedelta(days=59)), "2 months")


if __name__ == "__main__":
    unittest.main()
