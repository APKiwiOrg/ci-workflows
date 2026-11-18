import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-catalog-refresh.yml', import.meta.url))
const stepName = 'Dispatch catalog refresh'
const origin = 'https://site.example'
const repository = 'APKiwiOrg/site'
const sha = '1234567890abcdef1234567890abcdef12345678'
const snapshot = { source: 'live', deployedSha: sha, snapshot: {} }

// Use the real jq and workflow script. Only network commands are replaced.
function runRefresh(env = {}) {
  assert.ok(existsSync(workflowPath), 'the reusable catalog refresh workflow exists')
  const script = [
    'curl() {',
    '  printf "%s\\n" "$@" >&2',
    '  local output= format=',
    '  while [ "$#" -gt 0 ]; do',
    '    case "$1" in',
    '      --output) output=$2; shift ;;',
    '      --write-out) format=$2; shift ;;',
    '    esac',
    '    shift',
    '  done',
    '  if [ -n "$output" ]; then printf "%s\\n" "$TEST_SNAPSHOT" > "$output"; else printf "%s\\n" "$TEST_SNAPSHOT"; fi',
    '  if [ "$format" = "%{http_code}" ]; then printf "%s" "$TEST_HTTP_STATUS"; fi',
    '  if [ -n "$TEST_DEPLOY_DURING_FETCH" ]; then : > "$TEST_DEPLOY_DURING_FETCH"; fi',
    '  return "$TEST_CURL_EXIT"',
    '}',
    'gh() {',
    '  if [ "$1" = api ]; then',
    '    [ "$2" = "--method" ] && [ "$3" = GET ] && [ "$4" = "repos/$GH_REPO/actions/workflows/deploy.yml/runs" ] || return 3',
    '    if [ "$TEST_LIST_EXIT" != 0 ]; then return "$TEST_LIST_EXIT"; fi',
    '    local active_status=$TEST_ACTIVE_STATUS',
    '    if [ -f "$TEST_DEPLOY_DURING_FETCH" ]; then active_status=pending; fi',
    '    case " $* " in',
    '      *" status=$active_status "*) printf "1\\n" ;;',
    '      *) printf "0\\n" ;;',
    '    esac',
    '  else',
    '    printf "%s\\n" "$@"',
    '  fi',
    '}',
    stepScript(workflowPath, 'refresh', stepName),
  ].join('\n')
  return spawnSync('bash', ['-e', '-c', script], {
    encoding: 'utf8',
    env: {
      PATH: process.env.PATH, ENABLED: 'true', SITE_ORIGIN: origin,
      GH_REPO: repository, TEST_ACTIVE_STATUS: '', TEST_DEPLOY_DURING_FETCH: '', TEST_LIST_EXIT: '0',
      TEST_SNAPSHOT: JSON.stringify(snapshot), TEST_CURL_EXIT: '0', TEST_HTTP_STATUS: '200', ...env,
    },
  })
}

