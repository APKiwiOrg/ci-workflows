#!/bin/sh
# check-dashes.sh - enforce the em/en-dash ban (global CLAUDE.md writing style, restated in this
# repo's AGENTS.md) in .md/.ts/.tsx files. Two modes so the local hook and CI cannot drift apart on what
# counts as a violation:
#
#   (default)   staged mode - checks only ADDED lines in the currently staged diff. What
#               .githooks/pre-commit runs at commit time: pre-existing dashes elsewhere in a touched
#               file are ignored, only what you are adding is checked.
#   --tree      whole-tree mode - checks every tracked .md/.ts/.tsx file as it stands. What CI runs, so a
#               commit that bypassed the hook (--no-verify, another IDE, the GitHub web UI) still gets
#               caught.
set -eu

mode=${1:-staged}

case "$mode" in
  --tree)
    hit=$(git ls-files '*.md' '*.ts' '*.tsx' | xargs grep -lE '—|–' 2>/dev/null || true)
    if [ -n "$hit" ]; then
      echo "check-dashes: em-dash or en-dash found in tracked .md/.ts/.tsx files (banned in shipped text):" >&2
      printf '%s\n' "$hit" | sed 's/^/  /' >&2
      exit 1
    fi
    ;;
  staged)
    added=$(git diff --cached --unified=0 -- '*.md' '*.ts' '*.tsx' 2>/dev/null | grep -E '^\+' | grep -vE '^\+\+\+' || true)
    if printf '%s' "$added" | LC_ALL=C grep -qF -e '—' -e '–'; then
      echo "check-dashes: staged .md/.ts/.tsx additions contain an em-dash or en-dash (banned in shipped text)." >&2
      exit 1
    fi
    ;;
  *)
    echo "check-dashes: unknown mode '$mode' (expected: staged, --tree)" >&2
    exit 2
    ;;
esac
