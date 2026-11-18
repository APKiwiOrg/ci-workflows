#!/bin/sh
# Exercise the stress workflow's own shell steps, extracted from the YAML, against a stub dotnet. Proves the
# input checks, disk cleanup and evidence, log and TRX retention, per-run artifacts and step wiring without
# a build, a real test process, sleeps or load. Every case runs at most three stub iterations.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/stress-workflow.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
pass=0
# The fixtures bring their own git filters. A host's system or user git configuration, such as an installed Git LFS
# filter on a hosted runner, must not rewrite fixture files on add, clone or checkout.
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM GIT_CONFIG_GLOBAL

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
compare_check = steps.get('Check comparison inputs', {})
free_disk = steps.get('Free disk space for comparison', {})
checkout = steps.get('Check out the comparison ref', {})
for name in ('Check comparison inputs', 'Check the inputs', 'Check out the comparison ref', 'Build once', 'Repeat the selection'):
    need(steps.get(name, {}).get('env', {}).get('COMPARE_REF') == '${{ inputs.compare-ref }}',
         'COMPARE_REF reaches %s through env' % name)
need(checkout.get('if') == "inputs.compare-ref != ''", 'empty compare-ref skips the extra checkout')
step_order = list(steps)
first_step = job['steps'][0]
need(compare_check and first_step == compare_check and compare_check.get('if') == "inputs.compare-ref != ''",
     'compare-only input gate precedes the first checkout')
need(checkout and step_order.index('Cache NuGet packages') < step_order.index(checkout['name'])
     < step_order.index('Build once') < step_order.index('Repeat the selection'),
     'comparison checkout stays outside primary cache key calculation and both builds precede testing')
need(free_disk.get('if') == "inputs.compare-ref != ''",
     'disk cleanup runs only in compare mode')
need(free_disk and step_order.index('Check the inputs') < step_order.index(free_disk['name'])
     < step_order.index('Build once'), 'disk cleanup follows input checks and precedes both builds')
need(free_disk and not free_disk.get('continue-on-error') and 'if' not in steps['Build once']
     and 'if' not in repeat, 'disk cleanup failure skips both builds and the runs')
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
need(free_disk and step_order.index(free_disk['name']) < step_order.index(compare['name']),
     'the always-on compare upload follows the disk preflight even when it fails')
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
for name, step in (('check', check), ('compare-check', compare_check), ('free-disk', free_disk),
                   ('repeat', repeat), ('checkout', checkout), ('build', steps['Build once'])):
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
  [ -f evicted ] && [ -d local-feed ] || exit 98
  if [ "$(cat side.txt)" = "$STUB_FAIL_BUILD" ]; then exit 7; fi
  touch built
  exit
fi
if [ ! -f built ]; then
  echo 'stub: test ran before its own build' >&2
  exit 98
fi
project=$2
configuration=$4
call=$(( $(cat "$STUB_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$call" > "$STUB_COUNT"
outcome=$(sed -n "${call}p" "$STUB_PLAN")
trx=""
results=""
filter=""
no_build=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-build) no_build=true ;;
    --filter) filter=$2; shift ;;
    --logger) trx=${2#trx;LogFileName=}; shift ;;
    --results-directory) results=$2; shift ;;
  esac
  shift
done
if [ "$no_build" != true ]; then echo 'stub: missing --no-build' >&2; exit 98; fi
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
cat > "$work/bin/stat" <<'EOF'
#!/bin/sh
[ "$#" -eq 4 ] && [ "$1 $2 $3" = '-Lc %d --' ] || exit 98
case "$4" in
  "$GITHUB_WORKSPACE") echo 1 ;;
  "$RUNNER_TEMP") if [ -f "$STUB_DISK/runner-temp" ]; then echo 2; else echo 1; fi ;;
  *) exit 98 ;;
esac
EOF
cat > "$work/bin/df" <<'EOF'
#!/bin/sh
set -eu
[ "$#" -eq 3 ] && [ "$1 $2" = '-Pk --' ] || exit 98
case "$3" in
  "$GITHUB_WORKSPACE") side=workspace ;;
  "$RUNNER_TEMP") side=runner-temp ;;
  *) exit 98 ;;
