#!/usr/bin/env python3
"""Say whether the git tree being built already passed a full CI run, so a release can skip retesting it.

A full passing run leaves an artifact named ci-passed-full-TREE, where TREE is `git rev-parse HEAD^{tree}`.
The tree counts as tested only when such an artifact is unexpired and the run that made it concluded
`success`, ran the given workflow file on an event that tests this repository's own code (push,
pull_request, schedule, workflow_dispatch or merge_group), and belongs to this repository with its head in
this repository too. A pull_request_target or workflow_run run executes in this repository while the code it
tests can come from a fork, so it never vouches. A marker is uploaded before its run ends, so a run with no
conclusion yet does not count. For push, schedule, workflow_dispatch and merge_group runs,
the run's head commit must have the marker's tree. A pull request normally tests the merge commit, which the run API does
not name, so its marker still supplies the tree.

A full run that failed leaves ci-failed-full-TREE. When such a marker is unexpired, its run passes the same
origin and event checks, and it was made at or after the pass that would be reused, the newest evidence is a
failure and the tree is not reused. Its run's status and conclusion do not matter: only a verdict that did not
pass uploads one, so a run still completing is failing. Each marker lookup reads one page, so a lookup with
more markers than the page holds means no reuse. Any other answer, and any error, means no reuse with the
reason stated.

Inputs come from the environment: CI_TESTED_TREE_WORKFLOW (the workflow file whose full run counts),
CI_API_TOKEN and the runner's GITHUB_REPOSITORY, GITHUB_API_URL and GITHUB_OUTPUT. Outputs are appended to
GITHUB_OUTPUT as reuse, tree, reason and run-url. Python 3.8 or later, standard library only.
"""
import datetime
import os
import re
import subprocess
import sys
import urllib.parse
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "_shared"))
import github_api  # noqa: E402

DEFAULT_WORKFLOW = ".github/workflows/ci.yml"
MARKER_PREFIX = "ci-passed-full-"
FAILED_MARKER_PREFIX = "ci-failed-full-"
# Events whose runs can vouch for this repository's own code. Fork runs are refused separately.
HEAD_TREE_EVENTS = ("push", "schedule", "workflow_dispatch", "merge_group")
VOUCHING_EVENTS = HEAD_TREE_EVENTS + ("pull_request",)
# Older markers are only consulted when newer ones fail a check. This bounds the API calls one check makes.
MAX_RUNS_CHECKED = 10
ARTIFACTS_PER_PAGE = 100
_LATEST = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)

_TREE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _marker_problem(artifact, name, now):
    """Why an artifact cannot vouch for the tree, or None when it can, pending its run."""
    if not isinstance(artifact, dict) or artifact.get("name") != name:
        return "artifact is not the marker"
    if artifact.get("expired") is not False:
        return "marker has expired"
    expires = github_api.parse_time(artifact.get("expires_at"))
    if expires is None or expires <= now:
        return "marker has expired"
    run = artifact.get("workflow_run")
    if not isinstance(run, dict) or not isinstance(run.get("id"), int):
        return "marker names no run"
    return None


def _origin_problem(run, repo, workflow):
    """Why a run is not a run of the workflow on this repository's own code, or None when it is."""
    if run.get("path") != workflow:
        return "ran %s, not %s" % (run.get("path"), workflow)
    if (run.get("repository") or {}).get("full_name") != repo:
        return "belongs to another repository"
    if (run.get("head_repository") or {}).get("full_name") != repo:
        return "ran a head from another repository"
    if run.get("event") not in VOUCHING_EVENTS:
        return "ran on %s, which can test code from outside this repository" % run.get("event")
    return None


def _run_problem(run, repo, workflow):
    """Why a run is not a full pass of the workflow in this repository, or None when it is."""
    if run.get("conclusion") is None:
        return "has not finished"
    if run.get("conclusion") != "success":
        return "concluded %s" % run.get("conclusion")
    return _origin_problem(run, repo, workflow)


def _head_tree_problem(run, repo, tree, fetch):
    """Why a run's head cannot vouch for the marker, or None when it can."""
    head = run.get("head_sha")
    if not isinstance(head, str) or not _TREE.fullmatch(head):
        return "has no readable head SHA"
    commit = fetch("/repos/%s/git/commits/%s" % (repo, head))
    commit_tree = commit.get("tree")
    sha = commit_tree.get("sha") if isinstance(commit_tree, dict) else None
    if not isinstance(sha, str) or not _TREE.fullmatch(sha):
        return "head commit has no readable tree"
    if sha != tree:
        return "head commit has a different tree"
    return None


def _artifacts(repo, name, fetch):
    answer = fetch("/repos/%s/actions/artifacts?name=%s&per_page=%d"
                   % (repo, urllib.parse.quote(name, safe=""), ARTIFACTS_PER_PAGE))
    artifacts = answer.get("artifacts")
    if not isinstance(artifacts, list):
        raise github_api.ApiError("the answer for %s holds no artifact list" % name)
    total = answer.get("total_count")
    if not isinstance(total, int):
        raise github_api.ApiError("the answer for %s holds no count" % name)
    if total > len(artifacts):
        # A newer failed marker, or the pass that matters, could sit on a page not read.
        raise github_api.ApiError("more %s markers than one page holds (%d of %d listed)"
                                  % (name, len(artifacts), total))
    return artifacts


