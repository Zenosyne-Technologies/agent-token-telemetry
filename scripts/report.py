#!/usr/bin/env python3
"""Deterministic, read-only markdown reports for the telemetry slash commands.

`report.py info [--cwd PATH]` and `report.py project-stats` print finished
markdown to stdout; the command prompts run this and echo the output verbatim,
so no model tokens are spent re-deriving SQL or formatting tables.

Backend seam: every fetch_* function takes a DB-API 2.0 connection and returns
plain data; every render_* function turns that data into markdown. `open_ro()`
is the ONLY place a concrete backend (today: local SQLite, mode=ro — this
script can never create or migrate anything) is chosen. A future server-hosted
DB plugs in by extending open_ro(), leaving queries and rendering untouched.
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture


def open_ro(db):
    """Read-only connection to the telemetry store, or None when absent.
    The single backend-selection point (see module docstring)."""
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def has_column(conn, table, column):
    return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def fmt_n(n):
    return format(int(n or 0), ",")


def fmt_usd(v):
    """Cost cells: max two decimals, trailing zeros cut ($1.50 -> $1.5,
    $25.00 -> $25). A nonzero cost must never render as $0 — tiny values
    show as <$0.01 instead of claiming the work was free."""
    if 0 < v < 0.005:
        return "<$0.01"
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"${s}"


def humanize(day, today=None):
    """`2026-08-06` -> `2026-08-06 (today)` / `(3 days ago)` / `(2 months ago)`."""
    if not day:
        return ""
    today = today or datetime.date.today()
    delta = (today - datetime.date.fromisoformat(day)).days
    if delta <= 0:
        rel = "today"
    elif delta == 1:
        rel = "yesterday"
    elif delta < 60:
        rel = f"{delta} days ago"
    elif delta < 730:
        rel = f"{delta // 30} months ago"
    else:
        rel = f"{delta // 365} years ago"
    return f"{day} ({rel})"


def rate_subquery(column):
    return (f"(SELECT pr.{column} FROM pricing pr"
            " WHERE m.name LIKE pr.model_prefix || '%'"
            " AND pr.effective_from <= e.ts"
            " ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC"
            " LIMIT 1)")


# ---------------------------------------------------------------- project-stats

STATS_KEYS = ("path", "name", "sessions", "events", "input", "output",
              "cache_read", "cache_write",
              "classic_in", "classic_out", "cached_r", "cached_w",
              "rate_from", "unpriced_events", "first_seen", "last_activity")


def priced_cte(conn):
    """Shared per-event pricing CTE body. Pre-v4 DBs (not yet migrated by a
    capture) lack the cache-TTL split; the 1h portion is 0 there, which is
    exactly the pre-v4 estimate."""
    cw1h = "e.cache_w_1h" if has_column(conn, "events", "cache_w_1h") else "0"
    cw1h_usd = (rate_subquery("cache_w_1h_usd")
                if has_column(conn, "pricing", "cache_w_1h_usd") else "NULL")
    return cw1h, cw1h_usd


def fetch_project_stats(conn):
    """One dict per project, ordered by estimated cost. Each event prices at
    the rate in force at its own timestamp (see docs/TELEMETRY-CONTRACT.md).
    Cost components are returned separately: classic (uncached input/output)
    and cached (read/write) sum to the estimate."""
    cw1h, cw1h_usd = priced_cte(conn)
    name = "p.name" if has_column(conn, "projects", "name") else "NULL"
    rows = conn.execute(f"""
WITH priced AS (
  SELECT p.path AS path, {name} AS name, s.id AS session_id, e.ts AS ts,
         e.in_tok, e.out_tok, e.cache_r, e.cache_w, {cw1h} AS cache_w_1h,
         {rate_subquery('in_usd')} AS in_usd,
         {rate_subquery('out_usd')} AS out_usd,
         {rate_subquery('cache_r_usd')} AS cache_r_usd,
         {rate_subquery('cache_w_usd')} AS cache_w_usd,
         {cw1h_usd} AS cache_w_1h_usd,
         {rate_subquery('effective_from')} AS rate_from
  FROM projects p
  LEFT JOIN sessions s ON s.project_id = p.id
  LEFT JOIN events   e ON e.session_id = s.id
  LEFT JOIN models   m ON m.id = e.model_id
)
SELECT path, name,
       COUNT(DISTINCT session_id) AS sessions,
       COUNT(ts) AS events,
       COALESCE(SUM(in_tok), 0) AS input,
       COALESCE(SUM(out_tok), 0) AS output,
       COALESCE(SUM(cache_r), 0) AS cache_read,
       COALESCE(SUM(cache_w), 0) AS cache_write,
       COALESCE(SUM(in_tok * COALESCE(in_usd, 0)), 0) / 1000000.0
         AS classic_in,
       COALESCE(SUM(out_tok * COALESCE(out_usd, 0)), 0) / 1000000.0
         AS classic_out,
       COALESCE(SUM(cache_r * COALESCE(cache_r_usd, 0)), 0) / 1000000.0
         AS cached_r,
       COALESCE(SUM((cache_w - cache_w_1h) * COALESCE(cache_w_usd, 0)
             + cache_w_1h * COALESCE(cache_w_1h_usd, cache_w_usd, 0)), 0)
             / 1000000.0 AS cached_w,
       MAX(rate_from) AS rate_from,
       SUM(CASE WHEN ts IS NOT NULL AND rate_from IS NULL THEN 1 ELSE 0 END)
         AS unpriced_events,
       date(MIN(ts), 'unixepoch', 'localtime') AS first_seen,
       date(MAX(ts), 'unixepoch', 'localtime') AS last_activity
