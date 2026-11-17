#!/bin/sh
# check-workflows.sh - the two rules every workflow and action in this public repository keeps.
#   1. No workflow job runs on a self-hosted runner. The repository is public, so a pull request could run
#      code on one, and the org's self-hosted runners are the dev Mac. An action has no runner of its own,
#      it runs on its caller's, so this rule reads workflows only.
#   2. Every third-party action and reusable workflow is pinned to a full commit SHA, in every workflow
#      under .github/workflows and every action.yml or action.yaml under .github/actions. A local `./`
#      reference is exempt, since it is the same commit.
# Pure POSIX sh, grep and find, so it runs the same in the pre-commit hook, in CI and by hand.
fail=0

# Exits 0 when the file has a third-party `uses:` that is not pinned to a full SHA, naming each one.
has_unpinned() {
  grep -nE '^[[:space:]]*(-[[:space:]]*)?uses:[[:space:]]*[^.[:space:]]' "$1" | while IFS= read -r line; do
    case "$line" in
      *@[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
      *) echo "check-workflows: $1: not pinned to a full SHA: $line" >&2; echo x ;;
    esac
  done | grep -q x
}

for f in .github/workflows/*.yml; do
  [ -f "$f" ] || continue
  if grep -nE '^[[:space:]]*runs-on:.*self-hosted' "$f"; then
    echo "check-workflows: $f uses a self-hosted runner" >&2
    fail=1
  fi
  has_unpinned "$f" && fail=1
done

if [ -d .github/actions ]; then
  # One path per line, so a folder name with a space still reads as one path.
  saved_ifs=$IFS
  IFS='
'
  set -f
  for f in $(find .github/actions -type f \( -name action.yml -o -name action.yaml \) | sort); do
    has_unpinned "$f" && fail=1
  done
  set +f
  IFS=$saved_ifs
fi

if [ "$fail" -eq 0 ]; then
  echo "check-workflows: every workflow is hosted and every workflow and action is SHA-pinned"
fi
exit $fail
