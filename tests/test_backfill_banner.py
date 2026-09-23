"""The dashboard's "backfill available" line (AOS-149): the cached plan
summary `pricing_update.py --backfill-plan` writes (scripts/backfill_summary.py),
its fingerprint-based staleness, its symlink-safe atomic writer, and the
dashboard's read path, which degrades every bad state to "no line".

Every test points TOKEN_TELEMETRY_DB at a fixture DB in a temp dir, so the
summary sidecar lands next to it — never in the real telemetry directory.
The page-level behaviour (the line rendered by the real client script) is in
tests/test_dashboard_client.py."""
import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import backfill_summary
import capture
import dashboard
import pricing_update
import settings

from tests.test_backfill import D, DAY, FAM_OPUS, OPUS_55, Fixture

LINE_TAIL = (" — run /token-telemetry:pricing-update to review and confirm"
             " (plan computed just now).")


class Base(unittest.TestCase):
    """A fixture with one offerable bundle (claude-opus-5-5: two estimated
    events on D before its own row at D+1, one own-priced event after), with
    TOKEN_TELEMETRY_DB pointed at it."""

    def setUp(self):
        self.f = Fixture()
        env = mock.patch.dict(os.environ, {"TOKEN_TELEMETRY_DB": str(self.f.path)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.f.close)
        self.f.price("claude-opus-", FAM_OPUS, D - 30 * DAY)
        self.f.price("claude-opus-5-5", OPUS_55, D + DAY)
        self.early = [self.f.event("claude-opus-5-5", D + 3600),
                      self.f.event("claude-opus-5-5", D + 7200)]
        self.late = self.f.event("claude-opus-5-5", D + DAY + 60)
        self.cache = pathlib.Path(self.f.tmp.name) / "backfill-plan.json"

    def cli(self, *args, db=True):
        out, err = io.StringIO(), io.StringIO()
        argv = (["--db", str(self.f.path)] if db else []) + list(args)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pricing_update.main(argv)
        return code, out.getvalue(), err.getvalue()

    def line(self, now=None):
        ro = dashboard.sqlite3.connect(f"file:{self.f.path}?mode=ro", uri=True)
        try:
            return dashboard.backfill_line(ro, int(time.time()) if now is None else now)
        finally:
            ro.close()

    def payload(self):
        ro = dashboard.sqlite3.connect(f"file:{self.f.path}?mode=ro", uri=True)
        ro.row_factory = dashboard.sqlite3.Row
        try:
            return dashboard.build_data(ro, {"period": ["year"]})
        finally:
            ro.close()

    def fp(self):
        return backfill_summary.fingerprint(self.f.conn)

    def expected_line(self):
        return "Backfill available: 1 bundle, -$3.60" + LINE_TAIL


class TestCacheWriter(Base):

    def test_plan_writes_summary_for_the_dashboard_db(self):
        code, out, err = self.cli("--backfill-plan")
        self.assertEqual(code, 0, out)
        self.assertEqual(err, "")
        self.assertTrue(self.cache.is_file())
        d = json.loads(self.cache.read_text())
        self.assertEqual(d["bundles"], 1)
        self.assertEqual(d["deltaText"], "-$3.60")
        self.assertEqual(d["fingerprint"], self.fp())
        self.assertEqual(d["version"], backfill_summary.CACHE_VERSION)
        self.assertLessEqual(abs(d["computedAt"] - time.time()), 60)
        # the delta is the plan's own "everything offered" figure
        self.assertIn("(-$3.60).", out)

    def test_plan_stdout_unchanged_by_caching(self):
        _, with_cache, _ = self.cli("--backfill-plan")
        self.assertEqual(
            with_cache.rstrip("\n"),
            pricing_update.render_backfill_plan(
                pricing_update.backfill_plan(self.f.conn, pricing_update.datetime.date.today())))

    def test_json_plan_also_writes(self):
        code, out, _ = self.cli("--backfill-plan", "--json")
        self.assertEqual(code, 0, out)
        json.loads(out)
        self.assertTrue(self.cache.is_file())

    def test_summary_file_is_0600(self):
        self.cli("--backfill-plan")
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)

    def test_no_temp_file_left_behind(self):
        self.cli("--backfill-plan")
        self.cli("--backfill-plan")
        names = sorted(p.name for p in self.cache.parent.iterdir()
                       if "backfill-plan" in p.name)
        self.assertEqual(names, ["backfill-plan.json"])

    def test_plan_run_is_still_read_only(self):
        self.f.conn.close()
        before = self.f.digest()
        self.cli("--backfill-plan")
        self.assertEqual(self.f.digest(), before)
        self.f.conn = capture.connect(self.f.path)

    def test_plan_over_another_db_never_writes(self):
        # the dashboard reads TOKEN_TELEMETRY_DB; a plan over a different
        # file must not leave a summary for the dashboard's DB
        other_dir = tempfile.TemporaryDirectory()
        self.addCleanup(other_dir.cleanup)
        other = pathlib.Path(other_dir.name) / "usage.db"
        capture.connect(other).close()
        with mock.patch.dict(os.environ, {"TOKEN_TELEMETRY_DB": str(other)}):
            code, out, _ = self.cli("--backfill-plan")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.cache.exists())
        self.assertFalse((pathlib.Path(other_dir.name) / "backfill-plan.json").exists())

    def test_db_changed_during_plan_is_not_cached(self):
        # fingerprint before != after (a capture or refresh landed while the
        # plan was computing): the summary would describe a state the plan
        # never saw, so nothing is written
        plan_ = pricing_update.backfill_plan(self.f.conn, pricing_update.datetime.date.today())
        wrote = pricing_update._cache_plan_summary(
            self.f.path, plan_, "a" * 64, "b" * 64)
        self.assertFalse(wrote)
        self.assertFalse(self.cache.exists())
        wrote = pricing_update._cache_plan_summary(
            self.f.path, plan_, self.fp(), self.fp())
        self.assertTrue(wrote)
        self.assertTrue(self.cache.exists())

    def test_symlinked_target_is_refused_and_left_untouched(self):
        victim = pathlib.Path(self.f.tmp.name) / "victim.txt"
        victim.write_text("keep me")
        self.cache.symlink_to(victim)
        code, out, err = self.cli("--backfill-plan")
        self.assertEqual(code, 0, out)
        self.assertIn("claude-opus-5-5", out)
        self.assertIn("backfill summary was not updated", err)
        self.assertTrue(self.cache.is_symlink())
        self.assertEqual(victim.read_text(), "keep me")

    def test_symlinked_temp_name_is_never_followed(self):
        victim = pathlib.Path(self.f.tmp.name) / "victim.txt"
        victim.write_text("keep me")
        with mock.patch.object(backfill_summary.secrets, "token_hex",
                               return_value="fixed"):
            tmp = self.cache.with_name(
                f".{self.cache.name}.{os.getpid()}.fixed.tmp")
            tmp.symlink_to(victim)
            with self.assertRaises(OSError):
                backfill_summary.write({"bundles": 1, "deltaText": "-$1.00"},
                                       self.fp(), int(time.time()))
        self.assertEqual(victim.read_text(), "keep me")
        self.assertFalse(self.cache.exists())

    def test_successful_apply_clears_summary(self):
        self.cli("--backfill-plan")
        self.assertTrue(self.cache.exists())
        code, out, _ = self.cli("--backfill-apply", "claude-opus-5-5")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.cache.exists())

    def test_refused_apply_keeps_summary(self):
        self.cli("--backfill-plan")
        code, out, _ = self.cli("--backfill-apply", "claude-sonnet-5")
        self.assertEqual(code, 1, out)
        self.assertTrue(self.cache.exists())

    def test_nothing_offered_writes_zero_bundles(self):
        f2 = Fixture()
        self.addCleanup(f2.close)
        with mock.patch.dict(os.environ, {"TOKEN_TELEMETRY_DB": str(f2.path)}):
            with contextlib.redirect_stdout(io.StringIO()):
                pricing_update.main(["--backfill-plan"])
            d = json.loads((pathlib.Path(f2.tmp.name) / "backfill-plan.json").read_text())
        self.assertEqual((d["bundles"], d["deltaText"]), (0, None))


