# ci-workflows

Shared reusable GitHub Actions workflows. One implementation lives here and each repository calls it with
a small caller, so a fix lands once. The stress workflow serves APKiwiOrg repositories. The site workflows
and the osv-scan report serve the owner's private site repositories, each one made from or synced with
APKiwiOrg/site-template.

Every workflow here runs on a GitHub-hosted runner, never a self-hosted one. The org's self-hosted runners
are the dev Mac, and this repository is public.

Because it is public, this repository holds generic steps only, with no site or client names, URLs, identifiers,
secrets or site code. Secrets live in each private site repository and are passed by name at run time.

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
  -f filter='FullyQualifiedName=<Namespace>.<Class>.<Method>&DisplayName~<unit>' \
  -f iterations=1 -f load=0 -f configuration=Release -f artifact-env=<CALLER_REPORTS_VARIABLE>
```

The input only says where output goes. It implies no permission to run a workload, authorizes no manifest,
and changes no caller's approval rules. Choosing what runs, and getting the owner's permission for any
timing or load run, stays with the caller.

Quote the whole filter for the shell, without embedded quotes around a simple unit name. Embedded quotes
can be stripped while dotnet forwards the filter to the test adapter, leaving an invalid condition and no
selected tests. Check the retained TRX count before treating a run as evidence.

## site-ci

Runs a site's CI. `config` validates the site config before any install. `checks` lints, typechecks, tests
and builds, then holds the build to the asset budget. `audit` installs and audits the root and any api package.
`repo` runs the repository convention checks, the site template drift check and the shell script tests.
`secrets` scans every commit with a checksum-verified gitleaks. `infra` compiles the Bicep template and
parameters and checks the infrastructure policy. `e2e` builds and runs the browser smoke after `checks`
passes. Every checkout drops its credentials.

A site adds this caller as `.github/workflows/ci.yml`, pinned to a commit of this repository. `site-deploy`
accepts only successful main-push runs of a workflow in that file named `CI`:

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  ci:
    uses: APKiwiOrg/ci-workflows/.github/workflows/site-ci.yml@<full commit sha>
```

GitHub names a called job `<caller job> / <job name>`, for example `ci / Lint, typecheck, test and build`, so
a ruleset that requires checks by their old names needs updating when a site moves to this caller.

Optional inputs and their defaults are `node-version-file` (`.nvmrc`), `analytics-test-token`
(`00000000000000000000000000000000`) and `bicep-file` (`infra/main.bicep`). The config job always uses
`.nvmrc`. The version input applies to later jobs. Builds and tests receive the fixture token as
`VITE_CLOUDFLARE_ANALYTICS_TOKEN` only when analytics is `cloudflare`. The registered site token never
reaches CI.

Both site workflows call `site-config.yml` through `./.github/workflows/site-config.yml`. GitHub resolves
this local reusable workflow from the same commit as its calling workflow, so validation stays on the
site caller's existing pin. Its checkout reads the site repository. The optional `checkout-ref` input
selects the source commit to read, without changing the validation implementation's ref.

The shared config job runs `node scripts/site-config.mjs --github-output` once from that checkout before
any install. The CLI validates the config and appends these five `key=value` lines to `$GITHUB_OUTPUT`:

| Key | Required value | Use |
| --- | --- | --- |
| `app-location` | `build/client` or `out` | Asset budget, artifact upload and deployment folder |
| `has-form` | `true` or `false` | Exposed capability, independent of the api directory |
| `has-theme` | `true` or `false` | Run the theme check only when true |
| `analytics` | `cloudflare` or `none` | Pass a build token only for cloudflare |
| `infra-params` | Compact JSON array such as `["infra/params/site.bicepparam"]` | Compile every params file |

The workflows reject a missing CLI, a failed CLI, missing or duplicate keys and invalid values. The params
array must be nonempty and contain repo-relative `.bicepparam` paths without empty, `.` or `..` segments.
Absolute paths, backslashes and control characters are refused. Every listed params file must exist.
There are no default capabilities or output folders.

