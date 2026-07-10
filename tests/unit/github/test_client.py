#!/usr/bin/env python3
"""Tests for hephaestus.github.client.gh_call public contract."""

import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import hephaestus.github.client as client_module
from hephaestus.github.client import (
    _GH_BREAKER,
    _NON_TRANSIENT_PATTERNS,
    _PER_TARGET_PATTERNS,
    ClaudeUsageCapError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    _is_per_target_error,
    _is_service_failure,
    gh_call,
    gh_cli_timeout,
)


@pytest.fixture(autouse=True)
def _reset_breaker() -> Generator[None, None, None]:
    """Reset the GitHub API circuit breaker before each test."""
    _GH_BREAKER.reset()
    yield
    _GH_BREAKER.reset()


def _gh_error(stderr: str) -> subprocess.CalledProcessError:
    """Build a CalledProcessError shaped like a failed ``gh`` invocation."""
    return subprocess.CalledProcessError(1, ["gh"], output="", stderr=stderr)


class TestBreakerPredicateWiring:
    """The ``ignore`` predicate must be defined before ``_GH_BREAKER`` uses it."""

    def test_module_imports_in_a_fresh_interpreter(self) -> None:
        """``ignore=`` is evaluated at import: a forward reference is a NameError.

        No in-process test can catch this — importing this test module already
        imported ``client``. A lint autofix once rewrote
        ``ignore=lambda exc: _breaker_should_ignore(exc)`` into a direct
        reference while the predicate was still defined ~130 lines BELOW
        ``_GH_BREAKER``, making the module unimportable and failing every test
        job on every Python version.
        """
        proc = subprocess.run(
            [sys.executable, "-c", "import hephaestus.github.client"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr

    def test_predicate_is_defined_before_the_breaker(self) -> None:
        """Guard the ordering invariant directly, with a readable failure."""
        source = Path(client_module.__file__).read_text(encoding="utf-8")
        predicate_def = source.index("def _breaker_should_ignore")
        breaker_use = source.index("_GH_BREAKER = get_circuit_breaker")
        assert predicate_def < breaker_use, (
            "_breaker_should_ignore must be defined before _GH_BREAKER references it"
        )

    def test_breaker_actually_uses_the_predicate(self) -> None:
        """The live breaker carries the ignore predicate, not a stale None."""
        assert _GH_BREAKER._ignore is not None
        assert _GH_BREAKER._ignore(_gh_error("gh: Not Found (HTTP 404)")) is True
        assert _GH_BREAKER._ignore(_gh_error("gh: HTTP 401: Bad credentials")) is False


class TestPerTargetPatternInvariants:
    """``_PER_TARGET_PATTERNS`` must stay a strict subset of the non-transient set."""

    def test_every_per_target_pattern_is_also_non_transient(self) -> None:
        """A per-target error must already be non-transient, else it gets retried.

        The two lists answer different questions — "should we retry?" and "does
        this indicate an outage?" — but per-target implies non-transient. If a
        pattern lands only in the per-target list it would be retried 6x AND
        excluded from the breaker: the worst of both.
        """
        non_transient = {p.pattern for p in _NON_TRANSIENT_PATTERNS}
        per_target = {p.pattern for p in _PER_TARGET_PATTERNS}
        assert per_target <= non_transient, per_target - non_transient

    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: HTTP 401: Bad credentials",
            "gh: HTTP 403: Forbidden",
            "gh: HTTP 422: Unprocessable Entity",
            "gh: HTTP 400: Bad Request",
            "Resource not accessible by personal access token",
        ],
    )
    def test_credential_and_request_errors_are_not_per_target(self, stderr: str) -> None:
        """These recur on every later call, so they must still open the breaker."""
        assert not _is_per_target_error(stderr)
        assert _is_service_failure(_gh_error(stderr))

    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: Could not resolve to an Issue with the number of 188.",
            "gh: Not Found (HTTP 404)",
            "no checks reported on the 'main' branch",
            "Body is not editable",
        ],
    )
    def test_target_errors_are_not_service_failures(self, stderr: str) -> None:
        assert _is_per_target_error(stderr)
        assert not _is_service_failure(_gh_error(stderr))

    def test_non_calledprocess_exceptions_are_service_failures(self) -> None:
        """A ConnectionError carries no stderr — assume the service is down."""
        assert _is_service_failure(ConnectionError("reset by peer"))
        assert _is_service_failure(TimeoutError())


