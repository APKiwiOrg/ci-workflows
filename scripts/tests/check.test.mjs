// Checks the test gates of this repository's own check.yml and of the README's complete example workflow,
// which callers copy. On a full run ci-scope may list no segments, so a gate on segments alone would skip
// every test job. Each Test job's gate must therefore start with the full half.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, test } from 'node:test'
import { fileURLToPath } from 'node:url'

const checkPath = fileURLToPath(new URL('../../.github/workflows/check.yml', import.meta.url))
const readmePath = fileURLToPath(new URL('../../README.md', import.meta.url))
const FULL_GATE = "needs.scope.outputs.full == 'true' ||"

// The README's complete example is the first yaml block of its Example workflow section.
function readmeExample() {
  const section = readFileSync(readmePath, 'utf8').split('\n### Example workflow\n')[1]?.split(/\n#{2,3} /u)[0]
  const block = section?.match(/```yaml\n([\s\S]*?)```/u)?.[1]
  assert.ok(block, 'README has a complete example workflow')
  return block
}

// Each job under the top-level jobs: with its display name and if: line, read by indentation.
function jobs(text) {
  const lines = text.split('\n')
  const start = lines.indexOf('jobs:')
  assert.ok(start >= 0, 'the workflow has a top-level jobs:')
  const found = []
  for (const line of lines.slice(start + 1)) {
    const header = line.match(/^ {2}([\w-]+):$/u)
    if (header) {
      found.push({ id: header[1], name: '', gate: '' })
      continue
    }
    if (/^\S/u.test(line)) break
    const field = line.match(/^ {4}(name|if): (.+)$/u)
    if (field && found.length > 0) {
      const value = field[2].trim().replace(/^'(.*)'$/u, '$1')
      found.at(-1)[field[1] === 'name' ? 'name' : 'gate'] = value
    }
  }
  return found
}

function assertTestGates(text, minimum) {
  const testJobs = jobs(text).filter((job) => job.name.startsWith('Test'))
  assert.ok(testJobs.length >= minimum, `expected at least ${minimum} Test jobs, found ${testJobs.length}`)
  for (const job of testJobs) {
    assert.ok(job.gate.startsWith(FULL_GATE), `${job.id} gate must start with ${FULL_GATE}, found: ${job.gate}`)
  }
}

describe('every Test job runs on a full run whatever segments holds', () => {
  test('check.yml gates each Test job on full first', () => {
    assertTestGates(readFileSync(checkPath, 'utf8'), 2)
  })

  test('the README example gates its Test job on full first', () => {
    assertTestGates(readmeExample(), 1)
  })
})
