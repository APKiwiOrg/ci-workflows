// Run the workflows' config gate and capability steps against a fixture caller, without dependencies.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { calledWorkflowPath, jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflows = [
  [fileURLToPath(new URL('../../.github/workflows/site-ci.yml', import.meta.url)), 'config'],
  [fileURLToPath(new URL('../../.github/workflows/site-deploy.yml', import.meta.url)), 'config'],
]
const folders = []
const values = {
  'app-location': 'out', 'has-form': 'false', 'has-theme': 'false', 'has-accounts': 'false', 'has-catalog': 'false', analytics: 'none',
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
  const implementation = calledWorkflowPath(path, jobId)
  const result = run(folder, stepScript(implementation, 'config', 'Read site config'))
  return { ...result, output: readFileSync(join(folder, 'output'), 'utf8') }
}

function condition(path, jobId, name, outputs) {
  const source = stepBlock(path, jobId, name).match(/^ {8}if: (.+)$/mu)?.[1]
  assert.ok(source, `${name} has a capability condition`)
  return runInNewContext(source.replace(/\.([A-Za-z_]\w*(?:-\w+)+)/gu, "['$1']"), {
    needs: { config: { outputs } },
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
    test('the fixture CLI exports all seven validated job outputs', () => {
      const result = readConfig(path, jobId)
      assert.equal(result.status, 0, result.stderr)
      const implementation = calledWorkflowPath(path, jobId)
      const shared = readFileSync(implementation, 'utf8')
      for (const [key, value] of Object.entries(values)) {
        assert.ok(result.output.includes(`${key}=${value}\n`))
        assert.ok(jobBlock(implementation, 'config').includes(`${key}: \${{ steps.config.outputs.${key} }}`))
        const output = shared.match(new RegExp(`^ {6}${key}:\\n((?: {8}.+\\n)+)`, 'mu'))?.[1]
        assert.ok(output?.includes(`value: \${{ jobs.config.outputs.${key} }}`))
      }
      assert.match(jobBlock(path, jobId), /^ {4}uses: \.\/\.github\/workflows\/site-config\.yml$/mu)
      assert.doesNotMatch(readFileSync(path, 'utf8'), /node scripts\/site-config\.mjs --github-output/u)
      assert.equal(shared.match(/node scripts\/site-config\.mjs --github-output/gu)?.length, 1)
      assert.match(jobBlock(implementation, 'config'), /node-version-file: \.nvmrc/u)
      assert.match(jobBlock(implementation, 'config'), /^ {4}runs-on: ubuntu-26\.04$/mu)
      assert.match(jobBlock(implementation, 'config'), /persist-credentials: false/u)
      assert.doesNotMatch(shared, /npm ci|cache: npm|secrets\.|environment:/u)
      assert.match(jobBlock(path, jobId), /permissions:\n {6}contents: read\n/u)
      assert.match(jobBlock(implementation, 'config'), /permissions:\n {6}contents: read\n/u)
    })

    test('accepts the alternate framework and capability values', () => {
      assert.equal(readConfig(path, jobId, {
        'app-location': 'build/client', 'has-form': 'true', 'has-theme': 'true', 'has-accounts': 'true', 'has-catalog': 'true', analytics: 'cloudflare',
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

    for (const key of Object.keys(values).filter((key) => key !== 'has-catalog')) {
      test(`missing ${key} fails with a reason`, () => {
        const output = { ...values }
        delete output[key]
        const result = readConfig(path, jobId, output)
        assert.notEqual(result.status, 0)
        assert.ok(result.stderr.includes(key), result.stderr)
      })
    }

    for (const [key, invalid] of [
      ['app-location', 'dist'], ['has-form', 'TRUE'], ['has-theme', '0'], ['has-accounts', 'TRUE'],
      ['has-catalog', 'TRUE'], ['has-catalog', ''], ['analytics', 'other'],
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
    for (const id of ['checks', 'repo', 'secrets', 'infra']) {
      assert.match(jobBlock(ciPath, id), /^ {4}needs: config$/mu)
    }
  })

  test('SQL runs only for accounts with full or SQL scope after config validation', () => {
    const source = jobBlock(ciPath, 'checks').match(/^ {6}RUN_SQL: \$\{\{ (.+) \}\}$/mu)?.[1]
    assert.ok(source, 'SQL has a capability and scope condition')
    for (const capability of ['false', 'true']) {
      const outputs = fixtureOutputs(ciPath, 'config', { ...values, 'has-accounts': capability })
      for (const [full, segments, selected] of [[false, ['site'], false], [false, ['sql'], true], [true, [], true]]) {
        assert.equal(runInNewContext(source.replaceAll('outputs.has-accounts', "outputs['has-accounts']"), {
          needs: { config: { outputs } },
          inputs: { full, segments: JSON.stringify(segments) },
          contains: (list, item) => list.includes(item),
          fromJSON: JSON.parse,
        }), capability === 'true' && selected)
      }
    }
  })

  test('CI runs the theme check only when config says it applies', () => {
    const name = "'Test: site'"
    const source = stepBlock(ciPath, 'checks', name).match(/^ {10}HAS_THEME: \$\{\{ (.+) \}\}$/mu)?.[1]
    assert.ok(source, 'CI reads the theme capability')
    for (const capability of ['false', 'true']) {
      const outputs = fixtureOutputs(ciPath, 'config', { ...values, 'has-theme': capability })
      const theme = runInNewContext(source.replaceAll('outputs.has-theme', "outputs['has-theme']"), {
        needs: { config: { outputs } },
      })
      const folder = fixture()
      writeFileSync(join(folder, 'npm'), '#!/bin/sh\nprintf "%s\\n" "$*"\n', { mode: 0o700 })
      const result = run(folder, stepScript(ciPath, 'checks', name), { PATH: `${folder}:${process.env.PATH}`, HAS_THEME: theme })
      assert.equal(result.status, 0, result.stderr)
      assert.equal(result.stdout.split('\n').includes('run check:theme'), capability === 'true')
    }
  })

  test('a green pre-catalog rollback skips snapshot capture and runs the ordinary build', () => {
    const legacy = { ...values }
    delete legacy['has-catalog']
    const outputs = fixtureOutputs(deployPath, 'config', legacy)
    assert.deepEqual(outputs, { ...legacy, 'has-catalog': 'false' })
    assert.equal(condition(deployPath, 'build', 'Capture catalog snapshot', outputs), false)
    assert.equal(condition(deployPath, 'build', 'Build site', outputs), true)

    const folder = fixture()
    writeFileSync(join(folder, 'npm'), '#!/bin/sh\nprintf "%s\\n" "$*"\n', { mode: 0o700 })
    const result = run(folder, stepScript(deployPath, 'build', 'Build site'), {
      PATH: `${folder}:${process.env.PATH}`, BUILD_COMMAND: 'npm run build',
    })
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.stdout, 'ci\nrun build\n')
  })

  test('deploy keeps both analytics capability paths', () => {
    const deploy = fixtureOutputs(deployPath, 'config', values)
    assert.equal(condition(deployPath, 'build', 'Build site with analytics', deploy), false)
    assert.equal(condition(deployPath, 'build', 'Build site with analytics', { ...deploy, analytics: 'cloudflare' }), true)
    assert.equal(condition(deployPath, 'build', 'Build site', deploy), true)
    assert.doesNotMatch(stepBlock(deployPath, 'build', 'Build site'), /inputs\.analytics-token|VITE_CLOUDFLARE_ANALYTICS_TOKEN/u)
  })

  test('both callers resolve the same implementation while deploy passes its selected SHA', () => {
    const implementation = calledWorkflowPath(ciPath, 'config')
    assert.equal(calledWorkflowPath(deployPath, 'config'), implementation)
    assert.match(jobBlock(deployPath, 'config'), /^ {4}needs: guard$/mu)
    assert.match(jobBlock(deployPath, 'config'), /checkout-ref: \$\{\{ needs\.guard\.outputs\.sha \}\}/u)
    assert.match(jobBlock(implementation, 'config'), /^ {10}ref: \$\{\{ inputs\.checkout-ref \}\}$/mu)
    assert.doesNotMatch(jobBlock(implementation, 'config'), /repository:/u)
    assert.doesNotMatch(jobBlock(ciPath, 'config'), /checkout-ref:/u)
    assert.doesNotMatch(jobBlock(deployPath, 'guard'), /actions\/checkout|actions\/setup-node|Read site config/u)
  })

  test('api install, typecheck and test use the directory rather than the form flag', () => {
    const folder = fixture()
    writeFileSync(join(folder, 'npm'), '#!/bin/sh\nprintf "%s\\n" "$*" >> "$NPM_LOG"\n', { mode: 0o700 })
    const log = join(folder, 'npm-log')
    const env = { PATH: `${folder}:${process.env.PATH}`, NPM_LOG: log }
    const script = ['Install api', "'Test: site'"].map((name) => stepScript(ciPath, 'checks', name)).join('\n')
    const apiCalls = () => readFileSync(log, 'utf8').trim().split('\n').filter((call) => call.endsWith('--prefix api'))
    assert.equal(run(folder, script, env).status, 0)
    assert.deepEqual(apiCalls(), [])
    mkdirSync(join(folder, 'api'))
    rmSync(log)
    assert.equal(run(folder, script, env).status, 0)
    assert.deepEqual(apiCalls(), ['ci --prefix api', 'run typecheck --prefix api', 'test --prefix api'])
  })

  test('every params file is compiled and a missing or failing one fails the step', () => {
    const folder = fixture()
    const checkout = realpathSync(folder)
    mkdirSync(join(folder, 'compiled'))
    mkdirSync(join(folder, 'infra/params'), { recursive: true })
    for (const name of ['site', 'preview']) writeFileSync(join(folder, `infra/params/${name}.bicepparam`), '')
    writeFileSync(join(folder, 'az'), '#!/bin/sh\nprintf "%s\\n" "$*" >> "$PARAMS_LOG"\nif [ "$4" = "${AZ_FAIL_FILE:-}" ]; then exit 3; fi\n', { mode: 0o700 })
    const log = join(folder, 'params-log')
    const env = { INFRA_PARAMS: values['infra-params'], PATH: `${folder}:${process.env.PATH}`, PARAMS_LOG: log }
    const script = stepScript(ciPath, 'infra', 'Build parameters').replaceAll('/tmp/site-infra', join(folder, 'compiled'))
    const result = run(folder, script, env)
    assert.equal(result.status, 0, result.stderr)
    assert.equal(readFileSync(log, 'utf8'), `bicep build-params --file ${checkout}/infra/params/site.bicepparam --outfile ${folder}/compiled/params-0.json\nbicep build-params --file ${checkout}/infra/params/preview.bicepparam --outfile ${folder}/compiled/params-1.json\n`)
    assert.notEqual(run(folder, script, { ...env, AZ_FAIL_FILE: join(checkout, 'infra/params/preview.bicepparam') }).status, 0)
    rmSync(join(folder, 'infra/params/preview.bicepparam'))
    const missing = run(folder, script, env)
    assert.notEqual(missing.status, 0)
    assert.match(missing.stderr, /infra\/params\/preview\.bicepparam/u)
  })

  test('audit follows a successful install and audits the root and any API package', () => {
    const folder = fixture()
    writeFileSync(join(folder, 'npm'), '#!/bin/sh\nprintf "%s\\n" "$*" >> "$NPM_LOG"\nexit "${NPM_EXIT:-0}"\n', { mode: 0o700 })
    const log = join(folder, 'npm-log')
    const env = { PATH: `${folder}:${process.env.PATH}`, NPM_LOG: log }
    const name = "'Test: audit'"
    const script = stepScript(ciPath, 'checks', name)
    assert.equal(run(folder, script, env).status, 0)
    assert.equal(readFileSync(log, 'utf8'), 'audit --omit=dev --audit-level=high\naudit --audit-level=critical\n')
    mkdirSync(join(folder, 'api'))
    rmSync(log)
    assert.equal(run(folder, script, env).status, 0)
    assert.equal(readFileSync(log, 'utf8'), 'audit --omit=dev --audit-level=high\naudit --audit-level=critical\n'
      + 'audit --omit=dev --audit-level=high --prefix api\naudit --audit-level=critical --prefix api\n')
    rmSync(log)
    assert.notEqual(run(folder, script, { ...env, NPM_EXIT: '1' }).status, 0)
    assert.equal(readFileSync(log, 'utf8'), 'audit --omit=dev --audit-level=high\n')
    const checks = jobBlock(ciPath, 'checks')
    assert.ok(checks.includes('node scripts/check-npm-policy.mjs'))
    assert.ok(checks.indexOf('node scripts/check-npm-policy.mjs') < checks.indexOf('npm ci'))
    assert.match(stepBlock(ciPath, 'checks', name), /inputs\.full && steps\.install-api\.outcome == 'success'/u)
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