This is a breaking caller contract. The `app-location`, `has-api`, `has-e2e`, `has-infra` and `bicep-params`
inputs have been removed. The deploy workflow also removes `api-location`, since the checkout determines
whether `api/` exists. Existing callers keep their old SHA pin until the template provides the CLI contract,
removes those inputs and re-pins them in a later task. Update the template and its synced callers together.

The calling repository supplies `scripts/check-npm-policy.mjs` and `scripts/check-assets.mjs`, which
receives the absolute `app-location` folder after the build. The asset folder, Bicep template and params
file paths are resolved inside the checkout before CLI use. Paths and symlink targets outside the
checkout are refused. Names with spaces or leading hyphens remain single path arguments.
It also supplies the npm scripts `lint`, `typecheck`,
`test`, `build` and `test:e2e`, plus `check:theme` when `has-theme` is true, and the convention checks `scripts/check-dashes.sh`,
`scripts/check-prose.sh`, `scripts/check-scratch.sh` and `scripts/check-agent-instructions.sh`, each run with
`--tree`. A site made from APKiwiOrg/site-template carries `site-template.lock.json`, and `repo` then runs its
`scripts/check-site-template.mjs`, which fails when a template-managed file differs from the lock. A
repository with no lock prints one skip line instead. `repo` runs every `scripts/tests/*.test.sh` and fails
when there is none. When an `api/` directory exists, it is an npm package with `typecheck` and `test` scripts.
Every site supplies e2e and infrastructure checks. The infra job compiles every configured params file
with `--outfile` to a separate indexed deployment parameters JSON file in `/tmp/site-infra/`, a mode 0700
directory. The output must parse as JSON with a `parameters` object before the checker runs, since
`--stdout` produces a wrapper containing both parameters and template output. It then runs
`scripts/check-infra.mjs <compiled-template.json> <compiled-params.json>` once per params file, in config
order, including the bandwidth alert policy. The checker uses the compiled parameters to skip resources
that file disables. A missing params file, a compilation failure, an invalid output or a policy failure fails the job and
names the params file. A failed policy check stops later checks. The audit job runs the npm policy check
before `npm ci`, then `npm audit --audit-level=high` in the root and, when present, in `api/`.

## site-deploy

Deploys a site to Azure Static Web Apps, uploading only a SHA that a successful main-push CI run verified. An
automatic run deploys the newest such commit on main, so CI finishing out of order cannot leave production
behind. A manual run from main deploys the exact full SHA it is given, which is the rollback path. Nothing
runs unless `enabled` is true.

`guard` selects the SHA with a read-only token. `config` passes that SHA to the shared config workflow,
which checks out the site without persisted credentials and validates its config using `.nvmrc`.
Build and deploy require both jobs, so a rollback reads the selected commit's source and config while
validation stays at the shared workflow's pinned commit. `build` runs `npm ci` and the build command with no secret
and uploads the app folder. `deploy` is the only job in the `production` environment and the only one that
reads the deploy token. It installs nothing, uploads the built folder with the api folder for Oryx to build,
then smoke tests the URL the deploy returns.

A site adds this caller as `.github/workflows/deploy.yml`, pinned to a commit of this repository:

```yaml
name: Deploy
on:
  workflow_run: { workflows: [CI], branches: [main], types: [completed] }
  workflow_dispatch: { inputs: { sha: { type: string, required: false } } }
permissions: { actions: read, contents: read }
concurrency: { group: deploy-main, cancel-in-progress: false }
jobs:
  deploy:
    uses: APKiwiOrg/ci-workflows/.github/workflows/site-deploy.yml@<full commit sha>
    with:
      enabled: ${{ vars.AZURE_ENABLED == 'true' }}
      dispatch-sha: ${{ inputs.sha }}
      analytics-token: ${{ vars.CLOUDFLARE_ANALYTICS_TOKEN }}
    secrets:
      AZURE_STATIC_WEB_APPS_API_TOKEN: ${{ secrets.AZURE_STATIC_WEB_APPS_API_TOKEN }}
```