def newer_failure(repo, tree, workflow, since, fetch, now):
    # type: (str, str, str, datetime.datetime, github_api.Fetch, datetime.datetime) -> Optional[str]
    """The reason a failed full run of the tree made at or after `since` stops reuse, or None. A failed
    marker with no readable creation time counts as newer. Raises on an API error."""
    name = FAILED_MARKER_PREFIX + tree
    candidates = []
    for artifact in _artifacts(repo, name, fetch):
        if _marker_problem(artifact, name, now) is not None:
            continue
        created = github_api.parse_time(artifact.get("created_at")) or _LATEST
        if created >= since:
            candidates.append((created, artifact["workflow_run"]["id"]))
    candidates.sort(reverse=True)
    for _, run_id in candidates[:MAX_RUNS_CHECKED]:
        run = fetch("/repos/%s/actions/runs/%d" % (repo, run_id))
        if _origin_problem(run, repo, workflow) is None:
            return "Run %d failed %s in full on this tree after the pass: %s" % (
                run_id, workflow, run.get("html_url") or "no link")
    if len(candidates) > MAX_RUNS_CHECKED:
        return "More than %d newer failed full runs of this tree to check" % MAX_RUNS_CHECKED
    return None


def find_pass(repo, tree, workflow, fetch, now=None):
    # type: (str, str, str, github_api.Fetch, Optional[datetime.datetime]) -> Tuple[bool, str, str]
    """Returns (reuse, reason, run_url). Markers are checked newest first and the newest one whose run
    passes every check is used, unless a failed full run of the tree is as new or newer."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(tree, str) or not _TREE.fullmatch(tree):
        return False, "No tree to look up", ""
    if not workflow:
        return False, "No workflow file named", ""
    name = MARKER_PREFIX + tree
    try:
        artifacts = _artifacts(repo, name, fetch)
        dated = []
        first_problem = None
        for artifact in artifacts:
            problem = _marker_problem(artifact, name, now)
            created = github_api.parse_time(artifact.get("created_at")) if problem is None else None
            if problem is None and created is None:
                problem = "marker has no creation time"
            if problem is not None:
                first_problem = first_problem or problem
                continue
            dated.append((created, artifact["workflow_run"]["id"]))
        if not dated:
            return False, "No usable marker for this tree: %s" % (first_problem or "none uploaded"), ""
        dated.sort(reverse=True)
        newest_problem = None
        for created, run_id in dated[:MAX_RUNS_CHECKED]:
            run = fetch("/repos/%s/actions/runs/%d" % (repo, run_id))
            problem = _run_problem(run, repo, workflow)
            if problem is None:
                if run.get("event") in HEAD_TREE_EVENTS:
                    tree_problem = _head_tree_problem(run, repo, tree, fetch)
                    if tree_problem:
                        return False, "Marker from run %d %s" % (run_id, tree_problem), ""
                failure = newer_failure(repo, tree, workflow, created, fetch, now)
                if failure:
                    return False, failure, ""
                url = run.get("html_url") or ""
                return True, "Run %d passed %s in full on this tree" % (run_id, workflow), url
            newest_problem = newest_problem or "run %d %s" % (run_id, problem)
        return False, "No full pass of %s for this tree: newest marker's %s" % (workflow, newest_problem), ""
    except Exception as error:  # noqa: BLE001 Any doubt at all means the tree is retested.
        return False, "Marker lookup failed: %s" % error, ""


def output_lines(reuse, tree, reason, run_url):
    # type: (bool, str, str, str) -> List[str]
    return [
        "reuse=" + ("true" if reuse else "false"),
        "tree=" + tree,
        "reason=" + _CONTROL.sub(" ", reason),
        "run-url=" + _CONTROL.sub(" ", run_url),
    ]


def head_tree():
    # type: () -> Optional[str]
    proc = subprocess.run(["git", "rev-parse", "HEAD^{tree}"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tree = proc.stdout.decode("ascii", "replace").strip()
    if proc.returncode != 0 or not _TREE.fullmatch(tree):
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        return None
    return tree


def main():
    # type: () -> int
    env = os.environ
    try:
        tree = head_tree()
        if tree is None:
            reuse, reason, run_url = False, "Cannot read the tree of HEAD. Check the repository out first.", ""
        else:
            workflow = env.get("CI_TESTED_TREE_WORKFLOW") or DEFAULT_WORKFLOW
            reuse, reason, run_url = find_pass(
                env.get("GITHUB_REPOSITORY", ""), tree, workflow, github_api.fetch_from_env(env))
    except Exception as error:  # noqa: BLE001 A crash here must still answer no reuse.
        tree, reuse, reason, run_url = None, False, "Check failed: %s" % type(error).__name__, ""
    lines = output_lines(reuse, tree or "", reason, run_url)
    for line in lines:
        print(line)
    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
