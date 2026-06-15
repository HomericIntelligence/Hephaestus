"""Shared gh CLI adapter with circuit breaker, rate-limit retry, and throttle.

Public contract
---------------
``gh_call(args, *, check=True, retry_on_rate_limit=True, max_retries=6)``
   Run ``gh <args>``. Every invocation passes through the ``github-api``
   circuit breaker, the per-thread throttle (``GH_RATE_LIMIT_PER_SEC``),
   REST + GraphQL rate-limit detection (waits until reset), and Claude
   per-period usage-cap interception.

Bare ``subprocess.run(["gh", ...])`` calls bypass the breaker and are a
reliability bug — route them through ``gh_call``.

Raises:
    GitHubRateLimitError, GitHubUnavailableError, ClaudeUsageCapError,
    subprocess.CalledProcessError.

"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time

from hephaestus.github.rate_limit import (
    detect_claude_usage_cap,
    detect_claude_usage_limit,
    detect_rate_limit,
    detect_secondary_rate_limit,
    gh_global_throttle_acquire,
    wait_until,
)
from hephaestus.resilience.circuit_breaker import (
    CircuitBreakerOpenError,
    get_circuit_breaker,
)
from hephaestus.utils.helpers import run_subprocess

logger = logging.getLogger(__name__)


def gh_cli_timeout() -> int:
    """Timeout for individual ``gh`` CLI calls (default 120s, env HEPH_GH_TIMEOUT)."""
    raw = os.environ.get("HEPH_GH_TIMEOUT")
    if raw is None:
        return 120
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer HEPH_GH_TIMEOUT=%r — using default 120s", raw)
        return 120


_GH_THROTTLE = threading.local()
_GH_BREAKER = get_circuit_breaker(
    "github-api",
    failure_threshold=5,
    recovery_timeout=60,
    half_open_max_calls=2,
)


class GitHubRateLimitError(RuntimeError):
    """Raised when GitHub reports the API rate limit has been exceeded.

    Subclasses :class:`RuntimeError` so existing ``except RuntimeError``
    handlers continue to catch it; callers that want rate-limit-specific
    handling (e.g. exit cleanly instead of aborting a batch) should catch
    this class explicitly.

    Attributes:
        reset_epoch: Unix timestamp at which the relevant rate-limit
            window resets, or ``0`` if the reset time could not be
            determined.

    """

    def __init__(self, message: str, reset_epoch: int = 0) -> None:
        """Initialise the error with an optional reset epoch.

        Args:
            message: Human-readable error description, typically the
                upstream GitHub message.
            reset_epoch: Unix timestamp at which the limit resets, or
                ``0`` if unknown.

        """
        super().__init__(message)
        self.reset_epoch: int = reset_epoch


class GitHubUnavailableError(RuntimeError):
    """Circuit breaker is open due to sustained GitHub unavailability."""

    pass


class ClaudeUsageCapError(RuntimeError):
    """Raised when the Claude CLI reports that the per-period usage cap has been hit.

    Subclasses :class:`RuntimeError` so that existing ``except RuntimeError``
    handlers continue to catch it.

    Attributes:
        reset_epoch: Unix timestamp at which the cap resets, or ``None`` if the
            reset time could not be determined.

    """

    def __init__(self, message: str, reset_epoch: int | None = None) -> None:
        """Initialise the error with an optional reset epoch.

        Args:
            message: Human-readable error description.
            reset_epoch: Unix timestamp at which the cap resets, or ``None``.

        """
        super().__init__(message)
        self.reset_epoch: int | None = reset_epoch


def _gh_throttle_wait() -> None:
    rate = float(os.environ.get("GH_RATE_LIMIT_PER_SEC", "5"))
    if rate <= 0:
        return
    min_interval = 1.0 / rate
    last = getattr(_GH_THROTTLE, "last_call", 0.0)
    now = time.monotonic()
    elapsed = now - last
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _GH_THROTTLE.last_call = time.monotonic()


# GraphQL emits "Resource not accessible by …" with HTTP 200 when the token
# is valid but lacks scope for the mutation (e.g. addComment outside the PAT's
# allowed orgs). None of the HTTP-status patterns above match it, so without
# this entry the call gets retried and dumps the full body on every attempt.
_TOKEN_SCOPE_PATTERN = re.compile(
    r"resource not accessible by (personal access token|integration)",
    re.IGNORECASE,
)

_NON_TRANSIENT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:^|\s)403(?:\s|$)|forbidden|permission denied",
        r"(?:^|\s)404(?:\s|$)|not found",
        r"(?:^|\s)400(?:\s|$)|bad request",
        r"(?:^|\s)401(?:\s|$)|unauthorized",
        r"(?:^|\s)422(?:\s|$)|unprocessable entity",
        r"invalid argument",
        r"unknown json field",
        # GraphQL schema errors are deterministic — a bad mutation/field or an
        # unused variable can never succeed on retry (#1040).
        r"doesn't accept argument",
        r"is declared by .* but not used",
        # A malformed GraphQL query (e.g. a single-quoted string literal from a
        # stray repr) is a syntax error that can never parse on retry. gh surfaces
        # these as "Expected VALUE, actual: UNKNOWN_CHAR" / "Parse error" (#1350).
        r"Expected VALUE",
        r"UNKNOWN_CHAR",
        r"Parse error",
        # "Body is not editable": editing a review comment owned by another
        # app/account (Copilot, CodeQL) is forbidden and never succeeds on
        # retry. Fail fast so the caller posts its own editable comment (#1327).
        r"not editable",
    )
]
_NON_TRANSIENT_PATTERNS.append(_TOKEN_SCOPE_PATTERN)


def _is_token_scope_error(stderr: str) -> bool:
    return bool(_TOKEN_SCOPE_PATTERN.search(stderr))


def _is_non_transient_error(stderr: str) -> bool:
    return any(p.search(stderr) for p in _NON_TRANSIENT_PATTERNS)


def _raise_if_claude_usage(stderr: str, cause: subprocess.CalledProcessError) -> None:
    """Convert Claude usage-cap/usage-limit stderr into ClaudeUsageCapError.

    Returns silently when *stderr* matches neither pattern. Hoisted out of
    :func:`_gh_call_impl` to keep its cyclomatic complexity under the linter cap.
    """
    reset_epoch = detect_claude_usage_cap(stderr)
    if reset_epoch is not None:
        raise ClaudeUsageCapError(
            f"Claude API usage cap reached. Resets at epoch {reset_epoch}.",
            reset_epoch=reset_epoch,
        ) from cause
    if detect_claude_usage_limit(stderr):
        raise ClaudeUsageCapError(
            "Claude API usage limit reached. Please check your billing.",
            reset_epoch=None,
        ) from cause


def _log_token_scope_remediation(args: list[str], stderr: str) -> None:
    """Log a one-shot, actionable remediation block for token-scope failures.

    Fires from the non-transient branch in :func:`_gh_call_impl` so it logs exactly
    once per failed call (no retry spam). The message names the gh subcommand
    that failed, the required token scopes, and the GITHUB_TOKEN=-blanking
    workaround for the common case where a low-scope env token shadows gh's
    stored credentials.
    """
    subcommand = " ".join(args[:2]) if args else "<unknown>"
    logger.error(
        "Cannot run `gh %s`: GitHub token lacks required scopes.\n"
        "\n"
        "  Required scopes for this script:\n"
        "    - Classic PAT:   repo  (full)             — covers issue:write + pr:write\n"
        "    - Fine-grained:  Issues:        Read & Write\n"
        "                     Pull requests: Read & Write\n"
        "                     Contents:      Read & Write   (if pushes are needed)\n"
        "\n"
        "  How to fix:\n"
        "    1. Check which token gh is using:  gh auth status\n"
        "    2. If GITHUB_TOKEN is set in your env, it overrides gh's stored creds.\n"
        "       Either:\n"
        "         a) unset GITHUB_TOKEN  (lets gh use its own login), or\n"
        "         b) regenerate the PAT with the scopes above:\n"
        "            https://github.com/settings/tokens\n"
        "    3. Re-run with:  GITHUB_TOKEN= <your-command>\n"
        "       (the leading `GITHUB_TOKEN=` blanks the env var for one command)\n"
        "\n"
        "  Original error: %s",
        subcommand,
        stderr.strip()[:200],
    )


def _extract_reset_epoch(e: subprocess.CalledProcessError) -> int | None:
    """Return a rate-limit reset epoch parsed from a failed ``gh`` invocation.

    Inspects stderr first (REST CLI message form) and falls back to stdout
    because GraphQL rate-limit errors arrive in the JSON payload that gh
    streams to stdout. Returns ``None`` if the failure is not rate-limit-
    related.
    """
    stderr = e.stderr if e.stderr else ""
    epoch = detect_rate_limit(stderr)
    if epoch is None and e.stdout:
        epoch = detect_rate_limit(e.stdout)
    return epoch


def _handle_rate_limit_attempt(
    *,
    reset_epoch: int,
    attempt: int,
    max_retries: int,
    retry_on_rate_limit: bool,
    cause: BaseException,
    base_wait_seconds: int = 60,
) -> None:
    """Wait for a rate-limit reset, or raise :class:`GitHubRateLimitError`.

    Centralises the "we got rate-limited; should we retry?" decision so the
    two except-blocks in :func:`_gh_call_impl` share identical behavior. Raises
    immediately if retries are disabled or exhausted; otherwise sleeps and
    returns so the caller can ``continue`` the retry loop.

    Args:
        reset_epoch: Unix timestamp when the rate limit resets, or ``0`` when
            unknown (no epoch embedded in the error message).
        attempt: Current attempt index (0-based).
        max_retries: Total retry budget.
        retry_on_rate_limit: If False, raise immediately instead of waiting.
        cause: The exception that triggered this call.
        base_wait_seconds: Initial wait duration (seconds) when ``reset_epoch``
            is ``0`` (no epoch available).  Doubles each attempt, capped at
            300s.  Defaults to 60; pass 15 for secondary rate limits which
            carry no reset epoch but typically clear faster.

    """
    if not retry_on_rate_limit or attempt == max_retries - 1:
        raise GitHubRateLimitError(
            f"GitHub API rate limit reached. Reset at epoch {reset_epoch}",
            reset_epoch=reset_epoch,
        ) from cause
    if reset_epoch > 0:
        wait_until(reset_epoch)
        return
    wait_seconds = min(base_wait_seconds * (2**attempt), 300)  # cap at 5 minutes
    logger.warning("Rate limited but no reset time, waiting %ss", wait_seconds)
    time.sleep(wait_seconds)


def _gh_call_impl(
    args: list[str],
    check: bool = True,
    retry_on_rate_limit: bool = True,
    max_retries: int = 6,
) -> subprocess.CompletedProcess[str]:
    """Implement gh CLI call with rate limit handling (circuit breaker will wrap this).

    Args:
        args: Arguments to pass to gh
        check: Whether to raise on non-zero exit
        retry_on_rate_limit: Whether to retry on rate limit
        max_retries: Maximum retry attempts

    Returns:
        CompletedProcess instance

    Raises:
        subprocess.CalledProcessError: If command fails and check=True
        ClaudeUsageCapError: If a Claude per-period usage cap is detected.
        RuntimeError: For other non-transient or exhausted-retry failures.

    """
    for attempt in range(max_retries):
        try:
            gh_global_throttle_acquire()
            _gh_throttle_wait()
            result = run_subprocess(
                ["gh", *args],
                check=check,
                timeout=gh_cli_timeout(),
            )
            return result
        except subprocess.CalledProcessError as e:
            stderr = e.stderr if e.stderr else ""
            _raise_if_claude_usage(stderr, e)

            reset_epoch = _extract_reset_epoch(e)
            if reset_epoch is not None:
                _handle_rate_limit_attempt(
                    reset_epoch=reset_epoch,
                    attempt=attempt,
                    max_retries=max_retries,
                    retry_on_rate_limit=retry_on_rate_limit,
                    cause=e,
                )
                continue

            if _is_non_transient_error(stderr):
                logger.error("Non-transient error detected: %s", stderr[:200])
                if _is_token_scope_error(stderr):
                    _log_token_scope_remediation(args, stderr)
                raise

            # Secondary rate limit carries no reset epoch — check both stderr
            # and stdout (GraphQL errors can land on stdout).
            stdout = e.stdout if e.stdout else ""
            if detect_secondary_rate_limit(stderr) or detect_secondary_rate_limit(stdout):
                logger.warning(
                    "GitHub secondary rate limit hit (attempt %s), waiting before retry",
                    attempt + 1,
                )
                _handle_rate_limit_attempt(
                    reset_epoch=0,
                    attempt=attempt,
                    max_retries=max_retries,
                    retry_on_rate_limit=retry_on_rate_limit,
                    cause=e,
                    base_wait_seconds=15,
                )
                continue

            if attempt == max_retries - 1:
                raise

            wait_seconds = 2**attempt
            logger.warning(
                "gh call failed (attempt %s), retrying in %ss", attempt + 1, wait_seconds
            )
            time.sleep(wait_seconds)
        except GitHubRateLimitError as e:
            # Raised from inside _check_graphql_errors when the JSON payload
            # carries a RATE_LIMITED entry (HTTP 200, gh exits 0).
            _handle_rate_limit_attempt(
                reset_epoch=e.reset_epoch,
                attempt=attempt,
                max_retries=max_retries,
                retry_on_rate_limit=retry_on_rate_limit,
                cause=e,
            )
            continue

    # Should not reach here, but satisfy type checker
    raise RuntimeError("gh call failed after all retries")


def _gh_call(
    args: list[str],
    check: bool = True,
    retry_on_rate_limit: bool = True,
    max_retries: int = 6,
) -> subprocess.CompletedProcess[str]:
    """Call gh CLI with rate limit handling and circuit breaker protection.

    Wraps the implementation in a circuit breaker that opens after sustained
    failures, causing fail-fast with GitHubUnavailableError instead of
    exhausting per-call-site retry budgets.

    Args:
        args: Arguments to pass to gh
        check: Whether to raise on non-zero exit
        retry_on_rate_limit: Whether to retry on rate limit
        max_retries: Maximum retry attempts

    Returns:
        CompletedProcess instance

    Raises:
        subprocess.CalledProcessError: If command fails and check=True
        ClaudeUsageCapError: If a Claude per-period usage cap is detected.
        GitHubUnavailableError: If the circuit breaker is open due to
            sustained GitHub API unavailability.
        RuntimeError: For other non-transient or exhausted-retry failures.

    """
    try:
        return _GH_BREAKER.call(
            _gh_call_impl,
            args,
            check=check,
            retry_on_rate_limit=retry_on_rate_limit,
            max_retries=max_retries,
        )
    except CircuitBreakerOpenError as exc:
        # Translate to a domain exception (RuntimeError subclass) so existing
        # exception handlers that catch RuntimeError/Exception continue to work.
        raise GitHubUnavailableError(
            "GitHub API circuit breaker is open due to sustained unavailability"
        ) from exc


gh_call = _gh_call

__all__ = [
    "ClaudeUsageCapError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "gh_call",
    "gh_cli_timeout",
]
