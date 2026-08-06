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

STATS_KEYS = ("project", "sessions", "events", "input", "output",
              "est_cost_usd", "rate_from", "unpriced_events",
              "first_seen", "last_activity")


def fetch_project_stats(conn):
    """One dict per project, ordered by estimated cost. Each event prices at
    the rate in force at its own timestamp (see docs/TELEMETRY-CONTRACT.md)."""
    # Pre-v4 DBs (not yet migrated by a capture) lack the cache-TTL split;
    # treat the 1h portion as 0 there, which is exactly the pre-v4 estimate.
    cw1h = "e.cache_w_1h" if has_column(conn, "events", "cache_w_1h") else "0"
    cw1h_usd = (rate_subquery("cache_w_1h_usd")
                if has_column(conn, "pricing", "cache_w_1h_usd") else "NULL")
    rows = conn.execute(f"""
WITH priced AS (
  SELECT p.path AS project, s.id AS session_id, e.ts AS ts,
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
SELECT project,
       COUNT(DISTINCT session_id) AS sessions,
       COUNT(ts) AS events,
       COALESCE(SUM(in_tok), 0) AS input,
       COALESCE(SUM(out_tok), 0) AS output,
       ROUND(COALESCE(SUM(in_tok * COALESCE(in_usd, 0)
             + out_tok * COALESCE(out_usd, 0)
             + cache_r * COALESCE(cache_r_usd, 0)
             + (cache_w - cache_w_1h) * COALESCE(cache_w_usd, 0)
             + cache_w_1h * COALESCE(cache_w_1h_usd, cache_w_usd, 0)), 0)
             / 1000000.0, 4) AS est_cost_usd,
       MAX(rate_from) AS rate_from,
       SUM(CASE WHEN ts IS NOT NULL AND rate_from IS NULL THEN 1 ELSE 0 END)
         AS unpriced_events,
       date(MIN(ts), 'unixepoch') AS first_seen,
       date(MAX(ts), 'unixepoch') AS last_activity
FROM priced GROUP BY project
ORDER BY est_cost_usd DESC, output DESC;""").fetchall()
    return [dict(zip(STATS_KEYS, r)) for r in rows]


def render_project_stats(rows):
    out = ["| project | sessions | events | input | output | est. cost |"
           " first seen | last activity |",
           "|---|---|---:|---:|---:|---:|---|---|"]
    seed_seen = False
    for r in rows:
        if r["events"] == 0:
            cost_cell = "—"
        elif r["rate_from"] is None or r["unpriced_events"] == r["events"]:
            cost_cell = "unpriced"
        else:
            if r["rate_from"] == 0:
                label, seed_seen = "seed rates", True
            else:
                label = "rates " + datetime.date.fromtimestamp(
                    r["rate_from"]).isoformat()
            if 0 < r["unpriced_events"] < r["events"]:
                label += (f", {r['unpriced_events']} of {r['events']}"
                          " events unpriced")
            cost_cell = f"${r['est_cost_usd']:.4f} ({label})"
        out.append(
            f"| `{r['project']}` | {r['sessions']} | {fmt_n(r['events'])} |"
            f" {fmt_n(r['input'])} | {fmt_n(r['output'])} | {cost_cell} |"
            f" {humanize(r['first_seen'])} | {humanize(r['last_activity'])} |")
    if seed_seen:
        out += ["", "Some rows price at the undated seed —"
                " `/token-telemetry:pricing-update` replaces it with dated"
                " published rates."]
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
            "SELECT COUNT(*), MIN(date(ts,'unixepoch')),"
            " MAX(date(ts,'unixepoch')) FROM events").fetchone()
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
    ap.add_argument("command", choices=["info", "project-stats"])
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    db = args.db or capture.db_path()
    conn = open_ro(db)
    try:
        if args.command == "project-stats":
            if conn is None:
                print("No telemetry has been recorded yet — enable capture"
                      " for a project with `/token-telemetry:enable` (and"
                      " restart Claude Code so the hooks load).")
            else:
                print(render_project_stats(fetch_project_stats(conn)))
        else:
            print(render_info(fetch_info(conn, db, args.cwd)))
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
