#!/usr/bin/env bash
# Mirror each agent's CLAUDE.md -> AGENTS.md so every coding assistant
# (Claude Code reads CLAUDE.md; Codex/Cursor/Gemini read AGENTS.md) sees the
# same per-agent context. CLAUDE.md is the SINGLE SOURCE OF TRUTH — edit it,
# then run this to refresh the AGENTS.md mirrors.
#
# Why copies and not symlinks: Git Bash / Windows checkouts don't preserve
# symlinks, so a symlinked AGENTS.md breaks for teammates on Windows.
#
# Usage:  bash ops/sync-agent-docs.sh        (run from anywhere)
set -euo pipefail
cd "$(dirname "$0")/.."

AGENTS=("BSL Agent" "Flico Agent" "HattonHills" "SLIC Agent" "Sofia Agent" "Kavya")
changed=0
for d in "${AGENTS[@]}"; do
  if [ -f "$d/CLAUDE.md" ]; then
    if ! cmp -s "$d/CLAUDE.md" "$d/AGENTS.md" 2>/dev/null; then
      cp "$d/CLAUDE.md" "$d/AGENTS.md"
      echo "synced  $d/AGENTS.md"
      changed=1
    fi
  fi
done
[ "$changed" -eq 0 ] && echo "all AGENTS.md already in sync"
exit 0
