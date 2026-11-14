// Checks site-ci.yml hardening and the wiring from validated config to each consumer job.
// The template drift step is run for real against a stub checker.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-ci.yml', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = (id) => jobBlock(workflowPath, id)
const jobIds = ['checks', 'repo', 'secrets', 'infra', 'e2e', 'audit', 'sql']
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
    for (const line of uses) {
      if (line.includes('uses: ./')) assert.equal(line.trim(), 'uses: ./.github/workflows/site-config.yml')
      else assert.match(line, /uses: [\w.-]+\/[\w./-]+@[a-f0-9]{40} # v[\d.]+$/u)
    }
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
    for (const name of ['Build parameters', 'Infrastructure policy']) {
      assert.match(step('infra', name), /INFRA_PARAMS: \$\{\{ needs\.config\.outputs\.infra-params \}\}/u)
    }
  })

  test('the asset budget checks the config app-location through env', () => {
    const budget = step('checks', 'Asset budget')
    assert.match(budget, /APP_LOCATION: \$\{\{ needs\.config\.outputs\.app-location \}\}/u)
  })

  test('only operational inputs remain', () => {
    const inputs = workflow().split('    inputs:\n')[1].split('\npermissions:')[0]
    assert.deepEqual(inputs.match(/^ {6}[a-z-]+:/gmu)?.map((line) => line.trim()), [
      'node-version-file:', 'analytics-test-token:', 'bicep-file:',
    ])
    for (const id of ['checks', 'repo', 'infra', 'e2e', 'audit', 'sql']) {
      assert.match(job(id), /node-version-file: \$\{\{ inputs\.node-version-file \}\}/u)
    }
  })
})

// Run the shipped startup script with Docker and sleep replaced at the process boundary.
// Login outcomes are deterministic and no container or real wait is needed locally.
function runSql({ readyAfter = 1, startExit = 0 } = {}) {
  const folder = mkdtempSync(join(tmpdir(), 'ciwf-sql-'))
  folders.push(folder)
  const log = join(folder, 'docker-log')
  const waits = join(folder, 'waits')
  const githubEnv = join(folder, 'github-env')
  writeFileSync(log, '')
  writeFileSync(waits, '')
  writeFileSync(githubEnv, '')
  writeFileSync(join(folder, 'docker'), `#!${process.execPath}
import { appendFileSync, readFileSync } from 'node:fs'
const args = process.argv.slice(2)
appendFileSync(process.env.DOCKER_LOG, JSON.stringify({
  args, password: process.env.MSSQL_SA_PASSWORD, loginPassword: process.env.SQLCMDPASSWORD,
}) + '\\n')
if (args[0] === 'run') process.exit(Number(process.env.START_EXIT))
if (args[0] !== 'exec') process.exit(2)
const attempts = readFileSync(process.env.DOCKER_LOG, 'utf8').trim().split('\\n')
  .map(JSON.parse).filter(({ args }) => args[0] === 'exec').length
process.exit(attempts >= Number(process.env.READY_AFTER) ? 0 : 1)
`, { mode: 0o700 })
  writeFileSync(join(folder, 'sleep'), '#!/bin/sh\nprintf "%s\\n" "$*" >> "$WAITS"\n', { mode: 0o700 })
  const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'sql', 'Start SQL Server')], {
    cwd: folder, encoding: 'utf8', env: {
      PATH: `${folder}:${process.env.PATH}`, GITHUB_ENV: githubEnv, DOCKER_LOG: log, WAITS: waits,
      READY_AFTER: String(readyAfter), START_EXIT: String(startExit),
    },
  })
  return {
    ...result,
    events: readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse),
    waits: readFileSync(waits, 'utf8').trim().split('\n').filter(Boolean),
    environment: readFileSync(githubEnv, 'utf8'),
  }
}

