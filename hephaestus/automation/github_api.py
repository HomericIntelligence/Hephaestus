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


def gh_list_labels(refresh: bool = False) -> set[str]:
    """Return the set of label names that exist in the current repository.

    Args:
        refresh: If True, bypass the in-process cache and re-fetch.

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
        r"invalid argument",
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


def _gh_call(
    args: list[str],
    check: bool = True,
    retry_on_rate_limit: bool = True,
    max_retries: int = 6,
) -> subprocess.CompletedProcess[str]:
    """Call gh CLI with rate limit handling.

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


def gh_pr_create(
    branch: str,
    title: str,
    body: str,
    auto_merge: bool = True,
    base: str = "main",
) -> int:
    """Create a pull request.

    Enforces three policy properties at creation time:

    1. *body* must contain a literal ``Closes #N`` line.
    2. Every commit on *branch* (vs *base*) must be cryptographically signed.
    3. Auto-merge MUST be enabled when ``auto_merge=True`` (the default); a
       failure to enable auto-merge raises instead of warning.

    The CI gate (``.github/workflows/_required.yml`` job ``pr-policy``) and the
    PR review prompt re-check the same three properties, so a slip past one
    layer will surface at the next.

    Args:
        branch: Branch name
        title: PR title
        body: PR description
        auto_merge: Whether to enable auto-merge (default True; required by policy)
        base: Base branch to compare against for signed-commit validation

    Returns:
        PR number

    Raises:
        ValueError: If *body* lacks ``Closes #N`` or *branch* has unsigned commits.
        RuntimeError: If the underlying ``gh`` CLI call fails, or auto-merge
            cannot be enabled when ``auto_merge=True``.

    """
    # Policy gate #1: PR body must reference the closing issue.
    _assert_body_has_closes(body)

    # Policy gate #2: every commit on the branch must be signed.
    _assert_branch_commits_signed(branch, base=base)

    try:
        # Create PR
        with _body_file(body) as body_path:
            result = _gh_call(
                [
                    "pr",
                    "create",
                    "--head",
                    branch,
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

        # Policy gate #3: auto-merge is mandatory when requested. A failure here
        # used to log a warning; under the new policy it must raise so the
        # operator sees the violation.
        if auto_merge:
            try:
                _gh_call(["pr", "merge", str(pr_number), "--auto", "--rebase"])
                logger.info("Enabled auto-merge for PR #%s", pr_number)
            except Exception as e:
                logger.error("Failed to enable auto-merge for PR #%s: %s", pr_number, e)
                raise RuntimeError(
                    f"Auto-merge could not be enabled for PR #{pr_number}: {e}. "
                    "Auto-merge is required by repo policy; resolve the underlying "
                    "issue (e.g. branch protection, merge method) and re-run."
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
    fragments = [
        f"issue{idx}: issue(number: {num}) {{ number state }}" for idx, num in enumerate(batch)
    ]
    query = f"""
        query {{
            repository(owner: "{owner}", name: "{repo}") {{
                {" ".join(fragments)}
            }}
        }}
        """
    states: dict[int, IssueState] = {}
    try:
        result = _gh_call(["api", "graphql", "-f", f"query={query}"])
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
        return cached_states[issue_number] == IssueState.CLOSED

    try:
        issue_data = gh_issue_json(issue_number)
        return cast(bool, issue_data["state"] == "CLOSED")
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

    # Fetch the PR node ID via REST
    pr_info = _gh_call(["api", f"repos/{owner}/{repo}/pulls/{pr_number}", "--jq", ".node_id"])
    pr_node_id = pr_info.stdout.strip()

    # Build threads list for the mutation
    thread_items = [
        {
            "path": c["path"],
            "line": c["line"],
            "side": c.get("side", "RIGHT"),
            "body": c["body"],
        }
        for c in comments
    ]

    # The mutation returns only the review-level comment nodes.  Each
    # inline comment belongs to exactly one review thread; we ask for the
    # ``pullRequestReviewThread`` on every comment so we can collect the
    # thread IDs created by *this* review only — not every unresolved thread
    # on the PR.  This fixes the "foreign thread" bug (#375) where the old
    # approach fetched ``pullRequest { reviewThreads(last: 50) }`` and
    # returned pre-existing threads from human reviewers.
    mutation = """
mutation AddReview(
  $prId: ID!, $body: String!,
  $event: PullRequestReviewEvent!,
  $comments: [DraftPullRequestReviewComment!]
) {
  addPullRequestReview(
    input: {pullRequestId: $prId, body: $body, event: $event, comments: $comments}
  ) {
    pullRequestReview {
      id
      comments(first: 50) {
        nodes {
          pullRequestReviewThread {
            id
            isResolved
          }
        }
      }
    }
  }
}
"""

    threads_json = json.dumps(thread_items)
    result = _gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"prId={pr_node_id}",
            "-f",
            f"body={summary}",
            "-f",
            f"event={event}",
            "-f",
            f"comments={threads_json}",
        ]
    )

    data = json.loads(result.stdout)
    _check_graphql_errors(data, f"gh_pr_review_post(pr={pr_number})")
    review_data = data.get("data", {}).get("addPullRequestReview", {}).get("pullRequestReview", {})
    comment_nodes = review_data.get("comments", {}).get("nodes", [])

    # Deduplicate: multiple comments may belong to the same thread (e.g.
    # multi-line comments), so collect unique thread IDs using a dict to
    # preserve insertion order.
    seen: dict[str, None] = {}
    for comment_node in comment_nodes:
        thread_info = comment_node.get("pullRequestReviewThread")
        if thread_info is None:
            continue
        tid = thread_info.get("id")
        if tid and not thread_info.get("isResolved", False):
            seen[tid] = None

    thread_ids: list[str] = list(seen)
    logger.info("Posted PR review on #%s; created %s thread(s)", pr_number, len(thread_ids))
    return thread_ids


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

    query = f"""
query GetThreads {{
  repository(owner: "{owner}", name: "{repo}") {{
    pullRequest(number: {pr_number}) {{
      reviewThreads(first: 100) {{
        nodes {{
          id
          isResolved
          path
          line
          comments(first: 1) {{
            nodes {{ body }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    result = _gh_call(["api", "graphql", "-f", f"query={query}"])
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
        required (bool)

    """
    if dry_run:
        logger.info("[dry_run] Would fetch CI checks for PR #%s", pr_number)
        return []

    result = _gh_call(
        ["pr", "checks", str(pr_number), "--json", "name,status,conclusion,workflow,required"]
    )
    raw: list[dict[str, Any]] = json.loads(result.stdout)

    checks: list[dict[str, Any]] = [
        {
            "name": item.get("name", ""),
            "status": item.get("status", ""),
            "conclusion": item.get("conclusion") or None,
            "required": bool(item.get("required", False)),
        }
        for item in raw
    ]

    logger.debug("Fetched %s CI check(s) for PR #%s", len(checks), pr_number)
    return checks
