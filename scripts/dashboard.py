#!/usr/bin/env python3
"""Local interactive dashboard for the token-telemetry store.

Read-only, zero-dependency (Python 3 stdlib only). Serves a single-page
frontend plus a JSON API that computes every KPI, breakdown and cost total
**server-side from usage.db** — the browser receives aggregates and one page of
events, never the raw record set. This is the query-backed counterpart to the
markdown `report.py`; both open the DB `mode=ro` and price each event at the
rate in force at its own timestamp (see docs/TELEMETRY-CONTRACT.md).

Subcommands:
  dashboard.py open  [--port N] [--no-browser]   launch (or reattach) + open browser
  dashboard.py serve [--port N]                  run the blocking server (internal)
  dashboard.py stop                              stop a backgrounded server

`open` spawns `serve` as a detached background process so the caller returns
immediately; a runtime file next to the DB records pid+port so a second `open`
reattaches instead of starting a duplicate.
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture

HERE = Path(__file__).resolve().parent
HTML = HERE / "dashboard.html"
LOGO = HERE / "dashboard-assets" / "marvin-wordmark.png"
FAVICON = HERE / "dashboard-assets" / "favicon.png"

# rolling windows, in days; the period filter maps onto these. Default: week.
PERIODS = {"day": 1, "week": 7, "month": 30, "year": 365}
DEFAULT_PERIOD = "week"

# The page auto-refreshes every 5 minutes; with no request for longer than this
# (two missed cycles + margin) the backgrounded server assumes every tab is gone
# and self-terminates, so nothing lingers after the dashboard is closed.
IDLE_TIMEOUT = 11 * 60
_ACTIVITY = [time.time()]   # wall-clock of the last handled request (mutable cell)

# events sort key -> derived row field; whitelisted so nothing user-supplied
# ever reaches a column name.
SORT_MAP = {
    "ts": "ts", "project": "project", "model": "modelName", "agent": "agent",
    "kind": "kind", "total": "total", "cost": "cost", "consumed": "consumed",
    "cachetok": "cachetok", "costConsumed": "costConsumed", "costCache": "costCache",
    "calls": "calls", "ctx": "ctx",
}

# ------------------------------------------------------------------ DB access

def runtime_path():
    return Path(capture.db_path()).parent / "dashboard.runtime.json"


def open_ro():
    """Read-only connection to the telemetry store, or None when absent."""
    db = capture.db_path()
    if not Path(db).exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rate(col):
    """Correlated subquery resolving one pricing column for event `e` / model
    `m` at the event's own timestamp — longest matching prefix, latest rate in
    force. Mirrors report.py so costs are identical across both surfaces."""
    return (f"(SELECT pr.{col} FROM pricing pr "
            f"WHERE m.name LIKE pr.model_prefix || '%' AND pr.effective_from <= e.ts "
            f"ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC LIMIT 1)")


def _pretty_model(name):
    if not name or not name.startswith("claude-"):
        return name or "unknown"
    parts = name[len("claude-"):].split("-")
    fam = parts[0].capitalize()
    nums = [p for p in parts[1:] if p.isdigit()]
    if len(nums) >= 2:
        return f"{fam} {nums[0]}.{nums[1]}"
    if len(nums) == 1:
        return f"{fam} {nums[0]}"
    return fam


def _pretty_project(name, path):
    if name and name.strip():
        return name
    base = os.path.basename((path or "").rstrip("/"))
    return base or (path or "unknown")


def fetch_domains(conn, since):
    """Distinct models / agents / projects present in the window, ignoring the
    active selection so every filter option stays visible."""
    models = [r[0] for r in conn.execute(
        "SELECT DISTINCT m.name FROM events e JOIN models m ON m.id=e.model_id "
        "WHERE e.ts >= ? ORDER BY m.name", (since,))]
    agents = [r[0] for r in conn.execute(
        "SELECT DISTINCT COALESCE(e.agent,'main') a FROM events e "
        "WHERE e.ts >= ? ORDER BY a", (since,))]
    projects = [{"key": r["path"], "name": _pretty_project(r["name"], r["path"])}
                for r in conn.execute(
        "SELECT DISTINCT p.path, p.name FROM events e "
        "JOIN sessions s ON s.id=e.session_id JOIN projects p ON p.id=s.project_id "
        "WHERE e.ts >= ? ORDER BY p.path", (since,))]
    return {
        "models": [{"key": m, "name": _pretty_model(m)} for m in models],
        "agents": agents,
        "projects": projects,
    }


def fetch_rows(conn, since, models, agents):
    """Priced per-event rows in the window, filtered by model/agent in SQL.
    SQL resolves each pricing rate; Python derives every token/cost split so the
    composition panel and all breakdowns share one source of truth. Project
    filtering is applied later in Python so the project table can still show
    every project under the current model/agent filter."""
    # Backlog roll-ups (first captures of pre-telemetry history, note =
    # 'backlog-capture') are excluded from every dashboard window: their
    # timestamp is the capture day, not when the tokens were spent. All-time
    # truth including them lives in /token-telemetry:project-stats.
    where = ["e.ts >= ?", "COALESCE(e.note,'') <> 'backlog-capture'"]
    params = [since]
    if models:
        where.append("m.name IN (%s)" % ",".join("?" * len(models)))
        params += models
    if agents:
        where.append("COALESCE(e.agent,'main') IN (%s)" % ",".join("?" * len(agents)))
        params += agents
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    calls_col = "e.api_calls" if "api_calls" in cols else "NULL"
    ctx_col = "e.ctx_tokens" if "ctx_tokens" in cols else "NULL"
    sql = f"""
      SELECT e.ts AS ts, m.name AS model, COALESCE(e.agent,'main') AS agent,
             p.name AS pname, p.path AS ppath, e.kind AS kind, s.uuid AS session,
             e.in_tok AS in_tok, e.out_tok AS out_tok, e.cache_r AS cache_r,
             e.cache_w AS cache_w, e.cache_w_1h AS cache_w_1h,
             {calls_col} AS api_calls, {ctx_col} AS ctx_tokens,
             {_rate('in_usd')} AS r_in, {_rate('out_usd')} AS r_out,
             {_rate('cache_r_usd')} AS r_cr, {_rate('cache_w_usd')} AS r_cw,
             {_rate('cache_w_1h_usd')} AS r_cw1h
      FROM events e
      JOIN models m ON m.id=e.model_id
      JOIN sessions s ON s.id=e.session_id
      JOIN projects p ON p.id=s.project_id
      WHERE {" AND ".join(where)}
      ORDER BY e.ts
    """
    rows = []
    for r in conn.execute(sql, params):
        in_t, out_t = r["in_tok"] or 0, r["out_tok"] or 0
        cr, cw, cw1 = r["cache_r"] or 0, r["cache_w"] or 0, r["cache_w_1h"] or 0
        r_in, r_out = r["r_in"] or 0.0, r["r_out"] or 0.0
        r_cr, r_cw = r["r_cr"] or 0.0, r["r_cw"] or 0.0
        r_cw1h = r["r_cw1h"] if r["r_cw1h"] is not None else r_cw
        cost_in = in_t * r_in / 1e6
        cost_out = out_t * r_out / 1e6
        cost_cr = cr * r_cr / 1e6
        cost_cw = ((cw - cw1) * r_cw + cw1 * r_cw1h) / 1e6
        rows.append({
            "ts": r["ts"], "model": r["model"], "modelName": _pretty_model(r["model"]),
            "agent": r["agent"], "project": _pretty_project(r["pname"], r["ppath"]),
            "projectKey": r["ppath"], "kind": r["kind"], "session": r["session"],
            "in": in_t, "out": out_t, "cache_r": cr, "cache_w": cw,
            "calls": r["api_calls"], "ctx": r["ctx_tokens"],
            "consumed": in_t + out_t, "cachetok": cr + cw,
            "total": in_t + out_t + cr + cw,
            "costIn": cost_in, "costOut": cost_out, "costCacheR": cost_cr,
            "costCacheW": cost_cw,
            "costConsumed": cost_in + cost_out, "costCache": cost_cr + cost_cw,
            "cost": cost_in + cost_out + cost_cr + cost_cw,
        })
    return rows


# ---------------------------------------------------------------- aggregation

_AGG_FIELDS = ("cost", "total", "consumed", "cachetok", "costConsumed",
               "costCache", "in", "out", "cache_r", "cache_w",
               "costIn", "costOut", "costCacheR", "costCacheW")


def _group(rows, key, name_of):
    out = {}
    for r in rows:
        k = r[key]
        g = out.get(k)
        if g is None:
            g = out[k] = {"key": k, "name": name_of(r), "n": 0,
                          **{f: 0 for f in _AGG_FIELDS}}
        g["n"] += 1
        for f in _AGG_FIELDS:
            g[f] += r[f]
    return sorted(out.values(), key=lambda g: g["cost"], reverse=True)


def _sum(rows, f):
    return sum(r[f] for r in rows)


def _composition(rows):
    tok = {"in": _sum(rows, "in"), "out": _sum(rows, "out"),
           "cache_r": _sum(rows, "cache_r"), "cache_w": _sum(rows, "cache_w")}
    cost = {"in": _sum(rows, "costIn"), "out": _sum(rows, "costOut"),
            "cache_r": _sum(rows, "costCacheR"), "cache_w": _sum(rows, "costCacheW")}
    return {"tokens": tok, "cost": cost}


def _timeline(rows):
    pts = sorted(rows, key=lambda r: r["ts"])
    if len(pts) <= 1500:
        return [{"ts": r["ts"], "cost": r["cost"], "total": r["total"],
                 "consumed": r["consumed"], "cachetok": r["cachetok"],
                 "model": r["modelName"]} for r in pts]
    buckets = {}  # large window: bucket by day so the line stays bounded
    for r in pts:
        day = (r["ts"] // 86400) * 86400
        b = buckets.setdefault(day, {"ts": day, "cost": 0.0, "total": 0,
                                     "consumed": 0, "cachetok": 0, "model": ""})
        for f in ("cost", "total", "consumed", "cachetok"):
            b[f] += r[f]
    return [buckets[k] for k in sorted(buckets)]


def _event_dto(r):
    return {"ts": r["ts"], "project": r["project"], "model": r["model"],
            "modelName": r["modelName"], "agent": r["agent"], "kind": r["kind"],
            "total": r["total"], "cost": r["cost"], "consumed": r["consumed"],
            "cachetok": r["cachetok"], "costConsumed": r["costConsumed"],
            "costCache": r["costCache"],
            "in": r["in"], "out": r["out"],
            "cache_r": r["cache_r"], "cache_w": r["cache_w"],
            "costIn": r["costIn"], "costOut": r["costOut"],
            "costCacheR": r["costCacheR"], "costCacheW": r["costCacheW"],
            "calls": r["calls"], "ctx": r["ctx"]}


def build_data(conn, q):
    def one(name, default):
        return (q.get(name, [default])[0]) or default

    def listparam(name):
        vals = []
        for chunk in q.get(name, []):
            vals += [v for v in chunk.split(",") if v]
        return vals

    period = one("period", DEFAULT_PERIOD)
    if period not in PERIODS:
        period = DEFAULT_PERIOD
    now = int(time.time())
    since = now - PERIODS[period] * 86400

    models, agents = listparam("models"), listparam("agents")
    project = one("project", "") or None
    sort = SORT_MAP.get(one("sort", "ts"), "ts")
    reverse = one("dir", "desc") == "desc"
    try:
        page = max(0, int(one("page", "0")))
    except ValueError:
        page = 0
    try:
        page_size = min(500, max(1, int(one("pageSize", "12"))))
    except ValueError:
        page_size = 12

    domains = fetch_domains(conn, since)
    base_rows = fetch_rows(conn, since, models, agents)   # window + model/agent
    backlog_excluded = conn.execute(
        "SELECT COUNT(*) FROM events WHERE ts >= ?"
        " AND COALESCE(note,'') = 'backlog-capture'", (since,)).fetchone()[0]
    by_project = _group(base_rows, "projectKey", name_of=lambda r: r["project"])
    rows = [r for r in base_rows if project is None or r["projectKey"] == project]

    total, cachetok, cost = _sum(rows, "total"), _sum(rows, "cachetok"), _sum(rows, "cost")
    kpis = {
        "cost": cost, "total": total, "consumed": _sum(rows, "consumed"),
        "cachetok": cachetok, "out": _sum(rows, "out"),
        "outCost": _sum(rows, "costOut"),
        "events": len(rows), "sessions": len({r["session"] for r in rows}),
        "projects": len({r["projectKey"] for r in rows}),
        "cachePct": (cachetok / total * 100) if total else 0.0,
        "outCostPct": (_sum(rows, "costOut") / cost * 100) if cost else 0.0,
    }

    # None-safe sort: pre-v6 rows carry NULL calls/ctx and must not crash it
    ev = sorted(rows, key=lambda r: (r[sort] is None, r[sort]), reverse=reverse)
    start = page * page_size
    return {
        "period": period,
        "domains": domains,
        "kpis": kpis,
        "byModel": _group(rows, "model", name_of=lambda r: r["modelName"]),
        "byAgent": _group(rows, "agent", name_of=lambda r: r["agent"]),
        "byProject": by_project,
        "composition": _composition(rows),
        "timeline": _timeline(rows),
        "events": {
            "rows": [_event_dto(r) for r in ev[start:start + page_size]],
            "total": len(rows), "start": start, "pageSize": page_size, "page": page,
            "sums": {"cost": cost, "total": total},
        },
        "backlogExcluded": backlog_excluded,
        "generatedAt": now,
    }


# ------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet; the server runs backgrounded

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            self._send(200, path.read_bytes(), ctype)
        except OSError:
            self._send(404, b"not found", "text/plain")

    def do_GET(self):
        _ACTIVITY[0] = time.time()   # keep-alive: any request defers idle shutdown
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send_file(HTML, "text/html; charset=utf-8")
        if u.path == "/logo.png":
            return self._send_file(LOGO, "image/png")
        if u.path in ("/favicon.png", "/favicon.ico"):
            return self._send_file(FAVICON, "image/png")
        if u.path == "/api/data":
            return self._api_data(parse_qs(u.query))
        self._send(404, b"not found", "text/plain")

    def _api_data(self, q):
        conn = open_ro()
        if conn is None:
            return self._send(200, json.dumps({"error": "no telemetry database found"}).encode(),
                              "application/json")
        try:
            data = build_data(conn, q)
        finally:
            conn.close()
        self._send(200, json.dumps(data).encode(), "application/json")


# --------------------------------------------------------------- process mgmt

def _free_port(preferred):
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return preferred


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _read_runtime():
    try:
        return json.loads(runtime_path().read_text())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _open_browser(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def cmd_serve(port):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    _ACTIVITY[0] = time.time()
    stop = threading.Event()

    def watchdog():
        # poll on a short cadence; shut the server down once the dashboard has
        # gone quiet for IDLE_TIMEOUT (shutdown() must run off the serve thread).
        while not stop.wait(30):
            if time.time() - _ACTIVITY[0] > IDLE_TIMEOUT:
                httpd.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        # The runtime marker is deliberately left in place: `dashboard-restart`
        # reuses its port so the still-open browser tab reconnects to the same
        # URL. A stale marker (dead pid) is harmless — `open`'s reattach check
        # requires a live pid, and `stop` clears the file explicitly.


def _spawn_detached(port):
    """Start `serve` on `port` as a detached background process; record pid+port."""
    logf = open(runtime_path().parent / "dashboard.log", "a")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(port)],
        stdout=logf, stderr=logf, start_new_session=True)
    for _ in range(60):
        if _port_open(port):
            break
        time.sleep(0.1)
    runtime_path().write_text(json.dumps(
        {"pid": proc.pid, "port": port, "started": int(time.time()),
         "script": str(Path(__file__).resolve())}))
    return proc.pid


def _stop_running(rt, port):
    """SIGTERM a live server from `rt` and wait for `port` to be released."""
    if rt and _pid_alive(rt.get("pid", -1)):
        try:
            os.kill(rt["pid"], signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not _port_open(port):
                break
            time.sleep(0.1)


def cmd_open(port, no_browser):
    rt = _read_runtime()
    if rt and _pid_alive(rt.get("pid", -1)) and _port_open(rt.get("port", 0)):
        # Reattach ONLY to a server running THIS script. A detached server
        # survives plugin updates and Claude Code restarts, so after an update
        # the running process may be an older version serving the old UI —
        # replace it in place (same port, so an open tab reconnects on its
        # own) instead of silently reattaching to stale code.
        if rt.get("script") == str(Path(__file__).resolve()):
            url = f"http://127.0.0.1:{rt['port']}/"
            if not no_browser:
                _open_browser(url)
            print(f"Token-telemetry dashboard already running at {url}")
            return
        target = rt["port"]
        _stop_running(rt, target)
        port = _free_port(target)
        pid = _spawn_detached(port)
        url = f"http://127.0.0.1:{port}/"
        if not no_browser:
            _open_browser(url)
        print(f"Token-telemetry dashboard was running an older plugin version"
              f" — replaced in place at {url}")
        print(f"(background pid {pid} — an already-open tab reconnects"
              " automatically)")
        return
    port = _free_port(port)
    pid = _spawn_detached(port)
    url = f"http://127.0.0.1:{port}/"
    if not no_browser:
        _open_browser(url)
    print(f"Token-telemetry dashboard running at {url}")
    print(f"(background pid {pid} — stop with: python3 dashboard.py stop)")


def cmd_restart(port):
    """Restart the server on the SAME port the open tab is polling, WITHOUT
    opening a second browser tab. The tab's connection-lost modal reconnects on
    its own once the port is serving again."""
    rt = _read_runtime()
    target = rt["port"] if rt else port
    _stop_running(rt, target)
    target = _free_port(target)
    pid = _spawn_detached(target)
    print(f"Token-telemetry dashboard server restarted at http://127.0.0.1:{target}/")
    print(f"(background pid {pid} — no new tab opened; your open dashboard reconnects automatically)")


def cmd_stop():
    rt = _read_runtime()
    if rt and _pid_alive(rt.get("pid", -1)):
        try:
            os.kill(rt["pid"], signal.SIGTERM)
            print("Dashboard stopped.")
        except OSError as e:
            print(f"Could not stop pid {rt['pid']}: {e}")
    else:
        print("No running dashboard found.")
    try:
        runtime_path().unlink()
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="token-telemetry interactive dashboard")
    sub = ap.add_subparsers(dest="command")
    op = sub.add_parser("open", help="launch (or reattach) and open the browser")
    op.add_argument("--port", type=int, default=8756)
    op.add_argument("--no-browser", action="store_true")
    sv = sub.add_parser("serve", help="run the blocking server (internal)")
    sv.add_argument("--port", type=int, default=8756)
    rs = sub.add_parser("restart", help="restart the server in place (no new browser tab)")
    rs.add_argument("--port", type=int, default=8756)
    sub.add_parser("stop", help="stop a backgrounded dashboard server")
    args = ap.parse_args(argv)

    if args.command == "serve":
        cmd_serve(args.port)
    elif args.command == "restart":
        cmd_restart(args.port)
    elif args.command == "stop":
        cmd_stop()
    else:  # default: open
        cmd_open(getattr(args, "port", 8756), getattr(args, "no_browser", False))


if __name__ == "__main__":
    main()
