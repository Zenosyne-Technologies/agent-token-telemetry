#!/usr/bin/env python3
"""Deterministic pricing refresh from Anthropic's published pricing page.

`pricing_update.py [--db PATH] [--html FILE]` fetches
https://platform.claude.com/docs/en/about-claude/pricing, parses the model
pricing table, diffs it against the `pricing` table and inserts effective-dated
rows per the insert-only contract (docs/TELEMETRY-CONTRACT.md), then prints a
finished markdown report. The command prompt runs this and echoes stdout
verbatim; the LLM flow is only the fallback when this exits non-zero (exit 2 =
fetch/parse failure — the page layout changed or the network is down).

`--backfill-plan [--json]` prints the read-only consent-gated backfill plan
(estimated events that a copy of their model's own, later-minted rate would
re-price) and `--backfill-apply PREFIX...` applies the user-confirmed prefixes
— contract §Pricing table, "Third narrow case" (consent-gated backfill). Neither
fetches the page.

Backend seam: DB work goes through capture.connect() (the schema owner);
parsing and planning are pure functions over plain data, reusable unchanged
when other database backends arrive.
"""
import argparse
import datetime
import json
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


# ------------------------------------------------------------------ backfill
#
# Consent-gated backfill (docs/TELEMETRY-CONTRACT.md §Pricing table, "Third
# narrow case"). When a model's events were priced at an ESTIMATE
# (a family default or ancestor row) before its own row was first minted, the
# plan offers ONE extra INSERT per such prefix: a copy of the prefix's earliest
# own row R0, dated the UTC start of the day of the earliest estimated event it
# would own-price. The plan is read-only and computes each candidate's impact
# set by actually resolving events with and without the hypothetical row
# (through a TEMP shadow copy of `pricing`, never by assumption); the apply
# re-plans, inserts inside one transaction, verifies, and rolls back on any
# surprise. Applying is the user's explicit decision in the interactive
# command flow, never this script's.

DAY = 86400
_BACKFILL_SRC_RE = re.compile(r"^backfill:(.*); confirmed \d{4}-\d{2}-\d{2}$",
                              re.S)
_EPS = 1e-9


def _day_start(ts):
    """UTC midnight (unix seconds) of the day containing ``ts``."""
    return int(ts) - int(ts) % DAY


def _iso(ts):
    """``ts`` (unix seconds) as a UTC ``YYYY-MM-DD`` date string."""
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc).date().isoformat()


def human_span(first, last):
    """Human length of the inclusive UTC-date window ``first``..``last``.

    :param first: ``datetime.date`` of the first impacted event.
    :param last: ``datetime.date`` of the last impacted event.
    :returns: ``"1 day"``, ``"9 days"``, ``"3 weeks"`` or ``"2 months"`` —
        days under two weeks, weeks under ~two months, months beyond.
    """
    days = (last - first).days + 1
    if days < 14:
        return f"{days} day" + ("s" if days != 1 else "")
    if days < 60:
        return f"{round(days / 7)} weeks"
    return f"{round(days / 30)} months"


def _cost(tok, rate):
    """USD cost of one event's tokens at one pricing row — the report's
    formula (report.fetch_project_stats): missing rates price as 0, the 1h
    cache-write portion falls back to the 5m rate when the row predates the
    split. ``rate`` ``None`` (unpriced) costs 0."""
    if rate is None:
        return 0.0
    in_tok, out_tok, cr, cw, cw1h = tok

    def r(k):
        return rate[k] or 0.0
    w1h = rate["cache_w_1h_usd"]
    w1h = w1h if w1h is not None else r("cache_w_usd")
    return (in_tok * r("in_usd") + out_tok * r("out_usd") + cr * r("cache_r_usd")
            + (cw - cw1h) * r("cache_w_usd") + cw1h * w1h) / 1_000_000.0


