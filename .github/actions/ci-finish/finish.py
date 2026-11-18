#!/usr/bin/env python3
"""Give a CI run one verdict and one summary, and prepare the tested-tree marker of a full run.

The verdict starts from the calling job's `needs`: a job that failed or was cancelled fails the run, and
input that is not a JSON object naming at least one job fails it, as does a `full` input other than true or
false. A skipped job fails it unless its id is in the segment list on a run that is not full, or in the
always-allowed list and not the segment list. Every available API row is audited. A full run requires
every other job to complete with success or an explicitly allowed skip. Unreadable data fails a full run.
On a partial run, only known failures and identifiable unfinished jobs add to the needs verdict. The
current check run ID identifies the finishing job. Without a match, a partial run cannot tell which
unfinished row is its own, but still counts completed failures. A full re-run must account for every
job name from the first attempt. Summary fallback preserves the verdict.

A full run that passed prepares the ci-passed-full-TREE marker, but only when the jobs API was read, since
otherwise a job missing from `needs` could have failed unseen. A full run that failed with at least one job
in `needs` reporting `failure` prepares ci-failed-full-TREE instead, so ci-tested-tree knows a newer full run
of the tree failed. A run whose only problems are cancelled or wrongly skipped jobs prepares neither.

Inputs come from the environment: CI_FINISH_NEEDS, CI_FINISH_ALLOW_SKIPPED, CI_FINISH_ALLOW_SKIPPED_ALWAYS,
CI_FINISH_FULL, CI_FINISH_SEGMENTS, CI_FINISH_SCOPE_REASON, CI_FINISH_TITLE, CI_FINISH_CHECK_RUN_ID,
CI_API_TOKEN and the runner's GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_EVENT_NAME,
GITHUB_SERVER_URL,
GITHUB_API_URL, GITHUB_STEP_SUMMARY, GITHUB_OUTPUT and RUNNER_TEMP. The summary is appended to
GITHUB_STEP_SUMMARY and the outputs passed, upload, tree, artifact and marker to GITHUB_OUTPUT. The script
itself always exits 0, so the action's upload and verdict steps still run. Python 3.8 or later, standard
library only.
"""
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from typing import Any, Callable, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "_shared"))
import github_api  # noqa: E402

DEFAULT_JOB_NAME = "CI passed"
MARKER_PREFIX = "ci-passed-full-"
FAILED_MARKER_PREFIX = "ci-failed-full-"
# Known API conclusions that constitute a failure even on a partial run.
FAILING_CONCLUSIONS = ("failure", "timed_out", "cancelled", "action_required", "stale", "startup_failure")
NOT_AN_OBJECT = "needs input is not a JSON object"
BAD_FULL = "full input is not true or false"
NOT_AFFECTED = "Not affected by this change"
JOBS_PER_PAGE = 100
JOB_PAGES = 3
JOBS_ATTEMPTS = 3
JOBS_RETRY_SECONDS = 1
AUDIT_BUDGET_SECONDS = 45

_JOB_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_STAGE = re.compile(r"(?:Scope|Build|Build and test|Test|Package|CI passed|(?:Test|Package|Deploy): \S.*)")
_TREE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# Every ASCII punctuation character. CommonMark shows a backslash-escaped one as itself, so text from git
# or the API cannot form a link, image, emphasis, HTML, entity or autolink in a summary cell.
_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")

WORDS = {"success": "Passed", "failure": "Failed", "cancelled": "Cancelled", "skipped": "Skipped"}
ICONS = {
    "Passed": "\u2705",
    "Failed": "\U0001F534",
    "Cancelled": "\U0001F534",
    "Skipped": "\u26AA",
    "Running": "\U0001F504",
}


class Row(NamedTuple):
    stage: str
    result: str
    seconds: Optional[float]
    detail: str


class Outcome(NamedTuple):
    passed: bool
    problems: List[str]
    summary: str
    # True when the marker named `artifact` for `tree` should be uploaded.
    upload: bool
    tree: str
    # Lines for the step log saying which summary detail could not be read.
    notes: List[str]
    # ci-passed-full-TREE or ci-failed-full-TREE when upload is true, otherwise empty.
    artifact: str = ""


