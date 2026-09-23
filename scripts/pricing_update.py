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
fetches the page. `--backfill-apply` exits 2, touching nothing, when any
argument is not a well-formed pricing prefix (:func:`is_pricing_prefix`), and 1
when the apply is refused or rolled back (nothing written). `--backfill-apply`
consumes every remaining raw argument as a candidate prefix (validated before
argument parsing even runs — see :func:`_reject_option_shaped_backfill_apply`),
so `--db`/`--html` MUST be given before `--backfill-apply` on the command line;
anything after it that is not a well-formed prefix, including another flag,
is rejected by position, never echoed. `--backfill-plan` and
`--backfill-apply` are mutually exclusive — rejected with exit 2 before any
DB is opened if both appear anywhere on the command line
(:func:`_reject_combined_backfill_flags`), and again by the argparse parser
itself, so a pre-approved `--backfill-plan ...` prefix match can never also
apply.

Backend seam: DB work goes through capture.connect() (the schema owner);
parsing and planning are pure functions over plain data, reusable unchanged
when other database backends arrive.
"""
import argparse
import datetime
import decimal
import json
import math
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture
import report

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
# The model families the page parser recognizes (:func:`parse_models`).
FAMILIES = ("Fable", "Mythos", "Opus", "Sonnet", "Haiku")
# The exact shape :func:`specific_prefixes` mints for a parsed version
# (``claude-<family>-<digits>[-<digits>]``), plus the legacy aliases it mints
# from SPECIAL_PREFIXES — the only strings ``--backfill-apply`` accepts.
_PREFIX_RE = re.compile(
    r"claude-(?:" + "|".join(f.lower() for f in FAMILIES) + r")-[0-9]+(?:-[0-9]+)?")
_LEGACY_PREFIXES = frozenset(p for ps in SPECIAL_PREFIXES.values() for p in ps)

# Parser bounds (AOS-143 security review): a bad row minted from the parsed
# pricing page is permanent (insert-only history), so what a page can mint is
# bounded on every axis a hostile or broken page could abuse.
MAX_RATE_USD = 10000.0          # money() ceiling: no rate this high is real
MAX_CANDIDATES = 500            # candidate rows a single run may mint
BACKDATE_MAX_DAYS = 365         # oldest a `starting <d>` row may be minted at

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
# Unicode bidi-control characters (U+202A-U+202E, U+2066-U+2069): can
# visually reorder or spoof rendered/terminal text (mirrors report.md_cell).
_BIDI_CHAR_RE = re.compile(r"[‪-‮⁦-⁩]")
_ERROR_TEXT_MAX = 200


def _safe_error_text(value):
    """Make untrusted page text (a raw cell, a header row) safe to embed in
    an exception message that may reach stderr or a terminal: strip ASCII
    control characters and DEL, strip Unicode bidi-control characters, then
    cap length so one hostile page cell cannot blow up the printed message.
    A parse-failure message must never carry raw, unsanitized page text.

    :param value: the untrusted text (or a list/tuple of cells — stringified
        first).
    :returns: the sanitized, length-capped text.
    """
    s = _BIDI_CHAR_RE.sub("", _CONTROL_CHAR_RE.sub("", str(value)))
    return s if len(s) <= _ERROR_TEXT_MAX else s[:_ERROR_TEXT_MAX] + "…"


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
    """Parse a ``$<amount> / MTok`` cell to a float, or ``None`` when the
    cell has no dollar amount. Deliberately permissive about magnitude —
    ``parse_models`` is the bound-enforcement point (:data:`MAX_RATE_USD`),
    so every caller sees the same, single check."""
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
        raise ValueError("unexpected pricing table header:"
                         f" {_safe_error_text(header)}")

    entries = []
    for row in table[1:]:
        if len(row) <= max(col.values()):
            continue
        m = re.search(r"Claude\s+(" + "|".join(FAMILIES) + r")"
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
            raise ValueError("unparseable rate cell in row:"
                             f" {_safe_error_text(row[0])}")
        # Bound what a page can mint (AOS-143): reject a non-finite rate (a
        # very long digit string overflows float() to `inf` with no
        # exception) and any rate over MAX_RATE_USD/MTok. A single bad cell
        # refuses the WHOLE run — money() is permissive on purpose, this is
        # the one enforcement point every caller shares.
        bad = [k for k, v in rates.items()
               if not math.isfinite(v) or v > MAX_RATE_USD]
        if bad:
            raise ValueError(
                "rate out of bounds (non-finite, or over"
                f" ${MAX_RATE_USD:g}/MTok) for Claude {m.group(1).title()}"
                f" {m.group(2)}: {', '.join(sorted(bad))}")
        entries.append({"family": m.group(1).lower(), "version": m.group(2),
                        "rates": rates, "condition": condition})
    if not entries:
        raise ValueError("no model rows parsed from the pricing table")
    return entries


def is_pricing_prefix(value):
    """Whether ``value`` has the strict shape of a version pricing prefix
    this script mints: ``claude-<family>-<digits>[-<digits>]`` (ASCII,
    lowercase, whole string — see :func:`specific_prefixes`) or one of the
    legacy aliases in ``SPECIAL_PREFIXES``. ``--backfill-apply`` rejects any
    other argument before it touches the DB (defence in depth: a prefix is
    passed on a shell command line, so nothing else may ever reach it).

    :param value: a candidate prefix string (a command-line argument).
    :returns: ``True`` for a well-formed prefix, else ``False``.
    """
    return isinstance(value, str) and (
        value in _LEGACY_PREFIXES or _PREFIX_RE.fullmatch(value) is not None)


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


def future_only_newest(entries, today):
    """Families whose newest (first-listed) version's ONLY listing(s) are a
    FUTURE ``starting <d>`` increase (``d > today``) — so, like
    :func:`stale_intros`, no rate is in force for it and no family default is
    minted either (:func:`build_candidates`). Unlike :func:`stale_intros`
    (an EXPIRED ``through``), this is a scheduled increase that has not
    arrived yet: the family default is not stale, it simply has not been
    told to change. Distinct from :func:`stale_intros` so a version mixing an
    expired ``through`` with a future ``starting`` is reported once, by
    :func:`stale_intros`, not twice.

    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :returns: ``[(family, version, earliest_future_starting_date)]`` in page
        order.
    """
    newest_version, order, rows = {}, {}, {}
    for i, e in enumerate(entries):
        newest_version.setdefault(e["family"], e["version"])
        key = (e["family"], e["version"])
        order.setdefault(key, i)
        rows.setdefault(key, []).append(e)
    out = []
    for fam, ver in newest_version.items():
        key = (fam, ver)
        listings = rows[key]
        if any(r["condition"] is None or in_force(r["condition"], today)
               for r in listings):
            continue
        if any(r["condition"][0] != "starting" for r in listings):
            continue  # an expired `through` here is stale_intros's to report
        out.append((fam, ver, min(r["condition"][1] for r in listings)))
    out.sort(key=lambda t: order[(t[0], t[1])])
    return out


def filter_backdated_starting(conn, entries, today):
    """Drop every in-force ``starting <d>`` entry ``d`` is too old to trust
    (AOS-143 parser bounds): an in-force ``starting`` row is normally minted
    dated ``d`` however far back (:func:`build_candidates`), which would
    re-price every event of that prefix back to ``d`` — permanently, since
    pricing history is insert-only. Refused when ``d`` is more than
    :data:`BACKDATE_MAX_DAYS` days before ``today``, OR earlier than the
    latest ``effective_from`` already recorded for the version's own
    prefix(es) (a real increase is never older than what is already on
    record). A future ``starting`` (``d > today``) is left alone — it is
    never minted anyway (:func:`in_force`), so it cannot be backdated.

    :param conn: an open, readable telemetry DB connection.
    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :returns: ``(filtered_entries, warnings)`` — ``entries`` with every
        refused row removed, and one ``(family, version, date, reason)``
        tuple per refusal, in page order.
    """
    cutoff = today - datetime.timedelta(days=BACKDATE_MAX_DAYS)
    filtered, warnings = [], []
    for e in entries:
        cond = e["condition"]
        if cond is None or cond[0] != "starting" or cond[1] > today:
            filtered.append(e)
            continue
        date = cond[1]
        reason = None
        if date < cutoff:
            reason = (f"more than {BACKDATE_MAX_DAYS} days before today"
                      f" ({today.isoformat()})")
        else:
            latest = None
            for p in specific_prefixes(e["family"], e["version"]):
                row = conn.execute(
                    "SELECT MAX(effective_from) FROM pricing"
                    " WHERE provider=? AND model_prefix=?",
                    (PROVIDER, p)).fetchone()
                if row and row[0] is not None:
                    latest = row[0] if latest is None else max(latest, row[0])
            if latest is not None and _epoch(date) < latest:
                reason = ("earlier than the latest recorded rate for"
                          f" `{specific_prefixes(e['family'], e['version'])[0]}`"
                          f" ({_iso(latest)})")
        if reason is None:
            filtered.append(e)
        else:
            warnings.append((e["family"], e["version"], date, reason))
    return filtered, warnings


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


def render(candidates, inserted, unpriced, today, stale=(), backdated=(),
           future_only=()):
    """The finished markdown run report.

    :param candidates: planned+applied candidates (:func:`plan`,
        :func:`apply`).
    :param inserted: number of rows inserted.
    :param unpriced: model names matching no pricing prefix.
    :param today: the run date (UTC).
    :param stale: :func:`stale_intros` output — one STALE-PRICE WARNING line
        per version whose only listed rate is an expired intro.
    :param backdated: :func:`filter_backdated_starting` warnings output —
        one BACKDATED-STARTING WARNING line per refused ``starting`` row.
    :param future_only: :func:`future_only_newest` output — one FUTURE-RATE
        WARNING line per family whose newest version's only rate has not
        started yet.
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
    for fam, ver, date, reason in backdated:
        out += ["", f"BACKDATED-STARTING WARNING: Claude {fam.capitalize()}"
                f" {ver} (`{specific_prefixes(fam, ver)[0]}`) — its"
                f" `starting {date.isoformat()}` row was refused ({reason});"
                " nothing was minted for it."]
    for fam, ver, date in future_only:
        out += ["", f"FUTURE-RATE WARNING: Claude {fam.capitalize()} {ver}"
                f" (`{specific_prefixes(fam, ver)[0]}`) — its only listed"
                f" rate starts {date.isoformat()}, still in the future; the"
                " family default keeps its last recorded rate until then."]
    out += ["", f"Source: {URL} — checked {today.isoformat()},"
            f" {inserted} row(s) inserted (history is insert-only; existing"
            " rows are never modified)."]
    return "\n".join(out)


