#!/usr/bin/env python3
"""Deterministic write-side storage operations for the telemetry commands.

Every mutating step the enable/disable/storage-* commands need lives here as a
subcommand taking argv values (never interpolated into SQL), so the command
prompts hold NO raw SQL and their permission grants pin to this one script:

  list-projects                     markdown table of projects with counts
  counts --project P                events / sessions / cursors / span
  export --project P --out FILE     carve one project into a self-contained DB
  audit --action A --project P --detail D
  delete --project P --action delete|delete-after-export --detail D
  clear-mirror-meta --project P     forget a project-level copy (bookkeeping)
  register-name --project P --name N

DBs are opened through capture.connect() (the schema owner) for writes. Shared
table copies introspect the COMMON columns of source and destination, so an
export never silently drops a column added by a later schema version.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture

SESSIONS = ("(SELECT id FROM sessions WHERE project_id ="
            " (SELECT id FROM projects WHERE path = ?))")


def fail(msg):
    print(msg, file=sys.stderr)
    return 1


def require_db(db):
    if not Path(db).exists():
        raise SystemExit(fail(f"central DB does not exist: {db}"))


def common_columns(conn, table, src_schema="src"):
    """Columns present in BOTH attached source and destination — additive
    schema changes never silently drop data from a copy."""
    dst = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    src = {r[1] for r in conn.execute(f"PRAGMA {src_schema}.table_info({table})")}
    return ", ".join(c for c in dst if c in src)


def audit_row(conn, action, project, detail):
    conn.execute(
        "INSERT INTO audit_log(ts, action, project, detail)"
        " VALUES (strftime('%s','now'), ?, ?, ?)", (action, project, detail))


def list_projects(db):
    require_db(db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    name_sel = "p.name" if "name" in cols else "NULL"
    rows = conn.execute(
        f"SELECT p.path, {name_sel}, COUNT(e.rowid)"
        " FROM projects p"
        " LEFT JOIN sessions s ON s.project_id = p.id"
        " LEFT JOIN events e ON e.session_id = s.id"
        " GROUP BY p.id, p.path ORDER BY COUNT(e.rowid) DESC").fetchall()
    conn.close()
    print("| # | project | name | events |")
    print("|---|---|---|---:|")
    for i, (path, name, events) in enumerate(rows, 1):
        print(f"| {i} | `{path}` | {name or '—'} | {events} |")
    return 0


def counts(db, project):
    require_db(db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        events = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE session_id IN {SESSIONS}",
            (project,)).fetchone()[0]
        sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_id ="
            " (SELECT id FROM projects WHERE path = ?)", (project,)).fetchone()[0]
        cursors = conn.execute(
            f"SELECT COUNT(*) FROM cursors WHERE session_id IN {SESSIONS}",
            (project,)).fetchone()[0]
        span = conn.execute(
            "SELECT MIN(date(ts,'unixepoch')) || ' .. ' ||"
            f" MAX(date(ts,'unixepoch')) FROM events WHERE session_id IN"
            f" {SESSIONS}", (project,)).fetchone()[0]
    finally:
        conn.close()
    print(f"events={events} sessions={sessions} cursors={cursors}"
          f" span={span or '-'}")
    return 0


def export(db, project, out):
    require_db(db)
    # Hard refusal: connect() would happily open an existing DB and add a
    # second project's rows, silently turning someone else's export into a
    # two-project file.
    if os.path.exists(out):
        return fail(f"export path exists, refusing: {out}")
    conn = capture.connect(out)  # current schema + pricing seed
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(db),))
        pid = "(SELECT id FROM src.projects WHERE path = ?)"
        sessions = f"(SELECT id FROM src.sessions WHERE project_id = {pid})"
        with conn:
            # Full reference tables: the export must price itself standalone.
            for table in ("models", "pricing"):
                cols = common_columns(conn, table)
                conn.execute(f"INSERT OR IGNORE INTO {table}({cols})"
                             f" SELECT {cols} FROM src.{table}")
            # ids copied verbatim so events/sessions keep their foreign keys.
            pcols = common_columns(conn, "projects")
            conn.execute(f"INSERT INTO projects({pcols}) SELECT {pcols}"
                         " FROM src.projects WHERE path = ?", (project,))
            scols = common_columns(conn, "sessions")
            conn.execute(f"INSERT INTO sessions({scols}) SELECT {scols}"
                         f" FROM src.sessions WHERE project_id = {pid}",
                         (project,))
            ecols = common_columns(conn, "events")
            conn.execute(f"INSERT INTO events({ecols}) SELECT {ecols}"
                         f" FROM src.events WHERE session_id IN {sessions}",
                         (project,))
            ccols = common_columns(conn, "cursors")
            conn.execute(f"INSERT INTO cursors({ccols}) SELECT {ccols}"
                         f" FROM src.cursors WHERE session_id IN {sessions}",
                         (project,))
        conn.execute("DETACH DATABASE src")
    finally:
        conn.close()
    print(f"exported {project} -> {out}")
    return 0


def delete(db, project, action, detail):
    require_db(db)
    if action not in ("delete", "delete-after-export"):
        return fail(f"unknown delete action: {action}")
    conn = capture.connect(db)
    try:
        # children before parents, audit row INSIDE the one transaction
        with conn:
            conn.execute(
                f"DELETE FROM events WHERE session_id IN {SESSIONS}", (project,))
            conn.execute(
                f"DELETE FROM cursors WHERE session_id IN {SESSIONS}", (project,))
            conn.execute(
                "DELETE FROM sessions WHERE project_id ="
                " (SELECT id FROM projects WHERE path = ?)", (project,))
            conn.execute("DELETE FROM projects WHERE path = ?", (project,))
            audit_row(conn, action, project, detail)
    finally:
        conn.close()
    print(f"deleted {project} ({detail})")
    return 0


def audit(db, action, project, detail):
    require_db(db)
    conn = capture.connect(db)
    try:
        with conn:
            audit_row(conn, action, project, detail)
    finally:
        conn.close()
    print(f"audit: {action} {project}")
    return 0


def clear_mirror_meta(db, project):
    """Bookkeeping, never worth failing over: silently no-op when the DB does
    not exist or predates the mirror columns."""
    if not Path(db).exists():
        print("no central DB - nothing to clear")
        return 0
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        if {"mirror_path", "mirror_last_at"} <= cols:
            with conn:
                conn.execute(
                    "UPDATE projects SET mirror_path=NULL, mirror_last_at=NULL"
                    " WHERE path = ?", (project,))
            print(f"mirror metadata cleared for {project}")
        else:
            print("DB predates mirror columns - nothing to clear")
    finally:
        conn.close()
    return 0


def register_name(db, project, name):
    conn = capture.connect(db)  # creating the row pre-capture is the point
    try:
        with conn:
            conn.execute(
                "INSERT INTO projects(path) SELECT ? WHERE NOT EXISTS"
                " (SELECT 1 FROM projects WHERE path = ?)", (project, project))
            conn.execute("UPDATE projects SET name = ? WHERE path = ?",
                         (name, project))
    finally:
        conn.close()
    print(f"registered name '{name}' for {project}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="manage.py")
    ap.add_argument("command", choices=[
        "list-projects", "counts", "export", "delete", "audit",
        "clear-mirror-meta", "register-name"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--project", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--action", default=None)
    ap.add_argument("--detail", default="")
    ap.add_argument("--name", default=None)
    a = ap.parse_args(argv)
    db = a.db or capture.db_path()
    need = {"counts": ("project",), "export": ("project", "out"),
            "delete": ("project", "action"), "audit": ("action", "project"),
            "clear-mirror-meta": ("project",),
            "register-name": ("project", "name")}
    for arg in need.get(a.command, ()):
        if getattr(a, arg) is None:
            return fail(f"{a.command} requires --{arg}")
    if a.command == "list-projects":
        return list_projects(db)
    if a.command == "counts":
        return counts(db, a.project)
    if a.command == "export":
        return export(db, a.project, a.out)
    if a.command == "delete":
        return delete(db, a.project, a.action, a.detail)
    if a.command == "audit":
        return audit(db, a.action, a.project, a.detail)
    if a.command == "clear-mirror-meta":
        return clear_mirror_meta(db, a.project)
    return register_name(db, a.project, a.name)


if __name__ == "__main__":
    sys.exit(main())