class TestFingerprint(Base):

    def test_new_capture_after_the_horizon_keeps_the_fingerprint(self):
        before = self.fp()
        self.f.event("claude-opus-5-5", D + 2 * DAY)
        self.f.event("claude-sonnet-5", D + 3 * DAY)
        self.assertEqual(self.fp(), before)

    def test_pricing_change_changes_the_fingerprint(self):
        before = self.fp()
        self.f.price("claude-sonnet-5", (3.0, 15.0, 0.3, 3.75, 6.0), D + 2 * DAY)
        self.assertNotEqual(self.fp(), before)

    def test_pricing_change_within_the_horizon_changes_the_fingerprint(self):
        # neither row moves the horizon (a later row of an existing prefix,
        # a family-default rate change), so only the pricing-row hash sees it
        before = self.fp()
        self.f.price("claude-opus-5-5", (4.5, 22.0, 0.3, 5.5, 9.0), D + 2 * DAY)
        later_row = self.fp()
        self.assertNotEqual(later_row, before)
        self.f.price("claude-opus-", (6.0, 30.0, 0.6, 7.5, 12.0), D - 10 * DAY)
        self.assertNotEqual(self.fp(), later_row)

    def test_event_before_the_horizon_changes_the_fingerprint(self):
        before = self.fp()
        rid = self.f.event("claude-opus-5-5", D + 100)
        added = self.fp()
        self.assertNotEqual(added, before)
        self.f.conn.execute("DELETE FROM events WHERE rowid = ?", (self.early[0],))
        self.f.conn.commit()
        self.assertNotEqual(self.fp(), added)
        self.assertIsNotNone(rid)


