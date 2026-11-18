#!/bin/sh
# Exercise the stress workflow's own shell steps, extracted from the YAML, against a stub dotnet. Proves the
# input checks, log and TRX retention, per-run artifact directories and the step wiring without a build, a
# real test process, sleeps or load. Every case runs at most three stub iterations.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/stress-workflow.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
pass=0

fail() {
  echo "FAIL: $*" >&2
  if [ -f "$work/case/out" ]; then sed 's/^/  | /' "$work/case/out" >&2; fi
  exit 1
}

# Extract the step scripts and check the wiring that no shell run can see.
wiring_failed=0
python3 - "$root/.github/workflows/stress-test.yml" "$work" <<'EOF' || wiring_failed=$?
import sys, yaml
workflow = yaml.safe_load(open(sys.argv[1]))
out = sys.argv[2]
problems = []
def need(ok, what):
    if not ok:
        problems.append(what)
inputs = workflow[True]['workflow_call']['inputs']
need(inputs.get('artifact-env', {}).get('type') == 'string', 'artifact-env is a string input')
need(inputs['artifact-env'].get('default') == '', 'artifact-env defaults to empty')
need(inputs['artifact-env'].get('required') is False, 'artifact-env is optional')
compare_ref = inputs.get('compare-ref', {})
need(compare_ref.get('type') == 'string' and compare_ref.get('default') == ''
     and compare_ref.get('required') is False, 'compare-ref is an optional string defaulting to empty')
job = workflow['jobs']['stress']
need(job['runs-on'] == 'ubuntu-26.04', 'job stays on ubuntu-26.04')
need(job['timeout-minutes'] == "${{ inputs.compare-ref != '' && 180 || 90 }}",
     'job timeout is 180 in compare mode and 90 otherwise')
steps = {step.get('name'): step for step in job['steps']}
for step in job['steps']:
    need('${{' not in step.get('run', ''), 'no expression inside a run line: %s' % step.get('name'))
check = steps['Check the inputs']
repeat = steps['Repeat the selection']
for step in (check, repeat):
    need(step['env'].get('ARTIFACT_ENV') == '${{ inputs.artifact-env }}', 'ARTIFACT_ENV reaches %s through env' % step['name'])
checkout = steps.get('Check out the comparison ref', {})
for step in (check, checkout, steps['Build once'], repeat):
    need(step.get('env', {}).get('COMPARE_REF') == '${{ inputs.compare-ref }}',
         'COMPARE_REF reaches %s through env' % step.get('name', 'comparison checkout'))
need(checkout.get('if') == "inputs.compare-ref != ''", 'empty compare-ref skips the extra checkout')
step_order = list(steps)
need(checkout and step_order.index('Cache NuGet packages') < step_order.index(checkout['name'])
     < step_order.index('Build once') < step_order.index('Repeat the selection'),
     'comparison checkout stays outside primary cache key calculation and both builds precede testing')
need(repeat.get('timeout-minutes') == "${{ inputs.compare-ref != '' && 150 || (inputs.artifact-env != '' && 60 || 90) }}",
     'repeat timeout is 150 in compare mode and keeps the legacy 60/90 limits otherwise')
need('timeout-minutes' not in check, 'check step keeps no timeout of its own')
cost = steps['Upload support cost artifacts']
artifacts = steps.get('Upload the stress artifacts', {})
failing = steps['Upload the failing runs']
compare = steps.get('Upload the stress comparison', {})
need(cost['if'] == "always() && inputs.compare-ref == '' && inputs.support-cost",
     'support-cost upload keeps its condition when compare-ref is empty')
need(compare.get('if') == "always() && inputs.compare-ref != ''", 'compare upload always runs only in compare mode')
need(compare.get('with') == {'name': 'stress-compare', 'path': '${{ runner.temp }}/stress',
                             'retention-days': 7, 'if-no-files-found': 'warn',
                             'include-hidden-files': True}, 'compare upload takes the entire tree')
need(cost['with'] == {'name': 'support-cost', 'path': '${{ runner.temp }}/stress/support-cost',
                      'retention-days': 7, 'if-no-files-found': 'error'}, 'support-cost upload unchanged')
