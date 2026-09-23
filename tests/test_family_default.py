"""The family-default flag — one definition, three implementations.

A pricing row is a FAMILY DEFAULT row when its `model_prefix` matches
`^claude-[a-z]+-$` (docs/TELEMETRY-CONTRACT.md §Pricing table). The Python
helper (`capture.is_family_default`), the SQLite expression
(`capture.family_default_sql`) and the Postgres expression in
`supabase/reports.sql` must agree on every string. The Postgres check needs a
reachable server (psql + PG* env) and skips cleanly otherwise.
"""
import json
import pathlib
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture

REPORTS_SQL = (pathlib.Path(__file__).resolve().parent.parent
               / "supabase" / "reports.sql").read_text()

POSITIVES = ["claude-opus-", "claude-fable-", "claude-sonnet-", "claude-haiku-",
             "claude-mythos-", "claude-a-", "claude-newfamily-"]
NEGATIVES = ["claude-opus-5-5", "claude-opus-4-2025", "claude-3-5-haiku",
             "claude-sonnet-4", "claude-3-5-haiku-", "claude-opus-4-",
             "claude-opus-4-0", "claude-fable-5-1", "claude-", "claude--",
             "claude-opus", "claude-Opus-", "CLAUDE-opus-", "xclaude-opus-",
             "claude-opus--", "claude-op-us-", "claude-op_us-", "claude-op%s-",
             "claude-opus-\n", "claude-opus- ", " claude-opus-", "claude-é-",
             "claude-opus-x", "", "gpt-4o"]
# Alphabet for the randomized sweep: the structural characters plus the ones a
# naive GLOB/LIKE would mishandle (digits, dash, upper case, LIKE wildcards).
ALPHABET = "abz-09AZ_%é"
PG_EXPR_RE = re.compile(r"model_prefix OPERATOR\(pg_catalog\.~\) '(\^claude-\[a-z\]\+-\$)'")


def sweep(n=3000, seed=133):
    """Deterministic random strings shaped `claude-<noise>` (and some noise
    without the prefix) — dense near the boundary the check has to get right."""
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        body = "".join(rnd.choice(ALPHABET) for _ in range(rnd.randint(0, 6)))
        out.append(("claude-" if rnd.random() < 0.9 else "") + body
                   + ("-" if rnd.random() < 0.6 else ""))
    return out


def sqlite_eval(values):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t(i INTEGER, x TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", enumerate(values))
        return [r[0] for r in conn.execute(
            f"SELECT {capture.family_default_sql('x')} FROM t ORDER BY i")]
    finally:
        conn.close()


class TestPythonHelper(unittest.TestCase):
    def test_positives(self):
        for p in POSITIVES:
            self.assertTrue(capture.is_family_default(p), p)

    def test_negatives(self):
        for p in NEGATIVES:
            self.assertFalse(capture.is_family_default(p), repr(p))

    def test_non_string_is_not_a_family_default(self):
        self.assertFalse(capture.is_family_default(None))

    def test_seed_rows_are_family_defaults(self):
        for row in capture.PRICING_SEED:
            self.assertTrue(capture.is_family_default(row[1]), row[1])

    def test_matches_the_contract_regex(self):
        regex = re.compile(r"^claude-[a-z]+-$")
        for s in POSITIVES + NEGATIVES + sweep():
            # `$` in Python also matches before a trailing newline; the
            # contract means end-of-string, so compare against \Z semantics.
            expected = bool(regex.match(s)) and not s.endswith("\n")
            self.assertEqual(capture.is_family_default(s), expected, repr(s))


class TestSqliteExpression(unittest.TestCase):
    def test_equivalent_to_python_on_the_named_cases(self):
        values = POSITIVES + NEGATIVES
        got = sqlite_eval(values)
        for v, g in zip(values, got):
            self.assertEqual(bool(g), capture.is_family_default(v), repr(v))

    def test_equivalent_to_python_on_a_randomized_sweep(self):
        values = sweep()
        got = sqlite_eval(values)
        self.assertEqual([bool(g) for g in got],
                         [capture.is_family_default(v) for v in values])

    def test_null_stays_null(self):
        self.assertEqual(sqlite_eval([None]), [None])

    def test_naive_glob_would_not_be_equivalent(self):
        # Documents why the structural check exists.
        conn = sqlite3.connect(":memory:")
        try:
            for bad in ("claude-opus-4-", "claude-haiku-3-5-"):
                self.assertEqual(conn.execute(
                    "SELECT ? GLOB 'claude-[a-z]*-'", (bad,)).fetchone()[0], 1)
            self.assertEqual(sqlite_eval(["claude-opus-4-",
                                          "claude-haiku-3-5-"]), [0, 0])
        finally:
            conn.close()


PG_TOOLS = shutil.which("psql") is not None


@unittest.skipUnless(PG_TOOLS, "no psql on PATH — the Postgres expression "
                     "equivalence runs where a Postgres is reachable (CI)")
class TestPostgresExpression(unittest.TestCase):
    def test_view_uses_the_contract_regex(self):
        self.assertEqual(len(PG_EXPR_RE.findall(REPORTS_SQL)), 1)

    def test_equivalent_to_python(self):
        pattern = PG_EXPR_RE.search(REPORTS_SQL).group(1)
        values = POSITIVES + NEGATIVES + sweep(800)
        lit = ",".join("(" + str(i) + ", '" + v.replace("'", "''") + "')"
                       for i, v in enumerate(values))
        sql = ("SELECT json_agg(x OPERATOR(pg_catalog.~) '" + pattern
               + "' ORDER BY i) FROM (VALUES " + lit + ") v(i, x);")
        r = subprocess.run(["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                            "-c", sql], capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0 and ("could not connect" in r.stderr
                                  or "connection to server" in r.stderr):
            self.skipTest(f"no reachable Postgres: {r.stderr.strip()}")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip())
        self.assertEqual(got, [capture.is_family_default(v) for v in values])


if __name__ == "__main__":
    unittest.main()
