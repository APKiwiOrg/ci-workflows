// Run the workflows' config gate and capability steps against a fixture caller, without dependencies.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflows = [
  [fileURLToPath(new URL('../../.github/workflows/site-ci.yml', import.meta.url)), 'config'],
  [fileURLToPath(new URL('../../.github/workflows/site-deploy.yml', import.meta.url)), 'guard'],
]
const folders = []
const values = {
  'app-location': 'out', 'has-form': 'false', 'has-theme': 'false', analytics: 'none',
  'infra-params': '["infra/params/site.bicepparam","infra/params/preview.bicepparam"]',
}

function fixture() {
  const folder = mkdtempSync(join(tmpdir(), 'ciwf-example-site-'))
  folders.push(folder)
  mkdirSync(join(folder, 'scripts'))
  writeFileSync(join(folder, 'output'), '')
  return folder
}

function run(folder, script, env = {}) {
  return spawnSync('bash', ['-e', '-c', script], {
    cwd: folder, encoding: 'utf8',
    env: { PATH: process.env.PATH, GITHUB_OUTPUT: join(folder, 'output'), ...env },
  })
}

function readConfig(path, jobId, output = values, options = {}) {
  const folder = fixture()
  if (!options.missing) {
    writeFileSync(join(folder, 'scripts/site-config.mjs'), [
      "import { appendFileSync } from 'node:fs'",
      "if (process.argv.slice(2).join(' ') !== '--github-output') process.exit(2)",
      `appendFileSync(process.env.GITHUB_OUTPUT, ${JSON.stringify(Object.entries(output).map(([key, value]) => `${key}=${value}\n`).join('') + (options.extra ?? ''))})`,
      `process.exit(${options.exit ?? 0})`,
    ].join('\n'))
  }
  const result = run(folder, stepScript(path, jobId, 'Read site config'))
  return { ...result, output: readFileSync(join(folder, 'output'), 'utf8') }
}

function condition(path, jobId, name, outputs) {
  const source = stepBlock(path, jobId, name).match(/^ {8}if: (.+)$/mu)?.[1]
  assert.ok(source, `${name} has a capability condition`)
  return runInNewContext(source.replace(/\.([A-Za-z_]\w*(?:-\w+)+)/gu, "['$1']"), {
    needs: { config: { outputs }, guard: { outputs } },
  })
}

function fixtureOutputs(path, jobId, config) {
  const result = readConfig(path, jobId, config)
  assert.equal(result.status, 0, result.stderr)
  return Object.fromEntries(result.output.trim().split('\n').map((line) => {
    const separator = line.indexOf('=')
    return [line.slice(0, separator), line.slice(separator + 1)]
  }))
}

afterEach(() => {
  for (const folder of folders.splice(0)) rmSync(folder, { recursive: true, force: true })
})

