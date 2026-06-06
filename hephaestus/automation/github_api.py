"""GitHub API utilities using gh CLI.

Provides:
- Issue data fetching with caching
- Rate-limited API calls
- Batch operations with GraphQL
- Secure file writing
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast

from hephaestus.github.rate_limit import (
    detect_claude_usage_cap,
    detect_claude_usage_limit,
    detect_rate_limit,
    gh_global_throttle_acquire,
    gh_rate_limit_reset_epoch,
    wait_until,
)
from hephaestus.io.utils import write_secure as io_write_secure
from hephaestus.resilience.circuit_breaker import CircuitBreakerOpenError, get_circuit_breaker

from .claude_timeouts import gh_cli_timeout
from .git_utils import get_repo_info, run
from .models import IssueInfo, IssueState

logger = logging.getLogger(__name__)

_label_cache: set[str] | None = None

# Per-thread proactive throttle for `gh` invocations. Default 5 calls/sec
# per worker thread; the GH_RATE_LIMIT_PER_SEC env var overrides for ops
# tuning, and 0 disables. With max-workers=3 the aggregate is ~15/sec,
# well below GitHub's per-token REST limits and tame enough that GH
# secondary rate limits stay quiet during phase bursts (e.g. a planner
# fetching N issue bodies back-to-back).
_GH_THROTTLE = threading.local()

# Circuit breaker for gh API calls. When a sustained GitHub outage occurs
# (5+ consecutive failures), the breaker opens and subsequent calls fail fast
# with GitHubUnavailableError instead of exhausting retry budgets at each
# call site. half_open_max_calls=2 (not the default 1) allows a pair of
# recovery attempts before fully closing.
_GH_BREAKER = get_circuit_breaker(
    "github-api",
    failure_threshold=5,
    recovery_timeout=60,
    half_open_max_calls=2,
)


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


class GitHubUnavailableError(RuntimeError):
    """Raised when the GitHub API is unavailable due to a circuit breaker opening.

    Subclasses :class:`RuntimeError` so that existing ``except RuntimeError``
    handlers continue to catch it. Represents a condition where repeated failures
    have caused the circuit breaker to open, indicating sustained GitHub API
    unavailability.

    """

    pass


def gh_list_labels(refresh: bool = False, *, raise_on_error: bool = False) -> set[str]:
    """Return the set of label names that exist in the current repository.

    Args:
        refresh: If True, bypass the in-process cache and re-fetch.
        raise_on_error: If True, propagate label-list failures instead of
            returning an empty set.

    Returns:
        Set of existing label names.

    """
    global _label_cache
    if _label_cache is not None and not refresh:
        return _label_cache

    try:
        result = _gh_call(["label", "list", "--json", "name", "--limit", "200"])
        data = json.loads(result.stdout)
        _label_cache = {item["name"] for item in data}
        return _label_cache
    except Exception as e:
        logger.warning("Could not fetch label list: %s; proceeding without validation", e)
        if raise_on_error:
            raise RuntimeError("Could not fetch label list") from e
        return set()


def gh_create_label(name: str, color: str = "ededed", description: str = "") -> None:
    """Create a GitHub label, updating it if it already exists.

    Args:
        name: Label name
        color: Hex color without leading ``#`` (default: neutral grey)
        description: Optional short description

    """
    cmd = ["label", "create", name, "--color", color, "--force"]
    if description:
        cmd.extend(["--description", description])
    _gh_call(cmd)
    if _label_cache is not None:
        _label_cache.add(name)
    logger.info("Created missing label '%s'", name)


def gh_issue_add_labels(issue_number: int, labels: list[str]) -> None:
    """Add labels to an existing issue, auto-creating any that don't exist yet.

    Idempotent: applying a label the issue already has is a no-op from
    GitHub's perspective. Missing repo-level labels are created on demand via
    :func:`gh_create_label`, which is what the state-label rollout relies on
    (a repo that hasn't run ``hephaestus-ensure-state-labels`` yet will still
    work — the first reviewer pass creates the labels).

    Args:
        issue_number: Issue to label.
        labels: Label names to add. Empty list is a no-op.

    """
    if not labels:
        return
    existing = gh_list_labels()
    for label in labels:
        if label not in existing:
            gh_create_label(label)
    cmd = ["issue", "edit", str(issue_number)]
    for label in labels:
        cmd += ["--add-label", label]
    _gh_call(cmd)
    logger.info("Added labels %s to issue #%s", labels, issue_number)


def gh_issue_remove_labels(issue_number: int, labels: list[str]) -> None:
    """Remove labels from an existing issue.

    Tolerant of labels the issue does not actually carry, and of mutually
    exclusive state labels that have not been created in the repository yet.
    Used to keep the ``state:*`` family mutually-exclusive (apply one, remove
    the other two).

    Args:
        issue_number: Issue to modify.
        labels: Label names to remove. Empty list is a no-op.

    """
    if not labels:
        return
    try:
        existing = gh_list_labels(raise_on_error=True)
    except RuntimeError as exc:
        logger.warning(
            "Could not validate repo labels before removing from issue #%s; "
            "attempting requested removals without filtering: %s",
            issue_number,
            exc,
        )
        labels_to_remove = list(labels)
    else:
        labels_to_remove = [label for label in labels if label in existing]
        missing = sorted(set(labels) - existing)
        if missing:
            logger.debug(
                "Skipping removal of repo labels that do not exist for issue #%s: %s",
                issue_number,
                missing,
            )
    if not labels_to_remove:
        return
    cmd = ["issue", "edit", str(issue_number)]
    for label in labels_to_remove:
        cmd += ["--remove-label", label]
    _gh_call(cmd)
    logger.info("Removed labels %s from issue #%s", labels_to_remove, issue_number)


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
    :func:`_gh_call` to keep its cyclomatic complexity under the linter cap.
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


@contextlib.contextmanager
def _body_file(body: str):  # type: ignore[no-untyped-def]
    """Yield a path to a temporary file containing *body*, deleted on exit.

    Use with ``gh <subcmd> --body-file <path>`` instead of ``--body <body>`` so
    large bodies (e.g. multi-KB implementation plans) don't bloat error logs or
    risk hitting argv-size limits. The CLAUDE.md convention says temporary
    files belong under ``build/`` of the current repo; if that directory
    exists we use it, otherwise we fall back to the system tempdir.
    """
    build_dir = Path.cwd() / "build"
    tmp_dir = str(build_dir) if build_dir.is_dir() else None
    fd, path = tempfile.mkstemp(
        prefix="gh-body-",
        suffix=".md",
        dir=tmp_dir,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _log_token_scope_remediation(args: list[str], stderr: str) -> None:
    """Log a one-shot, actionable remediation block for token-scope failures.

    Fires from the non-transient branch in :func:`_gh_call` so it logs exactly
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
            result = run(
                ["gh", *args],
                check=check,
                capture_output=True,
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
) -> None:
    """Wait for a rate-limit reset, or raise :class:`GitHubRateLimitError`.

    Centralises the "we got rate-limited; should we retry?" decision so the
    two except-blocks in :func:`_gh_call` share identical behavior. Raises
    immediately if retries are disabled or exhausted; otherwise sleeps and
    returns so the caller can ``continue`` the retry loop.
    """
    if not retry_on_rate_limit or attempt == max_retries - 1:
        raise GitHubRateLimitError(
            f"GitHub API rate limit reached. Reset at epoch {reset_epoch}",
            reset_epoch=reset_epoch,
        ) from cause
    if reset_epoch > 0:
        wait_until(reset_epoch)
        return
    wait_seconds = min(60 * (2**attempt), 300)  # cap at 5 minutes
    logger.warning("Rate limited but no reset time, waiting %ss", wait_seconds)
    time.sleep(wait_seconds)


