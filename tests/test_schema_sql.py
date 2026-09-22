"""Structural tests for the remote Postgres schema (AOS-104 P7). SECURITY-GATED.

Postgres RLS cannot run inside this stdlib/SQLite suite, so these tests assert
STRUCTURAL properties of ``supabase/schema.sql`` by parsing its text. They are a
tripwire against a policy silently losing its owner-scoping, an upsert target
drifting away from its unique constraint, or a bypass key sneaking in — NOT proof
of live enforcement. That a user cannot read another user's rows, and that an
anon request reads nothing, is verified by the MAINTAINER against a real
instance (developer handbook ``rls-remote-schema.md``).

What is asserted:
  * every owner-scoped table has ENABLE (and FORCE) ROW LEVEL SECURITY;
  * no policy is granted ``TO anon`` / ``TO public`` and none uses ``USING (true)``;
  * every policy is scoped ``TO authenticated`` and references ``auth.uid()``;
  * every table the client upserts has a UNIQUE/PRIMARY-KEY constraint matching
    the writer's ``on_conflict`` target (supabase_backend.UPSERT_ON_CONFLICT);
  * ``pricing`` carries its owner-scoped unique key and ``users`` its uuid PK;
  * no ``service_role`` / secret-key token appears anywhere in the file.
"""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import supabase_backend

SCHEMA_PATH = (pathlib.Path(__file__).resolve().parent.parent
               / "supabase" / "schema.sql")
SCHEMA = SCHEMA_PATH.read_text()
SCHEMA_LOWER = SCHEMA.lower()

# The owner-scoped tables that must all carry RLS. `users` is owner-scoped too
# (by its uuid == auth.uid()); cursors are deliberately absent (stay local).
RLS_TABLES = ("users", "projects", "models", "pricing", "sessions", "events")


def _table_body(table):
    """The text between the parens of ``CREATE TABLE ... <table> ( ... )`` —
    balanced-paren aware, so nested ``(...)`` inside a constraint is kept."""
    m = re.search(r"CREATE TABLE[^(]*\b" + table + r"\b[^(]*\(",
                  SCHEMA, re.IGNORECASE)
    assert m, f"no CREATE TABLE for {table}"
    depth = 0
    start = m.end() - 1
    for i in range(start, len(SCHEMA)):
        if SCHEMA[i] == "(":
            depth += 1
        elif SCHEMA[i] == ")":
            depth -= 1
            if depth == 0:
                return SCHEMA[start + 1:i]
    raise AssertionError(f"unbalanced parens in CREATE TABLE {table}")


def _colset(clause):
    """Parse a ``(a, b, c)`` column list into a frozenset of bare names."""
    return frozenset(c.strip() for c in clause.split(",") if c.strip())


def _key_colsets(table):
    """Every PRIMARY KEY / UNIQUE column set declared inside ``table``'s body,
    as a set of frozensets (order-independent). Handles ``UNIQUE NULLS NOT
    DISTINCT (...)`` and multi-line column lists."""
    body = _table_body(table)
    sets = set()
    # Table-level PRIMARY KEY (...) / UNIQUE [NULLS NOT DISTINCT] (...).
    for m in re.finditer(
            r"(?:PRIMARY\s+KEY|UNIQUE(?:\s+NULLS\s+NOT\s+DISTINCT)?)\s*\(([^)]*)\)",
            body, re.IGNORECASE):
        sets.add(_colset(m.group(1)))
    # Column-level inline PRIMARY KEY (e.g. `uuid uuid PRIMARY KEY,`) — the
    # single named column is the key. The negative lookahead excludes the
    # table-level form already handled above.
    for m in re.finditer(
            r"^\s*(\w+)\b[^,()\n]*\bPRIMARY\s+KEY\b(?!\s*\()",
            body, re.IGNORECASE | re.MULTILINE):
        sets.add(frozenset({m.group(1)}))
    return sets


def _policies():
    """Each ``CREATE POLICY ... ;`` statement text (whole statement)."""
    return re.findall(r"CREATE POLICY\b.*?;", SCHEMA, re.IGNORECASE | re.DOTALL)


