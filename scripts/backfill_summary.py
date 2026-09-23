"""Cached backfill-plan summary for the dashboard's own-price banner (AOS-149).

A full backfill plan (:func:`pricing_update.backfill_plan`) can take tens of
seconds on a large DB, so the dashboard never computes one. Instead,
``pricing_update.py --backfill-plan`` records a tiny summary of the plan it
just computed — how many bundles are offered, the combined cost change, when
it ran — in a sidecar JSON file inside the telemetry data directory, and the
dashboard only reads that file.

**Staleness is decided by a fingerprint, never by age.** The summary carries
:func:`fingerprint` of the DB state the plan was computed over; the dashboard
recomputes it (cheap: the ``pricing`` table plus one indexed aggregate over
the events a plan can look at) and shows the summary only when the two match.
A plan depends on exactly two things: every ``pricing`` row, and the events
dated before the plan's horizon (the latest first-own-row date of any
non-family-default prefix — no backfill row can ever re-price an event at or
after its prefix's first own row, so later events cannot change a plan). The
fingerprint hashes all pricing rows plus count/rowid/timestamp/model/token
aggregates of the events before that horizon, so a pricing refresh, a
backfill apply, an import of old events, or a deletion all invalidate the
cache, while ordinary new capture (dated after the horizon) does not.

Every failure mode degrades to "no summary": a missing, unreadable, oversized,
corrupt, wrong-version, wrongly-typed, symlinked or stale file reads back as
``None`` and the banner simply omits the line.

The file is written atomically (a fresh ``O_EXCL | O_NOFOLLOW`` temp file,
mode 0600, then :func:`os.replace`), never through a symlink, and only for
the DB the dashboard itself reads (``capture.db_path()``).
"""
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture
import settings

CACHE_NAME = "backfill-plan.json"
CACHE_VERSION = 1
MAX_CACHE_BYTES = 4096
# computedAt may be at most this far in the future (clock skew) before the
# file is treated as corrupt.
MAX_FUTURE_SKEW_S = 300
# The only shape a stored delta may take — exactly what
# pricing_update._usd_change renders ("-$272.46", "+$1.20", "$0.00",
# "+$0.0042"); anything else is treated as corrupt.
DELTA_RE = re.compile(r"^[+-]?\$[0-9]{1,3}(,[0-9]{3})*\.[0-9]{2}([0-9]{2})?$")
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def cache_path():
    """Absolute path of the summary sidecar: ``backfill-plan.json`` in the
    telemetry data directory (:func:`settings.telemetry_dir`, the directory
    holding ``usage.db``)."""
    return settings.telemetry_dir() / CACHE_NAME


def is_dashboard_db(db):
    """Whether ``db`` is the DB the dashboard reads (``capture.db_path()``).

    Only a plan over that DB may write the summary: a plan run against some
    other file (``--db``) must never make the dashboard claim a backfill for
    data it is not showing.

    :param db: path of the DB a plan ran against.
    :returns: ``True`` when both paths name the same existing file.
    """
    try:
        return os.path.samefile(db, capture.db_path())
    except OSError:
        return False


def _horizon(pricing_rows):
    """Latest first-row ``effective_from`` among non-family-default prefixes
    (0 when there is none) — the plan's event horizon (see module doc)."""
    first = {}
    for row in pricing_rows:
        prefix, eff = row[1], row[8]
        if prefix not in first or eff < first[prefix]:
            first[prefix] = eff
    return max((eff for prefix, eff in first.items()
                if eff and eff > 0 and not capture.is_family_default(prefix)),
               default=0)


