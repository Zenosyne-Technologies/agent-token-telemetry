"""Behavioural tests for dashboard.html's client code (AOS-135 S3).

These run the page's REAL inline script under the system ``node`` binary,
inside ``vm``, against the fake DOM in tests/dashboard_dom_harness.js — no npm
dependencies. The fake DOM is built from the shipped ``#price-warn`` markup
(parsed here), so renaming a structural id breaks these tests instead of
letting them pass vacuously. It throws on any innerHTML-family write to the
banner (however it is spelled or wherever the call lives), on removal of any
shipped banner node, and on a raw hostile model name reaching ANY innerHTML
sink on the page. See the harness header for the full contract.

Skipped, with a reason, when ``node`` is not on PATH — exactly like the
Postgres-gated parity tests. CI sets ``TOKEN_TELEMETRY_REQUIRE_NODE=1``, which
turns a missing ``node`` into a failure so the gate can never silently skip
there.
"""
import copy
import html.parser
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import capture
import dashboard

from tests.test_capture import entry

DASHBOARD_HTML = ROOT / "scripts" / "dashboard.html"
HARNESS = pathlib.Path(__file__).resolve().parent / "dashboard_dom_harness.js"

NODE = shutil.which("node")
REQUIRE_NODE = os.environ.get("TOKEN_TELEMETRY_REQUIRE_NODE") == "1"
NODE_REASON = ("no `node` on PATH — the dashboard client behaviour tests run "
               "the page's script under node; install Node.js (CI sets "
               "TOKEN_TELEMETRY_REQUIRE_NODE=1 so it can never skip there)")

HOOK = "globalThis.__dashboardTestHooks={renderPriceWarning, fmtUSD};\n"

# Model names that are markup if interpreted as HTML. They must only ever
# appear as text, and a raw copy must never reach an innerHTML sink.
HOSTILE = [
    '<img src=x onerror="globalThis.__pwned=1">',
    'claude-<svg onload=globalThis.__pwned=2>',
    '"><script>globalThis.__pwned=3</script>',
    "<b>bold</b> & 'quoted'",
]


