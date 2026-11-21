// Exercises site-catalog-import.yml: the settings step that skips while the site origin or the key is unset, the
// install and import steps run under bash against stubs, and the wiring that keeps the key in the import step only,
// the token read-only and the job on a hosted runner.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-catalog-import.yml', import.meta.url))
const readmePath = fileURLToPath(new URL('../../README.md', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = () => jobBlock(workflowPath, 'import')
const settingsStep = 'Check the import settings'
const policyStep = 'Check the npm install policy'
const installStep = 'Install the importer'
const importStep = 'Run the import'
const origin = 'https://site.example'
const key = 'example-import-key'
const folders = []

afterEach(() => {
  for (const path of folders.splice(0)) rmSync(path, { recursive: true, force: true })
})

function folder() {
  const root = mkdtempSync(join(tmpdir(), 'ciwf-site-catalog-import-'))
  folders.push(root)
  return root
}

// The job's steps in order, each as its block of lines.
const steps = () => job().split(/\n(?= {6}- )/u).slice(1)
const stepNames = () => steps().map((step) => step.match(/^ {6}- name: (.+)$/mu)?.[1] ?? null)

// Runs one step's script under bash -e, the shell GitHub uses for a run step with no shell set.
function runStep(name, { cwd, env = {}, path = process.env.PATH }) {
  const output = join(cwd, '.github-output')
  const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'import', name)], {
    cwd, encoding: 'utf8', env: { PATH: path, GITHUB_OUTPUT: output, ...env },
  })
  let lines = []
  try {
    lines = readFileSync(output, 'utf8').split('\n').filter(Boolean)
  } catch {}
  return { ...result, lines }
}

describe('the settings step skips until the site origin and the key are both set', () => {
  test('both set runs the import steps', () => {
    assert.ok(existsSync(workflowPath), 'the reusable catalog import workflow exists')
    const result = runStep(settingsStep, { cwd: folder(), env: { SITE_ORIGIN: origin, CATALOG_IMPORT_KEY: key } })
    assert.equal(result.status, 0, result.stderr)
    assert.deepEqual(result.lines, ['run=true', 'missing='])
    assert.doesNotMatch(result.stdout, /::notice::/u)
  })

  for (const [label, env, missing] of [
    ['the origin is empty', { SITE_ORIGIN: '', CATALOG_IMPORT_KEY: key }, 'site-origin'],
    ['the key is empty', { SITE_ORIGIN: origin, CATALOG_IMPORT_KEY: '' }, 'import-key'],
    ['the origin is unset', { CATALOG_IMPORT_KEY: key }, 'site-origin'],
    ['the key is unset', { SITE_ORIGIN: origin }, 'import-key'],
    ['both are empty', { SITE_ORIGIN: '', CATALOG_IMPORT_KEY: '' }, 'site-origin import-key'],
  ]) {
    test(`skips with a notice and passes when ${label}`, () => {
      const result = runStep(settingsStep, { cwd: folder(), env })
      assert.equal(result.status, 0, result.stderr)
      assert.deepEqual(result.lines, ['run=false', `missing=${missing}`])
      assert.equal(result.stdout.split('\n').filter((line) => line.startsWith('::notice::')).length, 1)
      assert.ok(!result.stdout.includes(key), 'the notice never prints the key')
    })
  }
})