class PricingRefused(Exception):
    """A run refused before any write; ``args[0]`` is the report/message."""


def run_update(conn, entries, today, source=URL):
    """Plan, apply and report one pricing refresh of parsed page ``entries``.

    Two bounds (AOS-143) can refuse work before any write reaches the DB:
    an in-force ``starting`` row too old to trust is dropped per-entry by
    :func:`filter_backdated_starting` (a warning, not a refusal — the rest
    of the run proceeds); a candidate count over :data:`MAX_CANDIDATES`
    refuses the WHOLE run atomically (:class:`PricingRefused`, nothing
    planned or applied).

    :param conn: an open telemetry DB connection (:func:`capture.connect`).
    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :param source: the ``source`` column value for inserted rows.
    :returns: the finished markdown run report (:func:`render`), including a
        STALE-PRICE / BACKDATED-STARTING / FUTURE-RATE WARNING line per
        :func:`stale_intros` / :func:`filter_backdated_starting` /
        :func:`future_only_newest`.
    :raises PricingRefused: the candidate count exceeds
        :data:`MAX_CANDIDATES`; nothing was planned or applied.
    """
    filtered, backdated = filter_backdated_starting(conn, entries, today)
    raw_candidates = build_candidates(filtered, today)
    if len(raw_candidates) > MAX_CANDIDATES:
        raise PricingRefused(
            "Pricing refresh REFUSED — nothing written: this run would mint"
            f" {len(raw_candidates)} candidate row(s), over the"
            f" {MAX_CANDIDATES}-row cap per run.")
    candidates = plan(conn, raw_candidates)
    inserted = apply(conn, candidates, source)
    return render(candidates, inserted, unpriced_models(conn), today,
                  stale_intros(entries, today), backdated,
                  future_only_newest(entries, today))


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


