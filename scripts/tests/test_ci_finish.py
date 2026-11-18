"""Tests for .github/actions/ci-finish/finish.py.

Run from the repository root:
    python3 -m unittest discover -s scripts/tests -p 'test_ci_*.py' -v

Every refusal sits beside a control that differs only in that one thing. A fake fetch returns canned
dictionaries, so no test reaches the network. The one real-process test points the API at a closed local
port.
"""
import datetime
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ACTIONS = os.path.join(ROOT, ".github", "actions")
SCRIPT = os.path.join(ACTIONS, "ci-finish", "finish.py")

_spec = importlib.util.spec_from_file_location("ci_finish", SCRIPT)
finish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(finish)
github_api = finish.github_api

REPO = "example-org/example-app"
RUN_ID = "7"
OWN_ID = 901
JOB_IDS = itertools.count(1000)
TREE = "c" * 40
NOW = datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
JOBS_PATH = "/repos/%s/actions/runs/%s/jobs?per_page=100&page=%d"
ARTIFACTS_PATH = "/repos/%s/actions/artifacts?per_page=100" % REPO


def job(name, conclusion="success", started="2026-01-20T11:58:00Z", completed="2026-01-20T11:58:52Z",
        steps=(), job_id=None):
    return {"id": next(JOB_IDS) if job_id is None else job_id,
            "name": name, "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
            "started_at": started, "completed_at": completed if conclusion else None, "steps": list(steps)}


def own_job(name="CI passed", conclusion=None, **fields):
    return job(name, conclusion, job_id=OWN_ID, **fields)


def step(name, conclusion="success", started="2026-01-20T11:58:10Z", completed="2026-01-20T11:58:40Z"):
    return {"name": name, "conclusion": conclusion, "started_at": started,
            "completed_at": completed if conclusion else None}


def needs(**results):
    return json.dumps({name.replace("_", "-"): {"result": result, "outputs": {}}
                       for name, result in results.items()})


class VerdictTests(unittest.TestCase):
    def test_all_success_passes(self):
        self.assertEqual(finish.verdict(needs(scope="success", build="success"), set()), (True, []))

    def test_failure_fails(self):
        passed, problems = finish.verdict(needs(scope="success", build="failure"), set())
        self.assertIs(passed, False)
        self.assertEqual(len(problems), 1)

    def test_cancelled_fails(self):
        passed, _ = finish.verdict(needs(scope="success", build="cancelled"), set())
        self.assertIs(passed, False)

    def test_cancelled_fails_even_when_allowed(self):
        passed, _ = finish.verdict(needs(scope="success", build="cancelled"), {"build"})
        self.assertIs(passed, False)

    def test_skipped_fails_unless_allowed(self):
        passed, _ = finish.verdict(needs(scope="success", test_api="skipped"), set())
        self.assertIs(passed, False)

    def test_control_skipped_and_allowed_passes(self):
        self.assertEqual(finish.verdict(needs(scope="success", test_api="skipped"), {"test-api"}), (True, []))

    def test_allowing_another_job_does_not_pass_a_skip(self):
        passed, _ = finish.verdict(needs(scope="success", test_api="skipped"), {"test-unit"})
        self.assertIs(passed, False)

    def test_empty_object_fails(self):
        passed, problems = finish.verdict("{}", set())
        self.assertIs(passed, False)
        self.assertEqual(len(problems), 1)

    def test_not_a_json_object_fails_with_the_stated_problem(self):
        for text in ("{", "", "[]", "null", '"success"', "not json"):
            with self.subTest(text=text):
                self.assertEqual(finish.verdict(text, set()), (False, ["needs input is not a JSON object"]))

    def test_job_entry_without_a_result_fails(self):
        passed, _ = finish.verdict('{"build": {"outputs": {}}}', set())
        self.assertIs(passed, False)

    def test_job_entry_that_is_not_an_object_fails(self):
        passed, _ = finish.verdict('{"build": "success"}', set())
        self.assertIs(passed, False)

    def test_unknown_result_fails(self):
        passed, _ = finish.verdict(needs(build="neutral"), set())
        self.assertIs(passed, False)

    def test_parsed_object_is_accepted(self):
        self.assertEqual(finish.verdict({"build": {"result": "success"}}, set()), (True, []))

    def test_segment_skip_fails_a_full_run(self):
        self.assertEqual(finish.verdict(needs(scope="success", test_api="skipped"), {"test-api"}, full=True),
                         (False, ["test-api was skipped on a full run"]))

    def test_control_segment_skip_passes_a_partial_run(self):
        self.assertEqual(finish.verdict(needs(scope="success", test_api="skipped"), {"test-api"}, full=False),
                         (True, []))

    def test_always_allowed_skip_passes_a_full_run(self):
        self.assertEqual(
            finish.verdict(needs(scope="success", deploy="skipped"), set(), always={"deploy"}, full=True),
            (True, []))

    def test_always_allowed_skip_passes_a_partial_run(self):
        self.assertEqual(finish.verdict(needs(scope="success", deploy="skipped"), set(), always={"deploy"}),
                         (True, []))

    def test_always_allowed_does_not_excuse_a_failure(self):
        passed, _ = finish.verdict(needs(scope="success", deploy="failure"), set(), always={"deploy"})
        self.assertIs(passed, False)

    def test_id_in_both_skip_lists_fails_a_full_run(self):
        # The segment rule wins, so a full run cannot pass with a segment job skipped.
        self.assertEqual(
            finish.verdict(needs(scope="success", test_api="skipped"), {"test-api"}, always={"test-api"},
                           full=True),
            (False, ["test-api was skipped on a full run"]))

    def test_control_id_in_both_skip_lists_passes_a_partial_run(self):
        self.assertEqual(
            finish.verdict(needs(scope="success", test_api="skipped"), {"test-api"}, always={"test-api"}),
            (True, []))