for (const [path, jobId] of workflows) {
  describe(`${path.split('/').at(-1)} config gate`, () => {
    test('the fixture CLI exports all five validated job outputs', () => {
      const result = readConfig(path, jobId)
      assert.equal(result.status, 0, result.stderr)
      for (const [key, value] of Object.entries(values)) {
        assert.ok(result.output.includes(`${key}=${value}\n`))
        assert.ok(jobBlock(path, jobId).includes(`${key}: \${{ steps.config.outputs.${key} }}`))
      }
      assert.equal(readFileSync(path, 'utf8').match(/node scripts\/site-config\.mjs --github-output/gu)?.length, 1)
      assert.match(jobBlock(path, jobId), /node-version-file: \.nvmrc/u)
      assert.match(jobBlock(path, jobId), /persist-credentials: false/u)
      assert.doesNotMatch(jobBlock(path, jobId), /npm ci|cache: npm/u)
    })

    test('accepts the alternate framework and capability values', () => {
      assert.equal(readConfig(path, jobId, {
        'app-location': 'build/client', 'has-form': 'true', 'has-theme': 'true', analytics: 'cloudflare',
        'infra-params': '["infra/params/site.bicepparam"]',
      }).status, 0)
    })

    test('a missing CLI fails with its path before install', () => {
      const result = readConfig(path, jobId, values, { missing: true })
      assert.notEqual(result.status, 0)
      assert.match(result.stderr, /scripts\/site-config\.mjs/u)
    })

    test('a failed CLI cannot publish even otherwise valid outputs', () => {
      assert.notEqual(readConfig(path, jobId, values, { exit: 7 }).status, 0)
    })

    for (const key of Object.keys(values)) {
      test(`missing ${key} fails with a reason`, () => {
        const output = { ...values }
        delete output[key]
        const result = readConfig(path, jobId, output)
        assert.notEqual(result.status, 0)
        assert.ok(result.stderr.includes(key), result.stderr)
      })
    }

    for (const [key, invalid] of [
      ['app-location', 'dist'], ['has-form', 'TRUE'], ['has-theme', '0'], ['analytics', 'other'],
      ['infra-params', 'not-json'], ['infra-params', '{}'], ['infra-params', '[]'],
      ['infra-params', '[null]'], ['infra-params', '["/tmp/site.bicepparam"]'],
      ['infra-params', '["../site.bicepparam"]'], ['infra-params', '["infra/../site.bicepparam"]'],
      ['infra-params', '["infra/site.json"]'], ['infra-params', '["infra/site.bicepparam", "infra/preview.bicepparam"]'],
    ]) {
      test(`rejects invalid ${key} ${invalid}`, () => {
        const result = readConfig(path, jobId, { ...values, [key]: invalid })
        assert.notEqual(result.status, 0)
        assert.ok(result.stderr.includes(key), result.stderr)
      })
    }

    test('duplicate config values cannot hide an invalid first value', () => {
      const result = readConfig(path, jobId, { ...values, 'app-location': 'dist' }, { extra: 'app-location=out\n' })
      assert.notEqual(result.status, 0)
    })
  })
}

const [ciPath] = workflows[0]
const [deployPath] = workflows[1]

