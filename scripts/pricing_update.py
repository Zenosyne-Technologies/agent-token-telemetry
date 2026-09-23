#!/usr/bin/env python3
"""Deterministic pricing refresh from Anthropic's published pricing page.

`pricing_update.py [--db PATH] [--html FILE]` fetches
https://platform.claude.com/docs/en/about-claude/pricing, parses the model
pricing table, diffs it against the `pricing` table and inserts effective-dated
rows per the insert-only contract (docs/TELEMETRY-CONTRACT.md), then prints a
finished markdown report. The command prompt runs this and echoes stdout
verbatim.

Three distinct exit codes cover a run that mints nothing (AOS-143 correction,
F1): `EXIT_BOUNDS_REFUSED` (1) — the page was REFUSED by a parser bound (an
out-of-bounds/malformed rate, or over `MAX_CANDIDATES` rows) — nothing is
wrong with fetching or reading the page, the page itself is untrustworthy, so
the LLM flow reports this and STOPS; `EXIT_FETCH_FAILED` (2) — the page (or
`--html` file) could not be read at all (network down, HTTP error, timeout,
missing/unreadable `--html` path) — this is the ONLY exit code the manual
fallback exists for; `EXIT_OTHER_ERROR` (3) — the page was read but its
structure did not parse (layout changed, table/column not found), an
unexpected error occurred, or the `--html` file existed but failed its
pre-read bounds check (not a regular file, or over `FETCH_MAX_BYTES` —
AOS-143 round 4, F2, :func:`_read_html_file`) — also no fallback, since a
page/file that reads but does not parse or bound-check as expected is
exactly the kind of anomaly the fallback's manual read must not be trusted
with either.

`--backfill-plan [--json]` prints the consent-gated backfill plan (estimated
events that a copy of their model's own, later-minted rate would re-price);
it reads the DB read-only (writes no DB row) and caches a plan summary in
`backfill-plan.json` next to the DB for the dashboard (see below).
`--backfill-apply PREFIX...` applies the user-confirmed prefixes
— contract §Pricing table, "Third narrow case" (consent-gated backfill). Neither
fetches the page. `--backfill-apply` exits 2, touching nothing, when any
argument is not a well-formed pricing prefix (:func:`is_pricing_prefix`), and 1
when the apply is refused or rolled back (nothing written). `--backfill-apply`
consumes every remaining raw argument as a candidate prefix (validated before
argument parsing even runs — see :func:`_reject_option_shaped_backfill_apply`),
so `--db` (only — see below) MUST be given before `--backfill-apply` on the
command line; anything after it that is not a well-formed prefix, including
another flag, is rejected by position, never echoed. `--backfill-plan` and
`--backfill-apply` are mutually exclusive — rejected with exit 2 before any
DB is opened if both appear anywhere on the command line
(:func:`_reject_combined_backfill_flags`), and again by the argparse parser
itself, so a pre-approved `--backfill-plan ...` prefix match can never also
apply. `--html` is refresh-only: backfill modes never fetch or parse a page,
so `--html` (or `--html=...`) is rejected with exit 2 alongside either
`--backfill-plan` or `--backfill-apply`, anywhere on the command line, before
argparse runs and before the DB or the HTML file is ever opened
(:func:`_reject_html_with_backfill`), and again as a third layer by the
argparse mutually-exclusive group — so a pre-approved `--html ...` prefix
match can never reach a backfill apply or plan, regardless of what other
flags precede or follow it.

A `--backfill-plan` run over the dashboard's own DB also caches a tiny summary
of the plan (bundle count, combined delta, computed-at, fingerprint) for the
dashboard's own-price banner, and a successful `--backfill-apply` clears it —
see `scripts/backfill_summary.py`. Neither changes stdout or the exit code.

Backend seam: DB work goes through capture.connect() (the schema owner);
parsing and planning are pure functions over plain data, reusable unchanged
when other database backends arrive.
"""
import argparse
import datetime
import decimal
import json
import math
import os
import re
import stat
import sys
import threading
import unicodedata
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
MIN_INOUT_RATE_USD = 0.01       # in_usd/out_usd floor (AOS-143 correction,
                                 # F4): $0 was already refused; anything under
                                 # a cent/MTok is effectively free and just as
                                 # implausible as the $10,000 ceiling.
MAX_CANDIDATES = 500            # candidate rows a single run may mint
BACKDATE_MAX_DAYS = 365         # oldest a `starting <d>` row may be minted at

# Fetch bounds (AOS-143 correction, round 2 — hang/DoS fix): a hostile or
# merely slow server could otherwise hang the fetch, or exhaust memory with
# an oversized/unbounded body, without ever tripping urllib's own per-socket-
# operation `timeout=` (see :func:`_fetch_page`).
FETCH_TIMEOUT_S = 30.0           # wall-clock seconds for connect + the FULL
                                  # read together, not per socket operation
FETCH_MAX_BYTES = 5 * 1024 * 1024  # cap on the fetched page's total body size

