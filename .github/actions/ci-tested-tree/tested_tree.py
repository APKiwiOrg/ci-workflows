#!/usr/bin/env python3
"""Say whether the git tree being built already passed a full CI run, so a release can skip retesting it.

A full passing run leaves an artifact named ci-passed-full-TREE, where TREE is `git rev-parse HEAD^{tree}`.
The tree counts as tested only when such an artifact is unexpired and the run that made it concluded
`success`, ran the given workflow file, and belongs to this repository with its head in this repository
too. A marker is uploaded before its run ends, so a run with no conclusion yet does not count. Any other
answer, and any error, means no reuse with the reason stated.

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
# Older markers are only consulted when newer ones fail a check. This bounds the API calls one check makes.
MAX_RUNS_CHECKED = 10

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


def _run_problem(run, repo, workflow):
    """Why a run is not a full pass of the workflow in this repository, or None when it is."""
    if run.get("conclusion") is None:
        return "has not finished"
    if run.get("conclusion") != "success":
        return "concluded %s" % run.get("conclusion")
    if run.get("path") != workflow:
        return "ran %s, not %s" % (run.get("path"), workflow)
    if (run.get("repository") or {}).get("full_name") != repo:
        return "belongs to another repository"
    if (run.get("head_repository") or {}).get("full_name") != repo:
        return "ran a head from another repository"
    return None


def find_pass(repo, tree, workflow, fetch, now=None):
    # type: (str, str, str, github_api.Fetch, Optional[datetime.datetime]) -> Tuple[bool, str, str]
    """Returns (reuse, reason, run_url). Markers are checked newest first and the newest one whose run
    passes every check is used."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(tree, str) or not _TREE.fullmatch(tree):
        return False, "No tree to look up", ""
    if not workflow:
        return False, "No workflow file named", ""
    name = MARKER_PREFIX + tree
    try:
        answer = fetch("/repos/%s/actions/artifacts?name=%s&per_page=100"
                       % (repo, urllib.parse.quote(name, safe="")))
        artifacts = answer.get("artifacts")
        if not isinstance(artifacts, list):
            return False, "Marker lookup failed: the answer holds no artifact list", ""
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
        for _, run_id in dated[:MAX_RUNS_CHECKED]:
            run = fetch("/repos/%s/actions/runs/%d" % (repo, run_id))
            problem = _run_problem(run, repo, workflow)
            if problem is None:
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
