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
  register-user --name N            mint/reuse the central identity, upsert users
  resolve-root [--cwd D]            JSON: the repository /enable and /disable act on
  disable [--cwd D]                 remove every opt-in marker of that repository
                                    (main + all worktrees + cwd), clear mirror meta

DBs are opened through capture.connect() (the schema owner) for writes. Shared
table copies introspect the COMMON columns of source and destination, so an
export never silently drops a column added by a later schema version.
"""
import argparse
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture
import migrate_lib
import settings

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


def common_column_list(conn, table, src_schema="src", exclude=()):
    """The list form of :func:`common_columns`, in destination order, with
    ``exclude`` names dropped — used by the importer, which must omit synthetic
    ``id`` columns (they are re-assigned on the destination) and remap the
    foreign keys that would have pointed at the source's ids."""
    dst = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    src = {r[1] for r in conn.execute(f"PRAGMA {src_schema}.table_info({table})")}
    return [c for c in dst if c in src and c not in exclude]


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


def register_user(db, name):
    """Establish the central identity and upsert the matching ``users`` row.

    Mints the uuid once (:func:`settings.ensure_identity` — stable thereafter),
    writes ``settings.json`` mode ``0600``, then upserts ``users(uuid, name,
    created_at)`` keyed on uuid: a new uuid inserts with ``created_at`` = now, an
    existing one only updates ``name`` (``created_at`` is never rewritten). The
    name is bound as an argument, never interpolated into SQL, and is treated as
    PII — it is not echoed to stdout.

    :param db: path to the central usage DB.
    :param name: the user's full name.
    :returns: 0 on success.
    """
    _, minted = settings.ensure_identity(name)
    uid = settings.current_owner_id()
    conn = capture.connect(db)  # creating the row pre-capture is the point
    try:
        with conn:
            conn.execute(
                "INSERT INTO users(uuid, name, created_at)"
                " VALUES (?, ?, strftime('%s','now'))"
                " ON CONFLICT(uuid) DO UPDATE SET name=excluded.name",
                (uid, name))
    finally:
        conn.close()
    print(f"identity registered (uuid {'minted' if minted else 'unchanged'});"
          " users row upserted")
    return 0


def _blocking_diffs(report):
    """The table diffs that make an import unsafe: columns present in the SOURCE
    but absent at the DESTINATION. Importing would silently drop that data, so
    the safeguard aborts instead. Columns present only at the destination are
    harmless — the source simply has no value for them and the copy leaves them
    NULL."""
    return [d for d in report.table_diffs if d.only_in_src]


def _describe_diffs(diffs):
    return "; ".join(
        f"{d.table}: source-only {list(d.only_in_src)}"
        + (f", dest-only {list(d.only_in_dst)}" if d.only_in_dst else "")
        for d in diffs) or "none"


def import_check(db, source, project):
    """Read-only preflight the migrate-to-central command reads before importing.

    Reports, in machine-parseable ``key=value`` lines, whether the source can be
    imported into the central DB without dropping data (req 12): the two DBs'
    schema versions, any structure diff, whether the diff is blocking, whether
    the destination already has the ``users`` table, and whether a central
    identity is set. Mutates nothing — neither DB is opened read-write."""
    if not Path(source).exists():
        return fail(f"source DB does not exist: {source}")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        central_exists = Path(db).exists()
        if central_exists:
            dst = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rep = migrate_lib.compat_report(src, dst, capture.SCHEMA_VERSION)
            finally:
                dst.close()
            blocking = _blocking_diffs(rep)
            src_ver, dst_ver = rep.src_version, rep.dst_version
            versions_match = rep.versions_match
            users_at_dst = rep.users_at_dst
            diff_desc = _describe_diffs(rep.table_diffs)
        else:
            # No central yet: the import creates it fresh at the kit's version,
            # so nothing can be source-only against it. Identity still governs
            # whether a name must be captured first.
            blocking = []
            src_ver = src.execute("PRAGMA user_version").fetchone()[0]
            dst_ver = None
            versions_match = False
            users_at_dst = False
            diff_desc = "none (central will be created fresh)"
    finally:
        src.close()
    identity_set = settings.current_user() is not None
    print(f"source_exists=yes central_exists={'yes' if central_exists else 'no'}")
    print(f"src_version={src_ver} dst_version="
          f"{dst_ver if dst_ver is not None else '-'}")
    print(f"versions_match={'yes' if versions_match else 'no'}")
    print(f"structure_diff={diff_desc}")
    print(f"blocking_diff={'yes' if blocking else 'no'}")
    print(f"users_at_dst={'yes' if users_at_dst else 'no'}")
    print(f"identity_set={'yes' if identity_set else 'no'}")
    print(f"compatible={'no' if blocking else 'yes'}")
    return 0