class ParseFullTests(unittest.TestCase):
    def test_true_and_false_in_any_case_with_spaces(self):
        for text, value in (("true", True), (" TRUE ", True), ("True", True), ("false", False),
                            ("False\n", False)):
            with self.subTest(text=text):
                self.assertIs(finish.parse_full(text), value)

    def test_anything_else_is_none(self):
        for text in ("", " ", "1", "0", "yes", "no", "truee", None):
            with self.subTest(text=text):
                self.assertIsNone(finish.parse_full(text))


class AllowedTests(unittest.TestCase):
    def test_spaces_and_commas_separate(self):
        self.assertEqual(finish.parse_allowed(" test-api, docs\nlint,,"), {"test-api", "docs", "lint"})

    def test_empty(self):
        self.assertEqual(finish.parse_allowed(""), set())


class StageOfTests(unittest.TestCase):
    CASES = [
        ("Scope", "Scope"),
        ("Build", "Build"),
        ("Build and test", "Build and test"),
        ("Test", "Test"),
        ("Test: unit", "Test: unit"),
        ("Package", "Package"),
        ("Package: linux-x64", "Package: linux-x64"),
        ("Deploy: staging", "Deploy: staging"),
        ("CI passed", "CI passed"),
        ("Checks / Build", "Build"),
        ("Outer / Checks / Test: api", "Test: api"),
        ("Build / compile", ""),
        ("build", ""),
        ("Test:", ""),
        ("Test: ", ""),
        ("Deploy", ""),
        ("Lint", ""),
        ("", ""),
    ]

    def test_table(self):
        for name, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(finish.stage_of(name), expected)


class DurationTests(unittest.TestCase):
    CASES = [
        (None, ""),
        (-5, "0 s"),
        (0, "0 s"),
        (52, "52 s"),
        (52.9, "52 s"),
        (59, "59 s"),
        (60, "1 min 0 s"),
        (108, "1 min 48 s"),
        (3599, "59 min 59 s"),
        (3600, "1 h 0 min"),
        (3720, "1 h 2 min"),
        (3779, "1 h 2 min"),
        (90061, "25 h 1 min"),
    ]

    def test_table(self):
        for seconds, expected in self.CASES:
            with self.subTest(seconds=seconds):
                self.assertEqual(finish.duration(seconds), expected)


class RowsTests(unittest.TestCase):
    def rows(self, jobs, segments=None, scope_reason=""):
        return [tuple(row) for row in finish.rows(jobs, OWN_ID, segments, scope_reason)]

    def test_job_named_by_stage_gives_one_row(self):
        self.assertEqual(self.rows([job("Build")]), [("Build", "Passed", 52, "")])

    def test_reusable_workflow_job_uses_the_called_name(self):
        self.assertEqual(self.rows([job("Checks / Build")]), [("Build", "Passed", 52, "")])

    def test_job_with_stage_named_steps_gives_one_row_per_step(self):
        steps = [step("Set up job"), step("Build"), step("Test: unit", "failure"), step("Post checkout")]
        self.assertEqual(self.rows([job("pipeline", "failure", steps=steps)]),
                         [("Build", "Passed", 30, ""), ("Test: unit", "Failed", 30, "")])

    def test_other_job(self):
        self.assertEqual(self.rows([job("lint")]), [("Other: lint", "Passed", 52, "")])

    def test_other_job_whose_steps_are_not_stages(self):
        self.assertEqual(self.rows([job("lint", steps=[step("Run linter")])]),
                         [("Other: lint", "Passed", 52, "")])

    def test_own_job_is_left_for_the_verdict_row(self):
        self.assertEqual(self.rows([job("Scope"), own_job()]), [("Scope", "Passed", 52, "")])

    def test_own_job_inside_a_reusable_workflow_is_left_out(self):
        self.assertEqual(self.rows([job("Scope"), own_job("Checks / CI passed")]),
                         [("Scope", "Passed", 52, "")])

    def test_result_words(self):
        jobs = [job("Test: a", "success"), job("Test: b", "failure"), job("Test: c", "cancelled"),
                job("Test: d", "skipped"), job("Test: e", None)]
        self.assertEqual([row[1] for row in self.rows(jobs, ["a", "b", "c", "d", "e"])],
                         ["Passed", "Failed", "Cancelled", "Skipped", "Running"])

    def test_skipped_and_running_rows_have_no_time(self):
        rows = self.rows([job("Test: d", "skipped"), job("Test: e", None)], ["d", "e"])
        self.assertEqual([row[2] for row in rows], [None, None])

    def test_row_without_readable_times_has_no_time(self):
        self.assertEqual(self.rows([job("Build", started=None)]), [("Build", "Passed", None, "")])

    def test_scope_row_carries_the_scope_reason(self):
        self.assertEqual(self.rows([job("Scope")], scope_reason="3 files changed. Segments: unit"),
                         [("Scope", "Passed", 52, "3 files changed. Segments: unit")])

    def test_skipped_segment_outside_the_selection_says_so(self):
        self.assertEqual(self.rows([job("Test: api", "skipped")], ["unit"]),
                         [("Test: api", "Skipped", None, "Not affected by this change")])

    def test_control_skipped_segment_inside_the_selection_has_no_detail(self):
        self.assertEqual(self.rows([job("Test: api", "skipped")], ["unit", "api"]),
                         [("Test: api", "Skipped", None, "")])

    def test_control_segment_that_ran_outside_the_selection_has_no_detail(self):
        self.assertEqual(self.rows([job("Test: api")], ["unit"]), [("Test: api", "Passed", 52, "")])

    def test_skipped_segment_without_a_selection_has_no_detail(self):
        self.assertEqual(self.rows([job("Test: api", "skipped")], None),
                         [("Test: api", "Skipped", None, "")])


