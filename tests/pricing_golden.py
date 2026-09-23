"""Golden per-event price resolution from the PRE-AOS-133 pricing rules.

`tests/fixtures/pricing_golden_pre_aos133.json` records, for every scenario
below (a fixture page or an inline entry list, run on a given date), the
pricing row each (model name, timestamp) event resolves to after a fresh DB is
loaded with ONLY that run's `build_candidates` output:
`[model_prefix, in_usd, out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,
effective_from]`, or null when unpriced. Resolution is the production one
(`report.resolved_subquery`: longest prefix, then greatest effective_from <=
ts) — unchanged by AOS-133.

The golden was generated ONCE from the pre-change code — `build_candidates` of
`scripts/pricing_update.py` at commit 4f8c215 (v0.14.1), loaded as a separate
module straight from git:

    python3 tests/pricing_golden.py --generate

`tests/test_pricing_update.py::TestGoldenPreAos133` runs the SAME scenarios
through the current `build_candidates` and fails on any cost difference not on
its reviewed allow-list. Regenerating is only ever needed if a scenario is
added; the baseline commit stays 4f8c215.
"""
import datetime
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scripts"))
import capture  # noqa: E402
import report  # noqa: E402

GOLDEN = HERE / "fixtures" / "pricing_golden_pre_aos133.json"
BASELINE_COMMIT = "4f8c215"
RATE_COLS = ("in_usd", "out_usd", "cache_r_usd", "cache_w_usd",
             "cache_w_1h_usd", "effective_from")
DAY = 86400
# event timestamps, relative to the run date's UTC midnight
TS_OFFSETS = {"-20d": -20 * DAY, "-1s": -1, "0": 0, "+1h": 3600,
              "+30d": 30 * DAY}

FIXTURE_NAMES = [
    "claude-fable-5", "claude-fable-5-20260601", "claude-fable-5-1-20260901",
    "claude-fable-5-1-x", "claude-fable-6", "claude-mythos-5-1-x",
    "claude-opus-5", "claude-opus-5-5", "claude-opus-5-5-20261001",
    "claude-opus-4-8", "claude-opus-4-5-20251101", "claude-opus-4-1",
    "claude-opus-4-1-20250805", "claude-opus-4-0", "claude-opus-4-20250514",
    "claude-opus-4-9", "claude-sonnet-5", "claude-sonnet-5-20260101",
    "claude-sonnet-5-1", "claude-sonnet-4-6", "claude-sonnet-4-5",
    "claude-sonnet-4-20250514", "claude-sonnet-4-7",
    "claude-haiku-4-5-20251001", "claude-haiku-5", "claude-3-5-haiku-20241022",
    "claude-3-5-haiku-x", "gpt-4o"]


def _r(i, o):
    return {"in_usd": i, "out_usd": o, "cache_r_usd": i / 10,
            "cache_w_usd": i * 1.25, "cache_w_1h_usd": i * 2}


def _e(fam, ver, rates, cond=None):
    return {"family": fam, "version": ver, "rates": rates,
            "condition": None if cond is None else [cond[0], cond[1]]}


