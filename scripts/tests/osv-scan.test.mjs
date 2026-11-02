// Exercises osv-scan.yml: its install and scan step scripts against stub curl and osv-scanner binaries, so
// nothing is downloaded or sent to OSV, and the wiring that keeps the run report-only and read-only.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { accessSync, constants, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepBlock, stepScript } from './workflow-steps.mjs'

const workflowPath = fileURLToPath(new URL('../../.github/workflows/osv-scan.yml', import.meta.url))
const readmePath = fileURLToPath(new URL('../../README.md', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = () => jobBlock(workflowPath, 'scan')
const installStep = 'Install osv-scanner'
const scanStep = 'Scan the dependencies'
const uploadStep = 'Upload the JSON report'
const folders = []

// A failed real scan still writes a report that reads as clean, so the stub does the same on any exit but 0
// and 1, and the tests check that it never reaches the summary.
const misleading = 'Total 0 packages affected by 0 known vulnerabilities'
const findingsTable = '| OSV URL | CVSS | Ecosystem | Package | Version | Source |'
const stubScanner = [
  '#!/bin/sh',
  'printf "%s\\n" "$*" >> "$TEST_OSV_LOG"',
  'format= output=',
  'while [ "$#" -gt 0 ]; do',
  '  case "$1" in',
  '    --format) format=$2; shift ;;',
  '    --output-file) output=$2; shift ;;',
  '  esac',
  '  shift',
  'done',
  'if [ "$format" = json ]; then code=$TEST_OSV_JSON_EXIT; else code=$TEST_OSV_MARKDOWN_EXIT; fi',
  'case "$code" in',
  `  0) report='{"results":[]}'; [ "$format" = json ] || report='No issues found' ;;`,
  `  1) report='{"results":[{}]}'; [ "$format" = json ] || report='${findingsTable}' ;;`,
  `  *) report='{"results":[]}'; [ "$format" = json ] || report='${misleading}' ;;`,
  'esac',
  'printf "%s\\n" "$report" > "$output"',
  'exit "$code"',
  '',
].join('\n')

function folder() {
  const path = mkdtempSync(join(tmpdir(), 'ciwf-osv-scan-'))
  folders.push(path)
  return path
}

// Runs the scan step under bash -e, the shell GitHub uses for a run step with no shell set.
function runScan({ json = 0, markdown = 0 } = {}) {
  const root = folder()
  const temp = join(root, 'temp')
  const checkout = join(root, 'checkout')
  mkdirSync(temp)
  mkdirSync(checkout)
  writeFileSync(join(temp, 'osv-scanner'), stubScanner, { mode: 0o700 })
  const log = join(root, 'log')
  const summary = join(root, 'summary')
  const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'scan', scanStep)], {
    cwd: checkout,
    encoding: 'utf8',
    env: {
      PATH: process.env.PATH, RUNNER_TEMP: temp, GITHUB_STEP_SUMMARY: summary, OSV_SCANNER_VERSION: '2.7.0',
      TEST_OSV_LOG: log, TEST_OSV_JSON_EXIT: String(json), TEST_OSV_MARKDOWN_EXIT: String(markdown),
    },
  })
  const calls = existsSync(log) ? readFileSync(log, 'utf8').split('\n').filter(Boolean) : []
  return {
    ...result,
    calls,
    summary: existsSync(summary) ? readFileSync(summary, 'utf8') : '',
    report: join(temp, 'osv-scan', 'osv-scanner.json'),
  }
}

afterEach(() => {
  for (const path of folders.splice(0)) rmSync(path, { recursive: true, force: true })
})

