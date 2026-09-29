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
