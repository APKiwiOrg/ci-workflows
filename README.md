# ci-workflows

Shared reusable GitHub Actions workflows and composite actions. One implementation lives here and each
repository calls it with a small caller, so a fix lands once. The stress workflow serves APKiwiOrg
repositories. The site workflows and the osv-scan report serve the owner's private site repositories, each
one made from or synced with APKiwiOrg/site-template.

Every reusable workflow here runs on a GitHub-hosted runner, never a self-hosted one. The org's self-hosted
runners are the dev Mac, and this repository is public. An action runs on its caller's runner. The
[actions](#actions) give any repository's CI the [stage names](#stage-names) below, one verdict and one
summary.

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

The calling site owns the triggers, scope and final `CI passed` job. Keep its workflow file
`.github/workflows/ci.yml` and name `CI` if it calls the shared deploy workflow. The deploy guard
uses both to identify successful main-push runs.

Call `APKiwiOrg/ci-workflows/.github/workflows/site-ci.yml@<full commit sha>` with `contents: read`.
Pass `full` (boolean, default true) and `segments` (JSON array string, default `[]`) from the caller's
scope decision. A full run selects every applicable stage. A partial run selects the stages below.
An empty partial selection still validates config, checks repository conventions and scans commit history.

| Segment | Selected work |
| --- | --- |
| `site` | Build, asset budget, lint, types, unit tests, theme policy and Playwright smoke |
| `sql` | SQL and blob integration tests when the site has accounts |
| `infra` | Bicep template, params and infrastructure policy |

Full runs also audit dependencies. The Node stages share a job with one install per required package
and one frontend build. Playwright uses that build in the same workspace after the core checks pass.
Config validation runs before any install and the workflow exposes the validated `has-accounts` output
for the caller's summary. Every checkout drops its credentials. Give the final caller job the outcome
of this reusable call and run the [summary action](#ci-finish) there to keep one verdict.

The three operational inputs are `node-version-file` (default `.nvmrc`), `analytics-test-token`
(default `00000000000000000000000000000000`) and `bicep-file` (default `infra/main.bicep`).
The config job always uses `.nvmrc`. The version input applies to the other Node jobs. Cloudflare
builds and tests receive the fixture analytics token. Builds for other modes have no analytics token.

Both site workflows call `site-config.yml` through `./.github/workflows/site-config.yml`. GitHub resolves
this local reusable workflow from the same commit as its calling workflow, so validation stays on the
site caller's existing pin. Its checkout reads the site repository. The optional `checkout-ref` input
selects the source commit to read, without changing the validation implementation's ref.

The shared config job runs `node scripts/site-config.mjs --github-output` once from that checkout before
any install. The CLI validates the config and appends these six `key=value` lines to `$GITHUB_OUTPUT`:

| Key | Required value | Use |
| --- | --- | --- |
| `app-location` | `build/client` or `out` | Asset budget, artifact upload and deployment folder |
| `has-form` | `true` or `false` | Exposed capability, independent of the api directory |
| `has-theme` | `true` or `false` | Run the theme check only when true |
| `has-accounts` | `true` or `false` | Run the SQL integration stage only when true |
| `analytics` | `cloudflare` or `none` | Pass a build token only for cloudflare |
| `infra-params` | Compact JSON array such as `["infra/params/site.bicepparam"]` | Compile every params file |

The workflows reject a missing CLI, a failed CLI, missing or duplicate keys and invalid values. The params
array must be nonempty and contain repo-relative `.bicepparam` paths without empty, `.` or `..` segments.
Absolute paths, backslashes and control characters are refused. Every listed params file must exist.
There are no default capabilities or output folders.

The SQL stage starts a digest-pinned SQL Server 2022 container on the hosted runner with a random, masked
SA password. It tries up to 30 logins, two seconds apart, and fails if SQL never answers. After a login
succeeds, it publishes an encrypted, local `ACCOUNTS_SQL_TEST_SERVER` connection string through
`GITHUB_ENV`. It then starts a digest-pinned Azurite storage emulator running only the blob service on
`127.0.0.1:10000`, with the API version check off because the storage SDK can send a newer version than the
emulator knows. It probes the blob endpoint up to 30 times, two seconds apart, treats any HTTP status as
ready and fails if nothing answers. After the shared npm policy check and API install, it runs
`npm run test:sql --prefix api` with that connection string and `MEDIA_BLOB_TEST_CONNECTION` set to the
SDK's emulator shorthand `UseDevelopmentStorage=true` in the test environment, so no emulator key appears in
the workflow. The blob integration tests use that variable. Accounts sites must supply this api script. Sites
with `has-accounts` set to `false` skip this stage.

Site config supplies `app-location` and `infra-params`. API presence comes from the checkout.
The former `app-location`, `has-api`, `has-e2e`, `has-infra` and `bicep-params` inputs are unsupported.
The deploy workflow likewise determines `api-location` from the checkout.

The calling repository supplies `scripts/check-npm-policy.mjs` and `scripts/check-assets.mjs`, which
receives the absolute `app-location` folder after the build. The asset folder, Bicep template and params
file paths are resolved inside the checkout before CLI use. Paths and symlink targets outside the
checkout are refused. Names with spaces or leading hyphens remain single path arguments.
It also supplies the npm scripts `lint`, `typecheck`,
`test`, `build` and `test:e2e`, plus `check:theme` when `has-theme` is true, and the convention checks `scripts/check-dashes.sh`,
`scripts/check-prose.sh`, `scripts/check-scratch.sh` and `scripts/check-agent-instructions.sh`, each run with
`--tree`. A synced site carries `site-template.lock.json`, and the repo stage then runs its
`scripts/check-site-template.mjs`, which fails when a template-managed file differs from the lock. A
repository with no lock prints one skip line instead. The repo stage runs every `scripts/tests/*.test.sh` and fails
when there is none. When an `api/` directory exists, it is an npm package with `typecheck` and `test` scripts.
Every site supplies e2e and infrastructure checks. The infra job compiles every configured params file
with `--outfile` to a separate indexed deployment parameters JSON file in `/tmp/site-infra/`, a mode 0700
directory. The output must parse as JSON with a `parameters` object before the checker runs, since
`--stdout` produces a wrapper containing both parameters and template output. It then runs
`scripts/check-infra.mjs <compiled-template.json> <compiled-params.json>` once per params file, in config
order, including the bandwidth alert policy. The checker uses the compiled parameters to skip resources
that file disables. A missing params file, a compilation failure, an invalid output or a policy failure fails the job and
names the params file. A failed policy check stops later checks. After the shared install, the audit stage
runs two audits in the root and, when present, in `api/`: `npm audit --omit=dev
--audit-level=high` for production dependencies and `npm audit --audit-level=critical` for every dependency. A high
advisory in dev tooling alone reaches no deployed code, so it is left to the weekly osv-scanner report rather than
failing every site, while a critical one, malware included, fails wherever it sits.

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

## site-maintenance

Runs the daily maintenance of a site with member accounts: retention purges, file retries, photo rechecks and the
moderation digest. The work lives in the site's api behind a keyed `POST /api/maintenance` route, so this workflow
only calls it. Its token reads contents only.

The job checks out the calling repository and runs its `scripts/run-maintenance.mjs` with Node from `.nvmrc`, as
`site-deploy` runs the site's smoke script, so the call protocol ships with the api it calls. Nothing is installed.
The script repeats the call until the site reports every task done, up to 12 calls, and retries a 404 or 5xx after
10 seconds while the api starts or migrates. It fails the run when the finished call reports a deletion queued or a
photo pending for more than a day, when no call finishes, or when the route refuses the key or is unset, so GitHub
emails the repository owner. It prints the counts each call reports, never the key or any other part of a response.

While the `site-origin` input or the `maintenance-key` secret is empty, the job skips with a notice and passes. A
template repository and a site before its owner gate therefore stay green. A secret cannot appear in a job
condition, so the first step makes that decision and the later steps read its output.

A site adds this caller as `.github/workflows/maintenance.yml`, pinned to a commit of this repository. The
schedule lands early in the morning in New Zealand:

```yaml
name: Maintenance
on:
  schedule:
    - cron: '17 14 * * *'
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: maintenance
  cancel-in-progress: false
jobs:
  maintain:
    uses: APKiwiOrg/ci-workflows/.github/workflows/site-maintenance.yml@<full commit sha>
    with:
      site-origin: ${{ vars.SITE_ORIGIN }}
    secrets:
      maintenance-key: ${{ secrets.MAINTENANCE_KEY }}
```

The caller owns the `maintenance` concurrency group, so a manual run never overlaps the scheduled one and the site's
once-a-day digest cannot go twice. `SITE_ORIGIN` is a repository variable holding the site's https origin, and
`MAINTENANCE_KEY` is a repository secret holding the same value as the site's `maintenance-key` Key Vault secret.
Every caller passes the key by that name, since `secrets: inherit` does not cross organisations.

Run it by hand from a site:

```bash
gh workflow run maintenance.yml --ref main
```

## Stage names

A CI workflow names each job, or each step of a job, with one of these stage names, exactly as written. The
summary that `ci-finish` writes has one row per stage. A job whose name is not a stage shows
its stage-named steps as rows, and a job with neither shows as one `Other: NAME` row. Job ids are lowercase
with hyphens, for example `test-unit` for the job named `Test: unit`.

| Stage | Purpose |
| --- | --- |
| `Scope` | Decides which test segments the change needs, with `ci-scope`. |
| `Build` | Compiles the code without running tests. |
| `Build and test` | Builds and runs every test in one job, when the two cannot be split. |
| `Test` | Runs every test in one job. |
| `Test: SEGMENT` | Runs one test segment named in the rules file, for example `Test: unit` or `Test: api`. |
| `Package` | Produces the release artifact. |
| `Package: TARGET` | Produces the package for one target, for example `Package: linux-x64`. |
| `Deploy: ENVIRONMENT` | Deploys to one environment, for example `Deploy: staging`. |
| `CI passed` | The last job. Gives the run its one verdict and summary with `ci-finish`. |

`CI passed` is the one status check to require in branch protection. It passes only when every job in its
`needs` passed or was skipped as allowed, and every other job of the run has finished without failing, so the
required check never changes when jobs are added, renamed or split. A `Test: SEGMENT` job may skip only on a partial run, when its
segment was not selected, and a job that does not test may skip when it is listed as always allowed to.

## Actions

Three composite actions under `.github/actions/`. A caller pins each one to a commit of this repository, the
same way it pins a reusable workflow:

```yaml
uses: APKiwiOrg/ci-workflows/.github/actions/ci-scope@<full commit sha>
```

Each action runs a standard library Python script with `python3` 3.8 or later under `bash`. They need
`python3` on the runner's `PATH` under `bash`, which GitHub-hosted Linux and macOS runners have, and a Windows
job sets Python up first. A job in a container needs both in its image. Inputs reach the scripts through
`env` only.

### ci-scope

Decides which test segments a run needs from the files its change touched and a rules file.

A push to a branch compares with the last passing run, not with the commit before the push. It reads one
page of the newest completed runs of the same workflow on the branch from the Actions API, counting only runs
on `push`, `schedule` or `workflow_dispatch` in this repository, and walks them from newest to oldest. The
first that passed and whose commit is an ancestor of `HEAD` is the base. So a push never selects less than
every change since the last run that passed: the segments a failed or cancelled push run touched stay
selected until a run passes. A `schedule` or `workflow_dispatch` run is full by its event, so when it fails
no change in the diff explains it. When such a run failed or timed out after the base run started, the push is
a full run until a run passes. The reason names the base, for example
`3 files changed since the last passing run (1a2b3c4). Segments: unit`. A pull request compares three-dot
with its base, so only the pull request's own changes count.

| Input | Default | Meaning |
| --- | --- | --- |
| `rules` | `.github/ci-scope.txt` | Path to the rules file, relative to the workspace. |
| `token` | `${{ github.token }}` | Token that can read the workflow's runs. |

| Output | Meaning |
| --- | --- |
| `full` | `true` when every test job runs, whatever `segments` holds. `false` when only the listed segments run. |
| `segments` | JSON list of segment names, sorted. On a full run, every segment the rules name, which may be none. |
| `reason` | One line saying why the run is full or which segments it needs. |
| `changed` | How many files the change touched, empty when the run went full before comparing. |

On a full run every test job runs, so callers gate each test job on
`needs.scope.outputs.full == 'true' || contains(fromJSON(needs.scope.outputs.segments), 'NAME')`. `segments`
is empty on a full run when the rules file is missing or names no segment, and a gate on `segments` alone
would then skip every test.

The calling job needs `actions: read` and `contents: read`, and checks the repository out with
`fetch-depth: 0` first. The last passing run's commit and a pull request's merge base are only in a full
clone, so without full history every run is a full run.

The rules file holds one `NAME: GLOB GLOB ...` line per segment. `full` and `ignore` are reserved names, and
the same names in other letter cases are malformed. Blank lines and lines starting with `#` are skipped, and a
name given twice adds globs. Globs match the whole repository-relative path: `*` matches within one path
segment, `?` matches one character, `**` as a whole segment matches any number of segments, and a glob with no
`/` matches the file name at any depth. A glob that starts or ends with `/` or holds `//` can never match, so it
is malformed.

```text
full: global.json
unit: src/** tests/unit/**
api: src/Api/** tests/api/**
ignore: docs/** **/*.md
```

A changed file selects every segment whose globs match it. The run is full, with the reason in `reason`, on
a schedule, a manual dispatch or a tag, when the rules file is missing or has a malformed line, when there is
no base commit to compare (no passing run on the branch yet, an API error, or a base the clone cannot
fetch), when a schedule or dispatch run failed after the base run started, when the rules file or the workflow file
itself changed, when a changed file matches `full`, and when a changed file matches no line at all. That last
rule means a new folder is never silently untested. The rules file and workflow file rules mean new rules,
pins or gates never decide how much of their own change is tested, whatever segment a glob puts them in. A file that matches only `ignore`
needs no test. Only `push` and `pull_request` events compare, and every other event is a full run.

```yaml
- uses: actions/checkout@<full commit sha>
  with:
    fetch-depth: 0
    persist-credentials: false
- id: scope
  uses: APKiwiOrg/ci-workflows/.github/actions/ci-scope@<full commit sha>
```

### ci-tested-tree

Says whether the checked-out git tree already passed a full run of the CI workflow, so a release or deploy
workflow can skip tests that already passed on the same code. The tree is `git rev-parse HEAD^{tree}`, so a
commit counts as tested when its content matches a commit that passed, whatever its history.

| Input | Default | Meaning |
| --- | --- | --- |
| `workflow` | `.github/workflows/ci.yml` | Path of the workflow file whose full passing run counts. |
| `token` | `${{ github.token }}` | Token that can read the repository's Actions artifacts and runs. |

| Output | Meaning |
| --- | --- |
| `reuse` | `true` when this tree already passed a full run of the workflow, `false` otherwise. |
| `tree` | The tree id of `HEAD`, empty when it cannot be read. |
| `reason` | One line saying why the tree is or is not reused. |
| `run-url` | Link to the run that passed, empty when `reuse` is false. |

The calling job needs `actions: read` and `contents: read`, and checks the repository out first. Any history
depth works.

It looks for the `ci-passed-full-TREE` artifact that `ci-finish` uploads. `reuse` is true only when that
artifact is unexpired and the run that made it concluded success, ran the `workflow` file, and belongs to
this repository with its head here too. The run's event must be `push`, `pull_request`, `schedule`,
`workflow_dispatch` or `merge_group`. A `pull_request_target` or `workflow_run` run executes in this
repository while the code it tests can come from a fork, so it never vouches for a tree. A run still going, a
failed run, a marker from another workflow file or repository, an expired marker and any API error each give
`reuse` false with the reason.

It also looks for `ci-failed-full-TREE`, which `ci-finish` uploads when a full run of the tree fails. When
such a marker is unexpired, its run passes the same origin and event checks, and it is as new as the pass or
newer, `reuse` is false and the reason links the failed run. The run's status does not matter: only a verdict
that did not pass uploads that marker, so a run still completing is failing. The code did not change, so that
failure is flakiness or a dependency outside the tree, and it is the newest evidence about the tree. Each
lookup reads one page of 100 markers, and more markers than that gives `reuse` false.

```yaml
- uses: actions/checkout@<full commit sha>
  with:
    persist-credentials: false
- id: tested
  uses: APKiwiOrg/ci-workflows/.github/actions/ci-tested-tree@<full commit sha>
  with:
    workflow: .github/workflows/ci.yml
- name: Test
  if: steps.tested.outputs.reuse != 'true'
  run: make test
```

### ci-finish

Gives a CI run one verdict and one summary, and after a full run marks the tree as passed or failed. It runs
as the last job, `CI passed`.

| Input | Default | Meaning |
| --- | --- | --- |
| `needs` | required | The calling job's `${{ toJSON(needs) }}`. |
| `allow-skipped` | empty | Job ids skipped when their segment is not selected, separated by spaces or commas. |
| `allow-skipped-always` | empty | Job ids that may be skipped on any run, separated by spaces or commas. |
| `full` | required | `true` when `ci-scope` made this a full run, so every segment job must run, `false` otherwise. Any other value fails the run. |
| `segments` | empty | The JSON list of segments `ci-scope` selected, used to explain skipped test segments. |
| `scope-reason` | empty | The reason `ci-scope` gave, shown on the `Scope` row. |
| `title` | empty | Summary heading. Empty means the repository's name followed by `CI`. |
| `job-name` | `CI passed` | Display name of the job that runs the action, so its row is the verdict and comes last. It must match, or the job counts itself as unfinished and the run fails. |
| `token` | `${{ github.token }}` | Token that can read the run's jobs and the repository's artifacts. |

It has no outputs. The calling job runs under `if: always()`, needs `actions: read` and `contents: read`, and
checks the repository out first, since the marker names the checked-out tree. Every job of the workflow that
does not itself wait for the verdict must be in its `needs`. A job that runs after the verdict, with `needs`
on the `CI passed` job, is not counted. The marker upload uses the runner's own artifact token and needs no further permission.

The verdict starts from `needs`. A job that succeeded passes. A failed or cancelled job fails the run, and so
does empty or malformed `needs` or a `full` value other than `true` or `false`. A skipped job fails the run
unless its id is listed:

- `allow-skipped` names the `Test: SEGMENT` jobs, which are skipped when their segment is not selected. On a
  full run every segment runs, so a skip of one of them fails the run, even when its id is in
  `allow-skipped-always` too.
- `allow-skipped-always` names jobs that do not test and may skip on any run, such as a deploy that runs
  only on the default branch.

The jobs API can then only turn a pass into a fail, never the reverse. A job of the run that has not
finished, or that failed, timed out or was cancelled, and that `needs` does not report, fails the run. That
catches a job missing from `needs` whether it already failed or is still going, and it means a job allowed to
fail (`continue-on-error`) does not belong in the workflow whose verdict this is. While `CI passed` runs, the
jobs API does not list a job that needs it, so such a job never counts. An API error leaves the verdict from
`needs` unchanged and withholds the pass marker, since a job missing from `needs` could have failed unseen. The APIs otherwise supply the
summary's names, times and its `Full suite last passed` footer, which counts only markers from runs of this
repository's own code. Text from git or the API is escaped in the summary, so a file or job name cannot
render as a link or other Markdown.

The action writes the summary first, then uploads a marker artifact holding the tree and the run's link, kept
30 days, then fails the job when the run did not pass. A failed run still shows its summary. A full run that
passed, with the jobs API read, uploads `ci-passed-full-TREE`. A full run where a job in `needs` reports `failure` uploads
`ci-failed-full-TREE`, so `ci-tested-tree` does not reuse an older pass of a tree whose newest full run
failed. A run whose only problems are cancelled or wrongly skipped jobs, and a partial run, upload no
marker. A job inside a reusable workflow is matched as `CALLER / NAME` when `job-name` is set to its name.

```yaml
- uses: actions/checkout@<full commit sha>
  with:
    persist-credentials: false
- uses: APKiwiOrg/ci-workflows/.github/actions/ci-finish@<full commit sha>
  with:
    needs: ${{ toJSON(needs) }}
    allow-skipped: test-unit test-api
    full: ${{ needs.scope.outputs.full }}
    segments: ${{ needs.scope.outputs.segments }}
    scope-reason: ${{ needs.scope.outputs.reason }}
```

### Example workflow

A complete `.github/workflows/ci.yml` for `example-app` with one test segment, `unit`. This repository's own
`check.yml` has the same shape with two segments.

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name }}-${{ github.event_name == 'pull_request' && github.ref || github.sha }}
  cancel-in-progress: true
