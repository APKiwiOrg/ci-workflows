#!/bin/sh
# check-workflows.sh - the two rules every workflow in this public repository keeps.
#   1. No job runs on a self-hosted runner. The repository is public, so a pull request could run code on
#      one, and the org's self-hosted runners are the dev Mac.
#   2. Every third-party action and reusable workflow is pinned to a full commit SHA.
# Pure POSIX sh and grep, so it runs the same in the pre-commit hook, in CI and by hand.
fail=0
for f in .github/workflows/*.yml; do
  [ -f "$f" ] || continue
  if grep -nE '^[[:space:]]*runs-on:.*self-hosted' "$f"; then
    echo "check-workflows: $f uses a self-hosted runner" >&2
    fail=1
  fi
  grep -nE '^[[:space:]]*(-[[:space:]]*)?uses:[[:space:]]*[^.[:space:]]' "$f" | while IFS= read -r line; do
    case "$line" in
      *@[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
      *) echo "check-workflows: $f: not pinned to a full SHA: $line" >&2; echo x ;;
    esac
  done | grep -q x && fail=1
done
if [ "$fail" -eq 0 ]; then echo "check-workflows: every workflow is hosted and SHA-pinned"; fi
exit $fail