esac
echo "$side" >> "$STUB_DISK/calls"
available=25165824
if [ -f "$STUB_DISK/$side" ]; then
  call=$(grep -cx "$side" "$STUB_DISK/calls")
  available=$(sed -n "${call}p" "$STUB_DISK/$side")
  [ -n "$available" ] || exit 98
fi
echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'
printf '/dev/%s 33554432 %s %s 30%% %s\n' "$side" "$((33554432 - available))" "$available" "$3"
EOF
cat > "$work/bin/sudo" <<'EOF'
#!/bin/sh
[ "$#" -eq 5 ] && [ "$1 $2 $3 $4" = '-n rm -rf --' ] || exit 98
case "$5" in
  "$STUB_IMAGE"/*) ;;
  *) echo 'unsafe cleanup fixture path' >&2; exit 98 ;;
esac
echo "${5#"$STUB_IMAGE"}" >> "$STUB_DISK/removals"
[ -d "$5" ] || exit 98
if [ "$5" = "$STUB_REMOVE_FAILURE" ]; then echo 'stub: removal denied' >&2; exit 1; fi
rm -rf -- "$5"
EOF
chmod +x "$work/bin/dotnet" "$work/bin/stat" "$work/bin/df" "$work/bin/sudo"
cat > "$work/repo/scripts/record-support-cost-exit.py" <<'EOF'
import sys
open(sys.argv[1], 'a').write('exit %s\n' % sys.argv[2])
EOF
: > "$work/repo/Stub.csproj"
: > "$work/repo/built"

start_case() {
  rm -rf "$work/case"
  mkdir -p "$work/case/temp" "$work/case/disk"
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
  set -- "$@" bash -e "$work/$script"
  if [ "$script" = free-disk.sh ]; then set -- "$@" "$work/case/image"; fi
  actual=0
  (cd "$checkout_dir" && env -i PATH="$work/bin:$PATH" HOME="$work/case" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    RUNNER_TEMP="$work/case/temp" GITHUB_WORKSPACE="$checkout_dir" GITHUB_STEP_SUMMARY="$work/case/summary" \
    GITHUB_REF=refs/heads/primary GITHUB_SHA=runner-sha GITHUB_EVENT_NAME=workflow_dispatch \
    STUB_PLAN="$work/case/plan" STUB_COUNT="$work/case/count" STUB_SEEN="$work/case/seen" \
    STUB_WATCH=KE_COVERAGE_COST_REPORTS STUB_TRACE="$work/case/trace" STUB_BUILDS="$work/case/builds" \
    STUB_FAIL_BUILD='' STUB_DISK="$work/case/disk" STUB_IMAGE="$work/case/image" STUB_REMOVE_FAILURE='' \
    PROJECT=Stub.csproj FILTER='FullyQualifiedName~Stub&Name!=x' ITERATIONS=1 LOAD=0 \
    CONFIGURATION=Release SUPPORT_COST=false ARTIFACT_ENV='' COMPARE_REF='' BUILD_TARGET='' "$@") \
    > "$work/case/out" 2>&1 || actual=$?
  [ "$actual" -eq "$expected" ] || fail "$script exited $actual, expected $expected"
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
lacks primary; lacks compare; lacks refs.txt; lacks progress.txt
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
ln -s Stub.csproj "$origin/Inside.csproj"
ln -s ../../outside.csproj "$origin/Outside.csproj"
ln -s ../outside-target "$origin/OutsideBuild"
printf '%s\n' '*.bin filter=lfs' > "$origin/.gitattributes"
printf '%s\n' 'version https://git-lfs.github.com/spec/v1' > "$origin/Pointer.bin"
mkdir "$origin/scripts"
printf '%s\n' ': > evicted' > "$origin/scripts/evict-stale-vendored.sh"
printf '%s\n' local-feed > "$origin/nuget.config"
git -C "$origin" add .
fixture_commit() { git -C "$origin" -c user.name=Fixture -c user.email=fixture@example.test commit -qm "$1"; }
fixture_commit baseline
compare_commit=$(git -C "$origin" rev-parse HEAD)
git -C "$origin" -c user.name=Fixture -c user.email=fixture@example.test tag -am baseline compare-tag
git -C "$origin" branch tag baseline
git -C "$origin" update-ref refs/tags/non-commit "$(git -C "$origin" rev-parse HEAD:side.txt)"
git -C "$origin" switch -qc missing-project
git -C "$origin" rm -q Stub.csproj
fixture_commit 'remove the test project'
git -C "$origin" switch -qc primary baseline
git -C "$origin" mv Compare.slnx Primary.sln
printf '%s\n' primary > "$origin/side.txt"
ln -sf Stub.csproj "$origin/Outside.csproj"
git -C "$origin" add side.txt Outside.csproj
fixture_commit primary
primary_commit=$(git -C "$origin" rev-parse HEAD)

start_compare() {
  start_case "$1"
  checkout_dir="$work/case/primary"
  git clone -q --depth 1 --branch primary "file://$origin" "$checkout_dir" || fail 'create primary fixture'
  (cd "$checkout_dir" && sh scripts/evict-stale-vendored.sh && mkdir local-feed) || fail 'prepare primary build'
  mkdir -p "$stress"
}

# Link and build the real worktree. The input gate is covered separately against the runner lifecycle.
build_pair() {
  run_case_step free-disk.sh 0
  run_case_step checkout.sh 0 COMPARE_REF=baseline "$@"
  run_case_step build.sh 0 COMPARE_REF=baseline "$@"
}

comparison_disk_removes_tool() {
  start_compare ''
  tool="$work/case/image/$1"
  mkdir -p "$tool"
  : > "$tool/installed"
  run_case_step free-disk.sh 0
  [ ! -e "$tool" ] || fail "$1 was not removed"
}

comparison_disk_skips_missing() {
  start_compare ''
  image="$work/case/image"
  mkdir -p "$image/opt/hostedtoolcache/node" "$image/opt/hostedtoolcache/Python" "$image/usr/share/dotnet"
  run_case_step free-disk.sh 0
  [ ! -e "$work/case/disk/removals" ] || fail 'cleanup tried to remove unlisted or missing tools'
  for tool in opt/hostedtoolcache/node opt/hostedtoolcache/Python usr/share/dotnet; do
    [ -d "$image/$tool" ] || fail "cleanup removed $tool"
  done
}

comparison_disk_removal_failure() {
  start_compare ''
  android="$work/case/image/usr/local/lib/android"
  swift="$work/case/image/usr/share/swift"
  mkdir -p "$android" "$swift"
  run_case_step free-disk.sh 0 STUB_REMOVE_FAILURE="$android"
  grep -qE "^::warning title=compare-disk-cleanup::.*$android" "$work/case/out" ||
    fail 'the failed removal has no warning identifying its directory'
  [ -d "$android" ] && [ ! -e "$swift" ] || fail 'a failed removal prevented cleanup of another directory'
}

append_disk_log() {
  grep '^disk-' "$work/case/out" >> "$work/case/disk.log" || fail 'step log has no disk readings'
}

comparison_disk_evidence() {
  start_compare 'pass pass'
  printf '%s\n' 13631488 23068672 18874368 14680064 12582912 > "$work/case/disk/workspace"
  if [ "$1" = split ]; then
    printf '%s\n' 19922944 20971520 20971520 15728640 14680064 > "$work/case/disk/runner-temp"
  fi
  run_case_step compare-check.sh 0 COMPARE_REF=baseline
  run_case_step check.sh 0 COMPARE_REF=baseline
  run_case_step free-disk.sh 0
  append_disk_log
  run_case_step checkout.sh 0 COMPARE_REF=baseline
  run_case_step build.sh 0 COMPARE_REF=baseline
  append_disk_log
  run_case_step repeat.sh 0 COMPARE_REF=baseline
  append_disk_log
  tab=$(printf '\t')
  case "$1" in
    shared)
      cat > "$work/case/expected" <<EOF_DISK
disk-before-freeing-workspace${tab}13631488
disk-after-freeing-workspace${tab}23068672
disk-after-primary-build-workspace${tab}18874368
disk-after-comparison-build-workspace${tab}14680064
disk-after-runs-workspace${tab}12582912
EOF_DISK
      ;;
    split)
      cat > "$work/case/expected" <<EOF_DISK
disk-before-freeing-workspace${tab}13631488
disk-before-freeing-runner-temp${tab}19922944
disk-after-freeing-workspace${tab}23068672
disk-after-freeing-runner-temp${tab}20971520
disk-after-primary-build-workspace${tab}18874368
disk-after-primary-build-runner-temp${tab}20971520
disk-after-comparison-build-workspace${tab}14680064
disk-after-comparison-build-runner-temp${tab}15728640
disk-after-runs-workspace${tab}12582912
disk-after-runs-runner-temp${tab}14680064
EOF_DISK
      ;;
  esac
  grep '^disk-' "$stress/progress.txt" > "$work/case/readings" || fail 'artifact has no disk readings'
  diff -u "$work/case/expected" "$work/case/readings" || fail 'disk evidence is out of order or incomplete'
  diff -u "$work/case/readings" "$work/case/disk.log" || fail 'disk evidence differs from the step logs'
}

comparison_disk_floor() {
  start_compare 'pass pass'
  available=1048576
  printf '%s\n' 524288 "$available" > "$work/case/disk/$1"
  run_case_step compare-check.sh 0 COMPARE_REF=baseline
  run_case_step check.sh 0 COMPARE_REF=baseline
  run_case_step free-disk.sh 1
  diagnostic=$(grep '^::error title=compare-disk-space::' "$work/case/out") || fail 'missing disk space error'
  case "$diagnostic" in *"$1"*) ;; *) fail 'disk space error does not identify the filesystem' ;; esac
  required=$(printf '%s\n' "$diagnostic" | awk -v available="$available" '
    { for (i = 1; i < NF; i++) if ($(i+1) == "KiB") {
        if ($i == available) found = 1
        if ($i > available && $i ~ /^[0-9]+$/) required = $i
      }
      if (found && required) print required
    }')
  [ -n "$required" ] || fail 'disk space error does not report available and required KiB'
  tab=$(printf '\t')
  says "$stress/progress.txt" "disk-before-freeing-$1${tab}524288"
  says "$stress/progress.txt" "disk-after-freeing-$1${tab}$available"
  [ ! -e "$work/case/builds" ] && [ ! -e "$work/case/count" ] || fail 'a build or run started during preflight'

  start_compare 'pass pass'
  printf '%s\n' 524288 "$required" > "$work/case/disk/$1"
  run_case_step free-disk.sh 0
}

legacy_disk() {
  start_compare 'pass'
  run_case_step build.sh 0
  run_case_step repeat.sh 0
  lacks progress.txt
  [ ! -e "$work/case/disk/calls" ] || fail 'single-ref run measured disk usage'
  [ ! -e "$work/case/disk/removals" ] || fail 'single-ref run removed preinstalled tools'
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

comparison_lfs_skip() {
  start_compare ''
  cat > "$work/case/lfs-smudge.sh" <<'EOF_LFS'
#!/bin/sh
if [ "${GIT_LFS_SKIP_SMUDGE:-0}" = 1 ]; then cat; else echo 'unexpected LFS smudge' >&2; exit 1; fi
EOF_LFS
  git -C "$checkout_dir" config filter.lfs.smudge "sh '$work/case/lfs-smudge.sh'"
  git -C "$checkout_dir" config filter.lfs.required true
  run_case_step checkout.sh 0 COMPARE_REF=baseline
  cmp "$work/case/temp/stress-compare/Pointer.bin" "$checkout_dir/Pointer.bin" ||
    fail 'comparison changed the LFS pointer'
}

comparison_build_failure() {
  start_compare ''
  run_case_step free-disk.sh 0
  run_case_step checkout.sh 0 COMPARE_REF=baseline
  run_case_step build.sh 7 COMPARE_REF=baseline STUB_FAIL_BUILD="$1"
  says "$stress/progress.txt" "$1 build: exit 7"
  if [ "$1" = compare ]; then
    says "$stress/progress.txt" 'primary build: exit 0'
  else
    if grep -q '^compare ' "$work/case/builds"; then fail 'comparison ran after primary build failed'; fi
  fi
  [ ! -e "$work/case/count" ] || fail 'tests ran after a build failure'
}

comparison_paths() {
  start_compare 'pass pass'
  : > "$work/case/outside.csproj"
  : > "$work/case/outside-target"
  : > "$work/case/temp/outside-target"
  run_case_step free-disk.sh 0
  run_case_step checkout.sh 0 COMPARE_REF=baseline PROJECT="$1"
  run_case_step build.sh "$3" COMPARE_REF=baseline PROJECT="$1" BUILD_TARGET="$2"
  if [ "$3" -eq 0 ]; then
    run_case_step repeat.sh 0 COMPARE_REF=baseline PROJECT="$1"
    calls 2
  else
    says "$work/case/out" 'outside its checkout'
    [ ! -e "$work/case/builds" ] || fail 'a build ran before both sides were checked'
  fi
}

comparison_alternates() {
  start_compare 'pass pass pass pass'
  run_case_step compare-check.sh 0 COMPARE_REF=baseline
  run_case_step check.sh 0 COMPARE_REF=baseline
  build_pair CONFIGURATION=Debug NUGET_PACKAGES="$work/case/primary-packages"
  cat > "$work/case/expected" <<EOF_BUILD
primary Primary.sln Debug $work/case/primary-packages
compare Compare.slnx Debug $work/case/temp/stress-compare-packages
EOF_BUILD
  diff -u "$work/case/expected" "$work/case/builds" || fail 'build each linked ref with its own solution and packages'
  [ -f "$work/case/temp/stress-compare/.git" ] || fail 'comparison is not a linked worktree'
  for stage in 'comparison validation' 'input validation' fetch peel worktree 'compare project' 'primary build' 'compare build'; do
    says "$stress/progress.txt" "$stage: exit 0"
  done
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
  if [ "$1" = primary ]; then start_compare "$2 pass"; else start_compare "pass $2"; fi
  build_pair
  run_case_step repeat.sh 1 COMPARE_REF=baseline
  calls 2
  has primary/run-1.log; has primary/run-1.trx
  has compare/run-1.log; has compare/run-1.trx
  for side in primary compare; do
    counts='1 passed, 0 failed, 0 selected nothing'
    if [ "$side" = "$1" ]; then counts=$3; fi
    grep -qE "$side.*$counts" "$work/case/summary" || fail "$side should report $counts"
  done
}

comparison_summary() {
  start_compare 'pass pass'
  build_pair
  # These are legal ref names, and the fences must be longer than their longest backtick spans.
  # shellcheck disable=SC2016 # literal backticks in the ref names
  run_case_step repeat.sh 0 'GITHUB_REF=refs/heads/a`b' 'COMPARE_REF=c``d`'
  # shellcheck disable=SC2016 # literal backticks from the summary's Markdown
  says "$work/case/summary" '`` refs/heads/a`b ``'
  # shellcheck disable=SC2016 # literal backticks from the summary's Markdown
  says "$work/case/summary" '``` c``d` ```'
}

legacy_environment() {
  # Read the process environment before a shell interpreter can reinitialize OLDPWD.
  mkdir -p "$work/legacy-bin"
  cat > "$work/legacy-bin/dotnet" <<'EOF_DOTNET'
#!/usr/bin/env python3
import os
import sys

with open(os.environ['STUB_OLDPWD'], 'w') as captured:
    captured.write(os.environ.get('OLDPWD', 'unset') + '\n')
os.execv(os.environ['STUB_DOTNET'], sys.argv)
EOF_DOTNET
  chmod +x "$work/legacy-bin/dotnet"
  # shellcheck disable=SC2016 # the step shell expands this value at startup
  printf '%s\n' 'export OLDPWD="$LEGACY_OLDPWD"' > "$work/legacy-env"
  start_case 'pass'
  run_case_step repeat.sh 0 PATH="$work/legacy-bin:$work/bin:$PATH" \
    BASH_ENV="$work/legacy-env" LEGACY_OLDPWD="$work" \
    STUB_DOTNET="$work/bin/dotnet" STUB_OLDPWD="$work/case/oldpwd"
  [ "$(cat "$work/case/oldpwd")" = "$work" ] || fail 'primary changed the inherited OLDPWD'
}

comparison_event_gate() {
  start_case ''
  run_case_step compare-check.sh 1 COMPARE_REF=baseline GITHUB_EVENT_NAME="$1"
  says "$work/case/out" 'compare-ref requires a workflow_dispatch caller'
  says "$stress/progress.txt" 'primary ref: refs/heads/primary'
  says "$stress/progress.txt" 'compare ref: baseline'
  says "$stress/progress.txt" 'comparison validation: exit 1'
  run_case_step check.sh 0 GITHUB_EVENT_NAME="$1"
}

comparison_short_tag() {
  start_compare ''
  run_case_step check.sh 1 COMPARE_REF=tag
  says "$work/case/out" 'fully qualified ref'
  says "$stress/progress.txt" 'input validation: exit 1'
}

comparison_input_failure() {
  start_compare ''
  run_case_step check.sh 1 COMPARE_REF=baseline PROJECT=Missing.csproj
  says "$stress/progress.txt" 'input validation: exit 1'
}

comparison_worktree_failure() {
  start_compare ''
  mkdir "$work/case/temp/stress-compare"
  : > "$work/case/temp/stress-compare/occupied"
  run_case_step checkout.sh 128 COMPARE_REF=baseline
  says "$stress/progress.txt" 'fetch: exit 0'
  says "$stress/progress.txt" 'peel: exit 0'
  says "$stress/progress.txt" "$compare_commit"
  says "$stress/progress.txt" 'worktree: exit 128'
}

comparison_bad_ref() {
  start_case ''
  mkdir -p "$stress"
  run_case_step check.sh 1 COMPARE_REF="$1"
  says "$work/case/out" '::error::compare-ref'
}

comparison_bad_path() {
  start_case ''
  mkdir -p "$stress"
  run_case_step check.sh 1 COMPARE_REF=baseline "$1"
  says "$work/case/out" '::error::compare-ref requires checkout-relative'
}

comparison_conflict() {
  start_case ''
  mkdir -p "$stress"
  run_case_step check.sh 0 COMPARE_REF=baseline
  run_case_step check.sh 0 SUPPORT_COST=true
  run_case_step check.sh 1 COMPARE_REF=baseline SUPPORT_COST=true
  says "$work/case/out" '::error::support-cost cannot be used with compare-ref'
}

comparison_bad_checkout() {
  start_compare ''
  run_case_step checkout.sh 1 COMPARE_REF="$1"
  says "$work/case/out" "::error::$2"
  says "$stress/progress.txt" "$3: exit 1"
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
compare_case comparison_checkout refs/heads/tag
compare_case comparison_lfs_skip
for tool in usr/local/lib/android opt/hostedtoolcache/CodeQL usr/local/.ghcup usr/share/swift; do
  compare_case comparison_disk_removes_tool "$tool"
done
compare_case comparison_disk_skips_missing
compare_case comparison_disk_removal_failure
compare_case comparison_disk_evidence shared
compare_case comparison_disk_evidence split
compare_case comparison_disk_floor workspace
compare_case comparison_disk_floor runner-temp
compare_case legacy_disk
compare_case comparison_build_failure primary
compare_case comparison_build_failure compare
compare_case comparison_paths Inside.csproj Inside.csproj 0
compare_case comparison_paths Outside.csproj '' 1
compare_case comparison_paths Stub.csproj OutsideBuild 1
compare_case comparison_alternates
for side in primary compare; do
  compare_case comparison_verdict "$side" fail:3 '0 passed, 1 failed, 0 selected nothing'
  compare_case comparison_verdict "$side" empty '0 passed, 0 failed, 1 selected nothing'
done
compare_case comparison_summary
compare_case legacy_environment
compare_case comparison_event_gate pull_request_target
compare_case comparison_event_gate workflow_run
compare_case comparison_short_tag
compare_case comparison_input_failure
compare_case comparison_worktree_failure
compare_case comparison_bad_ref '   '
compare_case comparison_bad_ref -baseline
compare_case comparison_bad_ref 'baseline:refs/heads/unwanted'
compare_case comparison_bad_path "PROJECT=$work/repo/Stub.csproj"
compare_case comparison_bad_path BUILD_TARGET=../Stub.csproj
compare_case comparison_conflict
compare_case comparison_bad_checkout non-commit 'compare-ref must resolve to a commit' peel
compare_case comparison_bad_checkout missing-project 'project Stub.csproj does not exist on the comparison ref' 'compare project'
compare_case comparison_bad_checkout missing-ref 'compare-ref could not be fetched' fetch

[ "$wiring_failed" -eq 0 ] && [ "$compare_failures" -eq 0 ] || exit 1
echo "stress-test-workflow tests: $pass passed"