# main()'s three distinct exit codes for a run that writes nothing (AOS-143
# correction, F1) — see the module docstring for what each one means and why
# only EXIT_FETCH_FAILED reaches the command's manual fallback.
EXIT_BOUNDS_REFUSED = 1
EXIT_FETCH_FAILED = 2
EXIT_OTHER_ERROR = 3

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Unicode bidi-control characters: U+200E LRM, U+200F RLM, U+061C ALM (AOS-143
# correction, F5 — these carry the Bidi_Control property but were missing
# from the original range-only set below) plus U+202A-U+202E and
# U+2066-U+2069, which can visually reorder or spoof rendered/terminal text
# (mirrors report.md_cell).
_BIDI_CHAR_RE = re.compile(r"[‎‏؜‪-‮⁦-⁩]")
_ERROR_TEXT_MAX = 200


def _safe_error_text(value):
    """Make untrusted page text (a raw cell, a header row) safe to embed in
    an exception message that may reach stderr or a terminal: strip ASCII
    control characters and DEL (U+0000-U+001F, U+007F) AND the C1 control
    range (U+0080-U+009F, which includes U+0085 NEL and U+009B — the 8-bit
    form of CSI, usable to start a terminal escape sequence without a 7-bit
    ESC byte), strip Unicode bidi-control characters, then cap length so one
    hostile page cell cannot blow up the printed message. A parse-failure
    message must never carry raw, unsanitized page text.

    :param value: the untrusted text (or a list/tuple of cells — stringified
        first).
    :returns: the sanitized, length-capped text.
    """
    s = _BIDI_CHAR_RE.sub("", _CONTROL_CHAR_RE.sub("", str(value)))
    return s if len(s) <= _ERROR_TEXT_MAX else s[:_ERROR_TEXT_MAX] + "…"


# Cell text follows RENDERED-TEXT semantics (AOS-151 security correction,
# round 2, C1): a cell's text is what a browser shows, not a join of its raw
# text nodes. A comment contributes nothing and is never a boundary (the live
# page is React SSR and splits text nodes with ``<!-- -->``); an inline
# element concatenates with its neighbours with no inserted space (so
# ``4<b>.5</b>`` reads ``4.5``, as it renders); a block-level element, a
# ``<br>``, or a flex/grid ITEM is a boundary and inserts one space (so
# ``5<br>1M-token`` reads ``5 1M-token``). Every Unicode PRIVATE-USE code
# point (category ``Co``) is then dropped (AOS-151 security correction,
# round 2, R2-2) — these are icon-font glyphs, assigned per font and never
# text, so the live page's retired/invite-only badge (a ``Co`` glyph sitting
# right after the model version, e.g. U+E0F0) is removed rather than relied
# on to be a whitespace boundary; before this, that boundary was recognized
# only via the badge's exact ``inline-flex`` container class
# (:data:`_FLEX_GRID_CLASSES`), so a class rename or an unstyled container
# made the whole run refuse. Whitespace then collapses to single spaces —
# again after the ``Co`` strip, so removing a glyph between two boundary
# spaces never leaves a double space behind.
#
# Block-level: the HTML UA-stylesheet elements whose default ``display`` is
# block, list-item or a table part.
_BLOCK_TAGS = frozenset((
    "address", "article", "aside", "blockquote", "body", "caption", "center",
    "col", "colgroup", "dd", "details", "dialog", "dir", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hgroup", "hr", "html", "legend", "li",
    "listing", "main", "menu", "nav", "ol", "p", "plaintext", "pre",
    "search", "section", "summary", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "ul", "xmp"))
# Void elements never get an end tag, so they are never pushed on the
# element stack.
_VOID_TAGS = frozenset((
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr"))
# Flex/grid containers BLOCKIFY their children (CSS Display 3 §2.7): every
# child element, and every run of text directly inside, becomes its own
# block-level flex/grid item, so two inline elements that are siblings inside
# a flex container render as separate boxes, not one run of text. This is
# how the live page separates a model name from its tagline: the name cell is
# ``<div class="flex min-w-0 flex-col"><a …>Claude Opus 5.5</a><span …>For
# long-running…</span></div>`` — an ``<a>`` and a ``<span>``, both inline, with
# no whitespace between them, shown on two lines only because their parent is
# a flex column. (Retired-model badges also sit in a ``<span
# class="inline-flex …">``, but their private-use glyph is dropped outright
# by :func:`_strip_private_use` regardless of this container recognition —
# round 2, R2-2 — so a class rename or an unstyled badge container no longer
# blocks the run.) The page's CSS is Tailwind utility classes, so a container
# is recognized by an exact unprefixed class token below (a responsive/state
# variant such as ``md:flex`` is conditional and ignored) or by an inline
# ``style`` declaring ``display: [inline-]flex|grid``. No other CSS is
# modelled: an element restyled some other way reads as its tag's default,
# and where that glues text onto a version the strict version boundary in
# :func:`_version_boundary_ok` refuses the run rather than guessing.
_FLEX_GRID_CLASSES = frozenset(("flex", "inline-flex", "grid", "inline-grid"))
_FLEX_GRID_STYLE_RE = re.compile(
    r"(?:^|;)\s*display\s*:\s*(?:inline-)?(?:flex|grid)\b", re.I)


# A browser clamps a cell's colspan to 1..1000 (HTML "rules for parsing
# non-negative integers"; 0 or unparseable -> 1).
_MAX_COLSPAN = 1000
_COLSPAN_RE = re.compile(r"[\t\n\f\r ]*\+?([0-9]+)")


def _colspan(attrs):
    """A cell's column span as a browser computes it: the leading
    non-negative integer of its ``colspan`` attribute, 1 when the attribute
    is absent, unparseable or 0, and clamped to :data:`_MAX_COLSPAN`. A
    digit run longer than four characters is already over the clamp, so it
    is never converted (a hostile 5 MB digit string costs nothing).

    :param attrs: the cell's ``(name, value)`` attribute pairs.
    :returns: the span, ``1``..``_MAX_COLSPAN``.
    """
    for name, value in attrs:
        if name != "colspan":
            continue
        m = _COLSPAN_RE.match(value or "")
        if not m:
            return 1
        digits = m.group(1)
        n = _MAX_COLSPAN if len(digits) > 4 else int(digits)
        return min(max(n, 1), _MAX_COLSPAN)
    return 1


class _Row(list):
    """One table row: a list of cell texts, plus ``width`` — the row's
    EFFECTIVE width, the sum of its cells' :func:`_colspan` (AOS-151 security
    correction, round 2, C3). A plain list compares equal to it."""

    def __init__(self):
        super().__init__()
        self.width = 0


def _is_flex_or_grid_container(attrs):
    """Whether an element's attributes make it a flex or grid container
    (see :data:`_FLEX_GRID_CLASSES`): an exact ``flex``/``inline-flex``/
    ``grid``/``inline-grid`` class token, or an inline ``style`` declaring
    ``display: [inline-]flex|grid``.

    :param attrs: the ``(name, value)`` pairs :class:`HTMLParser` passes to
        ``handle_starttag``.
    :returns: ``True`` for a flex/grid container, else ``False``.
    """
    for name, value in attrs:
        if not value:
            continue
        if name == "class" and not _FLEX_GRID_CLASSES.isdisjoint(value.split()):
            return True
        if name == "style" and _FLEX_GRID_STYLE_RE.search(value):
            return True
    return False


def _strip_private_use(text):
    """Drop every Unicode PRIVATE-USE code point (category ``Co``) from
    ``text`` (AOS-151 security correction, round 2, R2-2).

    A ``Co`` code point is an icon-font glyph — assigned per font, never
    text — so it is removed outright rather than trusted to behave like a
    character a page author could have typed (in particular, trusted to be
    whitespace or to sit only where a container's flex/grid styling makes
    it one). This runs before :class:`TableCollector`'s own whitespace
    collapse, so a glyph removed from between two boundary spaces never
    leaves a double space behind.

    :param text: the raw, uncollapsed cell text.
    :returns: ``text`` with every ``Co``-category character removed.
    """
    return "".join(ch for ch in text if unicodedata.category(ch) != "Co")


class TableCollector(HTMLParser):
    """Every <table> as a list of rows, each row a list of cell texts.

    A cell's text is its RENDERED text (AOS-151 security correction, round
    2, C1): comments contribute nothing, inline elements concatenate with no
    inserted space, block-level elements, ``<br>`` and flex/grid items
    (:data:`_FLEX_GRID_CLASSES`) insert one boundary space, every Unicode
    PRIVATE-USE code point (:func:`_strip_private_use`, round 2, R2-2) is
    dropped, and whitespace collapses to single spaces. Inside a cell an
    element stack tracks which open element is a flex/grid container (its
    children are blockified) and which ones must emit a boundary when they
    close; a per-tag count keeps an end tag with no matching open element an
    O(1) no-op, so a flood of stray end tags can never make each one rescan
    a deep stack."""

    def __init__(self):
        super().__init__()
        self.tables, self._rows, self._row, self._cell = [], None, None, None
        self._cell_span = 1
        # In-cell element stack: [tag, is_flex_or_grid_container,
        # emits_a_boundary_when_closed] per open element, plus a count of
        # each tag's open entries.
        self._stack, self._open = [], {}

    def _start_cell(self, attrs):
        self._cell, self._cell_span = [], _colspan(attrs)
        self._stack = [["td", _is_flex_or_grid_container(attrs), False]]
        self._open = {}

    def _in_cell_start(self, tag, attrs):
        parent_is_container = self._stack[-1][1] if self._stack else False
        if tag in _VOID_TAGS:
            # <br>/<hr> always break the line; any other void element (an
            # <img>, say) is a boundary only as a flex/grid item. <wbr>
            # generates no box and never breaks the text.
            if tag in ("br", "hr") or (parent_is_container and tag != "wbr"):
                self._cell.append(" ")
            return
        boundary = tag in _BLOCK_TAGS or parent_is_container
        if boundary:
            self._cell.append(" ")
        self._stack.append([tag, _is_flex_or_grid_container(attrs), boundary])
        self._open[tag] = self._open.get(tag, 0) + 1

    def _in_cell_end(self, tag):
        if tag in _VOID_TAGS:
            # A stray </br> is a <br> to a browser; other void end tags are
            # ignored.
            if tag == "br":
                self._cell.append(" ")
            return
        if not self._open.get(tag):
            # No matching open element: a stray block end tag still breaks
            # the line (a browser opens and closes an empty one); a stray
            # inline end tag is ignored.
            if tag in _BLOCK_TAGS:
                self._cell.append(" ")
            return
        # Close up to and including the most recent open ``tag`` — every
        # element it implicitly closes emits its own boundary as well.
        while True:
            name, _container, boundary = self._stack.pop()
            self._open[name] -= 1
            if boundary:
                self._cell.append(" ")
            if name == tag:
                break

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._row = _Row()
        elif tag in ("td", "th") and self._row is not None:
            self._start_cell(attrs)
        elif self._cell is not None:
            self._in_cell_start(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            # Strip private-use glyphs (AOS-151 security correction, round
            # 2, R2-2) BEFORE collapsing whitespace (any Unicode whitespace,
            # as it always has) to single spaces and trimming — the
            # boundary spaces inserted above included — so a glyph the
            # strip removes never leaves a double space in the result.
            text = _strip_private_use("".join(self._cell))
            self._row.append(" ".join(text.split()))
            self._row.width += self._cell_span
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None
        elif self._cell is not None:
            self._in_cell_end(tag)

    def handle_data(self, data):
        # A comment never reaches here (HTMLParser.handle_comment is a
        # no-op), so the text on either side of one concatenates directly.
        if self._cell is not None:
            self._cell.append(data)


# The full contiguous "numeric-ish" run after a `$` — digits plus every
# character a malformed or obfuscated rate could plausibly contain — captured
# WHOLE so it can be validated as a unit, rather than matching only a
# well-formed prefix and silently dropping the rest (AOS-143 correction: that
# used to mint `$1,500` as `$1`). The class is the union of two generations
# of that fix: the original round's comma/dot/exponent/sign set (comma, extra
# dot, `e`/`E` exponent marker, `+`/`-` sign — dropping any of these from the
# class would silently re-open the exponent-truncation bug it fixed, e.g.
# `$1e309` collapsing to the leading `$1`) plus AOS-143 correction, round 2,
# F6's additional separator/suffix/notation characters found to survive the
# first round's class and still truncate silently: an apostrophe or
# underscore thousands separator, four non-ASCII "thousands-grouping" space
# characters plus a literal ASCII space (thin space U+2009, narrow no-break
# space U+202F, no-break space U+00A0, figure space U+2007), the `k`/`K`/
# `m`/`M` magnitude suffixes, and `x`/`X`/`^` for a written-out `1x10^6`
# exponent. A char outside this whole class (e.g. a non-ASCII digit such as
# `١`) never starts a run at all, which is the pre-existing, still-safe "no
# $-prefixed numeric token" path (:func:`money` returns ``None``, and
# :func:`parse_models` raises its own generic, non-refusing parse error).
_MONEY_TOKEN_RE = re.compile(
    r"\$ ?([0-9.,'_     kKmMxX^+\-eE]+)")
# The only token shape money() accepts: ASCII digits, with at most one `.`.
# Any other character surviving inside the captured run above — a
# thousands/grouping separator, a magnitude suffix, a written-out exponent,
# a sign, a second `.`, a classic `e`/`E` exponent marker — fails this match
# and refuses the whole run (AOS-143 correction, round 2, F6) instead of
# silently keeping only the run's well-formed leading digits.
_STRICT_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")

# Characters that, found immediately after a money() token's matched run,
# mean the run was TRUNCATED rather than complete (AOS-143 round 4, N1):
# each is a digit-group or decimal separator lookalike that
# :data:`_MONEY_TOKEN_RE`'s character class does not recognize, so the run
# stops one character early and leaves only the leading digits looking
# well-formed — e.g. `$1，500` (fullwidth comma) matches only `1`, which
# passes :data:`_STRICT_NUMBER_RE` unless this lookahead also refuses it.
_TRAILING_SEPARATOR_LOOKALIKES = frozenset("，٫٬'’_.")


def _rate_token_truncated(text, pos):
    """True if the character at ``text[pos]`` — immediately following a
    :func:`money` token's matched run — shows the run was TRUNCATED rather
    than complete (AOS-143 round 4, N1), instead of the cell genuinely
    ending there (or continuing with unrelated text such as `` / MTok``):

    - a Unicode digit the token's own character class does not recognize
      (``ch.isdigit()`` or Unicode category ``Nd`` — covers Arabic-Indic,
      fullwidth, and other non-ASCII digit scripts);
    - a digit-group or decimal-separator lookalike
      (:data:`_TRAILING_SEPARATOR_LOOKALIKES`);
    - a dash (Unicode category ``Pd``, or the ASCII hyphen) immediately
      followed by a digit — a written-out range such as ``$3–4``.

    :param text: the original, untrimmed cell text.
    :param pos: the index immediately after the matched run
        (:meth:`re.Match.end`).
    :returns: ``True`` when the character at ``pos`` indicates truncation.
    """
    if pos >= len(text):
        return False
    ch = text[pos]
    if ch.isdigit() or unicodedata.category(ch) == "Nd":
        return True
    if ch in _TRAILING_SEPARATOR_LOOKALIKES:
        return True
    if ch == "-" or unicodedata.category(ch) == "Pd":
        nxt = text[pos + 1] if pos + 1 < len(text) else ""
        if nxt and (nxt.isdigit() or unicodedata.category(nxt) == "Nd"):
            return True
    return False


def money(text):
    """Parse a ``$<amount> / MTok`` cell to a float, or ``None`` when the
    cell has no ``$``-prefixed numeric token at all (a genuinely non-numeric
    cell — :func:`parse_models` raises its own "unparseable rate cell" error
    for that case; this also covers a token that starts with a character
    :data:`_MONEY_TOKEN_RE`'s class does not recognize at all, such as a
    non-ASCII digit like ``١`` — no run is captured, so nothing is refused
    either, matching the safe pre-existing behaviour for that case).

    When a ``$``-prefixed token IS present, the WHOLE contiguous run
    (:data:`_MONEY_TOKEN_RE`) — trailing ASCII spaces trimmed, since a
    legitimate cell continues `` / MTok`` and the space before the slash is
    itself a run character — must be a plain, unambiguous decimal number —
    ASCII digits with at most one ``.`` (:data:`_STRICT_NUMBER_RE`) — or
    :class:`PricingRefused` is raised (AOS-143 correction, extended round 2,
    F6): a thousands separator, whether a comma (``$1,500``), an apostrophe
    (``$1'500``), an underscore (``$1_500``), or a thousands-grouping space —
    ASCII or one of four Unicode space variants (``$1 500``); more than one
    decimal point (``$4.00.00``); a magnitude suffix (``$1k``, ``$1M``); a
    written-out exponent (``$1x10^6``); a leading sign (``$-4``, ``$+4``); or
    a classic exponent marker, complete (``$1e309``) or dangling after a bare
    ``.`` (``$1.e3``) — used to be silently truncated to its leading digits
    (``$1``) instead of refusing the malformed cell outright; round 2 closed
    the remaining separator/suffix/notation shapes the original class let
    through silently the same way. Magnitude and the in/out floor are
    :func:`parse_models`'s job (:data:`MAX_RATE_USD`, :data:`MIN_INOUT_RATE_USD`),
    so every caller shares the one enforcement point for those; a malformed
    TOKEN, by contrast, is refused right here, since no caller should ever
    see a guessed-at number.

    The run can still be truncated rather than malformed (AOS-143 round 4,
    N1): a character right after the matched run that :data:`_MONEY_TOKEN_RE`'s
    class does not recognize (e.g. a non-ASCII digit, a fullwidth or Arabic
    separator, or an en/em dash immediately followed by a digit) simply ends
    the run one character early, leaving only its well-formed leading digits
    to pass :data:`_STRICT_NUMBER_RE` — silently minting a wrong number
    (``$3٠٠`` -> ``3``, ``$3–4`` -> ``3``) instead of refusing the cell.
    :func:`_rate_token_truncated` checks the character immediately following
    the run for exactly this shape and raises here too."""
    m = _MONEY_TOKEN_RE.search(text)
    if not m:
        return None
    token = m.group(1).rstrip(" ")
    if not _STRICT_NUMBER_RE.match(token) or _rate_token_truncated(text, m.end()):
        raise PricingRefused(
            "Pricing refresh REFUSED — nothing written: malformed rate cell"
            " (expected a plain decimal number such as $3 or $3.75 — not a"
            " thousands separator, a magnitude suffix, scientific/written-out"
            " exponent notation, a sign, or a truncated/ambiguous run):"
            f" {_safe_error_text(text)}")
    return float(token)


# The rate-table header layouts the parser recognizes. Only WHERE the header
# sits and WHAT its cells say differ between them; the data rows below it go
# through the one shared row loop in :func:`parse_models`, so every AOS-143
# bound applies identically whichever layout matched. Header text is matched
# case-insensitively: the page has shipped both title case ("Base Input
# Tokens") and sentence case ("Base input tokens") for the same columns.
#
# Two-row layout (the live page since at least 2026-09-23): a column-group row
# ("Model" | "Base tokens" colSpan=2 | "Prompt caching" colSpan=3) above the
# per-column row ("Name" | "Input" | "Output" | "5m writes" | "1h writes" |
# "Hits and refreshes"). The per-column row is the one aligned with the data
# cells, so it is the header the columns are mapped from.
_TWO_ROW_GROUP_MARKERS = ("base tokens", "prompt caching")
_TWO_ROW_NEEDLES = (("in", "input"), ("out", "output"),
                    ("w5", "5m writes"), ("w1h", "1h writes"),
                    ("cr", "hits and refreshes"))
# Single-row layout (the page before 2026-09): one header row, "Model" |
# "Base input tokens" | "5m cache writes" | "1h cache writes" | "Cache hits
# and refreshes" | "Output tokens". Still accepted so an `--html` save of the
# older page (and the committed fixtures) keeps parsing.
_SINGLE_ROW_MARKER = "base input tokens"
_SINGLE_ROW_NEEDLES = (("in", "base input"), ("out", "output"),
                       ("w5", "5m cache"), ("w1h", "1h cache"),
                       ("cr", "cache hits"))


def _locate_rate_table(tables):
    """Find the model-pricing table and split it into header and data rows.

    The first table, in page order, that matches either recognized layout
    wins: the two-row layout (its first row carries both
    :data:`_TWO_ROW_GROUP_MARKERS`; the header is its SECOND row) or the
    single-row layout (a first-row cell contains :data:`_SINGLE_ROW_MARKER`).
    Other tables on the page (batch, fast mode, tool-use token counts) carry
    neither marker set and are ignored.

    :param tables: :class:`TableCollector` ``tables`` (rows of cell texts).
    :returns: ``(header_row, data_rows, needles)`` — the per-column header,
        the rows below it, and the ``(key, needle)`` pairs to map it with.
    :raises ValueError: when no table matches either layout (a structural
        parse failure, :data:`EXIT_OTHER_ERROR`).
    """
    for t in tables:
        if not t:
            continue
        first = [c.lower() for c in t[0]]
        if len(t) >= 2 and all(any(m in c for c in first)
                               for m in _TWO_ROW_GROUP_MARKERS):
            return t[1], t[2:], _TWO_ROW_NEEDLES
        if any(_SINGLE_ROW_MARKER in c for c in first):
            return t[0], t[1:], _SINGLE_ROW_NEEDLES
    raise ValueError("model pricing table not found on the page")


def _map_rate_columns(header, needles):
    """Map each rate key to its column index in ``header``.

    Every needle must match EXACTLY ONE header cell (case-insensitive
    substring), no two keys may share a column, and no rate may sit in
    column 0 (the model-name column every row is identified by). Anything
    else — a genuinely absent column, an ambiguous header, a shifted layout —
    raises rather than mapping a wrong column to a rate.

    :param header: the per-column header row (cell texts).
    :param needles: ``(key, needle)`` pairs for the matched layout.
    :returns: ``{"in"|"out"|"w5"|"w1h"|"cr": column index}``.
    :raises ValueError: on any mapping failure, with the header text passed
        through :func:`_safe_error_text`.
    """
    cells = [c.lower() for c in header]
    col = {}
    for key, needle in needles:
        hits = [i for i, c in enumerate(cells) if needle in c]
        if len(hits) == 1:
            col[key] = hits[0]
    if (len(col) != len(needles) or len(set(col.values())) != len(col)
            or 0 in col.values()):
        raise ValueError("unexpected pricing table header:"
                         f" {_safe_error_text(header)}")
    return col


# A data row's model name + version (AOS-151 security correction, N1/N2):
# ``Claude <Family> <version>``, where the version is CAPPED at two
# `.`-separated components of at most three digits each. The cap alone would
# still let a longer/malformed version silently truncate to a well-formed-
# looking prefix (e.g. ``9_9`` -> ``9``, or a 5,000-digit run -> its leading
# three digits) — :func:`_version_boundary_ok` is what turns that truncation
# into a refusal, by checking the character the grammar stopped at.
_MODEL_NAME_RE = re.compile(
    r"Claude\s+(" + "|".join(FAMILIES) + r")"
    r"\s+([0-9]{1,3}(?:\.[0-9]{1,3})?)")


def _version_boundary_ok(text, pos):
    """Whether a model version :data:`_MODEL_NAME_RE` matched in ``text`` is
    COMPLETE rather than truncated by the regex's capped grammar (AOS-151
    security correction, N1/N2; strict since round 2, C2).

    The character at ``text[pos]`` — immediately after the matched version
    (:meth:`re.Match.end`) — must be end-of-text or Unicode whitespace
    (``str.isspace``). ANYTHING else refuses: a letter or digit of any
    script (a tagline glued onto the digits, an extra digit run past the
    3-digit cap), ``.``/``_``/``-``/``,``/``/`` (a third component, a
    ``9_9``/``4-5``/``4,5``/``4/5`` continuation), a separator lookalike
    (``٫``, ``．``, ``․``, ``·``, ``–``, ``‐``, ``＿`` …) and an invisible
    format or combining character (category Cf/Mn such as U+200B, U+2060,
    U+FEFF, U+00AD, U+200E, U+0301). The earlier rule allowed "punctuation
    other than ``._-``", and every lookalike above slipped through it,
    silently truncating ``4٫5`` to Opus 4. No punctuation is allowed: none of
    the committed page captures (tests/fixtures) has a rate-table model name
    followed by anything but end-of-cell or whitespace, so no legitimate
    form needs an exception. A private-use icon glyph (category Co, such as
    the live page's retired-model badge, U+E0F0) never reaches this check at
    all — :class:`TableCollector` already dropped it (round 2, R2-2) — so it
    can never itself force a refusal, and a genuine trailing whitespace
    boundary is never obscured by a Co glyph sitting after it.

    :param text: the row's name-cell text (rendered, private-use-stripped
        and collapsed to single spaces by :class:`TableCollector`).
    :param pos: the index right after the matched version.
    :returns: ``True`` when the version is complete, else ``False``.
    """
    return pos >= len(text) or text[pos].isspace()


def parse_models(html, skipped=None):
    """The model-pricing table -> ordered entries:
    {family, version, rates, condition: None|('through'|'starting', date)}.

    The table and its header are located by :func:`_locate_rate_table` (the
    current two-row layout or the older single-row one) and mapped by
    :func:`_map_rate_columns`; every data row, whichever layout matched,
    then goes through the same token, bound and sanitization checks below.

    A model row that does not occupy the header's columns cell for cell —
    its effective width (colspan summed) differs from the header's, or it
    holds a merged cell — is SKIPPED, never read (AOS-151 security
    correction, round 2, C3): its rates are never shifted into other
    columns, the rest of the page is still recorded, and the row is
    reported through ``skipped`` rather than dropped silently.

    Raises :class:`ValueError` for a structural parse failure (table/column
    not found, a cell with no dollar amount at all, no model rows) — main()
    maps this to :data:`EXIT_OTHER_ERROR`. Raises :class:`PricingRefused`
    instead (AOS-143 correction) for a page BOUND violation on an otherwise
    well-formed cell — a malformed numeric token (:func:`money`) or a rate
    outside :data:`MAX_RATE_USD`/:data:`MIN_INOUT_RATE_USD` — which main()
    maps to :data:`EXIT_BOUNDS_REFUSED` instead: the page read and parsed
    fine, it is just untrustworthy, a different failure mode from either a
    fetch failure or a structural one — and, since AOS-151, for a malformed
    model version (:func:`_version_boundary_ok`).

    :param html: the pricing page HTML.
    :param skipped: an optional list; each skipped model row is appended to
        it as ``(family, version, row_number, cells, width, header_width)``
        — ``row_number`` is 1-based among the table's data rows. No page
        text (the row's name cell) is carried into this tuple, so nothing
        page-controlled reaches the report the agent reads (AOS-151
        security correction, R2-1); the row is identified by its family,
        version and 1-based row index only. :func:`run_update` renders
        these as SKIPPED-ROW warnings.
    :returns: the parsed entries, in page order.
    """
    tc = TableCollector()
    tc.feed(html)
    header, body, needles = _locate_rate_table(tc.tables)
    col = _map_rate_columns(header, needles)
    # The columns are mapped by header CELL index, so a header holding a
    # merged cell would map every rate against the wrong grid column.
    if getattr(header, "width", len(header)) != len(header):
        raise ValueError("unexpected pricing table header (merged cell):"
                         f" {_safe_error_text(header)}")
    if skipped is None:
        skipped = []

    entries = []
    for row_number, row in enumerate(body, 1):
        # Match the model name FIRST (AOS-151 security correction, N4): a
        # row with no ``Claude <Family> <version>`` cell is not a model row
        # at all — e.g. a colspan-merged section heading such as the live
        # page's single-cell "Additional models" divider — and is passed
        # over without a warning, never reaching the width check below.
        m = _MODEL_NAME_RE.search(row[0])
        if not m:
            continue
        if not _version_boundary_ok(row[0], m.end(2)):
            raise PricingRefused(
                "Pricing refresh REFUSED — nothing written: malformed model"
                " version (the character right after the version is neither"
                " whitespace nor the end of the name: a glued tagline, an"
                " extra version component or digit run, a punctuation or"
                " lookalike-separator continuation, or an invisible format"
                f" character) in row: {_safe_error_text(row[0])}")
        # A model row must occupy the header's columns cell for cell
        # (AOS-151 security correction, N4; skip-and-warn since round 2,
        # C3): a wider row (an extra cell) used to shift every rate one
        # column silently, and a narrower one (a colspan merge, a "Contact
        # sales" row) was dropped with no warning. Such a row is now
        # SKIPPED — its rates are never read, so never shifted — and
        # reported as a SKIPPED-ROW warning; one odd row never blocks the
        # refresh of every other model on the page. ``width`` is the row's
        # EFFECTIVE width (colspan summed, :class:`_Row`), and a merged cell
        # (cells != width) is skipped too, since rates are read by cell
        # index. Ordering matters: the model name is matched and its version
        # checked first, so a non-model row (the live page's "Additional
        # models" divider) is not a skipped model row, and a malformed
        # version still refuses the whole run whatever the row's width.
        width = getattr(row, "width", len(row))
        if len(row) != len(header) or width != len(header):
            skipped.append((m.group(1).lower(), m.group(2), row_number,
                            len(row), width, len(header)))
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
        # Bound what a page can mint (AOS-143, corrected): reject a
        # non-finite rate (a very long digit string overflows float() to
        # `inf` with no exception), any rate over MAX_RATE_USD/MTok, and an
        # in_usd/out_usd under MIN_INOUT_RATE_USD (a $0, negative, or
        # near-zero charge would mint a permanent free/negative rate; cache
        # rates are legitimately allowed to be $0, so no lower bound applies
        # to them). A single bad cell refuses the WHOLE run as a page bound
        # violation (:class:`PricingRefused`), not a generic parse failure —
        # this is the one enforcement point every caller shares.
        bad = [k for k, v in rates.items()
               if not math.isfinite(v) or v > MAX_RATE_USD
               or (k in ("in_usd", "out_usd") and v < MIN_INOUT_RATE_USD)]
        if bad:
            raise PricingRefused(
                "Pricing refresh REFUSED — nothing written: rate out of"
                f" bounds (non-finite, over ${MAX_RATE_USD:g}/MTok, or an"
                f" in/out rate under ${MIN_INOUT_RATE_USD:g}/MTok) for"
                f" Claude {m.group(1).title()} {m.group(2)}:"
                f" {', '.join(sorted(bad))}")
        entries.append({"family": m.group(1).lower(), "version": m.group(2),
                        "rates": rates, "condition": condition})
    if not entries:
        raise ValueError(
            "no model rows parsed from the pricing table"
            + (f" ({len(skipped)} model row(s) skipped: width mismatch or"
               " merged cells)" if skipped else ""))
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


def row_key(entry):
    """The identity of one parsed page row for the backdate bound:
    ``(family, version, condition)``. Two rows of the same version differ by
    their condition (``None`` or ``('through'|'starting', date)``), so a
    refusal keyed on this names exactly one row, never a whole version.

    :param entry: one :func:`parse_models` entry.
    :returns: the hashable row key.
    """
    return (entry["family"], entry["version"], entry["condition"])


def build_candidates(entries, today, refused_rows=frozenset()):
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

    ``refused_rows`` (AOS-143 correction) names, by :func:`row_key`, every
    in-force ``starting`` row :func:`filter_backdated_starting` refused as
    dated more than :data:`BACKDATE_MAX_DAYS` days back. The refusal is PER
    ROW: IN-FORCE STATUS IS STILL DECIDED FROM THE UNFILTERED PAGE, exactly as
    before AOS-143, and then each candidate row is skipped only if it is
    itself a refused row — every other row of the same version (a newer
    arrived ``starting`` increase, an in-force ``through`` intro) mints as
    it would without the bound. A version with any in-force conditional
    (refused or not) stays in ``conditioned`` below, so its unconditional
    base rate stays suppressed. The family default takes the newest
    version's latest SURVIVING in-force row; if every in-force row of that
    version was refused, no family row is minted (its ``rows`` lookup below
    finds none) and the family default keeps its last recorded rate, like
    the version itself. Nothing that was suppressed before AOS-143 becomes
    mintable because of a refusal.

    Ordering: on a ``(prefix, effective_from)`` collision the first candidate
    wins. In-force conditionals are listed before unconditional rows (and an
    in-force conditional suppresses the version's unconditional rate anyway),
    so the in-force rate wins regardless of page order; among several
    unconditional rows for one version (e.g. a long-context row) the FIRST
    listed wins.

    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :param refused_rows: set of :func:`row_key` values — see above. Empty
        by default so callers that never refuse a row (most tests) are
        unaffected.
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
        if row_key(e) in refused_rows:
            continue  # skip only this row; its version stays `conditioned`
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


def filter_backdated_starting(entries, today):
    """Identify every in-force ``starting <d>`` ROW whose date ``d`` is too
    old to trust (AOS-143 parser bounds, corrected): an in-force ``starting``
    row is normally minted dated ``d`` however far back
    (:func:`build_candidates`), which would re-price every event of that
    prefix back to ``d`` — permanently, since pricing history is
    insert-only. Refused when ``d`` is more than :data:`BACKDATE_MAX_DAYS`
    days before ``today``. The bound applies to ``starting`` rows ONLY: a
    ``through`` row (an intro) is never refused, whatever its date, and an
    unconditional row is never refused. A future ``starting`` (``d > today``)
    is left alone — it is never minted anyway (:func:`in_force`).

    This is a PER-ROW refusal to mint, not a removal from the page and not a
    refusal of the version: the caller (:func:`run_update`) still passes every
    entry, unfiltered, to :func:`build_candidates`, which decides in-force
    status from the page exactly as before AOS-143 and only skips the rows
    named here — a newer arrived ``starting`` row or an in-force intro of the
    same version still mints.

    There is deliberately no other bound: a ``starting`` date equal to or
    earlier than a prefix's already-recorded rows is a NORMAL steady-state
    re-run — the increase was minted when it first arrived, and ``INSERT OR
    IGNORE`` (:func:`apply`) makes every later run at the same date a no-op.

    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :returns: ``(refused_rows, warnings)`` — ``refused_rows`` the set of
        :func:`row_key` values of the refused rows (:func:`build_candidates`
        takes this), and one ``(family, version, date, rates)`` tuple per
        refused row, in page order (:func:`run_update` drops the ones
        already recorded, :func:`render` words the rest).
    """
    cutoff = today - datetime.timedelta(days=BACKDATE_MAX_DAYS)
    refused, warnings = set(), []
    for e in entries:
        cond = e["condition"]
        if cond is None or cond[0] != "starting" or cond[1] > today:
            continue
        date = cond[1]
        if date < cutoff:
            refused.add(row_key(e))
            warnings.append((e["family"], e["version"], date, e["rates"]))
    return refused, warnings


def already_recorded(conn, family, version, date, rates):
    """Whether a refused ``starting`` row is already in the DB exactly as the
    page lists it: every specific prefix of ``(family, version)`` holds a row
    dated ``date`` with identical rates. Such a row was minted when the
    increase first arrived; refusing it now is a no-op, not a loss, so
    :func:`run_update` prints no BACKDATED-STARTING WARNING for it.

    :param conn: an open telemetry DB connection.
    :param family: the row's model family (lowercase).
    :param version: the row's version string.
    :param date: the row's ``starting`` date.
    :param rates: the row's parsed rate dict (:data:`RATE_KEYS`).
    :returns: ``True`` only when every prefix has an identical row.
    """
    for p in specific_prefixes(family, version):
        row = conn.execute(
            "SELECT in_usd, out_usd, cache_r_usd, cache_w_usd, cache_w_1h_usd"
            " FROM pricing WHERE provider=? AND model_prefix=?"
            " AND effective_from=?", (PROVIDER, p, _epoch(date))).fetchone()
        if row is None or dict(zip(RATE_KEYS, row)) != rates:
            return False
    return True


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


WARNING_CAP = 20   # per-category cap on STALE/BACKDATED/FUTURE/SKIPPED-ROW
                   # report lines


def _capped_warnings(items, line_fn, kind):
    """At most :data:`WARNING_CAP` rendered ``kind`` warning lines for
    ``items`` (:func:`stale_intros` / :func:`filter_backdated_starting` /
    :func:`future_only_newest` output, or :func:`parse_models`' skipped
    model rows), each its own blank-line-prefixed
    block via ``line_fn``, plus one "... and N more" summary line when
    ``items`` holds more than the cap (AOS-143 correction, F3 sev4-low): an
    adversarial or just very large page can otherwise print thousands of
    per-row warning lines into the report the command prints verbatim into
    the agent's context, uncapped and at linear cost per line.

    :param items: the warning tuples for one category, in page order.
    :param line_fn: renders one item to its single warning-line string.
    :param kind: the category's label (e.g. ``"BACKDATED-STARTING"``), used
        only in the summary line.
    :returns: report lines (each preceded by its own blank line), ready to
        extend the report's ``out`` list.
    """
    items = list(items)
    shown, extra = items[:WARNING_CAP], len(items) - WARNING_CAP
    out = []
    for item in shown:
        out += ["", line_fn(item)]
    if extra > 0:
        out += ["", f"… and {extra} more {kind} warning(s) (capped at"
                    f" {WARNING_CAP} per run)."]
    return out


def render(candidates, inserted, unpriced, today, stale=(), backdated=(),
           future_only=(), skipped_rows=()):
    """The finished markdown run report.

    :param candidates: planned+applied candidates (:func:`plan`,
        :func:`apply`).
    :param inserted: number of rows inserted.
    :param unpriced: model names matching no pricing prefix.
    :param today: the run date (UTC).
    :param stale: :func:`stale_intros` output — one STALE-PRICE WARNING line
        per version whose only listed rate is an expired intro.
    :param backdated: ``(family, version, date, rates)`` tuples — the
        :func:`filter_backdated_starting` warnings :func:`run_update` kept
        (refused rows not already recorded); one BACKDATED-STARTING WARNING
        line each.
    :param future_only: :func:`future_only_newest` output — one FUTURE-RATE
        WARNING line per family whose newest version's only rate has not
        started yet.
    :param skipped_rows: :func:`parse_models`' ``skipped`` tuples — one
        SKIPPED-ROW WARNING line per model row whose width did not match
        the header (AOS-151 security correction, round 2, C3). The row is
        identified only by its family, version and 1-based row index; no
        page text reaches this report (AOS-151 security correction, R2-1).
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
    def _stale_line(item):
        fam, ver, end = item
        return (f"STALE-PRICE WARNING: Claude {fam.capitalize()} {ver}"
                f" (`{specific_prefixes(fam, ver)[0]}`) — its introductory"
                f" rate ended {end.isoformat()} and the page lists no rate in"
                " force after it; nothing was minted, so events for it keep"
                " the last recorded rate until the page publishes a"
                " post-intro rate.")

    def _backdated_line(item):
        fam, ver, date, _rates = item
        name = f"Claude {fam.capitalize()} {ver}"
        return (f"BACKDATED-STARTING WARNING: {name}"
                f" (`{specific_prefixes(fam, ver)[0]}`) — a starting row dated"
                f" {date.isoformat()} is more than {BACKDATE_MAX_DAYS} days"
                " old and was not recorded; the rows already recorded for"
                f" {name} are unchanged.")

    def _skipped_line(item):
        fam, ver, row_number, cells, width, header_width = item
        return (f"SKIPPED-ROW WARNING: Claude {fam.capitalize()} {ver}"
                f" (skipped rate-table row {row_number}: its cell count/width"
                f" does not match the header) — the row has {cells} cell(s)"
                f" spanning {width} column(s) but the header has"
                f" {header_width}; its rates were NOT read (never shifted"
                " into other columns) and nothing was minted for it. Every"
                " other listed model was processed as usual.")

    def _future_line(item):
        fam, ver, date = item
        return (f"FUTURE-RATE WARNING: Claude {fam.capitalize()} {ver}"
                f" (`{specific_prefixes(fam, ver)[0]}`) — its only listed"
                f" rate starts {date.isoformat()}, still in the future; the"
                " family default keeps its last recorded rate until then.")

    out += _capped_warnings(stale, _stale_line, "STALE-PRICE")
    out += _capped_warnings(backdated, _backdated_line, "BACKDATED-STARTING")
    out += _capped_warnings(future_only, _future_line, "FUTURE-RATE")
    out += _capped_warnings(skipped_rows, _skipped_line, "SKIPPED-ROW")
    out += ["", f"Source: {URL} — checked {today.isoformat()},"
            f" {inserted} row(s) inserted (history is insert-only; existing"
            " rows are never modified)."]
    return "\n".join(out)


class PricingRefused(Exception):
    """A run refused before any write; ``args[0]`` is the report/message.
    Raised for every page-BOUND violation (AOS-143, corrected): a malformed
    or out-of-range rate (:func:`money`, :func:`parse_models`) as well as the
    over-:data:`MAX_CANDIDATES` row-count cap below — main() maps all of
    these to :data:`EXIT_BOUNDS_REFUSED`, distinct from a fetch failure or a
    structural parse failure (:class:`ValueError`)."""


def run_update(conn, entries, today, source=URL, skipped_rows=()):
    """Plan, apply and report one pricing refresh of parsed page ``entries``.

    Two bounds (AOS-143) can refuse work before any write reaches the DB: an
    in-force ``starting`` row too old to trust has its INSERT skipped, named
    by :func:`filter_backdated_starting` (a per-ROW refusal, warned not
    fatal — every other row, including a newer increase of the same version,
    proceeds, and :func:`build_candidates` still decides in-force status from
    the unfiltered page, so a refusal never makes a suppressed rate
    mintable; a refused row already recorded with identical rates
    (:func:`already_recorded`) is a no-op and is not warned); a candidate
    count over
    :data:`MAX_CANDIDATES` refuses the WHOLE run atomically
    (:class:`PricingRefused`, nothing planned or applied).

    :param conn: an open telemetry DB connection (:func:`capture.connect`).
    :param entries: :func:`parse_models` output, in page order.
    :param today: the run date (UTC).
    :param source: the ``source`` column value for inserted rows.
    :param skipped_rows: the model rows :func:`parse_models` skipped for a
        width mismatch (its ``skipped`` list), reported as SKIPPED-ROW
        warnings.
    :returns: the finished markdown run report (:func:`render`), including a
        STALE-PRICE / BACKDATED-STARTING / FUTURE-RATE / SKIPPED-ROW WARNING
        line per :func:`stale_intros` / :func:`filter_backdated_starting` /
        :func:`future_only_newest` / ``skipped_rows`` item.
    :raises PricingRefused: the candidate count exceeds
        :data:`MAX_CANDIDATES`; nothing was planned or applied.
    """
    refused_rows, backdated = filter_backdated_starting(entries, today)
    backdated = [w for w in backdated if not already_recorded(conn, *w)]
    raw_candidates = build_candidates(entries, today, refused_rows)
    if len(raw_candidates) > MAX_CANDIDATES:
        raise PricingRefused(
            "Pricing refresh REFUSED — nothing written: this run would mint"
            f" {len(raw_candidates)} candidate row(s), over the"
            f" {MAX_CANDIDATES}-row cap per run.")
    candidates = plan(conn, raw_candidates)
    inserted = apply(conn, candidates, source)
    return render(candidates, inserted, unpriced_models(conn), today,
                  stale_intros(entries, today), backdated,
                  future_only_newest(entries, today), skipped_rows)


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


# ------------------------------------------------- dashboard plan summary
#
# The dashboard's own-price banner shows a one-line "backfill available" note
# from a cached summary of the last plan (AOS-149, scripts/backfill_summary.py)
# instead of computing a plan on page load. The cache is best-effort: it never
# changes this script's stdout or exit code, and any failure only leaves the
# dashboard without the line.

def _summary_fingerprint(conn):
    """:func:`backfill_summary.fingerprint` of ``conn``, or ``None`` on any
    error (the summary is then simply not written)."""
    try:
        import backfill_summary
        return backfill_summary.fingerprint(conn)
    except Exception:   # noqa: BLE001 - best-effort cache, never fatal
        return None


def _cache_plan_summary(db, plan_, fp_before, fp_after, computed_at=None):
    """Write the dashboard's plan summary for a ``--backfill-plan`` run.

    Written only when the plan ran against the dashboard's own DB
    (``capture.db_path()``) and the DB state did not change while the plan
    was computing (``fp_before == fp_after``, both non-``None``) — a summary
    is never keyed to a fingerprint the plan did not actually see. A write
    failure prints one note to stderr and is otherwise ignored.

    :param db: the DB path the plan ran against.
    :param plan_: :func:`backfill_plan` output.
    :param fp_before: fingerprint taken just before the plan.
    :param fp_after: fingerprint taken just after the plan.
    :param computed_at: epoch seconds to stamp (default: now).
    :returns: ``True`` when the summary was written.
    """
    try:
        import backfill_summary
        if fp_before is None or fp_before != fp_after:
            return False
        if not backfill_summary.is_dashboard_db(db):
            return False
        comb = plan_.get("combined")
        delta = (_usd_change(comb["cost_now"], comb["cost_after"])[2]
                 if comb is not None else None)
        at = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp()
                 if computed_at is None else computed_at)
        backfill_summary.write(backfill_summary.summarize(plan_, delta),
                               fp_before, at)
        return True
    except Exception as exc:   # noqa: BLE001 - best-effort cache
        print("note: the dashboard's backfill summary was not updated"
              f" ({type(exc).__name__}).", file=sys.stderr)
        return False


def _clear_plan_summary(db):
    """Remove the dashboard's plan summary after a successful
    ``--backfill-apply`` on the dashboard's own DB (the applied rows change
    the pricing table, so the old summary no longer describes it). Never
    raises.

    :param db: the DB path the apply ran against.
    :returns: ``True`` when a summary file was removed.
    """
    try:
        import backfill_summary
        if not backfill_summary.is_dashboard_db(db):
            return False
        return backfill_summary.clear()
    except Exception:   # noqa: BLE001 - best-effort cache
        return False


def _fetch_page(url, timeout_s=FETCH_TIMEOUT_S, max_bytes=FETCH_MAX_BYTES):
    """Fetch ``url`` under a hard wall-clock deadline covering connect AND
    the full body read together, with a hard cap on the response body size
    (AOS-143 correction, round 2 — hang/DoS fix).

    ``urllib.request.urlopen(..., timeout=N)`` only bounds each individual
    blocking socket operation, not the fetch as a whole: a server that keeps
    the connection open and trickles a byte (or a few) through just before
    each such operation would time out never trips it, and the fetch could
    hang indefinitely. To close that gap, the fetch (connect plus the whole
    read) runs in a daemon thread that this function joins with a real
    wall-clock timeout; a thread still running once that timeout elapses is
    abandoned — it is a daemon thread holding no resource the rest of the
    process needs, and the process is about to exit anyway on the fetch
    failure this raises — rather than waited on further. ``resp.read()``
    likewise has no size limit of its own, so the body is read in bounded
    chunks and the fetch aborts the moment the running total exceeds
    ``max_bytes``, instead of buffering an unbounded or oversized body in
    memory first.

    :param url: the page URL to fetch.
    :param timeout_s: total seconds allowed for connect + the full read,
        together (default :data:`FETCH_TIMEOUT_S`).
    :param max_bytes: maximum total response body size in bytes (default
        :data:`FETCH_MAX_BYTES`).
    :raises Exception: on any fetch failure, deadline overrun, or oversized
        body — :func:`main` treats every exception from this function the
        same way, as :data:`EXIT_FETCH_FAILED`.
    :returns: the decoded page text.
    """
    outcome = {}

    def worker():
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "token-telemetry-pricing-update"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                chunks, total = [], 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        outcome["exc"] = Exception(
                            f"response body exceeded {max_bytes} bytes")
                        return
                    chunks.append(chunk)
            outcome["html"] = b"".join(chunks).decode(
                "utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - reported to the caller,
                                   # which maps every fetch exception alike
            outcome["exc"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise Exception(f"fetch exceeded {timeout_s:g}s total timeout")
    if "exc" in outcome:
        raise outcome["exc"]
    return outcome["html"]


class _HtmlFileInvalid(Exception):
    """Raised by :func:`_read_html_file` when the ``--html`` path fails the
    pre-read bounds check — not a regular file, or larger than
    :data:`FETCH_MAX_BYTES` (AOS-143 round 4, F2). Kept distinct from a
    plain unreadable-path error (missing file, permission denied), which
    :func:`main` still maps to :data:`EXIT_FETCH_FAILED` like any other
    fetch failure: this exception instead maps to :data:`EXIT_OTHER_ERROR`,
    since the file IS there but is not a trustworthy pricing-page source."""


def _read_html_file(path, max_bytes=FETCH_MAX_BYTES):
    """Read a local ``--html`` file under the same bound the network fetch
    enforces (AOS-143 round 4, F2). This path is reached only through the
    command's manual fallback, which is deliberately NOT pre-approved (a
    Claude Code permission prompt gates it) — but once approved, a hostile
    or mistaken path (a FIFO, a device node, a directory, an oversized file)
    must not hang the process or exhaust memory the way an unbounded
    ``read_text()`` could.

    The file type is checked with :func:`os.stat` and :data:`stat.S_ISREG`
    BEFORE the file is ever opened, so a FIFO blocks on neither the stat nor
    a subsequent read attempt. At most ``max_bytes + 1`` bytes are then
    read, which is enough to detect an over-cap file without buffering an
    unbounded one.

    :param path: the ``--html`` argument.
    :param max_bytes: the same cap the network fetch enforces
        (default :data:`FETCH_MAX_BYTES`).
    :raises OSError: the path does not exist or cannot be stat'd — mapped by
        :func:`main` to :data:`EXIT_FETCH_FAILED`, same as any other fetch
        failure.
    :raises _HtmlFileInvalid: the path is not a regular file, or its
        content exceeds ``max_bytes``.
    :returns: the file's decoded text.
    """
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise _HtmlFileInvalid(
            f"--html path is not a regular file: {_safe_error_text(path)}")
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise _HtmlFileInvalid(f"--html file exceeded {max_bytes} bytes")
    return data.decode("utf-8", errors="replace")


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


def _argv_has_flag(argv, flag):
    """True when ``flag`` appears in ``argv`` either as its own token
    (``--foo``) or as the ``--foo=value`` combined form (AOS-143 round 4,
    N2) — the pre-argparse guards below used exact-token membership only,
    so a token spelled ``--backfill-apply=X`` (valid argparse syntax, and
    argparse itself accepts it — see ``_reject_option_shaped_backfill_apply``)
    was invisible to them even though the equivalent space-separated form
    was caught.

    :param argv: the raw argument list, before ``argparse.parse_args``.
    :param flag: the long-option spelling to look for, e.g. ``"--html"``.
    :returns: ``True`` if ``flag`` or ``flag + "="...`` is present.
    """
    return any(tok == flag or tok.startswith(flag + "=") for tok in argv)


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
    :returns: ``True`` when both flags appear anywhere in ``argv`` (either
        as their own token or as the ``--flag=value`` combined form — AOS-143
        round 4, N2, :func:`_argv_has_flag`), else ``False``.
    """
    return (_argv_has_flag(argv, "--backfill-plan")
            and _argv_has_flag(argv, "--backfill-apply"))


def _reject_html_with_backfill(argv):
    """Defence in depth, same style and exit code as
    :func:`_reject_combined_backfill_flags`: refuse ``--html`` (or
    ``--html=...``) together with either ``--backfill-plan`` or
    ``--backfill-apply``, BEFORE argparse ever runs and before any DB is
    opened or file read (AOS-143, round 3, security review). Backfill modes
    never fetch or parse a page, so ``--html`` has no meaning with either —
    and, critically, the command prompt pre-approves any ``Bash(... --html
    <file> ...)`` invocation by prefix match on the leading tokens alone,
    with no regard for what backfill flag follows later on the same command
    line. Checking membership in ``argv`` (rather than relying on argument
    order or on argparse) catches every ordering: ``--html`` before or after
    ``--db``, before or after the backfill flag, and both the space-separated
    and ``--html=FILE`` forms. ``--html`` is also declared mutually exclusive
    with both backfill flags in the argparse parser itself, as a third,
    independent layer.

    :param argv: the raw argument list, before ``argparse.parse_args``.
    :returns: ``True`` when ``--html``/``--html=...`` appears anywhere in
        ``argv`` together with ``--backfill-plan``/``--backfill-plan=...``
        or ``--backfill-apply``/``--backfill-apply=...`` (AOS-143 round 4,
        N2, :func:`_argv_has_flag`), else ``False``.
    """
    has_html = _argv_has_flag(argv, "--html")
    has_backfill = (_argv_has_flag(argv, "--backfill-plan")
                     or _argv_has_flag(argv, "--backfill-apply"))
    return has_html and has_backfill


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

    The combined ``--backfill-apply=VALUE`` form (AOS-143 round 4, N2) is
    also recognized: it is valid argparse syntax — argparse accepts exactly
    one value that way — and this guard used to look only for the exact
    token ``--backfill-apply``, so ``--backfill-apply=claude-opus-5-5
    --db=other.db`` was invisible to it even though the equivalent
    space-separated form (``--backfill-apply claude-opus-5-5 --db
    other.db``) was already caught, since ``--db`` itself is not a
    well-formed pricing prefix.

    :param argv: the raw argument list, before ``argparse.parse_args``.
    :returns: the 1-based positions (within the arguments following
        ``--backfill-apply``) of every offending token, or ``None`` when
        ``--backfill-apply`` is absent or every following token is a
        well-formed prefix.
    """
    start = None
    for i, tok in enumerate(argv):
        if tok == "--backfill-apply" or tok.startswith("--backfill-apply="):
            start = i + 1
            break
    if start is None:
        return None
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
    # Third: --html has no meaning with either backfill mode (neither fetches
    # or parses a page), and — the point of this guard (AOS-143, round 3) —
    # the command prompt's `Bash(... --html:*)` pre-approval matches on the
    # command's leading tokens alone, with no regard for a backfill flag
    # appearing later on the same command line. Reject the combination by
    # plain membership in argv, before argparse ever runs and before the DB
    # or the HTML file is opened, so no ordering of --html relative to --db
    # or the backfill flag can let a pre-approved --html invocation apply or
    # plan.
    if _reject_html_with_backfill(raw_argv):
        print("Backfill REFUSED — nothing written: --html is rejected"
              " together with --backfill-plan or --backfill-apply"
              " (backfill modes never fetch or parse a page).")
        return 2

    ap = _ArgumentParser(prog="pricing_update.py", allow_abbrev=False)
    ap.add_argument("--db", default=None)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--html", default=None,
                       help="parse a local HTML file instead of fetching"
                            " (tests); mutually exclusive with"
                            " --backfill-plan/--backfill-apply")
    group.add_argument("--backfill-plan", action="store_true",
                       help="print the backfill plan and exit (DB read-only;"
                            " caches a plan-summary sidecar for the"
                            " dashboard)")
    ap.add_argument("--json", action="store_true",
                    help="with --backfill-plan: machine-readable JSON")
    group.add_argument("--backfill-apply", nargs="+", metavar="PREFIX",
                       help="insert the backfill row for each confirmed"
                            " prefix (all-or-nothing; re-plans first;"
                            " --db must be given BEFORE this flag;"
                            " mutually exclusive with --backfill-plan/--html)")
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
            fp_before = _summary_fingerprint(conn)
            p = backfill_plan(conn, today)
            fp_after = _summary_fingerprint(conn)
        finally:
            conn.close()
        if args.json:
            print(json.dumps(_json_plan(p), indent=2, sort_keys=True))
        else:
            print(render_backfill_plan(p))
        _cache_plan_summary(db, p, fp_before, fp_after)
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
        _clear_plan_summary(db)
        return 0
    # Fetching (network, or --html file for tests) is kept in its own try
    # block, distinct from parsing below (AOS-143 correction, F1): a fetch
    # failure is the ONLY case that reaches the command's manual fallback,
    # and only on an interactive run — never on an unattended/scheduled one
    # (`commands/schedule-pricing.md`) — a page that fetched fine but did
    # not parse/validate must never be handed to the fallback. The fallback
    # itself no longer parses or inserts anything by hand either (AOS-143
    # correction, round 2, F1b): it re-fetches the page and re-runs this
    # same script with `--html`, so every bound below always applies through
    # this one implementation. The network fetch (`_fetch_page`) itself
    # enforces its own wall-clock deadline and body-size cap (AOS-143
    # correction, round 2 — hang/DoS fix) rather than relying solely on
    # urlopen's per-socket-operation `timeout=`. F2/F3 (sev3 pre-existing):
    # the exception text can carry an attacker- or MITM-controlled raw HTTP
    # status line, so it is sanitized with the same sanitizer used for
    # page-derived error text before it ever reaches stderr.
    try:
        if args.html:
            html = _read_html_file(args.html, FETCH_MAX_BYTES)
        else:
            html = _fetch_page(URL, FETCH_TIMEOUT_S, FETCH_MAX_BYTES)
    except _HtmlFileInvalid as exc:
        # Not a plain "could not be read" failure (that stays a fetch
        # failure below) — the file IS there but fails the bounds check
        # (AOS-143 round 4, F2), so nothing was read/written and this is
        # never eligible for the command's fallback.
        print(f"pricing page read failed: {_safe_error_text(exc)}",
              file=sys.stderr)
        return EXIT_OTHER_ERROR
    except Exception as exc:  # noqa: BLE001 - any fetch failure -> fallback
        print(f"pricing page fetch failed: {_safe_error_text(exc)}",
              file=sys.stderr)
        return EXIT_FETCH_FAILED
    # Parsing/validating the fetched page is a SEPARATE failure mode (AOS-143
    # correction, F1): PricingRefused means the page read and parsed fine but
    # violated a bound (a malformed/out-of-range rate) — report and STOP, no
    # fallback, since the manual fallback has none of these bounds. Any other
    # parse failure (layout changed, table/column not found) is a THIRD,
    # distinct outcome — also no fallback, since a page that read fine but
    # did not parse as expected is exactly the kind of anomaly the
    # fallback's unbounded manual read must not be trusted with either.
    skipped_rows = []
    try:
        entries = parse_models(html, skipped_rows)
    except PricingRefused as exc:
        print(exc.args[0])
        return EXIT_BOUNDS_REFUSED
    except Exception as exc:  # noqa: BLE001 - any other parse failure
        print(f"pricing page parse failed: {_safe_error_text(exc)}",
              file=sys.stderr)
        return EXIT_OTHER_ERROR
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    # Opening the DB is its own failure mode too (AOS-143 correction, round
    # 2, INFO): an unwritable/unopenable DB (e.g. a permissions problem)
    # must map to the documented EXIT_OTHER_ERROR, not propagate as an
    # uncaught traceback that exits 1 like a bound refusal — a DB problem is
    # neither a fetch failure nor a page-bound refusal.
    try:
        conn = capture.connect(db)
    except Exception as exc:  # noqa: BLE001 - DB open failure
        print(f"pricing DB error: {_safe_error_text(exc)}", file=sys.stderr)
        return EXIT_OTHER_ERROR
    try:
        print(run_update(conn, entries, today, skipped_rows=skipped_rows))
    except PricingRefused as exc:
        print(exc.args[0])
        return EXIT_BOUNDS_REFUSED
    except Exception as exc:  # noqa: BLE001 - any other unexpected error
                               # updating the DB (also EXIT_OTHER_ERROR)
        print(f"pricing update failed: {_safe_error_text(exc)}",
              file=sys.stderr)
        return EXIT_OTHER_ERROR
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