FROM priced GROUP BY path
ORDER BY classic_in + classic_out + cached_r + cached_w DESC, output DESC;
""").fetchall()
    return [dict(zip(STATS_KEYS, r)) for r in rows]


def split_cell(total, left, right, bold=False):
    t = fmt_usd(total)
    if bold:
        t = f"**{t}**"
    return f"{t} ({fmt_usd(left)} / {fmt_usd(right)})"


def render_project_stats(rows):
    out = ["| project | sessions | events | input | output | cache read |"
           " cache write | est. cost (input / output) |"
           " classic (input / output) | cached (read / write) |"
           " first seen | last activity |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    seed_seen = False
    for r in rows:
        project = r["name"] or Path(r["path"]).name
        classic = r["classic_in"] + r["classic_out"]
        cached = r["cached_r"] + r["cached_w"]
        if r["events"] == 0:
            est_cell = classic_cell = cached_cell = "—"
        elif r["rate_from"] is None or r["unpriced_events"] == r["events"]:
            est_cell = classic_cell = cached_cell = "unpriced"
        else:
            # est. cost input side = everything but generated output tokens
            est_cell = split_cell(classic + cached,
                                  r["classic_in"] + cached, r["classic_out"],
                                  bold=True)
            classic_cell = split_cell(classic, r["classic_in"],
                                      r["classic_out"])
            cached_cell = split_cell(cached, r["cached_r"], r["cached_w"])
            if r["rate_from"] == 0:
                seed_seen = True
            if 0 < r["unpriced_events"] < r["events"]:
                est_cell += (f" — {r['unpriced_events']} of {r['events']}"
                             " events unpriced")
        out.append(
            f"| {project} | {r['sessions']} | {fmt_n(r['events'])} |"
            f" {fmt_n(r['input'])} | {fmt_n(r['output'])} |"
            f" {fmt_n(r['cache_read'])} | {fmt_n(r['cache_write'])} |"
            f" {est_cell} | {classic_cell} | {cached_cell} |"
            f" {humanize(r['first_seen'])} | {humanize(r['last_activity'])} |")
    if seed_seen:
        out += ["", "Some rows price at the undated seed —"
                " `/token-telemetry:pricing-update` replaces it with dated"
                " published rates."]
    return "\n".join(out)


# ----------------------------------------------------------------- token-stats

# First-capture roll-ups of pre-telemetry history: excluded from every
# windowed figure (their timestamp is the capture day, not when the tokens
# were spent) but always included in all-time views. See TELEMETRY-CONTRACT.md.
NOT_BACKLOG = "COALESCE(note,'') <> 'backlog-capture'"
# "Today" means the reader's local day: SQLite's bare 'start of day' is UTC,
# which after local midnight reports the day before as today. Reports and the
# dashboard must agree on where a day starts.
LOCAL_TODAY = "ts >= strftime('%s','now','localtime','start of day','utc')"


def fetch_token_stats(conn):
    """Every breakdown the token-stats report shows, as plain data."""
    cw1h, cw1h_usd = priced_cte(conn)
    name = "p.name" if has_column(conn, "projects", "name") else "NULL"

    def totals(where):
        return conn.execute(
            "SELECT COALESCE(SUM(in_tok),0), COALESCE(SUM(out_tok),0),"
            " COALESCE(SUM(cache_r),0), COALESCE(SUM(cache_w),0), COUNT(*)"
            f" FROM events WHERE {where} AND {NOT_BACKLOG}").fetchone()

    d = {"today": totals(LOCAL_TODAY),
         "week": totals("ts >= strftime('%s','now','-7 days')")}
    d["backlog_excluded"] = conn.execute(
        "SELECT COUNT(*) FROM events"
        " WHERE ts >= strftime('%s','now','-7 days')"
        " AND COALESCE(note,'') = 'backlog-capture'").fetchone()[0]
    d["by_project"] = [
        (nm or Path(path).name, *rest) for path, nm, *rest in conn.execute(
            f"SELECT p.path, {name}, SUM(e.in_tok), SUM(e.out_tok),"
            " SUM(e.cache_r), SUM(e.cache_w), COUNT(*)"
            " FROM events e JOIN sessions s ON s.id = e.session_id"
            " JOIN projects p ON p.id = s.project_id"
            " WHERE e.ts >= strftime('%s','now','-7 days')"
            " GROUP BY p.path ORDER BY SUM(e.out_tok) DESC").fetchall()]
    d["by_agent"] = conn.execute(
        "SELECT COALESCE(agent, CASE kind WHEN 0 THEN 'main' ELSE 'subagent'"
        " END), SUM(in_tok), SUM(out_tok), COUNT(*)"
        " FROM events WHERE ts >= strftime('%s','now','-7 days')"
        f" AND {NOT_BACKLOG}"
        " GROUP BY 1 ORDER BY SUM(out_tok) DESC").fetchall()
    d["by_model"] = conn.execute(f"""
