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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture
import storage


def open_ro(db):
    """Read-only connection to the telemetry store, or None when absent.
    The single backend-selection point (see module docstring): it delegates to
    the storage seam, so a future server-hosted backend plugs in there rather
    than here. Reports keep running SQLite SQL over the returned connection."""
    return storage.LocalSqliteBackend(db).open_ro()


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


MD_CELL_MAX = 120


def md_cell(value):
    """Make an untrusted string (project/model/agent/issue name-like data,
    ultimately repo- or caller-influenced) safe to interpolate into one
    markdown table cell: fold newlines/tabs and the Unicode line/paragraph
    separators (U+2028/U+2029) into a single space, drop every other C0
    control character and DEL anywhere in the string (terminals and
    downstream renderers must never see a raw escape byte — e.g. ANSI
    sequences) plus the Unicode bidi-control characters (U+202A-U+202E,
    U+2066-U+2069) that can visually reorder or spoof rendered text, escape
    characters that would break the table structure or bleed formatting
    into the surrounding document, strip leading markdown control
    characters, and cap length so one hostile value can't blow up the
    render.

    Backslashes are escaped BEFORE pipes, so every ``|`` in the result is
    preceded by an odd number of backslashes. A GFM table-row scanner reads
    a backslash plus the next character as one escaped pair: escaping only
    the pipe would turn an input backslash-pipe into backslash-backslash-
    pipe — an escaped backslash followed by a REAL cell delimiter that
    splits the cell (mid-code-span, too). Outside a code span a doubled
    backslash renders as one; inside one it shows as two (code spans take
    backslashes literally) — cosmetic, never structural. The length cap
    applies to the content before escaping, so an escape pair is never cut
    in half."""
    s = str(value)
    s = re.sub(r"[\r\n\t  ]+", " ", s)
    s = re.sub(r"[\x00-\x1f\x7f‪-‮⁦-⁩]", "", s)
    s = s.lstrip("#>-*+= ")
    cut = len(s) > MD_CELL_MAX
    if cut:
        s = s[:MD_CELL_MAX]
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")
    return s + "…" if cut else s


def resolved_subquery(expr):
    """Correlated subquery evaluating ``expr`` (over pricing alias ``pr``) on
    the pricing row resolved for event ``e`` / model ``m``: longest matching
    prefix, then the latest ``effective_from <= e.ts``. NULL when no row
    matches (the event is unpriced). The one definition of the resolution
    every rate column and the estimated flag share."""
    return (f"(SELECT {expr} FROM pricing pr"
            " WHERE m.name LIKE pr.model_prefix || '%'"
            " AND pr.effective_from <= e.ts"
            " ORDER BY LENGTH(pr.model_prefix) DESC, pr.effective_from DESC"
            " LIMIT 1)")


def tier_case(model="model_name", kind="kind", agent="agent"):
    """Role-based cost tier for one event, as a SQL CASE expression. Mirrors
    the kit's ``token-economics.md`` "Tier mapping" verbatim (restated in
    ``docs/TELEMETRY-CONTRACT.md``'s own "Tier mapping" section): the main
    session (``kind = 0``, ``agent`` NULL) is always 'orchestrator' — since kit
    v0.30.0 the orchestrator runs on the heavy tier's own model, so a model
    prefix can no longer tell them apart. Named ``marvin:*`` personas route to
    their tier directly. ANY other agent (a persona the kit hasn't named, or a
    NULL agent on a non-main-session row) falls back to its ``{model}``'s
    prefix — the pre-v0.30 rule, kept as the catch-all, with 'unknown' for a
    model matching no known family. ``{model}``/``{kind}``/``{agent}`` are the
    SQL expressions for those three columns in the query this is spliced
    into."""
    return (
        "CASE"
        f" WHEN {kind} = 0 AND {agent} IS NULL THEN 'orchestrator'"
        f" WHEN {agent} IN ('marvin:developer', 'marvin:researcher')"
        f" OR {agent} LIKE 'marvin:validator-%' THEN 'heavy'"
        f" WHEN {agent} LIKE 'marvin:escalation-%' THEN 'ladder'"
        f" WHEN {agent} IN ('marvin:developer-small', 'marvin:documenter')"
        " THEN 'small'"
        f" WHEN {agent} = 'marvin:ponytail' THEN 'micro'"
        f" WHEN {model} LIKE 'claude-opus-%' THEN 'heavy'"
        f" WHEN {model} LIKE 'claude-sonnet-%' THEN 'small'"
        f" WHEN {model} LIKE 'claude-haiku-%' THEN 'micro'"
        f" WHEN {model} LIKE 'claude-fable-%' THEN 'ladder'"
        " ELSE 'unknown' END"
    )