describe('the scan step reports results and fails on scan errors', () => {
  test('a clean scan passes and publishes its report', () => {
    const result = runScan()
    assert.equal(result.status, 0, result.stderr)
    assert.match(result.summary, /No issues found/u)
    assert.ok(existsSync(result.report), 'the JSON report is left for the upload')
    assert.doesNotMatch(result.stdout, /::(?:warning|error)::/u)
  })

  test('findings pass with a warning and publish the findings table', () => {
    const result = runScan({ json: 1, markdown: 1 })
    assert.equal(result.status, 0, result.stderr)
    assert.ok(result.summary.includes(findingsTable), 'the summary carries the findings table')
    assert.ok(existsSync(result.report), 'the JSON report is left for the upload')
    assert.match(result.stdout, /^::warning::/mu)
  })

  // Exit 2 guards the rule that every exit but 0 and 1 is an error, not only 127 and above.
  for (const [label, exits, code] of [
    ['the JSON scan exits 127', { json: 127 }, 127],
    ['the JSON scan exits 2', { json: 2 }, 2],
    ['the markdown scan exits 127 after a clean JSON scan', { json: 0, markdown: 127 }, 127],
  ]) {
    test(`a scan error fails the step and publishes no report, when ${label}`, () => {
      const result = runScan(exits)
      assert.notEqual(result.status, 0)
      assert.match(result.stdout, /^::error::/mu)
      assert.match(result.summary, new RegExp(`\\b${code}\\b`, 'u'), 'the summary names the exit code')
      assert.ok(!result.summary.includes(misleading), 'the failed scan report never reaches the summary')
    })
  }

  test('a JSON scan error stops before the markdown scan', () => {
    assert.equal(runScan({ json: 127 }).calls.length, 1)
  })

  test('both scans are recursive over the checkout and accept a repository with no lockfiles', () => {
    const { calls } = runScan()
    assert.equal(calls.length, 2)
    for (const [index, format] of ['json', 'markdown'].entries()) {
      const args = calls[index].split(' ')
      assert.deepEqual(args.slice(0, 2), ['scan', 'source'])
      assert.ok(args.includes('-r'), 'the scan is recursive')
      assert.ok(args.includes('--allow-no-lockfiles'), 'no lockfile is a clean result')
      assert.equal(args[args.indexOf('--format') + 1], format)
      assert.equal(args.at(-1), '.', 'the scan reads the checkout')
    }
  })
})

// The runner's GNU sha256sum takes --check --strict. The macOS one does not, so those cases run on CI.
const gnuSha256sum = /GNU coreutils/u.test(spawnSync('sha256sum', ['--version'], { encoding: 'utf8' }).stdout ?? '')

describe('the install step checks the release binary', () => {
  const release = 'https://github.com/google/osv-scanner/releases/download/v2.7.0/osv-scanner_linux_amd64'
  const stubCurl = [
    '#!/bin/sh',
    'out= url=',
    'while [ "$#" -gt 0 ]; do',
    '  case "$1" in',
    '    -o) out=$2; shift ;;',
    '    -*) ;;',
    '    *) url=$1 ;;',
    '  esac',
    '  shift',
    'done',
    'printf "%s\\n" "$url" >> "$TEST_CURL_LOG"',
    'printf "%s" "$TEST_CURL_BYTES" > "$out"',
    '',
  ].join('\n')

  function runInstall(checksumOf) {
    const root = folder()
    const temp = join(root, 'temp')
    const bin = join(root, 'bin')
    mkdirSync(temp)
    mkdirSync(bin)
    writeFileSync(join(bin, 'curl'), stubCurl, { mode: 0o700 })
    const log = join(root, 'log')
    const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'scan', installStep)], {
      cwd: root,
      encoding: 'utf8',
      env: {
        PATH: `${bin}:${process.env.PATH}`, RUNNER_TEMP: temp, TEST_CURL_LOG: log, TEST_CURL_BYTES: 'release bytes',
        OSV_SCANNER_VERSION: '2.7.0', OSV_SCANNER_SHA256: createHash('sha256').update(checksumOf).digest('hex'),
      },
    })
    const binary = join(temp, 'osv-scanner')
    let executable = false
    try {
      accessSync(binary, constants.X_OK)
      executable = true
    } catch {}
    return { ...result, urls: existsSync(log) ? readFileSync(log, 'utf8').split('\n').filter(Boolean) : [], executable }
  }

  test('the checksum is checked before the binary is made executable', () => {
    const script = stepScript(workflowPath, 'scan', installStep)
    const check = script.indexOf('sha256sum --check --strict')
    assert.ok(check >= 0, 'the step checks the pinned checksum')
    assert.ok(check < script.indexOf('chmod +x'), 'the check comes first')
  })

  test('a binary matching the pinned checksum is installed from the pinned release', { skip: !gnuSha256sum && 'needs GNU sha256sum' }, () => {
    const result = runInstall('release bytes')
    assert.equal(result.status, 0, result.stderr)
    assert.deepEqual(result.urls, [release])
    assert.ok(result.executable, 'the binary is executable')
  })

  test('a binary that differs from the pinned checksum is refused', { skip: !gnuSha256sum && 'needs GNU sha256sum' }, () => {
    const result = runInstall('other bytes')
    assert.notEqual(result.status, 0)
    assert.equal(result.executable, false, 'the refused binary is never made executable')
  })
})