def _check_graphql_errors(data: dict[str, Any], context: str) -> None:
    """Raise RuntimeError if a GraphQL response carries an ``errors`` array.

    The GitHub GraphQL API returns HTTP 200 with ``{"errors": [...]}`` for
    permission, validation, and visibility failures. ``gh`` exits 0 in that
    case, so a plain exit-code check sees success while the operation has
    actually failed. This helper is the single place we surface those.

    ``context`` appears verbatim in the error message so logs identify which
    operation failed.
    """
    errors = data.get("errors")
    if not errors:
        return

    for err in errors:
        msg = err.get("message", "") if isinstance(err, dict) else ""
        err_type = err.get("type", "") if isinstance(err, dict) else ""
        if err_type == "RATE_LIMITED" or "rate limit" in msg.lower():
            reset = gh_rate_limit_reset_epoch() or 0
            raise GitHubRateLimitError(f"GraphQL {context} rate-limited: {msg}", reset_epoch=reset)

    raise RuntimeError(f"GraphQL {context} failed: {errors!r}")


def gh_issue_json(issue_number: int) -> dict[str, Any]:
    """Fetch issue data as JSON.

    Args:
        issue_number: GitHub issue number

    Returns:
        Issue data dictionary

    Raises:
        RuntimeError: If issue fetch fails

    """
    try:
        result = _gh_call(
            ["issue", "view", str(issue_number), "--json", "number,title,state,labels,body"],
        )
        return cast(dict[str, Any], json.loads(result.stdout))
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to fetch issue #{issue_number}: {e}") from e


def gh_issue_comment(issue_number: int, body: str) -> None:
    """Post a comment to an issue.

    Args:
        issue_number: GitHub issue number
        body: Comment body text

    Raises:
        RuntimeError: If comment post fails

    """
    try:
        with _body_file(body) as path:
            _gh_call(["issue", "comment", str(issue_number), "--body-file", path])
        logger.info("Posted comment to issue #%s", issue_number)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to post comment to issue #{issue_number}: {e}") from e


def _fetch_issue_comment_ids(issue_number: int) -> list[dict[str, Any]]:
    """Return up to 100 most-recent issue comments as ``{databaseId, body}`` dicts.

    Unlike :func:`_fetch_issue_comments_graphql` in ``review_state`` (which
    fetches ``body``/``url`` for verdict parsing), this also requests
    ``databaseId`` — the integer id required by the REST update/delete
    endpoints (``/repos/{o}/{r}/issues/comments/{id}``). Newest-first from
    GraphQL is reversed to chronological order so "last match wins" matches
    the rest of the pipeline.

    Returns an empty list on any failure (callers then fall back to create).
    """
    owner, name = get_repo_info()
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    issue(number:$number){"
        "      comments(last: 100, orderBy: {field: UPDATED_AT, direction: DESC}){"
        "        nodes{ databaseId body }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        result = _gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={issue_number}",
            ],
        )
        data = json.loads(result.stdout)
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("comments", {})
            .get("nodes", [])
        )
        return list(reversed(nodes))
    except Exception as exc:
        logger.warning("Failed to fetch comment ids for issue #%s: %s", issue_number, exc)
        return []


