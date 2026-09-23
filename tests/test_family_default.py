"""The estimated flag — one definition, three implementations.

A pricing row is a FAMILY DEFAULT row when its `model_prefix` matches
`^claude-[a-z]+-$`, and an ANCESTOR row for a model when R (the model name
with the row's prefix removed from its start) matches `^-[0-9]{1,2}(-|$)`; an
event resolved to either is ESTIMATED (docs/TELEMETRY-CONTRACT.md §Pricing
table). The Python helpers (`capture.is_family_default`,
`capture.is_ancestor_row`, `capture.is_estimated`), the SQLite expressions
(`capture.family_default_sql`, `capture.ancestor_row_sql`,
`capture.estimated_sql`) and the Postgres expression in `supabase/reports.sql`
must agree on every input. The Postgres checks need a reachable server (psql +
PG* env) and skip cleanly otherwise.
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
# The view's whole `estimated` expression, evaluated verbatim against Postgres.
PG_ESTIMATED_RE = re.compile(
    r"(\(pr\.model_prefix OPERATOR\(pg_catalog\.~\) '\^claude-\[a-z\]\+-\$'"
    r".*?\)) AS estimated", re.S)

# Remainders R (model name minus the row's prefix) at the ancestor boundary.
R_ANCESTOR = ["-5", "-5-x", "-55", "-0", "-09", "-5-", "-55-", "-5-20260101",
              "-1-x"]
R_NOT_ANCESTOR = ["", "-555", "-20251001", "0514", "-a", "-", "--5", "-5a",
                  "-55a", "5", "-5\n", "-5 ", " -5", "-\u0665", "-\uff15",
                  "-5\u0665", "-a-5", "-555-1", "8"]
# (model name, prefix) pairs: a fixed prefix plus every R, the real-world
# shapes named in the contract, and NULLs.
PAIR_CASES = (
    [("claude-opus-5" + r, "claude-opus-5") for r in R_ANCESTOR + R_NOT_ANCESTOR]
    + [("claude-opus-5-5", "claude-opus-5"),                 # ancestor
       ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),     # date snapshot
       ("claude-opus-4-20250514", "claude-opus-4-2025"),      # R = 0514
       ("claude-sonnet-4-5", "claude-sonnet-4"),              # ancestor
       ("claude-sonnet-4-20250514", "claude-sonnet-4"),       # snapshot
       ("claude-sonnet-4-5-8", "claude-sonnet-4-5-"),         # R = 8
       ("claude-3-5-haiku-20241022", "claude-3-5-haiku"),     # legacy own
       ("claude-opus-5-5", "claude-opus-"),                   # family default
       ("claude-opus-5", "claude-opus-5"),                    # R empty: own
       ("claude-op", "claude-opus-5"),                        # name shorter
       ("claude-opus-5", None), (None, "claude-opus-5"), (None, None)])


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


def pair_sweep(n=3000, seed=140):
    """Deterministic random (name, prefix) pairs: prefix + random R drawn
    from the characters the ancestor check has to get right."""
    rnd = random.Random(seed)
    alphabet = "0159-a\u0665\n "
    out = []
    for _ in range(n):
        r = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 5)))
        prefix = rnd.choice(["claude-opus-5", "claude-opus-", "claude-3-5-haiku"])
        out.append((prefix + r, prefix))
    return out


def sqlite_eval_pairs(fn, pairs):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t(i INTEGER, n TEXT, p TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?, ?)",
                         [(i, n, p) for i, (n, p) in enumerate(pairs)])
        return [r[0] for r in conn.execute(
            f"SELECT {fn('n', 'p')} FROM t ORDER BY i")]
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


class TestAncestorAndEstimated(unittest.TestCase):
    def test_ancestor_matches_the_contract_regex(self):
        regex = re.compile(r"^-[0-9]{1,2}(-|$)")
        for n, p in PAIR_CASES + pair_sweep():
            if n is None or p is None:
                self.assertFalse(capture.is_ancestor_row(n, p))
                continue
            r = n[len(p):]
            # contract `$` = end of string (Python's `$` also matches before
            # a trailing newline, so compare with that case excluded)
            m = regex.match(r)
            expected = bool(m) and not (m.group(1) == "" and m.end() != len(r))
            self.assertEqual(capture.is_ancestor_row(n, p), expected,
                             (n, p))

    def test_named_ancestor_boundary(self):
        for r in R_ANCESTOR:
            self.assertTrue(capture.is_ancestor_row("claude-opus-5" + r,
                                                    "claude-opus-5"), repr(r))
        for r in R_NOT_ANCESTOR:
            self.assertFalse(capture.is_ancestor_row("claude-opus-5" + r,
                                                     "claude-opus-5"), repr(r))

    def test_estimated_is_family_default_or_ancestor(self):
        self.assertTrue(capture.is_estimated("claude-opus-5-5", "claude-opus-"))
        self.assertTrue(capture.is_estimated("claude-opus-5-5", "claude-opus-5"))
        self.assertFalse(capture.is_estimated("claude-haiku-4-5-20251001",
                                              "claude-haiku-4-5"))
        self.assertFalse(capture.is_estimated("claude-opus-4-20250514",
                                              "claude-opus-4-2025"))
        self.assertFalse(capture.is_estimated("claude-opus-5", "claude-opus-5"))
        self.assertFalse(capture.is_estimated("gpt-4o", None))

    def test_sqlite_ancestor_equivalent(self):
        pairs = PAIR_CASES + pair_sweep()
        got = sqlite_eval_pairs(capture.ancestor_row_sql, pairs)
        for (n, p), g in zip(pairs, got):
            if n is None or p is None:
                self.assertIsNone(g, (n, p))
            else:
                self.assertEqual(bool(g), capture.is_ancestor_row(n, p), (n, p))

    def test_sqlite_estimated_equivalent(self):
        pairs = PAIR_CASES + pair_sweep()
        got = sqlite_eval_pairs(capture.estimated_sql, pairs)
        for (n, p), g in zip(pairs, got):
            if p is None:
                self.assertIsNone(g, (n, p))       # unpriced stays NULL
            elif n is None:
                self.assertEqual(g, 1 if capture.is_family_default(p)
                                 else None, (n, p))
            else:
                self.assertEqual(bool(g), capture.is_estimated(n, p), (n, p))


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

    def test_view_estimated_expression_equivalent(self):
        # The view's `estimated` expression, verbatim, over (name, prefix)
        # pairs bound to the aliases it uses (`m.name`, `pr.model_prefix`).
        m = PG_ESTIMATED_RE.findall(REPORTS_SQL)
        self.assertEqual(len(m), 1)
        pairs = PAIR_CASES + pair_sweep(800)

        def lit(v):
            if v is None:
                return "NULL::text"
            return "U&'" + "".join(
                c if c.isascii() and (c.isalnum() or c in "-_ ")
                else "\\%04x" % ord(c) for c in v) + "'"
        rows = ",".join(f"({i}, {lit(n)}, {lit(p)})"
                        for i, (n, p) in enumerate(pairs))
        sql = ("SELECT json_agg(" + m[0] + " ORDER BY m.i) FROM (VALUES "
               + rows + ") AS m(i, name, model_prefix)"
               " CROSS JOIN LATERAL (SELECT m.model_prefix) AS pr(model_prefix);")
        r = subprocess.run(["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                            "-c", sql], capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0 and ("could not connect" in r.stderr
                                  or "connection to server" in r.stderr):
            self.skipTest(f"no reachable Postgres: {r.stderr.strip()}")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip())
        sq = sqlite_eval_pairs(capture.estimated_sql, pairs)
        for (n, p), g, s in zip(pairs, got, sq):
            if p is None or n is None:     # unpriced / no name: NULL-safe
                self.assertEqual(g, True if capture.is_family_default(p)
                                 else None, (n, p))
                self.assertEqual(s, 1 if capture.is_family_default(p)
                                 else None, (n, p))
                continue
            self.assertEqual(g, capture.is_estimated(n, p), (n, p))
            self.assertEqual(g, bool(s), (n, p))


if __name__ == "__main__":
    unittest.main()