class _Shadow:
    """Event resolution against the ``pricing`` table, with and without
    hypothetical rows, on one connection.

    It creates ``temp.pricing`` — a copy of ``main.pricing`` whose ``_rid``
    column is the real row's rowid. SQLite resolves an unqualified ``pricing``
    to the TEMP schema first, so the production resolver
    (``report.resolved_subquery``, unchanged) runs against the copy;
    hypothetical rows get negative ``_rid``s and live only in the copy. The
    real table is never touched. :meth:`close` drops the copy.
    """

    def __init__(self, conn):
        self.conn = conn
        pcols = {r[1] for r in conn.execute("PRAGMA main.table_info(pricing)")}
        ecols = {r[1] for r in conn.execute("PRAGMA main.table_info(events)")}
        w1h = "cache_w_1h_usd" if "cache_w_1h_usd" in pcols else "NULL"
        self.ev_w1h = "e.cache_w_1h" if "cache_w_1h" in ecols else "0"
        conn.execute("DROP TABLE IF EXISTS temp.pricing")
        conn.execute(
            "CREATE TEMP TABLE pricing AS SELECT rowid AS _rid, provider,"
            " model_prefix, model_version, in_usd, out_usd, cache_r_usd,"
            f" cache_w_usd, {w1h} AS cache_w_1h_usd, effective_from, source"
            " FROM main.pricing")
        self.rows = {}
        for r in conn.execute(
                "SELECT _rid, provider, model_prefix, model_version, in_usd,"
                " out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd,"
                " effective_from, source FROM temp.pricing"):
            self.rows[r[0]] = self._row(r[1:])
        self.events, self.baseline = {}, {}
        for rowid, name, ts, tok, rid in self._resolve(None, with_events=True):
            self.events[rowid] = (name, ts, tok)
            self.baseline[rowid] = rid

    @staticmethod
    def _row(vals):
        keys = ("provider", "model_prefix", "model_version") + RATE_KEYS + (
            "effective_from", "source")
        return dict(zip(keys, vals))

    def _resolve(self, before, with_events=False, expr="pr._rid"):
        import report  # the one resolver definition; imported lazily
        sql = ("SELECT e.rowid, m.name, e.ts, e.in_tok, e.out_tok, e.cache_r,"
               f" e.cache_w, {self.ev_w1h}, {report.resolved_subquery(expr)}"
               " FROM events e JOIN models m ON m.id = e.model_id")
        args = ()
        if before is not None:
            sql += " WHERE e.ts < ?"
            args = (before,)
        for r in self.conn.execute(sql, args):
            if with_events:
                yield r[0], r[1], r[2], tuple(v or 0 for v in r[3:8]), r[8]
            else:
                yield r[0], r[8]

    def resolve(self, before=None):
        """``{event rowid: resolved _rid or None}`` for events with
        ``ts < before`` (all events when ``before`` is None)."""
        return dict(self._resolve(before))

    def resolve_main(self):
        """``{event rowid: resolved main.pricing rowid or None}`` against the
        REAL table — only meaningful once the copy is dropped."""
        return dict(self._resolve(None, expr="pr.rowid"))

    def add(self, rid, row):
        """Insert hypothetical ``row`` into the copy under ``_rid = rid``."""
        self.conn.execute(
            "INSERT INTO temp.pricing(_rid, provider, model_prefix,"
            " model_version, in_usd, out_usd, cache_r_usd, cache_w_usd,"
            " cache_w_1h_usd, effective_from, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, row["provider"], row["model_prefix"], row["model_version"],
             *(row[k] for k in RATE_KEYS), row["effective_from"],
             row["source"]))

    def remove_hypothetical(self):
        self.conn.execute("DELETE FROM temp.pricing WHERE _rid < 0")

    def rate(self, rid, hypo=None):
        return (hypo or {}).get(rid) or self.rows.get(rid)

    def close(self):
        self.conn.execute("DROP TABLE IF EXISTS temp.pricing")


def _original_source(source):
    """R0's provenance, unwrapped if R0 is itself an earlier backfill row."""
    m = _BACKFILL_SRC_RE.match(source or "")
    return m.group(1) if m else (source or "")