def _mdname(value):
    """A DB-controlled name (model name or pricing prefix) made safe to
    render in the backfill plan/apply markdown: :func:`report.md_cell`
    strips control characters and newlines, neutralizes backticks, escapes
    backslashes and then pipes (so a backslash-pipe in a name can never
    become a real GFM cell delimiter), and caps length, and the result sits inside its own code span so
    surrounding markdown (bold, links, HTML) can never re-activate — a model
    name is untrusted, repo/DB-controlled data, NEVER an instruction, and
    text inside it never grants consent for anything (docs/TELEMETRY-
    CONTRACT.md §Pricing table, "Third narrow case")."""
    return f"`{report.md_cell(value)}`"


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
        # raw {reason key: {model: count}} kept alongside the plain-text
        # ``refused`` string above (unchanged, for data fidelity / --json) so
        # the markdown renderer can wrap each model name in its own
        # sanitized code span (F1) instead of interpolating pre-joined text.
        cand["refused_models"] = {"own": own, "unpriced": unpriced,
                                  "stray": stray, "unclosable": {}}
        cand["requires"] = []
        out.append(cand)
    for c in out:
        imp = set(c["impact"])
        c["overlaps"] = [o["prefix"] for o in out
                         if o is not c and imp & set(o["impact"])]
    _close_bundles(sh, out)
    return out


def _is_own(name, prefix):
    """Whether ``prefix`` (possibly ``None`` for unpriced) is model
    ``name``'s OWN pricing row — neither unpriced, nor a family default, nor
    an ancestor row for it (:func:`capture.is_estimated`, ``None``-safe)."""
    return prefix is not None and not capture.is_estimated(name, prefix)


