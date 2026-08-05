# Handbooks — developer / user / admin wikis

Three living, Obsidian-compatible wikis under `.docs/handbooks/<audience>/` (`developer`, `user`, `admin`) that document the product as it currently is — not the changelog of *what changed* (that's `documentation-agent.md`'s job), but the current shape of *what exists*. Every consumer of this file — the documentation agent, milestone validation — reads it instead of re-deriving these rules.

## Layout

One page per LOGICAL unit — a business logic, feature, or module grouping the ORCHESTRATOR sets at planning time. NEVER one page per code file: hundreds of files must collapse into a handful of units. A unit that outgrows a single page becomes a subfolder — `<unit>/<unit>.md` is its main page (overview + the unit's FULL `sources` list) with topic sub-files beside it (each carrying its own narrower `sources`), all wiki-linked from the main page. Plain markdown, per-folder `INDEX.md` as the table of contents. Every page (a subfolder registers its main page) MUST be in its folder's INDEX with a one-line description — **a page not in INDEX doesn't exist**, even if the file sits on disk.

Scope: document only what the PROJECT introduces. Framework defaults and stock conventions (e.g., a Laravel app's standard structure or routing conventions) are never referenced or explained — only the functions, logic, and nuances this project added on top.

## Page format

YAML frontmatter:
- `title` — page title
- `audience` — `developer` | `user` | `admin`
- `module` — the module/feature this page covers
- `sources` — list of repo code paths this page documents (the discovery key)
- `updated` — `YYYY-MM-DD`
- `related` — list of quoted `"[[Page Name]]"` links (quoting keeps the YAML valid for Obsidian's property editor)

Body: Obsidian wikilinks (`[[Page Name]]`) between pages; kebab-case filenames; relative-only references (no absolute paths, no external URLs standing in for internal links).

## Audience voice

- **Developer**: software logic — purpose, the WHY behind nuanced behavior, flows/steps/rules that are NOT common sense, and how this module connects to others (link them). Never restate what clean code already says.
- **User / Admin**: plain language — what it does and what to be aware of. Structure by what a LAYMAN would search for: main business capability, menu item, or screen/function (combine when natural) — not by code layout. Admin covers operation/configuration; user covers usage. No agent/tier internals, no code.

## Discovery rule (MANDATORY, before writing)

1. Read all three INDEX.md files.
2. Grep the handbooks for every touched path: `grep -rl "<path>" .docs/handbooks/`.
3. Matches → AMEND, never create a near-duplicate. Multiple hits are NORMAL: exclude `INDEX.md` rows, then treat every remaining page as an amend candidate — a changed source may need its developer page AND its user/admin pages updated, each in its own voice.
4. No match → check whether an existing logical unit should absorb the topic (extend its page or add a sub-file) before creating a new unit; the orchestrator owns the unit map.
5. On every create or rename: update `sources`, `updated`, `related`, and the folder's INDEX row. On rename: fix every wikilink that pointed at the old title.

## When triggered

- **Per task**: the documentation agent checks/amends/creates pages — logic changed → developer page; user-visible behavior changed → user page; operational/config surface changed → admin page; nothing relevant changed → say so explicitly, don't force a page.
- **Per milestone**: validation includes a coverage pass — each shipped epic's functionality has current pages in all three audiences.

## Secrets

Handbooks are shareable surfaces (`security.md` applies): no secrets, tokens, or internal-only URLs in any page.