# Scenario list: (name, source, today, names). source is {"fixture": file} or
# {"entries": [...]} (condition dates as ISO strings).
T = "2026-09-15"
SCENARIOS = [
    ("page@2026-08-06 intro in force, increase future",
     {"fixture": "pricing-page.html"}, "2026-08-06", FIXTURE_NAMES),
    ("page@2026-08-31 intro last day",
     {"fixture": "pricing-page.html"}, "2026-08-31", FIXTURE_NAMES),
    ("page@2026-09-15 intro expired, increase arrived",
     {"fixture": "pricing-page.html"}, T, FIXTURE_NAMES),
    ("current@2026-09-21",
     {"fixture": "pricing-page-current.html"}, "2026-09-21", FIXTURE_NAMES),
    ("increase@2026-08-15 before starting",
     {"fixture": "pricing-page-increase.html"}, "2026-08-15", FIXTURE_NAMES),
    ("increase@2026-09-15 after starting",
     {"fixture": "pricing-page-increase.html"}, T, FIXTURE_NAMES),
    ("expired-intro@2026-08-15 intro in force",
     {"fixture": "pricing-page-expired-intro.html"}, "2026-08-15",
     FIXTURE_NAMES),
    ("expired-intro@2026-09-15 intro expired",
     {"fixture": "pricing-page-expired-intro.html"}, T, FIXTURE_NAMES),
    ("C newest uncond + arrived starting",
     {"entries": [_e("sonnet", "5", _r(3, 15)),
                  _e("sonnet", "5", _r(4, 20), ("starting", "2026-09-01")),
                  _e("sonnet", "4.5", _r(3, 15))]}, T,
     ["claude-sonnet-5", "claude-sonnet-5-20260101", "claude-sonnet-4-5",
      "claude-sonnet-5-1"]),
    ("C2 arrived starting listed first",
     {"entries": [_e("sonnet", "5", _r(4, 20), ("starting", "2026-09-01")),
                  _e("sonnet", "5", _r(3, 15)),
                  _e("sonnet", "4.5", _r(3, 15))]}, T,
     ["claude-sonnet-5", "claude-sonnet-4-5"]),
    ("C3 starting today",
     {"entries": [_e("sonnet", "5", _r(3, 15)),
                  _e("sonnet", "5", _r(4, 20), ("starting", T))]}, T,
     ["claude-sonnet-5"]),
    ("D uncond (other rate) before in-force through",
     {"entries": [_e("opus", "5", _r(5, 25)), _e("opus", "4.1", _r(15, 75)),
                  _e("opus", "4.1", _r(10, 50), ("through", "2026-10-01"))]},
     T, ["claude-opus-4-1", "claude-opus-5"]),
    ("D2 in-force through before uncond (other rate)",
     {"entries": [_e("opus", "5", _r(5, 25)),
                  _e("opus", "4.1", _r(10, 50), ("through", "2026-10-01")),
                  _e("opus", "4.1", _r(15, 75))]},
     T, ["claude-opus-4-1"]),
    ("E two uncond rows for one version",
     {"entries": [_e("sonnet", "4.5", _r(3, 15)),
                  _e("sonnet", "4.5", _r(6, 22.5)),
                  _e("sonnet", "4", _r(3, 15))]}, T,
     ["claude-sonnet-4-5", "claude-sonnet-4-20250514"]),
    ("F anti-shadowing",
     {"entries": [_e("fable", "5.1", _r(10, 50)),
                  _e("fable", "5", _r(12, 60))]}, T,
     ["claude-fable-5-1", "claude-fable-5-1-2026", "claude-fable-5",
      "claude-fable-5-2026", "claude-fable-6"]),
    ("G newest with in-force through + older sibling",
     {"entries": [_e("fable", "5.1", _r(10, 50)),
                  _e("fable", "5.1", _r(8, 40), ("through", "2026-10-01")),
                  _e("fable", "5", _r(12, 60))]}, T,
     ["claude-fable-5-1", "claude-fable-5"]),
    ("H conditional-only family",
     {"entries": [_e("mythos", "5", _r(10, 50),
                     ("through", "2026-10-01"))]}, T, ["claude-mythos-5"]),
    ("I future starting",
     {"entries": [_e("haiku", "5", _r(1, 5)),
                  _e("haiku", "5", _r(2, 10), ("starting", "2026-12-01"))]},
     T, ["claude-haiku-5"]),
    ("J Opus 4 alias prefixes",
     {"entries": [_e("opus", "4", _r(15, 75)),
                  _e("opus", "4.1", _r(20, 80))]}, T,
     ["claude-opus-4-20250514", "claude-opus-4-0", "claude-opus-4-1",
      "claude-opus-4-5"]),
    ("K Haiku 3.5 legacy alias newest",
     {"entries": [_e("haiku", "3.5", _r(0.8, 4)),
                  _e("haiku", "3", _r(0.25, 1.25))]}, T,
     ["claude-3-5-haiku-20241022", "claude-haiku-3", "claude-haiku-3-5"]),
]