need(artifacts.get('if') == "always() && inputs.compare-ref == '' && inputs.artifact-env != ''",
     'stress-artifacts upload runs always in single-ref artifact mode')
need(compare.get('uses') == artifacts.get('uses') == failing['uses'] == cost['uses'],
     'every upload uses the same pinned action')
need(artifacts.get('with') == {'name': 'stress-artifacts', 'path': '${{ runner.temp }}/stress',
                               'retention-days': 7, 'if-no-files-found': 'warn'}, 'stress-artifacts upload shape')
need(failing['if'] == "failure() && inputs.compare-ref == '' && inputs.artifact-env == ''",
     'failing-runs upload keeps its condition when compare-ref is empty')
need(failing['with'] == {'name': 'stress-test-results', 'path': '${{ runner.temp }}/stress',
                         'retention-days': 7, 'if-no-files-found': 'ignore'}, 'failing-runs upload unchanged')
for name, step in (('check', check), ('repeat', repeat), ('checkout', checkout), ('build', steps['Build once'])):
    if 'run' in step:
        with open(out + '/' + name + '.sh', 'w') as script:
            script.write(step['run'])
if problems:
    for problem in problems:
        print('FAIL: ' + problem, file=sys.stderr)
    sys.exit(1)
EOF
[ "$wiring_failed" -ne 0 ] || pass=$((pass + 1))

# The stub reads one outcome per call from STUB_PLAN: pass, empty, notrx or fail:<exit>. notrx exits zero
# without writing a TRX. It records what it saw of
# STUB_WATCH in STUB_SEEN and writes a condition file into that directory, as a caller's tests would.
mkdir -p "$work/bin" "$work/repo/scripts"
cat > "$work/bin/dotnet" <<'EOF'
#!/bin/sh
if [ "$1" = build ]; then
  printf '%s %s %s %s\n' "$(cat side.txt)" "$2" "$4" "${NUGET_PACKAGES:-default}" >> "$STUB_BUILDS"
  [ -f evicted ] && [ -d local-feed ]
  exit $?
fi
project=$2
configuration=$4
call=$(( $(cat "$STUB_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$call" > "$STUB_COUNT"
outcome=$(sed -n "${call}p" "$STUB_PLAN")
trx=""
results=""
filter=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --filter) filter=$2; shift ;;
    --logger) trx=${2#trx;LogFileName=}; shift ;;
    --results-directory) results=$2; shift ;;
  esac
  shift
done
if [ -f side.txt ]; then
  printf '%s %s %s %s %s %s\n' "$(cat side.txt)" "$GITHUB_SHA" \
    "$project" "$configuration" "$filter" "${NUGET_PACKAGES:-default}" >> "$STUB_TRACE"
fi
dir=$(printenv "$STUB_WATCH" || true)
echo "$call ${dir:-unset}" >> "$STUB_SEEN"
echo "stub call $call: $outcome"
if [ -n "$dir" ]; then echo "{\"call\": $call}" > "$dir/condition-1.json"; fi
if [ -n "${KHAOZ_SUPPORT_COST_ARTIFACT:-}" ]; then
  mkdir -p "$(dirname "$KHAOZ_SUPPORT_COST_ARTIFACT")"
  echo "{\"call\": $call}" > "$KHAOZ_SUPPORT_COST_ARTIFACT"
fi
case "$outcome" in
  pass) executed=1 code=0 ;;
  empty) executed=0 code=0 ;;
  notrx) exit 0 ;;
  fail:*) executed=1 code=${outcome#fail:}; echo "[FAIL] Stub.Test"; echo "Error Message: stub failure $call" ;;
  *) echo "stub: no plan for call $call" >&2; exit 99 ;;
esac
echo "<Counters total=\"1\" executed=\"$executed\" />" > "$results/$trx"
exit "$code"
EOF
chmod +x "$work/bin/dotnet"
cat > "$work/repo/scripts/record-support-cost-exit.py" <<'EOF'
import sys
open(sys.argv[1], 'a').write('exit %s\n' % sys.argv[2])
EOF
: > "$work/repo/Stub.csproj"

