#!/usr/bin/env bash
# Adopted from the game-repo fleet (game-template/scripts/sync-labels.sh). This copy is ci-workflows-local:
# there is no template propagation and no pre-commit template-sync gate in this repo, so edit it here.
#
# ci-workflows' issue label vocabulary, as code. Idempotent: run it any time.
#
# The backlog lives in GitHub Issues. These labels are the vocabulary for this repo's issue
# backlog, kept as code so they don't drift from what the same labels mean in the user's other
# repos. Hand-clicked labels drift within a week. This file is the source of truth instead.
#
#   kind/*        backlog vs roadmap. The rule: if it needs a spec, it is a roadmap item.
#                 Otherwise it is a TODO.
#   confidence/*  how much to trust a backlog item is real, as written. Required on kind/backlog
#                 by convention, not by machinery: ci-workflows has no issue-label workflow, so unlike the
#                 game repos there is no workflow here to reject an unlabelled issue. This script
#                 creates and syncs the label vocabulary, nothing more. Applying the right label
#                 when filing is on the agent, which is what AGENTS.md already says. The
#                 distinction a flat list destroys: a checked finding and an unverified guess
#                 look identical once they are both just an issue.
#   needs/*       this item is waiting on something. needs/upstream replaces the old
#                 **Handed off:** ledger line, which needed a reciprocity guard to stay honest.
#                 A cross-repo issue reference backlinks both sides by itself.
#   priority/*    how urgent, as an explicit tier. Replaces the old "priority is the board's
#                 order" convention, which the Projects v2 API never actually exposed.
#                 critical > high > medium > low. medium is the default.
#
# Usage: scripts/sync-labels.sh [--repo OWNER/NAME] [--dry-run]

set -uo pipefail

repo=""
dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '4,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

command -v gh >/dev/null 2>&1 || { echo "sync-labels: gh is required" >&2; exit 1; }
if [ -z "$repo" ]; then
  repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null) \
    || { echo "sync-labels: could not resolve the repo; pass --repo OWNER/NAME" >&2; exit 1; }
fi

# name|colour|description
labels='
kind/backlog|0e8a16|Noticed and not done. A gap, a follow-up, a polish item. No spec needed.
kind/roadmap|1d76db|A program: earns its own design spec and its own release.
confidence/verified|0e8a16|Checked against the code. Act on it as written.
confidence/lead|fbca04|Surfaced but not checked. May be wrong. Confirm against the code first.
confidence/authored|c5def5|Written deliberately by someone with the context. Act on it.
confidence/refuted|b60205|Investigated and found not to be real. Closed, and kept so it is not re-raised.
needs/confidence|d93f0b|Backlog item with no confidence rating. Add one, by hand: nothing here applies it.
needs/upstream|5319e7|Waiting on another repo. Cross-reference the upstream issue so both sides backlink.
priority/critical|b60205|Actively harmful now: prod security, data loss, common-path crashes, progress-losing bugs.
priority/high|d93f0b|Confirmed and important: verified bugs with real impact, reachable security, explicit near-term.
priority/medium|fbca04|Worth doing, not urgent. Clear-value features/polish and plausible leads. The default tier.
priority/low|ededed|Nice-to-have, speculative, or deferred: pull-gated, possible non-bug, cosmetic, someday.
'

printf 'sync-labels: %s\n' "$repo"
rc=0
# Fed by process substitution, not a pipe: a piped `while` runs in a subshell, so an rc=1 set
# inside it is discarded and the script exits 0 having reported failures.
while IFS='|' read -r name colour desc; do
  [ -z "${name:-}" ] && continue
  if [ "$dry" = 1 ]; then
    printf '  would ensure  %-22s %s\n' "$name" "$desc"
    continue
  fi
  # --force makes this an upsert: colour and description converge on re-run, so editing this
  # file and re-running is the way to change a label, everywhere, at once.
  if out=$(gh label create "$name" --repo "$repo" --color "$colour" --description "$desc" --force 2>&1); then
    printf '  ok  %s\n' "$name"
  else
    printf '  FAIL %s: %s\n' "$name" "$out" >&2
    rc=1
  fi
done < <(printf '%s\n' "$labels")

exit $rc