class _Page(html.parser.HTMLParser):
    """Collects every id in the shipped markup, the inline script body, and
    the ``#price-warn`` subtree as a ``{tag, attrs, children}`` tree."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.scripts = [], []
        self.banner, self._stack, self._in_script = None, [], False

    def handle_starttag(self, tag, attrs):
        a = {k: ("" if v is None else v) for k, v in attrs}
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "script":
            self._in_script = True
            self.scripts.append("")
            return
        node = {"tag": tag, "attrs": a, "children": []}
        if self._stack:
            self._stack[-1]["children"].append(node)
        elif a.get("id") == "price-warn":
            self.banner = node
        else:
            return
        if tag not in self.VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._stack and self._stack[-1]["tag"] == tag and tag not in self.VOID:
            self._stack.pop()

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
            return
        if self._stack:
            assert self._stack[-1]["tag"] == tag, (
                f"unbalanced #price-warn markup: </{tag}> closes "
                f"<{self._stack[-1]['tag']}>")
            self._stack.pop()

    def handle_data(self, data):
        if self._in_script:
            self.scripts[-1] += data
        elif self._stack:
            self._stack[-1]["children"].append(data)


def parse_page(source):
    """``(banner_tree, other_ids, hooked_script)`` from dashboard.html text."""
    p = _Page()
    p.feed(source)
    p.close()
    assert p.banner is not None, "no #price-warn element in dashboard.html"
    assert len(p.scripts) == 1, f"expected one inline script, found {len(p.scripts)}"
    script = p.scripts[0].rstrip()
    assert script.endswith("})();"), "page script is no longer one trailing IIFE"
    hooked = script[:-len("})();")] + HOOK + "})();\n"
    banner_ids = set()

    def walk(n):
        if "id" in n["attrs"]:
            banner_ids.add(n["attrs"]["id"])
        for c in n["children"]:
            if isinstance(c, dict):
                walk(c)
    walk(p.banner)
    return p.banner, [i for i in p.ids if i not in banner_ids], hooked


def run_harness(job, source=None):
    """Run one harness job against ``source`` (the shipped page by default)."""
    banner, ids, script = parse_page(source if source is not None
                                     else DASHBOARD_HTML.read_text())
    job = dict(job, bannerTree=banner, pageIds=ids, script=script,
               hostile=[h for h in HOSTILE])
    r = subprocess.run([NODE, str(HARNESS)], input=json.dumps(job),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise AssertionError(f"harness failed: rc={r.returncode}\n{r.stderr}")
    out = json.loads(r.stdout)
    if "fatal" in out:
        raise AssertionError(f"harness fatal: {out['fatal']}")
    return out


def detail_row(model, events, cost, priced=None, first=1788000000, last=None):
    """One :func:`dashboard.fetch_price_warning`-shaped row."""
    priced = events if priced is None else priced
    return {"model": model, "modelName": dashboard._pretty_model(model),
            "events": events, "pricedEvents": priced, "firstSeen": first,
            "lastSeen": first if last is None else last, "cost": cost,
            "unpriced": priced == 0}


# The six banner states the sequence walks, all server-formatted.
EST = [detail_row("claude-opus-5-5", 1, 1.305),
       detail_row("claude-sonnet-5", 3, 7.12, last=1789000000),
       detail_row("mistral-large-2", 3, 2.7, priced=1, last=1789500000)]
UNP = [detail_row("gpt-4o", 2, 0.0, priced=0, last=1788100000)] + [
    detail_row(h, 1, 0.0, priced=0) for h in HOSTILE]
PW_EMPTY = dashboard.build_price_warning([])
PW_FULL = dashboard.build_price_warning(EST + UNP)
PW_EST = dashboard.build_price_warning(EST)
PW_UNP = dashboard.build_price_warning(UNP)
SEQUENCE = [("empty", PW_EMPTY), ("full", PW_FULL), ("empty", PW_EMPTY),
            ("full", PW_FULL), ("estimated-only", PW_EST),
            ("unpriced-only", PW_UNP)]

CELL_CLASSES = ["price-warn-model", "price-warn-id", "", "", ""]


def expected_rows(items):
    """What each ``<li>`` must hold: five text-only spans in this order."""
    return [[it["modelName"], it["model"], it["eventsText"],
             it["dateRangeText"], it["costText"]] for it in items]


@unittest.skipUnless(NODE or REQUIRE_NODE, NODE_REASON)
class _NodeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise AssertionError("TOKEN_TELEMETRY_REQUIRE_NODE=1 but no `node` on PATH")


class TestPriceWarningRendererBehaviour(_NodeTestCase):
    """``renderPriceWarning`` called directly (the real function, in the
    page's real closure) under the throwing fake DOM."""

    def assert_state(self, snap, name, pw):
        where = f"{snap['label']} ({name})"
        self.assertIsNone(snap["error"], f"{where}: renderer threw")
        self.assertEqual(snap["violations"], [], where)
        self.assertTrue(snap["structureIntact"],
                        f"{where}: a shipped #price-warn node was removed")
        est, unp = pw["estimated"], pw["unpriced"]
        if not est and not unp:
            self.assertTrue(snap["boxHidden"], f"{where}: banner shown when empty")
            self.assertEqual(snap["msg"], "", where)
        else:
            self.assertFalse(snap["boxHidden"], f"{where}: banner hidden")
            self.assertEqual(snap["msg"], f"{pw['heading']} {pw['note']}", where)
        for key, items, heading in (("estimated", est, pw["estimatedHeading"]),
                                    ("unpriced", unp, pw["unpricedHeading"])):
            g = snap[key]
            self.assertEqual(g["hidden"], not items, f"{where}: {key} group visibility")
            self.assertEqual(g["heading"], heading if items else "", f"{where}: {key} heading")
            self.assertEqual(len(g["rows"]), len(items), f"{where}: one row per {key} model")
            for row in g["rows"]:
                self.assertEqual(row["tag"], "li", where)
                self.assertEqual([c["tag"] for c in row["cells"]], ["span"] * 5, where)
                self.assertEqual([c["cls"] for c in row["cells"]], CELL_CLASSES, where)
                self.assertEqual([c["elementChildren"] for c in row["cells"]], [0] * 5,
                                 f"{where}: a cell holds markup, not text")
            self.assertEqual([[c["text"] for c in r["cells"]] for r in g["rows"]],
                             expected_rows(items), f"{where}: {key} cells")

    def test_sequence_empty_full_empty_full_estimated_unpriced(self):
        out = run_harness({"mode": "direct", "steps": [pw for _, pw in SEQUENCE]})
        self.assertEqual(len(out["snapshots"]), len(SEQUENCE))
        for snap, (name, pw) in zip(out["snapshots"], SEQUENCE):
            self.assert_state(snap, name, pw)

    def test_first_render_empty_keeps_banner_hidden_and_structure_intact(self):
        out = run_harness({"mode": "direct", "steps": [PW_EMPTY, PW_EMPTY]})
        for snap in out["snapshots"]:
            self.assert_state(snap, "empty", PW_EMPTY)

    def test_hostile_names_render_only_as_exact_text(self):
        out = run_harness({"mode": "direct", "steps": [PW_UNP]})
        snap = out["snapshots"][0]
        self.assert_state(snap, "unpriced-only", PW_UNP)
        ids = [r["cells"][1]["text"] for r in snap["unpriced"]["rows"]]
        for h in HOSTILE:
            self.assertIn(h, ids)

    def test_mixed_model_row_says_what_its_cost_covers(self):
        out = run_harness({"mode": "direct", "steps": [PW_EST]})
        rows = {r["cells"][1]["text"]: [c["text"] for c in r["cells"]]
                for r in out["snapshots"][0]["estimated"]["rows"]}
        mixed = rows["mistral-large-2"]
        self.assertEqual(mixed[2], "3 events")
        self.assertEqual(mixed[4], "$2.70 for 1 priced event; 2 unpriced, not counted")
        self.assertEqual(rows["claude-sonnet-5"][2], "3 events")
        self.assertEqual(rows["claude-opus-5-5"][4], "$1.31")   # 1.305: page rounding


class TestDashboardPageRender(_NodeTestCase):
    """The whole page: the real ``load()`` -> ``renderAll()`` path fed a real
    ``/api/data`` payload (``dashboard.build_data`` over a DB whose model,
    agent and project names are hostile) through a fake ``fetch``, then
    refreshed through the page's own ``#reset`` button. Every innerHTML sink
    on the page is live here: a raw hostile name reaching any of them, any
    innerHTML-family write into the banner, or any exception in rendering
    (which the page turns into its "Connection lost" modal) fails."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        db = pathlib.Path(tmp.name) / "usage.db"
        now = int(time.time())

        def ins(model, ts, mid, project="/proj", agent=None):
            conn = capture.connect(db)
            groups = capture.aggregate([entry(
                model=model, inp=100000, out=20000, cr=50000, cw=10000,
                cw1h=4000, mid=mid,
                ts=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)))])
            with conn:
                capture.insert_events(conn, project, "s1", 0, agent, groups)
            conn.close()

        ins("claude-opus-5-5", now - 3600, "a", agent=HOSTILE[3])
        ins("claude-sonnet-5", now - 7200, "b", project="/p/<i>x</i>")
        ins("gpt-4o", now - 60, "c")
        for i, h in enumerate(HOSTILE):
            ins(h, now - 100 - i, f"h{i}", agent=h, project=f"/p/{h}")
        ro = dashboard.sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ro.row_factory = dashboard.sqlite3.Row
        cls.payload = dashboard.build_data(ro, {"period": ["week"]})
        ro.close()
        pw = cls.payload["priceWarning"]
        assert pw["estimated"] and pw["unpriced"], pw

    def variant(self, pw):
        p = copy.deepcopy(self.payload)
        p["priceWarning"] = pw
        return p

    def run_sequence(self, source=None):
        full = self.payload["priceWarning"]
        est = dict(full, unpriced=[])
        unp = dict(full, estimated=[])
        seq = [PW_EMPTY, full, PW_EMPTY, full, est, unp]
        out = run_harness({"mode": "page",
                           "steps": [self.variant(pw) for pw in seq]}, source)
        return seq, out

    def test_full_page_render_sequence_keeps_banner_correct_and_names_inert(self):
        seq, out = self.run_sequence()
        self.assertEqual(out["violations"], [])
        self.assertEqual(len(out["snapshots"]), len(seq))
        for snap, pw in zip(out["snapshots"], seq):
            where = snap["label"]
            self.assertFalse(snap["modalShown"],
                             f"{where}: render threw -> 'Connection lost'")
            self.assertTrue(snap["structureIntact"], where)
            empty = not pw["estimated"] and not pw["unpriced"]
            self.assertEqual(snap["boxHidden"], empty, where)
            for key in ("estimated", "unpriced"):
                self.assertEqual(snap[key]["hidden"], not pw[key], f"{where}: {key}")
                self.assertEqual([[c["text"] for c in r["cells"]] for r in snap[key]["rows"]],
                                 expected_rows(pw[key]), f"{where}: {key}")
        unpriced_ids = {r["cells"][1]["text"] for r in out["snapshots"][1]["unpriced"]["rows"]}
        for h in HOSTILE:
            self.assertIn(h, unpriced_ids)

    def test_guard_is_live_unescaped_name_in_a_page_sink_is_caught(self):
        # Negative control: drop esc() from the model chips (a sink OUTSIDE
        # the banner). The taint check must catch it — proving the page-wide
        # run is not vacuous.
        src = DASHBOARD_HTML.read_text()
        needle = "</span>${esc(it.name)}</button>`"
        self.assertEqual(src.count(needle), 1)
        _, out = self.run_sequence(src.replace(needle, "</span>${it.name}</button>`"))
        self.assertTrue(any("raw hostile name" in v for v in out["violations"]),
                        out["violations"])
        self.assertTrue(out["snapshots"][0]["modalShown"])

    def test_guard_is_live_original_F1_wipe_is_caught(self):
        # Negative control: the original F1 bug (box.innerHTML="" on the empty
        # path) must be caught and must surface as the "Connection lost" modal.
        src = DASHBOARD_HTML.read_text()
        needle = '      box.hidden=true;\n      $("price-warn-msg").textContent="";\n'
        self.assertEqual(src.count(needle), 1)
        _, out = self.run_sequence(src.replace(
            needle, '      box.hidden=true;\n      box.innerHTML="";\n'))
        self.assertTrue(any("innerHTML write on #price-warn" in v for v in out["violations"]),
                        out["violations"])
        self.assertTrue(any(s["modalShown"] for s in out["snapshots"]))


class TestBannerCostMatchesPageFormatter(_NodeTestCase):
    """One formatter: the banner's server-side ``_warn_usd`` must render every
    cost exactly as the page's own ``fmtUSD`` does (the "By model" bars,
    KPIs and tables), including half-cent ties where Python's default
    rounding and the browser's disagree."""

    VALUES = [0, 0.00005, 0.00015, 0.0001234, 0.00994, 0.00995, 0.01, 0.0105,
              0.0115, 0.0145, 0.0155,
              1.3049999999, 2.6749999999, 999.9949999999, 0.0624999999,
              0.0625, 0.125, 0.3125, 0.355, 0.9995, 0.99951, 1, 1.005, 1.015,
              1.1025, 1.125, 1.305, 2.675, 7.12, 7.187544, 999.995, 1000,
              1234.565, 98765.4321, 1234567.895]

    def test_warn_usd_equals_page_fmtUSD(self):
        import random
        rnd = random.Random(135)
        values = self.VALUES + [round(rnd.uniform(0, 5000), rnd.randint(2, 6))
                                for _ in range(400)]
        values += [rnd.uniform(0, 2) for _ in range(200)]
        out = run_harness({"mode": "fmt", "fmtValues": values})
        mismatches = [(v, js, dashboard._warn_usd(v))
                      for v, js in zip(values, out["fmt"])
                      if js != dashboard._warn_usd(v)]
        self.assertEqual(mismatches, [])


