"""Tests for .github/actions/ci-tested-tree/tested_tree.py and the API helper in .github/actions/_shared.

Run from the repository root:
    python3 -m unittest discover -s scripts/tests -p 'test_ci_*.py' -v

Every refusal sits beside a control that differs only in that one thing. A fake fetch returns canned
dictionaries, so no test reaches the network. The one real-process test points the API at a closed local
port.
"""
import datetime
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ACTIONS = os.path.join(ROOT, ".github", "actions")
SCRIPT = os.path.join(ACTIONS, "ci-tested-tree", "tested_tree.py")

_spec = importlib.util.spec_from_file_location("ci_tested_tree", SCRIPT)
tested_tree = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tested_tree)
github_api = tested_tree.github_api

REPO = "example-org/example-app"
HEAD = "b" * 40
TREE = "c" * 40
WORKFLOW = ".github/workflows/ci.yml"
NOW = datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
RUN_URL = "https://github.com/example-org/example-app/actions/runs/%d"
ARTIFACTS_PATH = "/repos/%s/actions/artifacts?name=ci-passed-full-%s&per_page=100" % (REPO, TREE)
COMMIT_PATH = "/repos/%s/git/commits/%s" % (REPO, HEAD)
FAILED_PATH = "/repos/%s/actions/artifacts?name=ci-failed-full-%s&per_page=100" % (REPO, TREE)


def artifact(run_id=101, created="2026-01-19T10:00:00Z", expires="2026-02-18T10:00:00Z", expired=False,
             name="ci-passed-full-" + TREE):
    return {
        "name": name,
        "expired": expired,
        "created_at": created,
        "expires_at": expires,
        "workflow_run": {"id": run_id, "repository_id": 1, "head_repository_id": 1},
    }


def failed(run_id=201, created="2026-01-19T12:00:00Z", **kwargs):
    return artifact(run_id=run_id, created=created, name="ci-failed-full-" + TREE, **kwargs)


def run(run_id=101, conclusion="success", path=WORKFLOW, repo=REPO, head_repo=REPO, event="push"):
    return {
        "id": run_id,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "event": event,
        "head_sha": HEAD,
        "path": path,
        "html_url": RUN_URL % run_id,
        "repository": {"full_name": repo},
        "head_repository": {"full_name": head_repo},
    }


class FakeFetch(object):
    """Answers API paths from a dictionary. A missing path or an exception value raises ApiError."""

    def __init__(self, artifacts=None, runs=(), raw=None, failures=()):
        self.answers = {
            FAILED_PATH: {"total_count": len(failures), "artifacts": list(failures)},
            COMMIT_PATH: {"sha": HEAD, "tree": {"sha": TREE}},
        }
        self.answers.update(raw or {})
        if artifacts is not None:
            self.answers[ARTIFACTS_PATH] = {"total_count": len(artifacts), "artifacts": artifacts}
        for item in runs:
            self.answers["/repos/%s/actions/runs/%d" % (REPO, item["id"])] = item

    def __call__(self, path):
        answer = self.answers.get(path)
        if answer is None:
            raise github_api.ApiError("HTTP 404 for " + path)
        if isinstance(answer, Exception):
            raise answer
        return answer


def check(fetch, workflow=WORKFLOW, tree=TREE, repo=REPO):
    return tested_tree.find_pass(repo, tree, workflow, fetch, now=NOW)


