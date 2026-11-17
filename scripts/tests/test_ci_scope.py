"""Tests for .github/actions/ci-scope/scope.py.

Run from the repository root:
    python3 -m unittest discover -s scripts/tests -p 'test_ci_*.py' -v

Every refusal or full-run case sits beside a control that differs only in that one thing. Only the
end to end test builds a git repository and serves the API locally. The rest pass plain values, a fake git
and a fake API.
"""
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "actions", "ci-scope", "scope.py")

_spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope)

BASE = "a" * 40
HEAD = "b" * 40
OTHER = "d" * 40
ZEROS = "0" * 40
REPO = "example-org/example-app"
RUN_ID = "900"
WORKFLOW = scope.Workflow(REPO, ".github/workflows/ci.yml", "main", RUN_ID)
RUNS_PATH = "/repos/%s/actions/workflows/ci.yml/runs?branch=main&status=completed&per_page=20" % REPO

RULES_TEXT = """\
# comment
unit: src/core/** tests/core/**
api: src/api/** tests/api/**
full: .github/** *.props global.json
ignore: docs/** **/*.md
"""


class FakeGit(object):
    """Stands in for git. Knows which commits the clone holds and which a fetch can bring in."""

    def __init__(self, present=(), fetchable=(), diff=b"", diff_code=0, ancestors=()):
        self.present = set(present)
        self.fetchable = set(fetchable)
        # Commits that are ancestors of HEAD, as `merge-base --is-ancestor` answers.
        self.ancestors = set(ancestors)
        self.diff = diff
        self.diff_code = diff_code
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "cat-file":
            sha = args[2].split("^", 1)[0]
            return (0 if sha in self.present else 1), b""
        if args[0] == "fetch":
            if args[-1] in self.fetchable:
                self.present.add(args[-1])
                return 0, b""
            return 128, b""
        if args[0] == "diff":
            return self.diff_code, self.diff
        if args[:2] == ["merge-base", "--is-ancestor"] and args[3] == HEAD:
            return (0 if args[2] in self.ancestors else 1), b""
        raise AssertionError("unexpected git call: %r" % (args,))


class FakeFetch(object):
    """Stands in for the API. Answers the list of completed runs, or raises the given error."""

    def __init__(self, runs=(), path=RUNS_PATH, answer=None, error=None):
        self.path = path
        self.answer = answer if answer is not None else {"total_count": len(runs), "workflow_runs": list(runs)}
        self.error = error
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if self.error is not None:
            raise self.error
        if path != self.path:
            raise scope.github_api.ApiError("HTTP 404 for " + path)
        return self.answer


def passing_run(run_id=1, sha=BASE, created="2026-01-19T10:00:00Z", conclusion="success", event="push",
                path=".github/workflows/ci.yml", repo=REPO, head_repo=REPO, updated=None):
    """A run as the list-runs-for-a-workflow endpoint returns it. updated_at is when it completed, the same
    as created_at unless given."""
    return {"id": run_id, "head_sha": sha, "created_at": created, "updated_at": updated or created,
            "status": "completed",
            "conclusion": conclusion, "event": event, "path": path,
            "html_url": "https://github.com/%s/actions/runs/%s" % (repo, run_id),
            "repository": {"full_name": repo}, "head_repository": {"full_name": head_repo}}


def failed_full_run(run_id=2, sha=BASE, created="2026-01-19T11:00:00Z", conclusion="failure", event="schedule",
                    **fields):
    """A run that was full by its event and did not pass."""
    return passing_run(run_id, sha, created, conclusion, event, **fields)


def names(*paths):
    return b"".join(p.encode("utf-8") + b"\0" for p in paths)