WITH priced AS (
  SELECT e.in_tok, e.out_tok, e.cache_r, e.cache_w, {cw1h} AS cache_w_1h,
         m.name AS model_name,
         {rate_subquery('in_usd')} AS in_usd,
         {rate_subquery('out_usd')} AS out_usd,
         {rate_subquery('cache_r_usd')} AS cache_r_usd,
         {rate_subquery('cache_w_usd')} AS cache_w_usd,
         {cw1h_usd} AS cache_w_1h_usd,
         {rate_subquery('effective_from')} AS rate_from
  FROM events e JOIN models m ON m.id = e.model_id
  WHERE e.ts >= strftime('%s','now','-7 days') AND {NOT_BACKLOG}
)
SELECT model_name,
       CASE
         WHEN model_name LIKE 'claude-fable-%' THEN 'orchestrator'
         WHEN model_name LIKE 'claude-opus-%' THEN 'heavy'
         WHEN model_name LIKE 'claude-sonnet-%' THEN 'small'
         WHEN model_name LIKE 'claude-haiku-%' THEN 'micro'
         ELSE 'unknown' END,
       SUM(in_tok), SUM(out_tok),
       ROUND(SUM(in_tok*COALESCE(in_usd,0) + out_tok*COALESCE(out_usd,0)
             + cache_r*COALESCE(cache_r_usd,0)
             + (cache_w - cache_w_1h)*COALESCE(cache_w_usd,0)
             + cache_w_1h*COALESCE(cache_w_1h_usd, cache_w_usd, 0))
             / 1000000.0, 4),
       MAX(rate_from)
