#!/usr/bin/env python3
"""Give a CI run one verdict and one summary, and prepare the tested-tree marker of a full run.

The verdict starts from the calling job's `needs`: a job that failed or was cancelled fails the run, and
input that is not a JSON object naming at least one job fails it, as does a `full` input other than true or
false. A skipped job fails it unless its id is in the segment list on a run that is not full, or in the
always-allowed list and not the segment list, since a full run runs every segment. The jobs API can then only
turn a pass into a fail. A job that has not finished, or that failed, timed out or was cancelled, fails
the run. On a full run a skipped job also fails it unless its exact API name is always allowed. This
includes jobs inside reusable workflows that are not individually in `needs`. A job that needs the
finishing job is not listed by the API while the
finishing job runs, so it is never counted. An API error leaves the verdict from `needs` as it is. The API
otherwise supplies the summary's stage names and times, and the artifacts API its footer.

A full run that passed prepares the ci-passed-full-TREE marker, but only when the jobs API was read, since
otherwise a job missing from `needs` could have failed unseen. A full run that failed with at least one job
in `needs` reporting `failure` prepares ci-failed-full-TREE instead, so ci-tested-tree knows a newer full run
of the tree failed. A run whose only problems are cancelled or wrongly skipped jobs prepares neither.

Inputs come from the environment: CI_FINISH_NEEDS, CI_FINISH_ALLOW_SKIPPED, CI_FINISH_ALLOW_SKIPPED_ALWAYS,
CI_FINISH_FULL, CI_FINISH_SEGMENTS, CI_FINISH_SCOPE_REASON, CI_FINISH_TITLE, CI_FINISH_JOB_NAME,
CI_API_TOKEN and the runner's GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_EVENT_NAME, GITHUB_SERVER_URL,
GITHUB_API_URL, GITHUB_STEP_SUMMARY, GITHUB_OUTPUT and RUNNER_TEMP. The summary is appended to
GITHUB_STEP_SUMMARY and the outputs passed, upload, tree, artifact and marker to GITHUB_OUTPUT. The script
itself always exits 0, so the action's upload and verdict steps still run. Python 3.8 or later, standard
library only.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "_shared"))
import github_api  # noqa: E402

DEFAULT_JOB_NAME = "CI passed"
MARKER_PREFIX = "ci-passed-full-"
FAILED_MARKER_PREFIX = "ci-failed-full-"
# Job conclusions from the API that fail the run when `needs` does not report the job.
FAILING_CONCLUSIONS = ("failure", "timed_out", "cancelled")
NOT_AN_OBJECT = "needs input is not a JSON object"
BAD_FULL = "full input is not true or false"
NOT_AFFECTED = "Not affected by this change"
JOBS_PER_PAGE = 100
JOB_PAGES = 3

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


def parse_allowed(text):
    # type: (str) -> Set[str]
    return {name for name in re.split(r"[\s,]+", text or "") if name}


def parse_always_allowed(text):
    # type: (str) -> Set[str]
    """Accept the legacy job ID list or a JSON array with exact API names and job IDs."""
    if not text.lstrip().startswith("["):
        return parse_allowed(text)
    values = _string_list(text)
    return set(values) if values is not None else set()


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


def unreported_problems(jobs, own_job_name, full, allowed_skips):
    # type: (Sequence[dict], str, bool, Set[str]) -> List[str]
    """Problems visible in the jobs API after needs reported a pass."""
    problems = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = job.get("name")
        status = job.get("status")
        conclusion = job.get("conclusion")
        if status == "completed" and conclusion == "skipped":
            if full and name not in allowed_skips:
                problems.append("%s was skipped on a full run" % name)
        elif _is_own(name, own_job_name):
            continue
        elif status != "completed":
            problems.append("%s has not finished and needs does not wait for it" % name)
        elif conclusion in FAILING_CONCLUSIONS:
            problems.append("%s: %s, not reported by needs (missing from needs or allowed to fail)"
                            % (name, conclusion))
    return problems


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
    # Other API conclusions such as timed_out are shown as failures. They never reach the verdict.
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


def _is_own(name, own_job_name):
    return name == own_job_name or (isinstance(name, str) and name.endswith(" / " + own_job_name))


def rows(jobs, own_job_name, segments, scope_reason):
    # type: (Sequence[dict], str, Optional[Sequence[str]], str) -> List[Row]
    """One row per stage-named job, else per stage-named step, else one Other row. The job running this
    action is left out, since its row is the verdict and comes last."""
    table = []
    for job in jobs:
        if not isinstance(job, dict) or _is_own(job.get("name"), own_job_name):
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
        table.append(Row(str(job_id), WORDS.get(result, "Failed"), None, ""))
    return table


def table(needs, jobs, own_job_name, segments, scope_reason, passed, now):
    # type: (Any, Optional[Sequence[dict]], str, Optional[Sequence[str]], str, bool, datetime.datetime)
    # -> List[Row]
    """Every row of the summary. jobs is None when the API could not be read."""
    if jobs is None:
        body = needs_rows(needs)
        own = None
    else:
        body = rows(jobs, own_job_name, segments, scope_reason)
        own = next((j for j in jobs if isinstance(j, dict) and _is_own(j.get("name"), own_job_name)), None)
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


def fetch_jobs(repo, run_id, fetch):
    # type: (str, str, github_api.Fetch) -> List[dict]
    """The run's jobs, up to JOB_PAGES pages. A longer run keeps the rows it has. Raises ApiError."""
    jobs = []  # type: List[dict]
    for page in range(1, JOB_PAGES + 1):
        answer = fetch("/repos/%s/actions/runs/%s/jobs?per_page=%d&page=%d"
                       % (repo, run_id, JOBS_PER_PAGE, page))
        batch = answer.get("jobs")
        if not isinstance(batch, list):
            raise github_api.ApiError("jobs answer holds no job list")
        jobs.extend(batch)
        if len(batch) < JOBS_PER_PAGE:
            break
    return jobs


