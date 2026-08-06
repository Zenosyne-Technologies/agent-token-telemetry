#!/usr/bin/env python3
"""Deterministic pricing refresh from Anthropic's published pricing page.

`pricing_update.py [--db PATH] [--html FILE]` fetches
https://platform.claude.com/docs/en/about-claude/pricing, parses the model
pricing table, diffs it against the `pricing` table and inserts effective-dated
rows per the insert-only contract (docs/TELEMETRY-CONTRACT.md), then prints a
finished markdown report. The command prompt runs this and echoes stdout
verbatim; the LLM flow is only the fallback when this exits non-zero (exit 2 =
fetch/parse failure — the page layout changed or the network is down).

Backend seam: DB work goes through capture.connect() (the schema owner);
parsing and planning are pure functions over plain data, reusable unchanged
when other database backends arrive.
"""
import argparse
import datetime
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture

URL = "https://platform.claude.com/docs/en/about-claude/pricing"
PROVIDER = "anthropic"
FAMILIES = ("fable", "mythos", "opus", "sonnet", "haiku")
# Model families whose API ids do not follow the claude-<family>-<version>
# scheme, or that need extra alias prefixes to match real model names.
SPECIAL_PREFIXES = {
    ("haiku", "3.5"): ["claude-3-5-haiku"],
    ("opus", "4"): ["claude-opus-4-0", "claude-opus-4-2025"],
}
RATE_KEYS = ("in_usd", "out_usd", "cache_r_usd", "cache_w_usd",
             "cache_w_1h_usd")


class TableCollector(HTMLParser):
    """Every <table> as a list of rows, each row a list of cell texts."""

    def __init__(self):
        super().__init__()
        self.tables, self._rows, self._row, self._cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def money(text):
    m = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", text)
    return float(m.group(1)) if m else None


def parse_models(html):
    """The model-pricing table -> ordered entries:
    {family, version, rates, condition: None|('through'|'starting', date)}."""
    tc = TableCollector()
    tc.feed(html)
    table = next((t for t in tc.tables
                  if t and any("Base Input Tokens" in c for c in t[0])), None)
    if table is None:
        raise ValueError("model pricing table not found on the page")
    header = table[0]
    col = {}
    for i, cell in enumerate(header):
        for key, needle in (("in", "Base Input"), ("w5", "5m Cache"),
                            ("w1h", "1h Cache"), ("cr", "Cache Hits"),
                            ("out", "Output")):
            if needle in cell:
                col[key] = i
    if set(col) != {"in", "w5", "w1h", "cr", "out"}:
        raise ValueError(f"unexpected pricing table header: {header}")

    entries = []
    for row in table[1:]:
        if len(row) <= max(col.values()):
            continue
        m = re.search(r"Claude\s+(Fable|Mythos|Opus|Sonnet|Haiku)"
                      r"\s+([0-9]+(?:\.[0-9]+)?)", row[0])
        if not m:
            continue
        condition = None
        dm = re.search(r"(through|starting)\s+([A-Z][a-z]+ [0-9]{1,2}, [0-9]{4})",
                       row[0])
        if dm:
            condition = (dm.group(1),
                         datetime.datetime.strptime(dm.group(2), "%B %d, %Y")
                         .date())
        rates = {"in_usd": money(row[col["in"]]),
                 "out_usd": money(row[col["out"]]),
                 "cache_r_usd": money(row[col["cr"]]),
                 "cache_w_usd": money(row[col["w5"]]),
                 "cache_w_1h_usd": money(row[col["w1h"]])}
        if any(v is None for v in rates.values()):
            raise ValueError(f"unparseable rate cell in row: {row[0]}")
        entries.append({"family": m.group(1).lower(), "version": m.group(2),
                        "rates": rates, "condition": condition})
    if not entries:
        raise ValueError("no model rows parsed from the pricing table")
    return entries


def specific_prefixes(family, version):
    return SPECIAL_PREFIXES.get((family, version),
                                [f"claude-{family}-{version.replace('.', '-')}"])


