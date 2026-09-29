#!/usr/bin/env bash
# Adopted from the game-repo fleet (game-template/scripts/ledger.sh). This copy is ci-workflows-local:
# there is no template propagation and no pre-commit template-sync gate in this repo, so edit it here.
#
# The discovered-work ledger: a local mirror of this repo's GitHub issue backlog.
#
# The backlog lives in GitHub Issues. This script keeps a local copy of it so that:
#
#   1. the SessionStart hook can state the open count without a network call in the hot path,
#   2. `search` keeps substring/identifier lookup working. GitHub's search is tokenized, so it
#      will not reliably find `SwingCooldowns` or `GroundNormalMinY`. grep over a local mirror
#      will. The mirror carries CLOSED issues too, which is the whole point: a declined item
#      stays findable, so the same non-bug does not get re-raised every few months.
#
# THE ONE RULE: never report a count this script did not actually read.
#
# `gh issue list` exits non-zero and prints nothing to stdout when auth is expired, the network
# is down, or no token exists. A naive `gh issue list | jq length` renders that as "0", which is
# not a degraded answer, it is an inverted one: "0 open" reads as "the sweep is clean" at the
# exact moment the tool has no idea what is open. So the cache is written ONLY after a verified
# success, and every reader distinguishes "mirror unavailable/stale" from "nothing open" and
# says which. An unknown answers "UNKNOWN", loudly. It never answers "0".
#
# Subcommands:
#   sync [--if-stale]   refresh the mirror from GitHub. Loud and non-zero on failure.
#   status              one-line backlog state for the SessionStart hook. Always exits 0.
#   search <term>       grep the mirrored titles + bodies, open and closed. Prior-art lookup.
#   show <number>       print one mirrored issue in full.
#
# Env: GH_TOKEN            a token, if `gh auth login` is not set up (Codex and CI need this).
#      CIW_LEDGER_STALE_AFTER  seconds before --if-stale refetches (default 900).
#      CIW_LEDGER_TIMEOUT      seconds before a fetch is abandoned (default 20).

set -uo pipefail

STALE_AFTER=${CIW_LEDGER_STALE_AFTER:-900}
FETCH_TIMEOUT=${CIW_LEDGER_TIMEOUT:-20}
FETCH_LIMIT=${CIW_LEDGER_LIMIT:-2000}

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "ledger: not inside a git repository" >&2; exit 2; }
cache="$root/.ledger"
issues="$cache/issues.json"
meta="$cache/meta.json"

# ---------------------------------------------------------------------------- helpers

die() { printf 'ledger: %s\n' "$1" >&2; exit 1; }

have_jq() { command -v jq >/dev/null 2>&1; }

# Portable timeout. macOS ships neither timeout(1) nor gtimeout(1), and this must run on the
# system bash (3.2), so no `wait -n` and no associative arrays.
run_with_timeout() {
  local secs="$1"; shift
  local out_f="$1"; shift
  local err_f="$1"; shift
  "$@" >"$out_f" 2>"$err_f" &
  local pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  local watcher=$!
  wait "$pid"; local rc=$?
  kill -TERM "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
  return $rc
}

now_epoch() { date +%s; }

# "3m" / "5h" / "2d". Deliberately coarse: the reader needs the order of magnitude.
human_age() {
  local s="$1"
  if [ "$s" -lt 90 ]; then printf '%ss' "$s"
  elif [ "$s" -lt 5400 ]; then printf '%sm' "$((s / 60))"
  elif [ "$s" -lt 172800 ]; then printf '%sh' "$((s / 3600))"
  else printf '%sd' "$((s / 86400))"; fi
}

meta_field() {
  [ -f "$meta" ] || return 1
  have_jq || return 1
  jq -r --arg k "$1" '.[$k] // empty' "$meta" 2>/dev/null
}

# Age of the last GOOD sync, in seconds. Non-zero if there has never been one.
cache_age() {
  local at; at=$(meta_field fetched_at_epoch) || return 1
  [ -n "$at" ] || return 1
  echo $(( $(now_epoch) - at ))
}

# ---------------------------------------------------------------------------- sync

cmd_sync() {
  local if_stale=0
  [ "${1:-}" = "--if-stale" ] && if_stale=1

  have_jq || die "jq is required (brew install jq)"
  command -v gh >/dev/null 2>&1 || die "gh is required (brew install gh)"

  if [ "$if_stale" = 1 ]; then
    local age
    if age=$(cache_age); then
      if [ "$age" -lt "$STALE_AFTER" ]; then
        return 0   # mirror is recent enough; skip the network entirely
      fi
    fi
  fi

  mkdir -p "$cache"
  local out_f err_f rc
  out_f=$(mktemp) || die "mktemp failed"
  err_f=$(mktemp) || die "mktemp failed"

  # --state all: closed issues must stay searchable, or a declined item gets re-raised.
  run_with_timeout "$FETCH_TIMEOUT" "$out_f" "$err_f" \
    gh issue list --state all --limit "$FETCH_LIMIT" \
      --json number,title,body,labels,url,state,updatedAt,createdAt
  rc=$?

  # A fetch counts as successful ONLY if gh said so AND the payload is really a JSON array.
  # Anything else leaves the previous mirror untouched: a stale-but-true mirror beats a
  # confidently-empty one.
  if [ "$rc" -ne 0 ] || ! jq -e 'type == "array"' "$out_f" >/dev/null 2>&1; then
    local reason
    reason=$(head -3 "$err_f" | tr '\n' ' ' | sed 's/[[:space:]]*$//')
    [ -z "$reason" ] && reason="gh exited $rc with no diagnostic"
    case "$reason" in
      *401*|*"Bad credentials"*|*"auth login"*)
        reason="$reason -- export GH_TOKEN or run: gh auth login" ;;
    esac
    rm -f "$out_f" "$err_f"
    printf 'ledger: sync FAILED, mirror NOT updated: %s\n' "$reason" >&2
    return 1
  fi

  local count truncated
  count=$(jq 'length' "$out_f")
  truncated=false
  [ "$count" -ge "$FETCH_LIMIT" ] && truncated=true

  mv "$out_f" "$issues"
  jq -n \
    --argjson total "$count" \
    --argjson open "$(jq '[.[] | select(.state == "OPEN")] | length' "$issues")" \
    --argjson truncated "$truncated" \
    --arg epoch "$(now_epoch)" \
    --arg iso "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg repo "$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo unknown)" \
    '{fetched_at_epoch: ($epoch | tonumber), fetched_at: $iso, repo: $repo,
      total: $total, open: $open, truncated: $truncated}' > "$meta"
  rm -f "$err_f"

  # A silent cap reads as "covered everything". Say it out loud instead.
  if [ "$truncated" = true ]; then
    printf 'ledger: WARNING mirror hit the %s-issue fetch cap; it may be incomplete. Raise CIW_LEDGER_LIMIT.\n' \
      "$FETCH_LIMIT" >&2
  fi
  return 0
}

