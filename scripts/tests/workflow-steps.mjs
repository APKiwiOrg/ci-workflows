// Reads jobs and step scripts out of a workflow file by indentation, so a test can assert on one job and
// run one step's shell with stubs. Node built-ins only, because this repository has no Node toolchain.
import { readFileSync } from 'node:fs'

const indent = (line) => line.length - line.trimStart().length
const blank = (line) => line.trim() === ''

// The lines after lines[start] up to the first non-blank line indented no deeper than it.
function children(lines, start) {
  const depth = indent(lines[start])
  const body = []
  for (const line of lines.slice(start + 1)) {
    if (!blank(line) && indent(line) <= depth) break
    body.push(line)
  }
  while (body.length > 0 && blank(body.at(-1))) body.pop()
  return body
}

// The body of one job under jobs:, header excluded, with its original indentation.
export function jobBlock(workflowPath, jobId) {
  const lines = readFileSync(workflowPath, 'utf8').split('\n')
  const jobs = lines.indexOf('jobs:')
  if (jobs < 0) throw new Error(`${workflowPath} has no top-level jobs:`)
  const section = children(lines, jobs)
  const depth = Math.min(...section.filter((line) => !blank(line)).map(indent))
  const header = section.findIndex((line) => indent(line) === depth && line.trim() === `${jobId}:`)
  if (header < 0) throw new Error(`${workflowPath} has no job ${jobId}`)
  return `${children(section, header).join('\n')}\n`
}

export function stepBlock(workflowPath, jobId, stepName) {
  const lines = jobBlock(workflowPath, jobId).split('\n')
  const start = lines.findIndex((line) => line.trim() === `- name: ${stepName}`)
  if (start < 0) throw new Error(`job ${jobId} has no step ${stepName}`)
  return `${children(lines, start).join('\n')}\n`
}

// The run: | script of the named step in one job, dedented to column zero.
export function stepScript(workflowPath, jobId, stepName) {
  const lines = jobBlock(workflowPath, jobId).split('\n')
  const start = lines.findIndex((line) => line.trim() === `- name: ${stepName}`)
  if (start < 0) throw new Error(`job ${jobId} has no step ${stepName}`)
  const step = children(lines, start)
  const run = step.findIndex((line) => /^run: \|-?$/u.test(line.trim()))
  if (run < 0) throw new Error(`step ${stepName} has no run: | block`)
  const script = children(step, run)
  const depth = Math.min(...script.filter((line) => !blank(line)).map(indent))
  return `${script.map((line) => line.slice(depth)).join('\n')}\n`
}
