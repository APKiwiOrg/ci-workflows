# ci-workflows

Shared reusable GitHub Actions workflows for every APKiwiOrg repository. One implementation lives here and
each repository calls it with a small caller, so a fix lands once.

Every workflow here runs on a GitHub-hosted runner, never a self-hosted one. The org's self-hosted runners
are the dev Mac, and this repository is public.

## stress-test

Repeats one test selection many times, optionally under synthetic CPU load, and fails when any run fails or
when the selection runs no tests. It is the only place flake hunts and stress proofs run. A load or stress
run needs the owner's explicit permission before it starts.

A repository adds this caller as `.github/workflows/stress-test.yml`, pinned to a commit of this repository:

```yaml
name: stress-test
on:
  workflow_dispatch:
    inputs:
      project:
        description: Test project to build and run (path to its .csproj)
        required: true
        type: string
      filter:
        description: dotnet test --filter expression selecting the tests to repeat
        required: true
        type: string
      iterations:
        description: How many times to run the selection, 1 to 200
        default: '20'
        type: string
      load:
        description: Busy-loop processes kept running during every run, 0 to 16
        default: '0'
        type: string
      configuration:
        description: Build configuration
        default: Release
        type: choice
        options: [Release, Debug]
      support-cost:
        description: Record fixed-compilation support cost artifacts, Release and no load only
        default: false
        type: boolean
      build-target:
        description: Solution or project to build first, empty for the default
        default: ''
        type: string
      artifact-env:
        description: Environment variable naming each run's retained directory, empty for none
        default: ''
        type: string
permissions:
  contents: read
jobs:
  stress:
    uses: APKiwiOrg/ci-workflows/.github/workflows/stress-test.yml@<full commit sha>
    with:
      project: ${{ inputs.project }}
      filter: ${{ inputs.filter }}
      iterations: ${{ inputs.iterations }}
      load: ${{ inputs.load }}
      configuration: ${{ inputs.configuration }}
      support-cost: ${{ inputs.support-cost }}
      build-target: ${{ inputs.build-target }}
      artifact-env: ${{ inputs.artifact-env }}
```

A caller whose `nuget.config` restores from GitHub Packages also passes
`secrets: { packages-token: ${{ secrets.GITHUB_TOKEN }} }`.

Run it from a pushed branch:

```bash
gh workflow run stress-test.yml --ref <branch> \
  -f project=<Tests project>.csproj -f filter="FullyQualifiedName~<Name>" -f iterations=50 -f load=4
```

The summary reports passed, failed and selected-nothing counts. Failing runs upload their logs and results as
`stress-test-results` for seven days. It bills GitHub-hosted Linux minutes at 1x, so a 50-run proof of a ten
second test costs about ten minutes.

It builds the caller's single root `.slnx` or `.sln` before the runs, so suites that guard production
assemblies pass, and falls back to the test project when the root holds no single solution. The optional
`build-target` input overrides both.

The workflow evicts stale vendored engine packages when the caller has `scripts/evict-stale-vendored.sh`, and
creates `local-feed` when the caller's `nuget.config` names it.

The optional `support-cost` boolean defaults to false. When true, configuration must be Release and load
must be zero. Each iteration sets `DOTNET_TieredCompilation=0` and writes to a separate
`KHAOZ_SUPPORT_COST_ARTIFACT` path under the runner's temporary `stress/support-cost/run-N.json` directory.
The caller supplies `scripts/record-support-cost-exit.py` to finalize each JSON file with the actual test
process exit. Missing artifacts or failed finalization fail the iteration. Successful logs and TRX files
are still removed, while failing runs keep both. The `support-cost` upload retains all cost files for seven
days on success or failure. Committed measurement artifacts are never output paths.

The caller owns calibration and normalized verdicts. KhaozEngine uses its committed M2 Max baseline as
read-only input and the legacy flat-box tick from the same hosted invocation. A reusable-workflow change
is proven by one caller run with one iteration, no load and Release before keeping the new caller pin.
Repeated timing still requires the owner's explicit approval.

The optional `artifact-env` string defaults to empty, which changes nothing above. When set, it names an
environment variable the workflow gives each run's test process, pointing at that run's own directory
`stress/artifacts/run-N` under the runner's temporary directory. Whatever the tests write there, for example
one JSON file per finished condition, survives a later failure, crash or timeout in the same run. Every
run's `run-N.log` and `run-N.trx` are kept, passing runs included. Each log starts with the directory it was
given and ends with the actual `dotnet test` exit, unless the step was killed first. The whole `stress`
tree uploads as `stress-artifacts` for seven days on success, failure or cancellation, with a warning
rather than an error when it holds no files. Empty directories do not upload. The `stress-test-results`
failure upload stands down in this mode because `stress-artifacts` already holds the same files. A
`support-cost` run still uploads its own `support-cost` artifact.

The name must be an ASCII shell identifier that is not already set in the runner environment. Shell and
process control names (`PATH`, `HOME`, `IFS`, `CDPATH`, `ENV`, `SHELLOPTS`, `BASHOPTS`, `TMPDIR`, `LANG`,
`TZ`, `CI`, the proxy variables and similar), the workflow's own variables, and the `BASH_`, `GITHUB_`,
`RUNNER_`, `ACTIONS_`, `LD_`, `DYLD_`, `LC_`, `DOTNET_`, `COMPLUS_`, `CORECLR_`, `NUGET_`, `MSBUILD`,
`VSTEST` and `PYTHON` prefixes are refused in any letter case. The value reaches the test process as one
quoted assignment, never through `eval` or a shell export.

In artifact mode the repeat step stops after 60 minutes instead of running to the 90-minute job limit, so
a selection that overruns still leaves job time to upload what it wrote. That is nominal headroom only. It
does not promise that checkout, restore, the build and the runs fit, and a job cancelled at its 90-minute
limit may upload nothing. A caller with its own deadline, such as a per-unit budget in a measurement
manifest, keeps that deadline cooperative and well inside the step limit. The hard limits are the step's 60
minutes and the job's 90.

A cost or measurement run builds only what it needs and keeps its evidence, for example:

```bash
gh workflow run stress-test.yml --ref <branch> \
  -f project=<Cost tests>.csproj -f build-target=<Cost tests>.csproj \
  -f filter='FullyQualifiedName=<Namespace>.<Class>.<Method>&DisplayName~"<unit>"' \
  -f iterations=1 -f load=0 -f configuration=Release -f artifact-env=<CALLER_REPORTS_VARIABLE>
```

The input only says where output goes. It implies no permission to run a workload, authorizes no manifest,
and changes no caller's approval rules. Choosing what runs, and getting the owner's permission for any
timing or load run, stays with the caller.