class TableTests(unittest.TestCase):
    def test_verdict_row_comes_last_with_the_verdict_and_own_time(self):
        jobs = [own_job(started="2026-01-20T11:59:58Z"), job("Build")]
        table = [tuple(row) for row in finish.table(
            needs(build="success"), jobs, OWN_ID, None, "", True, NOW)]
        self.assertEqual(table, [("Build", "Passed", 52, ""), ("CI passed", "Passed", 2, "")])

    def test_failed_verdict_row(self):
        table = finish.table(needs(build="failure"), [job("Build", "failure")], OWN_ID, None, "", False, NOW)
        self.assertEqual(tuple(table[-1]), ("CI passed", "Failed", None, ""))

    def test_api_unavailable_falls_back_to_needs(self):
        table = [tuple(row) for row in finish.table(
            needs(scope="success", test_unit="failure", test_api="skipped", deploy="cancelled"),
            None, OWN_ID, ["unit"], "reason", False, NOW)]
        self.assertEqual(table, [
            ("scope", "Passed", None, ""),
            ("test-unit", "Failed", None, ""),
            ("test-api", "Skipped", None, ""),
            ("deploy", "Cancelled", None, ""),
            ("CI passed", "Failed", None, ""),
        ])

    def test_malformed_needs_and_no_api_still_give_the_verdict_row(self):
        table = finish.table("{", None, OWN_ID, None, "", False, NOW)
        self.assertEqual([tuple(row) for row in table], [("CI passed", "Failed", None, "")])


class LastFullPassTests(unittest.TestCase):
    def marker(self, created, name="ci-passed-full-" + TREE, expired=False, expires="2026-02-18T10:00:00Z",
               head_repository_id=1):
        return {"name": name, "created_at": created, "expired": expired, "expires_at": expires,
                "workflow_run": {"id": 101, "repository_id": 1, "head_repository_id": head_repository_id}}

    def test_newest_unexpired_marker_date(self):
        artifacts = [self.marker("2026-01-10T23:30:00Z"), self.marker("2026-01-15T08:00:00Z")]
        self.assertEqual(finish.last_full_pass(artifacts, NOW), "2026-01-15")

    def test_expired_markers_are_skipped(self):
        artifacts = [self.marker("2026-01-10T23:30:00Z"), self.marker("2026-01-15T08:00:00Z", expired=True)]
        self.assertEqual(finish.last_full_pass(artifacts, NOW), "2026-01-10")

    def test_markers_past_their_expiry_time_are_skipped(self):
        artifacts = [self.marker("2026-01-10T23:30:00Z"),
                     self.marker("2026-01-15T08:00:00Z", expires="2026-01-20T11:00:00Z")]
        self.assertEqual(finish.last_full_pass(artifacts, NOW), "2026-01-10")

    def test_other_artifacts_are_skipped(self):
        artifacts = [self.marker("2026-01-10T23:30:00Z"), self.marker("2026-01-15T08:00:00Z", name="coverage")]
        self.assertEqual(finish.last_full_pass(artifacts, NOW), "2026-01-10")

    def test_markers_from_a_fork_are_skipped(self):
        artifacts = [self.marker("2026-01-10T23:30:00Z"),
                     self.marker("2026-01-15T08:00:00Z", head_repository_id=2)]
        self.assertEqual(finish.last_full_pass(artifacts, NOW), "2026-01-10")

    def test_markers_without_a_run_are_skipped(self):
        marker = self.marker("2026-01-15T08:00:00Z")
        del marker["workflow_run"]
        self.assertEqual(finish.last_full_pass([self.marker("2026-01-10T23:30:00Z"), marker], NOW),
                         "2026-01-10")

    def test_failed_markers_are_skipped(self):
        artifacts = [self.marker("2026-01-10T23:30:00Z"),
                     self.marker("2026-01-15T08:00:00Z", name="ci-failed-full-" + TREE)]
        self.assertEqual(finish.last_full_pass(artifacts, NOW), "2026-01-10")

    def test_none_recorded(self):
        self.assertEqual(finish.last_full_pass([], NOW), "not recorded")
        self.assertEqual(finish.last_full_pass([self.marker("2026-01-15T08:00:00Z", expired=True)], NOW),
                         "not recorded")


class RenderTests(unittest.TestCase):
    def test_passed_summary(self):
        rows = [
            ("Scope", "Passed", 6, "3 files changed. Segments: unit"),
            ("Test: unit", "Passed", 52, ""),
            ("Test: api", "Skipped", None, "Not affected by this change"),
            ("CI passed", "Passed", 2, ""),
        ]
        expected = (
            "## example-app CI, push\n"
            "\n"
            "Passed in 1 min 48 s. Ran 1 of 2 test segments.\n"
            "\n"
            "| Stage | Result | Time | Detail |\n"
            "| --- | --- | --- | --- |\n"
            "| Scope | \u2705 Passed | 6 s | 3 files changed\\. Segments\\: unit |\n"
            "| Test\\: unit | \u2705 Passed | 52 s |  |\n"
            "| Test\\: api | \u26AA Skipped |  | Not affected by this change |\n"
            "| CI passed | \u2705 Passed | 2 s |  |\n"
            "\n"
            "Full suite last passed: 2026-01-15\n"
        )
        self.assertEqual(finish.render("example-app CI", "push", True, rows, "2026-01-15", 108), expected)

    def test_failed_summary_without_segments_or_duration(self):
        rows = [
            ("Build", "Failed", 70, "a | b"),
            ("Other: lint", "Cancelled", 3, ""),
            ("Deploy: staging", "Running", None, ""),
            ("CI passed", "Failed", None, ""),
        ]
        expected = (
            "## example-app CI, pull_request\n"
            "\n"
            "Failed.\n"
            "\n"
            "| Stage | Result | Time | Detail |\n"
            "| --- | --- | --- | --- |\n"
            "| Build | \U0001F534 Failed | 1 min 10 s | a \\| b |\n"
            "| Other\\: lint | \U0001F534 Cancelled | 3 s |  |\n"
            "| Deploy\\: staging | \U0001F504 Running |  |  |\n"
            "| CI passed | \U0001F534 Failed |  |  |\n"
            "\n"
            "Full suite last passed: not recorded\n"
        )
        self.assertEqual(
            finish.render("example-app CI", "pull_request", False, rows, "not recorded", None), expected)

    def test_one_segment_is_singular(self):
        text = finish.render("t", "push", True, [("Test: unit", "Passed", 1, "")], "this run", 1)
        self.assertIn("Passed in 1 s. Ran 1 of 1 test segment.\n", text)

    def test_hostile_text_cannot_render_as_markdown(self):
        hostile = ("[Passed, safe to merge](https://example.test) ![x](y) <b>*bold*</b> _u_ ~s~ `c` "
                   "&amp; www.example.test a@example.test :rocket: $x$ #1 @someone \\")
        text = finish.render("t", "pull_request", False, [("Scope", "Passed", 1, hostile)], "x", 1)
        cell = text.split("| Scope |", 1)[1].split("\n", 1)[0]
        # Every ASCII punctuation character is escaped, so CommonMark shows it as itself.
        for index, char in enumerate(cell):
            if char in "[]()!<>*_~`&.:$#@":
                self.assertEqual(cell[index - 1], "\\", "unescaped %r in %r" % (char, cell))
        self.assertIn("\\[Passed\\, safe to merge\\]\\(https\\:\\/\\/example\\.test\\)", cell)

    def test_hostile_job_name_is_escaped(self):
        rows = finish.rows([job("[ok](https://example.test)")], OWN_ID, None, "")
        text = finish.render("t", "push", True, rows, "x", 1)
        self.assertIn("| Other\\: \\[ok\\]\\(https\\:\\/\\/example\\.test\\) |", text)


