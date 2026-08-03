import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture


def entry(model="claude-sonnet-5", inp=100, out=50, cr=10, cw=5,
          side=False, ts="2026-07-17T10:00:00.000Z", typ="assistant"):
    return {
        "type": typ,
        "isSidechain": side,
        "timestamp": ts,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw,
        }},
    }


def write_jsonl(path, entries, trailing_partial=None):
    with open(path, "wb") as f:
        for e in entries:
            f.write(json.dumps(e).encode() + b"\n")
        if trailing_partial is not None:
            f.write(trailing_partial)


class TestReadNewEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_all_from_zero(self):
        write_jsonl(self.path, [entry(), entry()])
        entries, offset = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(offset, self.path.stat().st_size)

    def test_incremental_read_from_offset(self):
        write_jsonl(self.path, [entry()])
        _, offset1 = capture.read_new_entries(self.path, 0)
        with open(self.path, "ab") as f:
            f.write(json.dumps(entry(inp=7)).encode() + b"\n")
        entries, offset2 = capture.read_new_entries(self.path, offset1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"]["usage"]["input_tokens"], 7)
        self.assertEqual(offset2, self.path.stat().st_size)

    def test_partial_trailing_line_not_consumed(self):
        write_jsonl(self.path, [entry()], trailing_partial=b'{"type":"assist')
        entries, offset = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 1)
        # offset stops after the last complete line
        self.assertEqual(offset, self.path.stat().st_size - len(b'{"type":"assist'))

    def test_malformed_lines_skipped(self):
        with open(self.path, "wb") as f:
            f.write(b"not json at all\n")
            f.write(json.dumps(entry()).encode() + b"\n")
        entries, _ = capture.read_new_entries(self.path, 0)
        self.assertEqual(len(entries), 1)


class TestAggregate(unittest.TestCase):
    def test_sums_single_model(self):
        groups = capture.aggregate([entry(inp=100, out=50), entry(inp=10, out=5)])
        g = groups[("claude-sonnet-5", 0)]
        self.assertEqual(g["in"], 110)
        self.assertEqual(g["out"], 55)
        self.assertEqual(g["cr"], 20)
        self.assertEqual(g["cw"], 10)

    def test_groups_by_model_and_sidechain(self):
        groups = capture.aggregate([
            entry(model="claude-sonnet-5"),
            entry(model="claude-haiku-4-5", side=True),
        ])
        self.assertIn(("claude-sonnet-5", 0), groups)
        self.assertIn(("claude-haiku-4-5", 1), groups)

    def test_non_assistant_and_no_usage_skipped(self):
        no_usage = {"type": "assistant", "message": {"model": "m"}}
        groups = capture.aggregate([entry(typ="user"), no_usage])
        self.assertEqual(groups, {})

    def test_first_last_timestamps(self):
        groups = capture.aggregate([
            entry(ts="2026-07-17T10:00:00.000Z"),
            entry(ts="2026-07-17T10:00:30.000Z"),
        ])
        g = groups[("claude-sonnet-5", 0)]
        self.assertAlmostEqual(g["last"] - g["first"], 30.0)


if __name__ == "__main__":
    unittest.main()