class TestRowLevelSecurity(unittest.TestCase):
    def test_every_table_enables_and_forces_rls(self):
        for t in RLS_TABLES:
            self.assertRegex(
                SCHEMA, r"ALTER TABLE[^;]*\b" + t + r"\b[^;]*ENABLE ROW LEVEL SECURITY",
                f"{t} missing ENABLE ROW LEVEL SECURITY")
            self.assertRegex(
                SCHEMA, r"ALTER TABLE[^;]*\b" + t + r"\b[^;]*FORCE ROW LEVEL SECURITY",
                f"{t} missing FORCE ROW LEVEL SECURITY")

    def test_there_is_a_policy_per_table(self):
        policies = _policies()
        self.assertEqual(len(policies), len(RLS_TABLES),
                         "expected exactly one policy per owner-scoped table")
        for t in RLS_TABLES:
            self.assertTrue(
                any(re.search(r"\bON\b[^;]*\b" + t + r"\b", p, re.IGNORECASE)
                    for p in policies),
                f"no policy targets {t}")

    def test_no_policy_is_open_to_anon_or_public(self):
        for p in _policies():
            low = p.lower()
            self.assertNotIn("to anon", low, "policy is open TO anon")
            self.assertNotIn("to public", low, "policy is open TO public")
            self.assertNotIn("using (true)", low, "policy uses USING (true)")
            self.assertNotIn("using(true)", low, "policy uses USING(true)")

    def test_every_policy_is_authenticated_and_auth_uid_scoped(self):
        for p in _policies():
            low = p.lower()
            self.assertIn("to authenticated", low,
                          f"policy not scoped TO authenticated: {p[:60]}")
            self.assertIn("auth.uid()", low,
                          f"policy does not reference auth.uid(): {p[:60]}")

    def test_no_service_role_or_secret_key_anywhere(self):
        for forbidden in ("service_role", "sb_secret", "secret_key", "bypassrls"):
            self.assertNotIn(forbidden, SCHEMA_LOWER, forbidden)


class TestUniqueKeysMatchUpserts(unittest.TestCase):
    def test_each_upserted_table_has_a_matching_unique_key(self):
        # The writer's on_conflict target for each upserted table must equal a
        # UNIQUE/PRIMARY KEY column set declared on that table — so
        # Prefer:resolution=merge-duplicates is a real idempotent merge.
        for table, on_conflict in supabase_backend.UPSERT_ON_CONFLICT.items():
            target = _colset(on_conflict)
            self.assertIn(
                target, _key_colsets(table),
                f"{table}: on_conflict {sorted(target)} has no matching "
                f"UNIQUE/PK constraint in schema.sql "
                f"(declared: {[sorted(s) for s in _key_colsets(table)]})")

    def test_events_key_is_nulls_not_distinct(self):
        # Nullable event columns (agent, dur_ms, branch, ...) are part of the
        # key, so the constraint MUST treat NULLs as equal or a re-sent row with
        # a NULL would silently duplicate under default SQL semantics.
        body = _table_body("events")
        self.assertRegex(body, r"UNIQUE\s+NULLS\s+NOT\s+DISTINCT",
                         "events unique key must be NULLS NOT DISTINCT")

    def test_pricing_and_users_carry_their_keys(self):
        self.assertIn(
            _colset("owner_id, provider, model_prefix, model_version, effective_from"),
            _key_colsets("pricing"), "pricing missing its owner-scoped unique key")
        self.assertIn(_colset("uuid"), _key_colsets("users"),
                      "users missing its uuid primary key")

    def test_no_cursors_table(self):
        # Cursors are authoritative and LOCAL — they must never appear remotely.
        self.assertNotRegex(SCHEMA, r"CREATE TABLE[^(]*\bcursors\b")


class TestOwnerScoping(unittest.TestCase):
    def test_owner_scoped_tables_declare_owner_id_not_null(self):
        for t in ("projects", "models", "pricing", "sessions", "events"):
            body = _table_body(t)
            self.assertRegex(
                body, r"owner_id\s+uuid\s+NOT NULL",
                f"{t} must declare owner_id uuid NOT NULL")


if __name__ == "__main__":
    unittest.main()
