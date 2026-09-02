#!/usr/bin/env bash
# check-no-local-paths.sh — fail if any tracked file embeds a contributor's local home path
# (/Users/<name> or /home/<name>). The intended documentation placeholders /Users/me and
# /home/user are allowed. Keeps a local username out of the published repo. Mirrors the
# no-local-paths gate in the agent-operating-kit's validate-kit.sh.
set -u
cd "$(dirname "$0")/.." || exit 2
lp=$(git grep -hoIE '/(Users|home)/[A-Za-z0-9._-]+' -- . ':(exclude)scripts/check-no-local-paths.sh' 2>/dev/null \
  | grep -vxE '/Users/me|/home/user' | sort -u)
if [ -z "$lp" ]; then
  echo "ok: no local home paths in tracked files"
  exit 0
else
  echo "FAIL: local home path(s) in tracked files (use ~ or the /Users/me placeholder):"
  printf '%s\n' "$lp" | sed 's/^/  /'
  exit 1
fi
