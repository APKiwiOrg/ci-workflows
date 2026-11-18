import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-catalog-refresh.yml', import.meta.url))
const stepName = 'Dispatch catalog refresh'
const origin = 'https://site.example'
const sha = '1234567890abcdef1234567890abcdef12345678'
const snapshot = { source: 'live', deployedSha: sha, snapshot: {} }

// Use the real jq and workflow script. Only network commands are replaced.
function runRefresh(env = {}) {
  assert.ok(existsSync(workflowPath), 'the reusable catalog refresh workflow exists')
  const script = [
    'curl() { printf "%s\\n" "$@" >&2; printf "%s\\n" "$TEST_SNAPSHOT"; return "$TEST_CURL_EXIT"; }',
    'gh() { printf "%s\\n" "$@"; }',
    stepScript(workflowPath, 'refresh', stepName),
  ].join('\n')
  return spawnSync('bash', ['-e', '-c', script], {
    encoding: 'utf8',
    env: {
      PATH: process.env.PATH, ENABLED: 'true', SITE_ORIGIN: origin,
      TEST_SNAPSHOT: JSON.stringify(snapshot), TEST_CURL_EXIT: '0', ...env,
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
    const result = runRefresh(env)
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.stderr, '')
    assert.match(result.stdout, /^::notice::[^\n]+\n$/u)
  })
}

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

test('fails on an HTTP error even if the fetch prints a valid SHA', () => {
  const result = runRefresh({ TEST_CURL_EXIT: '22' })
  assert.notEqual(result.status, 0)
  assert.equal(result.stdout, '')
})

test('fetches the published snapshot and dispatches deploy.yml on main with its exact SHA', () => {
  const result = runRefresh({ SITE_ORIGIN: origin + '/' })
  assert.equal(result.status, 0, result.stderr)
  assert.equal(result.stdout, 'workflow\nrun\ndeploy.yml\n--ref\nmain\n-f\nsha=' + sha + '\n')
  const curlArgs = result.stderr.trim().split('\n')
  assert.equal(curlArgs.at(-1), origin + '/_catalog/snapshot.json')
  assert.ok(curlArgs.includes('--fail'))
  assert.ok(curlArgs.includes('--max-time'))
  assert.ok(Number(curlArgs[curlArgs.indexOf('--max-time') + 1]) > 0)
})