class FakeFetch(object):
    def __init__(self, jobs=None, artifacts=None):
        self.answers = {}
        if jobs is not None:
            self.answers[JOBS_PATH % (REPO, RUN_ID, 1)] = {"total_count": len(jobs), "jobs": jobs}
        if artifacts is not None:
            self.answers[ARTIFACTS_PATH] = {"total_count": len(artifacts), "artifacts": artifacts}

    def __call__(self, path):
        answer = self.answers.get(path)
        if answer is None:
            raise github_api.ApiError("HTTP 404 for " + path)
        return answer


class FetchJobsTests(unittest.TestCase):
    def test_reads_up_to_three_pages(self):
        pages = {}
        for page in (1, 2, 3, 4):
            pages[JOBS_PATH % (REPO, RUN_ID, page)] = {
                "total_count": 400, "jobs": [job("lint %d %d" % (page, i)) for i in range(100)]}
        jobs, error = finish.fetch_jobs(REPO, RUN_ID, pages.__getitem__)
        self.assertEqual(len(jobs), 300)
        self.assertIsNotNone(error)

    def test_stops_at_a_short_page(self):
        pages = {JOBS_PATH % (REPO, RUN_ID, 1): {"total_count": 2, "jobs": [job("Build"), job("Scope")]}}
        jobs, error = finish.fetch_jobs(REPO, RUN_ID, pages.__getitem__)
        self.assertEqual(len(jobs), 2)
        self.assertIsNone(error)

    def test_answer_without_a_list_reports_an_error(self):
        pages = {JOBS_PATH % (REPO, RUN_ID, 1): {"total_count": 2}}
        jobs, error = finish.fetch_jobs(REPO, RUN_ID, pages.__getitem__)
        self.assertEqual(jobs, [])
        self.assertIsNotNone(error)


def settings(**overrides):
    env = {
        "CI_FINISH_NEEDS": needs(scope="success", test_unit="success", test_api="skipped"),
        "CI_FINISH_ALLOW_SKIPPED": "test-api",
        "CI_FINISH_FULL": "false",
        "CI_FINISH_SEGMENTS": '["unit"]',
        "CI_FINISH_SCOPE_REASON": "1 file changed. Segments: unit",
        "CI_FINISH_TITLE": "",
        "CI_FINISH_CHECK_RUN_ID": str(OWN_ID),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_RUN_ID": RUN_ID,
        "GITHUB_EVENT_NAME": "push",
    }
    env.update(overrides)
    return env


JOBS = [
    job("Scope", started="2026-01-20T11:58:00Z", completed="2026-01-20T11:58:06Z"),
    job("Test: unit", started="2026-01-20T11:58:10Z", completed="2026-01-20T11:59:02Z"),
    job("Test: api", "skipped", started="2026-01-20T11:58:10Z", completed="2026-01-20T11:58:10Z"),
    own_job(started="2026-01-20T11:59:58Z"),
]
FULL_JOBS = JOBS[:2] + [job("Test: api"), JOBS[-1]]
ARTIFACTS = [{"name": "ci-passed-full-" + "d" * 40, "created_at": "2026-01-15T08:00:00Z",
              "expired": False, "expires_at": "2026-02-14T08:00:00Z",
              "workflow_run": {"id": 99, "repository_id": 1, "head_repository_id": 1}}]


def good_tree():
    return TREE


def no_tree():
    return None