FROM priced GROUP BY model_name ORDER BY SUM(out_tok) DESC;""").fetchall()
    d["by_kind"] = conn.execute(
        "SELECT CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END,"
        " SUM(in_tok), SUM(out_tok),"
        " ROUND(100.0 * SUM(cache_r) / NULLIF(SUM(in_tok) + SUM(cache_r), 0), 1)"
        " FROM events WHERE ts >= strftime('%s','now','-7 days')"
        f" AND {NOT_BACKLOG}"
        " GROUP BY kind").fetchall()
    d["by_tier"] = conn.execute(
        "SELECT CASE"
        " WHEN m.name LIKE 'claude-fable-%' THEN 'orchestrator'"
        " WHEN m.name LIKE 'claude-opus-%' THEN 'heavy'"
        " WHEN m.name LIKE 'claude-sonnet-%' THEN 'small'"
        " WHEN m.name LIKE 'claude-haiku-%' THEN 'micro'"
        " ELSE 'unknown' END,"
        " SUM(e.in_tok), SUM(e.out_tok), COUNT(*)"
        " FROM events e JOIN models m ON m.id = e.model_id"
        " WHERE e.ts >= strftime('%s','now','-7 days')"
        f" AND {NOT_BACKLOG}"
        " GROUP BY 1 ORDER BY SUM(e.out_tok) DESC").fetchall()
    d["by_issue"] = conn.execute(
        "SELECT issue_key, SUM(in_tok), SUM(out_tok), SUM(cache_r),"
        " SUM(cache_w), COUNT(*) FROM events WHERE issue_key IS NOT NULL"
        " GROUP BY issue_key ORDER BY SUM(out_tok) DESC").fetchall()
    return d


def md_table(header, align, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(align) + "|"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return out


def render_token_stats(d):
    def tok_row(label, t):
        return [label, fmt_n(t[0]), fmt_n(t[1]), fmt_n(t[2]), fmt_n(t[3]),
                str(t[4])]

    week_cost = sum(r[4] for r in d["by_model"])
    rates = [r[5] for r in d["by_model"] if r[5] is not None]
    if not rates:
        cost_label = "unpriced"
    elif min(rates) == 0:
        cost_label = f"{fmt_usd(week_cost)} (seed rates)"
    else:
        cost_label = (f"{fmt_usd(week_cost)} (rates as of "
                      + datetime.date.fromtimestamp(max(rates)).isoformat() + ")")
    out = [f"**Today: {fmt_n(d['today'][1])} output /"
           f" {fmt_n(d['today'][0])} input tokens, {d['today'][4]} events —"
           f" trailing 7 days est. cost {cost_label}.**", ""]
    out += md_table(
        ["window", "input", "output", "cache read", "cache write", "events"],
        ["---", "---:", "---:", "---:", "---:", "---:"],
        [tok_row("today", d["today"]), tok_row("7 days", d["week"])])
    out += ["", "**By project (7 days)**", ""]
    out += md_table(
        ["project", "input", "output", "cache read", "cache write", "events"],
        ["---", "---:", "---:", "---:", "---:", "---:"],
        [tok_row(r[0], r[1:]) for r in d["by_project"]])
    out += ["", "**By model (7 days)**", ""]
    model_rows = []
    for name, tier, inp, outp, cost, rate_from in d["by_model"]:
        label = ("unpriced" if rate_from is None
                 else fmt_usd(cost)
                 + (" (seed rates)" if rate_from == 0 else ""))
        model_rows.append([name, tier, fmt_n(inp), fmt_n(outp), label])
    out += md_table(["model", "tier", "input", "output", "est. cost"],
                    ["---", "---", "---:", "---:", "---:"], model_rows)
    out += ["", "**By agent and kind (7 days)**", ""]
    out += md_table(["agent", "input", "output", "events"],
                    ["---", "---:", "---:", "---:"],
                    [[r[0], fmt_n(r[1]), fmt_n(r[2]), str(r[3])]
                     for r in d["by_agent"]])
    out += [""]
    out += md_table(["kind", "input", "output", "cache hit %"],
                    ["---", "---:", "---:", "---:"],
                    [[r[0], fmt_n(r[1]), fmt_n(r[2]),
                      "—" if r[3] is None else f"{r[3]}%"]
                     for r in d["by_kind"]])
    out += ["", "**By tier (7 days)**", ""]
    out += md_table(["tier", "input", "output", "events"],
                    ["---", "---:", "---:", "---:"],
                    [[r[0], fmt_n(r[1]), fmt_n(r[2]), str(r[3])]
                     for r in d["by_tier"]])
    if d["by_issue"]:
        out += ["", "**By issue (all-time)**", ""]
        out += md_table(
            ["issue", "input", "output", "cache read", "cache write",
             "events"],
            ["---", "---:", "---:", "---:", "---:", "---:"],
            [tok_row(r[0], r[1:]) for r in d["by_issue"]])
    else:
        out += ["", "No issue-tagged events recorded (sidecar or"
                " `<KEY>:` commit-subject fallback)."]
    if d.get("backlog_excluded"):
        n = d["backlog_excluded"]
        out += ["", f"{n} backlog roll-up event(s) — first captures of"
                " pre-telemetry session history — are excluded from the"
                " windowed figures above (their timestamp is the capture day,"
                " not when the tokens were spent). All-time views"
                " (`/token-telemetry:project-stats`, the by-issue table)"
                " include them."]
    return "\n".join(out)


# ---------------------------------------------------------------- scoped rollup

# Conservative tracker-key shape: leading letter, then letters/digits/underscore,
# a hyphen, then digits — e.g. AOS-79. Keys reach a git subprocess (--grep) and
# SQL; validating here means an invalid key is rejected with a named message
# instead of ever being interpolated into either.
SCOPE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")


def parse_scope_keys(raw):
    """Split `KEY1,KEY2,...` on commas, strip whitespace, drop empties, and
    partition into (valid, invalid) preserving first-seen order and dropping
    duplicates from valid. Never raises — an unparseable/empty result is a
    normal outcome the caller renders, not an error."""
    valid, invalid = [], []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if SCOPE_KEY_RE.match(tok):
            if tok not in valid:
                valid.append(tok)
        else:
            invalid.append(tok)
    return valid, invalid


def commits_for_key(root, key):
    """Commit shas whose SUBJECT LINE starts with `<key>:`, per the
    contract's per-issue fallback recipe. `--grep` alone is not enough here:
    it matches the pattern anywhere in the full commit message, so a commit
    whose unrelated subject merely has a later paragraph starting with
    `<key>:` would be misattributed. `--grep` is kept only as a cheap
    prefilter; the real check is `subject.startswith(f"{key}:")` in Python
    against just the `%s` (subject) field. Both %h (short, repo-default
    abbreviation) and %H (full) sha are collected in one pass since a sha
    captured under one repo's abbreviation setting may not match `%h` under
    another. The `--grep` pattern is anchored and regex-escaped so a key can
    never inject regex — belt-and-suspenders on top of SCOPE_KEY_RE, whose
    charset already excludes every regex metacharacter."""
    pattern = "^" + re.escape(key) + ":"
    prefix = f"{key}:"
    sep = "\x1f"  # unit separator: won't collide with real commit-subject text
    out = capture.git(root, "log", f"--format=%h{sep}%H{sep}%s",
                      f"--grep={pattern}")
    shas = set()
    if out:
        for line in out.splitlines():
            parts = line.split(sep, 2)
            if len(parts) != 3:
                continue
            short, full, subject = parts
            if subject.startswith(prefix):
                shas.add(short)
                shas.add(full)
    return sorted(shas)


def _rowids_where(conn, project_path, clause, param):
    return [r[0] for r in conn.execute(
        "SELECT e.rowid FROM events e"
        " JOIN sessions s ON s.id = e.session_id"
        " JOIN projects p ON p.id = s.project_id"
        f" WHERE p.path = ? AND {clause}", (project_path, *param))]


def rowids_for_issue_key(conn, project_path, key):
    return _rowids_where(conn, project_path, "e.issue_key = ?", (key,))


def rowids_for_commit_shas(conn, project_path, shas):
    if not shas:
        return []
    placeholders = ",".join("?" for _ in shas)  # parameterized, never inlined
    return _rowids_where(conn, project_path,
                         f"e.commit_sha IN ({placeholders})", tuple(shas))


def priced_sum_for_rowids(conn, rowids):
    """Same per-event pricing as fetch_project_stats/fetch_token_stats,
    restricted to a caller-supplied set of event rowids."""
    empty = {"in_tok": 0, "out_tok": 0, "cache_r": 0, "cache_w": 0,
             "events": 0, "cost": 0.0, "rate_from": None, "unpriced": 0}
    if not rowids:
        return empty
    cw1h, cw1h_usd = priced_cte(conn)
    placeholders = ",".join("?" for _ in rowids)  # parameterized, never inlined
    row = conn.execute(f"""
