#!/usr/bin/env bash
# Mirror each agent's CLAUDE.md -> AGENTS.md so every coding assistant
# (Claude Code reads CLAUDE.md; Codex/Cursor/Gemini read AGENTS.md) sees the
# same per-agent context. CLAUDE.md is the SINGLE SOURCE OF TRUTH — edit it,
# then run this to refresh the AGENTS.md mirrors.
#
# Agent dirs are discovered dynamically: any top-level directory that contains a
# CLAUDE.md and is NOT git-ignored (so agent-dashboard/, Taskforce_AI_Website/,
# SinhalaVITS-TTS-M2/ are skipped). New agents are picked up automatically.
#
# Why copies and not symlinks: Git Bash / Windows checkouts don't preserve
# symlinks, so a symlinked AGENTS.md breaks for teammates on Windows.
#
# Usage:  bash ops/sync-agent-docs.sh        (run from anywhere)
set -euo pipefail
cd "$(dirname "$0")/.."

changed=0
while IFS= read -r -d '' claude; do
  d="$(dirname "$claude")"
  # Skip anything git ignores (nested repos / vendored dirs with their own docs).
  if git check-ignore -q "$d" 2>/dev/null; then
    continue
  fi
  if ! cmp -s "$d/CLAUDE.md" "$d/AGENTS.md" 2>/dev/null; then
    cp "$d/CLAUDE.md" "$d/AGENTS.md"
    echo "synced  $d/AGENTS.md"
    changed=1
  fi
done < <(find . -mindepth 2 -maxdepth 2 -name CLAUDE.md -print0)

[ "$changed" -eq 0 ] && echo "all AGENTS.md already in sync"
exit 0