class MatchesTests(unittest.TestCase):
    CASES = [
        # (glob, path, expected)
        ("src/core/**", "src/core/a.py", True),
        ("src/core/**", "src/core/x/y/z.py", True),
        ("src/core/**", "src/corex/a.py", False),
        ("src/core/**", "src/a.py", False),
        ("src/core/**", "lib/src/core/a.py", False),
        ("docs/**", "docs/a.md", True),
        ("docs/**", "src/docs/a.md", False),
        ("**/*.md", "README.md", True),
        ("**/*.md", "docs/a/b.md", True),
        ("**/*.md", "README.mdx", False),
        ("src/**/test_*.py", "src/test_a.py", True),
        ("src/**/test_*.py", "src/a/b/test_x.py", True),
        ("src/**/test_*.py", "src/a/b/x_test.py", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/x/a.py", False),
        ("*.props", "Directory.Build.props", True),
        ("*.props", "a/b/Directory.Build.props", True),
        ("*.props", "a/b/Directory.Build.props.bak", False),
        ("global.json", "global.json", True),
        ("global.json", "sub/global.json", True),
        ("global.json", "global.jsonc", False),
        ("a?c.txt", "abc.txt", True),
        ("a?c.txt", "ac.txt", False),
        ("a?c.txt", "x/a/c.txt", False),
        (".github/**", ".github/workflows/check.yml", True),
        (".github/**", "x/.github/workflows/check.yml", False),
        ("a[1].txt", "a[1].txt", True),
        ("a[1].txt", "a1.txt", False),
        ("odd name.txt", "dir/odd name.txt", True),
        ("odd name.txt", "dir/odd_name.txt", False),
    ]

    def test_table(self):
        for glob, path, expected in self.CASES:
            with self.subTest(glob=glob, path=path):
                self.assertIs(scope.matches(glob, path), expected)


class ParseRulesTests(unittest.TestCase):
    def test_good_file(self):
        rules = scope.parse_rules(RULES_TEXT)
        self.assertIsNone(rules.error)
        self.assertEqual(rules.segments, {
            "unit": ["src/core/**", "tests/core/**"],
            "api": ["src/api/**", "tests/api/**"],
        })
        self.assertEqual(rules.full, [".github/**", "*.props", "global.json"])
        self.assertEqual(rules.ignore, ["docs/**", "**/*.md"])

    def test_comments_blank_lines_and_crlf_are_skipped(self):
        rules = scope.parse_rules("\r\n  # indented comment\r\n\r\nunit: src/**\r\n")
        self.assertIsNone(rules.error)
        self.assertEqual(rules.segments, {"unit": ["src/**"]})

    def test_repeated_name_adds_globs(self):
        rules = scope.parse_rules("unit: a/**\nunit: b/**\nfull: x\nfull: y\n")
        self.assertEqual(rules.segments, {"unit": ["a/**", "b/**"]})
        self.assertEqual(rules.full, ["x", "y"])

    def test_line_without_colon_is_an_error(self):
        rules = scope.parse_rules("# c\nunit: src/**\napi src/api/**\n")
        self.assertEqual(rules.error, "Full run: rules file line 3 is not NAME: GLOBS")
        self.assertEqual(rules.segments, {"unit": ["src/**"]})

    def test_control_same_line_with_colon(self):
        rules = scope.parse_rules("# c\nunit: src/**\napi: src/api/**\n")
        self.assertIsNone(rules.error)
        self.assertEqual(sorted(rules.segments), ["api", "unit"])

    def test_line_with_no_globs_is_an_error(self):
        rules = scope.parse_rules("unit:\n")
        self.assertEqual(rules.error, "Full run: rules file line 1 is not NAME: GLOBS")

    def test_name_with_a_space_is_an_error(self):
        rules = scope.parse_rules("unit tests: src/**\n")
        self.assertEqual(rules.error, "Full run: rules file line 1 is not NAME: GLOBS")

    def test_glob_with_a_leading_slash_is_an_error(self):
        rules = scope.parse_rules("w: .github/workflows/**\nfull: /.github/**\n")
        self.assertEqual(rules.error, "Full run: rules file line 2 has a glob that starts or ends with / or holds //")

    def test_glob_with_a_trailing_slash_or_double_slash_is_an_error(self):
        for glob in ("src/", "src//core/**", "/"):
            with self.subTest(glob=glob):
                self.assertIsNotNone(scope.parse_rules("unit: %s\n" % glob).error)

    def test_control_glob_without_a_stray_slash(self):
        self.assertIsNone(scope.parse_rules("w: .github/workflows/**\nfull: .github/**\n").error)

    def test_reserved_name_in_another_case_is_an_error(self):
        for name in ("Full", "FULL", "Ignore", "iGnOrE"):
            with self.subTest(name=name):
                rules = scope.parse_rules("unit: src/**\n%s: .github/**\n" % name)
                self.assertEqual(rules.error, "Full run: rules file line 2 names a segment %s, which is reserved"
                                 % name)
                self.assertEqual(rules.segments, {"unit": ["src/**"]})

    def test_control_reserved_names_in_lower_case(self):
        rules = scope.parse_rules("full: .github/**\nignore: docs/**\n")
        self.assertIsNone(rules.error)
        self.assertEqual((rules.full, rules.ignore), ([".github/**"], ["docs/**"]))

    def test_first_bad_line_is_reported_and_later_segments_still_listed(self):
        rules = scope.parse_rules("bad\nworse\napi: src/api/**\n")
        self.assertEqual(rules.error, "Full run: rules file line 1 is not NAME: GLOBS")
        self.assertEqual(rules.segments, {"api": ["src/api/**"]})


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.rules = scope.parse_rules(RULES_TEXT)

    def test_full_glob_change_is_a_full_run(self):
        full, segments, reason = scope.select(self.rules, ["src/core/a.py", "app/Directory.Build.props"])
        self.assertTrue(full)
        self.assertEqual(segments, ["api", "unit"])
        self.assertEqual(reason, "Full run: app/Directory.Build.props changes shared files")

    def test_control_without_the_full_glob_change(self):
        full, segments, reason = scope.select(self.rules, ["src/core/a.py", "app/Directory.Build.txt"])
        self.assertTrue(full)
        self.assertEqual(reason, "Full run: app/Directory.Build.txt matches no rule")
        full, segments, reason = scope.select(self.rules, ["src/core/a.py"])
        self.assertFalse(full)

    def test_full_glob_wins_over_ignore(self):
        full, _, reason = scope.select(self.rules, [".github/notes.md"])
        self.assertTrue(full)
        self.assertEqual(reason, "Full run: .github/notes.md changes shared files")

    def test_file_matching_no_rule_is_a_full_run(self):
        full, segments, reason = scope.select(self.rules, ["src/core/a.py", "tools/new.py"])
        self.assertTrue(full)
        self.assertEqual(segments, ["api", "unit"])
        self.assertEqual(reason, "Full run: tools/new.py matches no rule")

    def test_control_same_file_covered_by_ignore(self):
        rules = scope.parse_rules(RULES_TEXT + "ignore: tools/**\n")
        full, segments, reason = scope.select(rules, ["src/core/a.py", "tools/new.py"])
        self.assertFalse(full)
        self.assertEqual(segments, ["unit"])
        self.assertEqual(reason, "2 files changed. Segments: unit")

    def test_segments_are_sorted_and_named(self):
        full, segments, reason = scope.select(self.rules, ["tests/api/t.py", "src/core/a.py", "docs/a.md"])
        self.assertFalse(full)
        self.assertEqual(segments, ["api", "unit"])
        self.assertEqual(reason, "3 files changed. Segments: api, unit")

    def test_only_ignored_files_affect_no_segment(self):
        full, segments, reason = scope.select(self.rules, ["docs/a.md", "README.md"])
        self.assertFalse(full)
        self.assertEqual(segments, [])
        self.assertEqual(reason, "2 files changed. No segment affected")

    def test_one_file_reads_as_singular(self):
        _, _, reason = scope.select(self.rules, ["src/api/a.py"])
        self.assertEqual(reason, "1 file changed. Segments: api")

    def test_ignored_file_inside_a_segment_still_selects_it(self):
        full, segments, _ = scope.select(self.rules, ["src/core/README.md"])
        self.assertFalse(full)
        self.assertEqual(segments, ["unit"])

    def test_no_changed_files_affect_no_segment(self):
        self.assertEqual(scope.select(self.rules, []), (False, [], "0 files changed. No segment affected"))

    def test_since_names_the_base(self):
        self.assertEqual(scope.select(self.rules, ["src/api/a.py"], " since X"),
                         (False, ["api"], "1 file changed since X. Segments: api"))
        self.assertEqual(scope.select(self.rules, ["docs/a.md"], " since X"),
                         (False, [], "1 file changed since X. No segment affected"))


class FullEventTests(unittest.TestCase):
    def test_tag_is_a_full_run(self):
        self.assertEqual(scope.full_event_reason("push", "tag"), "Full run: tag")

    def test_schedule_is_a_full_run(self):
        self.assertEqual(scope.full_event_reason("schedule", "branch"), "Full run: schedule")

    def test_dispatch_is_a_full_run(self):
        self.assertEqual(scope.full_event_reason("workflow_dispatch", "branch"), "Full run: workflow_dispatch")

    def test_control_branch_push_and_pull_request_compare(self):
        self.assertIsNone(scope.full_event_reason("push", "branch"))
        self.assertIsNone(scope.full_event_reason("pull_request", "branch"))


class PullRequestBaseTests(unittest.TestCase):
    def test_base_sha(self):
        self.assertEqual(scope.pull_request_base({"pull_request": {"base": {"sha": BASE}}}), BASE)

    def test_missing_base(self):
        self.assertIsNone(scope.pull_request_base({}))

    def test_base_that_is_not_a_sha(self):
        self.assertIsNone(scope.pull_request_base({"pull_request": {"base": {"sha": "--upload-pack=x"}}}))

    def test_all_zeros(self):
        self.assertIsNone(scope.pull_request_base({"pull_request": {"base": {"sha": ZEROS}}}))


class WorkflowFromEnvTests(unittest.TestCase):
    def test_reads_the_workflow_path_branch_and_run(self):
        env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF_NAME": "main", "GITHUB_RUN_ID": RUN_ID,
               "GITHUB_WORKFLOW_REF": REPO + "/.github/workflows/ci.yml@refs/heads/main"}
        self.assertEqual(scope.workflow_from_env(env), WORKFLOW)

    def test_ref_of_another_repository_gives_no_path(self):
        env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF_NAME": "main", "GITHUB_RUN_ID": RUN_ID,
               "GITHUB_WORKFLOW_REF": "someone/else/.github/workflows/ci.yml@refs/heads/main"}
        self.assertEqual(scope.workflow_from_env(env).path, "")

    def test_empty_environment(self):
        self.assertEqual(scope.workflow_from_env({}), scope.Workflow("", "", "", ""))