start_case() {
  rm -rf "$work/case"
  mkdir -p "$work/case/temp"
  checkout_dir="$work/repo"
  # shellcheck disable=SC2086 # the plan splits into one outcome per stub call
  printf '%s\n' $1 > "$work/case/plan"
}

# run_case_step <script> <expected exit> [NAME=value...]: run a step in the current checkout with the
# runner's environment and the step's input defaults, overridden by the arguments.
run_case_step() {
  script=$1
  expected=$2
  shift 2
  [ -f "$work/$script" ] || fail "missing workflow step $script"
  actual=0
  (cd "$checkout_dir" && env -i PATH="$work/bin:$PATH" HOME="$work/case" \
    RUNNER_TEMP="$work/case/temp" GITHUB_STEP_SUMMARY="$work/case/summary" \
    GITHUB_REF=refs/heads/primary GITHUB_SHA=runner-sha \
    STUB_PLAN="$work/case/plan" STUB_COUNT="$work/case/count" STUB_SEEN="$work/case/seen" \
    STUB_WATCH=KE_COVERAGE_COST_REPORTS STUB_TRACE="$work/case/trace" STUB_BUILDS="$work/case/builds" \
    PROJECT=Stub.csproj FILTER='FullyQualifiedName~Stub&Name!=x' ITERATIONS=1 LOAD=0 \
    CONFIGURATION=Release SUPPORT_COST=false ARTIFACT_ENV='' COMPARE_REF='' BUILD_TARGET='' "$@" \
    bash -e "$work/$script") > "$work/case/out" 2>&1 || actual=$?
  [ "$actual" -eq "$expected" ] || fail "$script $* exited $actual, expected $expected"
  pass=$((pass + 1))
}

run_step() {
  start_case "$3"
  step_script=$1
  step_status=$2
  shift 3
  run_case_step "$step_script" "$step_status" "$@"
}
stress="$work/case/temp/stress"
has() { [ -e "$stress/$1" ] || fail "missing $1"; }
lacks() { [ ! -e "$stress/$1" ] || fail "unexpected $1"; }
says() { grep -qF -- "$2" "$1" || fail "$1 lacks: $2"; }
calls() { [ "$(cat "$work/case/count")" -eq "$1" ] || fail "stub ran $(cat "$work/case/count") times, expected $1"; }

# Default mode: passing runs delete their files, a failing run keeps its own log and TRX, nothing is set.
run_step repeat.sh 0 'pass pass' ITERATIONS=2
calls 2
lacks run-1.log; lacks run-1.trx; lacks run-2.log; lacks run-2.trx; lacks artifacts
lacks primary; lacks compare; lacks refs.txt
says "$work/case/seen" '1 unset'
says "$work/case/seen" '2 unset'
says "$work/case/summary" '2 passed, 0 failed, 0 selected nothing, of 2 runs'
if grep -q 'artifacts' "$work/case/summary"; then fail 'default summary mentions artifacts'; fi
run_step repeat.sh 1 'pass fail:3' ITERATIONS=2
lacks run-1.log; lacks run-1.trx; has run-2.log; has run-2.trx
says "$stress/run-2.log" 'Error Message: stub failure 2'
if grep -q 'stress-test: run' "$stress/run-2.log"; then fail 'default log gained artifact lines'; fi
says "$work/case/out" '::error::run 2 failed'
run_step repeat.sh 1 'empty' ITERATIONS=1
says "$work/case/out" '::error::run 1 selected no tests'
# Issue #1: a test process that exits zero but writes no TRX counts as selecting nothing. It keeps its log,
# and the later runs and the summary still happen.
run_step repeat.sh 1 'notrx pass' ITERATIONS=2
calls 2
has run-1.log; lacks run-1.trx; lacks run-2.log; lacks run-2.trx
says "$work/case/out" '::error::run 1 selected no tests'
says "$work/case/summary" '1 passed, 0 failed, 1 selected nothing, of 2 runs'

# Support-cost mode keeps its own protocol: the exit is finalized, passing logs still go.
run_step repeat.sh 0 'pass' SUPPORT_COST=true
says "$stress/support-cost/run-1.json" 'exit 0'
lacks run-1.log; lacks artifacts

