// Run the infra steps with a local compiler and checker to verify their file and process boundaries.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { afterEach, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-ci.yml', import.meta.url))
const folders = []
const primary = 'infra/params/primary/settings.bicepparam'
const secondary = 'infra/params/secondary/settings.bicepparam'
const third = 'infra/params/third/settings.bicepparam'

function runInfra(paths = ['infra/params/site.bicepparam'], options = {}) {
  const base = realpathSync(mkdtempSync(join(tmpdir(), 'ciwf-infra-')))
  folders.push(base)
  const folder = join(base, 'checkout')
  mkdirSync(folder)
  const compiled = join(folder, 'compiled')
  const log = join(folder, 'calls.jsonl')
  mkdirSync(join(folder, 'scripts'))
  writeFileSync(log, '')
  const template = options.template ?? 'infra/main.bicep'
  mkdirSync(dirname(join(folder, template)), { recursive: true })
  writeFileSync(join(folder, template), '')
  for (const [index, path] of paths.entries()) {
    if (path === options.missing) continue
    mkdirSync(dirname(join(folder, path)), { recursive: true })
    const target = path === options.symlink ? join(base, 'outside.bicepparam') : join(folder, path)
    writeFileSync(target, JSON.stringify({ parameters: { fixture: { value: index } } }))
    if (path === options.symlink) symlinkSync(target, join(folder, path))
  }
  writeFileSync(join(folder, 'az'), `#!/usr/bin/env node
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs'
const args = process.argv.slice(2)
if (args[0] !== 'bicep') process.exit(2)
const path = args[args.indexOf('--file') + 1]
if (!path || path.startsWith('-')) {
  console.error('--file: expected one argument')
  process.exit(2)
}
if (args[1] === 'build') {
  appendFileSync(process.env.INFRA_LOG, JSON.stringify({ kind: 'template', args }) + '\\n')
  writeFileSync(args[args.indexOf('--outfile') + 1], JSON.stringify({ resources: [] }))
} else if (args[1] === 'build-params') {
  appendFileSync(process.env.INFRA_LOG, JSON.stringify({ kind: 'compile', args }) + '\\n')
  if (path === process.env.COMPILE_FAIL) process.exit(3)
  const parameters = {
    $schema: 'deploymentParameters.json#', contentVersion: '1.0.0.0',
    ...JSON.parse(readFileSync(path, 'utf8')),
  }
  const wrapper = { parametersJson: JSON.stringify(parameters), templateJson: '{}', templateSpecId: null }
  let output = JSON.stringify(parameters)
  if (path === process.env.INVALID_FILE) {
    const shape = process.env.INVALID_SHAPE
    if (shape === 'wrapper') output = JSON.stringify(wrapper)
    else if (shape === 'invalid-json') output = '{'
    else if (shape === 'missing') output = '{}'
    else if (shape === 'root-null') output = 'null'
    else output = JSON.stringify({ parameters: JSON.parse(shape) })
  }
  if (args.includes('--stdout')) process.stdout.write(JSON.stringify(wrapper))
  else if (args.includes('--outfile')) writeFileSync(args[args.indexOf('--outfile') + 1], output)
  else process.exit(2)
} else process.exit(2)
`, { mode: 0o700 })
  writeFileSync(join(folder, 'scripts/check-infra.mjs'), `
import { appendFileSync, readFileSync } from 'node:fs'
const args = process.argv.slice(2)
const template = JSON.parse(readFileSync(args[0], 'utf8'))
const parameters = args[1] ? JSON.parse(readFileSync(args[1], 'utf8')) : null
appendFileSync(process.env.INFRA_LOG, JSON.stringify({ kind: 'check', args, template, parameters }) + '\\n')
if (args.length !== 2) process.exit(9)
if (String(parameters.parameters.fixture.value) === process.env.POLICY_FAIL) process.exit(7)
`)
  let result
  for (const name of ['Build template', 'Build parameters', 'Infrastructure policy']) {
    const inline = stepBlock(workflowPath, 'infra', name).match(/^ {8}run: ([^|\n].*)$/mu)?.[1]
    const script = (inline ?? stepScript(workflowPath, 'infra', name)).replaceAll('/tmp/site-infra', compiled)
    result = spawnSync('bash', ['-e', '-c', script], {
      cwd: folder, encoding: 'utf8',
      env: {
        PATH: `${folder}:${process.env.PATH}`, BICEP_FILE: template,
        INFRA_PARAMS: JSON.stringify(paths), INFRA_LOG: log,
        COMPILE_FAIL: options.compileFail ? join(folder, options.compileFail) : '', POLICY_FAIL: String(options.policyFail ?? ''),
        INVALID_FILE: options.invalidFile ? join(folder, options.invalidFile) : '', INVALID_SHAPE: options.invalidShape ?? '',
      },
    })
    if (result.status !== 0) break
  }
  const calls = readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line))
  return { ...result, folder, compiled, calls }
}

afterEach(() => {
  for (const folder of folders.splice(0)) rmSync(folder, { recursive: true, force: true })
})