def gh_issue_delete_comment(comment_id: int) -> None:
    """Delete a single issue comment by its REST database id.

    Args:
        comment_id: The integer ``databaseId`` of the issue comment.

    Raises:
        RuntimeError: If the delete call fails.

    """
    owner, name = get_repo_info()
    try:
        _gh_call(
            [
                "api",
                "--method",
                "DELETE",
                f"/repos/{owner}/{name}/issues/comments/{comment_id}",
            ],
        )
        logger.info("Deleted issue comment %s", comment_id)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to delete issue comment {comment_id}: {e}") from e


def gh_issue_upsert_comment(issue_number: int, marker_prefix: str, body: str) -> int | None:
    """Create-or-update the single issue comment whose body starts with ``marker_prefix``.

    Enforces the "one comment per role" invariant: the pipeline must keep at
    most one PLAN comment (``# Implementation Plan``) and one REVIEW comment
    (``## 🔍 Plan Review``) per issue, updated in place rather than appended.

    Behaviour:
    - Find every existing comment whose body ``startswith(marker_prefix)``.
    - If none: create a new comment.
    - If one or more: PATCH the newest matching comment with ``body`` and
      DELETE the older duplicates (one-time convergence for issues that
      already accumulated multiple plan/review comments).

    Args:
        issue_number: GitHub issue number.
        marker_prefix: The role marker the comment body must start with.
        body: The full new comment body (should itself start with the marker).

    Returns:
        The ``databaseId`` of the upserted comment, or ``None`` if a fresh
        comment was created via ``gh issue comment`` (whose id we do not parse).

    Raises:
        RuntimeError: If a create/update/delete call fails.

    """
    comments = _fetch_issue_comment_ids(issue_number)
    matching = [
        c
        for c in comments
        if str(c.get("body", "")).startswith(marker_prefix) and c.get("databaseId") is not None
    ]

    if not matching:
        # No existing comment with this marker — create a fresh one.
        gh_issue_comment(issue_number, body)
        return None

    # Newest matching comment wins (list is chronological → last element).
    target = matching[-1]
    target_id = int(target["databaseId"])
    owner, name = get_repo_info()

    # Delete older duplicates so only one comment with this marker remains.
    for dup in matching[:-1]:
        dup_id = dup.get("databaseId")
        if dup_id is not None:
            gh_issue_delete_comment(int(dup_id))

    try:
        with _body_file(body) as path:
            _gh_call(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"/repos/{owner}/{name}/issues/comments/{target_id}",
                    "-F",
                    f"body=@{path}",
                ],
            )
        logger.info("Updated issue comment %s (marker %r)", target_id, marker_prefix)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to update issue comment {target_id} on #{issue_number}: {e}"
        ) from e
    return target_id


def _parse_issue_number(output: str) -> int:
    """Extract issue number from gh issue create output (URL or bare number)."""
    match = re.search(r"/issues/(\d+)", output)
    if match:
        return int(match.group(1))
    return int(output.split("/")[-1])


def _ensure_labels_exist(labels: list[str]) -> None:
    """Create any labels in *labels* that do not yet exist in the repository."""
    existing = gh_list_labels()
    for label in labels:
        if label not in existing:
            gh_create_label(label)


def gh_issue_create(title: str, body: str, labels: list[str] | None = None) -> int:
    """Create a new GitHub issue, auto-creating any missing labels.

    Args:
        title: Issue title
        body: Issue body/description
        labels: Optional list of label names to apply. Missing labels are
            created automatically before the issue is filed.

    Returns:
        Created issue number

    Raises:
        RuntimeError: If issue creation fails

    """
    try:
        if labels:
            _ensure_labels_exist(labels)

        with _body_file(body) as body_path:
            cmd = ["issue", "create", "--title", title, "--body-file", body_path]
            if labels:
                for label in labels:
                    cmd.extend(["--label", label])

            try:
                result = _gh_call(cmd)
            except subprocess.CalledProcessError as e:
                # On a label-not-found error (race or cache miss), create the label and retry once.
                stderr = e.stderr if e.stderr else ""
                m = re.search(
                    r"could not add label:\s*'([^']+)'\s*not found", stderr, re.IGNORECASE
                )
                if m and labels:
                    missing_label = m.group(1)
                    logger.warning(
                        "Label '%s' not found after pre-create; recreating and retrying",
                        missing_label,
                    )
                    gh_create_label(missing_label)
                    result = _gh_call(cmd)
                else:
                    raise

        output = result.stdout.strip()
        try:
            issue_number = _parse_issue_number(output)
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"Failed to parse issue number from output: {output}") from e

        logger.info("Created issue #%s", issue_number)
        return issue_number

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to create issue: {e}") from e


def gh_list_open_issues(limit: int = 500) -> list[int]:
    """Return issue numbers for all open issues in the current repository, ascending.

    Args:
        limit: Maximum number of issues to fetch (default 500).

    Returns:
        Sorted list of open issue numbers.

    Raises:
        RuntimeError: If fetching issues fails.

    """
    try:
        result = _gh_call(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number",
            ]
        )
        data = json.loads(result.stdout)
        return sorted(item["number"] for item in data)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"Failed to list open issues: {e}") from e


# Repo policy: every PR body must contain a literal "Closes #N" line on its own.
# Variants (Fixes, Resolves, lowercase, trailing colon) are rejected even though
# GitHub recognises them, because the CI gate and the reviewer prompt match this
# exact regex. Keep the regex in sync across:
#   - this module (creation-time gate)
#   - prompts.PR_REVIEW_ANALYSIS_PROMPT (review-time gate)
#   - .github/workflows/_required.yml job `pr-policy` (CI gate)
_CLOSES_LINE_RE = re.compile(r"^Closes #\d+\s*$", re.MULTILINE)


