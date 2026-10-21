#!/bin/sh
# Exercise the stress workflow's own shell steps, extracted from the YAML, against a stub dotnet. Proves the
# input checks, log and TRX retention, per-run artifact directories and the step wiring without a build, a
# real test process, sleeps or load. Every case runs at most three stub iterations.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
pass=0

fail() {
  echo "FAIL: $*" >&2
  if [ -f "$work/case/out" ]; then sed 's/^/  | /' "$work/case/out" >&2; fi
  exit 1
}

# Extract the two step scripts and check the wiring that no shell run can see.
python3 - "$root/.github/workflows/stress-test.yml" "$work" <<'EOF'
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
job = workflow['jobs']['stress']
need(job['runs-on'] == 'ubuntu-26.04', 'job stays on ubuntu-26.04')
need(job['timeout-minutes'] == "${{ inputs.coverage-perf-cpu && (inputs.coverage-perf-preset == 'C0' && 20 || 120) || 90 }}",
     'job timeout stays 90 outside coverage CPU mode')
steps = {step.get('name'): step for step in job['steps']}
for step in job['steps']:
    need('${{' not in step.get('run', ''), 'no expression inside a run line: %s' % step.get('name'))
check = steps['Check the inputs']
repeat = steps['Repeat the selection']
need(repeat['env'].get('COVERAGE_PERF_PRESET') == '${{ inputs.coverage-perf-preset }}',
     'coverage timeout preset reaches the fixed test environment through env')
for step in (check, repeat):
    need(step['env'].get('ARTIFACT_ENV') == '${{ inputs.artifact-env }}', 'ARTIFACT_ENV reaches %s through env' % step['name'])
need(repeat.get('timeout-minutes') == "${{ inputs.coverage-perf-cpu && (inputs.coverage-perf-preset == 'C0' && 20 || 120) || (inputs.artifact-env != '' && 60 || 90) }}",
     'repeat step timeout stays 60 in artifact mode and 90 otherwise outside coverage CPU mode')
need('timeout-minutes' not in check, 'check step keeps no timeout of its own')
cost = steps['Upload support cost artifacts']
artifacts = steps.get('Upload the stress artifacts', {})
failing = steps['Upload the failing runs']
need(cost['if'] == 'always() && inputs.support-cost', 'support-cost upload condition unchanged')
need(cost['with'] == {'name': 'support-cost', 'path': '${{ runner.temp }}/stress/support-cost',
                      'retention-days': 7, 'if-no-files-found': 'error'}, 'support-cost upload unchanged')
need(artifacts.get('if') == "always() && inputs.artifact-env != ''", 'stress-artifacts upload runs always in artifact mode')
need(artifacts.get('uses') == failing['uses'] == cost['uses'], 'every upload uses the same pinned action')
need(artifacts.get('with') == {'name': 'stress-artifacts', 'path': '${{ runner.temp }}/stress',
                               'retention-days': 7, 'if-no-files-found': 'warn'}, 'stress-artifacts upload shape')
need(failing['if'] == "failure() && inputs.artifact-env == ''", 'failing-runs upload stands down in artifact mode')
need(failing['with'] == {'name': 'stress-test-results', 'path': '${{ runner.temp }}/stress',
                         'retention-days': 7, 'if-no-files-found': 'ignore'}, 'failing-runs upload unchanged')
if problems:
    for problem in problems:
        print('FAIL: ' + problem, file=sys.stderr)
    sys.exit(1)
open(out + '/check.sh', 'w').write(check['run'])
open(out + '/repeat.sh', 'w').write(repeat['run'])
EOF
pass=$((pass + 1))

# The stub reads one outcome per call from STUB_PLAN: pass, empty or fail:<exit>. It records what it saw of
# STUB_WATCH in STUB_SEEN and writes a condition file into that directory, as a caller's tests would.
mkdir -p "$work/bin" "$work/repo/scripts"
cat > "$work/bin/dotnet" <<'EOF'
#!/bin/sh
call=$(( $(cat "$STUB_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$call" > "$STUB_COUNT"
outcome=$(sed -n "${call}p" "$STUB_PLAN")
trx=""
results=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --logger) trx=${2#trx;LogFileName=}; shift ;;
    --results-directory) results=$2; shift ;;
  esac
  shift
done
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

# run_step <script> <expected exit> <plan> [NAME=value...]: a fresh runner temp, a clean environment and
# the step's own env defaults, overridden by the arguments.
run_step() {
  script=$1
  expected=$2
  plan=$3
  shift 3
  rm -rf "$work/case"
  mkdir -p "$work/case/temp"
  # shellcheck disable=SC2086 # the plan splits into one outcome per stub call
  printf '%s\n' $plan > "$work/case/plan"
  actual=0
  (cd "$work/repo" && env -i PATH="$work/bin:$PATH" HOME="$work/case" \
    RUNNER_TEMP="$work/case/temp" GITHUB_STEP_SUMMARY="$work/case/summary" \
    STUB_PLAN="$work/case/plan" STUB_COUNT="$work/case/count" STUB_SEEN="$work/case/seen" \
    STUB_WATCH=KE_COVERAGE_COST_REPORTS \
    PROJECT=Stub.csproj FILTER='FullyQualifiedName~Stub&Name!=x' ITERATIONS=1 LOAD=0 \
    CONFIGURATION=Release SUPPORT_COST=false ARTIFACT_ENV='' COVERAGE_PERF_CPU=false \
    COVERAGE_PERF_UNIT='' COVERAGE_PERF_MANIFEST_SHA256='' COVERAGE_PERF_WORKLOAD_SHA='' COVERAGE_PERF_PRESET='' "$@" \
    bash -e "$work/$script") > "$work/case/out" 2>&1 || actual=$?
  [ "$actual" -eq "$expected" ] || fail "$script $* exited $actual, expected $expected"
  pass=$((pass + 1))
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

echo "stress-test-workflow tests: $pass passed"
python3 "$root/scripts/tests/coverage-perf-cpu-workflow.test.py"
