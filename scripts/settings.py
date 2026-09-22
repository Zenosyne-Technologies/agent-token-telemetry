#!/usr/bin/env python3
"""Central telemetry settings — per-user identity and the active backend.

Stdlib only. The file lives at ``<telemetry-dir>/settings.json``, a sibling of
the usage DB (``$TOKEN_TELEMETRY_DB``'s directory, else ``~/.claude/telemetry/``)
— never inside a repo, so a user's full name (PII) cannot be committed. It is
written mode ``0600`` (owner-only) for the same reason.

Shape (P2 of the identity work — no ``supabase`` block yet)::

    {"user": {"uuid": "<uuid4>", "full_name": "<name>"}, "active_backend": "local"}

The uuid is minted once (:func:`ensure_identity`) and stable thereafter. Reads
are total: any absent/partial/corrupt file reports "no identity" and never
raises, because capture reads this on the Stop/SubagentStop hook path and must
never break a session. Writes happen only in the interactive command flow
(``/token-telemetry:enable`` via ``manage.py register-user``), never in capture.
"""
import json
import os
import uuid
from pathlib import Path

# The default (and, in P2, only) collection backend. A later phase adds the
# "supabase" option and the pointer flip; here it is written so the file already
# declares the field consumers will branch on.
DEFAULT_BACKEND = "local"


def telemetry_dir():
    """The telemetry directory — where the usage DB, error log and this file
    live. Mirrors ``capture.db_path()``'s resolution (``$TOKEN_TELEMETRY_DB``
    else the default), taking its PARENT so settings sit beside the DB whatever
    the override points at. Kept dependency-free (no import of ``capture``) so
    the hot capture path can read settings without an import cycle."""
    return Path(os.environ.get("TOKEN_TELEMETRY_DB",
                               "~/.claude/telemetry/usage.db")).expanduser().parent


def settings_path():
    """Absolute path to ``settings.json`` beside the usage DB."""
    return telemetry_dir() / "settings.json"


def read_settings():
    """Parse ``settings.json`` and return it as a dict.

    :returns: the parsed settings dict, or ``{}`` on ANY problem — absent,
        unreadable, truncated/half-written, corrupt, or a non-object top level.
        Never raises: capture calls this on the hook path.
    """
    try:
        with open(settings_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def current_user(settings=None):
    """The identity block if a valid one is set, else ``None``.

    :param settings: an already-read settings dict, to avoid a second file read;
        omitted, the file is read fresh.
    :returns: the ``{"uuid": ..., "full_name": ...}`` dict, or ``None`` when no
        settings, no ``user`` block, or no non-empty string ``uuid`` is present
        (a partial or malformed block reads as no identity).
    """
    s = settings if settings is not None else read_settings()
    user = s.get("user") if isinstance(s, dict) else None
    if not isinstance(user, dict):
        return None
    uid = user.get("uuid")
    return user if isinstance(uid, str) and uid else None


def current_owner_id(settings=None):
    """The uuid capture stamps onto a new session's ``owner_id``.

    :param settings: an already-read settings dict (optional), as
        :func:`current_user`.
    :returns: the current user's uuid, or ``None`` (pre-identity) when no valid
        identity is set. Never raises.
    """
    user = current_user(settings)
    return user.get("uuid") if user else None


def write_settings(settings):
    """Persist ``settings`` to ``settings.json`` atomically, mode ``0600``.

    Creates the telemetry directory if needed. Writes to a per-process temp file
    opened ``0600`` from creation (so the name is never briefly world-readable),
    then :func:`os.replace` swaps it in — a concurrent reader sees either the old
    or the new whole file, never a half-written one. The final mode is forced to
    ``0600`` so an already-present looser file is tightened.

    :param settings: the settings dict to serialize.
    :returns: the :class:`~pathlib.Path` written.
    :raises OSError: only on a genuine filesystem failure; interactive callers
        surface it, capture never calls this.
    """
    d = telemetry_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = settings_path()
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    # os.replace keeps the destination's prior mode when it already existed, so
    # force 0600 unconditionally rather than trusting the create mode.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def ensure_identity(full_name):
    """Mint the uuid on first use, persist the full name, return the settings.

    Idempotent on the uuid: it is generated with :func:`uuid.uuid4` only when no
    valid uuid is already stored, and reused unchanged on every later call — so
    re-running enable never re-mints. ``full_name`` is always written, so a
    changed name updates in place. ``active_backend`` defaults to
    :data:`DEFAULT_BACKEND` and any existing value is preserved.

    :param full_name: the user's full name (PII — never logged or URL-encoded).
    :returns: ``(settings, minted)`` where ``settings`` is the persisted dict and
        ``minted`` is ``True`` iff this call generated a new uuid.
    :raises OSError: propagated from :func:`write_settings` on a write failure.
    """
    settings = read_settings()
    user = settings.get("user")
    if not isinstance(user, dict):
        user = {}
    uid = user.get("uuid")
    minted = not (isinstance(uid, str) and uid)
    if minted:
        uid = str(uuid.uuid4())
    settings["user"] = {"uuid": uid, "full_name": full_name}
    settings.setdefault("active_backend", DEFAULT_BACKEND)
    write_settings(settings)
    return settings, minted