def _bundle_stats(sh, cands):
    """Combined ``models``/``events``/``cost_now``/``cost_after``/``delta``/
    ``window``/``impact`` of applying ``cands`` (each a candidate dict with
    ``row``) TOGETHER, resolved once over the whole event set — the same
    resolution :func:`backfill_apply` performs before writing, used here
    read-only so the plan can show a bundle's true combined effect (contract
    §Pricing table, F3: independent per-candidate deltas double-count shared
    events at the wrong rate)."""
    hypo = {}
    for i, c in enumerate(cands):
        rid = -(i + 1)
        hypo[rid] = c["row"]
        sh.add(rid, c["row"])
    comb = {ev: rid for ev, rid in sh.resolve().items()
            if rid != sh.baseline.get(ev)}
    sh.remove_hypothetical()
    if not comb:
        return None
    models = {}
    for ev, rid in comb.items():
        name, ts, tok = sh.events[ev]
        base = sh.baseline.get(ev)
        row = hypo.get(rid, sh.rows.get(rid))
        m = models.setdefault(name, {"model": name, "events": 0,
                                     "cost_now": 0.0, "cost_after": 0.0,
                                     "first": ts, "last": ts})
        m["events"] += 1
        m["cost_now"] += _cost(tok, sh.rows.get(base))
        m["cost_after"] += _cost(tok, row)
        m["first"], m["last"] = min(m["first"], ts), max(m["last"], ts)
    first = min(m["first"] for m in models.values())
    last = max(m["last"] for m in models.values())
    mlist = []
    for m in sorted(models.values(), key=lambda m: m["model"]):
        m["delta"] = m["cost_after"] - m["cost_now"]
        m["first"], m["last"] = _iso(m["first"]), _iso(m["last"])
        mlist.append(m)
    stats = {"models": mlist, "events": sum(m["events"] for m in mlist),
             "cost_now": sum(m["cost_now"] for m in mlist),
             "cost_after": sum(m["cost_after"] for m in mlist),
             "window": {"first": _iso(first), "last": _iso(last),
                        "span": human_span(
                            datetime.date.fromisoformat(_iso(first)),
                            datetime.date.fromisoformat(_iso(last)))},
             "impact": sorted(comb)}
    stats["delta"] = stats["cost_after"] - stats["cost_now"]
    return stats