describe('accounts SQL integration job', () => {
  test('starts a digest-pinned SQL Server 2022 with a masked random credential before publishing the connection', () => {
    const result = runSql({ readyAfter: 3 })
    assert.equal(result.status, 0, result.stderr)
    const [start, ...logins] = result.events
    assert.equal(start.args[0], 'run')
    assert.ok(start.args.includes('-d'))
    assert.ok(start.args.includes('ACCEPT_EULA=Y'))
    assert.ok(start.args.includes('127.0.0.1:1433:1433'))
    assert.match(start.args.at(-1), /^mcr\.microsoft\.com\/mssql\/server@sha256:[a-f0-9]{64}$/u)
    assert.match(stepScript(workflowPath, 'sql', 'Start SQL Server'), /mcr\.microsoft\.com\/mssql\/server@sha256:[a-f0-9]{64} # 2022-latest$/mu)
    assert.doesNotMatch(job('sql'), /^ {4}services:/mu)
    assert.ok(start.password.length >= 8 && start.password.length <= 128)
    for (const pattern of [/[A-Z]/u, /[a-z]/u, /[0-9]/u, /[^A-Za-z0-9]/u]) assert.match(start.password, pattern)
    assert.match(stepScript(workflowPath, 'sql', 'Start SQL Server'), /openssl rand/u)
    assert.equal(result.stdout, `::add-mask::${start.password}\n`)
    assert.equal(result.stderr, '')
    assert.equal(logins.length, 3)
    for (const login of logins) {
      assert.ok(login.args.includes('/opt/mssql-tools18/bin/sqlcmd'))
      assert.ok(login.args.includes('-C'))
      assert.ok(login.args.includes('-b'))
      assert.ok(login.args.includes('SQLCMDPASSWORD'))
      assert.equal(login.loginPassword, start.password)
    }
    assert.deepEqual(result.waits, ['2', '2'])
    assert.equal(result.environment,
      `ACCOUNTS_SQL_TEST_SERVER=Server=127.0.0.1,1433;User Id=sa;Password=${start.password};Encrypt=true;TrustServerCertificate=true\n`)
  })

  test('fails after 30 unanswered login attempts without publishing a connection', () => {
    const result = runSql({ readyAfter: 31 })
    assert.equal(result.status, 1)
    assert.equal(result.events.filter(({ args }) => args[0] === 'exec').length, 30)
    assert.deepEqual(result.waits, Array(29).fill('2'))
    assert.equal(result.environment, '')
    assert.equal(result.stdout, `::add-mask::${result.events[0].password}\n`)
    assert.ok(!result.stderr.includes(result.events[0].password))
  })

  test('a container start failure stops before login or connection publication', () => {
    const result = runSql({ startExit: 7 })
    assert.equal(result.status, 7)
    assert.equal(result.events.length, 1)
    assert.deepEqual(result.waits, [])
    assert.equal(result.environment, '')
  })

  test('SQL installs the api under npm policy and runs its SQL suite using the step environment', () => {
    const sql = job('sql')
    assert.match(sql, /permissions:\n {6}contents: read\n/u)
    assert.ok(sql.indexOf('node scripts/check-npm-policy.mjs') < sql.indexOf('npm ci --prefix api'))
    assert.match(step('sql', 'Install api'), /run: npm ci --prefix api/u)
    assert.match(step('sql', 'SQL integration tests'), /run: npm run test:sql --prefix api/u)
    assert.match(step('sql', 'SQL integration tests'), /ACCOUNTS_SQL_TEST_SERVER: \$\{\{ env\.ACCOUNTS_SQL_TEST_SERVER \}\}/u)
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

function runBudget(path, symlink = false) {
  const base = realpathSync(mkdtempSync(join(tmpdir(), 'ciwf-assets-')))
  folders.push(base)
  const folder = join(base, 'checkout')
  mkdirSync(join(folder, 'scripts'), { recursive: true })
  const target = symlink ? join(base, 'outside') : join(folder, path)
  mkdirSync(target, { recursive: true })
  if (symlink) symlinkSync(target, join(folder, path))
  writeFileSync(join(folder, 'scripts/check-assets.mjs'), 'console.log(JSON.stringify(process.argv.slice(2)))\n')
  const inline = step('checks', 'Asset budget').match(/^ {8}run: ([^|\n].*)$/mu)?.[1]
  const script = inline ?? stepScript(workflowPath, 'checks', 'Asset budget')
  const result = spawnSync('bash', ['-e', '-c', script], {
    cwd: folder, encoding: 'utf8', env: { PATH: process.env.PATH, APP_LOCATION: path },
  })
  return { ...result, folder }
}

for (const path of ['out', 'build/client', '-assets', 'built assets']) {
  test(`the asset checker receives an absolute single argument for ${path}`, () => {
    const result = runBudget(path)
    assert.equal(result.status, 0, result.stderr)
    assert.deepEqual(JSON.parse(result.stdout), [join(result.folder, path)])
  })
}

for (const path of ['../outside', '../checkout-sibling/outside', 'linked']) {
  test(`the asset checker refuses a checkout escape through ${path}`, () => {
    const result = runBudget(path, path === 'linked')
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /outside.*checkout/iu)
    assert.equal(result.stdout, '')
  })
}