# Artifact mode: every run gets its own directory, keeps its log and TRX, and records its actual exit.
run_step repeat.sh 1 'pass fail:3 pass' ITERATIONS=3 ARTIFACT_ENV=KE_COVERAGE_COST_REPORTS
calls 3
for n in 1 2 3; do
  has "run-$n.log"; has "run-$n.trx"
  says "$work/case/seen" "$n $stress/artifacts/run-$n"
  says "$stress/artifacts/run-$n/condition-1.json" "{\"call\": $n}"
  says "$stress/run-$n.log" "stress-test: run $n KE_COVERAGE_COST_REPORTS=$stress/artifacts/run-$n"
done
says "$stress/run-1.log" 'stress-test: run 1 dotnet test exit 0'
says "$stress/run-2.log" 'stress-test: run 2 dotnet test exit 3'
says "$stress/run-2.log" 'Error Message: stub failure 2'
says "$stress/run-3.log" 'stress-test: run 3 dotnet test exit 0'
says "$work/case/summary" '2 passed, 1 failed, 0 selected nothing, of 3 runs'
# shellcheck disable=SC2016 # literal backticks from the summary's markdown
says "$work/case/summary" '`KE_COVERAGE_COST_REPORTS` named a directory per run'
run_step repeat.sh 0 'pass' ARTIFACT_ENV=KE_COVERAGE_COST_REPORTS
has run-1.log; has run-1.trx; has artifacts/run-1/condition-1.json
run_step repeat.sh 1 'empty' ARTIFACT_ENV=KE_COVERAGE_COST_REPORTS
has run-1.log; has run-1.trx
says "$work/case/out" '::error::run 1 selected no tests'
# A shell local of the repeat step as the name reaches only the test process, never the step's own state.
run_step repeat.sh 0 'pass' ARTIFACT_ENV=log STUB_WATCH=log
has run-1.log; has artifacts/run-1/condition-1.json
says "$work/case/seen" "1 $stress/artifacts/run-1"

# Input checks. Valid names pass, and nothing below may run a command or echo a malformed value.
run_step check.sh 0 ''
run_step check.sh 0 '' ARTIFACT_ENV=KE_COVERAGE_COST_REPORTS
run_step check.sh 0 '' ARTIFACT_ENV=_reports9
newline=$(printf 'A\nB')
# shellcheck disable=SC2016 # the command substitutions are the hostile input, never expanded here
for bad in 9ABC A-B 'A B' 'A;touch pwned' '$(touch pwned)' '`touch pwned`' "$newline" 'A=B' '../x' \
    '::warning::x' "$(printf 'A\303\211')"; do
  run_step check.sh 1 '' ARTIFACT_ENV="$bad"
  says "$work/case/out" '::error::artifact-env must be a shell identifier'
  if grep -qF -e 'touch' -e '::warning::' "$work/case/out"; then fail "rejection echoed $bad"; fi
  [ ! -e "$work/repo/pwned" ] || fail "$bad ran a command"
done
for reserved in PATH path Home IFS CDPATH ENV BASH_ENV SHELLOPTS BASHOPTS GITHUB_TOKEN GITHUB_ENV \
    github_output RUNNER_TEMP ACTIONS_RUNTIME_TOKEN LD_PRELOAD DYLD_INSERT_LIBRARIES DOTNET_ROOT \
    NUGET_PACKAGES PYTHONPATH LC_ALL TMPDIR ARTIFACT_ENV FILTER BUILD_TARGET KHAOZ_SUPPORT_COST_ARTIFACT; do
  run_step check.sh 1 '' ARTIFACT_ENV="$reserved"
  says "$work/case/out" "::error::artifact-env $reserved is a reserved shell, process or runner name"
done
run_step check.sh 1 '' ARTIFACT_ENV=KE_INHERITED KE_INHERITED=1
says "$work/case/out" '::error::artifact-env KE_INHERITED is already set in the runner environment'
for shell_name in RANDOM PPID UID OPTIND; do
  run_step check.sh 1 '' ARTIFACT_ENV="$shell_name"
  says "$work/case/out" "::error::artifact-env $shell_name is already set in the runner environment"