def rung_case(agent="agent"):
    """Escalation rung for a ladder-tier event, read only from the exact
    ``marvin:escalation-<rung>`` persona name — never inferred from a model or
    effort setting. NULL for a ladder row with no such name: the model-prefix
    fallback (a ``claude-fable-*`` model under an agent that is not a named
    escalation persona), or an unrecognized ``marvin:escalation-*`` suffix.
    The by-rung query (:func:`fetch_token_stats`) COALESCEs this NULL into
    :data:`RUNG_FALLBACK_LABEL` so every ladder-tier row is represented and
    the rung breakdown sums to the tier total. ``{agent}`` is the SQL
    expression for the agent column in the query this is spliced into."""
    return (
        "CASE"
        f" WHEN {agent} = 'marvin:escalation-high' THEN 'high'"
        f" WHEN {agent} = 'marvin:escalation-xhigh' THEN 'xhigh'"
        f" WHEN {agent} = 'marvin:escalation-max' THEN 'max'"
        f" WHEN {agent} = 'marvin:escalation-frontier' THEN 'frontier'"
        " END"
    )


# The by-rung label for a ladder-tier event with no named rung (see
# rung_case). Mirrored verbatim by report_token_stats' v_rung_fallback in
# supabase/reports.sql.
RUNG_FALLBACK_LABEL = "no rung (fallback)"

# The kit's tier display order (docs/TELEMETRY-CONTRACT.md's "Tier mapping"),
# used ONLY to order the comma-joined tier list a by_model row shows for a
# model that served more than one tier (see fetch_token_stats). 'unknown' (a
# model matching no known family) is not part of the kit's own order, so it
# sorts last. Mirrored by the rank CASE in supabase/reports.sql's by_model
# aggregation (there computed the same way, over the SAME tier text).
TIER_DISPLAY_ORDER = ("orchestrator", "heavy", "ladder", "small", "micro")


def tier_rank_case(tier="tier"):
    """SQL CASE ranking a tier-text column ``{tier}`` 0..len-1 in
    :data:`TIER_DISPLAY_ORDER`, len for anything else (``'unknown'``). Used
    to ORDER the per-model tier list a by_model row shows, independently of
    by_model's own output-desc row order."""
    whens = " ".join(f"WHEN '{t}' THEN {i}"
                     for i, t in enumerate(TIER_DISPLAY_ORDER))
    return f"CASE {tier} {whens} ELSE {len(TIER_DISPLAY_ORDER)} END"


def rate_subquery(column):
    return resolved_subquery(f"pr.{column}")


def estimated_subquery():
    """1 when the event's resolved pricing row makes its cost an ESTIMATE — a
    family default row, or an ancestor row (the model is an unlisted point
    release priced at its nearest listed ancestor's row) — 0 when it is the
    model's own row, NULL when the event is unpriced. See
    capture.is_estimated."""
    return resolved_subquery(capture.estimated_sql("m.name", "pr.model_prefix"))


def fetch_models_without_own_price(conn):
    """Model names that have at least one NON-ZERO-TOKEN event and NO own
    pricing row: no row whose prefix matches them (any ``effective_from``)
    that is neither a family default row nor an ancestor row for that name
    (``capture.is_estimated``). Every priceable event of such a model prices
    at an estimate or not at all. A model whose events are ALL zero-token
    (e.g. a synthetic bookkeeping model with no input/output/cache tokens) is
    excluded: there is nothing of theirs to price, so naming them in the
    footer would be noise. Sorted by name. Prefix matching is the resolver's
    own ``LIKE model_prefix || '%'``."""
    return [r[0] for r in conn.execute(
        "SELECT m.name FROM models m"
        " WHERE EXISTS (SELECT 1 FROM events e WHERE e.model_id = m.id"
        "   AND (e.in_tok != 0 OR e.out_tok != 0 OR e.cache_r != 0"
        "        OR e.cache_w != 0))"
        " AND NOT EXISTS (SELECT 1 FROM pricing pr"
        "   WHERE m.name LIKE pr.model_prefix || '%'"
        f"  AND NOT {capture.estimated_sql('m.name', 'pr.model_prefix')})"
        " ORDER BY m.name").fetchall()]