test('each params file has a distinct compiled output and receives its own policy check in order', () => {
  const result = runInfra([primary, secondary])
  assert.equal(result.status, 0, result.stderr)
  assert.equal(statSync(result.compiled).mode & 0o777, 0o700)
  const compiles = result.calls.filter(({ kind }) => kind === 'compile')
  assert.deepEqual(compiles.map(({ args }) => args), [
    ['bicep', 'build-params', '--file', join(result.folder, primary), '--outfile', join(result.compiled, 'params-0.json')],
    ['bicep', 'build-params', '--file', join(result.folder, secondary), '--outfile', join(result.compiled, 'params-1.json')],
  ])
  const checks = result.calls.filter(({ kind }) => kind === 'check')
  assert.equal(checks.length, 2)
  const outputs = checks.map(({ args }) => {
    assert.equal(args.length, 2)
    assert.equal(args[0], join(result.compiled, 'main.json'))
    assert.equal(dirname(args[1]), result.compiled)
    return args[1]
  })
  assert.notEqual(outputs[0], outputs[1])
  assert.equal(readdirSync(result.compiled).length, 3)
  for (const [index, check] of checks.entries()) {
    assert.equal(statSync(outputs[index]).mode & 0o777, 0o600)
    assert.deepEqual(check.template, { resources: [] })
    assert.deepEqual(check.parameters, { $schema: 'deploymentParameters.json#', contentVersion: '1.0.0.0', parameters: { fixture: { value: index } } })
    assert.deepEqual(JSON.parse(readFileSync(outputs[index], 'utf8')), check.parameters)
  }
})

test('a failing second policy check names its params file and stops before checking later files', () => {
  const result = runInfra([primary, secondary, third], { policyFail: 1 })
  assert.equal(result.status, 7, result.stderr)
  assert.ok(result.stderr.includes(secondary), result.stderr)
  assert.deepEqual(result.calls.filter(({ kind }) => kind === 'check').map(({ parameters }) => parameters.parameters.fixture.value), [0, 1])
})

test('the default single params file is compiled once and checked once with two arguments', () => {
  const result = runInfra()
  assert.equal(result.status, 0, result.stderr)
  const calls = result.calls.filter(({ kind }) => kind !== 'template')
  assert.deepEqual(calls.map(({ kind }) => kind), ['compile', 'check'])
  assert.deepEqual(calls[0].args, ['bicep', 'build-params', '--file', join(result.folder, 'infra/params/site.bicepparam'), '--outfile', join(result.compiled, 'params-0.json')])
  assert.equal(calls[1].args.length, 2)
  assert.equal(calls[1].args[0], join(result.compiled, 'main.json'))
  assert.deepEqual(calls[1].parameters, { $schema: 'deploymentParameters.json#', contentVersion: '1.0.0.0', parameters: { fixture: { value: 0 } } })
})

test('a params compilation failure names the file and stops before later compiles or policy checks', () => {
  const result = runInfra([primary, secondary, third], { compileFail: secondary })
  assert.equal(result.status, 3, result.stderr)
  assert.ok(result.stderr.includes(secondary), result.stderr)
  assert.deepEqual(result.calls.filter(({ kind }) => kind !== 'template').map(({ kind, args }) => [kind, args[3]]), [
    ['compile', join(result.folder, primary)], ['compile', join(result.folder, secondary)],
  ])
})

test('a missing params file fails before compiling parameters or checking policy', () => {
  const result = runInfra([primary, secondary], { missing: secondary })
  assert.notEqual(result.status, 0)
  assert.ok(result.stderr.includes(secondary), result.stderr)
  assert.deepEqual(result.calls.map(({ kind }) => kind), ['template'])
  assert.ok(existsSync(join(result.compiled, 'main.json')))
})

for (const path of ['-leading.bicepparam', 'infra/params/with spaces.bicepparam']) {
  test(`the params compiler receives an absolute single argument for ${path}`, () => {
    const result = runInfra([path])
    assert.equal(result.status, 0, result.stderr)
    const compile = result.calls.find(({ kind }) => kind === 'compile')
    assert.deepEqual(compile.args, ['bicep', 'build-params', '--file', join(result.folder, path), '--outfile', join(result.compiled, 'params-0.json')])
    assert.deepEqual(result.calls.find(({ kind }) => kind === 'check').parameters, { $schema: 'deploymentParameters.json#', contentVersion: '1.0.0.0', parameters: { fixture: { value: 0 } } })
  })
}

for (const path of ['../outside.bicepparam', '../checkout-sibling/outside.bicepparam', 'linked.bicepparam']) {
  test(`the params compiler refuses a checkout escape through ${path}`, () => {
    const result = runInfra([path], { symlink: path === 'linked.bicepparam' ? path : undefined })
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /outside.*checkout/iu)
    assert.deepEqual(result.calls.map(({ kind }) => kind), ['template'])
  })
}

for (const path of ['-leading.bicep', 'infra/with spaces.bicep']) {
  test(`the template compiler receives an absolute single argument for ${path}`, () => {
    const result = runInfra(undefined, { template: path })
    assert.equal(result.status, 0, result.stderr)
    assert.deepEqual(result.calls[0].args, ['bicep', 'build', '--file', join(result.folder, path), '--outfile', join(result.compiled, 'main.json')])
  })
}

test('the template compiler refuses a path outside the checkout before invoking the CLI', () => {
  const result = runInfra(undefined, { template: '../outside.bicep' })
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /outside.*checkout/iu)
  assert.deepEqual(result.calls, [])
})

for (const shape of ['wrapper', 'invalid-json', 'missing', 'root-null', 'null', '[]', 'false', '"value"']) {
  test(`compiled params with ${shape} shape fail before checking that file or later files`, () => {
    const result = runInfra([primary, secondary, third], { invalidFile: secondary, invalidShape: shape })
    assert.equal(result.status, 1, result.stderr)
    assert.ok(result.stderr.includes(secondary), result.stderr)
    assert.ok(result.stderr.includes(join(result.compiled, 'params-1.json')), result.stderr)
    assert.match(result.stderr, /invalid compiled infrastructure params/iu)
    assert.deepEqual(result.calls.filter(({ kind }) => kind === 'check').map(({ parameters }) => parameters.parameters.fixture.value), [0])
  })
}