def _string_list(text):
    try:
        value = json.loads(text) if text else None
    except ValueError:
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


def _full(env):
    # Only a full run that says so is marked. An unreadable value fails the verdict anyway.
    return parse_full(env.get("CI_FINISH_FULL")) is True


def _verdict_from(env):
    full = parse_full(env.get("CI_FINISH_FULL"))
    passed, problems = verdict(
        env.get("CI_FINISH_NEEDS", ""), parse_allowed(env.get("CI_FINISH_ALLOW_SKIPPED", "")),
        parse_always_allowed(env.get("CI_FINISH_ALLOW_SKIPPED_ALWAYS", "")), full is True)
    if full is None:
        # Read as partial, `allow-skipped` would excuse every segment skip, so the run cannot pass.
        return False, [BAD_FULL] + problems
    return passed, problems


def marker_for(full, passed, needs, tree_of, jobs_read=True):
    # type: (bool, bool, Any, Callable[[], Optional[str]], bool) -> Tuple[str, str, List[str]]
    """Returns (artifact, tree, notes). The artifact is empty when no marker is due or the tree is unreadable.
    A pass is marked only when the jobs API was read, so no job missing from needs failed unseen."""
    if not full:
        return "", "", []
    if passed and not jobs_read:
        return "", "", ["Jobs API not read, so no tested-tree marker is uploaded."]
    if passed:
        prefix = MARKER_PREFIX
    elif any_failure(needs):
        prefix = FAILED_MARKER_PREFIX
    else:
        return "", "", []
    tree = tree_of() or ""
    if not _TREE.fullmatch(tree):
        return "", "", ["Cannot read the tree of HEAD, so no tested-tree marker is uploaded."]
    return prefix + tree, tree, []


def finish(env, fetch, tree_of, now):
    # type: (dict, github_api.Fetch, Callable[[], Optional[str]], datetime.datetime) -> Outcome
    needs = env.get("CI_FINISH_NEEDS", "")
    passed, problems = _verdict_from(env)
    full = _full(env)
    repo = env.get("GITHUB_REPOSITORY", "")
    own_job_name = env.get("CI_FINISH_JOB_NAME") or DEFAULT_JOB_NAME
    notes = []
    try:
        jobs = fetch_jobs(repo, env.get("GITHUB_RUN_ID", ""), fetch)  # type: Optional[List[dict]]
    except Exception as error:  # noqa: BLE001 The summary falls back to needs. The verdict stays as needs set it.
        notes.append("Jobs API unavailable, summary built from needs: %s" % error)
        jobs = None
    if passed and jobs is not None:
        # The API can only turn a pass into a fail, never the reverse.
        allowed_skips = (parse_always_allowed(env.get("CI_FINISH_ALLOW_SKIPPED_ALWAYS", ""))
                         - parse_allowed(env.get("CI_FINISH_ALLOW_SKIPPED", "")))
        unreported = unreported_problems(jobs, own_job_name, full, allowed_skips)
        if unreported:
            passed, problems = False, problems + unreported
    all_rows = table(needs, jobs, own_job_name,
                     _string_list(env.get("CI_FINISH_SEGMENTS", "")), env.get("CI_FINISH_SCOPE_REASON", ""),
                     passed, now)
    starts = [github_api.parse_time(j.get("started_at")) for j in jobs or [] if isinstance(j, dict)]
    starts = [s for s in starts if s is not None]
    seconds = max(0.0, (now - min(starts)).total_seconds()) if starts else None
    if full and passed and jobs is not None:
        footer = "this run"
    else:
        try:
            answer = fetch("/repos/%s/actions/artifacts?per_page=100" % repo)
            artifacts = answer.get("artifacts")
            footer = last_full_pass(artifacts if isinstance(artifacts, list) else [], now)
        except Exception as error:  # noqa: BLE001 Only the footer depends on this.
            notes.append("Artifacts API unavailable: %s" % error)
            footer = "not recorded"
    summary = render(_title(env), env.get("GITHUB_EVENT_NAME", ""), passed, all_rows, footer, seconds)
    artifact, tree, marker_notes = marker_for(full, passed, needs, tree_of, jobs is not None)
    return Outcome(passed, problems, summary, bool(artifact), tree, notes + marker_notes, artifact)


def plain_outcome(env, note, tree_of):
    # type: (dict, str, Callable[[], Optional[str]]) -> Outcome
    """The verdict with a summary built from needs alone, for when building the full summary crashed. It
    never prepares a pass marker, since the jobs API check did not run, but still prepares a failed one."""
    passed, problems = _verdict_from(env)
    needs = env.get("CI_FINISH_NEEDS", "")
    plain = needs_rows(needs) + [Row(DEFAULT_JOB_NAME, "Passed" if passed else "Failed", None, "")]
    summary = render(_title(env), env.get("GITHUB_EVENT_NAME", ""), passed, plain, "not recorded")
    artifact, tree, notes = marker_for(_full(env), passed, needs, tree_of, jobs_read=False)
    return Outcome(passed, problems, summary, bool(artifact), tree, [note] + notes, artifact)


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
    try:
        outcome = finish(env, github_api.fetch_from_env(env), head_tree,
                         datetime.datetime.now(datetime.timezone.utc))
    except Exception as error:  # noqa: BLE001 Keep the verdict and a plain summary whatever went wrong.
        outcome = plain_outcome(env, "Summary detail failed: " + type(error).__name__, head_tree)
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