class FinishTests(unittest.TestCase):
    def test_partial_pass_writes_the_summary_and_uploads_nothing(self):
        outcome = finish.finish(settings(), FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, True)
        self.assertIs(outcome.upload, False)
        expected = (
            "## example-app CI, push\n"
            "\n"
            "Passed in 2 min 0 s. Ran 1 of 2 test segments.\n"
            "\n"
            "| Stage | Result | Time | Detail |\n"
            "| --- | --- | --- | --- |\n"
            "| Scope | \u2705 Passed | 6 s | 1 file changed\\. Segments\\: unit |\n"
            "| Test\\: unit | \u2705 Passed | 52 s |  |\n"
            "| Test\\: api | \u26AA Skipped |  | Not affected by this change |\n"
            "| CI passed | \u2705 Passed | 2 s |  |\n"
            "\n"
            "Full suite last passed: 2026-01-15\n"
        )
        self.assertEqual(outcome.summary, expected)

    def full(self, **results):
        return settings(CI_FINISH_FULL="true", CI_FINISH_SEGMENTS='["api","unit"]',
                        CI_FINISH_NEEDS=needs(**results) if results else needs(
                            scope="success", test_unit="success", test_api="success"))

    def test_control_full_pass_uploads_the_marker(self):
        outcome = finish.finish(self.full(), FakeFetch(FULL_JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, True)
        self.assertIs(outcome.upload, True)
        self.assertEqual(outcome.tree, TREE)
        self.assertEqual(outcome.artifact, "ci-passed-full-" + TREE)
        self.assertTrue(outcome.summary.endswith("Full suite last passed: this run\n"))

    def test_full_run_with_an_allowed_segment_skip_fails_and_uploads_nothing(self):
        # Every segment runs on a full run, so a skipped segment job is a wiring fault, not a pass.
        outcome = finish.finish(self.full(scope="success", test_unit="success", test_api="skipped"),
                                FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertTrue(any("test-api" in problem for problem in outcome.problems))
        self.assertIs(outcome.upload, False)
        self.assertEqual(outcome.artifact, "")

    def test_full_run_with_no_known_segments_fails_when_every_test_job_skipped(self):
        # A missing rules file gives full=true and segments=[]. Test jobs gated on segments alone skip.
        env = settings(CI_FINISH_FULL="true", CI_FINISH_SEGMENTS="[]",
                       CI_FINISH_ALLOW_SKIPPED="test-workflows test-actions",
                       CI_FINISH_NEEDS=needs(scope="success", test_workflows="skipped", test_actions="skipped"))
        jobs = [job("Scope"), job("Test: workflows", "skipped"), job("Test: actions", "skipped"),
                own_job()]
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertIs(outcome.upload, False)

    def test_full_run_with_an_always_allowed_skip_passes_and_uploads_the_marker(self):
        env = self.full(scope="success", test_unit="success", test_api="success", deploy="skipped")
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = "deploy"
        jobs = FULL_JOBS + [job("deploy", "skipped")]
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, True)
        self.assertEqual(outcome.artifact, "ci-passed-full-" + TREE)

    def test_full_run_rejects_a_skipped_nested_job(self):
        jobs = [job("Scope"), job("checks / Publish docs, preview", "skipped"), own_job()]
        env = self.full(scope="success", checks="success")
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        with self.subTest("verdict"):
            self.assertIs(outcome.passed, False)
        with self.subTest("diagnostic"):
            self.assertTrue(any("checks / Publish docs, preview" in problem for problem in outcome.problems))
        with self.subTest("marker"):
            self.assertEqual((outcome.upload, outcome.artifact), (False, ""))

    def test_full_run_allows_a_skip_by_exact_nested_name(self):
        jobs = [job("Scope"), job("checks / Publish docs, preview", "skipped"), own_job()]
        env = self.full(scope="success", checks="success")
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = '["checks / Publish docs, preview"]'
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.artifact), (True, "ci-passed-full-" + TREE))

    def test_full_run_requires_the_caller_prefix_for_a_nested_skip(self):
        jobs = [job("Scope"), job("checks / Publish docs, preview", "skipped"), own_job()]
        env = self.full(scope="success", checks="success")
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = '["Publish docs, preview"]'
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)

    def test_partial_run_ignores_a_nested_skip(self):
        jobs = [job("Scope"), job("checks / Publish docs, preview", "skipped"), own_job()]
        env = settings(CI_FINISH_NEEDS=needs(scope="success", checks="success"))
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.artifact), (True, ""))

    def test_full_run_that_failed_uploads_the_failed_marker(self):
        outcome = finish.finish(self.full(scope="success", test_unit="failure", test_api="success"),
                                FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertIs(outcome.upload, True)
        self.assertEqual(outcome.tree, TREE)
        self.assertEqual(outcome.artifact, "ci-failed-full-" + TREE)
        self.assertTrue(outcome.summary.endswith("Full suite last passed: 2026-01-15\n"))

    def test_full_run_only_cancelled_uploads_no_marker(self):
        outcome = finish.finish(self.full(scope="success", test_unit="cancelled", test_api="success"),
                                FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertIs(outcome.upload, False)
        self.assertEqual(outcome.artifact, "")

    def test_full_run_only_a_disallowed_skip_uploads_no_marker(self):
        outcome = finish.finish(self.full(scope="success", test_unit="success", test_api="skipped"),
                                FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.upload, False)

    def test_partial_run_that_failed_uploads_no_marker(self):
        env = settings(CI_FINISH_NEEDS=needs(scope="success", test_unit="failure", test_api="skipped"))
        outcome = finish.finish(env, FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertIs(outcome.upload, False)

    def test_failed_full_run_without_a_tree_uploads_nothing(self):
        outcome = finish.finish(self.full(scope="success", test_unit="failure", test_api="success"),
                                FakeFetch(JOBS, ARTIFACTS), no_tree, NOW)
        self.assertIs(outcome.upload, False)
        self.assertEqual(outcome.artifact, "")

    def test_failed_job_missing_from_needs_fails_the_run(self):
        jobs = [job("Scope"), job("Test: unit", "failure"), own_job()]
        outcome = finish.finish(settings(CI_FINISH_NEEDS=needs(scope="success")), FakeFetch(jobs, ARTIFACTS),
                                good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertEqual(outcome.problems, [
            "Test: unit: failure, not reported by needs (missing from needs or allowed to fail)"])
        self.assertIn("| CI passed | \U0001F534 Failed |", outcome.summary)
        self.assertTrue(outcome.summary.split("\n")[2].startswith("Failed"))

    def test_timed_out_and_cancelled_jobs_missing_from_needs_fail_the_run(self):
        for conclusion in ("timed_out", "cancelled"):
            with self.subTest(conclusion=conclusion):
                jobs = [job("Scope"), job("lint", conclusion), own_job()]
                outcome = finish.finish(settings(CI_FINISH_NEEDS=needs(scope="success")),
                                        FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
                self.assertIs(outcome.passed, False)

    def test_unfinished_job_missing_from_needs_fails_the_run(self):
        # The jobs API lists a job outside needs that is still going as in_progress with no conclusion, and
        # does not list a job that needs the finishing job at all. So a listed unfinished job is one the
        # verdict does not wait for.
        for status in ("in_progress", "queued", "waiting", "pending", None):
            with self.subTest(status=status):
                slow = {"name": "Unlisted and slow", "status": status, "conclusion": None,
                        "started_at": "2026-01-20T11:58:00Z", "completed_at": None, "steps": []}
                jobs = [job("Scope"), job("Test: unit"), slow, own_job()]
                env = settings(CI_FINISH_NEEDS=needs(scope="success", test_unit="success"))
                outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
                self.assertIs(outcome.passed, False)
                self.assertEqual(outcome.problems,
                                 ["Unlisted and slow has not finished and needs does not wait for it"])
                self.assertIn("| CI passed | \U0001F534 Failed |", outcome.summary)

    def test_unfinished_job_missing_from_needs_withholds_the_pass_marker(self):
        jobs = [job("Scope"), job("Test: unit"), job("Test: api"), job("Package", None), own_job()]
        outcome = finish.finish(self.full(), FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertIs(outcome.upload, False)

    def test_control_finished_passing_or_skipped_jobs_missing_from_needs_keep_the_pass(self):
        jobs = [job("Scope"), job("lint"), job("docs", "skipped"), own_job()]
        outcome = finish.finish(settings(CI_FINISH_NEEDS=needs(scope="success")), FakeFetch(jobs, ARTIFACTS),
                                good_tree, NOW)
        self.assertIs(outcome.passed, True)
        self.assertEqual(outcome.problems, [])

    def test_unfinished_job_does_not_change_a_failed_verdict(self):
        jobs = [job("Scope"), job("Test: unit", "failure"), job("Package", None), own_job()]
        env = settings(CI_FINISH_NEEDS=needs(scope="success", test_unit="failure", test_api="skipped"))
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertEqual(outcome.problems, ["test-unit: failure"])

    def test_unrelated_running_job_is_not_exempted_by_the_configured_name(self):
        jobs = [job("Scope"), job("Verdict", None), own_job()]
        env = settings(CI_FINISH_NEEDS=needs(scope="success"), CI_FINISH_JOB_NAME="Verdict")
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertTrue(any("Verdict" in problem for problem in outcome.problems))

    def test_control_finishing_job_with_a_custom_name_is_left_out_by_id(self):
        jobs = [job("Scope"), own_job("Verdict")]
        env = settings(CI_FINISH_NEEDS=needs(scope="success"))
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, True)

    def test_control_finishing_row_needs_no_conclusion(self):
        jobs = [job("Scope"), dict(own_job(), status="completed")]
        env = self.full(scope="success")
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.artifact), (True, "ci-passed-full-" + TREE))

    def test_jobs_api_error_keeps_the_needs_verdict(self):
        outcome = finish.finish(settings(CI_FINISH_NEEDS=needs(scope="success")), FakeFetch(), good_tree, NOW)
        self.assertIs(outcome.passed, True)

    def test_failed_full_run_missing_from_needs_uploads_no_failed_marker(self):
        jobs = [job("Scope"), job("Test: unit", "failure"), own_job()]
        env = settings(CI_FINISH_FULL="true", CI_FINISH_NEEDS=needs(scope="success"))
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertIs(outcome.upload, False)

    @mock.patch("time.sleep")
    def test_full_run_with_jobs_api_down_fails_and_uploads_no_pass_marker(self, _sleep):
        outcome = finish.finish(self.full(), FakeFetch(artifacts=ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload, outcome.artifact), (False, False, ""))
        self.assertEqual(len(outcome.problems), 1)
        self.assertIn("jobs-audit-incomplete", outcome.problems[0])
        self.assertIn("HTTP 404", outcome.problems[0])
        self.assertIn("Re-run this job", outcome.problems[0])

    @mock.patch("time.sleep")
    def test_transient_jobs_api_error_can_recover_to_a_full_pass(self, _sleep):
        good_fetch = FakeFetch(FULL_JOBS, ARTIFACTS)
        first = True

        def fetch(path):
            nonlocal first
            if first and path == JOBS_PATH % (REPO, RUN_ID, 1):
                first = False
                raise github_api.ApiError("HTTP 503")
            return good_fetch(path)

        outcome = finish.finish(self.full(), fetch, good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.artifact), (True, "ci-passed-full-" + TREE))

    @mock.patch("time.sleep")
    def test_skip_found_before_a_later_page_error_is_preserved(self, _sleep):
        name = "checks / Optional report"
        jobs = FULL_JOBS + [job(name, "skipped")] + [job("Check %d" % n) for n in range(95)]
        fetch = FakeFetch(jobs, ARTIFACTS)
        fetch.answers[JOBS_PATH % (REPO, RUN_ID, 1)]["total_count"] = 101
        outcome = finish.finish(self.full(), fetch, good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any(name in problem for problem in outcome.problems))
        self.assertTrue(any("jobs-audit-incomplete" in problem for problem in outcome.problems))

    def test_summary_error_does_not_erase_an_audit_failure(self):
        name = "checks / Optional report"
        jobs = FULL_JOBS + [job(name, "skipped"), dict(job("Unlabelled check"), steps=7)]
        outcome = finish.finish(self.full(), FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any(name in problem for problem in outcome.problems))
        self.assertIn("| CI passed | \U0001F534 Failed |", outcome.summary)

    def test_full_run_fails_if_pagination_cap_leaves_unread_jobs(self):
        fetch = FakeFetch(artifacts=ARTIFACTS)
        jobs = FULL_JOBS + [job("Check %d" % n) for n in range(296)]
        for page in range(1, 4):
            fetch.answers[JOBS_PATH % (REPO, RUN_ID, page)] = {
                "total_count": 301, "jobs": jobs[(page - 1) * 100:page * 100]}
        outcome = finish.finish(self.full(), fetch, good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("jobs-audit-incomplete" in problem for problem in outcome.problems))

    def test_full_run_passes_after_reading_the_reported_count_across_pages(self):
        jobs = FULL_JOBS + [job("Check %d" % n) for n in range(97)]
        fetch = FakeFetch(jobs[:100], ARTIFACTS)
        fetch.answers[JOBS_PATH % (REPO, RUN_ID, 1)]["total_count"] = 101
        fetch.answers[JOBS_PATH % (REPO, RUN_ID, 2)] = {"total_count": 101, "jobs": jobs[100:]}
        outcome = finish.finish(self.full(), fetch, good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.artifact), (True, "ci-passed-full-" + TREE))

    def test_full_run_fails_if_a_short_page_claims_more_jobs(self):
        fetch = FakeFetch(FULL_JOBS, ARTIFACTS)
        fetch.answers[JOBS_PATH % (REPO, RUN_ID, 1)]["total_count"] += 1
        outcome = finish.finish(self.full(), fetch, good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("jobs-audit-incomplete" in problem for problem in outcome.problems))

    def test_full_run_fails_if_a_job_row_is_not_an_object(self):
        outcome = finish.finish(self.full(), FakeFetch(FULL_JOBS + [None], ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("jobs-audit-incomplete" in problem for problem in outcome.problems))

    def test_full_run_fails_if_a_completed_job_has_no_conclusion(self):
        jobs = FULL_JOBS + [dict(job("Final checks"), conclusion=None)]
        outcome = finish.finish(self.full(), FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("Final checks" in problem for problem in outcome.problems))

    def test_nested_job_with_the_verdict_suffix_is_still_audited(self):
        name = "checks / CI passed"
        jobs = FULL_JOBS + [job(name, "failure")]
        outcome = finish.finish(self.full(), FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any(name in problem for problem in outcome.problems))

    def test_full_run_exempts_only_the_check_run_id_even_with_two_identical_names(self):
        jobs = FULL_JOBS + [job("CI passed", None)]
        outcome = finish.finish(self.full(), FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("CI passed" in problem for problem in outcome.problems))

    def test_full_run_refuses_an_ambiguous_verdict_name_without_identity(self):
        env = self.full()
        del env["CI_FINISH_CHECK_RUN_ID"]
        jobs = FULL_JOBS + [job("CI passed", None)]
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))

    def test_full_run_refuses_an_ambiguous_exact_skip_exception(self):
        name = "checks / Optional report"
        env = self.full()
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = json.dumps([name])
        jobs = FULL_JOBS + [job(name, "skipped"), job(name)]
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any(name in problem for problem in outcome.problems))

    def test_full_run_explains_a_missing_display_name_in_an_old_id_list(self):
        env = self.full(scope="success", deploy="skipped")
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = "deploy"
        outcome = finish.finish(env, FakeFetch(FULL_JOBS + [job("Publish docs", "skipped")], ARTIFACTS),
                                good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("Publish docs" in problem and "allow-skipped-always" in problem
                            for problem in outcome.problems))

    def test_full_run_accepts_both_id_and_exact_display_name(self):
        env = self.full(scope="success", deploy="skipped")
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = '["deploy", "Publish docs"]'
        outcome = finish.finish(env, FakeFetch(FULL_JOBS + [job("Publish docs", "skipped")], ARTIFACTS),
                                good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.artifact), (True, "ci-passed-full-" + TREE))

    def test_json_shaped_always_list_cannot_excuse_a_skipped_job(self):
        env = self.full(scope="success", deploy="skipped")
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = '{"allow": deploy }'
        outcome = finish.finish(env, FakeFetch(FULL_JOBS + [job("deploy", "skipped")], ARTIFACTS),
                                good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("allow-skipped-always" in problem for problem in outcome.problems))

    def test_invalid_json_always_list_fails_even_when_no_job_skips(self):
        for bad in ('["deploy"', '["deploy", 1]', '"deploy"', '{"allow": ["deploy"]}'):
            with self.subTest(bad=bad):
                env = self.full()
                env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = bad
                outcome = finish.finish(env, FakeFetch(FULL_JOBS, ARTIFACTS), good_tree, NOW)
                self.assertEqual((outcome.passed, outcome.upload), (False, False))
                self.assertTrue(any("allow-skipped-always" in problem for problem in outcome.problems))

    def test_bad_token_in_legacy_always_list_invalidates_the_whole_input(self):
        env = self.full(scope="success", deploy="skipped")
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = "deploy, report!"
        outcome = finish.finish(env, FakeFetch(FULL_JOBS + [job("deploy", "skipped")], ARTIFACTS),
                                good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))

    def test_nested_name_is_rejected_in_the_segment_id_list(self):
        env = settings(CI_FINISH_NEEDS=needs(scope="success", checks="skipped"),
                       CI_FINISH_ALLOW_SKIPPED="checks / Optional report")
        outcome = finish.finish(env, FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.upload), (False, False))
        self.assertTrue(any("allow-skipped" in problem for problem in outcome.problems))

    def test_segment_id_does_not_override_an_unrelated_api_name(self):
        env = self.full(scope="success", report="success")
        env["CI_FINISH_ALLOW_SKIPPED"] = "report"
        env["CI_FINISH_ALLOW_SKIPPED_ALWAYS"] = '["report"]'
        jobs = FULL_JOBS + [job("report", "skipped")]
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertEqual((outcome.passed, outcome.artifact), (True, "ci-passed-full-" + TREE))

    @mock.patch("time.sleep")
    def test_full_failure_with_the_jobs_api_down_still_uploads_the_failed_marker(self, _sleep):
        outcome = finish.finish(self.full(scope="success", test_unit="failure", test_api="success"),
                                FakeFetch(artifacts=ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertEqual(outcome.artifact, "ci-failed-full-" + TREE)

    def test_id_in_both_skip_lists_fails_a_full_run_and_uploads_nothing(self):
        env = settings(CI_FINISH_FULL="true", CI_FINISH_SEGMENTS="[]",
                       CI_FINISH_ALLOW_SKIPPED="test-workflows test-actions",
                       CI_FINISH_ALLOW_SKIPPED_ALWAYS="test-workflows test-actions",
                       CI_FINISH_NEEDS=needs(scope="success", test_workflows="skipped", test_actions="skipped"))
        jobs = [job("Scope"), job("Test: workflows", "skipped"), job("Test: actions", "skipped"),
                own_job()]
        outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertTrue(any("test-workflows" in problem for problem in outcome.problems))
        self.assertTrue(any("test-actions" in problem for problem in outcome.problems))
        self.assertIs(outcome.upload, False)

    def test_full_input_other_than_true_or_false_fails(self):
        for value in ("1", "yes", "", " ", "on"):
            with self.subTest(value=value):
                outcome = finish.finish(settings(CI_FINISH_FULL=value), FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
                self.assertIs(outcome.passed, False)
                self.assertEqual(outcome.problems, ["full input is not true or false"])
                self.assertIs(outcome.upload, False)

    def test_missing_full_input_fails(self):
        env = settings()
        del env["CI_FINISH_FULL"]
        outcome = finish.finish(env, FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertEqual(outcome.problems, ["full input is not true or false"])

    def test_control_full_input_in_another_case_is_read(self):
        for value, upload in (("False", False), (" TRUE ", True)):
            with self.subTest(value=value):
                env = self.full()
                env["CI_FINISH_FULL"] = value
                jobs = FULL_JOBS if upload else JOBS
                outcome = finish.finish(env, FakeFetch(jobs, ARTIFACTS), good_tree, NOW)
                self.assertIs(outcome.passed, True)
                self.assertIs(outcome.upload, upload)

    def test_full_input_that_cannot_be_read_with_a_failure_uploads_no_failed_marker(self):
        env = self.full(scope="success", test_unit="failure", test_api="success")
        env["CI_FINISH_FULL"] = "yes"
        outcome = finish.finish(env, FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertEqual(outcome.problems, ["full input is not true or false", "test-unit: failure"])
        self.assertIs(outcome.upload, False)

    def test_full_pass_without_a_tree_uploads_nothing_and_still_passes(self):
        outcome = finish.finish(self.full(), FakeFetch(FULL_JOBS, ARTIFACTS), no_tree, NOW)
        self.assertIs(outcome.passed, True)
        self.assertIs(outcome.upload, False)

    def test_api_down_never_turns_a_failure_into_a_pass(self):
        env = settings(CI_FINISH_NEEDS=needs(scope="success", test_unit="failure"))
        outcome = finish.finish(env, FakeFetch(), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertIn("| test\\-unit | \U0001F534 Failed |  |  |\n", outcome.summary)
        self.assertTrue(outcome.summary.endswith("Full suite last passed: not recorded\n"))

    def test_control_api_down_keeps_a_pass(self):
        outcome = finish.finish(settings(), FakeFetch(), good_tree, NOW)
        self.assertIs(outcome.passed, True)

    def test_skipped_job_not_allowed_fails(self):
        outcome = finish.finish(settings(CI_FINISH_ALLOW_SKIPPED=""), FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)

    def test_malformed_needs_fails_and_still_writes_a_summary(self):
        outcome = finish.finish(settings(CI_FINISH_NEEDS="{"), FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIs(outcome.passed, False)
        self.assertEqual(outcome.problems, ["needs input is not a JSON object"])
        self.assertIn("| CI passed | \U0001F534 Failed |", outcome.summary)

    def test_title_input_overrides_the_default(self):
        outcome = finish.finish(settings(CI_FINISH_TITLE="Checks"), FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertTrue(outcome.summary.startswith("## Checks, push\n"))

    def test_malformed_segments_give_no_detail(self):
        outcome = finish.finish(settings(CI_FINISH_SEGMENTS="unit"), FakeFetch(JOBS, ARTIFACTS), good_tree, NOW)
        self.assertIn("| Test\\: api | \u26AA Skipped |  |  |\n", outcome.summary)


class ScriptProcessTests(unittest.TestCase):
    def run_script(self, tmp, **overrides):
        env = dict(os.environ)
        env.update(settings(**overrides))
        env.update({
            "GITHUB_API_URL": "http://127.0.0.1:9",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_OUTPUT": os.path.join(tmp, "output"),
            "GITHUB_STEP_SUMMARY": os.path.join(tmp, "summary"),
            "RUNNER_TEMP": tmp,
            "CI_API_TOKEN": "secret-token",
        })
        proc = subprocess.run([sys.executable, SCRIPT], cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertNotIn(b"secret-token", proc.stdout + proc.stderr)
        with open(env["GITHUB_OUTPUT"], encoding="utf-8") as handle:
            outputs = dict(line.split("=", 1) for line in handle.read().splitlines())
        with open(env["GITHUB_STEP_SUMMARY"], encoding="utf-8") as handle:
            summary = handle.read()
        return outputs, summary

    def test_full_failure_with_the_api_down_writes_summary_outputs_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs, summary = self.run_script(
                tmp, CI_FINISH_FULL="true", CI_FINISH_NEEDS=needs(scope="success", test_unit="failure"))
            self.assertEqual(outputs["passed"], "false")
            self.assertEqual(outputs["upload"], "true")
            self.assertRegex(outputs["tree"], r"^[0-9a-f]{40}$")
            self.assertEqual(outputs["artifact"], "ci-failed-full-" + outputs["tree"])
            self.assertTrue(summary.startswith("## example-app CI, push\n"))
            self.assertTrue(outputs["marker"].startswith(tmp))
            with open(outputs["marker"], encoding="utf-8") as handle:
                marker = handle.read()
            self.assertIn(outputs["tree"], marker)
            self.assertIn("https://github.com/%s/actions/runs/%s" % (REPO, RUN_ID), marker)


if __name__ == "__main__":
    unittest.main()