class TestPriceWarnStaticMarkupAndCSS(unittest.TestCase):
    """Static checks over the shipped page source — no ``node`` needed. The
    fake DOM in dashboard_dom_harness.js cannot evaluate CSS, so the
    ``display`` guard has to be asserted as text against the stylesheet; and
    the initial ``hidden`` state has to be asserted on the markup the server
    actually ships, before any client-side render runs."""

    # Whitespace-tolerant: collapses any run of whitespace (including
    # newlines) to a single space before matching, so reformatting the
    # guard rule across lines or re-indenting it does not break this test.
    GUARD_RE = re.compile(
        r"#price-warn\s*\[\s*hidden\s*\]\s*,\s*#price-warn\s+\[\s*hidden\s*\]"
        r"\s*\{\s*display\s*:\s*none\s*!important\s*;?\s*\}"
    )

    def test_id_guard_beats_any_display_override(self):
        # D8b/D9/D11/D12: a static class-based check (the old version of
        # this test) can be defeated by an id selector or an inline
        # style="display:…" — neither is `.price-warn`/`.price-warn-group`,
        # so it never appears in a class-based pattern, yet both can still
        # override `hidden`. Instead, assert the page ships ONE guard rule
        # that beats every one of those routes on its own terms: an
        # `!important` declaration on #price-warn[hidden] and its hidden
        # descendants always wins the cascade over a later selector (any
        # specificity), an id selector, or a non-important inline style —
        # see dashboard.html's "PRICE WARNING" comment block.
        source = DASHBOARD_HTML.read_text()
        style_blocks = re.findall(r"<style>(.*?)</style>", source, re.S)
        self.assertTrue(style_blocks, "no <style> block in dashboard.html")

        guard_hits = sum(len(self.GUARD_RE.findall(block)) for block in style_blocks)
        self.assertEqual(
            guard_hits, 1,
            "expected exactly one "
            "`#price-warn[hidden], #price-warn [hidden]{display:none !important;}` "
            "guard rule across all <style> blocks")

        # (b) no OTHER rule, anywhere, sets `display` with `!important` on a
        # selector that mentions price-warn: a second !important rule could
        # itself win a later cascade tie-break (author order) against the
        # guard, undoing the guarantee above. `price-warn` is checked as a
        # substring so this catches the id and every class variant alike.
        rule_re = re.compile(r"([^{}]+)\{([^{}]*)\}")
        for block in style_blocks:
            for sel_group, body in rule_re.findall(block):
                rule_text = f"{sel_group}{{{body}}}"
                if self.GUARD_RE.search(re.sub(r"\s+", " ", rule_text)):
                    continue  # the guard rule itself
                if "display" not in body or "!important" not in body:
                    continue
                for sel in sel_group.split(","):
                    self.assertNotIn(
                        "price-warn", sel,
                        f"unexpected extra !important display rule on "
                        f"{sel.strip()!r} — only the guard rule may use "
                        f"!important on a price-warn selector")

    def test_banner_ships_hidden_before_any_render(self):
        # C3: the server-rendered markup must carry `hidden` on #price-warn
        # itself, so the banner starts hidden before the first client-side
        # render (or if the first fetch fails and no render ever runs).
        banner, _, _ = parse_page(DASHBOARD_HTML.read_text())
        self.assertIn("hidden", banner["attrs"],
                       "#price-warn must ship with the `hidden` attribute")


if __name__ == "__main__":
    unittest.main()
