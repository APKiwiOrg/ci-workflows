#!/usr/bin/env bash
#
# Emits the SessionStart additionalContext payload (Claude Code and Codex share it).
#
# Injects two things every session needs before it decides what its task is:
#   1. concurrent work (worktrees + current branch), so a new chat sees what else is in flight
#   2. the discovered-work ledger: the open backlog count, straight from scripts/ledger.sh
#
# (2) is deliberately informational and never blocks. There is no signal for "you noticed
# something and did not write it down", so any hook aiming at that fires on non-violations and
# trains people to write junk entries to silence it. Putting the ledger in front of the session
# instead has no false positives and nothing to bypass.
#
# The backlog lives in GitHub Issues, so unlike the old docs/TODO.md grep this needs the network
# and a token. That is a real failure surface, and the ONE rule is that it must fail loudly:
# ledger.sh never reports a count it did not read, and says UNKNOWN or STALE instead of "0".
# A confident "0 open" while auth is dead would read as "the sweep is clean" at the exact moment
# the tool has no idea what is open. See scripts/ledger.sh for the full reasoning.
#
# Prints a single JSON object on stdout. Safe to run anywhere: outside a git repo, with no gh,
# no token, or no jq, it degrades rather than failing the session start.

set -uo pipefail

worktrees=$(git worktree list 2>/dev/null)
branch=$(git status -sb 2>/dev/null | head -1)

# Refresh the mirror only if it is old, and cap the wait: session start is a hot path and the
# network may be gone. A failed or skipped refresh is fine; status below reports the mirror's
# real age either way. stderr is dropped because this hook's stdout must stay pure JSON, and the
# same diagnostic is one `bash scripts/ledger.sh sync` away.
if [ -f scripts/ledger.sh ]; then
  CIW_LEDGER_TIMEOUT=${CIW_LEDGER_TIMEOUT:-8} bash scripts/ledger.sh sync --if-stale >/dev/null 2>&1
  ledger=$(bash scripts/ledger.sh status 2>/dev/null)
  [ -z "$ledger" ] && ledger="BACKLOG: UNKNOWN (scripts/ledger.sh status produced nothing)."
else
  ledger="(no scripts/ledger.sh in this repo)"
fi

ctx=$(printf 'git worktrees:\n%s\n\nbranch:\n%s\n\ndiscovered-work ledger (GitHub Issues):\n%s' \
  "$worktrees" "$branch" "$ledger")

if command -v jq >/dev/null 2>&1; then
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}' \
    "$(printf '%s' "$ctx" | jq -Rs .)"
else
  # jq absent: emit nothing rather than malformed JSON. A broken payload is worse than none.
  exit 0
fi
