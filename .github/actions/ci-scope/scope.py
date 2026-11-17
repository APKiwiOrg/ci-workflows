#!/usr/bin/env python3
"""Decide which test segments a CI run needs from the files its change touched.

The rules file lists segments and the globs that select them:

    unit: src/core/** tests/core/**
    full: .github/** *.props
    ignore: docs/** **/*.md

A run is full (every segment) when the event or ref asks for one, when the rules cannot be read, when
there is no base commit to compare with, when a changed file matches a `full` glob, or when a changed
file matches no rule at all, so a new folder is never silently left untested. Otherwise the run needs
the segments whose globs match a changed file.

Inputs come from the environment: CI_SCOPE_RULES (the rules file path) and the runner's GITHUB_EVENT_NAME,
GITHUB_EVENT_PATH, GITHUB_REF_TYPE, GITHUB_SHA and GITHUB_OUTPUT. Outputs are appended to GITHUB_OUTPUT as
full, segments (compact JSON), reason and changed. Python 3.8 or later, standard library only.
"""
import functools
import json
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

DEFAULT_RULES = ".github/ci-scope.txt"
FULL_EVENTS = ("schedule", "workflow_dispatch")
NO_BASE = "Full run: no base commit to compare"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

# A git runner takes the arguments after `git` and returns the exit code and standard output.
GitRunner = Callable[[Sequence[str]], Tuple[int, bytes]]


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
        if not colon or not globs or not _NAME.fullmatch(name):
            if error is None:
                error = "Full run: rules file line %d is not NAME: GLOBS" % number
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


def select(rules, files):
    # type: (Rules, Sequence[str]) -> Tuple[bool, List[str], str]
    """Pick the segments a set of changed files needs. Returns (full, segments, reason)."""
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
        return False, segments, "%s. Segments: %s" % (_count(files), ", ".join(segments))
    return False, segments, "%s. No segment affected" % _count(files)


def base_for(event_name, event, ref_type):
    # type: (str, dict, str) -> Tuple[Optional[str], bool, Optional[str]]
    """The commit to compare with. Returns (base, three_dot, full_reason). A full_reason means the event
    always runs everything. A None base with no reason means there is nothing to compare with."""
    if event_name in FULL_EVENTS:
        return None, False, "Full run: " + event_name
    if ref_type == "tag":
        return None, False, "Full run: tag"
    base = None
    three_dot = False
    if event_name == "push":
        base = event.get("before")
    elif event_name == "pull_request":
        base = ((event.get("pull_request") or {}).get("base") or {}).get("sha")
        three_dot = True
    # All zeros is a new branch. Anything that is not a sha is refused before it can reach git as an
    # argument, where a leading dash would read as an option.
    if not isinstance(base, str) or not _SHA.fullmatch(base) or not base.strip("0"):
        return None, False, None
    return base, three_dot, None


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


def decide(event_name, event, ref_type, rules_text, head, git):
    # type: (str, dict, str, Optional[str], str, GitRunner) -> Scope
    """Apply the selection rules in order. rules_text is None when the rules file cannot be read."""
    rules = parse_rules(rules_text) if rules_text is not None else None
    every = sorted(rules.segments) if rules is not None else []
    base, three_dot, reason = base_for(event_name, event, ref_type)
    if reason:
        return Scope(True, every, reason, None)
    if rules is None:
        return Scope(True, every, "Full run: no rules file", None)
    if rules.error:
        return Scope(True, every, rules.error, None)
    files = changed_files(base, head, three_dot, git) if base else None
    if files is None:
        return Scope(True, every, NO_BASE, None)
    full, segments, reason = select(rules, files)
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
    # cat-file -e failing is an answer, not an error. Other failures go to the log for diagnosis.
    if proc.returncode != 0 and args[0] != "cat-file":
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


def main():
    # type: () -> int
    env = os.environ
    head = env.get("GITHUB_SHA", "")
    if not _SHA.fullmatch(head):
        head = "HEAD"
    result = decide(
        env.get("GITHUB_EVENT_NAME", ""),
        _read_event(env.get("GITHUB_EVENT_PATH")),
        env.get("GITHUB_REF_TYPE", ""),
        _read_text(env.get("CI_SCOPE_RULES") or DEFAULT_RULES),
        head,
        run_git,
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