class FindPassTests(unittest.TestCase):
    def assertNoReuse(self, result):
        reuse, reason, run_url = result
        self.assertIs(reuse, False)
        self.assertTrue(reason)
        self.assertEqual(run_url, "")

    def test_control_full_pass_of_this_workflow_is_reused(self):
        reuse, reason, run_url = check(FakeFetch([artifact()], [run()]))
        self.assertIs(reuse, True)
        self.assertEqual(run_url, RUN_URL % 101)
        self.assertTrue(reason)

    def test_artifacts_api_error_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch(raw={ARTIFACTS_PATH: github_api.ApiError("HTTP 500")})))

    def test_run_api_error_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [])))

    def test_unexpected_exception_from_fetch_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch(raw={ARTIFACTS_PATH: RuntimeError("boom")})))

    def test_empty_artifact_list_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([], [run()])))

    def test_artifacts_answer_without_a_list_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch(raw={ARTIFACTS_PATH: {"total_count": 1}})))

    def test_more_pass_markers_than_one_page_is_no_reuse(self):
        answer = {"total_count": 101, "artifacts": [artifact()]}
        reuse, reason, run_url = check(FakeFetch(runs=[run()], raw={ARTIFACTS_PATH: answer}))
        self.assertIs(reuse, False)
        self.assertIn("more ci-passed-full-%s markers than one page holds" % TREE, reason)
        self.assertEqual(run_url, "")

    def test_pass_marker_answer_without_a_count_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch(runs=[run()], raw={ARTIFACTS_PATH: {"artifacts": [artifact()]}})))

    def test_run_still_going_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run(conclusion=None)])))

    def test_failed_run_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run(conclusion="failure")])))

    def test_marker_from_another_workflow_file_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run(path=".github/workflows/nightly.yml")])))

    def test_expired_marker_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact(expired=True)], [run()])))

    def test_marker_past_its_expiry_time_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact(expires="2026-01-20T11:59:59Z")], [run()])))

    def test_marker_without_a_readable_expiry_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact(expires="soon")], [run()])))

    def test_marker_with_expired_missing_is_no_reuse(self):
        marker = artifact()
        del marker["expired"]
        self.assertNoReuse(check(FakeFetch([marker], [run()])))

    def test_run_in_another_repository_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run(repo="someone/example-app")])))

    def test_run_from_a_fork_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run(head_repo="someone/example-app")])))

    def test_marker_with_another_name_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact(name="ci-passed-full-" + "d" * 40)], [run()])))

    def test_marker_without_a_run_is_no_reuse(self):
        marker = artifact()
        del marker["workflow_run"]
        self.assertNoReuse(check(FakeFetch([marker], [run()])))

    def test_tree_that_is_not_a_hash_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run()]), tree="HEAD"))

    def test_empty_workflow_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run()]), workflow=""))

    def test_newest_passing_marker_is_used(self):
        older = artifact(run_id=101, created="2026-01-18T10:00:00Z")
        newer = artifact(run_id=102, created="2026-01-19T10:00:00Z")
        reuse, _, run_url = check(FakeFetch([older, newer], [run(101), run(102)]))
        self.assertIs(reuse, True)
        self.assertEqual(run_url, RUN_URL % 102)

    def test_older_full_pass_counts_when_the_newest_marker_does_not(self):
        older = artifact(run_id=101, created="2026-01-18T10:00:00Z")
        newer = artifact(run_id=102, created="2026-01-19T10:00:00Z")
        fetch = FakeFetch([newer, older], [run(101), run(102, conclusion=None)])
        reuse, _, run_url = check(fetch)
        self.assertIs(reuse, True)
        self.assertEqual(run_url, RUN_URL % 101)


    def test_runs_of_events_that_test_this_repository_are_reused(self):
        for event in ("push", "pull_request", "schedule", "workflow_dispatch", "merge_group"):
            with self.subTest(event=event):
                reuse, _, _ = check(FakeFetch([artifact()], [run(event=event)]))
                self.assertIs(reuse, True)

    def test_marker_for_another_commit_tree_is_no_reuse(self):
        other_commit = {"sha": HEAD, "tree": {"sha": "d" * 40}}
        for event in ("push", "schedule", "workflow_dispatch", "merge_group"):
            with self.subTest(event=event):
                result = check(FakeFetch([artifact()], [run(event=event)], raw={COMMIT_PATH: other_commit}))
                self.assertNoReuse(result)

    def test_commit_lookup_failure_is_no_reuse(self):
        result = check(FakeFetch([artifact()], [run()], raw={COMMIT_PATH: github_api.ApiError("HTTP 404")}))
        self.assertNoReuse(result)

    def test_commit_without_a_tree_is_no_reuse(self):
        result = check(FakeFetch([artifact()], [run()], raw={COMMIT_PATH: {"sha": HEAD}}))
        self.assertNoReuse(result)

    def test_run_without_a_head_commit_is_no_reuse(self):
        item = run()
        del item["head_sha"]
        result = check(FakeFetch([artifact()], [item]))
        self.assertNoReuse(result)

    def test_pull_request_marker_can_vouch_for_a_different_tree_from_its_head(self):
        other_commit = {"sha": HEAD, "tree": {"sha": "d" * 40}}
        reuse, _, run_url = check(FakeFetch([artifact()], [run(event="pull_request")],
                                           raw={COMMIT_PATH: other_commit}))
        self.assertEqual((reuse, run_url), (True, RUN_URL % 101))

    def test_pull_request_target_run_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run(event="pull_request_target")])))

    def test_workflow_run_run_is_no_reuse(self):
        self.assertNoReuse(check(FakeFetch([artifact()], [run(event="workflow_run")])))

    def test_run_without_an_event_is_no_reuse(self):
        item = run()
        del item["event"]
        self.assertNoReuse(check(FakeFetch([artifact()], [item])))


