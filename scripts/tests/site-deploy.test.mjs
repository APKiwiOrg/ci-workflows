// Exercises site-deploy.yml: the guard's admission expression, its shell steps against a stub gh, and the
// wiring that keeps the deploy token and the production environment in the deploy job only.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const folders = []
const sha = '1234567890abcdef1234567890abcdef12345678'
const newerSha = 'abcdef1234567890abcdef1234567890abcdef12'
const olderSha = '0987654321fedcba0987654321fedcba09876543'
const repository = 'APKiwiOrg/site'
const siteOrigin = 'https://site.example'
const selectStep = 'Select the green main SHA to deploy'
const snapshotStep = 'Capture catalog snapshot'
const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-deploy.yml', import.meta.url))
const readmePath = fileURLToPath(new URL('../../README.md', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = (id) => jobBlock(workflowPath, id)

// The site-deploy caller is the first yaml block of the README's site-deploy section.
function caller() {
  const section = readFileSync(readmePath, 'utf8').split('\n## site-deploy\n')[1]?.split(/\n## /u)[0]
  const block = section?.match(/```yaml\n([\s\S]+?)```/u)?.[1]
  assert.ok(block, 'README documents the site-deploy caller')
  return block
}

// GitHub expressions read hyphenated names with dot syntax, which JavaScript reads as subtraction.
function expression(source, context) {
  return runInNewContext(source.replace(/\.([A-Za-z_]\w*(?:-\w+)+)/gu, "['$1']"), context)
}

function context(eventName = 'workflow_run', run = {}) {
  return {
    inputs: { enabled: true },
    github: {
      event_name: eventName,
      repository,
      ref: 'refs/heads/main',
      sha: newerSha,
      event: {
        workflow_run: {
          conclusion: 'success', event: 'push', head_branch: 'main', head_sha: sha,
          head_repository: { full_name: repository }, ...run,
        },
      },
    },
  }
}

function admitted(input) {
  const condition = job('guard').match(/^ {4}if: >-\n((?: {6}.+\n)+)/mu)?.[1]
  assert.ok(condition, 'the guard has a folded if')
  return expression(condition, input)
}

function envExpression(jobId, name) {
  const source = job(jobId).match(new RegExp(`${name}: \\$\\{\\{ (.+) \\}\\}`, 'u'))?.[1]
  assert.ok(source, `${jobId} sets ${name} from an expression`)
  return source
}

// The stubs answer the commits query and the CI runs query separately and record every call. Scripts run
// under bash -e, the shell GitHub uses for a run step with no shell set.
function runStep(jobId, name, env = {}, gh = {}) {
  const folder = mkdtempSync(join(tmpdir(), 'ciwf-site-deploy-'))
  folders.push(folder)
  const output = join(folder, 'output')
  const args = join(folder, 'args')
  const curlArgs = join(folder, 'curl-args')
  writeFileSync(output, '')
  writeFileSync(join(folder, 'gh'), [
    '#!/bin/sh',
    'printf "%s\\n" "$@" "@@" >> "$TEST_GH_ARGS"',
    'case "$*" in',
    '  *"/commits "*) printf "%s\\n" "$TEST_GH_COMMITS"; exit "$TEST_GH_COMMITS_EXIT" ;;',
    '  *) printf "%s\\n" "$TEST_GH_RUNS"; exit "$TEST_GH_RUNS_EXIT" ;;',
    'esac',
    '',
  ].join('\n'), { mode: 0o700 })
  writeFileSync(join(folder, 'curl'), [
    '#!/bin/sh',
    'printf "%s\\n" "$@" >> "$TEST_CURL_ARGS"',
    'while [ "$#" -gt 0 ]; do',
    '  case "$1" in',
    '    --output) printf "%s\\n" "$TEST_SNAPSHOT" > "$2"; shift ;;',
    '    --write-out) [ "$2" = "%{http_code}" ] && printf "%s" "$TEST_HTTP_STATUS"; shift ;;',
    '  esac',
    '  shift',
    'done',
    '',
  ].join('\n'), { mode: 0o700 })
  writeFileSync(join(folder, 'npm'), [
    '#!/bin/sh',
    'printf "npm %s token=%s\\n" "$*" "$VITE_CLOUDFLARE_ANALYTICS_TOKEN" >> "$TEST_GH_ARGS"',
    'exit "$TEST_NPM_EXIT"',
    '',
  ].join('\n'), { mode: 0o700 })
  const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, jobId, name)], {
    encoding: 'utf8',
    env: {
      PATH: `${folder}:${process.env.PATH}`, GITHUB_OUTPUT: output,
      GITHUB_REPOSITORY: repository, TEST_GH_ARGS: args, TEST_NPM_EXIT: '0',
      REFRESH: 'false', TEST_CURL_ARGS: curlArgs, TEST_HTTP_STATUS: '200',
      TEST_SNAPSHOT: JSON.stringify({ source: 'live', deployedSha: sha, snapshot: {} }),
      TEST_GH_COMMITS: gh.commits ?? '', TEST_GH_COMMITS_EXIT: String(gh.commitsExit ?? 0),
      TEST_GH_RUNS: gh.runs ?? '', TEST_GH_RUNS_EXIT: String(gh.runsExit ?? 0), ...env,
    },
  })
  const log = existsSync(args) ? readFileSync(args, 'utf8') : ''
  const calls = log.split('@@\n').slice(0, -1).map((call) => call.trim().split('\n'))
  const fetch = existsSync(curlArgs) ? readFileSync(curlArgs, 'utf8').trim().split('\n') : []
  return { ...result, output: readFileSync(output, 'utf8'), calls, log, fetch }
}

