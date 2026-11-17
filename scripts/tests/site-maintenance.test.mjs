// Exercises site-maintenance.yml: the step that skips while the site origin or the key is unset, run under bash with
// GitHub's output file, and the wiring that keeps the key in env, the token read-only and the job on a hosted runner.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-maintenance.yml', import.meta.url))
const readmePath = fileURLToPath(new URL('../../README.md', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = () => jobBlock(workflowPath, 'maintain')
const settingsStep = 'Check the maintenance settings'
const callStep = 'Call the maintenance route'
const folders = []

afterEach(() => {
  for (const path of folders.splice(0)) rmSync(path, { recursive: true, force: true })
})

// Runs the settings step under bash -e, the shell GitHub uses for a run step with no shell set.
function runSettings(env) {
  const root = mkdtempSync(join(tmpdir(), 'ciwf-site-maintenance-'))
  folders.push(root)
  const output = join(root, 'output')
  const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'maintain', settingsStep)], {
    cwd: root, encoding: 'utf8', env: { PATH: process.env.PATH, GITHUB_OUTPUT: output, ...env },
  })
  let lines = []
  try {
    lines = readFileSync(output, 'utf8').split('\n').filter(Boolean)
  } catch {}
  return { ...result, lines }
}

describe('the settings step skips until the site origin and the key are both set', () => {
  const origin = 'https://site.example'
  const key = 'example-maintenance-key'

  test('both set runs the maintenance steps', () => {
    const result = runSettings({ SITE_ORIGIN: origin, MAINTENANCE_KEY: key })
    assert.equal(result.status, 0, result.stderr)
    assert.deepEqual(result.lines, ['run=true'])
    assert.doesNotMatch(result.stdout, /::notice::/u)
  })

  for (const [label, env] of [
    ['the origin is empty', { SITE_ORIGIN: '', MAINTENANCE_KEY: key }],
    ['the key is empty', { SITE_ORIGIN: origin, MAINTENANCE_KEY: '' }],
    ['both are empty', { SITE_ORIGIN: '', MAINTENANCE_KEY: '' }],
  ]) {
    test(`skips with a notice and passes when ${label}`, () => {
      const result = runSettings(env)
      assert.equal(result.status, 0, result.stderr)
      assert.deepEqual(result.lines, ['run=false'])
      assert.match(result.stdout, /^::notice::/mu)
      assert.ok(!result.stdout.includes(key), 'the notice never prints the key')
    })
  }
})

describe('the workflow runs the site script with the key in env only', () => {
  test('it is called by a site with an optional origin input and an optional key secret', () => {
    assert.match(workflow(), /^on:\n {2}workflow_call:\n {4}inputs:\n {6}site-origin:\n/mu)
    assert.match(workflow(), /^ {4}secrets:\n {6}maintenance-key:\n(?: {8}.*\n)* {8}required: false$/mu)
    assert.doesNotMatch(workflow(), /^ {2}(?:schedule|push|pull_request|workflow_dispatch):/mu)
  })

  test('the token reads contents only and the job runs on the pinned hosted image with a timeout', () => {
    assert.match(workflow(), /^permissions: \{\}$/mu)
    assert.match(job(), /^ {4}permissions:\n {6}contents: read\n {4}\S/mu)
    assert.match(job(), /^ {4}runs-on: ubuntu-26\.04$/mu)
    assert.match(job(), /^ {4}timeout-minutes: \d+$/mu)
    assert.match(job(), /^ {10}persist-credentials: false$/mu)
  })

  test('every later step runs only when the settings step said so', () => {
    const steps = job().split(/\n(?= {6}- )/u).slice(1)
    assert.ok(steps.length > 1, 'the job has steps after the settings step')
    assert.match(steps[0], /^ {6}- name: Check the maintenance settings$/mu)
    for (const step of steps.slice(1)) assert.match(step, /^ {6}(?:- | {2})if: steps\.settings\.outputs\.run == 'true'$/mu)
  })

  test('the call step runs the checked-out script with the origin and key from env', () => {
    const call = stepBlock(workflowPath, 'maintain', callStep)
    assert.match(call, /^ {10}SITE_ORIGIN: \$\{\{ inputs\.site-origin \}\}$/mu)
    assert.match(call, /^ {10}MAINTENANCE_KEY: \$\{\{ secrets\.maintenance-key \}\}$/mu)
    assert.match(call, /^ {8}run: node scripts\/run-maintenance\.mjs$/mu)
  })

  test('no run line interpolates an expression', () => {
    assert.doesNotMatch(stepScript(workflowPath, 'maintain', settingsStep), /\$\{\{/u)
    for (const line of workflow().split('\n').filter((text) => /^\s+run: /u.test(text))) assert.doesNotMatch(line, /\$\{\{/u)
  })

  test('the README caller runs daily and by hand, one run at a time, with a read-only token', () => {
    const section = readFileSync(readmePath, 'utf8').split('\n## site-maintenance\n')[1]?.split(/\n## /u)[0]
    const caller = section?.match(/```yaml\n([\s\S]+?)```/u)?.[1]
    assert.ok(caller, 'README documents the site-maintenance caller')
    assert.match(caller, /^ {4}- cron: '\d{1,2} \d{1,2} \* \* \*'$/mu)
    assert.match(caller, /^ {2}workflow_dispatch:$/mu)
    assert.match(caller, /^permissions:\n {2}contents: read$/mu)
    assert.match(caller, /^concurrency:\n {2}group: maintenance\n {2}cancel-in-progress: false$/mu)
    assert.match(caller, /^ {4}uses: APKiwiOrg\/ci-workflows\/\.github\/workflows\/site-maintenance\.yml@<full commit sha>$/mu)
    assert.match(caller, /^ {6}site-origin: \$\{\{ vars\.SITE_ORIGIN \}\}$/mu)
    assert.match(caller, /^ {6}maintenance-key: \$\{\{ secrets\.MAINTENANCE_KEY \}\}$/mu)
  })
})