class NewerFailureTests(unittest.TestCase):
    """A full run of the same tree that failed after the pass is newer evidence and stops reuse."""

    PASS = artifact(run_id=101, created="2026-01-19T10:00:00Z")

    def test_pass_then_fail_is_no_reuse(self):
        reuse, reason, run_url = check(FakeFetch([self.PASS], [run(101), run(201, "failure")],
                                                 failures=[failed(201, "2026-01-19T12:00:00Z")]))
        self.assertIs(reuse, False)
        self.assertEqual(run_url, "")
        self.assertIn(RUN_URL % 201, reason)

    def test_control_fail_then_pass_is_reused(self):
        reuse, _, run_url = check(FakeFetch([self.PASS], [run(101), run(201, "failure")],
                                            failures=[failed(201, "2026-01-19T08:00:00Z")]))
        self.assertIs(reuse, True)
        self.assertEqual(run_url, RUN_URL % 101)

    def test_failure_at_the_same_time_as_the_pass_is_no_reuse(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure")],
                                      failures=[failed(201, "2026-01-19T10:00:00Z")]))
        self.assertIs(reuse, False)

    def test_failure_whose_run_later_passed_on_a_rerun_still_counts(self):
        # Only the pass checks a conclusion. A newer failed full run counts whatever its run became.
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "success")],
                                      failures=[failed(201, "2026-01-19T12:00:00Z")]))
        self.assertIs(reuse, False)

    def test_failed_marker_from_a_fork_changes_nothing(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure", head_repo="someone/example-app")],
                                      failures=[failed(201)]))
        self.assertIs(reuse, True)

    def test_failed_marker_from_another_repository_changes_nothing(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure", repo="someone/example-app")],
                                      failures=[failed(201)]))
        self.assertIs(reuse, True)

    def test_failed_marker_from_another_workflow_changes_nothing(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure", path=".github/workflows/x.yml")],
                                      failures=[failed(201)]))
        self.assertIs(reuse, True)

    def test_failed_marker_from_a_pull_request_target_run_changes_nothing(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure", event="pull_request_target")],
                                      failures=[failed(201)]))
        self.assertIs(reuse, True)

    def test_failed_marker_of_a_run_still_going_is_no_reuse(self):
        # Only a verdict that did not pass uploads a failed marker, so the run is failing as it completes.
        reuse, reason, _ = check(FakeFetch([self.PASS], [run(101), run(201, None)], failures=[failed(201)]))
        self.assertIs(reuse, False)
        self.assertIn(RUN_URL % 201, reason)

    def test_expired_failed_marker_changes_nothing(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure")],
                                      failures=[failed(201, expired=True)]))
        self.assertIs(reuse, True)

    def test_failed_marker_without_a_creation_time_counts_as_newer(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure")],
                                      failures=[failed(201, created=None)]))
        self.assertIs(reuse, False)

    def test_newest_counting_failure_is_found_past_ones_that_do_not_count(self):
        failures = [failed(201, "2026-01-19T13:00:00Z"), failed(202, "2026-01-19T12:00:00Z")]
        runs = [run(101), run(201, "failure", head_repo="someone/example-app"), run(202, "failure")]
        reuse, reason, _ = check(FakeFetch([self.PASS], runs, failures=failures))
        self.assertIs(reuse, False)
        self.assertIn(RUN_URL % 202, reason)

    def test_failure_between_an_older_pass_and_an_unfinished_newer_one_is_no_reuse(self):
        older = artifact(run_id=101, created="2026-01-18T10:00:00Z")
        newer = artifact(run_id=102, created="2026-01-19T10:00:00Z")
        runs = [run(101), run(102, conclusion=None), run(201, "failure")]
        reuse, _, _ = check(FakeFetch([newer, older], runs, failures=[failed(201, "2026-01-18T12:00:00Z")]))
        self.assertIs(reuse, False)

    def test_failed_marker_lookup_error_is_no_reuse(self):
        reuse, reason, run_url = check(FakeFetch([self.PASS], [run(101)],
                                                 raw={FAILED_PATH: github_api.ApiError("HTTP 500")}))
        self.assertIs(reuse, False)
        self.assertIn("HTTP 500", reason)
        self.assertEqual(run_url, "")

    def test_failed_marker_answer_without_a_list_is_no_reuse(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101)], raw={FAILED_PATH: {"total_count": 1}}))
        self.assertIs(reuse, False)

    def test_failed_run_lookup_error_is_no_reuse(self):
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101)], failures=[failed(201)]))
        self.assertIs(reuse, False)

    def test_more_failed_markers_than_one_page_is_no_reuse(self):
        answer = {"total_count": 101, "artifacts": [failed(201, "2026-01-19T08:00:00Z")]}
        reuse, reason, _ = check(FakeFetch([self.PASS], [run(101), run(201, "failure")], raw={FAILED_PATH: answer}))
        self.assertIs(reuse, False)
        self.assertIn("more ci-failed-full-%s markers than one page holds" % TREE, reason)

    def test_failed_marker_answer_without_a_count_is_no_reuse(self):
        answer = {"artifacts": []}
        reuse, _, _ = check(FakeFetch([self.PASS], [run(101)], raw={FAILED_PATH: answer}))
        self.assertIs(reuse, False)

    def test_too_many_newer_failures_to_check_is_no_reuse(self):
        count = tested_tree.MAX_RUNS_CHECKED + 1
        failures = [failed(300 + i, "2026-01-19T12:%02d:00Z" % i) for i in range(count)]
        runs = [run(101)] + [run(300 + i, "failure", head_repo="someone/example-app") for i in range(count)]
        reuse, _, _ = check(FakeFetch([self.PASS], runs, failures=failures))
        self.assertIs(reuse, False)


