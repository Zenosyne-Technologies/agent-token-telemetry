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
    # Header text is matched case-insensitively: the published page has shipped
    # both title case ("Base Input Tokens") and sentence case ("Base input
    # tokens") for the same columns, and a case-sensitive match silently failed
    # to find the table (exit 2) when the casing changed.
    table = next((t for t in tc.tables
                  if t and any("base input tokens" in c.lower()
                               for c in t[0])), None)
    if table is None:
        raise ValueError("model pricing table not found on the page")
    header = table[0]
    col = {}
    for i, cell in enumerate(header):
        cell_l = cell.lower()
        for key, needle in (("in", "base input"), ("w5", "5m cache"),
                            ("w1h", "1h cache"), ("cr", "cache hits"),
                            ("out", "output")):
            if needle in cell_l:
                col[key] = i
    # The guard still fires on a genuinely-absent column: every needle must have
    # matched some header cell, else an index is missing and we refuse rather
    # than map a wrong column.
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


def _epoch(d):
    """UTC midnight of date ``d`` as a unix timestamp."""
    return int(datetime.datetime.combine(
        d, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())


def in_force(condition, today):
    """Whether a page row's condition makes its rate the one charged ``today``.

    ``through <d>`` (an introductory rate) is in force while ``d >= today`` and
    EXPIRED once ``d < today``; ``starting <d>`` (a scheduled increase) is in
    force once ``d <= today``. An unconditional row (``None``) is never an
    in-force *conditional* — see :func:`build_candidates` for when its rate is
    minted.

    :param condition: ``None`` or ``('through'|'starting', datetime.date)``.
    :param today: the run date (UTC).
    :returns: ``True`` for an in-force conditional, else ``False``.
    """
    if condition is None:
        return False
    kind, date = condition
    return date >= today if kind == "through" else date <= today


def build_candidates(entries, today):
    """Deterministic prefix plan — mint only the rate IN FORCE today, per
    listed version (docs/TELEMETRY-CONTRACT.md §Pricing table):

    - an in-force ``through <d>`` intro rate (``d >= today``) gets the
      version's specific prefix(es) dated today; an EXPIRED one (``d < today``)
      mints nothing — a stale page footnote never re-asserts an intro rate;
    - an in-force ``starting <d>`` increase (``d <= today``) is dated ``d``; a
      future one mints nothing (a forecast is not a recorded charge);
    - an UNCONDITIONAL row's rate is minted, dated today, ONLY IF its version
      has no in-force conditional — otherwise a pre-increase or post-intro
      rate dated today would override the rate actually charged. Every listed
      version therefore gets its own specific prefix(es) (``claude-<family>-
      <version>`` or the legacy aliases), the family's newest one included;
    - ``claude-<family>-`` — the FAMILY DEFAULT row, the fallback for models
      of that family the page does not list — dated today at the in-force
      rate of the family's NEWEST version (its first-listed one: the page
      lists newest first), whether that version is listed unconditionally or
      only conditionally — the rate its own specific prefix resolves to from
      today on, by the same in-force rule as above. A family whose only
      listings are conditional still gets its family row. If the newest
      version has no in-force rate (only an expired intro, see
      :func:`stale_intros`), no family row is minted either — the family
      default keeps its last recorded rate, like the version itself.

    Ordering: on a ``(prefix, effective_from)`` collision the first candidate
    wins. In-force conditionals are listed before unconditional rows (and an
    in-force conditional suppresses the version's unconditional rate anyway),
    so the in-force rate wins regardless of page order; among several
    unconditional rows for one version (e.g. a long-context row) the FIRST
    listed wins.

    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :returns: candidate dicts ``{prefix, rates, effective_from}``, family
        rows first.
    """
    today_epoch = _epoch(today)

    def version(e):
        return (e["family"], e["version"])

    conditioned = {version(e) for e in entries
                   if in_force(e["condition"], today)}
    specific = []
    for e in entries:
        if not in_force(e["condition"], today):
            continue
        kind, date = e["condition"]
        eff = today_epoch if kind == "through" else _epoch(date)
        for p in specific_prefixes(e["family"], e["version"]):
            specific.append({"prefix": p, "rates": e["rates"],
                             "effective_from": eff})
    for e in entries:
        if e["condition"] is not None or version(e) in conditioned:
            continue
        for p in specific_prefixes(e["family"], e["version"]):
            specific.append({"prefix": p, "rates": e["rates"],
                             "effective_from": today_epoch})
    # keep first occurrence per (prefix, effective_from)
    seen, deduped = set(), []
    for c in specific:
        key = (c["prefix"], c["effective_from"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    # family default rows: the in-force rate of the family's newest version
    # (first listed; conditional-only listings count) — the row its own first
    # prefix resolves to from today on (greatest effective_from, first on a
    # tie). No in-force rate for the newest version -> no family row.
    newest = {}
    for e in entries:
        newest.setdefault(e["family"], e["version"])
    family_rows = []
    for fam, ver in newest.items():
        own = specific_prefixes(fam, ver)[0]
        rows = [c for c in deduped if c["prefix"] == own]
        if rows:
            best = max(rows, key=lambda c: c["effective_from"])
            family_rows.append({"prefix": f"claude-{fam}-",
                                "rates": best["rates"],
                                "effective_from": today_epoch})
    return family_rows + deduped


def stale_intros(entries, today):
    """Versions whose only rate on the page is an EXPIRED intro.

    A version listed with a ``through <d>`` intro rate where ``d < today`` and
    with no other rate in force today (no unconditional row, no in-force
    ``starting``/``through`` row) has no known in-force rate: nothing is
    minted for it — nor for its family default when it is the family's newest
    version (:func:`build_candidates`) — so its events keep the last
    recorded rate until the page publishes a post-intro rate. The run report
    names each such version in a STALE-PRICE WARNING line.

    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :returns: ``[(family, version, intro_end_date)]`` in page order, the
        latest expired intro end date per version.
    """
    priced, expired = set(), {}
    for e in entries:
        key = (e["family"], e["version"])
        cond = e["condition"]
        if cond is None or in_force(cond, today):
            priced.add(key)
        elif cond[0] == "through":
            expired[key] = max(expired.get(key, cond[1]), cond[1])
    return [(fam, ver, end) for (fam, ver), end in expired.items()
            if (fam, ver) not in priced]


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


def render(candidates, inserted, unpriced, today, stale=()):
    """The finished markdown run report.

    :param candidates: planned+applied candidates (:func:`plan`,
        :func:`apply`).
    :param inserted: number of rows inserted.
    :param unpriced: model names matching no pricing prefix.
    :param today: the run date (UTC).
    :param stale: :func:`stale_intros` output — one STALE-PRICE WARNING line
        per version whose only listed rate is an expired intro.
    :returns: the report text.
    """
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
    for fam, ver, end in stale:
        out += ["", f"STALE-PRICE WARNING: Claude {fam.capitalize()} {ver}"
                f" (`{specific_prefixes(fam, ver)[0]}`) — its introductory"
                f" rate ended {end.isoformat()} and the page lists no rate in"
                " force after it; nothing was minted, so events for it keep"
                " the last recorded rate until the page publishes a"
                " post-intro rate."]
    out += ["", f"Source: {URL} — checked {today.isoformat()},"
            f" {inserted} row(s) inserted (history is insert-only; existing"
            " rows are never modified)."]
    return "\n".join(out)


def run_update(conn, entries, today, source=URL):
    """Plan, apply and report one pricing refresh of parsed page ``entries``.

    :param conn: an open telemetry DB connection (:func:`capture.connect`).
    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :param source: the ``source`` column value for inserted rows.
    :returns: the finished markdown run report (:func:`render`), including a
        STALE-PRICE WARNING line per :func:`stale_intros` version.
    """
    candidates = plan(conn, build_candidates(entries, today))
    inserted = apply(conn, candidates, source)
    return render(candidates, inserted, unpriced_models(conn), today,
                  stale_intros(entries, today))


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
        print(run_update(conn, entries, today))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