class DecideTests(unittest.TestCase):
    """The table rows that need the event, the rules file, the API and git together."""

    def push(self, rules_text=RULES_TEXT, event_name="push", ref_type="branch", git=None, fetch=None,
             workflow=WORKFLOW, rules_path=".github/ci-scope.txt", event=None):
        git = git if git is not None else FakeGit(present=[BASE], ancestors=[BASE], diff=names("src/core/a.py"))
        fetch = fetch if fetch is not None else FakeFetch([passing_run()])
        event = event if event is not None else {"before": OTHER}
        return scope.decide(event_name, event, ref_type, rules_text, HEAD, git, fetch, workflow,
                            rules_path), git

    def test_control_push_selects_segments_since_the_last_passing_run(self):
        result, git = self.push()
        self.assertEqual(result, scope.Scope(
            False, ["unit"], "1 file changed since the last passing run (aaaaaaa). Segments: unit", 1))
        self.assertEqual(git.calls[-1], ["diff", "--name-only", "-z", "--no-renames", BASE + ".." + HEAD])

    def test_push_reads_one_page_of_completed_runs_of_this_workflow_on_the_branch(self):
        fetch = FakeFetch([passing_run()])
        self.push(fetch=fetch)
        self.assertEqual(fetch.calls, [RUNS_PATH])

    def test_branch_name_is_quoted_in_the_api_path(self):
        workflow = WORKFLOW._replace(branch="feature/x y")
        fetch = FakeFetch([passing_run()], path=RUNS_PATH.replace("branch=main", "branch=feature%2Fx%20y"))
        result, _ = self.push(fetch=fetch, workflow=workflow)
        self.assertFalse(result.full)

    def test_diff_is_taken_from_the_base_and_not_from_before(self):
        git = FakeGit(present=[BASE, OTHER], ancestors=[BASE, OTHER], diff=names("src/core/a.py"))
        self.push(git=git, event={"before": OTHER})
        diffs = [c for c in git.calls if c[0] == "diff"]
        self.assertEqual(diffs, [["diff", "--name-only", "-z", "--no-renames", BASE + ".." + HEAD]])

    def test_newest_passing_run_is_the_base(self):
        runs = [passing_run(1, BASE, "2026-01-18T10:00:00Z"), passing_run(2, OTHER, "2026-01-19T10:00:00Z")]
        git = FakeGit(present=[BASE, OTHER], ancestors=[BASE, OTHER], diff=names("src/core/a.py"))
        result, _ = self.push(git=git, fetch=FakeFetch(runs))
        self.assertEqual(git.calls[-1][-1], OTHER + ".." + HEAD)
        self.assertIn("(%s)" % OTHER[:7], result.reason)

    def test_newer_run_that_is_not_an_ancestor_is_passed_over(self):
        runs = [passing_run(2, OTHER, "2026-01-19T10:00:00Z"), passing_run(1, BASE, "2026-01-18T10:00:00Z")]
        git = FakeGit(present=[BASE, OTHER], ancestors=[BASE], diff=names("src/core/a.py"))
        self.push(git=git, fetch=FakeFetch(runs))
        self.assertEqual(git.calls[-1][-1], BASE + ".." + HEAD)

    def test_run_whose_commit_is_not_in_the_clone_is_passed_over(self):
        runs = [passing_run(2, OTHER, "2026-01-19T10:00:00Z"), passing_run(1, BASE, "2026-01-18T10:00:00Z")]
        git = FakeGit(present=[BASE], ancestors=[BASE, OTHER], diff=names("src/core/a.py"))
        self.push(git=git, fetch=FakeFetch(runs))
        self.assertEqual(git.calls[-1][-1], BASE + ".." + HEAD)
        self.assertFalse([c for c in git.calls if c[0] == "fetch"])

    def test_runs_that_cannot_be_a_base_are_passed_over(self):
        refused = [
            passing_run(int(RUN_ID), OTHER),
            passing_run(3, OTHER, head_repo="someone/example-app"),
            passing_run(4, OTHER, repo="someone/example-app"),
            passing_run(5, OTHER, path=".github/workflows/nightly.yml"),
            passing_run(6, OTHER, event="pull_request"),
            passing_run(7, OTHER, event="pull_request_target"),
            passing_run(8, OTHER, event="workflow_run"),
            passing_run(9, OTHER, conclusion="failure"),
            passing_run(10, "not-a-sha"),
        ]
        for item in refused:
            with self.subTest(run=item["id"], event=item["event"]):
                git = FakeGit(present=[BASE, OTHER], ancestors=[BASE, OTHER], diff=names("src/core/a.py"))
                older = passing_run(1, BASE, "2026-01-18T10:00:00Z")
                self.push(git=git, fetch=FakeFetch([item, older]))
                self.assertEqual(git.calls[-1][-1], BASE + ".." + HEAD)

    def test_control_schedule_and_dispatch_runs_can_be_a_base(self):
        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                git = FakeGit(present=[BASE, OTHER], ancestors=[BASE, OTHER], diff=names("src/core/a.py"))
                runs = [passing_run(2, OTHER, event=event), passing_run(1, BASE, "2026-01-18T10:00:00Z")]
                self.push(git=git, fetch=FakeFetch(runs))
                self.assertEqual(git.calls[-1][-1], OTHER + ".." + HEAD)

    # A run full by its event that failed has no change in the next diff to explain it, so it keeps the
    # next push full until a run passes.
    FAILED_FULL_REASON = "Full run: the last full run on main failed (https://github.com/%s/actions/runs/2)" % REPO

    def test_failed_schedule_run_newer_than_the_base_is_a_full_run(self):
        runs = [failed_full_run(), passing_run(1, BASE, "2026-01-19T10:00:00Z")]
        result, git = self.push(fetch=FakeFetch(runs))
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], self.FAILED_FULL_REASON, None))
        self.assertFalse([c for c in git.calls if c[0] == "diff"])

    def test_control_passing_schedule_run_newer_than_the_base_selects_the_segment(self):
        runs = [failed_full_run(conclusion="success"), passing_run(1, BASE, "2026-01-19T10:00:00Z")]
        result, _ = self.push(fetch=FakeFetch(runs))
        self.assertEqual(result, scope.Scope(
            False, ["unit"], "1 file changed since the last passing run (aaaaaaa). Segments: unit", 1))

    def test_failed_or_timed_out_dispatch_and_schedule_runs_are_a_full_run(self):
        for event in ("schedule", "workflow_dispatch"):
            for conclusion in ("failure", "timed_out"):
                with self.subTest(event=event, conclusion=conclusion):
                    runs = [failed_full_run(event=event, conclusion=conclusion),
                            passing_run(1, BASE, "2026-01-19T10:00:00Z")]
                    result, _ = self.push(fetch=FakeFetch(runs))
                    self.assertEqual(result, scope.Scope(True, ["api", "unit"], self.FAILED_FULL_REASON, None))

    def test_failed_push_run_newer_than_the_base_does_not_force_a_full_run(self):
        runs = [failed_full_run(event="push"), passing_run(1, BASE, "2026-01-19T10:00:00Z")]
        result, git = self.push(fetch=FakeFetch(runs))
        self.assertFalse(result.full)
        self.assertEqual(git.calls[-1][-1], BASE + ".." + HEAD)

    def test_failed_full_run_older_than_the_base_does_not_force_a_full_run(self):
        runs = [passing_run(1, BASE, "2026-01-19T12:00:00Z"), failed_full_run()]
        result, _ = self.push(fetch=FakeFetch(runs))
        self.assertEqual(result.segments, ["unit"])
        self.assertFalse(result.full)

    def test_failed_full_run_that_finished_after_the_base_started_is_a_full_run(self):
        # The base run started before the failure was known, so its scope could not have made it full.
        runs = [passing_run(1, BASE, "2026-01-19T12:00:00Z", updated="2026-01-19T12:20:00Z"),
                failed_full_run(created="2026-01-19T11:00:00Z", updated="2026-01-19T12:10:00Z")]
        result, _ = self.push(fetch=FakeFetch(runs))
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], self.FAILED_FULL_REASON, None))

    def test_control_failed_full_run_that_finished_before_the_base_started_does_not_force_a_full_run(self):
        runs = [passing_run(1, BASE, "2026-01-19T12:00:00Z", updated="2026-01-19T12:20:00Z"),
                failed_full_run(created="2026-01-19T11:00:00Z", updated="2026-01-19T11:50:00Z")]
        result, _ = self.push(fetch=FakeFetch(runs))
        self.assertFalse(result.full)
        self.assertEqual(result.segments, ["unit"])

    def test_base_with_no_readable_time_lets_any_failed_full_run_count(self):
        runs = [passing_run(1, BASE, None, updated="2026-01-19T12:20:00Z"),
                failed_full_run(created="2026-01-19T09:00:00Z", updated="2026-01-19T09:30:00Z")]
        result, _ = self.push(fetch=FakeFetch(runs))
        self.assertEqual(result.reason, self.FAILED_FULL_REASON)

    def test_failed_full_run_listed_out_of_order_is_still_newer_than_the_base(self):
        runs = [passing_run(1, BASE, "2026-01-19T10:00:00Z"), failed_full_run()]
        result, _ = self.push(fetch=FakeFetch(runs))
        self.assertEqual(result.reason, self.FAILED_FULL_REASON)

    def test_failed_full_run_with_no_readable_time_counts_as_newest(self):
        runs = [passing_run(1, BASE, "2026-01-19T12:00:00Z"), failed_full_run(created=None)]
        result, _ = self.push(fetch=FakeFetch(runs))
        self.assertEqual(result.reason, self.FAILED_FULL_REASON)

    def test_newer_passing_run_that_is_not_an_ancestor_does_not_hide_a_failed_full_run(self):
        runs = [passing_run(3, OTHER, "2026-01-19T12:00:00Z"), failed_full_run(),
                passing_run(1, BASE, "2026-01-19T10:00:00Z")]
        git = FakeGit(present=[BASE, OTHER], ancestors=[BASE], diff=names("src/core/a.py"))
        result, _ = self.push(git=git, fetch=FakeFetch(runs))
        self.assertEqual(result.reason, self.FAILED_FULL_REASON)

    def test_failed_full_runs_that_do_not_count_change_nothing(self):
        refused = [
            failed_full_run(head_repo="someone/example-app"),
            failed_full_run(repo="someone/example-app"),
            failed_full_run(path=".github/workflows/nightly.yml"),
            failed_full_run(event="workflow_run"),
            failed_full_run(event="pull_request"),
            failed_full_run(conclusion=None),
            failed_full_run(conclusion="cancelled"),
            failed_full_run(conclusion="skipped"),
        ]
        for item in refused:
            with self.subTest(run=item):
                runs = [item, passing_run(1, BASE, "2026-01-19T10:00:00Z")]
                result, _ = self.push(fetch=FakeFetch(runs))
                self.assertEqual(result.segments, ["unit"])
                self.assertFalse(result.full)

    def test_failed_full_run_without_a_link_names_its_id(self):
        failed = failed_full_run()
        del failed["html_url"]
        result, _ = self.push(fetch=FakeFetch([failed, passing_run(1, BASE, "2026-01-19T10:00:00Z")]))
        self.assertEqual(result.reason, "Full run: the last full run on main failed (run 2)")

    def test_changed_workflow_file_is_a_full_run(self):
        rules = "unit: src/core/**\nworkflows: .github/workflows/**\n"
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=names("src/core/a.py", ".github/workflows/ci.yml"))
        result, _ = self.push(rules_text=rules, git=git)
        self.assertEqual(result, scope.Scope(
            True, ["unit", "workflows"], "Full run: .github/workflows/ci.yml changes the workflow", 2))

    def test_control_another_workflow_file_changed_is_partial(self):
        rules = "unit: src/core/**\nworkflows: .github/workflows/**\n"
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=names("src/core/a.py", ".github/workflows/other.yml"))
        result, _ = self.push(rules_text=rules, git=git)
        self.assertEqual(result.segments, ["unit", "workflows"])
        self.assertFalse(result.full)

    def test_changed_workflow_file_on_a_pull_request_is_a_full_run(self):
        rules = "unit: src/core/**\nworkflows: .github/workflows/**\n"
        git = FakeGit(present=[BASE], diff=names(".github/workflows/ci.yml"))
        result = scope.decide("pull_request", {"pull_request": {"base": {"sha": BASE}}}, "branch", rules, HEAD,
                              git, FakeFetch(), WORKFLOW, ".github/ci-scope.txt")
        self.assertEqual(result.reason, "Full run: .github/workflows/ci.yml changes the workflow")

    def test_no_passing_run_is_a_full_run(self):
        result, git = self.push(fetch=FakeFetch([]))
        self.assertEqual(result, scope.Scope(
            True, ["api", "unit"], "Full run: no passing run of this workflow on main to compare with", None))
        self.assertFalse([c for c in git.calls if c[0] == "diff"])

    def test_only_runs_that_do_not_count_is_a_full_run(self):
        result, _ = self.push(fetch=FakeFetch([passing_run(3, BASE, head_repo="someone/example-app")]))
        self.assertTrue(result.full)
        self.assertEqual(result.reason, "Full run: no passing run of this workflow on main to compare with")

    def test_api_error_is_a_full_run(self):
        result, git = self.push(fetch=FakeFetch(error=scope.github_api.ApiError("HTTP 403 for /x")))
        self.assertEqual(result, scope.Scope(
            True, ["api", "unit"], "Full run: the last passing run could not be read (HTTP 403 for /x)", None))
        self.assertFalse([c for c in git.calls if c[0] == "diff"])

    def test_answer_without_a_run_list_is_a_full_run(self):
        result, _ = self.push(fetch=FakeFetch(answer={"total_count": 1}))
        self.assertTrue(result.full)
        self.assertTrue(result.reason.startswith("Full run: the last passing run could not be read"))

    def test_unknown_workflow_is_a_full_run_without_an_api_call(self):
        fetch = FakeFetch([passing_run()])
        result, _ = self.push(fetch=fetch, workflow=scope.Workflow(REPO, "", "main", RUN_ID))
        self.assertTrue(result.full)
        self.assertEqual(fetch.calls, [])

    def test_schedule_is_a_full_run_listing_every_segment(self):
        result, git = self.push(event_name="schedule")
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: schedule", None))
        self.assertEqual(git.calls, [])

    def test_tag_is_a_full_run_without_an_api_call(self):
        fetch = FakeFetch([passing_run()])
        result, _ = self.push(ref_type="tag", fetch=fetch)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: tag", None))
        self.assertEqual(fetch.calls, [])

    def test_missing_rules_file_is_a_full_run_with_no_known_segments(self):
        result, _ = self.push(rules_text=None)
        self.assertEqual(result, scope.Scope(True, [], "Full run: no rules file", None))

    def test_missing_rules_file_on_a_dispatch_is_a_full_run_with_no_known_segments(self):
        result, _ = self.push(rules_text=None, event_name="workflow_dispatch")
        self.assertEqual(result, scope.Scope(True, [], "Full run: workflow_dispatch", None))

    def test_empty_rules_file_is_a_full_run_with_no_known_segments(self):
        result, _ = self.push(rules_text="# nothing yet\n")
        self.assertEqual(result, scope.Scope(True, [], "Full run: src/core/a.py matches no rule", 1))

    def test_bad_rules_line_is_a_full_run(self):
        result, _ = self.push(rules_text=RULES_TEXT + "broken\n")
        self.assertEqual(
            result, scope.Scope(True, ["api", "unit"], "Full run: rules file line 6 is not NAME: GLOBS", None))

    def test_changed_rules_file_is_a_full_run(self):
        rules = RULES_TEXT + "ignore: **/*.txt\n"
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=names("src/core/a.py", "rules/scope.txt"))
        result, _ = self.push(rules_text=rules, git=git, rules_path="rules/scope.txt")
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: rules/scope.txt changes the rules", 2))

    def test_changed_rules_file_given_with_a_leading_dot_slash_is_a_full_run(self):
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=names("src/core/a.py", "rules/scope.txt"))
        result, _ = self.push(rules_text=RULES_TEXT + "ignore: **/*.txt\n", git=git,
                              rules_path="./rules/scope.txt")
        self.assertTrue(result.full)

    def test_control_rules_file_unchanged_is_partial(self):
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=names("src/core/a.py", "rules/other.txt"))
        result, _ = self.push(rules_text=RULES_TEXT + "ignore: **/*.txt\n", git=git, rules_path="rules/scope.txt")
        self.assertFalse(result.full)

    def test_failed_diff_is_a_full_run(self):
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=b"", diff_code=128)
        result, _ = self.push(git=git)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: no base commit to compare", None))

    def test_full_glob_change_counts_files(self):
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=names("global.json", "src/core/a.py"))
        result, _ = self.push(git=git)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: global.json changes shared files", 2))

    def test_odd_file_names_survive(self):
        git = FakeGit(present=[BASE], ancestors=[BASE], diff=names("src/core/odd name\n.py", "docs/x.md"))
        result, _ = self.push(git=git)
        self.assertEqual(result.segments, ["unit"])
        self.assertEqual(result.changed, 2)

    def pull_request(self, git, event=None):
        event = event if event is not None else {"pull_request": {"base": {"sha": BASE}}}
        fetch = FakeFetch([passing_run()])
        result = scope.decide("pull_request", event, "branch", RULES_TEXT, HEAD, git, fetch, WORKFLOW,
                              ".github/ci-scope.txt")
        self.assertEqual(fetch.calls, [])
        return result

    def test_pull_request_diffs_three_dot_without_the_api(self):
        git = FakeGit(present=[BASE], diff=names("src/api/a.py"))
        result = self.pull_request(git)
        self.assertEqual(result, scope.Scope(False, ["api"], "1 file changed. Segments: api", 1))
        self.assertEqual(git.calls[-1], ["diff", "--name-only", "-z", "--no-renames", BASE + "..." + HEAD])

    def test_pull_request_base_missing_and_unfetchable_is_a_full_run(self):
        git = FakeGit(present=[], diff=names("src/core/a.py"))
        result = self.pull_request(git)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: no base commit to compare", None))
        fetches = [c for c in git.calls if c[0] == "fetch"]
        self.assertEqual(fetches, [["fetch", "--no-tags", "--depth=1", "origin", BASE]])
        self.assertFalse([c for c in git.calls if c[0] == "diff"])

    def test_control_pull_request_base_missing_but_fetchable(self):
        git = FakeGit(present=[], fetchable=[BASE], diff=names("src/core/a.py"))
        self.assertEqual(self.pull_request(git),
                         scope.Scope(False, ["unit"], "1 file changed. Segments: unit", 1))

    def test_pull_request_base_present_is_not_fetched(self):
        git = FakeGit(present=[BASE], diff=names("src/core/a.py"))
        self.pull_request(git)
        self.assertFalse([c for c in git.calls if c[0] == "fetch"])

    def test_pull_request_without_a_base_is_a_full_run(self):
        git = FakeGit(present=[BASE], diff=names("src/core/a.py"))
        result = self.pull_request(git, event={})
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: no base commit to compare", None))

    def test_other_event_has_no_base(self):
        git = FakeGit(present=[BASE], diff=names("src/core/a.py"))
        result = scope.decide("merge_group", {"before": BASE}, "branch", RULES_TEXT, HEAD, git,
                              FakeFetch([passing_run()]), WORKFLOW, ".github/ci-scope.txt")
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: no base commit to compare", None))