class TestBannerLine(Base):

    def write_raw(self, data):
        if isinstance(data, (dict, list)):
            data = json.dumps(data)
        if isinstance(data, str):
            data = data.encode()
        self.cache.write_bytes(data)

    def valid(self, **over):
        d = {"version": 1, "computedAt": int(time.time()), "bundles": 1,
             "deltaText": "-$3.60", "fingerprint": self.fp()}
        d.update(over)
        return d

    def test_present(self):
        self.cli("--backfill-plan")
        self.assertEqual(self.line(), self.expected_line())
        self.assertEqual(self.payload()["priceWarning"]["backfill"], self.expected_line())

    def test_plural_and_age(self):
        now = int(time.time())
        self.write_raw(self.valid(bundles=2, computedAt=now - 3 * 3600,
                                  deltaText="+$1,234.5678"))
        self.assertEqual(self.line(now),
                         "Backfill available: 2 bundles, +$1,234.5678 — run"
                         " /token-telemetry:pricing-update to review and"
                         " confirm (plan computed 3 hours ago).")

    def test_absent(self):
        self.assertFalse(self.cache.exists())
        self.assertIsNone(self.line())
        self.assertIsNone(self.payload()["priceWarning"]["backfill"])

    def test_zero_bundles_is_no_line(self):
        self.write_raw(self.valid(bundles=0, deltaText=None))
        self.assertIsNone(self.line())

    def test_stale_after_pricing_refresh(self):
        self.cli("--backfill-plan")
        self.f.price("claude-sonnet-5", (3.0, 15.0, 0.3, 3.75, 6.0), D + 2 * DAY)
        self.assertIsNone(self.line())
        self.assertIsNone(self.payload()["priceWarning"]["backfill"])

    def test_stale_after_old_events_imported(self):
        self.cli("--backfill-plan")
        self.f.event("claude-opus-5-5", D + 50)
        self.assertIsNone(self.line())

    def test_stale_after_apply_even_if_file_restored(self):
        self.cli("--backfill-plan")
        saved = self.cache.read_bytes()
        self.cli("--backfill-apply", "claude-opus-5-5")
        self.cache.write_bytes(saved)
        self.assertIsNone(self.line())

    def test_fresh_capture_keeps_line(self):
        self.cli("--backfill-plan")
        self.f.event("claude-opus-5-5", int(time.time()))
        self.assertEqual(self.line(), self.expected_line())

    def test_corrupt_variants_are_no_line_and_no_error(self):
        now = int(time.time())
        cases = {
            "not json": b"{not json",
            "truncated": json.dumps(self.valid())[:-5].encode(),
            "binary": b"\xff\xfe\x00garbage",
            "empty": b"",
            "list": [1, 2],
            "wrong version": self.valid(version=2),
            "bundles str": self.valid(bundles="1"),
            "bundles bool": self.valid(bundles=True),
            "bundles negative": self.valid(bundles=-1),
            "computedAt float": self.valid(computedAt=now + 0.5),
            "computedAt future": self.valid(computedAt=now + 86400),
            "computedAt zero": self.valid(computedAt=0),
            "fingerprint short": self.valid(fingerprint="abc"),
            "fingerprint missing": {k: v for k, v in self.valid().items()
                                    if k != "fingerprint"},
            "delta hostile": self.valid(deltaText='<img src=x onerror="alert(1)">'),
            "delta missing": self.valid(deltaText=None),
            "delta number": self.valid(deltaText=-3.6),
            "oversized": json.dumps(self.valid(pad="x" * 5000)).encode(),
            # valid JSON whose first MAX_CACHE_BYTES still parse: only the
            # size check (not the bounded read) rejects it
            "oversized padded": (json.dumps(self.valid()) + " " * 5000).encode(),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.write_raw(data)
                self.assertIsNone(self.line(now), name)
                self.assertIsNone(self.payload()["priceWarning"]["backfill"], name)

    def test_fingerprint_error_is_no_line_and_no_error(self):
        self.cli("--backfill-plan")
        with mock.patch.object(backfill_summary, "fingerprint",
                               side_effect=dashboard.sqlite3.OperationalError("boom")):
            self.assertIsNone(self.line())
            self.assertIsNone(self.payload()["priceWarning"]["backfill"])

    def test_directory_at_cache_path_is_no_line(self):
        self.cache.mkdir()
        self.assertIsNone(self.line())
        self.payload()   # never raises

    def test_symlink_to_valid_summary_is_not_followed(self):
        real = pathlib.Path(self.f.tmp.name) / "elsewhere.json"
        real.write_text(json.dumps(self.valid()))
        self.cache.symlink_to(real)
        self.assertIsNone(self.line())

    def test_remote_backend_hides_the_line(self):
        self.cli("--backfill-plan")
        self.assertIsNotNone(self.line())
        settings.write_settings({"active_backend": "supabase"})
        self.assertIsNone(self.line())
        self.assertIsNone(self.payload()["priceWarning"]["backfill"])

    def test_banner_shows_line_even_with_no_model_listed(self):
        self.cli("--backfill-plan")
        pw = self.payload()["priceWarning"]
        self.assertEqual((pw["estimated"], pw["unpriced"]), ([], []))
        self.assertEqual(pw["backfill"], self.expected_line())

    def test_age_text(self):
        at = backfill_summary.age_text
        self.assertEqual([at(-5), at(0), at(59), at(60), at(119), at(3599),
                          at(3600), at(7200), at(47 * 3600), at(48 * 3600),
                          at(86400 * 9)],
                         ["just now", "just now", "just now", "1 minute ago",
                          "1 minute ago", "59 minutes ago", "1 hour ago",
                          "2 hours ago", "47 hours ago", "2 days ago",
                          "9 days ago"])

    def test_delta_shapes_accepted(self):
        for d in ("-$3.60", "+$1.20", "$0.00", "+$0.0042", "-$1,234,567.89"):
            self.assertRegex(d, backfill_summary.DELTA_RE)
        for d in ("$3.6", "3.60", "-$3.60 ", "$1,23.00", "+-$1.00", ""):
            self.assertNotRegex(d, backfill_summary.DELTA_RE)

    def test_delta_regex_matches_every_usd_change_output(self):
        for now, after in ((8.0, 6.2), (0.0, 0.0), (1.0, 1.0042), (0.5, 1e7),
                           (6450.37, 6177.91), (0.0001, 0.0)):
            self.assertRegex(pricing_update._usd_change(now, after)[2],
                             backfill_summary.DELTA_RE)


if __name__ == "__main__":
    unittest.main()