def _assert_body_has_closes(body: str) -> None:
    """Raise if *body* does not contain a 'Closes #N' line.

    Hard-fail rather than auto-fix: a missing closes line means the caller did
    not link a tracking issue, and silently appending one would just hide the
    bug. See repo PR policy.
    """
    if not _CLOSES_LINE_RE.search(body):
        raise ValueError(
            "PR body must contain a 'Closes #N' line per repo policy "
            "(must match ^Closes #\\d+$ on its own line, case-sensitive)"
        )


# %G? in `git log --format` returns a single character per commit indicating
# signature status. "G" = good signature, "U" = good but key not in local
# trust DB (acceptable — GitHub's isValid is the source of truth at PR time),
# anything else ("N" no sig, "B" bad sig, "X" expired sig, "Y" expired key,
# "R" revoked key, "E" can't check) is a policy violation.
_ACCEPTABLE_SIG_STATUSES = frozenset({"G", "U"})


def _assert_branch_commits_signed(branch: str, base: str = "main") -> None:
    """Raise if any commit on *branch* (since *base*) is unsigned or invalid.

    Uses ``git log --format='%H %G?'`` to enumerate commits and their signature
    status. The base ref is fetched first to ensure the range is meaningful in
    detached/shallow clones; failure to fetch is non-fatal because the existing
    local ref is sufficient when present.
    """
    # Best-effort fetch of the base ref. Don't fail signing checks just because
    # the operator is offline — the local base is usually fresh enough.
    with contextlib.suppress(Exception):
        run(["git", "fetch", "origin", base, "--quiet"], check=False, timeout=gh_cli_timeout())

    result = run(
        ["git", "log", "--format=%H %G?", f"origin/{base}..{branch}"],
        check=False,
        timeout=gh_cli_timeout(),
    )
    if result.returncode != 0:
        # Fall back to a non-origin range if origin/<base> is unknown locally
        result = run(
            ["git", "log", "--format=%H %G?", f"{base}..{branch}"],
            check=True,
            timeout=gh_cli_timeout(),
        )

    bad: list[tuple[str, str]] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        oid, status = parts[0], parts[1].strip()
        if status not in _ACCEPTABLE_SIG_STATUSES:
            bad.append((oid, status))

    if bad:
        bad_str = ", ".join(f"{oid[:10]}={status!r}" for oid, status in bad)
        raise ValueError(
            f"Unsigned or invalid commits on branch {branch!r} (vs {base}): {bad_str}. "
            "Every commit MUST be cryptographically signed per repo policy."
        )


def _find_open_pr_for_head(branch: str) -> int | None:
    """Return the number of an OPEN PR on ``branch``'s head, or None.

    Used by :func:`gh_pr_create` as an idempotency guard so a re-run on a
    branch that already has an open PR reuses it rather than creating a
    duplicate (issue #1018). Any failure to query or parse (no PRs, malformed
    output, transient error) is treated as "no open PR" so PR creation can
    proceed normally.

    Args:
        branch: Head branch name to look up.

    Returns:
        The PR number of the first OPEN PR on the head, or None.

    """
    try:
        result = _gh_call(
            ["pr", "list", "--head", branch, "--json", "number,state", "--limit", "10"]
        )
        prs = json.loads(result.stdout or "[]")
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError) as e:
        logger.debug("Open-PR lookup failed for head %s (treating as none): %s", branch, e)
        return None
    for pr in prs:
        if str(pr.get("state", "")).upper() == "OPEN":
            return cast(int, pr["number"])
    return None