def parse_needs(needs):
    # type: (Any) -> Optional[dict]
    """The needs object, from JSON text or already parsed, or None when it is not a JSON object."""
    if isinstance(needs, str):
        try:
            needs = json.loads(needs)
        except ValueError:
            return None
    return needs if isinstance(needs, dict) else None


def parse_allowed(text, api_names=False):
    # type: (str, bool) -> Optional[Set[str]]
    """Read an entire allow list or return None. Only the always list takes API names."""
    if text.lstrip().startswith(("[", "{", '"')):
        values = _string_list(text)
        if values is None:
            return None
        if api_names:
            return set(values)
    else:
        values = [name for name in re.split(r"[\s,]+", text) if name]
    return set(values) if all(_JOB_ID.fullmatch(name) for name in values) else None


def verdict(needs, allowed, always=(), full=False):
    # type: (Any, Iterable[str], Iterable[str], bool) -> Tuple[bool, List[str]]
    """Returns (passed, problems). Passed only when every job in needs succeeded or was an allowed skip.
    `allowed` excuses a skip only on a run that is not full, `always` on any run."""
    parsed = parse_needs(needs)
    if parsed is None:
        return False, [NOT_AN_OBJECT]
    if not parsed:
        return False, ["needs input names no jobs"]
    segment_jobs = set(allowed)
    # The segment rule wins for an id in both lists, so a full run never passes with a segment job skipped.
    excused = (set(always) - segment_jobs) | (set() if full else segment_jobs)
    problems = []
    for job_id, entry in parsed.items():
        result = entry.get("result") if isinstance(entry, dict) else None
        if result == "success" or (result == "skipped" and job_id in excused):
            continue
        if result == "skipped" and job_id in segment_jobs:
            problems.append("%s was skipped on a full run" % job_id)
        elif result in ("failure", "cancelled", "skipped"):
            problems.append("%s: %s" % (job_id, result))
        else:
            problems.append("%s: no recognised result" % job_id)
    return not problems, problems


def any_failure(needs):
    # type: (Any) -> bool
    """True when at least one job in needs reports `failure`, as opposed to only cancelled or skipped."""
    parsed = parse_needs(needs) or {}
    return any(isinstance(entry, dict) and entry.get("result") == "failure" for entry in parsed.values())


def job_names(jobs):
    return Counter(job["name"] for job in jobs
                   if isinstance(job, dict) and isinstance(job.get("name"), str) and job["name"])


def audit_jobs(jobs, own_id, full, allowed_names):
    # type: (Sequence[Any], Optional[int], bool, Set[str]) -> Tuple[List[str], List[str], Optional[int]]
    """Audit all available rows before deciding whether the jobs list was complete."""
    ids = Counter(job["id"] for job in jobs if isinstance(job, dict) and type(job.get("id")) is int)
    names = job_names(jobs)
    if ids[own_id] != 1:
        own_id = None

    problems = []
    incomplete = []
    if full and own_id is None:
        incomplete.append("the finishing job's check run ID could not be uniquely matched in the jobs API")
    for index, job in enumerate(jobs, 1):
        label = "jobs API row %d" % index
        if not isinstance(job, dict):
            if full:
                incomplete.append("%s is not an object" % label)
            continue
        job_id, name = job.get("id"), job.get("name")
        status, conclusion = job.get("status"), job.get("conclusion")
        valid_id = type(job_id) is int and job_id > 0
        if valid_id and job_id == own_id:
            continue
        if isinstance(name, str) and name:
            label = name
        elif full:
            incomplete.append("%s has no usable API name" % label)
        if full and (not valid_id or ids[job_id] != 1):
            incomplete.append("%s has an invalid or duplicate ID" % label)

        if status != "completed":
            if full or own_id is not None:
                problems.append("%s has not finished and needs does not wait for it" % label)
            continue

        if not full:
            if conclusion in FAILING_CONCLUSIONS:
                problems.append("%s: %s, not reported by needs (missing from needs or allowed to fail)"
                                % (label, conclusion))
            continue

        if conclusion == "success":
            continue
        if conclusion == "skipped":
            if not isinstance(name, str) or name not in allowed_names:
                problems.append("%s was skipped on a full run. List its exact API name in allow-skipped-always "
                                "only if the job does not test." % label)
            elif names[name] != 1:
                incomplete.append("skip exception %s matches more than one API job" % label)
        elif conclusion in FAILING_CONCLUSIONS or conclusion == "neutral":
            problems.append("%s concluded %s on a full run" % (label, conclusion))
        else:
            incomplete.append("%s has an unusable conclusion %r" % (label, conclusion))
    return problems, incomplete, own_id


