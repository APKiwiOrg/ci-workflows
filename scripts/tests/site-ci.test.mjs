// Checks site-ci.yml hardening and the wiring from validated config to each consumer job.
// The template drift step is run for real against a stub checker.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-ci.yml', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = (id) => jobBlock(workflowPath, id)
const jobIds = ['config', 'checks', 'repo', 'secrets', 'infra', 'e2e', 'audit']
const apiSteps = ['Install api', 'Typecheck api', 'Test api']
const driftStep = 'Site template drift'
const folders = []

const step = (jobId, name) => stepBlock(workflowPath, jobId, name)

describe('every job is hardened', () => {
  test('every checkout sets persist-credentials false', () => {
    const checkouts = workflow().match(/uses: actions\/checkout@.+\n(?: {8,}.+\n)*/gu) ?? []
    assert.equal(checkouts.length, jobIds.length)
    for (const checkout of checkouts) assert.match(checkout, /^ {10}persist-credentials: false$/mu)
  })

  test('every uses is pinned to a full sha', () => {
    const uses = workflow().match(/^ *(?:- )?uses: .+$/gmu) ?? []
    assert.ok(uses.length > 0)
    for (const line of uses) assert.match(line, /uses: [\w.-]+\/[\w./-]+@[a-f0-9]{40} # v[\d.]+$/u)
  })

  test('every job runs on the pinned hosted image', () => {
    for (const id of jobIds) assert.match(job(id), /^ {4}runs-on: ubuntu-26\.04$/mu)
  })

  test('the secrets job keeps the full history for gitleaks', () => {
    assert.match(job('secrets'), /^ {10}fetch-depth: 0$/mu)
  })

  test('no run line interpolates an expression', () => {
    assert.doesNotMatch(workflow(), /^ *(?:- )?run: .*\$\{\{/mu)
    for (const block of workflow().match(/run: \|\n(?: {10,}.*\n|\n)+/gu) ?? []) {
      assert.doesNotMatch(block, /\$\{\{/u)
    }
  })
})

describe('config controls the site layout', () => {
  test('every api step checks for the api directory', () => {
    for (const name of apiSteps) assert.match(step('checks', name), /if \[ -d api \]/u)
    assert.doesNotMatch(workflow(), /inputs\.has-api/u)
  })

  test('e2e and infra are required after config validation', () => {
    for (const id of ['e2e', 'infra']) assert.doesNotMatch(job(id), /^ {4}if:/mu)
  })

  test('the checks and browser tests receive a token only for cloudflare', () => {
    assert.doesNotMatch(workflow(), /^env:/mu)
    for (const id of ['checks', 'e2e']) {
      assert.match(step(id, 'Build with analytics'), /if: needs\.config\.outputs\.analytics == 'cloudflare'/u)
      assert.match(step(id, 'Build with analytics'), /VITE_CLOUDFLARE_ANALYTICS_TOKEN: \$\{\{ inputs\.analytics-test-token \}\}/u)
      assert.match(step(id, 'Build'), /if: needs\.config\.outputs\.analytics == 'none'/u)
    }
  })

  test('the infra job compiles the template input and all config params through env', () => {
    const template = step('infra', 'Build template')
    assert.match(template, /BICEP_FILE: \$\{\{ inputs\.bicep-file \}\}/u)
    assert.match(template, /az bicep build --file "\$BICEP_FILE"/u)
    assert.match(step('infra', 'Build parameters'), /INFRA_PARAMS: \$\{\{ needs\.config\.outputs\.infra-params \}\}/u)
    assert.match(template, /--outfile \/tmp\/site-infra\/main\.json/u)
    assert.match(job('infra'), /node scripts\/check-infra\.mjs \/tmp\/site-infra\/main\.json/u)
  })

  test('the asset budget checks the config app-location through env', () => {
    const budget = step('checks', 'Asset budget')
    assert.match(budget, /APP_LOCATION: \$\{\{ needs\.config\.outputs\.app-location \}\}/u)
    assert.match(budget, /run: node scripts\/check-assets\.mjs "\$APP_LOCATION"$/mu)
  })

  test('only operational inputs remain', () => {
    const inputs = workflow().split('    inputs:\n')[1].split('\npermissions:')[0]
    assert.deepEqual(inputs.match(/^ {6}[a-z-]+:/gmu)?.map((line) => line.trim()), [
      'node-version-file:', 'analytics-test-token:', 'bicep-file:',
    ])
    for (const id of ['checks', 'repo', 'infra', 'e2e', 'audit']) {
      assert.match(job(id), /node-version-file: \$\{\{ inputs\.node-version-file \}\}/u)
    }
  })
})

// Runs the drift step in a temp site under bash -e, the shell GitHub uses for a run step with no shell set.
// The stub checker exits with the given code. A null exit leaves the site without a lock.
function runDrift(checkerExit) {
  const folder = mkdtempSync(join(tmpdir(), 'ciwf-site-ci-'))
  folders.push(folder)
  mkdirSync(join(folder, 'scripts'))
  writeFileSync(join(folder, 'scripts', 'check-site-template.mjs'),
    `console.log('stub checker ran')\nprocess.exit(${checkerExit ?? 0})\n`)
  if (checkerExit !== null) writeFileSync(join(folder, 'site-template.lock.json'), '{}\n')
  return spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'repo', driftStep)], {
    cwd: folder, encoding: 'utf8', env: { PATH: process.env.PATH },
  })
}

afterEach(() => {
  for (const folder of folders.splice(0)) rmSync(folder, { recursive: true, force: true })
})

describe('the repo job fails on template drift', () => {
  test('a site with a lock fails when the checker fails', () => {
    const result = runDrift(1)
    assert.equal(result.status, 1)
    assert.equal(result.stdout, 'stub checker ran\n')
  })

  test('a site with a lock passes when the checker passes', () => {
    const result = runDrift(0)
    assert.equal(result.status, 0)
    assert.equal(result.stdout, 'stub checker ran\n')
  })

  test('a repository with no lock prints one skip line and passes', () => {
    const result = runDrift(null)
    assert.equal(result.status, 0)
    assert.equal(result.stdout, 'Site template drift: no site-template.lock.json, skipping\n')
    assert.equal(result.stderr, '')
  })
})