describe('capabilities control the consumer steps', () => {
  test('every CI job depends on validated config before any install', () => {
    for (const id of ['checks', 'repo', 'secrets', 'infra', 'audit']) {
      assert.match(jobBlock(ciPath, id), /^ {4}needs: config$/mu)
    }
    assert.match(jobBlock(ciPath, 'e2e'), /^ {4}needs: \[config, checks\]$/mu)
  })

  test('theme and token builds only run for their capabilities', () => {
    const plain = fixtureOutputs(ciPath, 'config', values)
    const themed = fixtureOutputs(ciPath, 'config', { ...values, 'has-theme': 'true', analytics: 'cloudflare' })
    assert.equal(condition(ciPath, 'checks', 'Theme conventions', plain), false)
    assert.equal(condition(ciPath, 'checks', 'Theme conventions', themed), true)
    for (const id of ['checks', 'e2e']) {
      assert.equal(condition(ciPath, id, 'Build with analytics', plain), false)
      assert.equal(condition(ciPath, id, 'Build with analytics', themed), true)
      assert.equal(condition(ciPath, id, 'Build', plain), true)
      assert.equal(condition(ciPath, id, 'Build', themed), false)
    }
    const deploy = fixtureOutputs(deployPath, 'guard', values)
    assert.equal(condition(deployPath, 'build', 'Build site with analytics', deploy), false)
    assert.equal(condition(deployPath, 'build', 'Build site with analytics', { ...deploy, analytics: 'cloudflare' }), true)
    assert.equal(condition(deployPath, 'build', 'Build site', deploy), true)
    assert.doesNotMatch(stepBlock(deployPath, 'build', 'Build site'), /inputs\.analytics-token|VITE_CLOUDFLARE_ANALYTICS_TOKEN/u)
  })

  test('api install, typecheck and test use the directory rather than the form flag', () => {
    const folder = fixture()
    writeFileSync(join(folder, 'npm'), '#!/bin/sh\nprintf "%s\\n" "$*" >> "$NPM_LOG"\n', { mode: 0o700 })
    const log = join(folder, 'npm-log')
    const env = { PATH: `${folder}:${process.env.PATH}`, NPM_LOG: log }
    const script = ['Install api', 'Typecheck api', 'Test api'].map((name) => stepScript(ciPath, 'checks', name)).join('\n')
    assert.equal(run(folder, script, env).status, 0)
    assert.throws(() => readFileSync(log), { code: 'ENOENT' })
    mkdirSync(join(folder, 'api'))
    assert.equal(run(folder, script, env).status, 0)
    assert.equal(readFileSync(log, 'utf8'), 'ci --prefix api\nrun typecheck --prefix api\ntest --prefix api\n')
  })

  test('every params file is compiled and a missing or failing one fails the step', () => {
    const folder = fixture()
    mkdirSync(join(folder, 'infra/params'), { recursive: true })
    for (const name of ['site', 'preview']) writeFileSync(join(folder, `infra/params/${name}.bicepparam`), '')
    writeFileSync(join(folder, 'az'), '#!/bin/sh\nprintf "%s\\n" "$*" >> "$PARAMS_LOG"\nif [ "$4" = "${AZ_FAIL_FILE:-}" ]; then exit 3; fi\n', { mode: 0o700 })
    const log = join(folder, 'params-log')
    const env = { INFRA_PARAMS: values['infra-params'], PATH: `${folder}:${process.env.PATH}`, PARAMS_LOG: log }
    const script = stepScript(ciPath, 'infra', 'Build parameters')
    const result = run(folder, script, env)
    assert.equal(result.status, 0, result.stderr)
    assert.equal(readFileSync(log, 'utf8'), 'bicep build-params --file infra/params/site.bicepparam --stdout\nbicep build-params --file infra/params/preview.bicepparam --stdout\n')
    assert.notEqual(run(folder, script, { ...env, AZ_FAIL_FILE: 'infra/params/preview.bicepparam' }).status, 0)
    rmSync(join(folder, 'infra/params/preview.bicepparam'))
    const missing = run(folder, script, env)
    assert.notEqual(missing.status, 0)
    assert.match(missing.stderr, /infra\/params\/preview\.bicepparam/u)
  })

  test('audit installs under policy and audits root and an api directory only when present', () => {
    const folder = fixture()
    writeFileSync(join(folder, 'npm'), '#!/bin/sh\nprintf "%s\\n" "$*" >> "$NPM_LOG"\nexit "${NPM_EXIT:-0}"\n', { mode: 0o700 })
    const log = join(folder, 'npm-log')
    const env = { PATH: `${folder}:${process.env.PATH}`, NPM_LOG: log }
    const script = stepScript(ciPath, 'audit', 'Install and audit')
    assert.equal(run(folder, script, env).status, 0)
    assert.equal(readFileSync(log, 'utf8'), 'ci\naudit --audit-level=high\n')
    mkdirSync(join(folder, 'api'))
    rmSync(log)
    assert.equal(run(folder, script, env).status, 0)
    assert.equal(readFileSync(log, 'utf8'), 'ci\naudit --audit-level=high\nci --prefix api\naudit --audit-level=high --prefix api\n')
    rmSync(log)
    assert.notEqual(run(folder, script, { ...env, NPM_EXIT: '1' }).status, 0)
    assert.equal(readFileSync(log, 'utf8'), 'ci\n')
    const audit = jobBlock(ciPath, 'audit')
    assert.ok(audit.indexOf('node scripts/check-npm-policy.mjs') < audit.indexOf('npm ci'))
  })

  test('deploy discovers an api directory independently of form capability', () => {
    const folder = fixture()
    const script = stepScript(deployPath, 'deploy', 'Detect api')
    assert.equal(run(folder, script).status, 0)
    assert.equal(readFileSync(join(folder, 'output'), 'utf8'), 'location=\n')
    mkdirSync(join(folder, 'api'))
    writeFileSync(join(folder, 'output'), '')
    assert.equal(run(folder, script).status, 0)
    assert.equal(readFileSync(join(folder, 'output'), 'utf8'), 'location=api\n')
  })
})