def _plan_candidates(sh, today):
    """Every backfill candidate over the shadow ``sh`` — see
    :func:`backfill_plan` for the rules. Leaves no hypothetical row behind."""
    by_prefix = {}
    for rid, row in sh.rows.items():
        cur = by_prefix.get(row["model_prefix"])
        if cur is None or (row["effective_from"], rid) < (
                sh.rows[cur]["effective_from"], cur):
            by_prefix[row["model_prefix"]] = rid
    out = []
    for prefix in sorted(by_prefix):
        r0_rid = by_prefix[prefix]
        r0 = sh.rows[r0_rid]
        if capture.is_family_default(prefix) or r0["effective_from"] <= 0:
            continue
        # events P prefixes, by the resolver's own LIKE semantics
        matched = {rid for (rid,) in sh.conn.execute(
            "SELECT e.rowid FROM events e JOIN models m ON m.id = e.model_id"
            " WHERE e.ts < ? AND m.name LIKE ? || '%'",
            (r0["effective_from"], prefix))}
        triggers = []
        for ev in matched:
            name, ts, _tok = sh.events[ev]
            base = sh.baseline.get(ev)
            if (base is not None and not capture.is_estimated(name, prefix)
                    and capture.is_estimated(
                        name, sh.rows[base]["model_prefix"])):
                triggers.append(ts)
        if not triggers:
            continue
        start = _day_start(min(triggers))
        hypo = dict(r0, effective_from=start,
                    source=f"backfill:{_original_source(r0['source'])};"
                           f" confirmed {today.isoformat()}")
        sh.add(-1, hypo)
        try:
            after = sh.resolve(before=r0["effective_from"])
        finally:
            sh.remove_hypothetical()
        changed = {ev: rid for ev, rid in after.items()
                   if rid != sh.baseline.get(ev)}
        if not changed:
            continue
        cand = {"prefix": prefix, "provider": r0["provider"],
                "model_version": r0["model_version"],
                "r0": {"effective_from": _iso(r0["effective_from"]),
                       "source": r0["source"],
                       "rates": {k: r0[k] for k in RATE_KEYS}},
                "row": hypo, "backfill_from": _iso(start),
                "impact": sorted(changed)}
        own, unpriced, stray, models = {}, {}, {}, {}
        for ev, rid in changed.items():
            name, ts, tok = sh.events[ev]
            base = sh.baseline.get(ev)
            if rid != -1:
                stray[name] = stray.get(name, 0) + 1
            if base is None:
                unpriced[name] = unpriced.get(name, 0) + 1
            elif not capture.is_estimated(
                    name, sh.rows[base]["model_prefix"]):
                own[name] = own.get(name, 0) + 1
            m = models.setdefault(name, {"model": name, "events": 0,
                                         "cost_now": 0.0, "cost_after": 0.0,
                                         "first": ts, "last": ts})
            m["events"] += 1
            m["cost_now"] += _cost(tok, sh.rows.get(base))
            m["cost_after"] += _cost(tok, hypo)
            m["first"], m["last"] = min(m["first"], ts), max(m["last"], ts)
        first = min(m["first"] for m in models.values())
        last = max(m["last"] for m in models.values())
        mlist = []
        for m in sorted(models.values(), key=lambda m: m["model"]):
            m["delta"] = m["cost_after"] - m["cost_now"]
            m["first"], m["last"] = _iso(m["first"]), _iso(m["last"])
            mlist.append(m)
        cand.update({
            "window": {"first": _iso(first), "last": _iso(last),
                       "span": human_span(
                           datetime.date.fromisoformat(_iso(first)),
                           datetime.date.fromisoformat(_iso(last)))},
            "models": mlist,
            "events": sum(m["events"] for m in mlist),
            "cost_now": sum(m["cost_now"] for m in mlist),
            "cost_after": sum(m["cost_after"] for m in mlist)})
        cand["delta"] = cand["cost_after"] - cand["cost_now"]
        reasons = []
        if own:
            reasons.append("would re-price events already priced by their"
                           " own row: " + ", ".join(
                               f"{k} ({v})" for k, v in sorted(own.items())))
        if unpriced:
            reasons.append("would price previously unpriced events: "
                           + ", ".join(f"{k} ({v})"
                                       for k, v in sorted(unpriced.items())))
        if stray:
            reasons.append("events would resolve to a row other than the"
                           " backfill row: " + ", ".join(
                               f"{k} ({v})" for k, v in sorted(stray.items())))
        cand["refused"] = "; ".join(reasons) or None
        out.append(cand)
    for c in out:
        imp = set(c["impact"])
        c["overlaps"] = [o["prefix"] for o in out
                         if o is not c and imp & set(o["impact"])]
    return out