def gh_pr_create(
    branch: str,
    title: str,
    body: str,
    auto_merge: bool = False,
    base: str = "main",
) -> int:
    """Create a pull request.

    Enforces PR body and signing policy at creation time:

    1. *body* must contain a literal ``Closes #N`` line.
    2. Every commit on *branch* (vs *base*) must be cryptographically signed.

    When ``auto_merge=True`` the helper also arms auto-merge immediately. The
    implementation pipeline deliberately passes ``False`` until the in-loop
    implementation review marks the PR GO.

    The CI gate (``.github/workflows/_required.yml`` job ``pr-policy``) and the
    PR review prompt re-check the same three properties, so a slip past one
    layer will surface at the next.

    Args:
        branch: Branch name
        title: PR title
        body: PR description
        auto_merge: Whether to enable auto-merge immediately (default False)
        base: Base branch to compare against for signed-commit validation

    Returns:
        PR number

    Raises:
        ValueError: If *body* lacks ``Closes #N`` or *branch* has unsigned commits.
        RuntimeError: If the underlying ``gh`` CLI call fails, or immediate
            auto-merge cannot be enabled when ``auto_merge=True``.

    """
    # Policy gate #1: PR body must reference the closing issue.
    _assert_body_has_closes(body)

    # Policy gate #2: every commit on the branch must be signed.
    _assert_branch_commits_signed(branch, base=base)

    # Idempotency guard: if an OPEN PR already exists on this head, reuse it
    # instead of opening a duplicate. This is the single chokepoint that all
    # PR-creation callers funnel through, so it prevents the duplicate-PR
    # failure observed on issue #768 (issue #1018). A closed/merged-only head
    # still gets a fresh PR — the issue may legitimately need new work, and the
    # worktree manager already extends the remote branch's history.
    existing_open_pr = _find_open_pr_for_head(branch)
    if existing_open_pr is not None:
        logger.info("Reusing existing open PR #%s on head %s", existing_open_pr, branch)
        return existing_open_pr

    try:
        # Create PR
        with _body_file(body) as body_path:
            result = _gh_call(
                [
                    "pr",
                    "create",
                    "--head",
                    branch,
                    "--base",
                    base,
                    "--title",
                    title,
                    "--body-file",
                    body_path,
                ]
            )

        # Extract PR number from URL in output
        output = result.stdout.strip()
        try:
            # Try to extract number from URL (e.g., https://github.com/owner/repo/pull/123)
            match = re.search(r"/pull/(\d+)", output)
            pr_number = int(match.group(1)) if match else int(output.split("/")[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"Failed to parse PR number from output: {output}") from e

        logger.info("Created PR #%s", pr_number)

        if auto_merge:
            try:
                _gh_call(["pr", "merge", str(pr_number), "--auto", "--squash"])
                logger.info("Enabled auto-merge for PR #%s", pr_number)
            except Exception as e:
                logger.error("Failed to enable auto-merge for PR #%s: %s", pr_number, e)
                raise RuntimeError(
                    f"Auto-merge could not be enabled for PR #{pr_number}: {e}. "
                    "Resolve the underlying issue (e.g. branch protection, merge method) "
                    "and re-run."
                ) from e

        return pr_number

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to create PR: {e}") from e


def _fetch_batch_states(batch: list[int], owner: str, repo: str) -> dict[int, IssueState]:
    """Fetch issue states for a single batch via GraphQL with individual fallback.

    Args:
        batch: Issue numbers to fetch.
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Mapping of issue number to IssueState for the batch.

    """
    # GraphQL cannot index a list variable to build per-element aliases, so we
    # declare one $nN:Int! per issue and bind each via -F nN=<int>. The f-string
    # interpolates only range(len(batch))-derived fragment indices (query structure),
    # never user data. This was smoke-tested against the live GitHub endpoint.
    var_decls = ",".join(f"$n{idx}:Int!" for idx in range(len(batch)))
    fragments = " ".join(
        f"issue{idx}: issue(number:$n{idx}){{ number state }}" for idx in range(len(batch))
    )
    query = (
        f"query($owner:String!,$name:String!,{var_decls})"
        f"{{repository(owner:$owner,name:$name){{{fragments}}}}}"
    )
    args = ["api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={repo}"]
    for idx, num in enumerate(batch):
        args.extend(["-F", f"n{idx}={int(num)}"])

    states: dict[int, IssueState] = {}
    try:
        result = _gh_call(args)
        data = json.loads(result.stdout)
        _check_graphql_errors(data, "prefetch_issue_states")
        repo_data = data.get("data", {}).get("repository", {})
        for key, issue_data in repo_data.items():
            if key.startswith("issue") and issue_data:
                states[issue_data["number"]] = IssueState(issue_data["state"])
        logger.debug("Fetched states for %s issues", len(batch))
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to batch fetch issue states: %s", e)
        for num in batch:
            try:
                issue_data = gh_issue_json(num)
                states[num] = IssueState(issue_data["state"])
            except Exception as e2:
                logger.warning("Failed to fetch state for issue #%s: %s", num, e2)
    return states


def prefetch_issue_states(issue_numbers: list[int]) -> dict[int, IssueState]:
    """Batch fetch issue states using GraphQL.

    Args:
        issue_numbers: List of issue numbers

    Returns:
        Dictionary mapping issue number to state

    """
    if not issue_numbers:
        return {}

    try:
        owner, repo = get_repo_info()
    except RuntimeError as e:
        logger.warning("Failed to get repo info: %s", e)
        return {}

    # Sanitize owner and repo to prevent GraphQL injection
    # Owner and repo should be alphanumeric with hyphens/underscores
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return {}

    batch_size = 100
    all_states: dict[int, IssueState] = {}
    for i in range(0, len(issue_numbers), batch_size):
        batch = issue_numbers[i : i + batch_size]
        all_states.update(_fetch_batch_states(batch, owner, repo))

    return all_states


def is_issue_closed(issue_number: int, cached_states: dict[int, IssueState] | None = None) -> bool:
    """Check if an issue is closed.

    Args:
        issue_number: GitHub issue number
        cached_states: Optional pre-fetched states cache

    Returns:
        True if issue is closed

    """
    if cached_states and issue_number in cached_states:
        # A merged PR referenced as a dependency is terminal too, so treat
        # MERGED as "closed" for skip purposes.
        return cached_states[issue_number].is_done

    try:
        issue_data = gh_issue_json(issue_number)
        return issue_data["state"] in ("CLOSED", "MERGED")
    except Exception as e:
        logger.warning("Failed to check if issue #%s is closed: %s", issue_number, e)
        return False


def parse_issue_dependencies(issue_body: str) -> list[int]:
    """Parse issue dependencies from issue body.

    Looks for patterns like:
    - Depends on #123
    - Depends: #123, #456
    - Blocked by #789

    Args:
        issue_body: Issue body text

    Returns:
        List of dependency issue numbers

    """
    dependencies = []

    # Pattern 1: Find all #numbers after dependency keywords
    dep_keywords = r"(?:depends on|blocked by|requires|dependencies?:?)"
    # Find all #123 patterns in lines containing dependency keywords
    for line in issue_body.split("\n"):
        if re.search(dep_keywords, line, re.IGNORECASE):
            # Find all #number patterns in this line
            for match in re.finditer(r"#(\d+)", line):
                dependencies.append(int(match.group(1)))

    # Pattern 2: Find issue references in lists under Dependencies heading
    # Look for a "Dependencies" section and extract list items from it
    dep_section_match = re.search(
        r"##\s*Dependencies.*?\n(.*?)(?=##|\Z)", issue_body, re.IGNORECASE | re.DOTALL
    )
    if dep_section_match:
        dep_section = dep_section_match.group(1)
        list_pattern = r"^\s*[-*]\s*#(\d+)"
        for match in re.finditer(list_pattern, dep_section, re.MULTILINE):
            dependencies.append(int(match.group(1)))

    return list(set(dependencies))  # Remove duplicates


def fetch_issue_info(issue_number: int) -> IssueInfo:
    """Fetch complete issue information.

    Args:
        issue_number: GitHub issue number

    Returns:
        IssueInfo instance

    Raises:
        RuntimeError: If fetch fails

    """
    issue_data = gh_issue_json(issue_number)

    return IssueInfo(
        number=issue_data["number"],
        title=issue_data["title"],
        body=issue_data.get("body", ""),
        state=IssueState(issue_data["state"]),
        labels=[label["name"] for label in issue_data.get("labels", [])],
        dependencies=parse_issue_dependencies(issue_data.get("body", "")),
    )


def write_secure(path: Path, content: str) -> None:
    """Write content to a file atomically with restrictive permissions.

    Thin wrapper over the canonical :func:`hephaestus.io.utils.write_secure` so
    automation state files share one atomic, ``0o600`` write implementation.

    Args:
        path: Destination file path
        content: Content to write

    """
    io_write_secure(path, content)
    logger.debug("Wrote %s bytes to %s", len(content), path)


# Matches a unified-diff hunk header: ``@@ -oldStart,oldLen +newStart,newLen @@``.
# The ``,len`` groups are optional (git omits them when the length is 1).
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _valid_review_positions(diff_text: str) -> dict[str, set[tuple[int, str]]]:
    """Map each changed file to the ``(line, side)`` positions GitHub will accept.

    GitHub's review API rejects (HTTP 422) any inline comment whose ``line``/
    ``side`` does not fall on a line present in the PR diff. A ``RIGHT`` comment
    must target an added (``+``) or context (`` ``) line in the new file; a
    ``LEFT`` comment must target a removed (``-``) or context line in the old
    file. This parses the unified diff once into the set of accepted positions.

    Args:
        diff_text: Unified diff (``gh pr diff <n>`` output).

    Returns:
        ``{path: {(line_number, side), ...}}`` for every changed file.

    """
    positions: dict[str, set[tuple[int, str]]] = {}
    current_path: str | None = None
    old_line = 0
    new_line = 0

    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            # ``+++ b/path`` (or ``+++ /dev/null``); strip the ``b/`` prefix.
            target = raw[4:].strip()
            if target == "/dev/null":
                current_path = None
            else:
                current_path = target[2:] if target.startswith("b/") else target
                positions.setdefault(current_path, set())
            continue
        if raw.startswith("--- "):
            # Old-file header; new-file header (+++) sets the path.
            continue

        header = _HUNK_HEADER_RE.match(raw)
        if header:
            old_line = int(header.group(1))
            new_line = int(header.group(2))
            continue

        if current_path is None or not raw:
            continue

        marker = raw[0]
        if marker == "+":
            positions[current_path].add((new_line, "RIGHT"))
            new_line += 1
        elif marker == "-":
            positions[current_path].add((old_line, "LEFT"))
            old_line += 1
        elif marker == " ":
            # Context line is valid on both sides.
            positions[current_path].add((new_line, "RIGHT"))
            positions[current_path].add((old_line, "LEFT"))
            old_line += 1
            new_line += 1
        # Any other marker (e.g. ``\`` for "No newline") is ignored.

    return positions


def _filter_comments_to_diff(
    comments: list[dict[str, Any]], diff_text: str
) -> list[dict[str, Any]]:
    """Drop inline comments whose ``(path, line, side)`` is not in the diff.

    Prevents an out-of-hunk comment from making GitHub reject the *entire*
    review with HTTP 422 (#1039). Dropped comments are logged at WARNING.

    Fails open: if ``diff_text`` is empty (the diff could not be fetched), the
    comments are returned unchanged — losing a comment because the diff was
    unavailable would be worse than a possible 422.

    Args:
        comments: Inline comment dicts with ``path``/``line``/``side``/``body``.
        diff_text: Unified diff to validate against.

    Returns:
        The subset of ``comments`` that target a line present in the diff.

    """
    if not diff_text.strip():
        return comments

    valid = _valid_review_positions(diff_text)
    kept: list[dict[str, Any]] = []
    for c in comments:
        path = c.get("path", "")
        line = c.get("line")
        side = c.get("side", "RIGHT")
        if path in valid and (line, side) in valid[path]:
            kept.append(c)
        else:
            logger.warning(
                "Dropping out-of-hunk review comment on %s:%s (%s) — not in PR diff",
                path,
                line,
                side,
            )
    return kept


def gh_pr_review_post(
    pr_number: int,
    comments: list[dict[str, Any]],
    summary: str,
    event: str = "COMMENT",
    dry_run: bool = False,
) -> list[str]:
    """Post a PR review with inline comments via GitHub GraphQL API.

    Args:
        pr_number: PR number
        comments: List of dicts with keys: path (str), line (int), side (str), body (str)
        summary: Overall review summary body
        event: Review event type: COMMENT, APPROVE, or REQUEST_CHANGES
        dry_run: If True, log intent and return empty list without posting

    Returns:
        List of created review thread IDs (empty on dry_run or if no comments)

    """
    if dry_run:
        logger.info(
            "[dry_run] Would post PR review on #%s with %s inline comments",
            pr_number,
            len(comments),
        )
        return []

    owner, repo = get_repo_info()

    # Build the inline-comment list. The REST reviews endpoint natively accepts
    # ``{path, line, side, body}`` — unlike the GraphQL
    # ``DraftPullRequestReviewComment`` input type, which only exposes
    # ``path``/``position``/``body`` and has NO ``line``/``side`` fields. The
    # previous GraphQL mutation therefore failed twice over:
    #   1. ``-f comments=<json-string>`` sent the array as a STRING, so GitHub
    #      rejected every post ("Variable $comments ... was provided invalid
    #      value"; even ``[]`` failed with 'Expected "[]" to be a key-value
    #      object'), and the loop treated every PR as a spurious NOGO.
    #   2. Even passed as a typed array, ``line``/``side`` are undefined on
    #      ``DraftPullRequestReviewComment``.
    # POST /pulls/{n}/reviews is the correct surface for ``line``/``side``
    # comments and for summary-only (empty ``comments``) reviews alike.
    #
    # #1039: GitHub returns HTTP 422 and rejects the WHOLE review if any inline
    # comment targets a line outside the PR diff hunks. The reviewer model can
    # cite such lines (especially since its diff context is truncated upstream),
    # so validate each comment against the live diff and drop the strays — a
    # logged degradation instead of a hard failure that the loop reads as a NOGO.
    if comments:
        diff_result = _gh_call(["pr", "diff", str(pr_number)], check=False)
        comments = _filter_comments_to_diff(comments, diff_result.stdout or "")

    review_comments = [
        {
            "path": c["path"],
            "line": c["line"],
            "side": c.get("side", "RIGHT"),
            "body": c["body"],
        }
        for c in comments
    ]

    request_body = json.dumps({"body": summary, "event": event, "comments": review_comments})
    fd, input_path = tempfile.mkstemp(prefix="gh-review-", suffix=".json")
    try:
        os.close(fd)
        io_write_secure(Path(input_path), request_body)
        result = _gh_call(
            [
                "api",
                "-X",
                "POST",
                f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                "--input",
                input_path,
            ]
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(input_path)

    review = json.loads(result.stdout)
    # The REST payload returns both the numeric ``id`` and the GraphQL global
    # ``node_id``. The thread-resolution follow-up matches on the GraphQL review
    # node id, so pass that through (preserving the #375 guarantee that only
    # threads created by *this* review are returned).
    review_node_id = review.get("node_id")
    if not review_node_id:
        logger.warning("Posted PR review on #%s but no review node id returned", pr_number)
        return []

    thread_ids = _review_threads_for_review(pr_number, review_node_id)
    logger.info("Posted PR review on #%s; created %s thread(s)", pr_number, len(thread_ids))
    return thread_ids


def _review_threads_for_review(pr_number: int, review_id: str) -> list[str]:
    """Return unresolved review-thread IDs belonging to review ``review_id``.

    A ``PullRequestReviewComment`` does not expose its parent thread, so we
    cannot derive thread IDs from the ``addPullRequestReview`` payload. Instead
    we list ``pullRequest.reviewThreads`` and keep the threads whose *first*
    comment was authored by this review (``comments.nodes[0].pullRequestReview.id
    == review_id``). This preserves the #375 guarantee — only threads created by
    *this* review are returned, not pre-existing human-reviewer threads — while
    using fields that actually exist in the GitHub GraphQL schema.

    Returns an empty list on any failure (the caller treats no-threads as
    "nothing to resolve later", which is safe).
    """
    owner, repo = get_repo_info()
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return []

    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    pullRequest(number:$number){"
        "      reviewThreads(first:100){"
        "        nodes{ id isResolved comments(first:1){ nodes{ pullRequestReview{ id } } } }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        result = _gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={repo}",
                "-F",
                f"number={int(pr_number)}",
            ]
        )
        data = json.loads(result.stdout)
        _check_graphql_errors(data, f"_review_threads_for_review(pr={pr_number})")
    except (subprocess.CalledProcessError, json.JSONDecodeError, RuntimeError) as exc:
        logger.warning("Could not fetch review threads for PR #%s: %s", pr_number, exc)
        return []

    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )

    # Preserve insertion order; a thread can hold multiple comments but its id
    # is unique, so a dict keyed on id dedupes naturally.
    seen: dict[str, None] = {}
    for node in nodes:
        if node.get("isResolved"):
            continue
        first_comments = node.get("comments", {}).get("nodes", [])
        if not first_comments:
            continue
        review = first_comments[0].get("pullRequestReview") or {}
        if review.get("id") != review_id:
            continue
        tid = node.get("id")
        if tid:
            seen[tid] = None

    return list(seen)


