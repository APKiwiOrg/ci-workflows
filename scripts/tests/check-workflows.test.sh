#!/bin/sh
# Exercise the hosted-runner and SHA-pin rules against fixture workflows and actions in a scratch folder.
set -eu
script=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)/check-workflows.sh
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT HUP INT TERM
cd "$fixture"
pass=0
sha=0123456789abcdef0123456789abcdef01234567
expect() {
  wanted=$1
  what=$2
  actual=0
  sh "$script" > result.log 2>&1 || actual=$?
  if [ "$actual" -ne "$wanted" ]; then
    cat result.log
    echo "FAIL: expected $wanted, got $actual ($what)" >&2
    exit 1
  fi
  pass=$((pass + 1))
}
mkdir -p .github/workflows .github/actions/pinned .github/actions/nested/deeper/inner
expect 0 'no workflows and no actions'

cat > .github/workflows/ci.yml <<EOF
jobs:
  test:
    runs-on: ubuntu-26.04
    steps:
      - uses: actions/checkout@$sha # v1.0.0
      - uses: ./.github/actions/pinned
EOF
expect 0 'a pinned workflow and a local action reference'

printf '      - uses: actions/setup-node@v4\n' >> .github/workflows/ci.yml
expect 1 'an unpinned workflow step'
sed '$d' .github/workflows/ci.yml > ci.yml && mv ci.yml .github/workflows/ci.yml
expect 0 'the unpinned workflow step removed'

sed 's/ubuntu-26.04/[self-hosted, linux]/' .github/workflows/ci.yml > .github/workflows/self.yml
expect 1 'a self-hosted workflow job'
rm .github/workflows/self.yml

# A pinned action is the control for each unpinned case below.
cat > .github/actions/pinned/action.yml <<EOF
name: pinned
runs:
  using: composite
  steps:
    - uses: actions/upload-artifact@$sha # v1.0.0
      with:
        name: marker
EOF
expect 0 'a pinned action'

# Inside a composite action `./` is the caller's workspace, not this repository's commit.
cp .github/actions/pinned/action.yml pinned.yml
printf '    - uses: ./.github/actions/other\n' >> .github/actions/pinned/action.yml
expect 1 'a local reference inside an action'
cp pinned.yml .github/actions/pinned/action.yml
printf '    - name: Other\n      uses: ../other\n' >> .github/actions/pinned/action.yml
expect 1 'a parent-relative reference inside an action'
mv pinned.yml .github/actions/pinned/action.yml
expect 0 'the local references removed'

cat > .github/actions/nested/deeper/inner/action.yml <<EOF
name: loose
runs:
  using: composite
  steps:
    - uses: actions/upload-artifact@v4
EOF
expect 1 'an unpinned action.yml in a nested folder'
rm .github/actions/nested/deeper/inner/action.yml

cat > .github/actions/pinned/action.yaml <<EOF
runs:
  using: composite
  steps:
    - name: Upload
      uses: actions/upload-artifact@main
EOF
expect 1 'an unpinned action.yaml'
rm .github/actions/pinned/action.yaml

mkdir -p '.github/actions/with space'
printf 'runs:\n  steps:\n    - uses: actions/cache@v4\n' > '.github/actions/with space/action.yml'
expect 1 'an unpinned action in a folder whose name has a space'
rm -r '.github/actions/with space'

# Only action.yml and action.yaml are actions. Other YAML beside them is not read as one.
printf 'uses: actions/cache@v4\n' > .github/actions/pinned/notes.yml
expect 0 'a non-action YAML file in an action folder'
echo "check-workflows.test: $pass passed"