def _group(cands):
    """Split candidates into (offered, confirm_only, refused)."""
    offered, zero, refused = [], [], []
    for c in cands:
        if c["refused"]:
            refused.append(c)
        elif all(abs(m["delta"]) < _EPS for m in c["models"]):
            zero.append(c)
        else:
            offered.append(c)
    return offered, zero, refused


def backfill_plan(conn, today):
    """The read-only backfill plan over the local central DB.

    A candidate is a non-family-default pricing prefix P whose EARLIEST row R0
    was preceded (``events.ts < R0.effective_from``) by ESTIMATED events
    (``capture.is_estimated`` of their currently resolved row) of models for
    which P is their OWN row (P prefixes the name and is not an ancestor row
    for it). The hypothetical backfill row is R0's rates dated the UTC start
    of the day of the earliest such event. The IMPACT SET is every event
    whose resolved row changes when that row is added — computed by resolving
    every event before R0 with and without it — and may include other models
    sharing the prefix (e.g. an unlisted successor under a predecessor's
    row), listed under their own names. A candidate whose impact set holds an
    event priced by an OWN (non-estimated) row, or an unpriced event, is
    REFUSED. Nothing is written: the hypothetical row lives in a TEMP copy.

    :param conn: a connection to the telemetry DB (read-only is enough).
    :param today: the run date (UTC) — stamped into the would-be ``source``.
    :returns: ``{"candidates": [...], "confirm_only": [...],
        "refused": [...]}`` — each entry a dict with ``prefix``, ``provider``,
        ``model_version``, ``r0`` (effective_from, source, rates),
        ``backfill_from``, ``window`` (first, last, span), ``models`` (per
        model: events, cost_now, cost_after, delta, first, last), ``events``,
        ``cost_now``, ``cost_after``, ``delta``, ``overlaps`` (other
        candidates sharing impacted events), ``refused`` (reason or None),
        ``impact`` (event rowids) and ``row`` (the would-be pricing row).
    """
    saved = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN")   # one read snapshot for the whole plan
    try:
        sh = _Shadow(conn)
        offered, zero, refused = _group(_plan_candidates(sh, today))
    finally:
        conn.execute("ROLLBACK")   # also discards the TEMP copy
        conn.isolation_level = saved
    return {"candidates": offered, "confirm_only": zero, "refused": refused}


def _usd(v, signed=False):
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else (
        "-" if v < 0 else "")
    a = abs(v)
    body = f"{a:,.2f}" if a >= 0.01 or a == 0 else f"{a:.4f}"
    return f"{sign}${body}"


def _cand_rows(cands, zero=False):
    out = []
    for c in cands:
        per = "<br>".join(
            f"`{m['model']}`: {m['events']:,}"
            + ("" if zero else f" ({_usd(m['cost_now'])} → "
               f"{_usd(m['cost_after'])}, {_usd(m['delta'], True)})")
            for m in c["models"])
        w = c["window"]
        window = (w["first"] if w["first"] == w["last"]
                  else f"{w['first']} → {w['last']}") + f" ({w['span']})"
        rate = fmt_rates(c["r0"]["rates"])
        note = (f" · overlaps {', '.join(f'`{o}`' for o in c['overlaps'])}"
                if c["overlaps"] else "")
        if zero:
            out.append(f"| `{c['prefix']}` | {window} | {per} |"
                       f" {rate} (own, {c['r0']['effective_from']}) |"
                       f" {c['backfill_from']}{note} |")
        else:
            out.append(f"| `{c['prefix']}` | {window} | {per} |"
                       f" {_usd(c['cost_now'])} → {_usd(c['cost_after'])} |"
                       f" **{_usd(c['delta'], True)}** |"
                       f" {rate} (own, {c['r0']['effective_from']}) |"
                       f" {c['backfill_from']}{note} |")
    return out