const guardStep = (name, env, gh) => runStep('guard', name, env, gh)

afterEach(() => {
  for (const folder of folders.splice(0)) rmSync(folder, { recursive: true, force: true })
})

describe('disabled and unverified SHAs cannot deploy', () => {
  for (const enabled of [undefined, false, '', 'false']) {
    test(`does no work when enabled is ${JSON.stringify(enabled)}`, () => {
      const input = context()
      input.inputs.enabled = enabled
      assert.equal(admitted(input), false)
      input.github.event_name = 'workflow_dispatch'
      assert.equal(admitted(input), false)
    })
  }

  for (const enabled of ['', 'false', 'TRUE', 'true ']) {
    test(`the shell gate also refuses non-exact enablement ${JSON.stringify(enabled)}`, () => {
      assert.notEqual(guardStep('Refuse unless explicitly enabled', { ENABLED: enabled }).status, 0)
    })
  }

  test('the shell gate accepts explicit true', () => {
    assert.equal(guardStep('Refuse unless explicitly enabled', { ENABLED: 'true' }).status, 0)
    assert.equal(envExpression('guard', 'ENABLED'), 'inputs.enabled')
  })

  test('successful main-push CI admits the run and the selected SHA feeds every later job', () => {
    assert.equal(admitted(context()), true)
    assert.ok(job('guard').includes('sha: ${{ steps.ci.outputs.sha }}'))
    assert.ok(job('config').includes('needs: guard'))
    assert.ok(job('config').includes('checkout-ref: ${{ needs.guard.outputs.sha }}'))
    assert.ok(job('build').includes('needs: [guard, config]'))
    assert.ok(job('deploy').includes('needs: [guard, config, build]'))
    assert.ok(job('build').includes('ref: ${{ needs.guard.outputs.sha }}'))
    assert.ok(job('deploy').includes('ref: ${{ needs.guard.outputs.sha }}'))
  })

  for (const run of [
    { conclusion: 'failure' }, { conclusion: 'cancelled' }, { conclusion: 'skipped' },
    { event: 'pull_request' }, { head_branch: 'feature/change' },
    { head_repository: { full_name: 'someone/fork' } },
  ]) {
    test(`rejects an unverified automatic run ${JSON.stringify(run)}`, () => {
      assert.equal(admitted(context('workflow_run', run)), false)
    })
  }

  test('a manual run from any branch but main is refused', () => {
    const input = context('workflow_dispatch')
    input.github.ref = 'refs/heads/feature/change'
    assert.equal(admitted(input), false)
  })

  test('a manual dispatch uses the exact green SHA without reading the live snapshot', () => {
    const input = context('workflow_dispatch')
    input.github.event = {}
    input.github.sha = sha
    assert.equal(admitted(input), true)
    assert.equal(expression(envExpression('guard', 'REQUESTED_SHA'), input), sha)
    const manual = { REQUESTED_SHA: sha, TRIGGER_EVENT: 'workflow_dispatch' }
    const result = guardStep(selectStep, manual, { runs: '1', commits: newerSha })
    assert.equal(result.status, 0)
    assert.equal(result.output, `sha=${sha}\n`)
    assert.ok(result.stdout.includes(`Deploying ${sha}, the SHA the manual run requested.`))
    assert.deepEqual(result.fetch, [])
    assert.deepEqual(result.calls, [[
      'api', '--method', 'GET', `repos/${repository}/actions/workflows/ci.yml/runs`,
      '-f', 'branch=main', '-f', 'event=push', '-f', `head_sha=${sha}`,
      '-f', 'status=success', '-f', 'per_page=1', '--jq', '.workflow_runs | length',
    ]])
    const missing = guardStep(selectStep, manual, { runs: '0' })
    assert.notEqual(missing.status, 0)
    assert.equal(missing.output, '')
    const unavailable = guardStep(selectStep, manual, { runsExit: 1 })
    assert.notEqual(unavailable.status, 0)
    assert.equal(unavailable.output, '')
  })

  const refresh = { REQUESTED_SHA: sha, TRIGGER_EVENT: 'workflow_dispatch', REFRESH: 'true', SITE_ORIGIN: siteOrigin }

  for (const [label, value] of [
    ['http', 'http://example.test'],
    ['credentials', 'https://member:sample-password@example.test'],
    ['empty userinfo', 'https://@example.test'],
    ['a path', 'https://example.test/catalog'],
    ['a dot path', 'https://example.test/.'],
    ['a query', 'https://example.test?key=value'],
    ['an empty query', 'https://example.test?'],
    ['a fragment', 'https://example.test#catalog'],
    ['an empty fragment', 'https://example.test#'],
    ['a missing host', 'https:///example.test'],
    ['an invalid port', 'https://example.test:65536'],
    ['a hexadecimal host', 'https://0xc0000201'],
    ['a decimal host', 'https://3221225985'],
    ['an explicit default port', 'https://example.test:443'],
    ['an internationalized host', 'https://münich.example'],
    ['a Unicode case rewrite', 'https://K.example.test'],
    ['a percent-encoded host', 'https://%65xample.test'],
    ['a line break', 'https://example.test\n'],
  ]) {
    test('the refresh guard refuses invalid SITE_ORIGIN with ' + label + ' before any request', () => {
      const result = guardStep(selectStep, { ...refresh, SITE_ORIGIN: value }, { runs: '1' })
      assert.equal(result.status, 1)
      assert.deepEqual(result.calls, [])
      assert.deepEqual(result.fetch, [])
      assert.equal(result.output, '')
      assert.match(result.stderr, /^::error::SITE_ORIGIN must be the site's https origin as the browser shows it\b/mu)
      assert.ok(!result.stderr.includes(value))
    })
  }

  for (const [value, expectedOrigin] of [
    ['https://example.test', 'https://example.test'],
    ['https://Example.TEST', 'https://example.test'],
    ['https://example.test/', 'https://example.test'],
    ['https://example.test:8443', 'https://example.test:8443'],
    ['https://[2001:db8::1]:8443/', 'https://[2001:db8::1]:8443'],
  ]) {
    test('a refresh deploys when the requested green SHA is live at ' + value, () => {
      const result = guardStep(selectStep, { ...refresh, SITE_ORIGIN: value }, { runs: '1' })
      assert.equal(result.status, 0, result.stderr)
      assert.equal(result.output, `sha=${sha}\n`)
      assert.equal(result.fetch.at(-1), expectedOrigin + '/_catalog/snapshot.json')
      assert.equal(result.fetch[result.fetch.indexOf('--max-time') + 1], '30')
      assert.equal(envExpression('guard', 'REFRESH'), 'inputs.refresh')
      assert.equal(envExpression('guard', 'SITE_ORIGIN'), 'inputs.site-origin')
    })
  }

  test('a queued refresh exits green and skips deployment if the live SHA moved', () => {
    const result = guardStep(selectStep, {
      ...refresh, TEST_SNAPSHOT: JSON.stringify({ source: 'live', deployedSha: newerSha, snapshot: {} }),
    }, { runs: '1' })
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.output, '')
    assert.match(result.stdout, /^::notice::/mu)

    const condition = job('config').match(/^ {4}if: (.+)$/mu)?.[1]
    assert.ok(condition, 'the config job requires a selected SHA')
    for (const [selected, expected] of [['', false], [sha, true]]) {
      assert.equal(expression(condition, { needs: { guard: { outputs: { sha: selected } } } }), expected)
    }
  })

  for (const [label, env] of [
    ['has no origin', { SITE_ORIGIN: '' }],
    ['has no readable SHA', { TEST_SNAPSHOT: JSON.stringify({ source: 'live', snapshot: {} }) }],
  ]) {
    test('a refresh fails when it ' + label, () => {
      const result = guardStep(selectStep, { ...refresh, ...env }, { runs: '1' })
      assert.equal(result.status, 1, result.stderr)
      assert.equal(result.output, '')
    })
  }

  for (const status of ['302', '206']) {
    test('the refresh guard refuses HTTP ' + status + ' with a valid body', () => {
      const result = guardStep(selectStep, { ...refresh, TEST_HTTP_STATUS: status }, { runs: '1' })
      assert.equal(result.status, 1)
      assert.equal(result.output, '')
      assert.match(result.stderr, new RegExp(`\\bHTTP ${status}\\b`, 'u'))
    })
  }

  test('a refresh with no green CI exits before any live read', () => {
    const result = guardStep(selectStep, refresh, { runs: '0' })
    assert.equal(result.status, 1)
    assert.equal(result.output, '')
    assert.deepEqual(result.fetch, [])
  })

  test('refuses a branch name instead of an immutable deployment SHA', () => {
    const result = guardStep(selectStep, { REQUESTED_SHA: 'main', TRIGGER_EVENT: 'workflow_dispatch' }, { runs: '1' })
    assert.notEqual(result.status, 0)
    assert.equal(result.output, '')
    assert.deepEqual(result.calls, [])
  })

  test('manual rollback can select a previous green SHA while running the workflow from main', () => {
    const input = context('workflow_dispatch')
    input.github.event.workflow_run = {}
    input.inputs['dispatch-sha'] = sha
    assert.match(workflow(), /\n {6}dispatch-sha:\n(?: {8}.+\n)* {8}default: ''\n/u)
    assert.match(caller(), /^ {2}workflow_dispatch:\n {4}inputs:\n {6}sha: \{ type: string, required: false \}$/mu)
    assert.match(caller(), /^ {6}refresh: \{ type: boolean, default: false \}$/mu)
    assert.match(caller(), /^ {6}refresh: \$\{\{ inputs\.refresh == true \}\}$/mu)
    assert.ok(caller().includes('dispatch-sha: ${{ inputs.sha }}'))
    assert.equal(expression(envExpression('guard', 'REQUESTED_SHA'), input), sha)
  })

  test('serializes uploads and leaves the api build to Oryx', () => {
    const source = workflow()
    assert.match(caller(), /workflow_run: \{ workflows: \[CI\], branches: \[main\], types: \[completed\] \}/u)
    assert.match(caller(), /concurrency: \{ group: deploy-main, cancel-in-progress: false \}/u)
    // A called workflow that repeats its caller's concurrency group deadlocks, so only the caller sets it.
    assert.match(source, /^on:\n {2}workflow_call:\n/mu)
    assert.doesNotMatch(source, /^concurrency:/mu)
    assert.doesNotMatch(source, /azure\/login|id-token:|skip_api_build|api_language|api_version/iu)
    assert.ok(job('guard').includes('actions: read'))
    const deploy = job('deploy')
    assert.ok(deploy.includes('app_location: ${{ needs.config.outputs.app-location }}'))
    assert.ok(deploy.includes('api_location: ${{ steps.api.outputs.location }}'))
    assert.ok(deploy.includes('skip_app_build: true'))
    assert.ok(deploy.includes('azure_static_web_apps_api_token: ${{ secrets.AZURE_STATIC_WEB_APPS_API_TOKEN }}'))
    assert.ok(deploy.includes('SITE_URL: ${{ steps.deploy.outputs.static_web_app_url }}'))
    assert.ok(deploy.includes('node scripts/smoke-site.mjs "$SITE_URL"'))
    assert.ok(deploy.indexOf('Azure/static-web-apps-deploy@') < deploy.indexOf('node scripts/smoke-site.mjs'))
  })
})