The caller owns the triggers and the `deploy-main` concurrency group. A called workflow that repeats its
caller's concurrency group deadlocks, so `site-deploy` sets none. The caller grants `actions: read` because a
called job can only narrow the caller's token, and the org default token has no actions read. YAML flow
mappings cannot hold a `${{ }}` expression, so `with` and `secrets` stay in block style.

Every caller must pass `AZURE_STATIC_WEB_APPS_API_TOKEN` by that name, as above. `secrets: inherit` does
not cross organisations, and the token in the caller's `production` environment takes precedence only over a
secret the caller passes by that name, because the deploy job declares the environment. The deploy job's
first step refuses a token that resolves empty. A caller outside APKiwiOrg also needs its organisation's
Actions policy to allow `APKiwiOrg/ci-workflows` reusable workflows, or the call never starts.

Redeploy or roll back to a green main SHA from main:

```bash
gh workflow run deploy.yml --ref main -f sha=<full green main sha>
```

The calling repository supplies `.github/workflows/ci.yml` named `CI`, whose successful main-push runs are
the verification, `scripts/check-npm-policy.mjs`, which runs before `npm ci`, and `scripts/smoke-site.mjs`,
which receives the deployed URL.

Optional inputs and their defaults are `dispatch-sha` (empty), `node-version-file` (`.nvmrc`),
`build-command` (`npm run build`) and `analytics-token` (empty). The required `enabled` input gates all work.
The build command runs as one bash command line after `npm ci`. Only `analytics: cloudflare` passes the
analytics token in `VITE_CLOUDFLARE_ANALYTICS_TOKEN`. With `none`, the build receives no analytics token.
It runs under `bash -euo pipefail`, so any failing command in it fails the build and nothing uploads.
The validated `app-location` selects the artifact and deploy folder. Oryx builds `api/` only when that
directory exists. The CLI contract and removed inputs are documented in the site-ci section above.

## site-live-throttle-check

Tests that a site's live enquiry throttle cannot be bypassed with a forged `X-Forwarded-For` header. Run it
after any change to how the site's api reads the client address, and before email delivery is ever switched
on. It has no secrets and no token permissions.

It first probes the delivery mode with a honeypot-only post, `{"notes_extra":"x"}`, which the handler answers
before the schema, timing and throttle checks without calling delivery. The run stops unless the probe answers
503 `enquiries-not-open`, so the valid posts only reach off mode and no email is sent. It then sends
`sender-limit` plus one valid synthetic enquiries, one plain and the rest each with a different forged
`X-Forwarded-For` from the `203.0.113.0/24` documentation range. Posts 1 to `sender-limit` must answer 503 and
the last post must answer 429 `rate-limited`. It prints status codes, never an address.

A site adds this caller as `.github/workflows/live-throttle-check.yml`, pinned to a commit of this repository.
`sender-limit` is the site's own per-sender limit, from 1 to 253, and the template's is 3:

```yaml
name: Live throttle check
on:
  workflow_dispatch:
    inputs:
      site-url:
        description: Site to check
        required: true
        type: string
permissions: {}
concurrency:
  group: live-throttle-check
  cancel-in-progress: false
jobs:
  check:
    uses: APKiwiOrg/ci-workflows/.github/workflows/site-live-throttle-check.yml@<full commit sha>
    with:
      site-url: ${{ inputs.site-url }}
      sender-limit: 3
```

The caller owns the concurrency group, so two runs never share the runner's throttle slots. A `default` on
`site-url` saves typing the hostname.

The optional boolean input `ask-name` defaults to `true`. For a site whose form asks for no name, add
`ask-name: false` under the caller's `with`. Every valid post then omits `name`, keeping `email`, the site's
fields and `elapsedMs` in that order. The honeypot-only probe is unchanged.

The optional string input `enquiry-fields` defaults to `{"needs":"Throttle check"}`. It supplies the site's
form fields on every valid post, replacing the default `needs` field when provided. For a form with `plan`
and optional `goals`, add this under the caller's `with` and choose values its schema accepts:

```yaml
      enquiry-fields: '{"plan":"Throttle check","goals":"Throttle check"}'
```