WITH priced AS (
  SELECT e.in_tok, e.out_tok, e.cache_r, e.cache_w, {cw1h} AS cache_w_1h,
         {rate_subquery('in_usd')} AS in_usd,
         {rate_subquery('out_usd')} AS out_usd,
         {rate_subquery('cache_r_usd')} AS cache_r_usd,
         {rate_subquery('cache_w_usd')} AS cache_w_usd,
         {cw1h_usd} AS cache_w_1h_usd,
         {rate_subquery('effective_from')} AS rate_from
  FROM events e LEFT JOIN models m ON m.id = e.model_id
  WHERE e.rowid IN ({placeholders})
)
SELECT COALESCE(SUM(in_tok), 0), COALESCE(SUM(out_tok), 0),
       COALESCE(SUM(cache_r), 0), COALESCE(SUM(cache_w), 0), COUNT(*),
       COALESCE(SUM(in_tok * COALESCE(in_usd, 0))
             + SUM(out_tok * COALESCE(out_usd, 0))
             + SUM(cache_r * COALESCE(cache_r_usd, 0))
             + SUM((cache_w - cache_w_1h) * COALESCE(cache_w_usd, 0))
             + SUM(cache_w_1h * COALESCE(cache_w_1h_usd, cache_w_usd, 0)),
             0) / 1000000.0,
       MAX(rate_from),
       SUM(CASE WHEN rate_from IS NULL THEN 1 ELSE 0 END)
