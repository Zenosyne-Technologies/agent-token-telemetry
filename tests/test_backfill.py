"""Consent-gated pricing backfill (`pricing_update.py --backfill-plan` /
`--backfill-apply`): docs/TELEMETRY-CONTRACT.md §Pricing table, "History is
never mutated", case 3. Fixture DBs only."""
import contextlib
import datetime
import hashlib
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture
import pricing_update
import report

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
            self.f.apply("claude-opus-5", "claude-opus-5-5", "claude-haiku-9")
        self.assertIn("claude-haiku-9", cm.exception.args[0])
        self.assertEqual(self.f.pricing_rows(), rows)
        self.assertEqual(self.f.resolved(), res)
        code, out = self.f.cli("--backfill-apply", "claude-opus-5",
                               "claude-haiku-9")
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
        self.assertTrue(out.startswith(
            "No backfill can be offered — 1 candidate(s) refused:"), out)

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


def gfm_row_cells(line):
    """Split one markdown table row into cells by the GFM table-row rules
    (cmark-gfm's cell scanner, which marked's table tokenizer mirrors),
    written independently of the code under test: a backslash and the
    character after it are ONE escaped pair and never a delimiter; an
    unescaped ``|`` ends a cell; leading/trailing pipes are optional; each
    cell is trimmed and a backslash-pipe then unescaped to ``|`` (left to
    right, as both renderers do) BEFORE inline (code-span) parsing."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    cells, cur, i = [], [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2])
            i += 2
        elif s[i] == "|":
            cells.append("".join(cur))
            cur, i = [], i + 1
        else:
            cur.append(s[i])
            i += 1
    if "".join(cur).strip():
        cells.append("".join(cur))
    return [c.strip().replace("\\|", "|") for c in cells]


def code_spans(text):
    """CommonMark code spans of one inline run: ``(inside, outside)`` —
    the list of code-span contents, and the text outside every span. A
    backslash-escaped character outside a span is literal (an escaped
    backtick opens nothing); a backtick run opens a span only when a run of
    the SAME length closes it, else it is literal."""
    inside, outside, i = [], [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            outside.append(text[i:i + 2])
            i += 2
            continue
        if ch != "`":
            outside.append(ch)
            i += 1
            continue
        j = i
        while j < len(text) and text[j] == "`":
            j += 1
        n, k, close = j - i, j, -1
        while k < len(text):
            if text[k] != "`":
                k += 1
                continue
            m = k
            while m < len(text) and text[m] == "`":
                m += 1
            if m - k == n:
                close = k
                break
            k = m
        if close < 0:
            outside.append(text[i:j])
            i = j
        else:
            inside.append(text[j:close])
            i = close + n
    return inside, "".join(outside)


def gfm_tables(md):
    """Every table in ``md`` as ``(header_cells, [row_cells, ...])`` — a
    table starts at a ``|`` line followed by a ``|---`` delimiter line and
    runs while lines start with ``|`` (the plan/apply renderers' shape)."""
    lines, out, i = md.splitlines(), [], 0
    while i < len(lines):
        if (lines[i].startswith("|") and i + 1 < len(lines)
                and lines[i + 1].startswith("|---")):
            head, rows, i = gfm_row_cells(lines[i]), [], i + 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(gfm_row_cells(lines[i]))
                i += 1
            out.append((head, rows))
        else:
            i += 1
    return out


class GfmAssertions:
    """Structural checks of rendered backfill markdown against hostile
    names: every table row keeps the header's column count, no injection
    marker ever appears OUTSIDE a code span (so no bold/link/heading text
    can activate), and every marker the output carries sits inside one."""

    MARK = "INJ"

    def assert_structurally_safe(self, md, expect_markers=()):
        tables = gfm_tables(md)
        self.assertTrue(tables, md)
        spans_all = []
        for head, rows in tables:
            for row in rows:
                self.assertEqual(len(row), len(head), (head, row))
                for cell in row:
                    inside, outside = code_spans(cell)
                    self.assertNotIn(self.MARK, outside, cell)
                    self.assertNotIn("`", outside, cell)
                    spans_all += inside
        for line in md.splitlines():
            if not line.startswith("|"):
                inside, outside = code_spans(line)
                self.assertNotIn(self.MARK, outside, line)
                spans_all += inside
        for m in expect_markers:
            self.assertTrue(any(m in sp for sp in spans_all),
                            f"{m} not inside any code span")
        return spans_all


class TestMdCellGfm(unittest.TestCase, GfmAssertions):
    """report.md_cell (shared by every report and by the backfill's
    _mdname): a value can never add, remove or split a GFM table cell,
    in a code span or in plain text."""

    HOSTILE = [
        "a \\| **INJ1** \\| b", "a \\\\| **INJ2** |", "a \\\\\\| INJ3",
        "a | INJ4 | b", "a `**INJ5**` b", "``INJ6``", "a\n| INJ7 | x |",
        "trailing \\", "\\", "\\|", "|", "a [INJ8](https://e.test)",
        "x" * 119 + "\\|INJ9", "x" * 119 + "|", "x" * 120 + "\\",
    ]

    def test_backslash_escaped_before_pipe(self):
        self.assertEqual(report.md_cell("a\\|b"), "a\\\\\\|b")
        self.assertEqual(report.md_cell("a|b"), "a\\|b")
        self.assertEqual(report.md_cell("a\\b"), "a\\\\b")

    def test_every_hostile_value_keeps_the_row_shape(self):
        for v in self.HOSTILE:
            cell = report.md_cell(v)
            for row in (f"| `{cell}` | {cell} | end |",
                        f"| {cell} | `{cell}` | end |"):
                cells = gfm_row_cells(row)
                self.assertEqual(len(cells), 3, (v, row, cells))
                self.assertEqual(cells[2], "end", (v, row))
            inside, outside = code_spans(gfm_row_cells(f"| `{cell}` |")[0])
            self.assertEqual((len(inside), outside), (1, ""), (v, cell))

    def test_cap_never_splits_an_escape_pair(self):
        out = report.md_cell("x" * 119 + "\\|tail")
        self.assertTrue(out.endswith("…"), out)
        self.assertEqual(len(gfm_row_cells(f"| `{out}` | end |")), 2, out)


HOSTILE_MODELS = [
    "claude-opus-5-5 \\| **INJA bold** \\| x",      # the validator repro
    "claude-opus-5-5 \\\\| **INJB** |",
    "claude-opus-5-5 ` **INJC** `",
    "claude-opus-5-5\n| INJD | fake | row |",
    "claude-opus-5-5 **INJE**",
    "claude-opus-5-5 [INJF](https://e.test)",
    "claude-opus-5-5 INJG trailing \\",
    "claude-opus-5-5 | INJH plain pipe",
]
HOSTILE_PREFIX = "claude-sonnet-9 \\| **INJP** [INJQ](https://e.test)"
HOSTILE_SUCC = "claude-fable-5-5-\\| **INJR** [INJS](https://e.test) \\|"
HOSTILE_OWN = "claude-haiku-5-1 \\| **INJT** \\| [INJU](https://e.test)"


class TestHostileNamesRender(Base, GfmAssertions):
    """F1: hostile model names and pricing prefixes — pipes, backslash-
    pipes, backticks, newlines, bold, links — through the plan table, the
    requires cell, the apply table and every refusal, parsed by the GFM
    rules above: no row changes shape and nothing escapes its code span."""

    def setUp(self):
        super().setUp()
        f = self.f
        f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        f.price("claude-opus-5-5", OPUS_55, D + 20 * DAY)
        for i, n in enumerate(HOSTILE_MODELS):
            f.event(n, D + DAY + i * 60)
        # hostile PREFIX offered as a candidate
        f.price("claude-sonnet-", FAM_OPUS, D - 30 * DAY)
        f.price(HOSTILE_PREFIX, OPUS_55, D + 20 * DAY)
        f.event(HOSTILE_PREFIX + " tail", D + 2 * DAY)
        # hostile successor prefix inside a "requires" cell
        f.price("claude-fable-", FAM_OPUS, D - 30 * DAY)
        f.price("claude-fable-5", OPUS_55, D + 20 * DAY)
        f.price(HOSTILE_SUCC, (2.0, 10.0, 0.2, 2.5, 4.0), D + 20 * DAY)
        f.event("claude-fable-5", D + DAY)
        f.event(HOSTILE_SUCC + "x", D + 3 * DAY)
        # hostile own-priced event in a REFUSED candidate's reason
        f.price("claude-haiku-", FAM_OPUS, D - 30 * DAY)
        f.price("claude-haiku-5", FAM_OPUS, D - 20 * DAY)
        f.price("claude-haiku-5-1", OPUS_55, D + 20 * DAY)
        f.event("claude-haiku-5-1", D + DAY)
        f.event(HOSTILE_OWN, D + DAY + 5)

    def test_plan_tables_keep_shape_and_names_stay_in_code_spans(self):
        plan = self.f.plan()
        self.assertEqual({c["prefix"] for c in plan["refused"]},
                         {"claude-haiku-5-1"})
        fable = [c for c in plan["candidates"]
                 if c["prefix"] == "claude-fable-5"]
        self.assertEqual(fable[0]["requires"], [HOSTILE_SUCC])
        code, md = self.f.cli("--backfill-plan")
        self.assertEqual(code, 0)
        self.assert_structurally_safe(md, expect_markers=[
            "INJA", "INJB", "INJC", "INJD", "INJE", "INJF", "INJG", "INJH",
            "INJP", "INJQ", "INJR", "INJS", "INJT", "INJU"])
        # the requires cell itself: the hostile successor is one code span
        row = next(r for _h, rows in gfm_tables(md) for r in rows
                   if r[0].startswith("`claude-fable-5` — requires"))
        inside, outside = code_spans(row[0])
        self.assertEqual(len(inside), 2, row[0])
        self.assertIn("INJR", inside[1])

    def test_apply_table_and_refusals_stay_structurally_safe(self):
        _, js = self.f.cli("--backfill-plan", "--json")
        data = json.loads(js)
        offered = [c["prefix"] for c in data["candidates"]
                   + data["confirm_only"]]
        # hostile prefixes never pass the CLI's argument check (exit 2), so
        # the renderer is exercised through the function itself
        out = self.f.apply(*offered)
        self.assert_structurally_safe(out, expect_markers=[
            "INJA", "INJH", "INJP", "INJR"])
        # refused + unknown hostile prefix: plain list lines, no table
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-haiku-5-1", "claude-bogus \\| **INJV** `x`")
        out = cm.exception.args[0]
        for line in out.splitlines():
            self.assertNotIn(self.MARK, code_spans(line)[1], line)
        self.assertIn("INJT", out)
        self.assertIn("INJV", out)


def _cmark_row_cells(row):
    """Independent GFM table-row cell counter (ported from the completion
    validator's cmark-gfm scanner, NOT the helper above): a backslash plus
    the next character is one pair, an unescaped ``|`` is a delimiter, and a
    cell's ``\\|`` becomes a literal ``|`` after the split."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    out, cur, i = [], "", 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            cur += s[i:i + 2]
            i += 2
            continue
        if ch == "|":
            out.append(cur)
            cur, i = "", i + 1
            continue
        cur += ch
        i += 1
    if cur.strip():
        out.append(cur)
    return [c.replace("\\|", "|") for c in out]


def _cmark_split_code(text):
    """``(spans, outside)`` per CommonMark (ported from the validator): a
    backtick run opens a code span only when a run of the same length closes
    it; an escaped backtick outside a span is literal."""
    spans, outside, i = [], "", 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "`":
            outside += text[i:i + 2]
            i += 2
            continue
        if text[i] == "`":
            n = len(re.match(r"`+", text[i:]).group(0))
            m = re.compile(r"(?<!`)`{%d}(?!`)" % n).search(text, i + n)
            if m:
                spans.append(text[i + n:m.start()])
                outside += "<C>"
                i = m.end()
                continue
            outside += "`" * n
            i += n
            continue
        outside += text[i]
        i += 1
    return spans, outside


# The renderer's own bold is only ever a signed dollar delta.
_DELTA_BOLD = re.compile(r"\*\*[+-]?\$[0-9,]+\.[0-9]+\*\*")


def _hx(tag):
    """A maximally hostile name fragment carrying marker ``tag``: backtick
    spans, a single- and a double-backslash pipe, bold, a link, a newline
    and U+202E — every way a raw name could split a cell or escape its
    code span."""
    return (f"`{tag}`\\|\\\\|**{tag}**[{tag}](u)\n"
            f"\u202e{tag}``")


# predecessor candidate prefix, its successor (an ANCESTOR-shaped extension,
# so the predecessor needs it: "requires" + overlaps), and a refused prefix
# whose unclosable successor model is named in the refusal reason
E2E_PRED = "claude-fable-5 " + _hx("INJP")
E2E_SUCC = E2E_PRED + "-5-" + _hx("INJS")
E2E_PRED_MODEL = E2E_PRED + " " + _hx("INJM")
E2E_SUCC_MODEL = E2E_SUCC + "x"
E2E_REF = "claude-sonnet-5 " + _hx("INJR")
E2E_REF_MODEL = E2E_REF + " u"
E2E_REF_BLOCK = E2E_REF + "-9-" + _hx("INJB")
E2E_UNKNOWN = "claude-opus-9 " + _hx("INJU")


class TestHostileNameEveryRenderPath(Base):
    """F1 end to end: ONE fixture whose hostile names reach every place the
    backfill renders a name — candidate prefix, per-model impact list
    (successor included), "requires" cell, overlaps note, refused prefix and
    reason, the all-refused plan, and the apply report (table rows, no-op
    rows, bad/refused list, F2 closure refusal, insert-collision refusal) —
    parsed with independent GFM rules: every table row has its header's
    width, every name sits inside a code span, and no bold/link syntax exists
    outside one. Each of the 13 ``_mdname`` call sites fails this test when
    it emits a raw code span instead."""

    def setUp(self):
        super().setUp()
        f = self.f
        for n in (E2E_PRED, E2E_SUCC, E2E_PRED_MODEL, E2E_SUCC_MODEL,
                  E2E_REF, E2E_REF_MODEL, E2E_REF_BLOCK, E2E_UNKNOWN):
            self.assertLess(len(n), report.MD_CELL_MAX, n)   # markers survive
        f.price("claude-fable-", FAM_OPUS, D - 30 * DAY)
        f.price(E2E_PRED, OPUS_55, D + 20 * DAY)
        f.price(E2E_SUCC, (2.0, 10.0, 0.2, 2.5, 4.0), D + 20 * DAY)
        f.event(E2E_PRED_MODEL, D + DAY)
        f.event(E2E_SUCC_MODEL, D + 3 * DAY)
        f.price("claude-sonnet-", FAM_OPUS, D - 30 * DAY)
        f.price(E2E_REF, OPUS_55, D + 20 * DAY)
        f.event(E2E_REF_MODEL, D + DAY)
        f.event(E2E_REF_BLOCK, D + 2 * DAY)

    def check(self, md, tags):
        """Assert ``md`` is structurally safe; return the code-span texts."""
        self.assertNotIn("\u202e", md)
        spans_all, hdr, tables = [], None, 0
        for ln, line in enumerate(md.split("\n"), 1):
            if line.startswith("|"):
                cells = _cmark_row_cells(line)
                if hdr is None:
                    hdr, tables = len(cells), tables + 1
                self.assertEqual(len(cells), hdr, (ln, line))
                parts = cells
            else:
                hdr, parts = None, [line]
            for part in parts:
                spans, outside = _cmark_split_code(part)
                spans_all += spans
                self.assertNotIn("INJ", outside, (ln, line))
                self.assertNotIn("`", outside, (ln, line))
                rest = _DELTA_BOLD.sub("", outside)
                self.assertNotIn("**", rest, (ln, line))
                self.assertNotRegex(rest, r"\]\(|<https?:|<a\b", (ln, line))
        for t in tags:
            self.assertTrue(any(t in sp for sp in spans_all),
                            f"{t} not rendered inside a code span:\n{md}")
        return tables

    def refusal(self, *prefixes):
        rows = self.f.pricing_rows()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply(*prefixes)
        self.assertEqual(self.f.pricing_rows(), rows)   # nothing written
        return cm.exception.args[0]

    def test_every_name_render_path(self):
        f = self.f
        plan = f.plan()
        by = {c["prefix"]: c for c in plan["candidates"]}
        self.assertEqual(set(by), {E2E_PRED, E2E_SUCC})
        self.assertEqual(by[E2E_PRED]["requires"], [E2E_SUCC])
        self.assertEqual(by[E2E_PRED]["overlaps"], [E2E_SUCC])
        self.assertEqual([c["prefix"] for c in plan["refused"]], [E2E_REF])
        # 1. the mixed plan: prefix cells, per-model lists (successor model
        # in the predecessor's row), requires cell, overlaps notes, refused
        # prefix + reason
        code, md = f.cli("--backfill-plan")
        self.assertEqual(code, 0)
        self.assertEqual(self.check(md, [
            "INJP", "INJS", "INJM", "INJR", "INJB"]), 1)
        row = next(ln for ln in md.split("\n") if ln.startswith("|")
                   and " — requires " in ln)
        spans = _cmark_split_code(_cmark_row_cells(row)[0])[0]
        self.assertEqual(len(spans), 2, row)          # prefix + requires
        self.assertIn("INJS", spans[1])
        self.assertTrue(any("overlaps" in c and "INJS" in
                            "".join(_cmark_split_code(c)[0])
                            for c in _cmark_row_cells(row)), row)
        # 2. apply refusals (nothing written): refused + unknown prefix
        # (bad list with the refused reason), F2 closure (predecessor names
        # its successor), and an insert collision (a trigger swallows the
        # INSERT, so the row "already exists")
        self.check(self.refusal(E2E_REF, E2E_UNKNOWN),
                   ["INJR", "INJB", "INJU"])
        self.check(self.refusal(E2E_PRED), ["INJP", "INJS"])
        f.conn.execute("CREATE TEMP TRIGGER swallow BEFORE INSERT ON"
                       " main.pricing BEGIN SELECT RAISE(IGNORE); END")
        msg = self.refusal(E2E_PRED, E2E_SUCC)
        f.conn.execute("DROP TRIGGER temp.swallow")
        self.assertIn("already exists", msg)
        self.check(msg, ["INJP"])
        # 3. the apply report table: prefixes and per-model names
        out = f.apply(E2E_PRED, E2E_SUCC)
        self.assertEqual(self.check(out, [
            "INJP", "INJS", "INJM"]), 1)
        self.assertIn("Total for the applied set:", out)
        # 4. no-op rows for already-backfilled prefixes
        out = f.apply(E2E_PRED, E2E_SUCC)
        self.assertEqual(out.count("already backfilled"), 2, out)
        self.assertEqual(self.check(out, ["INJP", "INJS"]), 1)
        # 5. the all-refused plan: header path, refused prefix + reason
        code, md = f.cli("--backfill-plan")
        self.assertEqual(code, 0)
        self.assertTrue(md.startswith("No backfill can be offered"), md)
        self.check(md, ["INJR", "INJB"])


class TestApplyArgumentShape(Base):
    """Defence in depth: ``--backfill-apply`` accepts only the strict prefix
    shape the parser mints and rejects anything else with exit 2 before the
    DB is opened — nothing written, the value not echoed."""

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        self.f.event("claude-opus-5-5", D + 60)

    def test_shell_metacharacter_prefix_is_rejected_nothing_written(self):
        evil = "claude-opus-5-5'; touch /tmp/pwned #"
        before = (self.f.digest(), self.f.pricing_rows())
        code, out = self.f.cli("--backfill-apply", "claude-opus-5-5", evil)
        self.assertEqual(code, 2, out)
        self.assertIn("rejected --backfill-apply argument(s) #2", out)
        self.assertNotIn("touch", out)
        self.assertEqual((self.f.digest(), self.f.pricing_rows()), before)
        # the well-formed prefix alone applies
        code, out = self.f.cli("--backfill-apply", "claude-opus-5-5")
        self.assertEqual(code, 0, out)

    def test_only_the_minted_shapes_pass(self):
        ok = pricing_update.is_pricing_prefix
        for v in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-4-5",
                  "claude-mythos-1", "claude-opus-4-2025", "claude-3-5-haiku",
                  "claude-opus-4-0"):
            self.assertTrue(ok(v), v)
        for v in ("claude-opus-", "claude-opus-5-5\n", "claude-opus-5-5 ",
                  "Claude-opus-5", "claude-opus-5-5-1", "claude-gpt-5",
                  "claude-opus-5-5;id", "claude-opus-5-5$(id)", "",
                  "claude-opus-٥", "-claude-opus-5"):
            self.assertFalse(ok(v), repr(v))
        code, out = self.f.cli("--backfill-apply", "claude-3-5-haiku")
        self.assertEqual(code, 1, out)   # well-formed, just not a candidate


class TestBundleClosure(Base):
    """F2/F3 pinned on the shared-prefix fixture: backfilling the
    predecessor alone would leave the successor's event on an ANCESTOR row
    (still an estimate), so the predecessor is offered only as a bundle with
    the successor, its row shows the bundle's combined figures, and an apply
    that names it without the successor is refused, writing nothing."""

    FAM = (6.0, 30.0, 0.6, 7.5, 12.0)

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-", self.FAM, D - 30 * DAY)
        self.f.price("claude-opus-5", FAM_OPUS, D + 5 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + 10 * DAY)
        self.e5 = self.f.event("claude-opus-5", D + 60)
        self.e55 = self.f.event("claude-opus-5-5", D + 2 * DAY)

    def by(self):
        return {c["prefix"]: c for c in self.f.plan()["candidates"]}

    def test_predecessor_requires_successor(self):
        by = self.by()
        self.assertEqual(by["claude-opus-5"]["requires"], ["claude-opus-5-5"])
        self.assertEqual(by["claude-opus-5-5"]["requires"], [])

    def test_bundle_row_carries_the_bundles_combined_figures(self):
        c5 = self.by()["claude-opus-5"]
        fam, own5, own55 = (ev_cost(self.FAM), ev_cost(FAM_OPUS),
                            ev_cost(OPUS_55))
        self.assertAlmostEqual(c5["cost_now"], 2 * fam)
        # the successor's event lands on ITS OWN row inside the bundle —
        # the solo figure would have priced it at the predecessor's rate
        self.assertAlmostEqual(c5["cost_after"], own5 + own55)
        self.assertAlmostEqual(c5["delta"], own5 + own55 - 2 * fam)
        per = {m["model"]: m["cost_after"] for m in c5["models"]}
        self.assertAlmostEqual(per["claude-opus-5-5"], own55)
        _, md = self.f.cli("--backfill-plan")
        self.assertIn("| `claude-opus-5` — requires `claude-opus-5-5`"
                      " (applied together) |", md)
        self.assertIn(f"${2 * fam:,.2f} → ${own5 + own55:,.2f}", md)

    def test_combined_total_equals_the_real_apply_of_everything(self):
        plan = self.f.plan()
        cb = plan["combined"]
        self.assertIsNotNone(cb)
        fam, own5, own55 = (ev_cost(self.FAM), ev_cost(FAM_OPUS),
                            ev_cost(OPUS_55))
        self.assertAlmostEqual(cb["cost_now"], 2 * fam)
        self.assertAlmostEqual(cb["cost_after"], own5 + own55)
        self.assertEqual(cb["events"], 2)
        _, md = self.f.cli("--backfill-plan")
        self.assertIn(f"If you apply everything offered: ${2 * fam:,.2f} →"
                      f" ${own5 + own55:,.2f} (-${2 * fam - own5 - own55:,.2f}).",
                      md)
        self.f.apply("claude-opus-5", "claude-opus-5-5")
        rates = {"claude-opus-5": FAM_OPUS, "claude-opus-5-5": OPUS_55}
        real = sum(ev_cost(rates[p]) for p, _eff in self.f.resolved().values())
        self.assertAlmostEqual(real, cb["cost_after"])

    def test_each_minimal_closed_bundle_is_offered_on_its_own_row(self):
        # overlapping bundles are NOT merged: the successor alone is a valid
        # (closed) bundle, and predecessor + successor is another
        plan = self.f.plan()
        bundles = sorted(sorted([c["prefix"], *c["requires"]])
                         for c in plan["candidates"] + plan["confirm_only"])
        self.assertEqual(bundles, [["claude-opus-5", "claude-opus-5-5"],
                                   ["claude-opus-5-5"]])
        _, md = self.f.cli("--backfill-plan")
        rows = [ln.split(" | ")[0] for ln in md.split("\n")
                if ln.startswith("| `claude-opus")]
        self.assertEqual(rows, [
            "| `claude-opus-5` — requires `claude-opus-5-5` (applied together)",
            "| `claude-opus-5-5`"])
        # the one-prefix bundle applies on its own; the predecessor then
        # stands alone
        code, out = self.f.cli("--backfill-apply", "claude-opus-5-5")
        self.assertEqual(code, 0, out)
        by = {c["prefix"]: c for c in self.f.plan()["candidates"]}
        self.assertEqual(by["claude-opus-5"]["requires"], [])

    def test_apply_total_matches_the_plan_line_for_the_same_set(self):
        _, md = self.f.cli("--backfill-plan")
        line = next(ln for ln in md.split("\n")
                    if ln.startswith("If you apply everything offered: "))
        figures = line[len("If you apply everything offered: "):]
        bundle = next(ln for ln in md.split("\n")
                      if ln.startswith("| `claude-opus-5` — requires"))
        now_after = figures.split(" (")[0]
        self.assertIn(f"| {now_after} |", bundle)   # same set, same figures
        code, out = self.f.cli("--backfill-apply", "claude-opus-5",
                               "claude-opus-5-5")
        self.assertEqual(code, 0, out)
        self.assertIn(f"\nTotal for the applied set: {figures}\n", out)

    def test_apply_predecessor_alone_is_refused_naming_the_successor(self):
        rows, res = self.f.pricing_rows(), self.f.resolved()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-opus-5")
        msg = cm.exception.args[0]
        self.assertIn("not closed under the own-row requirement", msg)
        self.assertIn("`claude-opus-5` requires `claude-opus-5-5`", msg)
        self.assertEqual(self.f.pricing_rows(), rows)
        self.assertEqual(self.f.resolved(), res)
        code, out = self.f.cli("--backfill-apply", "claude-opus-5")
        self.assertEqual(code, 1)
        self.assertIn("not closed", out)

    def test_post_apply_estimate_check_rolls_back_on_its_own(self):
        # fault injection: with the closure computation disabled the
        # predecessor claims to stand alone, passes the requires check and
        # the resolved-row verification — only the post-apply est=0 check
        # (every impacted event now OWN-priced) can catch it
        orig = pricing_update._close_bundles
        pricing_update._close_bundles = lambda sh, cands: None
        self.addCleanup(setattr, pricing_update, "_close_bundles", orig)
        rows = self.f.pricing_rows()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-opus-5")
        self.assertIn("ROLLED BACK", cm.exception.args[0])
        self.assertIn("1 event(s) still estimated after apply",
                      cm.exception.args[0])
        self.assertEqual(self.f.pricing_rows(), rows)


class TestTransitiveClosure(Base):
    """A 3-level chain: claude-opus-5-5-1's events are reachable only via
    claude-opus-5-5, so the predecessor's bundle must grow TRANSITIVELY."""

    def setUp(self):
        super().setUp()
        f = self.f
        f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.rates = {"claude-opus-5": (5.0, 0, 0, 0, 0),
                      "claude-opus-5-5": (3.0, 0, 0, 0, 0),
                      "claude-opus-5-5-1": (1.0, 0, 0, 0, 0)}
        for p, r in self.rates.items():
            f.price(p, r, D + 40 * DAY)
        for m, t in [("claude-opus-5", D + 10 * DAY + 60),
                     ("claude-opus-5", D + 12 * DAY),
                     ("claude-opus-5-5", D + 60),
                     ("claude-opus-5-5", D + 11 * DAY),
                     ("claude-opus-5-5-1", D + 5 * DAY)]:
            f.event(m, t)

    def test_requires_is_the_transitive_closure(self):
        by = {c["prefix"]: c for c in self.f.plan()["candidates"]}
        self.assertEqual(by["claude-opus-5"]["requires"],
                         ["claude-opus-5-5", "claude-opus-5-5-1"])
        self.assertEqual(by["claude-opus-5-5"]["requires"],
                         ["claude-opus-5-5-1"])
        self.assertEqual(by["claude-opus-5-5-1"]["requires"], [])

    def test_unclosed_subset_refused_closed_set_leaves_nothing_estimated(self):
        rows = self.f.pricing_rows()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-opus-5", "claude-opus-5-5")
        self.assertIn("requires `claude-opus-5-5-1`", cm.exception.args[0])
        self.assertEqual(self.f.pricing_rows(), rows)
        self.f.apply("claude-opus-5", "claude-opus-5-5", "claude-opus-5-5-1")
        names = dict(self.f.conn.execute(
            "SELECT e.rowid, m.name FROM events e JOIN models m"
            " ON m.id = e.model_id"))
        for ev, (prefix, _eff) in self.f.resolved().items():
            self.assertEqual(prefix, names[ev])   # every event own-priced


class TestUnclosable(Base):
    """A successor with no own row anywhere: the predecessor's backfill
    would leave it on an ancestor row, and no candidate can close it —
    refused (never offered), the reason naming the blocking model."""

    def setUp(self):
        super().setUp()
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5", OPUS_55, D + 20 * DAY)
        self.f.event("claude-opus-5", D + DAY)
        self.f.event("claude-opus-5-9", D + 2 * DAY)

    def test_refused_not_offered_with_the_blocking_model_named(self):
        c = self.only(self.f.plan(), "refused")
        self.assertEqual(c["prefix"], "claude-opus-5")
        self.assertEqual(c["refused_models"]["unclosable"],
                         {"claude-opus-5-9": 1})
        self.assertIn("no closing backfill for their own row:"
                      " claude-opus-5-9 (1)", c["refused"])
        _, md = self.f.cli("--backfill-plan")
        self.assertTrue(md.startswith(
            "No backfill can be offered — 1 candidate(s) refused:"), md)
        self.assertIn("- `claude-opus-5` (window 2026-09-23 → 2026-09-24):"
                      " would leave estimated events with no closing backfill"
                      " for their own row: `claude-opus-5-9` (1)", md)
        self.assertNotIn("If you apply everything offered", md)

    def test_apply_refused_writes_nothing(self):
        rows, res = self.f.pricing_rows(), self.f.resolved()
        with self.assertRaises(pricing_update.BackfillRefused) as cm:
            self.f.apply("claude-opus-5")
        self.assertIn("no closing backfill for their own row:"
                      " `claude-opus-5-9` (1)", cm.exception.args[0])
        self.assertEqual(self.f.pricing_rows(), rows)
        self.assertEqual(self.f.resolved(), res)


class TestRefusedCloser(Base):
    """The only candidate that could close the predecessor is itself
    refused (it would re-price an own-priced event), so the predecessor is
    unclosable too: a refused candidate never closes a bundle."""

    def test_predecessor_refused_when_its_closer_is_refused(self):
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5", (5.0, 0, 0, 0, 0), D + 20 * DAY)
        self.f.price("claude-opus-5-5", (3.0, 0, 0, 0, 0), D + 30 * DAY)
        self.f.event("claude-opus-5", D + DAY)
        self.f.event("claude-opus-5-5", D + 2 * DAY)
        self.f.event("claude-opus-5-5x", D + 25 * DAY)   # own-priced by -5
        plan = self.f.plan()
        self.assertEqual((plan["candidates"], plan["confirm_only"]), ([], []))
        by = {c["prefix"]: c for c in plan["refused"]}
        self.assertEqual(by["claude-opus-5-5"]["refused_models"]["own"],
                         {"claude-opus-5-5x": 1})
        self.assertEqual(by["claude-opus-5"]["refused_models"]["unclosable"],
                         {"claude-opus-5-5": 1})
        self.assertIsNone(plan["combined"])


class TestShownFiguresReconcile(Base):
    """F3 LOW: every displayed "now → after (delta)" triple subtracts
    exactly — the delta is the difference of the DISPLAYED totals."""

    def test_usd_change_uses_the_displayed_totals(self):
        chg = pricing_update._usd_change
        self.assertEqual(chg(6450.372261, 6177.905747),
                         ("$6,450.37", "$6,177.91", "-$272.46"))
        self.assertEqual(chg(0.0012, 0.0005), ("$0.0012", "$0.0005", "-$0.0007"))
        self.assertEqual(chg(1.0, 1.0), ("$1.00", "$1.00", "$0.00"))
        self.assertEqual(chg(0.30, 0.29), ("$0.30", "$0.29", "-$0.01"))

    def test_usd_change_reconciles_over_a_sweep(self):
        import decimal
        import random
        rnd = random.Random(136)

        def dec(t):
            return decimal.Decimal(t.replace("$", "").replace(",", ""))
        for _ in range(5000):
            now = rnd.choice([rnd.uniform(0, 0.02), rnd.uniform(0, 10_000)])
            after = rnd.choice([rnd.uniform(0, 0.02), rnd.uniform(0, 10_000)])
            n, a, d = pricing_update._usd_change(now, after)
            self.assertEqual(dec(a) - dec(n), dec(d), (now, after, n, a, d))

    def test_plan_and_apply_lines_reconcile_where_rounding_used_to_differ(self):
        # now 2,469,200 input tokens: $12.346 -> shown $12.35; after at
        # $4.10/M: $10.12372 -> shown $10.12. The unrounded delta -2.22228
        # rounds to -$2.22; the SHOWN figures subtract to -$2.23.
        own = (4.1, 20.0, 0.2, 5.0, 8.0)
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5-5", own, D + DAY)
        self.f.event("claude-opus-5-5", D + 60, tok=(2_469_200, 0, 0, 0, 0))
        _, md = self.f.cli("--backfill-plan")
        self.assertIn("($12.35 → $10.12, -$2.23)", md)
        self.assertIn("| $12.35 → $10.12 | **-$2.23** |", md)
        self.assertIn("If you apply everything offered: $12.35 → $10.12"
                      " (-$2.23).", md)
        self.assertNotIn("2.22", md)
        _, out = self.f.cli("--backfill-apply", "claude-opus-5-5")
        self.assertIn("| $12.35 → $10.12 | **-$2.23** |", out)


class TestApplyTotalReconciles(Base):
    """INFO 4: the apply report's total line is the applied set's single
    combined simulation, displayed like the plan's line — it reconciles to
    the cent with the plan's figure for the same set even where summing the
    per-row (individually rounded) figures would be a cent or two off."""

    def test_total_is_the_combined_figure_not_the_sum_of_rows(self):
        own = (4.1, 20.0, 0.2, 5.0, 8.0)
        tok = (2_469_200, 0, 0, 0, 0)
        for fam, prefix in (("claude-opus-", "claude-opus-5-5"),
                            ("claude-sonnet-", "claude-sonnet-5")):
            self.f.price(fam, FAM_OPUS, D - 30 * DAY)
            self.f.price(prefix, own, D + DAY)
            self.f.event(prefix, D + 60, tok=tok)
        # each row: $12.346 -> $10.12372, shown $12.35 -> $10.12 (-$2.23);
        # rows sum to -$4.46, the set is $24.692 -> $20.24744 (-$4.44)
        _, md = self.f.cli("--backfill-plan")
        self.assertEqual(md.count("| $12.35 → $10.12 | **-$2.23** |"), 2, md)
        self.assertIn("If you apply everything offered: $24.69 → $20.25"
                      " (-$4.44).", md)
        code, out = self.f.cli("--backfill-apply", "claude-opus-5-5",
                               "claude-sonnet-5")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("| $12.35 → $10.12 | **-$2.23** |"), 2)
        self.assertIn("Total for the applied set: $24.69 → $20.25 (-$4.44).",
                      out)

    def test_no_total_line_when_every_prefix_is_a_noop(self):
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        self.f.event("claude-opus-5-5", D + 60)
        self.f.apply("claude-opus-5-5")
        out = self.f.apply("claude-opus-5-5")
        self.assertIn("already backfilled", out)
        self.assertNotIn("Total for the applied set", out)


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