The input must be a JSON object with string values. It cannot contain `name`, `email`, `elapsedMs` or the
honeypot field `notes_extra`, even with `ask-name: false`. Invalid input stops before any request, including
the mode probe, without printing the supplied values. An empty object adds no site fields. The probe stays
honeypot-only because the handler answers it before schema validation, without calling delivery or spending
a throttle slot.

```bash
gh workflow run live-throttle-check.yml --ref main -f site-url=https://<site hostname>
```

Each run spends the runner's slots for the site's throttle window, so a rerun sooner from the same address
fails at post 1 with 429. A throttle held in one instance's memory also fails the run when that instance
recycles mid-run. Rerun after the window.

The check is meaningful only when `sender-limit` plus 1 is at most C, the smallest site-wide cap less any
enquiries the site already took in that cap's window. When `sender-limit` equals C, the site-wide cap answers
the last post's 429 and the run passes falsely, without testing the per-sender key. When `sender-limit` is
greater than C, the site-wide cap answers post C plus 1 with 429 and the run fails early with
`did not answer 503`. When C is 0 that is post 1, which reports a full bucket instead. The template's limit
of 3 with its cap of 4 a minute just fits. One run spends `sender-limit` slots of each site-wide cap for that
cap's window. The last post spends none, because the template throttle records a post only when every limit
allows it.

The calling repository supplies the template enquiry api at `/api/enquiry`. The default valid body
`{"name":"Throttle Check","email":"throttle-check@example.com","needs":"Throttle check","elapsedMs":5000}` fits
its schema, and `elapsedMs` clears the 3000 ms timing check. The handler checks the honeypot, then the schema,
then timing, then the throttle, and only then answers 503 `enquiries-not-open` in off mode, so the throttle's
429 still shows.

Sites with different form fields use `enquiry-fields` to match their schema and `ask-name: false` when no
name is accepted. The honeypot probe stays fixed.

## osv-scan

Reports known vulnerabilities in a repository's dependencies with osv-scanner, weekly and on demand. It is
report only and never a required check. Dependabot alerts stay the record. It has no inputs or secrets, and
its token reads contents only.

The job installs the pinned osv-scanner release binary after checking it against the pinned SHA-256, then
runs `osv-scanner scan source -r --allow-no-lockfiles` over the checkout twice, once for each format. The
markdown report goes into the job summary and the JSON report into the `osv-scanner` artifact, kept 14 days.
There is no code scanning upload, since the private site repositories lack it.

Exit 0 is a clean scan and exit 1 is findings. Both pass, and findings also raise a warning. Every other
exit is a scan error, which fails the job and names the exit in the summary. A failed scan still writes a
report that reads as clean, so that report is never published and nothing is uploaded. A repository with no
lockfile passes with no findings.

The report sits beside the `site-ci` audit job and does not replace it. That job fails CI on a high or
critical npm advisory, so the deploy never ships one. The weekly report also catches advisories published
after a commit landed, lower severities and lockfiles npm audit does not read, without blocking anything.

A site adds this caller as `.github/workflows/osv-scan.yml`, pinned to a commit of this repository. The
schedule lands the report early on Monday in New Zealand:

```yaml
name: OSV scan
on:
  schedule:
    - cron: '23 19 * * 0'
  workflow_dispatch:
permissions:
  contents: read
jobs:
  scan:
    uses: APKiwiOrg/ci-workflows/.github/workflows/osv-scan.yml@<full commit sha>
```

Run it by hand from a site, or from this repository to prove a change against its own checkout:

```bash
gh workflow run osv-scan.yml --ref main
```

osv-scanner reads `package-lock.json` and `uv.lock`, among others, and honours an `osv-scanner.toml` beside
a lockfile. Every `[[IgnoredVulns]]` entry there carries an `ignoreUntil` date and a `reason` that links the
dismissed Dependabot alert, so an ignore expires and the alert stays the record.

To move to a new osv-scanner release, update `OSV_SCANNER_VERSION` and `OSV_SCANNER_SHA256` together, taking
the `osv-scanner_linux_amd64` line from the release's `osv-scanner_SHA256SUMS`.