def fingerprint(conn):
    """Fingerprint of everything a backfill plan over ``conn`` depends on.

    :param conn: an open sqlite3 connection to the telemetry DB (read-only is
        enough).
    :returns: a 64-char lowercase hex SHA-256 string.
    :raises sqlite3.Error: on a DB error (callers treat it as "no summary").
    """
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(pricing)")}
    ecols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    w1h = "cache_w_1h_usd" if "cache_w_1h_usd" in pcols else "NULL"
    ew1h = "cache_w_1h" if "cache_w_1h" in ecols else "0"
    rows = [tuple(r) for r in conn.execute(
        "SELECT provider, model_prefix, model_version, in_usd, out_usd,"
        f" cache_r_usd, cache_w_usd, {w1h}, effective_from, source"
        " FROM pricing ORDER BY provider, model_prefix, model_version,"
        " effective_from, source")]
    horizon = _horizon(rows)
    agg = tuple(conn.execute(
        "SELECT COUNT(*), SUM(rowid), MAX(rowid), SUM(ts), SUM(model_id),"
        f" SUM(in_tok), SUM(out_tok), SUM(cache_r), SUM(cache_w), SUM({ew1h})"
        " FROM events WHERE ts < ?", (horizon,)).fetchone())
    blob = json.dumps({"v": CACHE_VERSION, "pricing": rows,
                       "horizon": horizon, "events": agg},
                      separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def summarize(plan_, delta_text):
    """The summary fields of one :func:`pricing_update.backfill_plan` result.

    :param plan_: the plan dict.
    :param delta_text: the combined signed delta as rendered for the plan's
        "If you apply everything offered" line, or ``None`` when nothing is
        offered.
    :returns: ``{"bundles": int, "deltaText": str|None}`` — ``bundles`` counts
        the offered rows (candidates plus confirm-only), each one bundle.
    """
    n = len(plan_["candidates"]) + len(plan_["confirm_only"])
    return {"bundles": n, "deltaText": delta_text if n else None}


def write(summary, fp, computed_at, path=None):
    """Atomically write the summary sidecar, never through a symlink.

    A fresh temp file beside the target is created ``O_CREAT | O_EXCL |
    O_NOFOLLOW`` with mode 0600 (an existing file or symlink at that name
    fails the open), written, then :func:`os.replace`-d over the target, and
    the final mode is forced to 0600. A target that already exists as a
    symlink (or any non-regular file) is refused and left untouched.

    :param summary: :func:`summarize` output.
    :param fp: :func:`fingerprint` of the state the plan was computed over.
    :param computed_at: epoch seconds the plan was computed.
    :param path: target path (default :func:`cache_path`).
    :returns: the path written.
    :raises OSError: when the target is a symlink/non-regular file or the
        write fails; no partial file is left behind.
    """
    path = Path(path or cache_path())
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise OSError(f"refusing to write {path.name}: not a regular file")
    body = json.dumps({"version": CACHE_VERSION, "computedAt": int(computed_at),
                       "bundles": int(summary["bundles"]),
                       "deltaText": summary["deltaText"], "fingerprint": fp},
                      sort_keys=True).encode()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def clear(path=None):
    """Remove the summary sidecar after a backfill apply.

    Only a regular file is removed; a symlink or other non-regular entry at
    that name is left alone (the reader never follows it anyway).

    :param path: target path (default :func:`cache_path`).
    :returns: ``True`` when a file was removed.
    """
    path = Path(path or cache_path())
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        os.unlink(path)
        return True
    except OSError:
        return False


def read(path=None, now=None):
    """Parse and validate the summary sidecar — never raises.

    :param path: source path (default :func:`cache_path`).
    :param now: current epoch seconds (for the future-timestamp check).
    :returns: ``{"computedAt": int, "bundles": int, "deltaText": str|None,
        "fingerprint": str}``, or ``None`` when the file is missing, a
        symlink, not a regular file, over :data:`MAX_CACHE_BYTES`, not JSON,
        the wrong version, or any field is missing/mistyped/out of range.
    """
    import time
    path = Path(path or cache_path())
    now = time.time() if now is None else now
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            # A FIFO, device, socket, etc. — closed unopened-for-reading, so
            # a FIFO with no writer (which would otherwise block open() or a
            # blocking read()) never has a byte read from it; treated the
            # same as a missing file.
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(MAX_CACHE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_CACHE_BYTES:
        return None
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or d.get("version") != CACHE_VERSION:
        return None
    at, n = d.get("computedAt"), d.get("bundles")
    delta, fp = d.get("deltaText"), d.get("fingerprint")
    if (type(at) is not int or at <= 0 or at > now + MAX_FUTURE_SKEW_S
            or type(n) is not int or n < 0
            or not isinstance(fp, str) or not _FP_RE.match(fp)):
        return None
    if n > 0 and not (isinstance(delta, str) and DELTA_RE.match(delta)):
        return None
    return {"computedAt": at, "bundles": n,
            "deltaText": delta if n > 0 else None, "fingerprint": fp}


def age_text(seconds):
    """Human age of a summary: ``"just now"``, ``"5 minutes ago"``,
    ``"3 hours ago"``, ``"2 days ago"``.

    :param seconds: age in seconds (negative reads as "just now").
    :returns: the display string.
    """
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    for size, unit, limit in ((60, "minute", 60), (3600, "hour", 48),
                              (86400, "day", None)):
        v = s // size
        if limit is None or v < limit:
            return f"{v} {unit}{'' if v == 1 else 's'} ago"
    return "just now"  # unreachable


def banner_line(conn, now, path=None):
    """The banner's "backfill available" line, or ``None`` — never raises.

    Shown only when the sidecar is valid (:func:`read`), offers at least one
    bundle, and its fingerprint equals :func:`fingerprint` of ``conn`` right
    now. The fingerprint query runs only when a valid, non-empty summary is
    present, so a dashboard with no summary pays nothing.

    :param conn: read-only connection to the dashboard's DB.
    :param now: current epoch seconds.
    :param path: sidecar path (default :func:`cache_path`).
    :returns: e.g. ``"Backfill available: 2 bundles, -$272.46 — run
        /token-telemetry:pricing-update to review and confirm (plan computed
        3 hours ago)."``, or ``None``.
    """
    try:
        s = read(path, now)
        if s is None or s["bundles"] < 1:
            return None
        if fingerprint(conn) != s["fingerprint"]:
            return None
        n = s["bundles"]
        return (f"Backfill available: {n} bundle{'' if n == 1 else 's'},"
                f" {s['deltaText']} — run /token-telemetry:pricing-update to"
                f" review and confirm (plan computed"
                f" {age_text(now - s['computedAt'])}).")
    except Exception:
        return None