class TestBreakerIgnoresPerTargetErrors:
    """Deterministic per-target errors must not open the breaker (#2048).

    Regression for the #1795 cascade: six wrong-repo ``Could not resolve`` 404s
    opened the ``github-api`` breaker (``failure_threshold=5``), poisoning 236
    items across 9 repos and aborting the run with ``agent_jobs=0``.

    A 404 proves the service is UP and answering correctly about a target that
    does not exist. An auth failure (401/403) proves every later call will fail
    too, so that must still open the breaker.
    """

    PER_TARGET: tuple[str, ...] = (
        "gh: Could not resolve to an Issue with the number of 188.",
        "gh: Not Found (HTTP 404)",
        "no checks reported on the 'main' branch",
        "Body is not editable",
    )

    @pytest.mark.parametrize("stderr", PER_TARGET)
    @patch("hephaestus.github.client._gh_call_impl")
    def test_per_target_errors_never_open_breaker(self, mock_impl: Mock, stderr: str) -> None:
        """Well past failure_threshold=5, the breaker stays closed."""
        mock_impl.side_effect = _gh_error(stderr)

        for _ in range(10):
            with pytest.raises(subprocess.CalledProcessError):
                gh_call(["api", "graphql"], log_on_error=False)

        # The 11th call must still reach gh, not be short-circuited.
        with pytest.raises(subprocess.CalledProcessError):
            gh_call(["api", "graphql"], log_on_error=False)
        assert mock_impl.call_count == 11

    @patch("hephaestus.github.client._gh_call_impl")
    def test_swallowed_404s_do_not_poison_a_later_unrelated_call(self, mock_impl: Mock) -> None:
        """The exact #1795 shape: caller catches each 404; a later call still succeeds.

        Catching the exception never prevented the breaker from counting it,
        because the breaker records the failure before the caller's ``except``
        runs. This is what turned 6 bad lookups into a whole-run abort.
        """
        ok = Mock(returncode=0, stdout="{}", stderr="")
        mock_impl.side_effect = [
            *[_gh_error("gh: Could not resolve to an Issue with the number of 188.")] * 6,
            ok,
        ]

        for _ in range(6):
            with pytest.raises(subprocess.CalledProcessError):
                gh_call(["api", "graphql"], log_on_error=False)

        assert gh_call(["api", "graphql"]) is ok  # would raise GitHubUnavailableError before

    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: HTTP 401: Bad credentials",
            "gh: HTTP 403: Forbidden",
        ],
    )
    @patch("hephaestus.github.client._gh_call_impl")
    def test_auth_errors_still_open_breaker(self, mock_impl: Mock, stderr: str) -> None:
        """401/403 are per-credential, not per-target: every later call fails too."""
        mock_impl.side_effect = _gh_error(stderr)

        for _ in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                gh_call(["api", "graphql"], log_on_error=False)

        with pytest.raises(GitHubUnavailableError):
            gh_call(["api", "graphql"], log_on_error=False)

    @patch("hephaestus.github.client._gh_call_impl")
    def test_transient_failures_still_open_breaker(self, mock_impl: Mock) -> None:
        """A genuinely unavailable service must still trip the breaker."""
        mock_impl.side_effect = ConnectionError("connection reset by peer")

        for _ in range(5):
            with pytest.raises(ConnectionError):
                gh_call(["api", "graphql"], log_on_error=False)

        with pytest.raises(GitHubUnavailableError):
            gh_call(["api", "graphql"], log_on_error=False)


class TestGhCallCircuitBreaker:
    """Test CircuitBreaker wrapping of gh_call."""

    @patch("hephaestus.github.client._gh_call_impl")
    def test_breaker_transitions_to_open_on_failures(self, mock_impl: Mock) -> None:
        """Circuit breaker transitions from CLOSED to OPEN after 5 consecutive failures."""
        # Simulate 5xx failures
        mock_impl.side_effect = subprocess.CalledProcessError(
            500, "gh", stderr="Internal Server Error"
        )

        # First 5 calls should fail with CalledProcessError
        for _i in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                gh_call(["issue", "list"])

        # Verify the mock was called 5 times
        assert mock_impl.call_count == 5

        # 6th call should fail with GitHubUnavailableError (circuit now OPEN)
        with pytest.raises(GitHubUnavailableError):
            gh_call(["issue", "list"])

        # Circuit is open: mock should NOT be called again (fail-fast)
        assert mock_impl.call_count == 5

    @patch("hephaestus.github.client._gh_call_impl")
    def test_circuit_breaker_open_error_is_runtime_error(self, mock_impl: Mock) -> None:
        """GitHubUnavailableError is a RuntimeError subclass."""
        mock_impl.side_effect = subprocess.CalledProcessError(
            500, "gh", stderr="Internal Server Error"
        )

        # Trigger 5 failures to open the breaker
        for _ in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                gh_call(["issue", "list"])

        # The error raised when breaker is open should be a RuntimeError
        with pytest.raises(RuntimeError):
            gh_call(["issue", "list"])

        # And specifically a GitHubUnavailableError
        with pytest.raises(GitHubUnavailableError):
            gh_call(["issue", "list"])