permissions:
  contents: read
jobs:
  scope:
    name: Scope
    runs-on: ubuntu-26.04
    timeout-minutes: 5
    permissions:
      actions: read
      contents: read
    outputs:
      full: ${{ steps.scope.outputs.full }}
      segments: ${{ steps.scope.outputs.segments }}
      reason: ${{ steps.scope.outputs.reason }}
    steps:
      - uses: actions/checkout@<full commit sha>
        with:
          fetch-depth: 0
          persist-credentials: false
      - id: scope
        uses: APKiwiOrg/ci-workflows/.github/actions/ci-scope@<full commit sha>

  test-unit:
    name: 'Test: unit'
    needs: scope
    if: needs.scope.outputs.full == 'true' || contains(fromJSON(needs.scope.outputs.segments), 'unit')
    runs-on: ubuntu-26.04
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@<full commit sha>
        with:
          persist-credentials: false
      - run: make test-unit

  ci-passed:
    name: CI passed
    needs: [scope, test-unit]
    if: always()
    runs-on: ubuntu-26.04
    timeout-minutes: 5
    permissions:
      actions: read
      contents: read
    steps:
      - uses: actions/checkout@<full commit sha>
        with:
          persist-credentials: false
      - uses: APKiwiOrg/ci-workflows/.github/actions/ci-finish@<full commit sha>
        with:
          needs: ${{ toJSON(needs) }}
          # The Test: SEGMENT jobs. They may skip only when their segment is not selected.
          allow-skipped: test-unit
          # Jobs that do not test and skip by design, such as a deploy that runs only on main.
          allow-skipped-always: ''
          full: ${{ needs.scope.outputs.full }}
          segments: ${{ needs.scope.outputs.segments }}
          scope-reason: ${{ needs.scope.outputs.reason }}