# ---------------------------------------------------------------------------- status

# The SessionStart line. Four distinct states, three of which are NOT a number.
cmd_status() {
  local hint="prior art: scripts/ledger.sh search <term> (searches closed issues too)"

  if ! have_jq; then
    printf 'BACKLOG: UNKNOWN (jq missing, cannot read the local mirror).\n'
    printf '  This is not "nothing open". Install jq, then: scripts/ledger.sh sync\n'
    return 0
  fi

  local age
  if ! age=$(cache_age) || [ ! -f "$issues" ]; then
    printf 'BACKLOG: UNKNOWN (no local mirror of the issue backlog yet).\n'
    printf '  This is NOT "nothing open" -- nothing has been read. Run: scripts/ledger.sh sync\n'
    printf '  (needs gh auth, or GH_TOKEN exported.)\n'
    return 0
  fi

  local open total repo
  open=$(jq -r '.open // "?"' "$meta" 2>/dev/null)
  total=$(jq -r '.total // "?"' "$meta" 2>/dev/null)
  repo=$(jq -r '.repo // "?"' "$meta" 2>/dev/null)

  if [ "$age" -lt "$STALE_AFTER" ]; then
    printf 'backlog: %s open (%s, mirror synced %s ago). %s\n' \
      "$open" "$repo" "$(human_age "$age")" "$hint"
  else
    printf 'BACKLOG: STALE MIRROR. Last good sync was %s ago and showed %s open (of %s total).\n' \
      "$(human_age "$age")" "$open" "$total"
    printf '  That count is from the stale mirror, NOT from GitHub now. Refresh: scripts/ledger.sh sync\n'
    printf '  %s\n' "$hint"
  fi

  if [ "$(jq -r '.truncated // false' "$meta" 2>/dev/null)" = "true" ]; then
    printf '  WARNING: the mirror hit its fetch cap and may be incomplete.\n'
  fi
  return 0
}

# ---------------------------------------------------------------------------- search

cmd_search() {
  [ $# -ge 1 ] || die "usage: ledger.sh search <term>"
  have_jq || die "jq is required (brew install jq)"
  if [ ! -f "$issues" ]; then
    printf 'ledger: no local mirror to search. Run: scripts/ledger.sh sync\n' >&2
    printf 'ledger: (a no-match here would be meaningless -- nothing has been read.)\n' >&2
    exit 1
  fi

  local age; age=$(cache_age) || age=""
  if [ -n "$age" ] && [ "$age" -ge "$STALE_AFTER" ]; then
    printf 'ledger: NOTE mirror is %s old; an issue filed since then will not be here.\n\n' \
      "$(human_age "$age")" >&2
  fi

  local term="$*"
  local hits
  hits=$(jq -r --arg t "$term" '
    [ .[] | select(((.title // "") + "\n" + (.body // "")) | ascii_downcase | contains($t | ascii_downcase)) ]
    | sort_by(.state, -.number)
    | .[]
    | "\(if .state == "CLOSED" then "[CLOSED] " else "" end)#\(.number)  \(.title)\n    \(.url)\n" +
      ( ((.body // "") | split("\n")
         | map(select(ascii_downcase | contains($t | ascii_downcase)))
         | map("    | " + .) | .[0:3] | join("\n")) )
  ' "$issues" 2>/dev/null)

  if [ -z "$hits" ]; then
    printf 'no match for "%s" in %s mirrored issues (open and closed).\n' \
      "$term" "$(jq 'length' "$issues" 2>/dev/null)"
    return 0
  fi
  printf '%s\n' "$hits"
}

# ---------------------------------------------------------------------------- show

cmd_show() {
  [ $# -ge 1 ] || die "usage: ledger.sh show <number>"
  have_jq || die "jq is required (brew install jq)"
  [ -f "$issues" ] || die "no local mirror. Run: scripts/ledger.sh sync"
  jq -r --argjson n "$1" '
    .[] | select(.number == $n)
    | "#\(.number)  \(.title)\n\(.url)\nstate: \(.state)   labels: \([.labels[].name] | join(", "))\n\n\(.body // "")"
  ' "$issues"
}

# ---------------------------------------------------------------------------- main

cmd=${1:-status}
[ $# -gt 0 ] && shift
case "$cmd" in
  sync)   cmd_sync "$@" ;;
  status) cmd_status "$@" ;;
  search) cmd_search "$@" ;;
  show)   cmd_show "$@" ;;
  help|-h|--help)
    sed -n '5,30p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown subcommand '$cmd' (try: sync | status | search | show)" ;;
esac