class SharedImportTests(unittest.TestCase):
    def test_helper_comes_from_the_shared_folder_beside_the_action(self):
        self.assertEqual(os.path.dirname(os.path.abspath(scope.github_api.__file__)),
                         os.path.join(ROOT, ".github", "actions", "_shared"))


class OutputLineTests(unittest.TestCase):
    def test_outputs_are_single_lines(self):
        lines = scope.output_lines(scope.Scope(True, ["api", "unit"], "Full run: a\nb matches no rule", None))
        self.assertEqual(lines, [
            "full=true",
            'segments=["api","unit"]',
            "reason=Full run: a b matches no rule",
            "changed=",
        ])

    def test_control_partial_run(self):
        lines = scope.output_lines(scope.Scope(False, [], "0 files changed. No segment affected", 0))
        self.assertEqual(lines, [
            "full=false",
            "segments=[]",
            "reason=0 files changed. No segment affected",
            "changed=0",
        ])


class RunsServer(object):
    """A local HTTP server that answers the list of completed runs, so the script's real API path runs."""

    def __init__(self, answers):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                answer = answers.get(self.path)
                body = json.dumps(answer if answer is not None else {"message": "Not Found"}).encode()
                self.send_response(200 if answer is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        return False


class MainEndToEndTest(unittest.TestCase):
    """One real run of the script against a temporary git repository and a local API."""

    def git(self, *args):
        return subprocess.run(
            ["git"] + list(args), cwd=self.repo, env=self.env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode("utf-8").strip()

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_push_selects_what_changed_since_the_last_passing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo = os.path.join(tmp, "example-app")
            os.makedirs(self.repo)
            self.env = dict(os.environ)
            self.env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
            self.git("init", "-q")
            self.git("config", "user.name", "Test")
            self.git("config", "user.email", "test@example.com")
            self.git("config", "commit.gpgsign", "false")
            self.write(".github/ci-scope.txt", RULES_TEXT)
            self.write("src/core/a.py", "one\n")
            self.write("src/api/b.py", "one\n")
            passed = self.commit("first, passed")
            # The run of this commit failed in api, so the next push must still test api.
            self.write("src/api/b.py", "two\n")
            failed = self.commit("second, failed")
            self.write("src/core/a.py", "two\n")
            self.write("docs/odd name.md", "notes\n")
            after = self.commit("third")
            # A passing run on another line of history is never a base.
            self.git("checkout", "-q", "-b", "elsewhere", passed)
            self.write("src/elsewhere.py", "x\n")
            unrelated = self.commit("unrelated")
            self.git("checkout", "-q", after)

            runs = {"workflow_runs": [
                passing_run(3, unrelated, "2026-01-19T12:00:00Z"),
                passing_run(2, failed, "2026-01-19T11:00:00Z", conclusion="failure"),
                passing_run(1, passed, "2026-01-19T10:00:00Z"),
            ]}
            event_path = os.path.join(tmp, "event.json")
            with open(event_path, "w") as handle:
                json.dump({"before": failed, "after": after}, handle)
            output_path = os.path.join(tmp, "output")
            with open(output_path, "w") as handle:
                handle.write("earlier=kept\n")

            with RunsServer({RUNS_PATH: runs}) as api:
                env = dict(self.env)
                env.pop("CI_SCOPE_RULES", None)
                env.update({
                    "GITHUB_EVENT_NAME": "push",
                    "GITHUB_EVENT_PATH": event_path,
                    "GITHUB_REF_TYPE": "branch",
                    "GITHUB_REF_NAME": "main",
                    "GITHUB_REPOSITORY": REPO,
                    "GITHUB_RUN_ID": RUN_ID,
                    "GITHUB_WORKFLOW_REF": REPO + "/.github/workflows/ci.yml@refs/heads/main",
                    "GITHUB_SHA": after,
                    "GITHUB_OUTPUT": output_path,
                    "GITHUB_API_URL": api.url,
                    "CI_API_TOKEN": "secret-token",
                })
                run = subprocess.run(
                    [sys.executable, SCRIPT], cwd=self.repo, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
            self.assertNotIn(b"secret-token", run.stdout + run.stderr)
            with open(output_path) as handle:
                written = handle.read().splitlines()
            self.assertEqual(written, [
                "earlier=kept",
                "full=false",
                'segments=["api","unit"]',
                "reason=3 files changed since the last passing run (%s). Segments: api, unit" % passed[:7],
                "changed=3",
            ])


if __name__ == "__main__":
    unittest.main()

# Probe. Touches one test file so a pull request selects one segment.
