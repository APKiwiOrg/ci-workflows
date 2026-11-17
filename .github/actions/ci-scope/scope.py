#!/usr/bin/env python3
"""Decide which test segments a CI run needs from the files its change touched.

The rules file lists segments and the globs that select them:

    unit: src/core/** tests/core/**
    full: .github/** *.props
    ignore: docs/** **/*.md

A push to a branch is compared with the newest passing run of the same workflow on that branch whose commit
is an ancestor of HEAD, read from the Actions API. So a segment that failed, or a push whose run was
cancelled, stays selected until a run passes, and a push never selects less than every change since the last
run that passed. A pull request is compared three-dot with its base.

A run is full when the event or ref asks for one, when the rules cannot be read, when there is no base
commit to compare with (no passing run on the branch, an API error, a base the clone cannot fetch), when the
rules file itself changed, when a changed file matches a `full` glob, or when a changed file matches no rule
at all, so a new folder is never silently left untested. Otherwise the run needs the segments whose globs
match a changed file. On a full run every test job runs, whatever `segments` lists. `segments` then holds the
segments the rules name, which is empty when the rules file is missing or empty.

Inputs come from the environment: CI_SCOPE_RULES (the rules file path), CI_API_TOKEN and the runner's
GITHUB_EVENT_NAME, GITHUB_EVENT_PATH, GITHUB_REF_TYPE, GITHUB_REF_NAME, GITHUB_SHA, GITHUB_REPOSITORY,
GITHUB_WORKFLOW_REF, GITHUB_RUN_ID, GITHUB_API_URL and GITHUB_OUTPUT. Outputs are appended to GITHUB_OUTPUT
as full, segments (compact JSON), reason and changed. Python 3.8 or later, standard library only.
"""
import datetime
import functools
import json
import os
import posixpath
import re
import subprocess
import sys
import urllib.parse
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "_shared"))
import github_api  # noqa: E402

DEFAULT_RULES = ".github/ci-scope.txt"
FULL_EVENTS = ("schedule", "workflow_dispatch")
RESERVED = ("full", "ignore")
NO_BASE = "Full run: no base commit to compare"
# Events whose passing run tested this branch's own commit, so its commit can be a push's base. A pull
# request run tested a merge commit, which is not on the branch.
BASE_EVENTS = ("push", "schedule", "workflow_dispatch")
RUNS_PER_PAGE = 20

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

# A git runner takes the arguments after `git` and returns the exit code and standard output.
GitRunner = Callable[[Sequence[str]], Tuple[int, bytes]]


class Workflow(NamedTuple):
    """The running workflow, for finding its last passing run on the branch. Empty fields are unknown."""
    repo: str
    # Repository-relative path of the workflow file, such as .github/workflows/ci.yml.
    path: str
    branch: str
    run_id: str


class Rules(NamedTuple):
    segments: Dict[str, List[str]]
    full: List[str]
    ignore: List[str]
    # The full-run reason for the first malformed line, or None when every line parsed.
    error: Optional[str] = None


class Scope(NamedTuple):
    full: bool
    segments: List[str]
    reason: str
    # How many files the change touched, or None when the run went full before comparing.
    changed: Optional[int]


def parse_rules(text):
    # type: (str) -> Rules
    """Parse NAME: GLOB GLOB ... lines. A bad line is recorded and parsing carries on, so a full run
    caused by it can still list every segment the file names."""
    segments = {}  # type: Dict[str, List[str]]
    full = []  # type: List[str]
    ignore = []  # type: List[str]
    error = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, colon, rest = line.partition(":")
        name = name.strip()
        globs = rest.split()
        problem = None
        if not colon or not globs or not _NAME.fullmatch(name):
            problem = "is not NAME: GLOBS"
        elif name.lower() in RESERVED and name not in RESERVED:
            # `Full:` would otherwise be a segment no job runs, so shared files would select nothing.
            problem = "names a segment %s, which is reserved" % name
        elif any(glob.startswith("/") or glob.endswith("/") or "//" in glob for glob in globs):
            # Paths are repository-relative with no empty segment, so such a glob never matches.
            problem = "has a glob that starts or ends with / or holds //"
        if problem:
            if error is None:
                error = "Full run: rules file line %d %s" % (number, problem)
            continue
        if name == "full":
            full.extend(globs)
        elif name == "ignore":
            ignore.extend(globs)
        else:
            segments.setdefault(name, []).extend(globs)
    return Rules(segments, full, ignore, error)