FROM priced""", rowids).fetchone()
    inp, outp, cr, cw, n, cost, rate_from, unpriced = row
    return {"in_tok": inp, "out_tok": outp, "cache_r": cr, "cache_w": cw,
            "events": n, "cost": cost or 0.0, "rate_from": rate_from,
            "unpriced": unpriced or 0}


def fetch_scoped_rollup(conn, cwd, scope_raw):
    """Caller-supplied issue-key-set scoping (the kit contract's per-issue
    recipe, summed across the set) — replaces the old milestone-branch-prefix
    grouping, which gitflow (kit v0.22.0) makes match nothing. Returns plain
    data; render_scoped_rollup turns it into the three-state
    empty-vs-broken-vs-covered markdown."""
    valid, invalid = parse_scope_keys(scope_raw)
    if not valid:
        return {"state": "empty_keyset", "invalid": invalid}
    if conn is None:
        return {"state": "absent", "keys": valid, "invalid": invalid}
    root = capture.find_project_root(cwd)
    project_events = conn.execute(
        "SELECT COUNT(*) FROM events e"
        " JOIN sessions s ON s.id = e.session_id"
        " JOIN projects p ON p.id = s.project_id WHERE p.path = ?",
        (str(root),)).fetchone()[0]
    if not project_events:
        return {"state": "absent", "keys": valid, "invalid": invalid}

    per_key = []
    for key in valid:
        rowids = rowids_for_issue_key(conn, str(root), key)
        if not rowids:
            shas = commits_for_key(root, key)
            rowids = rowids_for_commit_shas(conn, str(root), shas)
        per_key.append({"key": key, **priced_sum_for_rowids(conn, rowids)})

    covered = [k for k in per_key if k["events"] > 0]
    n, k = len(valid), len(covered)
    if k == 0:
        return {"state": "broken", "keys": valid, "invalid": invalid,
                "n": n, "k": k}
    totals = {
        "in_tok": sum(x["in_tok"] for x in covered),
        "out_tok": sum(x["out_tok"] for x in covered),
        "cache_r": sum(x["cache_r"] for x in covered),
        "cache_w": sum(x["cache_w"] for x in covered),
        "events": sum(x["events"] for x in covered),
        "cost": sum(x["cost"] for x in covered),
        "rate_from": [x["rate_from"] for x in covered],
        "unpriced": sum(x["unpriced"] for x in covered),
    }
    return {"state": "partial" if k < n else "full", "keys": valid,
            "invalid": invalid, "n": n, "k": k, **totals}


INVALID_ECHO_MAX = 32


def sanitize_invalid_echo(tok):
    """A rejected --scope token is arbitrary caller input, about to be
    echoed into rendered markdown — it must never carry backticks, pipes,
    or newlines that could break out of the inline-code span (or the wider
    table/document) it's shown in. Strip to a conservative safe charset and
    cap the length so one hostile token can't blow up the render."""
    cleaned = re.sub(r"[^A-Za-z0-9_,-]", "", tok)
    if len(cleaned) > INVALID_ECHO_MAX:
        cleaned = cleaned[:INVALID_ECHO_MAX] + "…"
    return cleaned or "(unprintable)"


def render_scoped_rollup(d):
    out = ["", "**Scoped rollup**", ""]
    if d["invalid"]:
        shown = ", ".join(f"`{sanitize_invalid_echo(t)}`" for t in d["invalid"])
        out.append(f"Rejected invalid scope key(s): {shown}"
                   " (must match `KEY-123`).")
    if d["state"] == "empty_keyset":
        out.append("scope resolution failed — empty key set")
        return "\n".join(out)
    if d["state"] == "absent":
        out.append("telemetry absent")
        return "\n".join(out)
    if d["state"] == "broken":
        out.append(f"0 of {d['n']} scoped issues have telemetry rows"
                   " (broken scope until proven otherwise).")
        return "\n".join(out)
    # partial or full coverage: a sum across the covered keys in the set
    rates = [r for r in d["rate_from"] if r is not None]
    if not rates:
        cost_label = "unpriced"
    elif min(rates) == 0:
        cost_label = f"{fmt_usd(d['cost'])} (seed rates)"
    else:
        cost_label = fmt_usd(d["cost"])
    out.append(f"**{cost_label}** — {fmt_n(d['events'])} events,"
               f" {fmt_n(d['in_tok'])} input / {fmt_n(d['out_tok'])} output"
               " tokens.")
    if d["k"] < d["n"]:
        out.append(f"{d['k']} of {d['n']} issues have rows.")
    return "\n".join(out)


# -------------------------------------------------------------- storage-status