test('the reusable workflow passes inputs and caller credentials through env without checkout', () => {
  assert.ok(existsSync(workflowPath), 'the reusable catalog refresh workflow exists')
  const workflow = readFileSync(workflowPath, 'utf8')
  const job = jobBlock(workflowPath, 'refresh')
  const step = stepBlock(workflowPath, 'refresh', stepName)
  assert.match(workflow, /^on:\n {2}workflow_call:\n {4}inputs:/mu)
  assert.match(workflow, /^ {6}enabled:\n(?: {8}.+\n)* {8}type: boolean$/mu)
  assert.match(workflow, /^ {6}site-origin:\n(?: {8}.+\n)* {8}type: string$/mu)
  assert.match(job, /^ {4}runs-on: ubuntu-26\.04$/mu)
  assert.match(job, /^ {4}timeout-minutes: [1-9]\d*$/mu)
  assert.match(job, /^ {4}permissions:\n {6}actions: write$/mu)
  assert.doesNotMatch(job, /actions\/checkout|secrets\./u)
  assert.match(step, /^ {10}ENABLED: \$\{\{ inputs\.enabled \}\}$/mu)
  assert.match(step, /^ {10}SITE_ORIGIN: \$\{\{ inputs\.site-origin \}\}$/mu)
  assert.match(step, /^ {10}GH_REPO: \$\{\{ github\.repository \}\}$/mu)
  assert.match(step, /^ {10}GH_TOKEN: \$\{\{ github\.token \}\}$/mu)
  assert.doesNotMatch(stepScript(workflowPath, 'refresh', stepName), /\$\{\{/u)
})

for (const [label, env] of [
  ['disabled', { ENABLED: 'false' }],
  ['the origin is empty', { SITE_ORIGIN: '' }],
]) {
  test('skips before fetching or dispatching when ' + label, () => {
    const result = runRefresh({ ...env, TEST_LIST_EXIT: '7' })
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.stderr, '')
    assert.match(result.stdout, /^::notice::[^\n]+\n$/u)
  })
}

for (const status of ['requested', 'queued', 'pending', 'waiting', 'in_progress']) {
  test('skips the refresh when a deploy is ' + status, () => {
    const result = runRefresh({ TEST_ACTIVE_STATUS: status })
    assert.equal(result.status, 0, result.stderr)
    assert.match(result.stdout, /^::notice::[^\n]+\n$/u)
  })
}

test('skips without dispatching when a deploy becomes pending during the snapshot fetch', (t) => {
  const folder = mkdtempSync(join(tmpdir(), 'ciwf-refresh-deploy-'))
  t.after(() => rmSync(folder, { recursive: true, force: true }))
  const result = runRefresh({ TEST_DEPLOY_DURING_FETCH: join(folder, 'pending') })

  assert.equal(result.status, 0, result.stderr)
  assert.match(result.stdout, /^::notice::[^\n]+\n$/u)
})

test('a failed active-deploy lookup prevents a dispatch', () => {
  const result = runRefresh({ TEST_LIST_EXIT: '7' })
  assert.equal(result.status, 7)
  assert.equal(result.stdout, '')
})

for (const [label, deployedSha] of [
  ['short', sha.slice(0, 7)],
  ['missing', undefined],
  ['uppercase', sha.toUpperCase()],
  ['followed by a newline', sha + '\n'],
]) {
  test('refuses to dispatch when the published SHA is ' + label, () => {
    const result = runRefresh({ TEST_SNAPSHOT: JSON.stringify({ ...snapshot, deployedSha }) })
    assert.equal(result.status, 1, result.stderr)
    assert.equal(result.stdout, '')
  })
}

for (const status of ['302', '206', '404']) {
  test('the refresh refuses HTTP ' + status + ' with a valid body', () => {
    const result = runRefresh({ TEST_HTTP_STATUS: status })
    assert.equal(result.status, 1)
    assert.equal(result.stdout, '')
    assert.match(result.stderr, new RegExp(`\\bHTTP ${status}\\b`, 'u'))
  })
}

test('a transport error prevents a dispatch even if a body was written', () => {
  const result = runRefresh({ TEST_CURL_EXIT: '7' })
  assert.equal(result.status, 7)
  assert.equal(result.stdout, '')
})

test('fetches the published snapshot and dispatches deploy.yml on main with its exact SHA', () => {
  const result = runRefresh({ SITE_ORIGIN: origin + '/' })
  assert.equal(result.status, 0, result.stderr)
  assert.equal(result.stdout, 'workflow\nrun\ndeploy.yml\n--ref\nmain\n-f\nsha=' + sha + '\n-f\nrefresh=true\n')
  const curlArgs = result.stderr.trim().split('\n')
  assert.equal(curlArgs.at(-1), origin + '/_catalog/snapshot.json')
  assert.equal(curlArgs[curlArgs.indexOf('--max-time') + 1], '30')
  assert.ok(!curlArgs.includes('--location'))
  assert.ok(!curlArgs.includes('-L'))
})
