// Exercises site-live-throttle-check.yml: its step script against a stub curl that answers each post in turn,
// so no request ever leaves the machine, and the wiring that keeps inputs in env and the token empty.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { jobBlock, stepScript } from './workflow-steps.mjs'

const folders = []
const siteUrl = 'https://site.example.test'
const endpoint = `${siteUrl}/api/enquiry`
const probe = '{"notes_extra":"x"}'
const stepName = 'Probe the mode, then post sender-limit plus one enquiries in off mode'
const workflowPath = fileURLToPath(new URL('../../.github/workflows/site-live-throttle-check.yml', import.meta.url))
const readmePath = fileURLToPath(new URL('../../README.md', import.meta.url))
const workflow = () => readFileSync(workflowPath, 'utf8')
const job = () => jobBlock(workflowPath, 'check')
const closed = '503 enquiries-not-open'
const limited = '429 rate-limited'

// The stub records each post's body, forged header and URL, then answers with the next "code body" line.
// It answers 000 with a failed exit once the lines run out, as curl does when nothing connects.
const stubCurl = [
  '#!/bin/sh',
  'out= xff= data= url=',
  'while [ "$#" -gt 0 ]; do',
  '  case "$1" in',
  '    --output) out=$2; shift ;;',
  '    --data) data=$2; shift ;;',
  '    --header) case "$2" in "X-Forwarded-For: "*) xff=${2#X-Forwarded-For: } ;; esac; shift ;;',
  '    --max-time|--request|--write-out) shift ;;',
  '    -*) ;;',
  '    *) url=$1 ;;',
  '  esac',
  '  shift',
  'done',
  'printf "%s\\n%s\\n%s\\n@@\\n" "$data" "$xff" "$url" >> "$TEST_CURL_LOG"',
  'n=$(grep -c "^@@$" "$TEST_CURL_LOG")',
  'answer=$(sed -n "${n}p" "$TEST_CURL_ANSWERS")',
  'case "$answer" in 000|"") printf 000; exit 7 ;; esac',
  'printf "{\\"code\\":\\"%s\\"}" "${answer#* }" > "$out"',
  'printf "%s" "${answer%% *}"',
  '',
].join('\n')

// Runs the step under bash -e, the shell GitHub uses for a run step with no shell set.
function runCheck(answers, env = {}) {
  const folder = mkdtempSync(join(tmpdir(), 'ciwf-site-throttle-'))
  folders.push(folder)
  const log = join(folder, 'log')
  const answerFile = join(folder, 'answers')
  writeFileSync(answerFile, answers.map((answer) => `${answer}\n`).join(''))
  writeFileSync(join(folder, 'curl'), stubCurl, { mode: 0o700 })
  const result = spawnSync('bash', ['-e', '-c', stepScript(workflowPath, 'check', stepName)], {
    encoding: 'utf8',
    env: {
      PATH: `${folder}:${process.env.PATH}`, TEST_CURL_LOG: log, TEST_CURL_ANSWERS: answerFile,
      SITE_URL: siteUrl, SENDER_LIMIT: '5', ...env,
    },
  })
  const recorded = existsSync(log) ? readFileSync(log, 'utf8') : ''
  const calls = recorded.split('@@\n').slice(0, -1).map((call) => {
    const [data, xff, url] = call.split('\n')
    return { data, xff, url }
  })
  return { ...result, calls }
}

// The template enquiry schema in api/src/enquiry/request.ts requires a name, an email and needs as
// non-empty strings and an integer elapsedMs. The handler answers too-fast below 3000 ms, before the
// throttle, so a faster body would never reach the throttle.
function assertTemplateEnquiry(data) {
  const body = JSON.parse(data)
  for (const field of ['name', 'email', 'needs']) {
    assert.equal(typeof body[field], 'string', `the body has ${field}`)
    assert.ok(body[field].trim().length > 0, `${field} is not empty`)
  }
  assert.equal(body.email, 'throttle-check@example.com')
  assert.ok(Number.isInteger(body.elapsedMs) && body.elapsedMs >= 3000, 'elapsedMs passes the timing check')
  assert.equal('notes_extra' in body, false, 'a valid post never trips the honeypot')
}

// A run where the probe and posts 1 to limit answer 503 and the last post answers the given line.
const passingAnswers = (limit, last = limited) => [closed, ...Array(limit).fill(closed), last]

afterEach(() => {
  for (const folder of folders.splice(0)) rmSync(folder, { recursive: true, force: true })
})

describe('the run stops unless the probe answers 503 enquiries-not-open', () => {
  for (const answer of ['202 accepted', '500 misconfigured', '503 other', '200 enquiries-not-open', limited, '000']) {
    test(`the run stops unless the probe answers 503 enquiries-not-open, here ${answer}`, () => {
      const result = runCheck([answer, closed, closed])
      assert.notEqual(result.status, 0)
      assert.equal(result.calls.length, 1, 'nothing is posted after the probe')
      assert.match(result.stdout, /^mode probe answered \d{3}$/mu)
    })
  }

  test('the probe is a honeypot-only post with no forged header', () => {
    const [first] = runCheck(['202 accepted']).calls
    assert.deepEqual(first, { data: probe, xff: '', url: endpoint })
  })

  test('the endpoint drops a trailing slash from the site url', () => {
    const [first] = runCheck(['202 accepted'], { SITE_URL: `${siteUrl}/` }).calls
    assert.equal(first.url, endpoint)
  })
})