def render_backfill_plan(plan_):
    """The markdown rendering of :func:`backfill_plan` output."""
    offered, zero, refused = (plan_["candidates"], plan_["confirm_only"],
                              plan_["refused"])
    if not (offered or zero or refused):
        return ("No backfill candidates: no model has estimated events before"
                " its own pricing row.")
    out = []
    n = len(offered) + len(zero)
    if n:
        out.append(f"Backfill available — {n} candidate prefix(es). Each would"
                   " INSERT one row (the prefix's earliest own rate, dated the"
                   " backfill date) re-pricing ONLY the estimated events"
                   " listed; nothing is written without explicit consent"
                   " (`--backfill-apply <prefix> ...`).")
    if offered:
        out += ["", "| prefix | window (span) | events per model"
                " (cost now → after, delta) | cost now → after | delta |"
                " rate copied (in / out / cache-read / 5m-write / 1h-write)"
                " | backfill from |", "|---|---|---|---|---|---|---|"]
        out += _cand_rows(offered)
        total = sum(c["delta"] for c in offered)
        out += ["", f"Total delta if every candidate above is applied"
                f" independently: {_usd(total, True)}."]
    if zero:
        out += ["", "Confirm only — no cost change (the own rate equals the"
                " estimate these events were priced at):", "",
                "| prefix | window (span) | events per model |"
                " rate copied | backfill from |", "|---|---|---|---|---|"]
        out += _cand_rows(zero, zero=True)
    if refused:
        out += ["", "Refused — not offered (a backfill would re-price events"
                " that are not estimates):", ""]
        for c in refused:
            out.append(f"- `{c['prefix']}` (window {c['window']['first']} →"
                       f" {c['window']['last']}): {c['refused']}")
    if any(c["overlaps"] for c in offered + zero):
        out += ["", "Overlapping candidates share impacted events; each row"
                " above is computed on its own. Applying several together"
                " prices a shared event at the longest matching backfill"
                " row — the apply report shows the combined result."]
    return "\n".join(out)


def _json_plan(plan_):
    def strip(c):
        return {k: v for k, v in c.items() if k not in ("impact", "row")} | {
            "impact_events": len(c["impact"])}
    return {k: [strip(c) for c in v] for k, v in plan_.items()}


class BackfillRefused(Exception):
    """An apply that wrote nothing; ``args[0]`` is the report text."""


