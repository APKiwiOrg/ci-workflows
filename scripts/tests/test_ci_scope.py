"""Tests for .github/actions/ci-scope/scope.py.

Run from the repository root:
    python3 -m unittest discover -s scripts/tests -p 'test_ci_*.py' -v

Every refusal or full-run case sits beside a control that differs only in that one thing. Only the
end to end test builds a git repository. The rest pass plain values and a fake git.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "actions", "ci-scope", "scope.py")

_spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope)

BASE = "a" * 40
HEAD = "b" * 40
ZEROS = "0" * 40

RULES_TEXT = """\
# comment
unit: src/core/** tests/core/**
api: src/api/** tests/api/**
full: .github/** *.props global.json
ignore: docs/** **/*.md
"""


class FakeGit(object):
    """Stands in for git. Knows which commits the clone holds and which a fetch can bring in."""

    def __init__(self, present=(), fetchable=(), diff=b"", diff_code=0):
        self.present = set(present)
        self.fetchable = set(fetchable)
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
        raise AssertionError("unexpected git call: %r" % (args,))


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


class BaseForTests(unittest.TestCase):
    def test_push_compares_two_dot_from_before(self):
        self.assertEqual(scope.base_for("push", {"before": BASE}, "branch"), (BASE, False, None))

    def test_new_branch_push_has_no_base(self):
        self.assertEqual(scope.base_for("push", {"before": ZEROS}, "branch"), (None, False, None))

    def test_push_without_before_has_no_base(self):
        self.assertEqual(scope.base_for("push", {}, "branch"), (None, False, None))

    def test_before_that_is_not_a_sha_has_no_base(self):
        self.assertEqual(scope.base_for("push", {"before": "--upload-pack=x"}, "branch"), (None, False, None))

    def test_pull_request_compares_three_dot_from_base_sha(self):
        event = {"pull_request": {"base": {"sha": BASE}}}
        self.assertEqual(scope.base_for("pull_request", event, "branch"), (BASE, True, None))

    def test_tag_is_a_full_run(self):
        self.assertEqual(scope.base_for("push", {"before": BASE}, "tag"), (None, False, "Full run: tag"))

    def test_schedule_is_a_full_run(self):
        self.assertEqual(scope.base_for("schedule", {}, "branch"), (None, False, "Full run: schedule"))

    def test_dispatch_is_a_full_run(self):
        self.assertEqual(
            scope.base_for("workflow_dispatch", {}, "branch"), (None, False, "Full run: workflow_dispatch"))

    def test_other_event_has_no_base(self):
        self.assertEqual(scope.base_for("merge_group", {"before": BASE}, "branch"), (None, False, None))


class DecideTests(unittest.TestCase):
    """The table rows that need the event, the rules file and git together."""

    def push(self, rules_text=RULES_TEXT, event_name="push", before=BASE, ref_type="branch", git=None):
        git = git if git is not None else FakeGit(present=[BASE], diff=names("src/core/a.py"))
        return scope.decide(event_name, {"before": before}, ref_type, rules_text, HEAD, git), git

    def test_control_push_selects_segments(self):
        result, git = self.push()
        self.assertEqual(result, scope.Scope(False, ["unit"], "1 file changed. Segments: unit", 1))
        self.assertEqual(git.calls[-1], ["diff", "--name-only", "-z", "--no-renames", BASE + ".." + HEAD])

    def test_schedule_is_a_full_run_listing_every_segment(self):
        result, git = self.push(event_name="schedule")
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: schedule", None))
        self.assertEqual(git.calls, [])

    def test_tag_is_a_full_run(self):
        result, _ = self.push(ref_type="tag")
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: tag", None))

    def test_missing_rules_file_is_a_full_run(self):
        result, _ = self.push(rules_text=None)
        self.assertEqual(result, scope.Scope(True, [], "Full run: no rules file", None))

    def test_bad_rules_line_is_a_full_run(self):
        result, _ = self.push(rules_text=RULES_TEXT + "broken\n")
        self.assertEqual(
            result, scope.Scope(True, ["api", "unit"], "Full run: rules file line 6 is not NAME: GLOBS", None))

    def test_new_branch_push_is_a_full_run(self):
        result, git = self.push(before=ZEROS)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: no base commit to compare", None))
        self.assertEqual(git.calls, [])

    def test_base_missing_and_unfetchable_is_a_full_run(self):
        git = FakeGit(present=[], diff=names("src/core/a.py"))
        result, _ = self.push(git=git)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: no base commit to compare", None))
        fetches = [c for c in git.calls if c[0] == "fetch"]
        self.assertEqual(fetches, [["fetch", "--no-tags", "--depth=1", "origin", BASE]])
        self.assertFalse([c for c in git.calls if c[0] == "diff"])

    def test_control_base_missing_but_fetchable(self):
        git = FakeGit(present=[], fetchable=[BASE], diff=names("src/core/a.py"))
        result, _ = self.push(git=git)
        self.assertEqual(result, scope.Scope(False, ["unit"], "1 file changed. Segments: unit", 1))

    def test_base_present_is_not_fetched(self):
        git = FakeGit(present=[BASE], diff=names("src/core/a.py"))
        self.push(git=git)
        self.assertFalse([c for c in git.calls if c[0] == "fetch"])

    def test_failed_diff_is_a_full_run(self):
        git = FakeGit(present=[BASE], diff=b"", diff_code=128)
        result, _ = self.push(git=git)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: no base commit to compare", None))

    def test_full_glob_change_counts_files(self):
        git = FakeGit(present=[BASE], diff=names("global.json", "src/core/a.py"))
        result, _ = self.push(git=git)
        self.assertEqual(result, scope.Scope(True, ["api", "unit"], "Full run: global.json changes shared files", 2))

    def test_pull_request_diffs_three_dot(self):
        git = FakeGit(present=[BASE], diff=names("src/api/a.py"))
        event = {"pull_request": {"base": {"sha": BASE}}}
        result = scope.decide("pull_request", event, "branch", RULES_TEXT, HEAD, git)
        self.assertEqual(result, scope.Scope(False, ["api"], "1 file changed. Segments: api", 1))
        self.assertEqual(git.calls[-1], ["diff", "--name-only", "-z", "--no-renames", BASE + "..." + HEAD])

    def test_odd_file_names_survive(self):
        git = FakeGit(present=[BASE], diff=names("src/core/odd name\n.py", "docs/x.md"))
        result, _ = self.push(git=git)
        self.assertEqual(result, scope.Scope(False, ["unit"], "2 files changed. Segments: unit", 2))


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


class MainEndToEndTest(unittest.TestCase):
    """One real run of the script against a temporary git repository with two commits."""

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

    def test_push_selects_the_changed_segment(self):
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
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "first")
            before = self.git("rev-parse", "HEAD")
            self.write("src/core/a.py", "two\n")
            self.write("docs/odd name.md", "notes\n")
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "second")
            after = self.git("rev-parse", "HEAD")

            event_path = os.path.join(tmp, "event.json")
            with open(event_path, "w") as handle:
                json.dump({"before": before, "after": after}, handle)
            output_path = os.path.join(tmp, "output")
            with open(output_path, "w") as handle:
                handle.write("earlier=kept\n")

            env = dict(self.env)
            env.pop("CI_SCOPE_RULES", None)
            env.update({
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_REF_TYPE": "branch",
                "GITHUB_SHA": after,
                "GITHUB_OUTPUT": output_path,
            })
            run = subprocess.run(
                [sys.executable, SCRIPT], cwd=self.repo, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
            with open(output_path) as handle:
                written = handle.read().splitlines()
            self.assertEqual(written, [
                "earlier=kept",
                "full=false",
                'segments=["unit"]',
                "reason=2 files changed. Segments: unit",
                "changed=2",
            ])


if __name__ == "__main__":
    unittest.main()