def build_candidates(entries, today):
    """Deterministic prefix plan:
    - `claude-<family>-` from each family's first (newest) unconditional row;
    - conditional rows always get their specific prefix, dated `through` ->
      today (rate in force now), `starting <d>` -> that date;
    - unconditional rows get a specific prefix only when their rates differ
      from the family rate (retired models on old pricing)."""
    today_epoch = int(datetime.datetime.combine(
        today, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())
    family_rates, candidates = {}, []

    def epoch(d):
        return int(datetime.datetime.combine(
            d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())

    for e in entries:
        fam, rates = e["family"], e["rates"]
        if e["condition"] is None and fam not in family_rates:
            family_rates[fam] = rates
            candidates.append({"prefix": f"claude-{fam}-", "rates": rates,
                               "effective_from": today_epoch})
    for e in entries:
        fam, rates = e["family"], e["rates"]
        if e["condition"] is not None:
            kind, date = e["condition"]
            eff = today_epoch if kind == "through" else epoch(date)
            for p in specific_prefixes(fam, e["version"]):
                candidates.append({"prefix": p, "rates": rates,
                                   "effective_from": eff})
        elif rates != family_rates.get(fam):
            for p in specific_prefixes(fam, e["version"]):
                candidates.append({"prefix": p, "rates": rates,
                                   "effective_from": today_epoch})
    # keep first occurrence per (prefix, effective_from)
    seen, out = set(), []
    for c in candidates:
        key = (c["prefix"], c["effective_from"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def plan(conn, candidates):
    """Attach a status to every candidate; only some statuses insert."""
    for c in candidates:
        row = conn.execute(
            "SELECT in_usd, out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,"
            " effective_from FROM pricing"
            " WHERE provider=? AND model_prefix=? AND effective_from<=?"
            " ORDER BY effective_from DESC LIMIT 1",
            (PROVIDER, c["prefix"], c["effective_from"])).fetchone()
        if row is None:
            c["status"] = "new"
        elif row[5] == 0:
            c["status"] = "seed replaced"
        else:
            old = dict(zip(RATE_KEYS, row[:5]))
            if old == c["rates"]:
                c["status"] = "unchanged"
            elif old["cache_w_1h_usd"] is None and {
                    k: v for k, v in old.items() if k != "cache_w_1h_usd"} == {
                    k: v for k, v in c["rates"].items() if k != "cache_w_1h_usd"}:
                c["status"] = "1h rate added"
            else:
                c["status"] = "updated"
                c["old"] = old
    return candidates


def apply(conn, candidates, source):
    inserted = 0
    with conn:
        for c in candidates:
            if c["status"] == "unchanged":
                continue
            r = c["rates"]
            cur = conn.execute(
                "INSERT OR IGNORE INTO pricing(provider, model_prefix,"
                " in_usd, out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,"
                " effective_from, source) VALUES (?,?,?,?,?,?,?,?,?)",
                (PROVIDER, c["prefix"], r["in_usd"], r["out_usd"],
                 r["cache_r_usd"], r["cache_w_usd"], r["cache_w_1h_usd"],
                 c["effective_from"], source))
            if cur.rowcount:
                inserted += 1
            else:
                c["status"] = "already recorded at this date"
    return inserted


def unpriced_models(conn):
    return [name for (name,) in conn.execute(
        "SELECT name FROM models WHERE NOT EXISTS (SELECT 1 FROM pricing"
        " WHERE name LIKE model_prefix || '%')").fetchall()]


def fmt_rates(r):
    def n(v):
        return f"{v:g}" if v is not None else "—"
    return (f"{n(r['in_usd'])} / {n(r['out_usd'])} / {n(r['cache_r_usd'])}"
            f" / {n(r['cache_w_usd'])} / {n(r['cache_w_1h_usd'])}")


def render(candidates, inserted, unpriced, today):
    out = ["| model prefix | in / out / cache-read / 5m-write / 1h-write"
           " (USD per MTok) | effective | status |", "|---|---|---|---|"]
    for c in candidates:
        eff = datetime.datetime.fromtimestamp(
            c["effective_from"], tz=datetime.timezone.utc).date().isoformat()
        status = c["status"]
        if status == "updated":
            status += f" (was {fmt_rates(c['old'])})"
        out.append(f"| `{c['prefix']}` | {fmt_rates(c['rates'])} |"
                   f" {eff} | {status} |")
    for name in unpriced:
        out.append(f"| `{name}` (in models table) | no published rate —"
                   " not fabricated | — | unpriced |")
    out += ["", f"Source: {URL} — checked {today.isoformat()},"
            f" {inserted} row(s) inserted (history is insert-only; existing"
            " rows are never modified)."]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pricing_update.py")
    ap.add_argument("--db", default=None)
    ap.add_argument("--html", default=None,
                    help="parse a local HTML file instead of fetching (tests)")
    args = ap.parse_args(argv)
    db = args.db or capture.db_path()
    if not Path(db).exists():
        print("No telemetry DB yet — nothing to update. Enable capture with"
              " `/token-telemetry:enable` first.")
        return 0
    try:
        if args.html:
            html = Path(args.html).read_text(errors="replace")
        else:
            req = urllib.request.Request(
                URL, headers={"User-Agent": "token-telemetry-pricing-update"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        entries = parse_models(html)
    except Exception as exc:  # noqa: BLE001 - any failure -> LLM fallback
        print(f"pricing page fetch/parse failed: {exc}", file=sys.stderr)
        return 2
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    conn = capture.connect(db)
    try:
        candidates = plan(conn, build_candidates(entries, today))
        inserted = apply(conn, candidates, URL)
        print(render(candidates, inserted, unpriced_models(conn), today))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
