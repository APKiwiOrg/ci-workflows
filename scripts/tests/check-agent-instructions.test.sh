#!/bin/sh
# Shared instruction-budget check, adopted from APKiwiOrg/game-template.
# Exercise the instruction budget using isolated git indexes, never the caller's index.
set -eu
script=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)/check-agent-instructions.sh
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT HUP INT TERM
git init -q "$fixture"
cd "$fixture"
pass=0
expect() {
  wanted=$1
  shift
  actual=0
  sh "$script" "$@" > result.log 2>&1 || actual=$?
  if [ "$actual" -ne "$wanted" ]; then
    cat result.log
    echo "FAIL: expected $wanted, got $actual ($*)" >&2
    exit 1
  fi
  pass=$((pass + 1))
}
bytes() { head -c "$1" /dev/zero | tr '\000' x; }
expect 0 --tree
expect 0
bytes 16000 > AGENTS.md
bytes 384 > CLAUDE.md
git add AGENTS.md CLAUDE.md
expect 0 --tree
expect 0
printf x >> CLAUDE.md
expect 1 --tree
# Staged mode must judge the index, not an unstaged oversized working copy.
expect 0
git add CLAUDE.md
expect 1
# Unstaged repairs cannot hide an oversized index.
printf '# Short\n' > AGENTS.md
expect 1
expect 0 --tree
git add AGENTS.md
expect 0
# Deleting one entry point uses the remaining staged content.
git rm -qf CLAUDE.md
expect 0
expect 0 --tree
# UTF-8 is measured in bytes, not characters.
awk 'BEGIN { for (i=0;i<6000;i++) printf "\303\251\303\251" }' > AGENTS.md
expect 1 --tree
git add AGENTS.md
expect 1
mkdir nested
cd nested
expect 1 --tree
expect 1
expect 2 --unknown
echo "check-agent-instructions.test: $pass passed"