describe('the install step', () => {
  // A stub npm records its arguments and exits as told.
  function stubNpm(root) {
    const bin = join(root, 'bin')
    mkdirSync(bin)
    writeFileSync(join(bin, 'npm'), '#!/bin/sh\nprintf "%s\\n" "$@" > "$NPM_ARGS"\nexit "${NPM_EXIT:-0}"\n')
    chmodSync(join(bin, 'npm'), 0o755)
    return `${bin}:${process.env.PATH}`
  }

  test('installs the importer package from its lockfile with scripts off', () => {
    const root = folder()
    const env = { NPM_ARGS: join(root, 'args') }
    const result = runStep(installStep, { cwd: root, env, path: stubNpm(root) })
    assert.equal(result.status, 0, result.stderr)
    assert.deepEqual(readFileSync(env.NPM_ARGS, 'utf8').split('\n').filter(Boolean), ['ci', '--prefix', 'importer', '--ignore-scripts'])
  })

  test('a failed install fails the step beside a passing control', () => {
    const root = folder()
    const env = { NPM_ARGS: join(root, 'args') }
    const path = stubNpm(root)
    assert.equal(runStep(installStep, { cwd: root, env: { ...env, NPM_EXIT: '0' }, path }).status, 0)
    assert.equal(runStep(installStep, { cwd: root, env: { ...env, NPM_EXIT: '1' }, path }).status, 1)
  })

  test('the install and the policy check that precedes it carry no secret and no env of their own', () => {
    for (const name of [policyStep, installStep]) {
      const block = stepBlock(workflowPath, 'import', name)
      assert.doesNotMatch(block, /secrets\.|\$\{\{ (?!steps\.settings\.outputs\.run)/u, name)
      assert.doesNotMatch(block, /^ {8}env:$/mu, name)
    }
    assert.match(stepScript(workflowPath, 'import', policyStep), /^node scripts\/check-npm-policy\.mjs$/mu)
  })
})

describe('the import step', () => {
  // A fixture importer in a site checkout, which records what it was given and exits as the real one does for a
  // stored outcome: 0 for applied or held, 1 for failed or any other outcome.
  function site(exitCode) {
    const root = folder()
    mkdirSync(join(root, 'importer'))
    writeFileSync(join(root, 'importer', 'run.mjs'), [
      "import { writeFileSync } from 'node:fs'",
      "writeFileSync('seen.json', JSON.stringify({ origin: process.env.SITE_ORIGIN, key: process.env.CATALOG_IMPORT_KEY, args: process.argv.slice(2) }))",
      `process.exitCode = ${exitCode}`,
    ].join('\n'))
    return root
  }

  for (const [outcome, exitCode] of [['held or applied', 0], ['failed', 1]]) {
    test(`the job takes the importer's exit ${exitCode} for a stored ${outcome} run`, () => {
      const root = site(exitCode)
      const result = runStep(importStep, { cwd: root, env: { SITE_ORIGIN: origin, CATALOG_IMPORT_KEY: key } })
      assert.equal(result.status, exitCode, result.stderr)
      assert.deepEqual(JSON.parse(readFileSync(join(root, 'seen.json'), 'utf8')), { origin, key, args: [] })
    })
  }

  test('the origin and key reach the importer from env only', () => {
    const block = stepBlock(workflowPath, 'import', importStep)
    assert.match(block, /^ {8}env:\n {10}SITE_ORIGIN: \$\{\{ inputs\.site-origin \}\}\n {10}CATALOG_IMPORT_KEY: \$\{\{ secrets\.import-key \}\}$/mu)
    assert.equal(stepScript(workflowPath, 'import', importStep), 'node importer/run.mjs\n')
  })
})

describe('the workflow runs the site importer with the key in the import step only', () => {
  test('it is called by a site with an optional origin input and an optional key secret', () => {
    assert.match(workflow(), /^on:\n {2}workflow_call:\n {4}inputs:\n {6}site-origin:\n(?: {8}.*\n)* {8}type: string$/mu)
    assert.match(workflow(), /^ {4}secrets:\n {6}import-key:\n(?: {8}.*\n)* {8}required: false$/mu)
    assert.match(workflow(), /^ {6}site-origin:\n(?: {8}.*\n)* {8}default: ''$/mu)
    assert.doesNotMatch(workflow(), /^ {2}(?:schedule|push|pull_request|workflow_dispatch):/mu)
  })

  test('the token reads contents only and the job runs on the pinned hosted image for two hours at most', () => {
    assert.match(workflow(), /^permissions: \{\}$/mu)
    assert.match(job(), /^ {4}permissions:\n {6}contents: read\n {4}\S/mu)
    assert.match(job(), /^ {4}runs-on: ubuntu-26\.04$/mu)
    assert.match(job(), /^ {4}timeout-minutes: 120$/mu)
    assert.match(stepBlock(workflowPath, 'import', 'Check out the site'), /^ {10}persist-credentials: false$/mu)
  })

  test('the steps check the settings, then check out, set up Node, enforce the npm policy, install and import', () => {
    assert.deepEqual(stepNames(), [settingsStep, 'Check out the site', 'Set up Node', policyStep, installStep, importStep])
  })

  test('every later step runs only when the settings step said so', () => {
    assert.ok(steps().length > 1, 'the job has steps after the settings step')
    for (const step of steps().slice(1)) assert.match(step, /^ {8}if: steps\.settings\.outputs\.run == 'true'$/mu)
  })

  test('Node comes from the site .nvmrc with no dependency cache', () => {
    const setup = stepBlock(workflowPath, 'import', 'Set up Node')
    assert.match(setup, /^ {10}node-version-file: \.nvmrc$/mu)
    assert.match(setup, /^ {10}package-manager-cache: false$/mu)
  })

  test('the key appears only in the settings and import steps', () => {
    const holders = steps().filter((step) => step.includes('secrets.')).map((step) => step.match(/^ {6}- name: (.+)$/mu)?.[1])
    assert.deepEqual(holders, [settingsStep, importStep])
    assert.equal(workflow().match(/secrets\.import-key/gu)?.length, 2)
    assert.doesNotMatch(job(), /^ {4}env:/mu)
  })

  test('every action is pinned to a full sha', () => {
    const uses = workflow().match(/^ *(?:- )?uses: .+$/gmu) ?? []
    assert.equal(uses.length, 2)
    for (const line of uses) assert.match(line, /uses: [\w.-]+\/[\w./-]+@[a-f0-9]{40} # v[\d.]+$/u)
  })

  test('no run line interpolates an expression', () => {
    for (const name of [settingsStep, policyStep, installStep, importStep]) assert.doesNotMatch(stepScript(workflowPath, 'import', name), /\$\{\{/u)
    for (const line of workflow().split('\n').filter((text) => /^\s+run: /u.test(text))) assert.doesNotMatch(line, /\$\{\{/u)
  })

  test('the README caller runs weekly and by hand, one run at a time, with a read-only token', () => {
    const section = readFileSync(readmePath, 'utf8').split('\n## site-catalog-import\n')[1]?.split(/\n## /u)[0]
    const caller = section?.match(/```yaml\n([\s\S]+?)```/u)?.[1]
    assert.ok(caller, 'README documents the site-catalog-import caller')
    assert.match(caller, /^on:\n {2}schedule:\n {4}- cron: '5 13 \* \* 1'\n {2}workflow_dispatch:$/mu)
    assert.match(caller, /^permissions:\n {2}contents: read$/mu)
    assert.match(caller, /^concurrency:\n {2}group: catalog-import\n {2}cancel-in-progress: false$/mu)
    assert.match(caller, /^ {4}uses: APKiwiOrg\/ci-workflows\/\.github\/workflows\/site-catalog-import\.yml@<full commit sha>$/mu)
    assert.match(caller, /^ {6}site-origin: \$\{\{ vars\.SITE_ORIGIN \}\}$/mu)
    assert.match(caller, /^ {6}import-key: \$\{\{ secrets\.CATALOG_IMPORT_KEY \}\}$/mu)
  })
})
