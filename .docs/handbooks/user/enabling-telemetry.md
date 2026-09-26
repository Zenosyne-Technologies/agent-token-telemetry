---
doc: Enabling Telemetry
type: handbook
status: active
summary: Turning telemetry capture on and off for a project with `/token-telemetry:enable` and `/token-telemetry:disable` — one repository is one project, a Claude Code worktree's sessions record under its main project's name, and both commands act on the whole repository at once.
keywords: [enable, disable, telemetry, worktree, project, capture, opt-in, main-repository]
level: project
audience: user
module: capture
sources: [commands/enable.md, commands/disable.md]
related: ["[[enabling-remote-telemetry]]", "[[reading-token-stats]]"]
created: 2026-09-26
updated: 2026-09-26
---

# Enabling Telemetry

Run `/token-telemetry:enable` to start recording token usage for a project, and
`/token-telemetry:disable` to stop. Both commands work on a whole **repository**,
not just the folder you happen to be sitting in — see below.

## Worktrees record under the main project

If you use Claude Code's worktrees — separate working folders for the same
repository, normally under `<repo>/.claude/worktrees/<name>` — a session
you run inside one of them is recorded under the repository's **main
project**, using that project's own name. It is not a separate, nameless
entry for the worktree folder; your usage stays together with the rest of
the repository's history.

## Opting the main repository in now covers its worktrees

This is a change in behavior: previously, a worktree with no opt-in marker of
its own was not captured at all, even when the main repository was enabled.
Now, enabling the main repository also covers every one of its worktrees —
you do not need to run `/token-telemetry:enable` again inside each one.

## `/enable` and `/disable` act on the whole repository

Both commands resolve to the repository's main checkout first, wherever you
run them from, and then apply there:

- `/token-telemetry:enable` turns capture on for the main checkout **and all
  of its worktrees**, and tells you so.
- `/token-telemetry:disable` turns capture off for the main checkout **and
  all of its worktrees**, and tells you so.

Running either command from inside a worktree has the same repository-wide
effect as running it from the main checkout.

See [[enabling-remote-telemetry]] for the separate, machine-level choice of
*where* telemetry is stored (local vs. a shared remote database) — it does
not change any of the per-repository behavior above.