def stage_of(name):
    # type: (Any) -> str
    """The stage a job's display name stands for, or "" when it is not a stage. A reusable workflow job
    shows as CALLER / CALLED, and only the part after the last ` / ` counts."""
    if not isinstance(name, str):
        return ""
    called = name.rsplit(" / ", 1)[-1].strip()
    return called if _STAGE.fullmatch(called) else ""


def _word(conclusion):
    if conclusion is None:
        return "Running"
    return WORDS.get(conclusion, "Failed")


def _seconds(item, word):
    if word in ("Skipped", "Running"):
        return None
    started = github_api.parse_time(item.get("started_at"))
    completed = github_api.parse_time(item.get("completed_at"))
    if started is None or completed is None:
        return None
    return max(0.0, (completed - started).total_seconds())


def _detail(stage, word, segments, scope_reason):
    if stage == "Scope":
        return scope_reason or ""
    if (word == "Skipped" and segments is not None and stage.startswith("Test: ")
            and stage[len("Test: "):] not in segments):
        return NOT_AFFECTED
    return ""


def _row(stage, item, segments, scope_reason):
    word = _word(item.get("conclusion"))
    return Row(stage, word, _seconds(item, word), _detail(stage, word, segments, scope_reason))


def rows(jobs, own_id, segments, scope_reason):
    # type: (Sequence[dict], Optional[int], Optional[Sequence[str]], str) -> List[Row]
    """One row per stage-named job, else per stage-named step, else one Other row. The job running this
    action is left out, since its row is the verdict and comes last."""
    table = []
    for job in jobs:
        if not isinstance(job, dict) or (own_id is not None and job.get("id") == own_id):
            continue
        stage = stage_of(job.get("name"))
        if stage:
            table.append(_row(stage, job, segments, scope_reason))
            continue
        steps = [s for s in (job.get("steps") or [])
                 if isinstance(s, dict) and _STAGE.fullmatch(str(s.get("name")))]
        if steps:
            table.extend(_row(s["name"], s, segments, scope_reason) for s in steps)
        else:
            table.append(_row("Other: %s" % job.get("name"), job, segments, scope_reason))
    return table


def needs_rows(needs):
    # type: (Any) -> List[Row]
    """The fallback when the jobs API cannot be read: one row per job id with its result and no time."""
    parsed = parse_needs(needs) or {}
    table = []
    for job_id, entry in parsed.items():
        result = entry.get("result") if isinstance(entry, dict) else None
        table.append(Row(str(job_id), WORDS.get(result, "Failed") if isinstance(result, str) else "Failed",
                         None, ""))
    return table


def table(needs, jobs, own_id, segments, scope_reason, passed, now):
    # type: (Any, Optional[Sequence[dict]], Optional[int], Optional[Sequence[str]], str, bool, datetime.datetime)
    # -> List[Row]
    """Every row of the summary. jobs is None when the API could not be read."""
    if jobs is None:
        body = needs_rows(needs)
        own = None
    else:
        body = rows(jobs, own_id, segments, scope_reason)
        own = next((j for j in jobs if own_id is not None and isinstance(j, dict)
                    and j.get("id") == own_id), None)
    started = github_api.parse_time(own.get("started_at")) if own else None
    seconds = max(0.0, (now - started).total_seconds()) if started else None
    return body + [Row(DEFAULT_JOB_NAME, "Passed" if passed else "Failed", seconds, "")]


