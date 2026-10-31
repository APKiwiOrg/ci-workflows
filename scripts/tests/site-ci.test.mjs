// Checks site-ci.yml statically: the hardening every job keeps and the inputs that switch the optional work.
// A static test cannot watch GitHub skip a job, so a switch is proven by the if that reads its input.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-ci.yml', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = (id) => jobBlock(workflowPath, id)
const jobIds = ['checks', 'repo', 'secrets', 'infra', 'e2e']
const apiSteps = ['Install api', 'Typecheck api', 'Test api']

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

  test('the inputs default to the APKiwi web layout', () => {
    const defaults = {
      'node-version-file': ['.nvmrc', 'string'],
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