```

The wiring that matters:

- `Scope` checks out with `fetch-depth: 0`, grants `actions: read` so a push can find its last passing run,
  and exposes `full`, `segments` and `reason` as job outputs.
- Each `Test: SEGMENT` job needs `scope` and runs when the run is full or its segment is in `segments`. On a
  full run `segments` can be empty, for example when the rules file is missing, so the `full == 'true'` half
  of the gate is what makes a full run test everything.
- `CI passed` needs every other job, runs under `if: always()` so it reports failures too, and grants
  `actions: read`. A job left out of `needs` fails the run when it failed or is still going as the verdict is
  given. `scope` is never listed as allowed to skip, so a failed `Scope` fails the run.
- `allow-skipped` lists the `Test: SEGMENT` jobs, which skip when their segment is not selected. A skip of
  one of them fails a full run.
- `allow-skipped-always` lists jobs that do not test and skip by design, such as a deploy that runs only on
  `main`. It is empty here because every job runs on every run.
- `CI passed` passes `full` from `Scope`, so only a full run leaves the marker that `ci-tested-tree` reads.
  The input is required, and an empty value, as when `Scope` failed, fails the run.
- A job allowed to fail does not belong in this workflow: `CI passed` fails when any job of the run fails,
  listed in `needs` or not.
- A manual dispatch is always a full run, so it needs no input to force one.
- A pull request's newer push cancels its older run, since the newer run covers the whole pull request.
  Every other run has a group of its own commit, so each push to `main` keeps its own verdict and marker,
  and no newer push cancels a run on `main` that is waiting to start.