def _project_counts(conn, pid):
    """(events, sessions) currently in ``conn`` for central project id ``pid``."""
    if pid is None:
        return 0, 0
    sess = ("(SELECT id FROM sessions WHERE project_id = ?)")
    e = conn.execute(
        f"SELECT COUNT(*) FROM events WHERE session_id IN {sess}",
        (pid,)).fetchone()[0]
    s = conn.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                     (pid,)).fetchone()[0]
    return e, s


def import_project(db, source, project, name=None):
    """Import one project's LOCAL mirror (or export) into the central DB.

    The inverse of :func:`export`: it ATTACHes the source and copies its rows
    INTO the central store rather than out of it, children after parents
    (projects -> models -> pricing -> sessions -> events), remapping every
    foreign key through the natural keys (project ``path``, session ``uuid``,
    model ``name``) so the source's synthetic ids never collide with the
    central's. Every copy uses :func:`common_column_list` introspection (shared
    columns only) and every value is bound, never interpolated. Rows are deduped
    on the full column tuple, so a re-run adds nothing (contract "Duplicates").

    The import, its moved-row counts and the ``import-project`` audit row are one
    transaction — any failure rolls the whole thing back and leaves the central
    DB untouched. ``cursors`` are deliberately NOT imported: they are central
    authoritative and a mirror's are empty, so importing them could only corrupt
    the central read state. After the commit the imported project's previously
    anonymous sessions are retro-linked to the current user (req 13).

    Compatibility safeguard (req 12): aborts before any write when the source
    carries a column the central DB cannot accept (data would be dropped). A
    pre-v7 central is auto-migrated to the current schema by the schema-owning
    ``connect()``, and the ``users`` row is (re)written from the identity — from
    ``--name`` when given (the command prompts and passes it), else from the
    central settings already on file.

    :param db: path to the central usage DB (created fresh if absent).
    :param source: path to the source mirror/export DB to import.
    :param project: the project ``path`` to import (its row in the source).
    :param name: the user's full name, passed by the command only when identity
        must be established/updated; never echoed (PII).
    :returns: 0 on success, 1 on a clean abort.
    """
    if not Path(source).exists():
        return fail(f"source DB does not exist: {source}")

    src_ro = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        src_pid = src_ro.execute(
            "SELECT id FROM projects WHERE path = ?", (project,)).fetchone()
    finally:
        src_ro.close()
    if src_pid is None:
        return fail(f"project not found in source: {project}")
    src_pid = src_pid[0]

    central = capture.connect(db)  # schema owner: creates/migrates to current v7
    try:
        # Compatibility safeguard (req 12) on a separate read-only handle to the
        # source — no writes to it, and the decision is made before any import.
        src_ro = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            rep = migrate_lib.compat_report(
                src_ro, central, capture.SCHEMA_VERSION)
        finally:
            src_ro.close()
        blocking = _blocking_diffs(rep)
        if blocking:
            return fail("incompatible source schema, refusing to import "
                        f"(would drop data): {_describe_diffs(blocking)}")

        central.execute("ATTACH DATABASE ? AS src", (str(source),))

        # Identity: establish/update from --name, else read what is on file. The
        # users row is (re)written so a freshly-migrated central has it too.
        if name is not None:
            settings.ensure_identity(name)
            uid = settings.current_owner_id()
            uname = name
        else:
            user = settings.current_user()
            uid = user.get("uuid") if user else None
            uname = user.get("full_name") if user else None
        if uid is None:
            return fail("no central identity set; pass --name to establish one")
        if uname:
            migrate_lib.ensure_users_row(central, uid, uname)

        pcols = common_column_list(central, "projects", exclude=("id",))
        mcols = common_column_list(central, "models", exclude=("id",))
        prcols = common_column_list(central, "pricing")
        scols = common_column_list(central, "sessions", exclude=("id",))
        ecols = common_column_list(central, "events")
        eother = [c for c in ecols if c not in ("session_id", "model_id")]

        with central:
            # projects: dedup on the natural key `path` (UNIQUE).
            central.execute(
                f"INSERT OR IGNORE INTO projects({', '.join(pcols)})"
                f" SELECT {', '.join('p.' + c for c in pcols)}"
                " FROM src.projects p WHERE p.path = ?", (project,))
            central_pid = central.execute(
                "SELECT id FROM projects WHERE path = ?", (project,)).fetchone()[0]
            before_e, before_s = _project_counts(central, central_pid)

            # models / pricing: shared reference data, deduped on their natural
            # keys (models.name UNIQUE; pricing's UNIQUE constraint).
            central.execute(
                f"INSERT OR IGNORE INTO models({', '.join(mcols)})"
                f" SELECT {', '.join(mcols)} FROM src.models")
            central.execute(
                f"INSERT OR IGNORE INTO pricing({', '.join(prcols)})"
                f" SELECT {', '.join(prcols)} FROM src.pricing")

            # sessions: natural key `uuid` (UNIQUE); project_id is remapped to
            # the central project id (bound), so an existing central session is
            # kept as-is and a new one lands under the right project.
            s_select = ("?" if c == "project_id" else f"s.{c}" for c in scols)
            central.execute(
                f"INSERT OR IGNORE INTO sessions({', '.join(scols)})"
                f" SELECT {', '.join(s_select)}"
                " FROM src.sessions s WHERE s.project_id = ?",
                (central_pid, src_pid))

            # events: no natural key — remap session_id (via session uuid) and
            # model_id (via model name), then dedup on the FULL remapped tuple
            # with null-safe `IS`, so a re-run inserts nothing.
            insert_cols = ["session_id", "model_id"] + eother
            select_exprs = ["cs.id", "cm.id"] + [f"e.{c}" for c in eother]
            dedup = (["ev.session_id IS cs.id", "ev.model_id IS cm.id"]
                     + [f"ev.{c} IS e.{c}" for c in eother])
            central.execute(
                f"INSERT INTO events({', '.join(insert_cols)})"
                f" SELECT {', '.join(select_exprs)}"
                " FROM src.events e"
                " JOIN src.sessions ss ON e.session_id = ss.id"
                " JOIN sessions cs ON cs.uuid = ss.uuid"
                " JOIN src.models sm ON e.model_id = sm.id"
                " JOIN models cm ON cm.name = sm.name"
                " WHERE ss.project_id = ?"
                f" AND NOT EXISTS (SELECT 1 FROM events ev"
                f" WHERE {' AND '.join(dedup)})", (src_pid,))

            after_e, after_s = _project_counts(central, central_pid)
            moved_e, moved_s = after_e - before_e, after_s - before_s
            audit_row(central, "import-project", project,
                      f"{source}; +{moved_e} events, +{moved_s} sessions")
        central.execute("DETACH DATABASE src")

        # Retro-link the imported project's still-anonymous sessions to the user
        # (req 13). Separate, idempotent transaction: a second run affects 0 rows.
        linked = migrate_lib.retro_link(central, uid, project_id=central_pid)
    finally:
        central.close()
    print(f"imported {project} <- {source}: +{moved_e} events,"
          f" +{moved_s} sessions; retro-linked {linked} sessions")
    return 0