# ---------------------------------------------------------------- project-stats

STATS_KEYS = ("path", "name", "sessions", "events", "input", "output",
              "cache_read", "cache_write",
              "classic_in", "classic_out", "cached_r", "cached_w",
              "rate_from", "unpriced_events", "first_seen", "last_activity",
              "estimated_events")


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
    and cached (read/write) sum to the estimate. ``estimated_events`` counts
    the project's estimated events (resolved row is a family default or an
    ancestor row — capture.is_estimated)."""
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
         {rate_subquery('effective_from')} AS rate_from,
         {estimated_subquery()} AS estimated
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
       date(MAX(ts), 'unixepoch', 'localtime') AS last_activity,
       SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END)
         AS estimated_events
FROM priced GROUP BY path
ORDER BY classic_in + classic_out + cached_r + cached_w DESC, output DESC;
""").fetchall()
    return [dict(zip(STATS_KEYS, r)) for r in rows]


def cost_notes(events, unpriced, estimated, require_estimated=False):
    """The qualifiers a cost figure carries, as a list of short phrases.

    Unpriced and estimated are different things and are reported separately,
    so both stay distinguishable when they occur together:

    - ``"U of M events unpriced"`` when ``0 < unpriced < events`` (an all-
      unpriced figure is rendered as ``unpriced`` by the caller instead);
    - ``"N of M events at an estimated rate"`` when ``0 < estimated <
      events``, or ``"the whole figure is an estimate (every event at an
      estimated rate)"`` when ``estimated == events``.

    Nothing is said for zero counts, and a count of ``None`` (a remote store
    whose report functions predate the figure) is treated as "not reported".

    :param events: the figure's event count ``M``.
    :param unpriced: how many of them resolved to no pricing row.
    :param estimated: how many priced at a family default or ancestor row
        (capture.is_estimated).
    :param require_estimated: when true, the unpriced phrase is withheld
        unless the same figure also carries an estimated marker
        (``estimated > 0``) — keeps the two only-distinguishable together
        (``token-stats``, the scoped rollup); ``project-stats`` always shows
        unpriced on its own and leaves this at the default ``False``.
    :returns: the phrases, in that order; empty when there is nothing to say.
    """
    events = events or 0
    notes = []
    if (unpriced and 0 < unpriced < events
            and (not require_estimated or (estimated and estimated > 0))):
        notes.append(f"{unpriced} of {events} events unpriced")
    if estimated and estimated > 0:
        if estimated >= events:
            notes.append("the whole figure is an estimate"
                         " (every event at an estimated rate)")
        else:
            notes.append(f"{estimated} of {events} events at an estimated"
                         " rate")
    return notes


def with_notes(cell, notes):
    """Append :func:`cost_notes` phrases to a table cost cell as
    ``cell — note, note``; the cell is returned unchanged when there are none.

    :param cell: the rendered cost cell.
    :param notes: phrases from :func:`cost_notes`.
    :returns: the cell with its qualifiers.
    """
    return cell + (" — " + ", ".join(notes) if notes else "")


def own_price_footer(models):
    """The one-line footer naming the models that have no own pricing row
    (every event of theirs is estimated or unpriced), or ``None`` when there
    are none — or when the list was not reported (``None``). Model names are
    untrusted and pass through :func:`md_cell`.

    :param models: model names from :func:`fetch_models_without_own_price`.
    :returns: the footer line, or ``None``.
    """
    if not models:
        return None
    names = ", ".join(f"`{md_cell(m)}`" for m in models)
    pronoun = "its" if len(models) == 1 else "their"
    return (f"No own published price for {names} — {pronoun} cost is an"
            " estimate (family default or nearest listed ancestor rate) or"
            " unpriced; `/token-telemetry:pricing-update` refreshes the"
            " pricing table.")