def _close_bundles(sh, cands):
    """Enforce F2 in place on ``cands`` (:func:`_plan_candidates` output,
    ``overlaps`` already set): an apply is valid only if, after it, every
    event in its impact set resolves to its OWN model's row.

    For each not-already-refused candidate, grow the smallest set of
    candidate prefixes (a bundle) whose combined hypothetical rows leave
    every impacted event own-priced (:func:`_is_own`) — reusing whichever
    OTHER offered candidate owns the model still estimated after this one
    alone. Sets ``requires`` (other prefixes that must apply together with
    this one; empty when it stands alone) and, for a candidate no bundle can
    close, extends ``refused``/``refused_models`` with the blocking model(s)
    instead. A candidate that needs bundling has its ``models``/``events``/
    ``cost_now``/``cost_after``/``delta``/``window``/``impact`` OVERWRITTEN
    with the bundle-combined figures (F3) — a candidate that stands alone is
    untouched.
    """
    by_prefix = {c["prefix"]: c for c in cands}
    closeable = {p: c for p, c in by_prefix.items() if not c["refused"]}
    for c in cands:
        if c["refused"]:
            continue
        chosen = {c["prefix"]}
        blocked = {}
        while True:
            hypo = {}
            for i, p in enumerate(sorted(chosen)):
                rid = -(i + 1)
                hypo[rid] = closeable[p]["row"]
                sh.add(rid, closeable[p]["row"])
            union = set()
            for p in chosen:
                union |= set(closeable[p]["impact"])
            resolved = sh.resolve()
            sh.remove_hypothetical()
            blocked, found = {}, set()
            for ev in union:
                rid = resolved.get(ev)
                name = sh.events[ev][0]
                prefix = (hypo[rid]["model_prefix"] if rid in hypo
                          else sh.rows[rid]["model_prefix"] if rid is not None
                          else None)
                if _is_own(name, prefix):
                    continue
                owner = max((p for p in closeable
                             if p not in chosen
                             and name.lower().startswith(p.lower())
                             and not capture.is_estimated(name, p)),
                            key=len, default=None)
                if owner is None:
                    blocked[name] = blocked.get(name, 0) + 1
                else:
                    found.add(owner)
            if blocked or not found:
                break
            chosen |= found
        if blocked:
            reason = ("would leave estimated events with no closing backfill"
                      " for their own row: " + ", ".join(
                          f"{k} ({v})" for k, v in sorted(blocked.items())))
            c["refused"] = f"{c['refused']}; {reason}" if c["refused"] \
                else reason
            c["refused_models"]["unclosable"] = blocked
            continue
        others = sorted(chosen - {c["prefix"]})
        c["requires"] = others
        if others:
            stats = _bundle_stats(sh, [closeable[p] for p in sorted(chosen)])
            if stats is not None:
                c.update(stats)


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

    F2: a candidate whose row would leave ANOTHER model's events resolving to
    a row that is still an estimate FOR THEM cannot stand alone — ``requires``
    names the other candidate prefix(es) it must apply together with (its
    own displayed figures are then the BUNDLE's combined effect, F3), and it
    is REFUSED instead when no candidate owns that other model.

    :param conn: a connection to the telemetry DB (read-only is enough).
    :param today: the run date (UTC) — stamped into the would-be ``source``.
    :returns: ``{"candidates": [...], "confirm_only": [...],
        "refused": [...], "combined": {...}|None}`` — each candidate entry a
        dict with ``prefix``, ``provider``, ``model_version``, ``r0``
        (effective_from, source, rates), ``backfill_from``, ``window``
        (first, last, span), ``models`` (per model: events, cost_now,
        cost_after, delta, first, last), ``events``, ``cost_now``,
        ``cost_after``, ``delta``, ``overlaps`` (other candidates sharing
        impacted events), ``requires`` (other prefixes it must bundle with),
        ``refused`` (reason or None), ``impact`` (event rowids) and ``row``
        (the would-be pricing row). ``combined`` is the true cost now/after/
        delta of applying every offered (``candidates`` + ``confirm_only``)
        bundle together, or ``None`` when there is nothing offered.
    """
    saved = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN")   # one read snapshot for the whole plan
    try:
        sh = _Shadow(conn)
        offered, zero, refused = _group(_plan_candidates(sh, today))
        # the true combined effect of every OFFERED bundle applied together
        # (F3): a single simulation, never a sum of per-row deltas, so a
        # shared event is never priced twice or at the wrong row.
        combined = (_bundle_stats(sh, offered + zero)
                   if (offered or zero) else None)
    finally:
        conn.execute("ROLLBACK")   # also discards the TEMP copy
        conn.isolation_level = saved
    return {"candidates": offered, "confirm_only": zero, "refused": refused,
            "combined": combined}


def _usd(v, signed=False):
    """``v`` dollars as display text: two decimals, or four when a nonzero
    amount is under a cent; ``signed`` adds ``+`` for a positive amount."""
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else (
        "-" if v < 0 else "")
    a = abs(v)
    body = f"{a:,.2f}" if a >= 0.01 or a == 0 else f"{a:.4f}"
    return f"{sign}${body}"


def _shown(v):
    """The exact amount :func:`_usd` displays for ``v`` (after its
    rounding), as a :class:`decimal.Decimal`."""
    return decimal.Decimal(_usd(v).replace("$", "").replace(",", ""))


def _usd_change(now, after):
    """``(now, after, delta)`` display strings for a cost change whose shown
    figures RECONCILE: the delta is the difference of the two DISPLAYED
    (rounded) totals, computed exactly, never the rounded unrounded delta —
    so a reader subtracting ``after - now`` gets exactly the delta shown
    (F3 LOW: $6,450.37 → $6,177.91 must read -$272.46, not -$272.47). The
    delta keeps four decimals only when the shown totals themselves carry
    sub-cent digits.

    :param now: cost before, USD (unrounded).
    :param after: cost after, USD (unrounded).
    :returns: ``(now_text, after_text, signed_delta_text)``.
    """
    d = _shown(after) - _shown(now)
    sign = "+" if d > 0 else "-" if d < 0 else ""
    a = abs(d)
    cents = a.quantize(decimal.Decimal("0.01"))
    body = f"{a:,.2f}" if a == cents else f"{a:,.4f}"
    return _usd(now), _usd(after), f"{sign}${body}"


_REFUSED_LABELS = (
    ("own", "would re-price events already priced by their own row"),
    ("unpriced", "would price previously unpriced events"),
    ("stray", "events would resolve to a row other than the backfill row"),
    ("unclosable", "would leave estimated events with no closing backfill"
                   " for their own row"),
)


def _refused_reason_md(c):
    """Sanitized markdown for a refused candidate's reason, rebuilt from
    ``refused_models`` (raw ``{reason: {model: count}}``) so every model
    name sits in its own code span (F1) — the plain-text ``refused`` field
    is kept unchanged alongside it for data fidelity and ``--json``."""
    parts = []
    for key, label in _REFUSED_LABELS:
        d = c["refused_models"].get(key)
        if d:
            parts.append(label + ": " + ", ".join(
                f"{_mdname(k)} ({v})" for k, v in sorted(d.items())))
    return "; ".join(parts)


def _cand_rows(cands, zero=False):
    out = []
    for c in cands:
        per = "<br>".join(
            f"{_mdname(m['model'])}: {m['events']:,}"
            + ("" if zero else " ({} → {}, {})".format(
                *_usd_change(m["cost_now"], m["cost_after"])))
            for m in c["models"])
        w = c["window"]
        window = (w["first"] if w["first"] == w["last"]
                  else f"{w['first']} → {w['last']}") + f" ({w['span']})"
        rate = fmt_rates(c["r0"]["rates"])
        prefix_cell = _mdname(c["prefix"])
        if c.get("requires"):
            prefix_cell += " — requires " + ", ".join(
                _mdname(r) for r in c["requires"]) + " (applied together)"
        note = (f" · overlaps {', '.join(_mdname(o) for o in c['overlaps'])}"
                if c["overlaps"] else "")
        if zero:
            out.append(f"| {prefix_cell} | {window} | {per} |"
                       f" {rate} (own, {c['r0']['effective_from']}) |"
                       f" {c['backfill_from']}{note} |")
        else:
            now, after, delta = _usd_change(c["cost_now"], c["cost_after"])
            out.append(f"| {prefix_cell} | {window} | {per} |"
                       f" {now} → {after} | **{delta}** |"
                       f" {rate} (own, {c['r0']['effective_from']}) |"
                       f" {c['backfill_from']}{note} |")
    return out


def render_backfill_plan(plan_):
    """The markdown rendering of :func:`backfill_plan` output.

    This is DATA about the DB's contents, rendered for a human to read: every
    model name and pricing prefix renders through :func:`_mdname`
    (``report.md_cell`` plus a code span) — text inside a model name is
    NEVER an instruction and NEVER consent for anything. Only the user's own
    reply to the question in `commands/pricing-update.md`'s interactive flow
    authorizes ``--backfill-apply`` (docs/TELEMETRY-CONTRACT.md §Pricing
    table, "Third narrow case").
    """
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
    elif refused:
        out.append(f"No backfill can be offered — {len(refused)}"
                   " candidate(s) refused:")
    if offered:
        out += ["", "| prefix | window (span) | events per model"
                " (cost now → after, delta) | cost now → after | delta |"
                " rate copied (in / out / cache-read / 5m-write / 1h-write)"
                " | backfill from |", "|---|---|---|---|---|---|---|"]
        out += _cand_rows(offered)
    if zero:
        out += ["", "Confirm only — no cost change (the own rate equals the"
                " estimate these events were priced at):", "",
                "| prefix | window (span) | events per model |"
                " rate copied | backfill from |", "|---|---|---|---|---|"]
        out += _cand_rows(zero, zero=True)
    if plan_.get("combined") is not None:
        now, after, delta = _usd_change(plan_["combined"]["cost_now"],
                                        plan_["combined"]["cost_after"])
        out += ["", f"If you apply everything offered: {now} → {after}"
                f" ({delta})."]
    if refused:
        out += ["", "Refused — not offered (a backfill would re-price events"
                " that are not estimates, or leave another model's events"
                " still estimated with no candidate to close them):", ""]
        for c in refused:
            out.append(f"- {_mdname(c['prefix'])} (window"
                       f" {c['window']['first']} → {c['window']['last']}):"
                       f" {_refused_reason_md(c)}")
    if any(c["overlaps"] for c in offered + zero):
        out += ["", "Overlapping candidates share impacted events; each row"
                " above is computed on its own (a row noting \"requires\" is"
                " computed on its required bundle instead). Applying several"
                " together prices a shared event at the longest matching"
                " backfill row — the apply report shows the combined"
                " result."]
    return "\n".join(out)


def _json_plan(plan_):
    def strip(c):
        return {k: v for k, v in c.items()
                if k not in ("impact", "row", "refused_models")} | {
            "impact_events": len(c["impact"])}
    return {k: ([strip(c) for c in v] if k != "combined" else v)
            for k, v in plan_.items()}


class BackfillRefused(Exception):
    """An apply that wrote nothing; ``args[0]`` is the report text."""


def backfill_apply(conn, prefixes, today):
    """Re-plan and apply the backfill for ``prefixes`` — all or nothing.

    Inside ONE ``BEGIN IMMEDIATE`` transaction: re-compute the plan (a stale
    one is never trusted); refuse the whole apply, writing nothing, if any
    named prefix is not a current non-refused candidate (a prefix that is
    already backfilled — no longer a candidate and holding a ``backfill:``
    row — is a no-op, so a re-run is idempotent); refuse it, naming the
    missing prefix, when a chosen candidate's ``requires`` (F2 — the own-row
    closure requirement) is not itself entirely among ``prefixes``; check the
    COMBINED hypothetical re-prices exactly the union of the named impact
    sets and only estimated events; ``INSERT OR IGNORE`` one row per prefix
    (R0's rates, ``effective_from`` = the backfill date, ``source`` =
    ``backfill:<R0 source>; confirmed <today>``); then verify every event
    against the real table — each impacted event resolves to its predicted
    new row and is no longer estimated (F2), no other event changed — and
    ROLL BACK on any mismatch. Never UPDATEs or DELETEs a pricing row.

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
                bad.append(f"{_mdname(p)}: " + (
                    f"refused — {_refused_reason_md(c)}" if c
                    else "not a backfill candidate"))
        if bad:
            raise BackfillRefused(
                "Backfill REFUSED — nothing written (all-or-nothing):\n"
                + "\n".join(f"- {b}" for b in bad))
        # F2: every chosen candidate's closure requirement must be named too
        # — applying a predecessor without the successor that still needs
        # closing is exactly the case F2 forbids.
        missing = [(c["prefix"], req) for c in chosen
                   for req in c.get("requires", []) if req not in names]
        if missing:
            lines = [f"- {_mdname(p)} requires {_mdname(req)}"
                     " (applied together)" for p, req in missing]
            raise BackfillRefused(
                "Backfill REFUSED — nothing written: the named set is not"
                " closed under the own-row requirement (F2):\n"
                + "\n".join(lines))
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
        # the applied set's combined figures, by the SAME single simulation
        # the plan uses for its bundle rows and its "everything offered" line
        # (:func:`_bundle_stats`), so the report's total reconciles to the
        # cent with the plan's line for the same set — never a sum of rows
        total = _bundle_stats(sh, chosen) if chosen else None
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
                    f" {_mdname(row['model_prefix'])} at"
                    f" {_iso(row['effective_from'])}"
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
        # F2: every impacted event must now be OWN-priced (est=0), not just
        # resolved to the predicted row — the row it predicted could still
        # be an estimate for it if the closure computation above were wrong.
        prefix_by_real_rowid = {real[rid]: hypo[rid]["model_prefix"]
                                for rid in hypo}
        still_estimated = [ev for ev in comb if not _is_own(
            sh.events[ev][0], prefix_by_real_rowid.get(actual.get(ev)))]
        if still_estimated:
            raise BackfillRefused(
                "Backfill ROLLED BACK — verification failed:"
                f" {len(still_estimated)} event(s) still estimated after"
                " apply.")
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
        cells = "<br>".join(f"{_mdname(k)}: {v:,}" for k, v in sorted(per.items()))
        b_txt, a_txt, d_txt = _usd_change(before, after)
        out.append(f"| {_mdname(c['prefix'])} | {c['backfill_from']} |"
                   f" {cells or '—'} | {b_txt} → {a_txt} | **{d_txt}** |")
    for p in noop:
        out.append(f"| {_mdname(p)} | — | already backfilled — nothing to do"
                   " | — | — |")
    if total is not None:
        t_now, t_after, t_delta = _usd_change(total["cost_now"],
                                              total["cost_after"])
        out += ["", f"Total for the applied set: {t_now} → {t_after}"
                f" ({t_delta})."]
    out += ["", f"Verified: {len(comb):,} event(s) now resolve to the new"
            f" backfill row(s) exactly as planned; 0 events outside the"
            f" impact set(s) changed. {len(real)} row(s) inserted (INSERT"
            " only; no row was updated or deleted)."]
    return "\n".join(out)