class TestGhCallRateLimit:
    """Test rate limit handling in gh_call."""

    @patch("hephaestus.github.client._gh_call_impl")
    def test_propagates_rate_limit_error(self, mock_impl: Mock) -> None:
        """gh_call propagates GitHubRateLimitError with reset_epoch."""
        mock_impl.side_effect = GitHubRateLimitError("rate limited", reset_epoch=1234)
        with pytest.raises(GitHubRateLimitError) as exc_info:
            gh_call(["api", "/repos/owner/repo"])
        assert exc_info.value.reset_epoch == 1234


class TestGhCallClaudeCap:
    """Test Claude usage cap handling in gh_call."""

    @patch("hephaestus.github.client._gh_call_impl")
    def test_propagates_claude_cap(self, mock_impl: Mock) -> None:
        """gh_call propagates ClaudeUsageCapError with reset_epoch."""
        mock_impl.side_effect = ClaudeUsageCapError("cap exceeded", reset_epoch=5678)
        with pytest.raises(ClaudeUsageCapError) as exc_info:
            gh_call(["api", "/x"])
        assert exc_info.value.reset_epoch == 5678


class TestGhCliTimeout:
    """Test gh_cli_timeout configuration."""

    def test_gh_cli_timeout_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh_cli_timeout returns 120 by default."""
        monkeypatch.delenv("HEPH_GH_TIMEOUT", raising=False)
        assert gh_cli_timeout() == 120

    def test_gh_cli_timeout_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh_cli_timeout respects HEPH_GH_TIMEOUT environment variable."""
        monkeypatch.setenv("HEPH_GH_TIMEOUT", "60")
        assert gh_cli_timeout() == 60

    def test_gh_cli_timeout_invalid_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh_cli_timeout falls back to 120 on non-integer HEPH_GH_TIMEOUT."""
        monkeypatch.setenv("HEPH_GH_TIMEOUT", "not_a_number")
        assert gh_cli_timeout() == 120


class TestGhCallPublicExports:
    """Test that gh_call is properly exported from hephaestus.github."""

    def test_gh_call_exported_from_package(self) -> None:
        """gh_call is exported from hephaestus.github.__init__."""
        import hephaestus.github as github_pkg

        assert hasattr(github_pkg, "gh_call")
        assert github_pkg.gh_call is gh_call

    def test_error_classes_exported_from_package(self) -> None:
        """Error classes are exported from hephaestus.github.__init__."""
        import hephaestus.github as github_pkg

        assert hasattr(github_pkg, "GitHubRateLimitError")
        assert hasattr(github_pkg, "GitHubUnavailableError")
        assert hasattr(github_pkg, "ClaudeUsageCapError")
        assert github_pkg.GitHubRateLimitError is GitHubRateLimitError
        assert github_pkg.GitHubUnavailableError is GitHubUnavailableError
        assert github_pkg.ClaudeUsageCapError is ClaudeUsageCapError


class TestNonTransientErrorClassification:
    """_is_non_transient_error: deterministic gh failures must not be retried."""

    def test_body_not_editable_is_non_transient(self) -> None:
        """#1327: editing a foreign-owned comment never succeeds on retry.

        Without this classification the deterministic "Body is not editable"
        rejection was retried ~6× per finding (66× in one observed run) before
        the caller could fall back to posting its own editable comment.
        """
        from hephaestus.github.client import _is_non_transient_error

        assert _is_non_transient_error("gh: Body is not editable") is True

    def test_no_checks_reported_is_non_transient(self) -> None:
        """#1587: 'no checks reported' is the expected post-push empty state.

        It exits non-zero but never succeeds on retry; classifying it
        non-transient makes gh_pr_checks fail fast and convert it to [] without
        burning exponential backoff or logging spurious ERRORs.
        """
        from hephaestus.github.client import _is_non_transient_error

        assert _is_non_transient_error("no checks reported on the '45-foo' branch") is True

    def test_transient_5xx_is_not_non_transient(self) -> None:
        """A 500 is retryable, so it must NOT be flagged non-transient."""
        from hephaestus.github.client import _is_non_transient_error

        assert _is_non_transient_error("Internal Server Error (HTTP 500)") is False

    def test_graphql_syntax_error_is_non_transient(self) -> None:
        """#1350: a malformed GraphQL query is a parse error, never retryable.

        A stray ``repr()`` once emitted single-quoted string literals
        (``owner:'H...'``), which gh rejected with
        ``Expected VALUE, actual: UNKNOWN_CHAR``. Such syntax errors can never
        succeed on retry, so they must fail fast instead of being retried ~6×.
        """
        from hephaestus.github.client import _is_non_transient_error

        assert (
            _is_non_transient_error(
                'gh: Expected VALUE, actual: UNKNOWN_CHAR ("H") at [1, 24]',
            )
            is True
        )