def db_family_size(path):
    """DB file plus its -wal/-shm siblings; unchecked-pointed WAL can hold a
    large share of the data."""
    total = 0
    for p in (path, f"{path}-wal", f"{path}-shm"):
        try:
            total += Path(p).stat().st_size
        except OSError:
            pass
    return total


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def fetch_storage_status(conn, db):
    d = {"db": str(db), "size": db_family_size(db),
         "schema": conn.execute("PRAGMA user_version").fetchone()[0],
         "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    mirror = {"mirror_path", "mirror_last_at"} <= cols
    d["pre_v3"] = not mirror
    mp = "p.mirror_path" if mirror else "NULL"
    ml = "p.mirror_last_at" if mirror else "NULL"
    d["projects"] = []
    for path, events, mirror_path, mirror_last in conn.execute(
            f"SELECT p.path, COUNT(e.rowid), {mp}, {ml}"
            " FROM projects p"
            " LEFT JOIN sessions s ON s.project_id = p.id"
            " LEFT JOIN events e ON e.session_id = s.id"
            " GROUP BY p.id, p.path ORDER BY COUNT(e.rowid) DESC"):
        row = {"path": path, "events": events, "mirror_path": mirror_path,
               "mirror_last_at": mirror_last, "mirror_size": None}
        if mirror_path:
            row["mirror_size"] = (db_family_size(mirror_path)
                                  if Path(mirror_path).exists() else None)
        d["projects"].append(row)
    d["audit"] = (conn.execute(
        "SELECT datetime(ts,'unixepoch','localtime'), action, project, detail"
        " FROM audit_log ORDER BY ts DESC LIMIT 5").fetchall()
        if has_column(conn, "audit_log", "action") else [])
    err = Path(db).parent / "error.log"
    d["error_log"] = None
    if err.exists():
        st = err.stat()
        d["error_log"] = {"size": st.st_size,
                          "mtime": datetime.datetime.fromtimestamp(st.st_mtime)
                          .strftime("%Y-%m-%d %H:%M")}
    return d


def render_storage_status(d):
    out = ["### Central DB", "",
           "| path | size (incl. -wal/-shm) | events | schema |",
           "|---|---|---:|---|",
           f"| `{d['db']}` | {fmt_bytes(d['size'])} | {fmt_n(d['events'])} |"
           f" v{d['schema']} |",
           "", "### Projects", "",
           "| project | events | mirror? | mirror size | last mirrored |",
           "|---|---:|---|---|---|"]
    any_mirror = False
    for p in d["projects"]:
        if p["mirror_path"]:
            any_mirror = True
            size = (fmt_bytes(p["mirror_size"]) if p["mirror_size"] is not None
                    else "not accessible on this machine")
            last = (humanize(datetime.date.fromtimestamp(
                p["mirror_last_at"]).isoformat())
                if p["mirror_last_at"] else "—")
            out.append(f"| `{p['path']}` | {fmt_n(p['events'])} | yes |"
                       f" {size} | {last} |")
        else:
            out.append(f"| `{p['path']}` | {fmt_n(p['events'])} | no | — | — |")
    if d["pre_v3"]:
        out += ["", "This DB predates the mirror columns (schema < 3); it"
                " upgrades on its next captured turn."]
    if any_mirror:
        out += ["", "`last mirrored` is configured state, not a write receipt"
                " — a recent value with a missing or stale mirror file means"
                " mirror writes are failing (check the error log)."]
    if d["error_log"]:
        out += ["", f"Error log: {d['error_log']['size']} bytes, last written"
                f" {d['error_log']['mtime']} (capture and mirror failures land"
                " there)."]
    if d["audit"]:
        out += ["", "Last storage-management actions:", ""]
        out += md_table(["at", "action", "project", "detail"],
                        ["---", "---", "---", "---"],
                        [[a, b, f"`{c}`", e or ""] for a, b, c, e in d["audit"]])
    return "\n".join(out)


# ------------------------------------------------------------------------ info

def fetch_info(conn, db, cwd):
    """Everything the status block needs, as one plain dict."""
    root = capture.find_project_root(cwd)
    plugin = json.loads((Path(__file__).resolve().parent.parent
                         / ".claude-plugin" / "plugin.json").read_text())
    d = {"db": str(db), "root": str(root),
         "plugin_name": plugin["name"], "plugin_version": plugin["version"],
         "enabled": capture.is_enabled(cwd), "storage_mode": None,
         "mode_explicit": False, "sidecar": None, "central": None,
         "events_here": None, "error_log": None, "mirror": None}

    if d["enabled"]:
        d["storage_mode"] = capture.read_storage_mode(root)
        try:
            first = (Path(root) / ".claude" / "telemetry").read_text() \
                .splitlines()[0].strip().lower()
            d["mode_explicit"] = first in ("central", "project")
        except (OSError, IndexError):
            pass

    sidecar = Path(root) / ".claude" / "telemetry-context.json"
    if sidecar.exists():
        try:
            sc = json.loads(sidecar.read_text())
            d["sidecar"] = {"issue_key": sc.get("issue_key"),
                            "size": sc.get("size")}
        except (OSError, ValueError):
            d["sidecar"] = "unreadable"

    if conn is not None:
        events, first_day, last_day = conn.execute(
            "SELECT COUNT(*), MIN(date(ts,'unixepoch','localtime')),"
            " MAX(date(ts,'unixepoch','localtime')) FROM events").fetchone()
        # Latest rate already in force — a pre-inserted future-dated row (e.g.
        # a published price change) must not masquerade as the current rate.
        pricing_rows, latest = conn.execute(
            "SELECT COUNT(*), MAX(CASE WHEN effective_from <="
            " strftime('%s','now') THEN effective_from END) FROM pricing"
        ).fetchone()
        d["central"] = {
            "schema": conn.execute("PRAGMA user_version").fetchone()[0],
            "events": events, "first_day": first_day, "last_day": last_day,
            "projects": conn.execute(
                "SELECT COUNT(*) FROM projects").fetchone()[0],
            "pricing_rows": pricing_rows, "latest_rate_from": latest,
        }
        d["events_here"] = conn.execute(
            "SELECT COUNT(*) FROM events e"
            " JOIN sessions s ON s.id = e.session_id"
            " JOIN projects p ON p.id = s.project_id WHERE p.path = ?",
            (str(root),)).fetchone()[0]

    err = Path(db).parent / "error.log"
    if err.exists():
        st = err.stat()
        d["error_log"] = {"size": st.st_size, "mtime": datetime.datetime
                          .fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")}

    if d["enabled"] and d["storage_mode"] == capture.STORAGE_PROJECT:
        mirror = capture.mirror_db_path(root)
        mconn = open_ro(mirror)
        if mconn is None:
            d["mirror"] = {"path": str(mirror), "exists": False}
        else:
            d["mirror"] = {
                "path": str(mirror), "exists": True,
                "schema": mconn.execute("PRAGMA user_version").fetchone()[0],
                "events": mconn.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0],
            }
            mconn.close()
    return d


def render_info(d):
    out = ["| | |", "|---|---|",
           f"| plugin | {d['plugin_name']} v{d['plugin_version']} |"]
    if d["enabled"]:
        explicit = "" if d["mode_explicit"] else " (default)"
        out.append(f"| project | `{d['root']}` — telemetry **enabled**,"
                   f" {d['storage_mode']} storage{explicit} |")
    else:
        out.append(f"| project | `{d['root']}` — telemetry **off**"
                   " (enable with `/token-telemetry:enable`) |")

    if d["sidecar"] == "unreadable":
        out.append("| sidecar | present but unreadable |")
    elif d["sidecar"]:
        out.append(f"| sidecar | issue_key={d['sidecar']['issue_key']},"
                   f" size={d['sidecar']['size']} |")
    else:
        out.append("| sidecar | none |")

    c = d["central"]
    if c is None:
        out.append(f"| central DB | `{d['db']}` — does not exist"
                   " (no telemetry recorded yet) |")
    else:
        latest = c["latest_rate_from"]
        rates = ("none" if latest is None
                 else "seed rates (undated)" if latest == 0
                 else "rates " + datetime.date.fromtimestamp(latest).isoformat())
        span = (f" ({c['first_day']} → {c['last_day']})" if c["events"] else "")
        out.append(f"| central DB | `{d['db']}` — schema v{c['schema']},"
                   f" {fmt_n(c['events'])} events{span},"
                   f" {c['projects']} projects,"
                   f" {c['pricing_rows']} pricing rows ({rates}) |")
        out.append(f"| this project | {fmt_n(d['events_here'])} events in the"
                   " central DB |")

    e = d["error_log"]
    out.append(f"| error log | {e['size']} bytes, last written {e['mtime']} |"
               if e else "| error log | none |")

    m = d["mirror"]
    if m is not None:
        if not m["exists"]:
            out.append(f"| mirror DB | `{m['path']}` — not written yet"
                       " (created on the first captured turn) |")
        else:
            note = ""
            if d["events_here"] is not None and m["events"] > d["events_here"]:
                note = (f" — more than central's {fmt_n(d['events_here'])} for"
                        " this project: replayed rows (mirror keeps no"
                        " cursors) or teammates' rows in a committed mirror;"
                        " not corruption")
            out.append(f"| mirror DB | `{m['path']}` — schema v{m['schema']},"
                       f" {fmt_n(m['events'])} events{note} |")

    if d["enabled"] and (d["events_here"] or 0) == 0:
        out += ["", "Enabled but no events for this project yet — most likely"
                " the capture hooks were not loaded when this session started."
                " Restart Claude Code; capture begins next session."]
    if c is not None and c["latest_rate_from"] == 0:
        out += ["", "Pricing is at the undated seed —"
                " `/token-telemetry:pricing-update` replaces it with dated"
                " published rates."]
    return "\n".join(out)


# ------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="report.py")
    ap.add_argument("command", choices=["info", "project-stats", "token-stats",
                                        "storage-status"])
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--db", default=None)
    ap.add_argument("--scope", default=None,
                    help="comma-separated issue keys (KEY1,KEY2,...) —"
                         " renders a Scoped rollup summed across the set"
                         " instead of the normal command output")
    args = ap.parse_args(argv)
    db = args.db or capture.db_path()
    conn = open_ro(db)
    try:
        if args.scope is not None:
            print(render_scoped_rollup(fetch_scoped_rollup(conn, args.cwd,
                                                            args.scope)))
        elif args.command == "info":
            print(render_info(fetch_info(conn, db, args.cwd)))
        elif conn is None:
            print("No telemetry has been recorded yet — enable capture"
                  " for a project with `/token-telemetry:enable` (and"
                  " restart Claude Code so the hooks load).")
        elif args.command == "project-stats":
            print(render_project_stats(fetch_project_stats(conn)))
        elif args.command == "storage-status":
            print(render_storage_status(fetch_storage_status(conn, db)))
        else:
            print(render_token_stats(fetch_token_stats(conn)))
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