def gh_pr_list_unresolved_threads(
    pr_number: int,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """List unresolved review threads for a PR.

    Args:
        pr_number: PR number
        dry_run: If True, return empty list

    Returns:
        List of thread dicts with keys: id (str), path (str), line (int | None), body (str)

    """
    if dry_run:
        logger.info("[dry_run] Would list unresolved threads for PR #%s", pr_number)
        return []

    owner, repo = get_repo_info()

    # Sanitize owner/repo to prevent injection (same pattern as prefetch_issue_states)
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return []

    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    pullRequest(number:$number){"
        "      reviewThreads(first:100){"
        "        nodes{ id isResolved path line comments(first:1){ nodes{ body } } }"
        "      }"
        "    }"
        "  }"
        "}"
    )

    result = _gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={repo}",
            "-F",
            f"number={int(pr_number)}",
        ]
    )
    data = json.loads(result.stdout)
    _check_graphql_errors(data, f"gh_pr_list_unresolved_threads(pr={pr_number})")

    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )

    threads: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("isResolved"):
            continue
        first_comment_nodes = node.get("comments", {}).get("nodes", [])
        body = first_comment_nodes[0]["body"] if first_comment_nodes else ""
        threads.append(
            {
                "id": node["id"],
                "path": node.get("path", ""),
                "line": node.get("line"),
                "body": body,
            }
        )

    logger.debug("Found %s unresolved thread(s) on PR #%s", len(threads), pr_number)
    return threads


