#!/bin/sh
# check-workflows.sh - the two rules every workflow and action in this public repository keeps.
#   1. No workflow job runs on a self-hosted runner. The repository is public, so a pull request could run
#      code on one, and the org's self-hosted runners are the dev Mac. An action has no runner of its own,
#      it runs on its caller's, so this rule reads workflows only.
#   2. Every third-party action and reusable workflow is pinned to a full commit SHA, in every workflow
#      under .github/workflows and every action.yml or action.yaml under .github/actions. In a workflow a
#      local `./` reference is exempt, since it is the same commit as the workflow.
#   3. An action.yml or action.yaml holds no local `./` or `../` reference. Inside a composite action such a
#      path is the caller's workspace, not this repository's commit, so it would run the caller's code.
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

# Exits 0 when the file has a `uses:` that is a relative path, naming each one.
has_local() {
  if grep -nE "^[[:space:]]*(-[[:space:]]*)?uses:[[:space:]]*[\"']?\." "$1" >&2; then
    echo "check-workflows: $1: a relative uses: inside an action is the caller's workspace" >&2
    return 0
  fi
  return 1
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
    has_local "$f" && fail=1
  done
  set +f
  IFS=$saved_ifs
fi

if [ "$fail" -eq 0 ]; then
  echo "check-workflows: every workflow is hosted, every workflow and action is SHA-pinned, and no action uses a relative path"
fi
exit $fail