done

# Use a real shallow checkout and a local origin, as checkout would prepare on the runner. The two refs
# have different root solutions and a marker distinguishing them in the dotnet stub's trace.
origin="$work/origin"
git init -q -b baseline "$origin"
printf '%s\n' compare > "$origin/side.txt"
: > "$origin/Stub.csproj"
: > "$origin/Compare.slnx"
mkdir "$origin/scripts"
printf '%s\n' ': > evicted' > "$origin/scripts/evict-stale-vendored.sh"
printf '%s\n' local-feed > "$origin/nuget.config"
git -C "$origin" add .
fixture_commit() { git -C "$origin" -c user.name=Fixture -c user.email=fixture@example.test commit -qm "$1"; }
fixture_commit baseline
compare_commit=$(git -C "$origin" rev-parse HEAD)
git -C "$origin" -c user.name=Fixture -c user.email=fixture@example.test tag -am baseline compare-tag
git -C "$origin" update-ref refs/tags/non-commit "$(git -C "$origin" rev-parse HEAD:side.txt)"
git -C "$origin" switch -qc missing-project
git -C "$origin" rm -q Stub.csproj
fixture_commit 'remove the test project'
git -C "$origin" switch -qc primary baseline
git -C "$origin" mv Compare.slnx Primary.sln
printf '%s\n' primary > "$origin/side.txt"
git -C "$origin" add side.txt
fixture_commit primary
primary_commit=$(git -C "$origin" rev-parse HEAD)

start_compare() {
  start_case "$1"
  checkout_dir="$work/case/primary"
  git clone -q --depth 1 --branch primary "file://$origin" "$checkout_dir" || fail 'create primary fixture'
}

# For step-level tests of build and repeat, prepare the second tree independently of the checkout step.
prepare_pair() {
  start_compare "$1"
  git clone -q --depth 1 --branch baseline "file://$origin" "$work/case/temp/stress-compare" ||
    fail 'create comparison fixture'
}

comparison_checkout() {
  start_compare ''
  run_case_step checkout.sh 0 COMPARE_REF="$1"
  compare_dir="$work/case/temp/stress-compare"
  [ "$(git -C "$compare_dir" rev-parse HEAD)" = "$compare_commit" ] || fail 'comparison checked out the wrong commit'
  if git -C "$compare_dir" symbolic-ref -q HEAD; then fail 'comparison HEAD is attached'; fi
  [ "$(git -C "$checkout_dir" rev-parse HEAD)" = "$primary_commit" ] || fail 'primary checkout changed'
  lacks primary/run-1.log
  tab=$(printf '\t')
  says "$stress/refs.txt" "primary${tab}refs/heads/primary${tab}$primary_commit"
  says "$stress/refs.txt" "compare${tab}$1${tab}$compare_commit"
  grep -qE '^Processor model: .+' "$stress/refs.txt" || fail 'missing processor model'
  grep -qE '^Logical processors: [1-9][0-9]*$' "$stress/refs.txt" || fail 'missing logical processor count'
}

comparison_build() {
  prepare_pair ''
  # The existing standalone setup steps already ran for the primary when the build step starts.
  (cd "$checkout_dir" && sh scripts/evict-stale-vendored.sh) ||
    fail 'prepare primary eviction marker'
  mkdir "$checkout_dir/local-feed"
  run_case_step build.sh 0 COMPARE_REF=baseline CONFIGURATION=Debug NUGET_PACKAGES="$work/case/primary-packages"
  cat > "$work/case/expected" <<EOF_BUILD
primary Primary.sln Debug $work/case/primary-packages
compare Compare.slnx Debug $work/case/temp/stress-compare-packages
EOF_BUILD
  diff -u "$work/case/expected" "$work/case/builds" || fail 'build each ref with its own solution and packages'
}

