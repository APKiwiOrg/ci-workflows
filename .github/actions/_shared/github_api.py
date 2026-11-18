"""The one way the CI actions read the GitHub REST API, plus the time parser both of them need.

Each action script puts this folder on sys.path from its own location. An action runs from
$GITHUB_ACTION_PATH, and the runner downloads the whole repository at the pinned commit, so this folder
always sits beside the action's own. Python 3.8 or later, standard library only.
"""
import datetime
import http.client
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

DEFAULT_API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 20
TOKEN_ENV = "CI_API_TOKEN"

_TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?Z")

# A fetch takes an API path such as /repos/OWNER/NAME/actions/artifacts and returns the decoded JSON
# object, or raises ApiError.
Fetch = Callable[[str], Dict[str, Any]]


class ApiError(Exception):
    """Any failure to read one API answer: HTTP status, network, timeout or a body that is not an object.
    The message never holds the token."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def make_fetch(token, base_url=None, open_url=urllib.request.urlopen):
    # type: (str, Optional[str], Callable[..., Any]) -> Fetch
    base = (base_url or DEFAULT_API_URL).rstrip("/")

    def fetch(path):
        # type: (str) -> Dict[str, Any]
        request = urllib.request.Request(base + path, method="GET")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if token:
            # Unredirected, so a redirect to another host never carries the token there.
            request.add_unredirected_header("Authorization", "Bearer " + token)
        try:
            with open_url(request, timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None)
                body = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise ApiError("HTTP %d for %s" % (status, path), retryable=status == 429 or 500 <= status < 600)
        except (OSError, http.client.HTTPException) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            retryable = (isinstance(cause, (OSError, http.client.IncompleteRead))
                         and not isinstance(cause, ssl.SSLCertVerificationError))
            raise ApiError("%s for %s" % (type(error).__name__, path), retryable=retryable)
        answer_label = "answer for %s" % path
        if status is not None:
            answer_label += " (HTTP %s)" % status
        try:
            answer = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise ApiError("%s is not JSON" % answer_label)
        if not isinstance(answer, dict):
            raise ApiError("%s is not a JSON object" % answer_label)
        return answer

    return fetch


def fetch_from_env(env=None):
    # type: (Optional[Dict[str, str]]) -> Fetch
    env = os.environ if env is None else env
    return make_fetch(env.get(TOKEN_ENV, ""), env.get("GITHUB_API_URL") or DEFAULT_API_URL)


def parse_time(text):
    # type: (Any) -> Optional[datetime.datetime]
    """An API time such as 2026-01-19T10:00:00Z as an aware UTC datetime, or None for anything else.
    datetime.fromisoformat cannot read the trailing Z before Python 3.11."""
    if not isinstance(text, str):
        return None
    match = _TIME.fullmatch(text)
    if not match:
        return None
    try:
        return datetime.datetime(*(int(part) for part in match.groups()), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
