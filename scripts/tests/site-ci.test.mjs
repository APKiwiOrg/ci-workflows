// Checks site-ci.yml statically: the hardening every job keeps and the inputs that switch the optional work.
// A static test cannot watch GitHub skip a job, so a switch is proven by the if that reads its input.
// The template drift step is run for real against a stub checker.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-ci.yml', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = (id) => jobBlock(workflowPath, id)
const jobIds = ['checks', 'repo', 'secrets', 'infra', 'e2e']
const apiSteps = ['Install api', 'Typecheck api', 'Test api']
const driftStep = 'Site template drift'
const folders = []

// The lines of one step, from its "- name:" line to the next step.
function step(jobId, name) {
  const block = job(jobId)
  const start = block.indexOf(`- name: ${name}\n`)
  assert.ok(start >= 0, `job ${jobId} has step ${name}`)
  const next = block.indexOf('\n      - ', start)
  return next < 0 ? block.slice(start) : block.slice(start, next + 1)
}

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

describe('inputs switch the optional work', () => {
  test('the e2e job if references inputs.has-e2e', () => {
    assert.match(job('e2e'), /^ {4}if: inputs\.has-e2e$/mu)
  })

  test('the infra job if references inputs.has-infra', () => {
    assert.match(job('infra'), /^ {4}if: inputs\.has-infra$/mu)
  })

  test('the api steps if references inputs.has-api', () => {
    for (const name of apiSteps) assert.match(step('checks', name), /^ {8}if: inputs\.has-api$/mu)
    const gated = job('checks').match(/inputs\.has-api/gu) ?? []
    assert.equal(gated.length, apiSteps.length)
  })

  test('the core jobs always run', () => {
    for (const id of ['checks', 'repo', 'secrets']) assert.doesNotMatch(job(id), /^ {4}if:/mu)
  })

  test('the build step passes analytics-test-token as VITE_CLOUDFLARE_ANALYTICS_TOKEN', () => {
    // Workflow-level env, so the build, the unit tests and the browser smoke all see the fixture token.
    assert.match(workflow(), /^env:\n(?: {2}#.*\n)* {2}VITE_CLOUDFLARE_ANALYTICS_TOKEN: \$\{\{ inputs\.analytics-test-token \}\}$/mu)
    assert.equal(workflow().match(/VITE_CLOUDFLARE_ANALYTICS_TOKEN/gu)?.length, 1)
    assert.match(step('checks', 'Build'), /run: npm run build/u)
    assert.match(step('e2e', 'Build'), /run: npm run build/u)
  })

  test('the infra job compiles the bicep inputs through env', () => {
    const template = step('infra', 'Build template')
    assert.match(template, /BICEP_FILE: \$\{\{ inputs\.bicep-file \}\}/u)
    assert.match(template, /az bicep build --file "\$BICEP_FILE"/u)
    const params = step('infra', 'Build parameters')
    assert.match(params, /BICEP_PARAMS: \$\{\{ inputs\.bicep-params \}\}/u)
    assert.match(params, /az bicep build-params --file "\$BICEP_PARAMS"/u)
  })

  test('the asset budget checks the app-location folder through env', () => {
    const budget = step('checks', 'Asset budget')
    assert.match(budget, /^ {10}APP_LOCATION: \$\{\{ inputs\.app-location \}\}$/mu)
    assert.match(budget, /run: node scripts\/check-assets\.mjs "\$APP_LOCATION"$/mu)
    assert.doesNotMatch(workflow(), /check-assets\.mjs build\/client/u)
  })

  test('the infra job compiles into a scratch folder named for no one site', () => {
    const infra = job('infra')
    assert.doesNotMatch(workflow(), /web-website/u)
    assert.match(step('infra', 'Build template'), /--outfile \/tmp\/site-infra\/main\.json/u)
    assert.match(infra, /node scripts\/check-infra\.mjs \/tmp\/site-infra\/main\.json/u)
  })

  test('the inputs default to the APKiwi web layout', () => {
    const defaults = {
      'node-version-file': ['.nvmrc', 'string'],
      'app-location': ['build/client', 'string'],
      'has-api': ['true', 'boolean'],
      'has-e2e': ['true', 'boolean'],
      'has-infra': ['true', 'boolean'],
      'analytics-test-token': ["'00000000000000000000000000000000'", 'string'],
      'bicep-file': ['infra/main.bicep', 'string'],
      'bicep-params': ['infra/params/site.bicepparam', 'string'],
    }
    for (const [name, [value, type]] of Object.entries(defaults)) {
      const input = workflow().match(new RegExp(`^ {6}${name}:\\n((?: {8}.+\\n)+)`, 'mu'))?.[1]
      assert.ok(input, `workflow_call declares ${name}`)
      assert.match(input, new RegExp(`^ {8}default: ${value.replace(/\./gu, '\\.')}$`, 'mu'))
      assert.match(input, new RegExp(`^ {8}type: ${type}$`, 'mu'))
    }
    for (const id of ['checks', 'repo', 'infra', 'e2e']) {
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