describe('an automatic run deploys the newest green main commit', () => {
  const automatic = (gh, trigger = sha) => guardStep(selectStep, { TRIGGER_SHA: trigger, TRIGGER_EVENT: 'workflow_run' }, gh)
  const lines = (...values) => values.join('\n')

  test('a green tip deploys and both queries are exact', () => {
    const result = automatic({ commits: lines(newerSha, sha), runs: lines(newerSha, sha) }, newerSha)
    assert.equal(result.status, 0)
    assert.equal(result.output, `sha=${newerSha}\n`)
    assert.ok(result.stdout.includes(`Deploying ${newerSha}, the newest main commit with a successful main-push CI run. CI passing for ${newerSha} started this run.`))
    assert.deepEqual(result.calls, [
      ['api', '--method', 'GET', `repos/${repository}/commits`, '-f', 'sha=main', '-f', 'per_page=30', '--jq', '.[].sha'],
      [
        'api', '--method', 'GET', `repos/${repository}/actions/workflows/ci.yml/runs`,
        '-f', 'branch=main', '-f', 'event=push', '-f', 'status=success', '-f', 'per_page=100',
        '--jq', '.workflow_runs[].head_sha',
      ],
    ])
  })

  test('a run for an older commit whose CI finished last deploys the green tip instead of skipping', () => {
    const result = automatic({ commits: lines(newerSha, sha, olderSha), runs: lines(newerSha, sha, olderSha) }, sha)
    assert.equal(result.status, 0)
    assert.equal(result.output, `sha=${newerSha}\n`)
  })

  test('a tip whose CI has not finished leaves the newest green commit to deploy', () => {
    const result = automatic({ commits: lines(newerSha, sha), runs: lines(sha, olderSha) })
    assert.equal(result.status, 0)
    assert.equal(result.output, `sha=${sha}\n`)
    assert.ok(result.stdout.includes(`Skipping ${newerSha}, it has no successful main-push CI run.`))
  })

  test('a failed tip keeps the newest older green commit', () => {
    const result = automatic({ commits: lines(newerSha, sha, olderSha), runs: olderSha }, olderSha)
    assert.equal(result.status, 0)
    assert.equal(result.output, `sha=${olderSha}\n`)
    assert.ok(result.stdout.includes(`Skipping ${newerSha}`))
    assert.ok(result.stdout.includes(`Skipping ${sha}`))
  })

  for (const [label, runs] of [
    ['no successful run', ''],
    ['successful runs only for SHAs no longer on main', 'fedcba0987654321fedcba0987654321fedcba09'],
  ]) {
    test(`fails when main has ${label}`, () => {
      const result = automatic({ commits: lines(newerSha, sha), runs })
      assert.notEqual(result.status, 0)
      assert.equal(result.output, '')
      assert.ok(result.stderr.includes("None of main's latest 30 commits has a successful main-push CI run."))
    })
  }

  test('never selects a value that is not a full commit SHA', () => {
    const result = automatic({ commits: lines('main', sha), runs: lines('main', sha) })
    assert.equal(result.status, 0)
    assert.equal(result.output, `sha=${sha}\n`)
  })

  for (const [label, gh] of [
    ['commits', { commits: lines(newerSha), commitsExit: 1, runs: newerSha }],
    ['CI runs', { commits: lines(newerSha), runs: newerSha, runsExit: 1 }],
  ]) {
    test(`a failed ${label} lookup fails the run instead of deploying`, () => {
      const result = automatic(gh)
      assert.notEqual(result.status, 0)
      assert.equal(result.output, '')
    })
  }

  test('the deploy guard has a five-minute timeout', () => {
    assert.equal(job('guard').match(/^ {4}timeout-minutes: (\d+)$/mu)?.[1], '5')
  })

  test('the guard reads both APIs with read-only permissions and every later job needs it', () => {
    const guard = job('guard')
    assert.ok(guard.includes('TRIGGER_SHA: ${{ github.event.workflow_run.head_sha }}'))
    assert.equal(expression(envExpression('guard', 'TRIGGER_SHA'), context()), sha)
    // The commits API on a private repository needs contents read beside actions read.
    assert.match(guard, /permissions:\n {6}actions: read\n {6}contents: read\n {4}outputs:/u)
    // No skip output remains, so a later job runs exactly when the guard succeeds.
    assert.doesNotMatch(workflow(), /outputs\.deploy|steps\.tip/u)
    for (const id of ['build', 'deploy']) assert.doesNotMatch(job(id), /^ {4}if:/mu)
  })

  test('the documented caller grants the token every permission the guard asks for', () => {
    // A called job can only narrow the caller's token, and the org default token has no actions read.
    assert.match(caller(), /^permissions: \{ actions: read, contents: read \}$/mu)
  })

  test('the documented caller keeps every expression out of a YAML flow mapping', () => {
    // A plain scalar inside { } cannot hold the braces of an expression, so such a caller fails to parse.
    assert.doesNotMatch(caller(), /\{[^\n}]*\$\{\{/u)
    assert.match(caller(), /\n {4}with:\n {6}enabled: \$\{\{ vars\.AZURE_ENABLED == 'true' \}\}\n/u)
  })
})

describe('the build never sees the deploy token', () => {
  test('a secretless build job installs and builds the exact SHA and hands over the app location', () => {
    const build = job('build')
    assert.doesNotMatch(build, /secrets\.|environment:/u)
    assert.match(build, /permissions:\n {6}contents: read\n {4}steps:/u)
    assert.ok(build.includes('persist-credentials: false'))
    assert.ok(build.includes('node-version-file: ${{ inputs.node-version-file }}'))
    assert.ok(build.indexOf('node scripts/check-npm-policy.mjs') < build.indexOf('npm ci'))
    assert.ok(build.indexOf('npm ci') < build.indexOf('"$BUILD_COMMAND"'))
    assert.equal(envExpression('build', 'VITE_CLOUDFLARE_ANALYTICS_TOKEN'), 'inputs.analytics-token')
    assert.equal(envExpression('build', 'BUILD_COMMAND'), 'inputs.build-command')
    assert.ok(build.indexOf('"$BUILD_COMMAND"') < build.indexOf('actions/upload-artifact@'))
    assert.match(build, /upload-artifact@[a-f0-9]{40} # v[\d.]+\n {8}with:\n {10}name: site\n {10}path: \$\{\{ needs\.config\.outputs\.app-location \}\}\n {10}if-no-files-found: error\n {10}retention-days: 1\n {10}overwrite: true\n/u)
  })

  test('catalog snapshot uses the selected deployment and precedes both build steps', () => {
    const snapshot = stepBlock(workflowPath, 'build', snapshotStep)
    const condition = snapshot.match(/^ {8}if: (.+)$/mu)?.[1]
    assert.ok(condition, 'the snapshot has a capability condition')
    for (const capability of ['true', 'false', undefined]) {
      assert.equal(expression(condition, {
        needs: { config: { outputs: { 'has-catalog': capability } } },
      }), capability === 'true')
    }

    assert.match(snapshot, /^ {10}SITE_ORIGIN: \$\{\{ inputs\.site-origin \}\}$/mu)
    assert.match(snapshot, /^ {10}DEPLOYED_SHA: \$\{\{ needs\.guard\.outputs\.sha \}\}$/mu)
    assert.match(snapshot, /^ {10}CATALOG_DEPLOY: '1'$/mu)
    assert.doesNotMatch(stepScript(workflowPath, 'build', snapshotStep), /\$\{\{/u)

    const build = job('build')
    const position = build.indexOf(snapshot)
    assert.ok(build.indexOf('actions/checkout@') < build.indexOf('actions/setup-node@'))
    assert.ok(build.indexOf('actions/setup-node@') < position)
    for (const name of ['Build site', 'Build site with analytics']) {
      assert.ok(position < build.indexOf('- name: ' + name + '\n'))
    }
  })

  for (const exitCode of [0, 1]) {
    test('catalog snapshot ' + (exitCode ? 'fails the build step on error' : 'records its source in the step summary'), () => {
      const folder = mkdtempSync(join(tmpdir(), 'ciwf-catalog-snapshot-'))
      folders.push(folder)
      const summary = join(folder, 'summary')
      const source = 'Catalog snapshot source: live\n'
      writeFileSync(summary, 'Existing summary\n')
      mkdirSync(join(folder, 'scripts'))
      writeFileSync(join(folder, 'scripts/catalog-snapshot.mjs'), exitCode
        ? 'process.exit(1)'
        : 'process.stdout.write(' + JSON.stringify(source) + ')')
      const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'build', snapshotStep)], {
        cwd: folder, encoding: 'utf8', env: { PATH: process.env.PATH, GITHUB_STEP_SUMMARY: summary },
      })

      assert.equal(result.status, exitCode, result.stderr)
      assert.equal(readFileSync(summary, 'utf8'), 'Existing summary\n' + (exitCode ? '' : source))
    })
  }

  test('the build step installs first, then runs the build command from env', () => {
    const env = { BUILD_COMMAND: 'npm run build' }
    const result = runStep('build', 'Build site', env)
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.log, 'npm ci token=\nnpm run build token=\n')
    const custom = runStep('build', 'Build site', { ...env, BUILD_COMMAND: 'npm run build:site -- --mode "a b"' })
    assert.equal(custom.status, 0, custom.stderr)
    assert.equal(custom.log, 'npm ci token=\nnpm run build:site -- --mode a b token=\n')
    const failedInstall = runStep('build', 'Build site', { ...env, TEST_NPM_EXIT: '1' })
    assert.notEqual(failedInstall.status, 0)
    assert.equal(failedInstall.log, 'npm ci token=\n')
    const analytics = runStep('build', 'Build site with analytics', {
      ...env, VITE_CLOUDFLARE_ANALYTICS_TOKEN: 'fixture-analytics',
    })
    assert.equal(analytics.status, 0, analytics.stderr)
    assert.equal(analytics.log, 'npm ci token=fixture-analytics\nnpm run build token=fixture-analytics\n')
  })

  for (const command of ['false; npm run after', 'false | cat; npm run after', 'echo "$UNSET_NAME"; npm run after']) {
    test(`a build command that fails part way fails the step: ${command}`, () => {
      const env = { BUILD_COMMAND: command }
      const result = runStep('build', 'Build site', env)
      assert.notEqual(result.status, 0)
      assert.equal(result.log, 'npm ci token=\n')
    })
  }

  test('only operational inputs remain', () => {
    const source = workflow()
    const defaults = {
      'dispatch-sha': "''", 'node-version-file': '.nvmrc',
      'build-command': 'npm run build', 'analytics-token': "''", 'site-origin': "''",
    }
    for (const [name, value] of Object.entries(defaults)) {
      assert.match(source, new RegExp(`\\n {6}${name}:\\n(?: {8}.+\\n)* {8}default: ${value.replace(/\./gu, '\\.')}\\n`, 'u'), name)
    }
    const inputs = source.split('    inputs:\n')[1].split('    secrets:')[0]
    assert.doesNotMatch(inputs, /\n {6}(?:app-location|api-location|has-api|has-e2e|has-infra|bicep-params):/u)
    assert.match(source, /\n {6}enabled:\n(?: {8}.+\n)* {8}required: true\n/u)
    assert.match(source, /\n {6}enabled:\n(?: {8}.+\n)* {8}type: boolean\n/u)
    assert.match(source, /\n {6}refresh:\n(?: {8}.+\n)* {8}default: false\n {8}type: boolean\n/u)
  })

  test('the deploy job installs nothing and uploads the downloaded artifact', () => {
    const deploy = job('deploy')
    assert.doesNotMatch(deploy, /npm (ci|install|run)|check-npm-policy/u)
    assert.ok(deploy.includes('persist-credentials: false'))
    assert.ok(deploy.includes('package-manager-cache: false'))
    assert.doesNotMatch(deploy, /cache: npm/u)
    assert.match(deploy, /download-artifact@[a-f0-9]{40} # v[\d.]+\n {8}with:\n {10}name: site\n {10}path: \$\{\{ needs\.config\.outputs\.app-location \}\}\n/u)
    assert.ok(deploy.indexOf('actions/download-artifact@') < deploy.indexOf('Azure/static-web-apps-deploy@'))
  })

  test('the deploy job is the only one that reads a secret', () => {
    for (const id of ['guard', 'config', 'build']) assert.doesNotMatch(job(id), /secrets\./u)
    assert.equal(workflow().match(/secrets\./gu)?.length, 2)
    assert.equal(job('deploy').match(/secrets\./gu)?.length, 2)
  })

  test('only the job that reads the token runs in the production environment', () => {
    assert.match(job('deploy'), /^ {4}environment: production$/mu)
    for (const id of ['guard', 'config', 'build']) assert.doesNotMatch(job(id), /environment:/u)
    assert.equal(workflow().match(/^\s+environment:/gmu)?.length, 1)
  })

  test('deploy job is the only job with environment production', () => {
    const ids = workflow().split('\njobs:\n')[1].match(/^ {2}[a-z][\w-]*(?=:$)/gmu).map((id) => id.trim())
    assert.deepEqual(ids, ['guard', 'config', 'build', 'deploy'])
    const withProduction = ids.filter((id) => /^ {4}environment: production$/mu.test(job(id)))
    assert.deepEqual(withProduction, ['deploy'])
  })

  test('only the deploy job references the AZURE_STATIC_WEB_APPS_API_TOKEN secret', () => {
    // The name matches the production environment secret, which wins over the passed value in a job that
    // declares the environment. Empty is caught by the deploy job's first step, not by required.
    assert.match(workflow(), /\n {4}secrets:\n {6}AZURE_STATIC_WEB_APPS_API_TOKEN:\n(?: {8}.+\n)* {8}required: false\n/u)
    assert.match(caller(), /\n {4}secrets:\n {6}AZURE_STATIC_WEB_APPS_API_TOKEN: \$\{\{ secrets\.AZURE_STATIC_WEB_APPS_API_TOKEN \}\}\n/u)
    assert.doesNotMatch(workflow(), /deploy-token/u)
    for (const id of ['guard', 'config', 'build']) assert.doesNotMatch(job(id), /AZURE_STATIC_WEB_APPS_API_TOKEN/u)
    // The only-reader test above counts both secret references. These assertions name them.
    const deploy = job('deploy')
    assert.ok(deploy.includes('azure_static_web_apps_api_token: ${{ secrets.AZURE_STATIC_WEB_APPS_API_TOKEN }}'))
    // Besides the action input, the token reaches only the empty check, through env.
    assert.deepEqual(deploy.match(/^ +[A-Z_]+: \$\{\{ secrets\.[A-Z_]+ \}\}$/gmu)?.map((line) => line.trim()), [
      'DEPLOY_TOKEN: ${{ secrets.AZURE_STATIC_WEB_APPS_API_TOKEN }}',
    ])
  })

  test('the deploy job first refuses an empty token without printing it', () => {
    const deploy = job('deploy')
    const steps = deploy.match(/^ {6}- .+$/gmu)
    assert.equal(steps?.[0].trim(), '- name: Require the deploy token')
    assert.ok(deploy.indexOf('- name: Require the deploy token') < deploy.indexOf('Azure/static-web-apps-deploy@'))
    const missing = runStep('deploy', 'Require the deploy token', { DEPLOY_TOKEN: '' })
    assert.notEqual(missing.status, 0)
    assert.ok(missing.stderr.includes('AZURE_STATIC_WEB_APPS_API_TOKEN is empty.'))
    const unset = runStep('deploy', 'Require the deploy token')
    assert.notEqual(unset.status, 0)
    assert.ok(unset.stderr.includes('AZURE_STATIC_WEB_APPS_API_TOKEN is empty.'))
    const token = 'token-value-that-must-not-print'
    const present = runStep('deploy', 'Require the deploy token', { DEPLOY_TOKEN: token })
    assert.equal(present.status, 0, present.stderr)
    assert.ok(!`${present.stdout}${present.stderr}`.includes(token))
  })
})