def gh_pr_resolve_thread(
    thread_id: str,
    reply_body: str,
    dry_run: bool = False,
) -> None:
    """Resolve a PR review thread with a reply comment.

    Args:
        thread_id: GraphQL node ID of the review thread
        reply_body: Reply comment text
        dry_run: If True, log intent without posting

    """
    if dry_run:
        logger.info("[dry_run] Would resolve thread %r with reply: %r", thread_id, reply_body)
        return

    # Step 1: post a reply to the thread via GraphQL addPullRequestReviewComment
    reply_mutation = """
mutation AddReply($threadId: ID!, $body: String!) {
  addPullRequestReviewComment(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id }
  }
}
"""
    reply_result = _gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={reply_mutation}",
            "-f",
            f"threadId={thread_id}",
            "-f",
            f"body={reply_body}",
        ]
    )
    # JSONDecodeError is benign here — only an explicit ``errors`` array must
    # surface. ``contextlib.suppress`` lets the helper raise on errors while
    # silently absorbing the no-body case.
    with contextlib.suppress(json.JSONDecodeError):
        _check_graphql_errors(
            json.loads(reply_result.stdout or "{}"),
            f"gh_pr_resolve_thread.reply(thread={thread_id})",
        )

    # Step 2: resolve the thread
    resolve_mutation = """
mutation ResolveThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""
    resolve_result = _gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={resolve_mutation}",
            "-f",
            f"threadId={thread_id}",
        ]
    )
    with contextlib.suppress(json.JSONDecodeError):
        _check_graphql_errors(
            json.loads(resolve_result.stdout or "{}"),
            f"gh_pr_resolve_thread.resolve(thread={thread_id})",
        )
    logger.info("Resolved review thread %r", thread_id)


# ``gh pr checks --json`` rollup buckets → (status, conclusion) in the contract that
# ci_driver.py consumes. A terminal bucket (anything but "pending") means the check has
# concluded; the bucket also tells us pass/fail/skip.
_PR_CHECK_BUCKET_MAP: dict[str, tuple[str, str | None]] = {
    "pass": ("completed", "success"),
    "fail": ("completed", "failure"),
    "cancel": ("completed", "failure"),
    "skipping": ("completed", "skipped"),
    "pending": ("in_progress", None),
}


def _map_pr_check(item: dict[str, Any]) -> dict[str, Any]:
    """Map one raw ``gh pr checks --json`` entry onto the status/conclusion contract."""
    bucket = str(item.get("bucket", "")).lower()
    status, conclusion = _PR_CHECK_BUCKET_MAP.get(bucket, ("in_progress", None))
    return {
        "name": item.get("name", ""),
        "status": status,
        "conclusion": conclusion,
        "required": False,
    }


# ``gh pr checks`` exits non-zero with this stderr when the PR's head branch
# has no check runs registered yet (a fresh PR before workflows have been
# scheduled, or a repo with no CI configured for the branch). It is *not* a
# real error — it is the empty result — so callers should see ``[]`` rather
# than a hard failure that aborts the entire CI drive.
_GH_PR_CHECKS_NO_CHECKS_FRAGMENT: str = "no checks reported"


def _is_gh_pr_checks_no_checks_error(exc: subprocess.CalledProcessError) -> bool:
    """Return True iff a failed ``gh pr checks`` is the no-checks-yet case."""
    blob = (exc.stderr or "") + (exc.stdout or "")
    return _GH_PR_CHECKS_NO_CHECKS_FRAGMENT in blob


def gh_pr_checks(
    pr_number: int,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Get CI check results for a PR.

    Args:
        pr_number: PR number
        dry_run: If True, return empty list

    Returns:
        List of check dicts with keys: name (str), status (str), conclusion (str | None),
        required (bool). Empty list if the PR has no check runs yet (``gh pr checks``
        treats this as an error but the driver treats it as the empty case).

        ``gh pr checks --json`` does not expose ``status``/``conclusion``/``required`` — it
        exposes ``state`` (e.g. ``SUCCESS``/``FAILURE``/``PENDING``) and ``bucket``
        (``pass``/``fail``/``pending``/``skipping``/``cancel``). Those are mapped here onto the
        ``status``/``conclusion`` keys this module's consumers expect. ``required`` is not in the
        schema, so it defaults to ``False`` (callers treat "no required checks" as "all required").

    """
    if dry_run:
        logger.info("[dry_run] Would fetch CI checks for PR #%s", pr_number)
        return []

    try:
        result = _gh_call(["pr", "checks", str(pr_number), "--json", "name,state,bucket,workflow"])
    except subprocess.CalledProcessError as exc:
        if _is_gh_pr_checks_no_checks_error(exc):
            logger.info(
                "PR #%s has no check runs registered yet (gh: %s); treating as empty",
                pr_number,
                _GH_PR_CHECKS_NO_CHECKS_FRAGMENT,
            )
            return []
        raise
    raw: list[dict[str, Any]] = json.loads(result.stdout)

    checks: list[dict[str, Any]] = [_map_pr_check(item) for item in raw]

    logger.debug("Fetched %s CI check(s) for PR #%s", len(checks), pr_number)
    return checks