def duration(seconds):
    # type: (Optional[float]) -> str
    if seconds is None:
        return ""
    whole = max(0, int(seconds))
    if whole < 60:
        return "%d s" % whole
    if whole < 3600:
        return "%d min %d s" % (whole // 60, whole % 60)
    return "%d h %d min" % (whole // 3600, whole % 3600 // 60)


def last_full_pass(artifacts, now):
    # type: (Sequence[dict], datetime.datetime) -> str
    """The UTC date of the newest unexpired full-pass marker from a run of this repository's own code, or
    "not recorded"."""
    newest = None
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not str(artifact.get("name", "")).startswith(MARKER_PREFIX):
            continue
        # A fork's pull request run uploads to this repository too, and no trusted run made its marker.
        run = artifact.get("workflow_run")
        if (not isinstance(run, dict) or not isinstance(run.get("repository_id"), int)
                or run.get("head_repository_id") != run.get("repository_id")):
            continue
        expires = github_api.parse_time(artifact.get("expires_at"))
        created = github_api.parse_time(artifact.get("created_at"))
        if artifact.get("expired") is not False or expires is None or expires <= now or created is None:
            continue
        newest = created if newest is None or created > newest else newest
    return newest.strftime("%Y-%m-%d") if newest else "not recorded"


def _cell(text):
    return _PUNCTUATION.sub(r"\\\1", _CONTROL.sub(" ", str(text)))


def render(title, event, passed, rows, footer, seconds=None):
    # type: (str, str, bool, Sequence[Tuple[str, str, Optional[float], str]], str, Optional[float]) -> str
    """The run summary in Markdown. seconds is the run's time so far, None when unknown."""
    result = "Passed" if passed else "Failed"
    line = result + (" in %s." % duration(seconds) if seconds is not None else ".")
    tests = [row for row in rows if row[0].startswith("Test: ")]
    if tests:
        ran = sum(1 for row in tests if row[1] != "Skipped")
        line += " Ran %d of %d test segment%s." % (ran, len(tests), "" if len(tests) == 1 else "s")
    out = ["## %s, %s" % (title, event), "", line, "",
           "| Stage | Result | Time | Detail |", "| --- | --- | --- | --- |"]
    for stage, word, row_seconds, detail in rows:
        out.append("| %s | %s %s | %s | %s |" % (
            _cell(stage), ICONS.get(word, ICONS["Failed"]), word, duration(row_seconds), _cell(detail)))
    out += ["", "Full suite last passed: %s" % footer, ""]
    return "\n".join(out)


class AuditFetch:
    """Share one deadline across job pages, retries and the optional artifact footer."""

    def __init__(self, fetch, attempts):
        self.fetch = fetch
        self.attempts = attempts
        self.deadline = time.monotonic() + AUDIT_BUDGET_SECONDS

    def _timeout(self):
        return github_api.ApiError("API audit exceeded its %g-second budget" % AUDIT_BUDGET_SECONDS)

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self._timeout()
        return remaining

    def request(self, path):
        self._remaining()
        result = queue.Queue(maxsize=1)

        def read():
            try:
                result.put((self.fetch(path), None))
            except Exception as error:  # noqa: BLE001 The caller handles the API failure.
                result.put((None, error))

        # urllib's socket timeout is not a total body timeout. The short-lived action
        # can return its verdict even if a network read is still blocked.
        threading.Thread(target=read, daemon=True).start()
        try:
            answer, error = result.get(timeout=self._remaining())
        except queue.Empty:
            raise self._timeout()
        if error is not None:
            raise error
        return answer

    def jobs(self, path):
        for attempt in range(self.attempts):
            try:
                return self.request(path)
            except github_api.ApiError as error:
                if not error.retryable or attempt == self.attempts - 1:
                    raise
                time.sleep(min(JOBS_RETRY_SECONDS, self._remaining()))


def fetch_jobs(repo, run_id, fetch, attempt=None):
    # type: (str, str, github_api.Fetch, Optional[int]) -> Tuple[List[Any], Optional[str]]
    """Return the rows read and the reason if they do not cover the reported count."""
    jobs = []  # type: List[Any]
    total = None
    run_path = "/repos/%s/actions/runs/%s" % (repo, run_id)
    if attempt is not None:
        run_path += "/attempts/%d" % attempt
    for page in range(1, JOB_PAGES + 1):
        path = "%s/jobs?per_page=%d&page=%d" % (run_path, JOBS_PER_PAGE, page)
        try:
            answer = fetch(path)
        except Exception as error:  # noqa: BLE001 The verdict still needs the earlier pages.
            return jobs, "jobs API page %d could not be read: %s" % (page, error)
        batch = answer.get("jobs") if isinstance(answer, dict) else None
        count = answer.get("total_count") if isinstance(answer, dict) else None
        if not isinstance(batch, list):
            return jobs, "jobs API page %d holds no job list" % page
        previous = len(jobs)
        jobs.extend(batch)
        if type(count) is not int or count < 0:
            return jobs, "jobs API page %d has no usable total count" % page
        if total is not None and count != total:
            return jobs, "jobs API total count changed on page %d" % page
        total = count
        if len(batch) != min(JOBS_PER_PAGE, total - previous):
            return jobs, "jobs API page %d has %d jobs, inconsistent with the reported count of %d" % (
                page, len(batch), total)
        if len(jobs) == total:
            return jobs, None
    return jobs, "jobs API page limit reached after %d of %d jobs" % (len(jobs), total)


def first_attempt_problem(repo, run_id, fetch, jobs):
    """GitHub can give carried-forward jobs new IDs. Compare names and their multiplicities."""
    first_jobs, error = fetch_jobs(repo, run_id, fetch, attempt=1)
    if error:
        return "cannot read the first attempt's jobs: %s" % error
    first_names = job_names(first_jobs)
    if sum(first_names.values()) != len(first_jobs):
        return "the first attempt's jobs include a row without a usable name"
    missing = first_names - job_names(jobs)
    if missing:
        return "the current jobs API list omits jobs from the first attempt: %s" % ", ".join(sorted(missing))
    return None


def _string_list(text):
    try:
        value = json.loads(text) if text else None
    except Exception:  # noqa: BLE001 Any decoder failure makes the whole input unusable.
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def _title(env):
    repo = env.get("GITHUB_REPOSITORY", "")
    return env.get("CI_FINISH_TITLE") or "%s CI" % repo.rsplit("/", 1)[-1]


def parse_full(text):
    # type: (Any) -> Optional[bool]
    """True or False for the `full` input, read after trimming and lower-casing, or None for anything else."""
    value = text.strip().lower() if isinstance(text, str) else None
    return {"true": True, "false": False}.get(value)


def _verdict_from(env, full):
    allowed = parse_allowed(env.get("CI_FINISH_ALLOW_SKIPPED", ""))
    always = parse_allowed(env.get("CI_FINISH_ALLOW_SKIPPED_ALWAYS", ""), api_names=True)
    problems = []
    if full is None:
        problems.append(BAD_FULL)
    if allowed is None:
        problems.append("[invalid-allow-list] Invalid allow-skipped input. Use direct job IDs.")
    if always is None:
        problems.append("[invalid-allow-list] Invalid allow-skipped-always input. "
                        "Use job IDs or a JSON array of strings.")
    _, needs_problems = verdict(env.get("CI_FINISH_NEEDS", ""), allowed or (), always or (), full is True)
    return problems + needs_problems, always or set()


def marker_for(full, passed, needs, tree_of, jobs_read=True):
    # type: (bool, bool, Any, Callable[[], Optional[str]], bool) -> Tuple[str, str, List[str]]
    """Returns (artifact, tree, notes). The artifact is empty when no marker is due or the tree is unreadable.
    A pass is marked only when the jobs API was read, so no job missing from needs failed unseen."""
    if not full:
        return "", "", []
    if passed and not jobs_read:
        return "", "", ["Jobs audit or summary unavailable, so no tested-tree marker is uploaded."]
    if passed:
        prefix = MARKER_PREFIX
    elif any_failure(needs):
        prefix = FAILED_MARKER_PREFIX
    else:
        return "", "", []
    try:
        tree = tree_of()
    except Exception:  # noqa: BLE001 A tree lookup failure must preserve the verdict.
        tree = None
    if not isinstance(tree, str) or not _TREE.fullmatch(tree):
        return "", "", ["Cannot read the tree of HEAD, so no tested-tree marker is uploaded."]
    return prefix + tree, tree, []


def finish(env, fetch, tree_of, now):
    # type: (dict, github_api.Fetch, Callable[[], Optional[str]], datetime.datetime) -> Outcome
    needs = env.get("CI_FINISH_NEEDS", "")
    full_input = parse_full(env.get("CI_FINISH_FULL"))
    full = full_input is True
    problems, allowed_names = _verdict_from(env, full_input)
    repo = env.get("GITHUB_REPOSITORY", "")
    check_run_id = env.get("CI_FINISH_CHECK_RUN_ID", "")
    own_id = int(check_run_id) if re.fullmatch(r"[1-9][0-9]{0,19}", check_run_id) else None
    api = AuditFetch(fetch, JOBS_ATTEMPTS if full else 1)
    run_id = env.get("GITHUB_RUN_ID", "")
    jobs, read_error = fetch_jobs(repo, run_id, api.jobs)
    job_problems, incomplete, own_id = audit_jobs(jobs, own_id, full, allowed_names)
    problems.extend(job_problems)
    attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
    if full and attempt != "1":
        if not re.fullmatch(r"[1-9][0-9]*", attempt):
            incomplete.append("GITHUB_RUN_ATTEMPT is not a positive integer")
        else:
            attempt_error = first_attempt_problem(repo, run_id, api.jobs, jobs)
            if attempt_error:
                incomplete.append(attempt_error)
    notes = []
    if full and (read_error or incomplete):
        cause = ". ".join(([read_error] if read_error else []) + incomplete)
        problems.append("[jobs-audit-incomplete] %s. Re-run this job." % cause)
    if read_error:
        notes.append("Jobs API unavailable, summary built from needs: %s" % read_error)
    if own_id is not None:
        notes.append("Finishing job check run ID matched jobs API: %d" % own_id)
    elif not full and jobs:
        notes.append("Finishing job identity unavailable, unfinished jobs omitted from the verdict.")
    passed = not problems
    jobs_read = read_error is None and not incomplete
    footer = "not recorded"
    if full and passed and jobs_read:
        footer = "this run"
    else:
        try:
            answer = api.request("/repos/%s/actions/artifacts?per_page=100" % repo)
            artifacts = answer.get("artifacts")
            footer = last_full_pass(artifacts if isinstance(artifacts, list) else [], now)
        except Exception as error:  # noqa: BLE001 Only the footer depends on this.
            notes.append("Artifacts API unavailable: %s" % error)

    title, event = _title(env), env.get("GITHUB_EVENT_NAME", "")
    try:
        all_rows = table(needs, jobs if jobs_read else None, own_id,
                         _string_list(env.get("CI_FINISH_SEGMENTS", "")), env.get("CI_FINISH_SCOPE_REASON", ""),
                         passed, now)
        starts = [github_api.parse_time(j.get("started_at")) for j in jobs if isinstance(j, dict)] if jobs_read else []
        starts = [s for s in starts if s is not None]
        seconds = max(0.0, (now - min(starts)).total_seconds()) if starts else None
        summary = render(title, event, passed, all_rows, footer, seconds)
    except Exception as error:  # noqa: BLE001 Keep the audited verdict and withhold any pass marker.
        notes.append("Summary detail failed: " + type(error).__name__)
        jobs_read = False
        plain = table(needs, None, own_id, None, "", passed, now)
        summary = render(title, event, passed, plain, "not recorded")

    artifact, tree, marker_notes = marker_for(full, passed, needs, tree_of, jobs_read)
    return Outcome(passed, problems, summary, bool(artifact), tree, notes + marker_notes, artifact)


def head_tree():
    # type: () -> Optional[str]
    proc = subprocess.run(["git", "rev-parse", "HEAD^{tree}"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        return None
    return proc.stdout.decode("ascii", "replace").strip()


def write_marker(env, tree):
    # type: (dict, str) -> str
    """Writes the marker file the upload step sends, the same for a passed and a failed marker, and returns
    its path."""
    folder = os.path.join(env.get("RUNNER_TEMP") or tempfile.gettempdir(), "ci-tree-marker")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "marker.txt")
    run_url = "%s/%s/actions/runs/%s" % (
        env.get("GITHUB_SERVER_URL", "https://github.com"), env.get("GITHUB_REPOSITORY", ""),
        env.get("GITHUB_RUN_ID", ""))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("tree %s\nrun %s\n" % (tree, run_url))
    return path


def main():
    # type: () -> int
    env = dict(os.environ)
    outcome = finish(env, github_api.fetch_from_env(env), head_tree,
                     datetime.datetime.now(datetime.timezone.utc))
    for note in outcome.notes:
        print(note)
    for problem in outcome.problems:
        print("Not passed: %s" % _CONTROL.sub(" ", problem))
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(outcome.summary)
    else:
        sys.stdout.write(outcome.summary)
    marker = write_marker(env, outcome.tree) if outcome.upload else ""
    lines = [
        "passed=" + ("true" if outcome.passed else "false"),
        "upload=" + ("true" if outcome.upload else "false"),
        "tree=" + outcome.tree,
        "artifact=" + outcome.artifact,
        "marker=" + marker,
    ]
    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
