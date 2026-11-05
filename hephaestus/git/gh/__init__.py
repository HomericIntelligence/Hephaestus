"""The ``gh`` command-line adapter for Hephaestus.

Public adapter contract
-----------------------
``gh_call`` is the canonical entry point for invoking the ``gh`` CLI
anywhere in hephaestus. It wraps every call in the ``github-api`` circuit
breaker (opens after 5 sustained failures, fail-fast for 60s), enforces
global throttling, detects REST and
GraphQL rate limits and waits until reset, and translates Claude
per-period usage caps into ``ClaudeUsageCapError``.

Bare ``subprocess.run(["gh", ...])`` calls bypass the breaker and are a
reliability bug — route them through ``gh_call``.

Exception hierarchy::

    RuntimeError
    ├── GitHubRateLimitError      (rate limit; carries reset_epoch)
    ├── GitHubUnavailableError    (breaker open; sustained outage)
    └── ClaudeUsageCapError       (Claude per-period cap)

Out of scope for ``gh_call``: ``tidy``'s interactive ``gh tidy`` Popen
requires direct stdin/stdout access (it stays in ``hephaestus.github.tidy``
until W4), and ``rate_limit``'s reset probe calls
``run_subprocess(["gh", "api", "rate_limit"])`` directly (bypassing
``gh_call``) to avoid recursing back into ``gh_call`` while ``gh_call`` is
classifying a rate-limit error.
"""

from hephaestus.git.gh.client import (
    ClaudeUsageCapError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    gh_call,
)
from hephaestus.git.gh.rate_limit import (
    detect_claude_usage_cap,
    detect_claude_usage_limit,
    detect_rate_limit,
    detect_session_limit,
    parse_reset_epoch,
    resolve_quota_reset_epoch,
    wait_until,
)

__all__ = [
    "ClaudeUsageCapError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "detect_claude_usage_cap",
    "detect_claude_usage_limit",
    "detect_rate_limit",
    "detect_session_limit",
    "gh_call",
    "parse_reset_epoch",
    "resolve_quota_reset_epoch",
    "wait_until",
]