describe('the workflow is a read-only report', () => {
  test('it runs from a caller or by hand, with no inputs or secrets', () => {
    assert.match(workflow(), /^on:\n {2}workflow_call:\n {2}workflow_dispatch:\n\n/mu)
    assert.doesNotMatch(workflow(), /^ *(?:inputs|secrets):/mu)
  })

  test('the token reads contents only and the job runs on the pinned hosted image', () => {
    assert.match(workflow(), /^permissions: \{\}$/mu)
    assert.match(job(), /^ {4}permissions:\n {6}contents: read\n {4}\S/mu)
    assert.match(job(), /^ {4}runs-on: ubuntu-26\.04$/mu)
    assert.match(job(), /^ {4}timeout-minutes: \d+$/mu)
    assert.match(job(), /^ {10}persist-credentials: false$/mu)
  })

  test('the version and its release checksum are pinned in the job env', () => {
    assert.match(job(), /^ {6}OSV_SCANNER_VERSION: \d+\.\d+\.\d+$/mu)
    assert.match(job(), /^ {6}OSV_SCANNER_SHA256: [a-f0-9]{64}$/mu)
  })

  test('the JSON report uploads as a short-lived artifact only after a successful scan', () => {
    const upload = stepBlock(workflowPath, 'scan', uploadStep)
    assert.doesNotMatch(upload, /^ *if:/mu, 'the upload runs only when the scan step succeeded')
    assert.match(upload, /^ {10}path: \$\{\{ runner\.temp \}\}\/osv-scan\/osv-scanner\.json$/mu)
    assert.match(upload, /^ {10}if-no-files-found: error$/mu)
    const days = Number(upload.match(/^ {10}retention-days: (\d+)$/mu)?.[1])
    assert.ok(days >= 1 && days <= 30, 'the artifact is kept for at most 30 days')
  })

  test('no run line interpolates an expression', () => {
    for (const name of [installStep, scanStep]) assert.doesNotMatch(stepScript(workflowPath, 'scan', name), /\$\{\{/u)
  })

  test('the README caller runs weekly and by hand with a read-only token', () => {
    const section = readFileSync(readmePath, 'utf8').split('\n## osv-scan\n')[1]?.split(/\n## /u)[0]
    const caller = section?.match(/```yaml\n([\s\S]+?)```/u)?.[1]
    assert.ok(caller, 'README documents the osv-scan caller')
    assert.match(caller, /^ {4}- cron: '\d{1,2} \d{1,2} \* \* [0-6]'$/mu)
    assert.match(caller, /^ {2}workflow_dispatch:$/mu)
    assert.match(caller, /^permissions:\n {2}contents: read$/mu)
    assert.match(caller, /^ {4}uses: APKiwiOrg\/ci-workflows\/\.github\/workflows\/osv-scan\.yml@<full commit sha>$/mu)
  })
})