def split_cell(total, left, right, bold=False):
    t = fmt_usd(total)
    if bold:
        t = f"**{t}**"
    return f"{t} ({fmt_usd(left)} / {fmt_usd(right)})"


def render_project_stats(rows):
    """Render :func:`fetch_project_stats` rows as the ``/project-stats`` table.
    The est. cost cell carries :func:`cost_notes` qualifiers (unpriced and
    estimated events of that project)."""
    out = ["| project | sessions | events | input | output | cache read |"
           " cache write | est. cost (input / output) |"
           " classic (input / output) | cached (read / write) |"
           " first seen | last activity |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    seed_seen = False
    for r in rows:
        project = md_cell(r["name"] or Path(r["path"]).name)
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
            est_cell = with_notes(est_cell, cost_notes(
                r["events"], r["unpriced_events"], r.get("estimated_events")))
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
    """Every breakdown the token-stats report shows, as plain data.

    Besides the rendered breakdowns it carries the estimated-flag data:
    ``estimated_by_model`` maps a model name to its count of events (same
    7-day, backlog-excluded window as ``by_model``) whose resolved pricing row
    makes it an estimate (capture.is_estimated); models with none are omitted.
    ``events_by_model`` maps every ``by_model`` name to its event count in that
    window and ``unpriced_by_model`` to its unpriced-event count (models with
    none omitted) — the ``M`` and ``U`` the per-model and headline cost
    qualifiers need (:func:`cost_notes`). ``models_without_own_price`` is
    :func:`fetch_models_without_own_price` (all-time), rendered as the
    :func:`own_price_footer`.

    ``by_tier`` is ROLE-tiered (see :func:`tier_case`), not model-tiered. Role
    splits are visible there (and in ``by_rung``), NOT in ``by_model``: that
    stays ONE row per model, as on the model-tiered kit, but its tier column
    now lists every tier the model actually served that window, comma-joined
    in :data:`TIER_DISPLAY_ORDER` (e.g. a model used both as the main
    session and as a ``marvin:developer`` subagent reads ``orchestrator,
    heavy``) — its estimated/event/unpriced counts stay per-model exactly as
    before role tiering. ``by_rung`` breaks the 'ladder' tier rows of
    ``by_tier`` down by escalation rung (see :func:`rung_case`); a ladder row
    with no named rung (the model-prefix fallback, or an unrecognized
    ``marvin:escalation-*`` suffix) is grouped under
    :data:`RUNG_FALLBACK_LABEL` instead of dropped, so ``by_rung``'s rows
    always sum to ``by_tier``'s ladder total."""
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
    # by_model, by_tier and by_rung are ONE statement over ONE `tagged` CTE,
    # which splices tier_case() and rung_case() exactly once: every breakdown
    # reads the same per-event `tier`/`rung` columns, so the three can never
    # classify the same event differently (twin of reports.sql's `tagged` CTE
    # in report_token_stats). Each output row carries its breakdown name in
    # column 0; the shared ORDER BY sorts every breakdown by output DESC, then
    # by its label's byte order (BINARY) as the deterministic tie-break —
    # model name, tier name or rung name — matching Postgres' COLLATE "C".
    #
    # by_model is ONE row per model (F1): a role-tiered model that served more
    # than one tier lists them ALL, comma-joined in the kit's display order
    # (tier_rank_case), rather than repeating the model; its
    # estimated/event/unpriced counts are summed across those tiers in SQL.
    # by_rung breaks the 'ladder' rows of by_tier down by escalation rung
    # (high / xhigh / max / frontier, read only from the named
    # marvin:escalation-* persona — see rung_case). A ladder row with no such
    # name (the model-prefix fallback, or an unrecognized marvin:escalation-*
    # suffix) is COALESCEd into RUNG_FALLBACK_LABEL rather than dropped, so
    # the rung breakdown always sums to the tier total; the label is a real
    # GROUP BY key, so it only appears when at least one such row exists.
    rows = conn.execute(f"""
WITH priced AS (
  SELECT e.in_tok, e.out_tok, e.cache_r, e.cache_w, {cw1h} AS cache_w_1h,
         e.kind AS kind, e.agent AS agent,
         m.name AS model_name,
         {rate_subquery('in_usd')} AS in_usd,
         {rate_subquery('out_usd')} AS out_usd,
         {rate_subquery('cache_r_usd')} AS cache_r_usd,
         {rate_subquery('cache_w_usd')} AS cache_w_usd,
         {cw1h_usd} AS cache_w_1h_usd,
         {rate_subquery('effective_from')} AS rate_from,
         {estimated_subquery()} AS estimated
  FROM events e JOIN models m ON m.id = e.model_id
  WHERE e.ts >= strftime('%s','now','-7 days') AND {NOT_BACKLOG}
),
tiered AS (
  SELECT *, {tier_case()} AS tier FROM priced
),
tagged AS (
  SELECT *, CASE WHEN tier = 'ladder'
                 THEN COALESCE({rung_case()}, :rung_fallback) END AS rung
  FROM tiered
),
per_model_tier AS (
  SELECT model_name, tier,
         SUM(in_tok) AS i, SUM(out_tok) AS o,
         SUM(in_tok*COALESCE(in_usd,0) + out_tok*COALESCE(out_usd,0)
             + cache_r*COALESCE(cache_r_usd,0)
             + (cache_w - cache_w_1h)*COALESCE(cache_w_usd,0)
             + cache_w_1h*COALESCE(cache_w_1h_usd, cache_w_usd, 0))
             / 1000000.0 AS cost,
         MAX(rate_from) AS rate_from,
         SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END) AS est_n,
         COUNT(*) AS ev_n,
         SUM(CASE WHEN rate_from IS NULL THEN 1 ELSE 0 END) AS unpriced_n
  FROM tagged GROUP BY model_name, tier
)
SELECT 'by_model' AS part, p1.model_name AS label,
       (SELECT group_concat(t, ', ') FROM (
          SELECT tier AS t FROM per_model_tier p2
          WHERE p2.model_name = p1.model_name
          ORDER BY """ + tier_rank_case("tier") + """
        )) AS tiers,
       SUM(p1.i) AS i, SUM(p1.o) AS o, ROUND(SUM(p1.cost), 4) AS cost,
       MAX(p1.rate_from) AS rate_from, SUM(p1.est_n) AS est_n,
       SUM(p1.ev_n) AS n, SUM(p1.unpriced_n) AS unpriced_n
FROM per_model_tier p1
GROUP BY p1.model_name
UNION ALL
SELECT 'by_tier', tier, NULL, SUM(in_tok), SUM(out_tok), NULL, NULL, NULL,
       COUNT(*), NULL
FROM tagged
GROUP BY tier
UNION ALL
SELECT 'by_rung', rung, NULL, SUM(in_tok), SUM(out_tok), NULL, NULL, NULL,
       COUNT(*), NULL
FROM tagged
WHERE tier = 'ladder'
GROUP BY rung
ORDER BY part, o DESC, label;""",
                        {"rung_fallback": RUNG_FALLBACK_LABEL}).fetchall()
    by_model = [r[1:] for r in rows if r[0] == "by_model"]
    d["by_model"] = [tuple(r[:6]) for r in by_model]
    d["estimated_by_model"] = {r[0]: r[6] for r in by_model if r[6]}
    d["events_by_model"] = {r[0]: r[7] for r in by_model}
    d["unpriced_by_model"] = {r[0]: r[8] for r in by_model if r[8]}
    d["models_without_own_price"] = fetch_models_without_own_price(conn)
    d["by_kind"] = conn.execute(
        "SELECT CASE kind WHEN 0 THEN 'main' ELSE 'subagent' END,"
        " SUM(in_tok), SUM(out_tok),"
        " ROUND(100.0 * SUM(cache_r) / NULLIF(SUM(in_tok) + SUM(cache_r), 0), 1)"
        " FROM events WHERE ts >= strftime('%s','now','-7 days')"
        f" AND {NOT_BACKLOG}"
        " GROUP BY kind").fetchall()
    d["by_tier"] = [(r[1], r[3], r[4], r[8]) for r in rows
                    if r[0] == "by_tier"]
    d["by_rung"] = [(r[1], r[3], r[4], r[8]) for r in rows
                    if r[0] == "by_rung"]
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
    """Render :func:`fetch_token_stats` data as the ``/token-stats`` markdown.

    Cost figures carry :func:`cost_notes` qualifiers (unpriced and estimated
    events, per model and summed in the headline) and the output ends with
    the :func:`own_price_footer` when any model has no own price. Keys a
    remote store did not report (``None``) render as "nothing to say"."""
    def tok_row(label, t):
        return [label, fmt_n(t[0]), fmt_n(t[1]), fmt_n(t[2]), fmt_n(t[3]),
                str(t[4])]

    est_by = d.get("estimated_by_model") or {}
    ev_by = d.get("events_by_model")
    un_by = d.get("unpriced_by_model") or {}

    def model_notes(name):
        if ev_by is None:
            return []
        return cost_notes(ev_by.get(name, 0), un_by.get(name, 0),
                          est_by.get(name, 0), require_estimated=True)

    week_cost = sum(r[4] for r in d["by_model"])
    rates = [r[5] for r in d["by_model"] if r[5] is not None]
    if not rates:
        cost_label = "unpriced"
    elif min(rates) == 0:
        cost_label = f"{fmt_usd(week_cost)} (seed rates)"
    else:
        cost_label = (f"{fmt_usd(week_cost)} (rates as of "
                      + datetime.date.fromtimestamp(max(rates)).isoformat() + ")")
    week_notes = ([] if ev_by is None else cost_notes(
        sum(ev_by.values()), sum(un_by.values()), sum(est_by.values()),
        require_estimated=True))
    if week_notes:
        cost_label += "; " + ", ".join(week_notes)
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
        [tok_row(md_cell(r[0]), r[1:]) for r in d["by_project"]])
    out += ["", "**By model (7 days)**", ""]
    model_rows = []
    for name, tier, inp, outp, cost, rate_from in d["by_model"]:
        label = ("unpriced" if rate_from is None
                 else with_notes(fmt_usd(cost)
                                 + (" (seed rates)" if rate_from == 0 else ""),
                                 model_notes(name)))
        model_rows.append([md_cell(name), tier, fmt_n(inp), fmt_n(outp), label])
    out += md_table(["model", "tier", "input", "output", "est. cost"],
                    ["---", "---", "---:", "---:", "---:"], model_rows)
    out += ["", "**By agent and kind (7 days)**", ""]
    out += md_table(["agent", "input", "output", "events"],
                    ["---", "---:", "---:", "---:"],
                    [[md_cell(r[0]), fmt_n(r[1]), fmt_n(r[2]), str(r[3])]
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
    if d.get("by_rung"):
        out += ["", "**By ladder rung (7 days)**", ""]
        out += md_table(["rung", "input", "output", "events"],
                        ["---", "---:", "---:", "---:"],
                        [[r[0], fmt_n(r[1]), fmt_n(r[2]), str(r[3])]
                         for r in d["by_rung"]])
    if d["by_issue"]:
        out += ["", "**By issue (all-time)**", ""]
        out += md_table(
            ["issue", "input", "output", "cache read", "cache write",
             "events"],
            ["---", "---:", "---:", "---:", "---:", "---:"],
            [tok_row(md_cell(r[0]), r[1:]) for r in d["by_issue"]])
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
    footer = own_price_footer(d.get("models_without_own_price"))
    if footer:
        out += ["", footer]
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
    restricted to a caller-supplied set of event rowids. ``estimated`` counts
    the estimated events (capture.is_estimated)."""
    empty = {"in_tok": 0, "out_tok": 0, "cache_r": 0, "cache_w": 0,
             "events": 0, "cost": 0.0, "rate_from": None, "unpriced": 0,
             "estimated": 0}
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
         {rate_subquery('effective_from')} AS rate_from,
         {estimated_subquery()} AS estimated
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
       SUM(CASE WHEN rate_from IS NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END)
FROM priced""", rowids).fetchone()
    inp, outp, cr, cw, n, cost, rate_from, unpriced, estimated = row
    return {"in_tok": inp, "out_tok": outp, "cache_r": cr, "cache_w": cw,
            "events": n, "cost": cost or 0.0, "rate_from": rate_from,
            "unpriced": unpriced or 0, "estimated": estimated or 0}


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
        "estimated": sum(x["estimated"] for x in covered),
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
    """Render :func:`fetch_scoped_rollup` data. A covered rollup's cost line
    ends with the :func:`cost_notes` qualifiers (unpriced and estimated events
    across the scoped set)."""
    out =["", "**Scoped rollup**", ""]
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
    notes = cost_notes(d["events"], d.get("unpriced"), d.get("estimated"),
                       require_estimated=True)
    out.append(f"**{cost_label}** — {fmt_n(d['events'])} events,"
               f" {fmt_n(d['in_tok'])} input / {fmt_n(d['out_tok'])} output"
               " tokens" + ("; " + ", ".join(notes) if notes else "") + ".")
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

def fetch_info_central(conn, project_path):
    """The DB-derived portion of ``/info`` — the ``central`` dict plus this
    project's event count — as one plain dict. Factored out of :func:`fetch_info`
    so it is the SINGLE definition of the info aggregation, shared by the local
    render, the storage seam's ``read_for_report("info")``, and the cross-dialect
    golden test. ``project_path`` scopes ``events_here`` to one project (the
    caller's resolved project root)."""
    events, first_day, last_day = conn.execute(
        "SELECT COUNT(*), MIN(date(ts,'unixepoch','localtime')),"
        " MAX(date(ts,'unixepoch','localtime')) FROM events").fetchone()
    # Latest rate already in force — a pre-inserted future-dated row (e.g.
    # a published price change) must not masquerade as the current rate.
    pricing_rows, latest = conn.execute(
        "SELECT COUNT(*), MAX(CASE WHEN effective_from <="
        " strftime('%s','now') THEN effective_from END) FROM pricing"
    ).fetchone()
    events_here = conn.execute(
        "SELECT COUNT(*) FROM events e"
        " JOIN sessions s ON s.id = e.session_id"
        " JOIN projects p ON p.id = s.project_id WHERE p.path = ?",
        (project_path,)).fetchone()[0]
    return {
        "schema": conn.execute("PRAGMA user_version").fetchone()[0],
        "events": events, "first_day": first_day, "last_day": last_day,
        "projects": conn.execute(
            "SELECT COUNT(*) FROM projects").fetchone()[0],
        "pricing_rows": pricing_rows, "latest_rate_from": latest,
        "events_here": events_here,
    }


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
        c = fetch_info_central(conn, str(root))
        d["events_here"] = c.pop("events_here")
        d["central"] = c

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


# --------------------------------------------------------------- remote routing

# Reports that the remote backend can serve through server-side aggregation
# (its RPC). `storage-status` describes the LOCAL store (the durable outbox +
# cursor DB that stays local even under a remote backend) and `--scope` is a
# local-project git/rowid rollup, so both always take the local path below.
REMOTE_REPORTS = ("info", "project-stats", "token-stats")


def render_remote_report(command, remote, db, cwd):
    """Render one report from the active remote backend's server-side
    aggregation. Reuses the SAME ``render_*`` functions as the local path — only
    the data source differs — because ``remote.read_for_report`` returns the
    identical Python shape as the local ``fetch_*`` (that equivalence is what the
    cross-dialect golden test guards). For ``info``, the local-filesystem portion
    (plugin/sidecar/error-log/mirror) is read locally exactly as before and only
    the DB-derived ``central`` block + this-project count come from the remote."""
    if command == "project-stats":
        return render_project_stats(remote.read_for_report("project-stats") or [])
    if command == "token-stats":
        return render_token_stats(remote.read_for_report("token-stats"))
    # info: local bits (conn=None) overlaid with the remote central aggregation.
    d = fetch_info(None, db, cwd)
    central = remote.read_for_report("info", project_path=d["root"])
    if central is not None:
        d["events_here"] = central.pop("events_here")
        d["central"] = central
    return render_info(d)


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

    # Remote read parity: when a remote backend is active, the three aggregation
    # reports show the CENTRAL (remote) view via server-side aggregation. The
    # local path below is untouched — byte-for-byte identical — for the default
    # `local` backend and for `--scope`/`storage-status`, which stay local.
    remote = storage.remote_backend_if_active()
    if (remote is not None and args.scope is None
            and args.command in REMOTE_REPORTS):
        try:
            print(render_remote_report(args.command, remote, db, args.cwd))
        except Exception:
            print("The remote telemetry backend could not be read right now"
                  " (it is unreachable or not yet provisioned). The local"
                  " store is unaffected; try again, or check"
                  " `/token-telemetry:info`.")
        finally:
            remote.close()
        return 0

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