def backfill_apply(conn, prefixes, today):
    """Re-plan and apply the backfill for ``prefixes`` — all or nothing.

    Inside ONE ``BEGIN IMMEDIATE`` transaction: re-compute the plan (a stale
    one is never trusted); refuse the whole apply, writing nothing, if any
    named prefix is not a current non-refused candidate (a prefix that is
    already backfilled — no longer a candidate and holding a ``backfill:``
    row — is a no-op, so a re-run is idempotent); check the COMBINED
    hypothetical re-prices exactly the union of the named impact sets and
    only estimated events; ``INSERT OR IGNORE`` one row per prefix (R0's
    rates, ``effective_from`` = the backfill date, ``source`` =
    ``backfill:<R0 source>; confirmed <today>``); then verify every event
    against the real table — each impacted event resolves to its predicted
    new row, no other event changed — and ROLL BACK on any mismatch. Never
    UPDATEs or DELETEs a pricing row.

    :param conn: a read-write connection (:func:`capture.connect`).
    :param prefixes: the pricing prefixes the user confirmed.
    :param today: the run date (UTC).
    :returns: the markdown apply report.
    :raises BackfillRefused: nothing was written (invalid prefix, interaction
        or verification failure); the message is the report.
    """
    names = list(dict.fromkeys(prefixes))
    saved = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    sh = None
    try:
        sh = _Shadow(conn)
        cands = {c["prefix"]: c for c in _plan_candidates(sh, today)}
        chosen, noop, bad = [], [], []
        for p in names:
            c = cands.get(p)
            if c is not None and not c["refused"]:
                chosen.append(c)
            elif c is None and conn.execute(
                    "SELECT 1 FROM main.pricing WHERE model_prefix = ?"
                    " AND source LIKE 'backfill:%'", (p,)).fetchone():
                noop.append(p)
            else:
                bad.append(f"`{p}`: " + (f"refused — {c['refused']}" if c
                                         else "not a backfill candidate"))
        if bad:
            raise BackfillRefused(
                "Backfill REFUSED — nothing written (all-or-nothing):\n"
                + "\n".join(f"- {b}" for b in bad))
        # combined hypothetical: all chosen rows at once
        hypo = {}
        for i, c in enumerate(chosen):
            hypo[-(i + 1)] = c["row"]
            sh.add(-(i + 1), c["row"])
        comb = {ev: rid for ev, rid in sh.resolve().items()
                if rid != sh.baseline.get(ev)}
        sh.remove_hypothetical()
        union = set().union(*(c["impact"] for c in chosen)) if chosen else set()
        if set(comb) != union or any(rid not in hypo for rid in comb.values()):
            raise BackfillRefused(
                "Backfill REFUSED — nothing written: applied together, the"
                " named rows would re-price a different event set than"
                " their plans.")
        sh.close()
        real = {}
        for rid, row in hypo.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO main.pricing(provider, model_prefix,"
                " model_version, in_usd, out_usd, cache_r_usd, cache_w_usd,"
                " cache_w_1h_usd, effective_from, source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row["provider"], row["model_prefix"], row["model_version"],
                 *(row[k] for k in RATE_KEYS), row["effective_from"],
                 row["source"]))
            if cur.rowcount != 1:
                raise BackfillRefused(
                    f"Backfill REFUSED — nothing written: a row for"
                    f" `{row['model_prefix']}` at {_iso(row['effective_from'])}"
                    " already exists.")
            real[rid] = cur.lastrowid
        # verify against the REAL table
        actual = sh.resolve_main()
        expected = dict(sh.baseline)
        expected.update({ev: real[rid] for ev, rid in comb.items()})
        wrong = [ev for ev in actual if actual[ev] != expected.get(ev)]
        if wrong or set(actual) != set(expected):
            raise BackfillRefused(
                "Backfill ROLLED BACK — verification failed:"
                f" {len(wrong)} event(s) resolved differently than planned.")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = saved

    out = ["| prefix | backfill row effective | events re-priced per model |"
           " cost before → after | delta |", "|---|---|---|---|---|"]
    new_rows = {real[rid]: row for rid, row in hypo.items()}
    for rid, c in zip(hypo, chosen):
        evs = [ev for ev, r in comb.items() if r == rid]
        per, before, after = {}, 0.0, 0.0
        for ev in evs:
            name, _ts, tok = sh.events[ev]
            b = _cost(tok, sh.rows.get(sh.baseline[ev]))
            a = _cost(tok, new_rows[actual[ev]])
            per[name] = per.get(name, 0) + 1
            before, after = before + b, after + a
        cells = "<br>".join(f"`{k}`: {v:,}" for k, v in sorted(per.items()))
        out.append(f"| `{c['prefix']}` | {c['backfill_from']} | {cells or '—'}"
                   f" | {_usd(before)} → {_usd(after)} |"
                   f" **{_usd(after - before, True)}** |")
    for p in noop:
        out.append(f"| `{p}` | — | already backfilled — nothing to do | — | — |")
    out += ["", f"Verified: {len(comb):,} event(s) now resolve to the new"
            f" backfill row(s) exactly as planned; 0 events outside the"
            f" impact set(s) changed. {len(real)} row(s) inserted (INSERT"
            " only; no row was updated or deleted)."]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pricing_update.py")
    ap.add_argument("--db", default=None)
    ap.add_argument("--html", default=None,
                    help="parse a local HTML file instead of fetching (tests)")
    ap.add_argument("--backfill-plan", action="store_true",
                    help="print the read-only backfill plan and exit")
    ap.add_argument("--json", action="store_true",
                    help="with --backfill-plan: machine-readable JSON")
    ap.add_argument("--backfill-apply", nargs="+", metavar="PREFIX",
                    help="insert the backfill row for each confirmed prefix"
                         " (all-or-nothing; re-plans first)")
    args = ap.parse_args(argv)
    db = args.db or capture.db_path()
    if not Path(db).exists():
        print("No telemetry DB yet — nothing to update. Enable capture with"
              " `/token-telemetry:enable` first.")
        return 0
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    if args.backfill_plan:
        import storage
        conn = storage.LocalSqliteBackend(db).open_ro()
        try:
            p = backfill_plan(conn, today)
        finally:
            conn.close()
        if args.json:
            print(json.dumps(_json_plan(p), indent=2, sort_keys=True))
        else:
            print(render_backfill_plan(p))
        return 0
    if args.backfill_apply:
        conn = capture.connect(db)
        try:
            print(backfill_apply(conn, args.backfill_apply, today))
        except BackfillRefused as exc:
            print(exc.args[0])
            return 1
        finally:
            conn.close()
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