class OutputLinesTests(unittest.TestCase):
    def test_outputs_are_single_lines(self):
        lines = tested_tree.output_lines(False, TREE, "bad\nreason", "")
        self.assertEqual(lines, ["reuse=false", "tree=" + TREE, "reason=bad reason", "run-url="])


class SharedImportTests(unittest.TestCase):
    def test_helper_comes_from_the_shared_folder_beside_the_action(self):
        self.assertEqual(os.path.dirname(os.path.abspath(github_api.__file__)),
                         os.path.join(ACTIONS, "_shared"))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class MakeFetchTests(unittest.TestCase):
    def opener(self, body=b"{}", error=None):
        seen = {}

        def open_url(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            if error is not None:
                raise error
            return FakeResponse(body)
        return open_url, seen

    def test_request_shape(self):
        open_url, seen = self.opener(b'{"ok": true}')
        fetch = github_api.make_fetch("secret-token", "https://api.example.test/", open_url)
        self.assertEqual(fetch("/repos/a/b"), {"ok": True})
        request = seen["request"]
        self.assertEqual(request.full_url, "https://api.example.test/repos/a/b")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.unredirected_hdrs.get("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(seen["timeout"], 20)

    def test_no_token_sends_no_authorization(self):
        open_url, seen = self.opener()
        github_api.make_fetch("", "https://api.example.test", open_url)("/x")
        self.assertIsNone(seen["request"].unredirected_hdrs.get("Authorization"))

    def test_http_error_raises_without_the_token(self):
        error = urllib.error.HTTPError("https://api.example.test/x", 403, "Forbidden", {}, None)
        open_url, _ = self.opener(error=error)
        with self.assertRaises(github_api.ApiError) as caught:
            github_api.make_fetch("secret-token", "https://api.example.test", open_url)("/x")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_network_error_raises(self):
        open_url, _ = self.opener(error=urllib.error.URLError("refused"))
        with self.assertRaises(github_api.ApiError):
            github_api.make_fetch("t", "https://api.example.test", open_url)("/x")

    def test_timeout_raises(self):
        open_url, _ = self.opener(error=TimeoutError("timed out"))
        with self.assertRaises(github_api.ApiError):
            github_api.make_fetch("t", "https://api.example.test", open_url)("/x")

    def test_body_that_is_not_json_raises(self):
        open_url, _ = self.opener(b"<html>")
        with self.assertRaises(github_api.ApiError):
            github_api.make_fetch("t", "https://api.example.test", open_url)("/x")

    def test_json_that_is_not_an_object_raises(self):
        open_url, _ = self.opener(b"[]")
        with self.assertRaises(github_api.ApiError):
            github_api.make_fetch("t", "https://api.example.test", open_url)("/x")


class ParseTimeTests(unittest.TestCase):
    CASES = [
        ("2026-01-19T10:00:00Z", datetime.datetime(2026, 1, 19, 10, 0, 0, tzinfo=datetime.timezone.utc)),
        ("2026-01-19T10:00:00.123Z", datetime.datetime(2026, 1, 19, 10, 0, 0, tzinfo=datetime.timezone.utc)),
        ("2026-01-19T10:00:00+00:00", None),
        ("2026-01-19", None),
        ("", None),
        (None, None),
        (5, None),
        ("2026-13-19T10:00:00Z", None),
    ]

    def test_table(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(github_api.parse_time(text), expected)


class ScriptProcessTests(unittest.TestCase):
    def test_unreachable_api_gives_no_reuse_and_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            env = dict(os.environ)
            env.update({
                "GITHUB_API_URL": "http://127.0.0.1:9",
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_OUTPUT": output,
                "CI_API_TOKEN": "secret-token",
                "CI_TESTED_TREE_WORKFLOW": WORKFLOW,
            })
            proc = subprocess.run([sys.executable, SCRIPT], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            with open(output, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            self.assertIn("reuse=false", lines)
            tree = [line for line in lines if line.startswith("tree=")]
            self.assertEqual(len(tree), 1)
            self.assertRegex(tree[0], r"^tree=[0-9a-f]{40}$")
            self.assertNotIn(b"secret-token", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