@functools.lru_cache(maxsize=None)
def _compile(glob):
    """One entry per path segment: None for `**`, otherwise a regex for that segment. A glob without
    a slash matches a file of that name at any depth, the same as `**/` in front of it."""
    parts = glob.split("/") if "/" in glob else ["**", glob]
    compiled = []
    for part in parts:
        if part == "**":
            compiled.append(None)
            continue
        pattern = "".join(
            ".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in part)
        compiled.append(re.compile(pattern, re.DOTALL))
    return tuple(compiled)


def matches(glob, path):
    # type: (str, str) -> bool
    """True when the glob matches the whole repository-relative path. `**` matches any number of path
    segments including none, `*` any run of characters within one segment and `?` one character."""
    parts = path.split("/")
    # Indexes into parts that the pattern so far can have consumed up to.
    reached = {0}
    for segment in _compile(glob):
        if segment is None:
            reached = set(range(min(reached), len(parts) + 1))
        else:
            reached = {i + 1 for i in reached if i < len(parts) and segment.fullmatch(parts[i])}
        if not reached:
            return False
    return len(parts) in reached


def _any(globs, path):
    return any(matches(glob, path) for glob in globs)


def _count(files):
    return "1 file changed" if len(files) == 1 else "%d files changed" % len(files)


def select(rules, files, since=""):
    # type: (Rules, Sequence[str], str) -> Tuple[bool, List[str], str]
    """Pick the segments a set of changed files needs. Returns (full, segments, reason). `since` follows the
    file count in the reason of a partial run, naming what the files changed since."""
    every = sorted(rules.segments)
    for path in files:
        if _any(rules.full, path):
            return True, every, "Full run: %s changes shared files" % path
    hit = set()
    for path in files:
        named = [name for name, globs in rules.segments.items() if _any(globs, path)]
        if not named and not _any(rules.ignore, path):
            return True, every, "Full run: %s matches no rule" % path
        hit.update(named)
    segments = sorted(hit)
    if segments:
        return False, segments, "%s%s. Segments: %s" % (_count(files), since, ", ".join(segments))
    return False, segments, "%s%s. No segment affected" % (_count(files), since)


def _is_sha(value):
    # Anything that is not a sha is refused before it can reach git as an argument, where a leading dash
    # would read as an option. All zeros is no commit.
    return isinstance(value, str) and bool(_SHA.fullmatch(value)) and bool(value.strip("0"))


def full_event_reason(event_name, ref_type):
    # type: (str, str) -> Optional[str]
    """The reason when the event or ref always runs everything, otherwise None."""
    if event_name in FULL_EVENTS:
        return "Full run: " + event_name
    if ref_type == "tag":
        return "Full run: tag"
    return None


def pull_request_base(event):
    # type: (dict) -> Optional[str]
    base = ((event.get("pull_request") or {}).get("base") or {}).get("sha")
    return base if _is_sha(base) else None


def _base_candidate(run, workflow):
    """The run's commit when the run is a pass of this workflow on this repository's own code, else None."""
    if not isinstance(run, dict) or run.get("conclusion") != "success":
        return None
    if run.get("path") != workflow.path or str(run.get("id")) == workflow.run_id:
        return None
    if ((run.get("repository") or {}).get("full_name") != workflow.repo
            or (run.get("head_repository") or {}).get("full_name") != workflow.repo):
        return None
    if run.get("event") not in BASE_EVENTS:
        return None
    sha = run.get("head_sha")
    return sha if _is_sha(sha) else None


def _created(run):
    # Runs with no readable time sort as oldest.
    return github_api.parse_time(run.get("created_at") if isinstance(run, dict) else None) or _EPOCH


def last_passing_base(workflow, head, fetch, git):
    # type: (Workflow, str, github_api.Fetch, GitRunner) -> Tuple[Optional[str], str]
    """The commit of the newest passing run of the workflow on the branch that is in the clone and an
    ancestor of HEAD. Returns (base, "") or (None, the full-run reason)."""
    if not (workflow.repo and workflow.path and workflow.branch):
        return None, "Full run: cannot tell which workflow and branch this run belongs to"
    path = "/repos/%s/actions/workflows/%s/runs?branch=%s&status=success&per_page=%d" % (
        workflow.repo, urllib.parse.quote(posixpath.basename(workflow.path), safe=""),
        urllib.parse.quote(workflow.branch, safe=""), RUNS_PER_PAGE)
    try:
        runs = fetch(path).get("workflow_runs")
        if not isinstance(runs, list):
            raise github_api.ApiError("the answer holds no run list")
    except Exception as error:  # noqa: BLE001 Any doubt about the base means a full run.
        return None, "Full run: the last passing run could not be read (%s)" % error
    for run in sorted(runs, key=_created, reverse=True):
        sha = _base_candidate(run, workflow)
        # A commit the clone does not hold is passed over: it was force-pushed away or never fetched.
        if (sha and git(["cat-file", "-e", sha + "^{commit}"])[0] == 0
                and git(["merge-base", "--is-ancestor", sha, head])[0] == 0):
            return sha, ""
    return None, "Full run: no passing run of this workflow on %s to compare with" % workflow.branch


def changed_files(base, head, three_dot, git):
    # type: (str, str, bool, GitRunner) -> Optional[List[str]]
    """The paths the change touched, or None when the base cannot be found or compared."""
    if git(["cat-file", "-e", base + "^{commit}"])[0] != 0:
        git(["fetch", "--no-tags", "--depth=1", "origin", base])
        if git(["cat-file", "-e", base + "^{commit}"])[0] != 0:
            return None
    # --no-renames lists a moved file under both its old and new path, so both sides select segments.
    spec = base + ("..." if three_dot else "..") + head
    code, out = git(["diff", "--name-only", "-z", "--no-renames", spec])
    if code != 0:
        return None
    return [name for name in out.decode("utf-8", "surrogateescape").split("\0") if name]


def _repo_path(path):
    return posixpath.normpath(path.replace("\\", "/"))


def decide(event_name, event, ref_type, rules_text, head, git, fetch, workflow, rules_path=DEFAULT_RULES):
    # type: (str, dict, str, Optional[str], str, GitRunner, github_api.Fetch, Workflow, str) -> Scope
    """Apply the selection rules in order. rules_text is None when the rules file cannot be read. On a full
    run `segments` lists every segment the rules name, which may be none."""
    rules = parse_rules(rules_text) if rules_text is not None else None
    every = sorted(rules.segments) if rules is not None else []
    reason = full_event_reason(event_name, ref_type)
    if reason:
        return Scope(True, every, reason, None)
    if rules is None:
        return Scope(True, every, "Full run: no rules file", None)
    if rules.error:
        return Scope(True, every, rules.error, None)
    since = ""
    three_dot = False
    if event_name == "push":
        base, reason = last_passing_base(workflow, head, fetch, git)
        if base is None:
            return Scope(True, every, reason, None)
        since = " since the last passing run (%s)" % base[:7]
    elif event_name == "pull_request":
        base, three_dot = pull_request_base(event), True
    else:
        base = None
    files = changed_files(base, head, three_dot, git) if base else None
    if files is None:
        return Scope(True, every, NO_BASE, None)
    if _repo_path(rules_path) in files:
        # New rules must not decide how much of their own change is tested.
        return Scope(True, every, "Full run: %s changes the rules" % _repo_path(rules_path), len(files))
    full, segments, reason = select(rules, files, since)
    return Scope(full, segments, reason, len(files))


def _one_line(text):
    # File names can hold control characters or bytes that are not UTF-8. An output value is one line.
    text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return _CONTROL.sub(" ", text)


def output_lines(result):
    # type: (Scope) -> List[str]
    return [
        "full=" + ("true" if result.full else "false"),
        "segments=" + json.dumps(result.segments, separators=(",", ":")),
        "reason=" + _one_line(result.reason),
        "changed=" + ("" if result.changed is None else str(result.changed)),
    ]


def run_git(args):
    # type: (Sequence[str]) -> Tuple[int, bytes]
    proc = subprocess.run(["git"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # cat-file -e and merge-base --is-ancestor failing is an answer, not an error. Other failures go to the
    # log for diagnosis.
    if proc.returncode != 0 and args[0] not in ("cat-file", "merge-base"):
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    return proc.returncode, proc.stdout


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _read_event(path):
    text = _read_text(path) if path else None
    try:
        event = json.loads(text) if text else {}
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def workflow_from_env(env):
    # type: (Dict[str, str]) -> Workflow
    """GITHUB_WORKFLOW_REF reads OWNER/NAME/.github/workflows/FILE@REF. A ref of another repository leaves
    the path empty, so the run goes full rather than comparing with the wrong workflow."""
    repo = env.get("GITHUB_REPOSITORY", "")
    ref = env.get("GITHUB_WORKFLOW_REF", "")
    path = ""
    if repo and ref.startswith(repo + "/"):
        path = ref[len(repo) + 1:].split("@", 1)[0]
    return Workflow(repo, path, env.get("GITHUB_REF_NAME", ""), env.get("GITHUB_RUN_ID", ""))


def main():
    # type: () -> int
    env = os.environ
    head = env.get("GITHUB_SHA", "")
    if not _SHA.fullmatch(head):
        head = "HEAD"
    rules_path = env.get("CI_SCOPE_RULES") or DEFAULT_RULES
    result = decide(
        env.get("GITHUB_EVENT_NAME", ""),
        _read_event(env.get("GITHUB_EVENT_PATH")),
        env.get("GITHUB_REF_TYPE", ""),
        _read_text(rules_path),
        head,
        run_git,
        github_api.fetch_from_env(env),
        workflow_from_env(env),
        rules_path,
    )
    lines = output_lines(result)
    for line in lines:
        print(line)
    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