describe('sends sender-limit plus one posts', () => {
  for (const limit of [1, 5, 7]) {
    test(`sends sender-limit plus one posts, here ${limit} plus one`, () => {
      const result = runCheck(passingAnswers(limit), { SENDER_LIMIT: String(limit) })
      assert.equal(result.status, 0, result.stderr)
      const posts = result.calls.slice(1)
      assert.equal(posts.length, limit + 1)
      for (const post of posts) {
        assert.equal(post.url, endpoint)
        assertTemplateEnquiry(post.data)
      }
      assert.equal(posts[0].xff, '', 'post 1 is plain')
      assert.deepEqual(posts.slice(1).map((post) => post.xff),
        Array.from({ length: limit }, (_, index) => `203.0.113.${index + 2}`))
      assert.match(result.stdout, new RegExp(`hit 429 on post ${limit + 1}\\n$`, 'u'))
    })
  }

  test('a last post that answers 503 fails because forged values bypass the throttle', () => {
    const result = runCheck(passingAnswers(5, closed))
    assert.notEqual(result.status, 0)
    assert.equal(result.calls.length, 7)
    assert.match(result.stderr, /post 6 did not answer 429 rate-limited/u)
  })

  test('a post before the last that answers 429 stops the run', () => {
    const result = runCheck([closed, closed, closed, limited, closed, closed, limited])
    assert.notEqual(result.status, 0)
    assert.equal(result.calls.length, 4)
    assert.match(result.stderr, /post 3 did not answer 503 enquiries-not-open/u)
  })

  test('post 1 answering 429 stops the run with the full bucket reason', () => {
    const result = runCheck([closed, limited, closed])
    assert.notEqual(result.status, 0)
    assert.equal(result.calls.length, 2)
    assert.match(result.stderr, /post 1 answered 429/u)
  })

  test('a dropped connection stops the run', () => {
    const result = runCheck([closed, closed, '000', closed])
    assert.notEqual(result.status, 0)
    assert.equal(result.calls.length, 3)
  })

  test('output names status codes only, never an address', () => {
    for (const answers of [passingAnswers(5), passingAnswers(5, closed), ['202 accepted']]) {
      const result = runCheck(answers)
      assert.doesNotMatch(`${result.stdout}${result.stderr}`, /203\.0\.113|@|example\.com/u)
    }
  })
})

describe('inputs are checked before anything is posted', () => {
  for (const url of ['', 'http://site.example.test', 'ftp://site.example.test', 'site.example.test']) {
    test(`site-url ${JSON.stringify(url)} is refused`, () => {
      const result = runCheck(passingAnswers(5), { SITE_URL: url })
      assert.notEqual(result.status, 0)
      assert.equal(result.calls.length, 0)
    })
  }

  for (const limit of ['', '0', '-1', '05', '2.5', 'abc', '5 ', '254', '1000', '99999999999999999999']) {
    test(`sender-limit ${JSON.stringify(limit)} is refused`, () => {
      const result = runCheck(passingAnswers(5), { SENDER_LIMIT: limit })
      assert.notEqual(result.status, 0)
      assert.equal(result.calls.length, 0)
      assert.match(result.stderr, /sender-limit must be a whole number from 1 to 253/u)
    })
  }

  test('sender-limit 253 is the largest accepted, so the last forged value is 203.0.113.254', () => {
    const result = runCheck(passingAnswers(253), { SENDER_LIMIT: '253' })
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.calls.at(-1).xff, '203.0.113.254')
  })
})

describe('the workflow is a tokenless reusable check', () => {
  test('it is called only, with the two required inputs', () => {
    assert.match(workflow(), /^on:\n {2}workflow_call:\n {4}inputs:\n/mu)
    assert.doesNotMatch(workflow(), /^ *(?:workflow_dispatch|secrets):/mu)
    for (const [name, type] of [['site-url', 'string'], ['sender-limit', 'number']]) {
      const input = workflow().match(new RegExp(`^ {6}${name}:\\n((?: {8}.+\\n)+)`, 'mu'))?.[1]
      assert.ok(input, `workflow_call declares ${name}`)
      assert.match(input, /^ {8}required: true$/mu)
      assert.match(input, new RegExp(`^ {8}type: ${type}$`, 'mu'))
    }
  })

  test('the token has no permissions and the job runs on the pinned hosted image', () => {
    assert.match(workflow(), /^permissions: \{\}$/mu)
    assert.equal(workflow().match(/permissions:/gu)?.length, 1)
    assert.match(job(), /^ {4}runs-on: ubuntu-26\.04$/mu)
    assert.match(job(), /^ {4}timeout-minutes: 5$/mu)
  })

  test('inputs reach the script only through env', () => {
    assert.match(job(), /^ {10}SITE_URL: \$\{\{ inputs\.site-url \}\}$/mu)
    assert.match(job(), /^ {10}SENDER_LIMIT: \$\{\{ inputs\.sender-limit \}\}$/mu)
    assert.doesNotMatch(stepScript(workflowPath, 'check', stepName), /\$\{\{/u)
  })

  test('the README caller passes both inputs with no token permissions', () => {
    const section = readFileSync(readmePath, 'utf8').split('\n## site-live-throttle-check\n')[1]?.split(/\n## /u)[0]
    const caller = section?.match(/```yaml\n([\s\S]+?)```/u)?.[1]
    assert.ok(caller, 'README documents the site-live-throttle-check caller')
    assert.match(caller, /^ {2}workflow_dispatch:$/mu)
    assert.match(caller, /^permissions: \{\}$/mu)
    assert.match(caller, /^ {4}uses: APKiwiOrg\/ci-workflows\/\.github\/workflows\/site-live-throttle-check\.yml@<full commit sha>$/mu)
    assert.match(caller, /^ {6}site-url: \$\{\{ inputs\.site-url \}\}$/mu)
    assert.match(caller, /^ {6}sender-limit: \d+$/mu)
  })
})
