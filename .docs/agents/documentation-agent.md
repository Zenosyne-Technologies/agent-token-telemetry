# Documentation agent (after a tracker task is done)

When a tracker task reaches Done (built + validated), dispatch a small-worker agent to document it. Brief it with the issue id, commit hashes, and the touched paths.

Scope (only what the change affects — skip untouched docs):
1. **Project docs** (`docs/`): update the relevant architecture note(s) with the new behavior/contract; roadmap result line if milestone-relevant; issue-log rows for bugs fixed en route.
2. **Code-level docs**: README/usage snippets where a public contract changed; doc comments only for non-obvious constraints (match surrounding density — no narration).
3. **Tracker**: closing comment on the issue — what shipped, commits, where the docs live.
4. **Config surface**: env examples + compose/deploy env blocks for any new variable — env-wiring is part of the feature, and it is the class of gap validators structurally miss.
5. **Project info** (`.docs/PROJECT-INFO.md`): update any meta fact the change altered — stack, dev command/ports, tracker coordinates, label-syntax version.

Keep diffs surgical; follow existing doc structure and tone. Attribution policy per core rules.