def epoch(d):
    return int(datetime.datetime.combine(
        d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())


def scenario_entries(source, parse_models):
    """The scenario's page entries, in the shape `build_candidates` takes."""
    if "fixture" in source:
        return parse_models((HERE / "fixtures" / source["fixture"])
                            .read_text())
    out = []
    for e in source["entries"]:
        cond = e["condition"]
        out.append({"family": e["family"], "version": e["version"],
                    "rates": dict(e["rates"]),
                    "condition": None if cond is None else
                    (cond[0], datetime.date.fromisoformat(cond[1]))})
    return out


def resolve(candidates, names, today):
    """Load ONLY `candidates` into a fresh DB (the seed rows are removed), add
    one event per (name, TS_OFFSETS) and return {name: {offset_label: row}}
    with row = [model_prefix, *RATE_COLS] resolved by the production resolver,
    or None when unpriced."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = capture.connect(pathlib.Path(tmp) / "u.db")
        try:
            conn.execute("DELETE FROM pricing")
            for c in candidates:
                r = c["rates"]
                conn.execute(
                    "INSERT INTO pricing(provider, model_prefix, in_usd,"
                    " out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,"
                    " effective_from, source) VALUES"
                    " ('anthropic',?,?,?,?,?,?,?,'golden')",
                    (c["prefix"], r["in_usd"], r["out_usd"], r["cache_r_usd"],
                     r["cache_w_usd"], r["cache_w_1h_usd"],
                     c["effective_from"]))
            pid = conn.execute("INSERT INTO projects(path) VALUES ('/p')"
                               ).lastrowid
            sid = conn.execute("INSERT INTO sessions(uuid, project_id)"
                               " VALUES ('s', ?)", (pid,)).lastrowid
            t0 = epoch(today)
            for n in names:
                mid = conn.execute("INSERT INTO models(name) VALUES (?)",
                                   (n,)).lastrowid
                for off in TS_OFFSETS.values():
                    conn.execute("INSERT INTO events(ts, session_id, kind,"
                                 " model_id) VALUES (?,?,0,?)",
                                 (t0 + off, sid, mid))
            cols = ", ".join([report.resolved_subquery("pr.model_prefix")]
                             + [report.rate_subquery(c) for c in RATE_COLS])
            out = {n: {} for n in names}
            label = {v: k for k, v in TS_OFFSETS.items()}
            for row in conn.execute(
                    f"SELECT m.name, e.ts, {cols} FROM events e"
                    " JOIN models m ON m.id = e.model_id"):
                name, ts, prefix = row[0], row[1], row[2]
                out[name][label[ts - t0]] = (None if prefix is None
                                             else [prefix, *row[3:]])
            return out
        finally:
            conn.close()


def run_scenarios(build_candidates, parse_models):
    """{scenario name: {model name: {offset label: row}}} for the given
    `build_candidates` implementation."""
    out = {}
    for name, source, today, names in SCENARIOS:
        d = datetime.date.fromisoformat(today)
        cands = build_candidates(scenario_entries(source, parse_models), d)
        out[name] = resolve(cands, names, d)
    return out


def load_baseline_module():
    """`scripts/pricing_update.py` at BASELINE_COMMIT, as a separate module."""
    src = subprocess.run(
        ["git", "-C", str(REPO), "show",
         f"{BASELINE_COMMIT}:scripts/pricing_update.py"],
        check=True, capture_output=True, text=True).stdout
    tmp = pathlib.Path(tempfile.mkdtemp()) / "pricing_update_pre_aos133.py"
    tmp.write_text(src)
    spec = importlib.util.spec_from_file_location("pricing_update_pre_aos133",
                                                  tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def generate():
    old = load_baseline_module()
    doc = {
        "_note": ("Golden per-event pricing resolution from the PRE-AOS-133 "
                  f"rules: build_candidates at commit {BASELINE_COMMIT} "
                  "(v0.14.1), loaded from git as a separate module. Generated "
                  "once by `python3 tests/pricing_golden.py --generate`; see "
                  "that file's docstring. Row = [model_prefix, in_usd, "
                  "out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd, "
                  "effective_from] or null (unpriced). Timestamps are offsets "
                  "from the run date's UTC midnight."),
        "baseline_commit": BASELINE_COMMIT,
        "ts_offsets": TS_OFFSETS,
        "scenarios": run_scenarios(old.build_candidates, old.parse_models),
    }
    GOLDEN.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN.relative_to(REPO)}")


if __name__ == "__main__":
    if sys.argv[1:] != ["--generate"]:
        sys.exit("usage: python3 tests/pricing_golden.py --generate")
    generate()