def list_worktrees(base):
    """The OTHER checkouts of the repository at ``base``, from
    ``git worktree list --porcelain -z`` — NUL-separated, so a path holding a
    newline can never inject an extra entry. Same hardening overrides as
    :func:`capture.git` (a hostile repo config cannot run programs).

    :param base: the main repository root.
    :returns: ``(paths, error)`` — ``error`` is None on success, else a short
        reason; a failure is never silently "no worktrees".
    """
    try:
        out = subprocess.run(
            ["git", "-c", "core.fsmonitor=false",
             "-c", "core.hooksPath=/dev/null", "-C", str(base),
             "worktree", "list", "--porcelain", "-z"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"git worktree list failed: {e}"
    if out.returncode != 0:
        msg = out.stderr.decode("utf-8", "replace").strip().splitlines()
        return [], ("git worktree list failed: "
                    + (msg[0] if msg else f"exit {out.returncode}"))
    base_real = os.path.realpath(base)
    paths = []
    for field in out.stdout.split(b"\0"):
        if field.startswith(b"worktree "):
            wt = os.fsdecode(field[len(b"worktree "):])
            if os.path.realpath(wt) != base_real:
                paths.append(wt)
    return paths, None


def repo_scope(cwd, db):
    """The repository ``/enable`` and ``/disable`` act on — resolved exactly as
    capture resolves its project key, so the commands and capture can never
    disagree about which project a directory belongs to.

    Inside a linked worktree that is the MAIN repository
    (:func:`capture.main_repo_root`), and the scope covers the main checkout
    and ALL its worktrees (listed with ``git worktree list --porcelain``
    through :func:`capture.git`'s hardened call). ``root`` is spelled as the
    central DB already stores it (:func:`capture.canonical_project_path`), so
    ``clear-mirror-meta``/``register-name`` hit the captured row.

    :param cwd: the directory the command runs from.
    :param db: the central DB path (may not exist yet).
    A ``.claude`` that is a symlink is never followed: its marker is not
    listed but reported under ``refused``.

    :returns: dict — ``root`` (project key), ``checkout``, ``is_worktree``,
        ``worktrees`` (other checkouts of the repo), ``worktree_error`` (None,
        or why they could not be listed), ``markers`` (existing
        ``.claude/telemetry`` entries in root, worktrees, checkout and cwd),
        ``refused`` (``[{path, reason}]``).
    """
    cwd = Path(cwd)
    checkout = capture.find_project_root(cwd)
    main_root = capture.main_repo_root(checkout)
    base = main_root or checkout
    root = str(base)
    if Path(db).exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                root = capture.canonical_project_path(conn, root)
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    worktrees, worktree_error = [], None
    if (Path(base) / ".git").is_dir():
        worktrees, worktree_error = list_worktrees(base)
    markers, refused, seen = [], [], set()
    for d in (base, *worktrees, checkout, cwd):
        real_dir = os.path.realpath(d)
        if real_dir in seen:
            continue
        seen.add(real_dir)
        claude = Path(d) / ".claude"
        if os.path.islink(claude):
            refused.append({"path": str(claude / "telemetry"),
                            "reason": ".claude is a symlink - not followed"})
            continue
        m = claude / "telemetry"
        if os.path.lexists(m):
            markers.append(str(m))
    return {"root": root, "checkout": str(checkout),
            "is_worktree": main_root is not None, "worktrees": worktrees,
            "worktree_error": worktree_error, "markers": markers,
            "refused": refused}


def disable(db, cwd):
    """Turn capture off for the WHOLE repository ``cwd`` belongs to: remove
    every opt-in marker :func:`repo_scope` finds (main root, every worktree,
    the checkout and cwd), then clear the root row's mirror metadata. Prints
    one JSON summary: ``root``, ``worktrees``, ``removed``, ``failed``
    (``[{path, reason}]``) and ``worktree_error``.

    Every marker is attempted independently — one failure never stops the
    rest, so the current checkout's marker always goes. Only a regular file
    or a symlink (unlinked, never followed) is removed; a directory or any
    other type is left and reported. A symlinked ``.claude`` is refused
    (:func:`repo_scope`).

    :param db: the central DB path.
    :param cwd: the directory the command runs from.
    :returns: 0 when every marker was removed and the worktrees could be
        listed; 2 otherwise (capture may continue where ``failed`` says).
    """
    scope = repo_scope(cwd, db)
    removed, failed = [], list(scope["refused"])
    for m in scope["markers"]:
        try:
            mode = os.lstat(m).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                kind = "a directory" if stat.S_ISDIR(mode) else "not a file"
                failed.append({"path": m, "reason": f"marker is {kind} - left"})
                continue
            os.unlink(m)
            removed.append(m)
        except FileNotFoundError:
            continue
        except OSError as e:
            failed.append({"path": m, "reason": e.strerror or str(e)})
    try:
        clear_mirror_meta(db, scope["root"])
    except sqlite3.Error as e:
        failed.append({"path": str(db), "reason": f"mirror meta not cleared: {e}"})
    print(json.dumps({"root": scope["root"], "worktrees": scope["worktrees"],
                      "removed": removed, "failed": failed,
                      "worktree_error": scope["worktree_error"]}))
    return 2 if failed or scope["worktree_error"] else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="manage.py")
    ap.add_argument("command", choices=[
        "list-projects", "counts", "export", "delete", "audit",
        "clear-mirror-meta", "register-name", "register-user",
        "import-check", "import-project", "resolve-root", "disable"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--project", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--source", default=None)
    ap.add_argument("--action", default=None)
    ap.add_argument("--detail", default="")
    ap.add_argument("--name", default=None)
    ap.add_argument("--cwd", default=None)
    a = ap.parse_args(argv)
    db = a.db or capture.db_path()
    need = {"counts": ("project",), "export": ("project", "out"),
            "delete": ("project", "action"), "audit": ("action", "project"),
            "clear-mirror-meta": ("project",),
            "register-name": ("project", "name"),
            "register-user": ("name",),
            "import-check": ("source", "project"),
            "import-project": ("source", "project")}
    for arg in need.get(a.command, ()):
        if getattr(a, arg) is None:
            return fail(f"{a.command} requires --{arg}")
    if a.command == "resolve-root":
        print(json.dumps(repo_scope(a.cwd or os.getcwd(), db)))
        return 0
    if a.command == "disable":
        return disable(db, a.cwd or os.getcwd())
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
    if a.command == "register-name":
        return register_name(db, a.project, a.name)
    if a.command == "import-check":
        return import_check(db, a.source, a.project)
    if a.command == "import-project":
        return import_project(db, a.source, a.project, a.name)
    return register_user(db, a.name)


if __name__ == "__main__":
    sys.exit(main())