class _ArgumentParser(argparse.ArgumentParser):
    """``argparse.ArgumentParser`` whose ``error()`` strips ASCII control
    characters (e.g. a raw ESC byte) out of argparse's own error message
    before printing it, so a hostile argv token cannot inject terminal
    escape sequences into stderr. Mirrors the base implementation otherwise
    (usage to stderr, then exit 2).
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        clean = _CONTROL_CHAR_RE.sub("", message)
        self.exit(2, f"{self.prog}: error: {clean}\n")


def _reject_combined_backfill_flags(argv):
    """Defence in depth against a future refactor reordering ``main``'s
    branches: refuse ``--backfill-plan`` and ``--backfill-apply`` together,
    BEFORE argparse ever runs and before any DB is opened. argparse's own
    ``nargs="+"`` on ``--backfill-apply`` only stops consuming values at the
    next token that LOOKS like an option, so this membership check catches
    both orderings — ``--backfill-plan`` before ``--backfill-apply`` (which
    argparse alone would parse as two independent, non-conflicting flags,
    each branch in ``main`` then deciding which one runs) and
    ``--backfill-apply`` before ``--backfill-plan`` (already separately
    caught, earlier in ``main`` and before this function even runs, by
    :func:`_reject_option_shaped_backfill_apply` treating ``--backfill-plan``
    as an option-shaped value following ``--backfill-apply``). The two flags
    are also declared mutually exclusive in the argparse parser itself, as a
    third, independent layer.

    :param argv: the raw argument list, before ``argparse.parse_args``.
    :returns: ``True`` when both flags appear anywhere in ``argv``, else
        ``False``.
    """
    return "--backfill-plan" in argv and "--backfill-apply" in argv


def _reject_option_shaped_backfill_apply(argv):
    """Defence in depth against option injection: reject any raw argv token
    after ``--backfill-apply`` that is not a well-formed pricing prefix,
    BEFORE argparse ever runs. ``--backfill-apply`` uses ``nargs="+"``, so
    without this guard argparse hands any ``-``-prefixed token (or, with
    abbreviation matching, a shortened flag) to its own option parser first
    — e.g. ``--backfill-apply claude-opus-5-5 --db=other.db`` would redirect
    the write to ``other.db`` instead of being rejected. Because argparse
    consumes every following token as a value once it sees
    ``--backfill-apply``, ``--db``/``--html`` must be given BEFORE it on the
    command line.

    :param argv: the raw argument list, before ``argparse.parse_args``.
    :returns: the 1-based positions (within the arguments following
        ``--backfill-apply``) of every offending token, or ``None`` when
        ``--backfill-apply`` is absent or every following token is a
        well-formed prefix.
    """
    if "--backfill-apply" not in argv:
        return None
    start = argv.index("--backfill-apply") + 1
    bad = [i for i, tok in enumerate(argv[start:], 1)
           if not is_pricing_prefix(tok)]
    return bad or None


def _backfill_apply_refusal(positions):
    """The REFUSED message for malformed ``--backfill-apply`` arguments,
    naming only their position — the value itself is never echoed back.

    :param positions: 1-based positions of the offending arguments.
    :returns: the report line to print.
    """
    return ("Backfill REFUSED — nothing written: rejected"
            " --backfill-apply argument(s) "
            + ", ".join(f"#{i}" for i in positions) + " — not a pricing"
            " prefix (expected claude-<family>-<version>, e.g."
            " claude-opus-5-5).")


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Validate BEFORE argparse ever runs. Nothing is opened here.
    # First: argparse would otherwise hand any option-shaped token following
    # --backfill-apply (or, via abbreviation matching, a shortened flag) to
    # its own parser first, redirecting the write or switching modes
    # (--db=, --d=, -h) instead of being rejected as a malformed prefix. This
    # already catches --backfill-apply PREFIX --backfill-plan, since
    # --backfill-plan is option-shaped.
    bad = _reject_option_shaped_backfill_apply(raw_argv)
    if bad:
        print(_backfill_apply_refusal(bad))
        return 2
    # Second: --backfill-plan and --backfill-apply are mutually exclusive —
    # catches the remaining ordering, --backfill-plan before --backfill-apply,
    # which argparse alone would parse as two independent, non-conflicting
    # flags and let main()'s branch order decide which one runs. A future
    # refactor of that branch order must not be able to make a pre-approved
    # `--backfill-plan ...` command line also apply.
    if _reject_combined_backfill_flags(raw_argv):
        print("Backfill REFUSED — nothing written: --backfill-plan and"
              " --backfill-apply are mutually exclusive.")
        return 2

    ap = _ArgumentParser(prog="pricing_update.py", allow_abbrev=False)
    ap.add_argument("--db", default=None)
    ap.add_argument("--html", default=None,
                    help="parse a local HTML file instead of fetching (tests)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--backfill-plan", action="store_true",
                       help="print the read-only backfill plan and exit")
    ap.add_argument("--json", action="store_true",
                    help="with --backfill-plan: machine-readable JSON")
    group.add_argument("--backfill-apply", nargs="+", metavar="PREFIX",
                       help="insert the backfill row for each confirmed"
                            " prefix (all-or-nothing; re-plans first;"
                            " --db/--html must be given BEFORE this flag;"
                            " mutually exclusive with --backfill-plan)")
    args = ap.parse_args(argv)
    if args.backfill_apply:
        # Second layer, defence in depth, before the DB is opened: only the
        # strict prefix shape the parser mints may ever reach an apply. The
        # offending value is identified by position, never echoed back.
        bad = [i for i, p in enumerate(args.backfill_apply, 1)
               if not is_pricing_prefix(p)]
        if bad:
            print(_backfill_apply_refusal(bad))
            return 2
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
    except PricingRefused as exc:
        print(exc.args[0])
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