comparison_alternates() {
  prepare_pair 'pass pass pass pass'
  run_case_step repeat.sh 0 COMPARE_REF=baseline ITERATIONS=2 CONFIGURATION=Debug ARTIFACT_ENV=KE_COVERAGE_COST_REPORTS
  compare_packages="$work/case/temp/stress-compare-packages"
  cat > "$work/case/expected" <<EOF_TRACE
primary runner-sha Stub.csproj Debug FullyQualifiedName~Stub&Name!=x default
compare $compare_commit Stub.csproj Debug FullyQualifiedName~Stub&Name!=x $compare_packages
compare $compare_commit Stub.csproj Debug FullyQualifiedName~Stub&Name!=x $compare_packages
primary runner-sha Stub.csproj Debug FullyQualifiedName~Stub&Name!=x default
EOF_TRACE
  diff -u "$work/case/expected" "$work/case/trace" || fail 'same selection alternates and only compare overrides GITHUB_SHA'
  for side in primary compare; do
    for n in 1 2; do
      has "$side/run-$n.log"; has "$side/run-$n.trx"; has "$side/artifacts/run-$n/condition-1.json"
    done
  done
  lacks run-1.log; lacks run-1.trx; lacks artifacts
  says "$stress/primary/artifacts/run-1/condition-1.json" '{"call": 1}'
  says "$stress/compare/artifacts/run-1/condition-1.json" '{"call": 2}'
  says "$stress/compare/artifacts/run-2/condition-1.json" '{"call": 3}'
  says "$stress/primary/artifacts/run-2/condition-1.json" '{"call": 4}'
  says "$work/case/summary" "refs/heads/primary"
  says "$work/case/summary" "$primary_commit"
  says "$work/case/summary" "baseline"
  says "$work/case/summary" "$compare_commit"
}

comparison_verdict() {
  prepare_pair "pass $1"
  run_case_step repeat.sh 1 COMPARE_REF=baseline
  calls 2
  has primary/run-1.log; has primary/run-1.trx
  has compare/run-1.log; has compare/run-1.trx
  grep -qE 'primary.*1 passed, 0 failed, 0 selected nothing' "$work/case/summary" ||
    fail 'primary side should pass'
  grep -qE "compare.*$2" "$work/case/summary" || fail 'compare side should have its own verdict'
}

comparison_bad_ref() {
  run_step check.sh 1 '' COMPARE_REF="$1"
  says "$work/case/out" '::error::compare-ref'
}

comparison_bad_path() {
  run_step check.sh 1 '' COMPARE_REF=baseline "$1"
  says "$work/case/out" '::error::compare-ref requires checkout-relative'
}

comparison_conflict() {
  run_step check.sh 0 '' COMPARE_REF=baseline
  run_step check.sh 0 '' SUPPORT_COST=true
  run_step check.sh 1 '' COMPARE_REF=baseline SUPPORT_COST=true
  says "$work/case/out" '::error::support-cost cannot be used with compare-ref'
}

comparison_bad_checkout() {
  start_compare ''
  run_case_step checkout.sh 1 COMPARE_REF="$1"
  says "$work/case/out" "::error::$2"
}

compare_failures=0
compare_case() {
  if ( "$@" ); then
    pass=$((pass + 1))
  else
    echo "FAIL: comparison case $*" >&2
    compare_failures=$((compare_failures + 1))
  fi
}
compare_case comparison_checkout refs/tags/compare-tag
compare_case comparison_checkout "$compare_commit"
compare_case comparison_build
compare_case comparison_alternates
compare_case comparison_verdict fail:3 '0 passed, 1 failed, 0 selected nothing'
compare_case comparison_verdict empty '0 passed, 0 failed, 1 selected nothing'
compare_case comparison_bad_ref '   '
compare_case comparison_bad_ref -baseline
compare_case comparison_bad_ref 'baseline:refs/heads/unwanted'
compare_case comparison_bad_path "PROJECT=$work/repo/Stub.csproj"
compare_case comparison_bad_path BUILD_TARGET=../Stub.csproj
compare_case comparison_conflict
compare_case comparison_bad_checkout non-commit 'compare-ref must resolve to a commit'
compare_case comparison_bad_checkout missing-project 'project Stub.csproj does not exist on the comparison ref'
compare_case comparison_bad_checkout missing-ref 'compare-ref could not be fetched'

[ "$wiring_failed" -eq 0 ] && [ "$compare_failures" -eq 0 ] || exit 1
echo "stress-test-workflow tests: $pass passed"
