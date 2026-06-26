"""GitHub API utilities using gh CLI.

Provides:
- Issue data fetching with caching
- Rate-limited API calls
- Batch operations with GraphQL
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

from hephaestus.github import client as _gh_client
from hephaestus.github.client import (
    ClaudeUsageCapError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    gh_call,
    gh_cli_timeout,
)
from hephaestus.github.rate_limit import (
    gh_rate_limit_reset_epoch,
)
from hephaestus.io.utils import write_secure

from .git_utils import get_repo_info, run
from .models import IssueInfo, IssueState

logger = logging.getLogger(__name__)

_label_cache: set[str] | None = None

# #1587: in-process memo for issue states so repeated prefetch_issue_states
# calls within ONE process (e.g. the parent loop's in-loop + post-loop
# closed-filter, and any phase that prefetches more than once) reuse the result
# of one gh GraphQL round-trip instead of re-querying. Process-scoped: it dies
# with the subprocess, so cross-phase staleness is bounded by the subprocess
# lifetime, and ``refresh=True`` forces a fresh read when a caller needs it.
_issue_state_cache: dict[int, IssueState] = {}

# Module-level aliases so existing test patches keep resolving.
# unittest.mock.patch walks the dotted path's module attributes at patch
# time; these names exist as attributes of this module after the
# aliases, so @patch("hephaestus.automation.github_api._gh_call") and
# similar test patches continue to work without churn.
_gh_call = gh_call
_GH_BREAKER = _gh_client._GH_BREAKER
_GH_THROTTLE = _gh_client._GH_THROTTLE
# Compatibility alias for existing github_api patch seams and downstream named
# imports. New code should import hephaestus.io.utils.write_secure directly.
io_write_secure = write_secure

__all__ = [
    "ClaudeUsageCapError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "gh_call",
    "gh_cli_timeout",
]


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


def _gh_commit_is_verified(oid: str) -> bool:
    """Return True if GitHub reports *oid*'s signature as verified.

    The local ``git log --format=%G?`` check returns ``N`` (no signature) for a
    commit that is actually **SSH-signed** when the local checkout has no
    ``gpg.ssh.allowedSignersFile`` configured — git cannot verify SSH signatures
    without it. GitHub, however, validates the signature server-side and exposes
    the result at ``repos/{owner}/{repo}/commits/{sha}`` under
    ``.commit.verification.verified``. That flag is the source of truth at PR
    time (the same rationale that makes ``U`` acceptable above), so we consult
    it before declaring a policy violation. Any lookup failure returns False so
    the caller falls back to the strict local verdict (fail safe).
    """
    try:
        owner, name = get_repo_info()
        result = _gh_call(
            [
                "api",
                f"repos/{owner}/{name}/commits/{oid}",
                "--jq",
                ".commit.verification.verified",
            ],
        )
        return (result.stdout or "").strip().lower() == "true"
    except Exception as exc:  # pragma: no cover - logged, treated as unverified
        logger.warning("Could not confirm GitHub signature for %s: %s", oid[:10], exc)
        return False


def _assert_branch_commits_signed(branch: str, base: str = "main") -> None:
    """Raise if any commit on *branch* (since *base*) is unsigned or invalid.

    Uses ``git log --format='%H %G?'`` to enumerate commits and their signature
    status. The base ref is fetched first to ensure the range is meaningful in
    detached/shallow clones; failure to fetch is non-fatal because the existing
    local ref is sufficient when present.

    A commit whose local status is *not* acceptable (e.g. ``N`` for an
    SSH-signed commit the local checkout can't verify without
    ``gpg.ssh.allowedSignersFile``) is re-checked against GitHub's commit
    verification API before it is flagged — GitHub's ``verified`` flag is
    authoritative at PR time. Only commits that fail BOTH the local check and
    the API check are treated as policy violations.
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
            # Local git couldn't bless it — but it may be SSH-signed and simply
            # unverifiable here. Defer to GitHub's authoritative verdict before
            # flagging it as a policy violation.
            if _gh_commit_is_verified(oid):
                continue
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


def prefetch_issue_states(
    issue_numbers: list[int], *, refresh: bool = False
) -> dict[int, IssueState]:
    """Batch fetch issue states using GraphQL, memoized in-process (#1587).

    Results are cached per process in :data:`_issue_state_cache`, so repeated
    calls within one process only query the numbers not already seen. The gh
    GraphQL round-trip is the most expensive of the loop's repeated lookups and
    previously had no caching at all (it ran once per phase-subprocess AND twice
    in the parent's closed-filter).

    Args:
        issue_numbers: List of issue numbers.
        refresh: When True, ignore the cache and re-query every number (and
            update the cache with fresh values). Use when a state may have
            changed mid-process and a stale read is unacceptable.

    Returns:
        Dictionary mapping issue number to state (only the requested numbers).

    """
    if not issue_numbers:
        return {}

    if not refresh:
        missing = [n for n in issue_numbers if n not in _issue_state_cache]
    else:
        missing = list(issue_numbers)
    if not missing:
        return {n: _issue_state_cache[n] for n in issue_numbers if n in _issue_state_cache}

    try:
        owner, repo = get_repo_info()
    except RuntimeError as e:
        logger.warning("Failed to get repo info: %s", e)
        return {n: _issue_state_cache[n] for n in issue_numbers if n in _issue_state_cache}

    # Sanitize owner and repo to prevent GraphQL injection
    # Owner and repo should be alphanumeric with hyphens/underscores
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return {n: _issue_state_cache[n] for n in issue_numbers if n in _issue_state_cache}

    batch_size = 100
    for i in range(0, len(missing), batch_size):
        batch = missing[i : i + batch_size]
        _issue_state_cache.update(_fetch_batch_states(batch, owner, repo))

    # Return only the requested numbers (those that resolved); a number that
    # failed to fetch is simply absent, matching the prior contract.
    return {n: _issue_state_cache[n] for n in issue_numbers if n in _issue_state_cache}


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


def fetch_open_prs() -> list[dict[str, Any]]:
    """Return every open PR's metadata via ``gh pr list`` (no row limit).

    Uses ``--limit 2147483647`` (INT_MAX) to honor the audit reviewer's
    'ALL open PRs' contract on repos with >200 open PRs. The gh CLI
    does not support a true no-cap sentinel; INT_MAX avoids pagination
    overhead while accommodating any realistic repo size.
    """
    result = _gh_call(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,url,isDraft",
            "--limit",
            "2147483647",
        ]
    )
    return cast(list[dict[str, Any]], json.loads(result.stdout or "[]"))


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


def gh_current_login() -> str | None:
    """Return the authenticated GitHub login for the current ``gh`` token."""
    try:
        result = _gh_call(["api", "user", "--jq", ".login"], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not determine current GitHub login: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning("Could not determine current GitHub login: %s", result.stderr or "")
        return None
    login = (result.stdout or "").strip()
    return login or None


ReviewCommentIndexKey = tuple[str, Any] | tuple[str, Any, str]


def _fetch_pr_inline_review_thread_nodes(pr_number: int) -> list[dict[str, Any]]:
    """Return PR review-thread nodes used by inline-comment dedupe helpers.

    Fails open: returns ``[]`` on any API/parse error so callers preserve their
    existing post-as-usual behaviour.
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
        "        nodes{ isResolved path line side:diffSide "
        "comments(first:20){ nodes{ id body viewerCanUpdate } } }"
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
            ],
            check=False,
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        logger.warning("PR #%s: could not fetch inline review-thread nodes (%s)", pr_number, exc)
        return []

    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def gh_pr_inline_comment_index(
    pr_number: int,
) -> dict[ReviewCommentIndexKey, tuple[str, str, bool]]:
    """Map ``(path, line)`` → ``(comment_node_id, body, editable)`` for unresolved threads.

    Returns the GraphQL node id, current body, AND ``viewerCanUpdate`` flag of
    the FIRST comment of each unresolved review thread, keyed by its
    ``(path, line)``. Used by :func:`gh_pr_review_post` to detect a line that
    already has a comment so the new content can be **appended** to the existing
    body in place rather than posted as a duplicate (#1083). The body is required
    because the ``updatePullRequestReviewComment`` mutation REPLACES the body —
    the caller must concatenate ``existing + new`` or the original comment is
    lost (#1085). The ``editable`` flag (``viewerCanUpdate``) is required because
    the first comment of a thread may belong to another app/account (Copilot,
    CodeQL); GitHub rejects an in-place edit of such a comment with
    ``Body is not editable``, so the caller must post its OWN editable shadow
    comment instead (#1327). Fails open: returns ``{}`` on any API/parse error so
    the caller posts everything as before.
    """
    index: dict[ReviewCommentIndexKey, tuple[str, str, bool]] = {}
    for node in _fetch_pr_inline_review_thread_nodes(pr_number):
        if node.get("isResolved"):
            continue
        comment_nodes = node.get("comments", {}).get("nodes", [])
        if not comment_nodes:
            continue
        comment_id = comment_nodes[0].get("id")
        if not comment_id:
            continue
        body = comment_nodes[0].get("body") or ""
        # Default to editable when the field is absent so behaviour is unchanged
        # for callers/tests that predate the ``viewerCanUpdate`` selection.
        editable = bool(comment_nodes[0].get("viewerCanUpdate", True))
        path = node.get("path") or ""
        line = node.get("line")
        side = node.get("side") or "RIGHT"
        index[(path, line, side)] = (comment_id, body, editable)
        # Preserve the older key shape for callers/tests that predate diff-side
        # support, and as a fallback if GitHub ever omits ``diffSide``.
        index.setdefault((path, line), (comment_id, body, editable))
    return index


def gh_pr_wont_fix_line_index(pr_number: int) -> set[ReviewCommentIndexKey]:
    """Return ``(path, line[, side])`` keys of WON'T-FIX-dismissed review threads.

    A thread is "won't-fix" when it is RESOLVED and any of its comments starts
    with :data:`~hephaestus.automation.protocol.WONT_FIX_MARKER` — the validator
    (or a human) dismissed the finding as intentional-by-design (#1163). The
    reviewer dedup (:func:`_edit_or_keep_comments`) uses this to SUPPRESS a
    re-raised finding on such a line, so an intentional-design comment cannot
    stack duplicate threads across runs. Fails open: ``set()`` on any error.
    """
    from .protocol import WONT_FIX_MARKER

    keys: set[ReviewCommentIndexKey] = set()
    for node in _fetch_pr_inline_review_thread_nodes(pr_number):
        if not node.get("isResolved"):
            continue
        comment_nodes = node.get("comments", {}).get("nodes", [])
        if not any(
            str(c.get("body") or "").lstrip().startswith(WONT_FIX_MARKER)
            for c in comment_nodes
            if isinstance(c, dict)
        ):
            continue
        path = node.get("path") or ""
        line = node.get("line")
        side = node.get("side") or "RIGHT"
        keys.add((path, line, side))
        keys.add((path, line))
    return keys


def gh_pr_update_review_comment(comment_node_id: str, body: str) -> None:
    """Replace a PR review comment's body via ``updatePullRequestReviewComment``.

    Used to edit an existing inline comment in place (#1083) instead of posting
    a duplicate on the same line.

    GitHub rejects this mutation when the token does not own the comment
    ("Body is not editable").  That failure is EXPECTED and fully recovered by
    :func:`_edit_or_keep_comments` — it catches the exception and posts an
    editable shadow comment instead (#1327).  We therefore suppress ERROR-level
    logs for this call (``log_on_error=False``) so routine automation-loop runs
    are not polluted with misleading error noise for a handled condition.
    Genuine, unexpected failures still propagate as exceptions to the caller.
    """
    mutation = (
        "mutation($id:ID!,$body:String!){"
        "  updatePullRequestReviewComment(input:{pullRequestReviewCommentId:$id,body:$body}){"
        "    pullRequestReviewComment{ id }"
        "  }"
        "}"
    )
    _gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"id={comment_node_id}",
            "-f",
            f"body={body}",
        ],
        log_on_error=False,
    )


# GitHub rejects ``updatePullRequestReviewComment`` on a comment the current
# token does not own ("gh: Body is not editable"). The first comment of a thread
# may belong to another app (Copilot, CodeQL), so this string is matched as a
# fallback signal when ``viewerCanUpdate`` was unavailable/stale (#1327).
_BODY_NOT_EDITABLE_MARKER = "not editable"

_ADDITIONAL_REVIEW_NOTE_DELIMITER = "\n\n---\n_Additional review note (same line):_\n\n"

_REVIEW_COMMENT_DEDUPE_STOPWORDS = {
    "about",
    "actual",
    "after",
    "again",
    "also",
    "another",
    "because",
    "before",
    "being",
    "cannot",
    "changed",
    "comment",
    "coverage",
    "current",
    "future",
    "only",
    "please",
    "production",
    "proves",
    "regression",
    "sibling",
    "still",
    "suite",
    "that",
    "this",
    "where",
    "with",
    "without",
    "would",
}

_REVIEW_COMMENT_SIGNATURE_TOKENS = {
    "claude",
    "codex",
    "is_codex",
    "review_text",
    "run_codex_text",
    "stderr",
    "stdout",
    "summary",
    "verdict",
}


def _normalize_review_comment_body(body: str) -> str:
    """Normalize a review comment body for duplicate-content comparison."""
    normalized = body.lower()
    normalized = re.sub(r"`([^`]*)`", r"\1", normalized)
    normalized = re.sub(r"[^a-z0-9_#./-]+", " ", normalized)
    return " ".join(normalized.split())


def _review_comment_keyword_tokens(body: str) -> set[str]:
    """Return content-bearing tokens for same-line review duplicate checks."""
    normalized = _normalize_review_comment_body(body)
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9_#./-]+", normalized):
        candidates = [token]
        if any(sep in token for sep in "./-"):
            candidates.extend(part for part in re.split(r"[./-]+", token) if part)
        for candidate in candidates:
            if len(candidate) < 4 and not candidate.startswith("#"):
                continue
            if candidate in _REVIEW_COMMENT_DEDUPE_STOPWORDS:
                continue
            tokens.add(candidate)
    return tokens


def _review_comment_already_covers(existing_body: str, new_body: str) -> bool:
    """Return True when an existing same-line comment already covers ``new_body``."""
    new_norm = _normalize_review_comment_body(new_body)
    if not new_norm:
        return True
    new_tokens = _review_comment_keyword_tokens(new_body)
    parts = existing_body.split(_ADDITIONAL_REVIEW_NOTE_DELIMITER)
    for part in parts:
        existing_norm = _normalize_review_comment_body(part)
        if not existing_norm:
            continue
        if new_norm in existing_norm or existing_norm in new_norm:
            return True
        if SequenceMatcher(None, existing_norm, new_norm).ratio() >= 0.82:
            return True
        existing_tokens = _review_comment_keyword_tokens(part)
        if new_tokens and existing_tokens:
            overlap = existing_tokens & new_tokens
            # Re-review wording varies, but true duplicates keep the same code
            # identifiers and defect nouns. Compare against the smaller set so
            # a shorter restatement of the same finding is still suppressed.
            overlap_ratio = len(overlap) / min(len(existing_tokens), len(new_tokens))
            if len(overlap) >= 6 and overlap_ratio >= 0.6:
                return True
            signature_overlap = overlap & _REVIEW_COMMENT_SIGNATURE_TOKENS
            if len(overlap) >= 6 and len(signature_overlap) >= 3:
                return True
    return False


def _post_shadow_review_comment(pr_number: int, comment: dict[str, Any]) -> tuple[str, str] | None:
    """Post OUR OWN editable inline comment shadowing a foreign (uneditable) one.

    The first comment of a thread can belong to another app/account (Copilot,
    CodeQL); GitHub forbids editing it ("Body is not editable"). Rather than
    silently drop the finding, we post a brand-new inline comment at the same
    ``(path, line, side)`` that WE own and can edit on every later run (#1327).

    Posting reuses :func:`gh_pr_review_post` with ``dedupe_existing=False`` so it
    does NOT re-enter the dedupe path (which would re-detect the same foreign
    comment and loop). ``gh_pr_review_post`` returns thread ids, not the inline
    comment node id, so we re-fetch the inline-comment index to recover the new
    comment's editable node id for subsequent same-line updates.

    Returns ``(new_comment_node_id, posted_body)`` on success, or ``None`` if the
    post or the follow-up lookup failed (the caller then falls back to posting the
    finding fresh).
    """
    path = comment.get("path") or ""
    line = comment.get("line")
    side = comment.get("side") or "RIGHT"
    body = comment.get("body") or ""
    try:
        gh_pr_review_post(
            pr_number,
            [{"path": path, "line": line, "side": side, "body": body}],
            summary="",
            dedupe_existing=False,
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        logger.warning(
            "PR #%s: could not post editable shadow comment on %s:%s (%s): %s",
            pr_number,
            path,
            line,
            side,
            exc,
        )
        return None
    # Re-fetch the index to discover OUR new comment's editable node id. Prefer an
    # editable entry for this exact line; that is the comment we just posted.
    refreshed = gh_pr_inline_comment_index(pr_number)
    entry = refreshed.get((path, line, side)) or refreshed.get((path, line))
    if entry is None or not entry[2]:
        logger.warning(
            "PR #%s: posted editable shadow comment on %s:%s (%s) but could not relocate "
            "its editable node id",
            pr_number,
            path,
            line,
            side,
        )
        return None
    return entry[0], entry[1]


def _edit_or_keep_comments(pr_number: int, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Edit comments whose line already has an UNRESOLVED bot comment; keep the rest.

    For each comment whose ``(path, line)`` matches an UNRESOLVED existing bot
    thread, skip it if the existing body already covers the same finding, else
    rewrite that thread's comment to ``existing_body + new_body`` (a single edit
    preserving the original text, #1085) and drop it from the returned list.

    When the existing thread's first comment is NOT editable — it belongs to
    another app/account such as Copilot or CodeQL (``viewerCanUpdate`` is false,
    or the update mutation reports "Body is not editable") — we do NOT silently
    keep the foreign comment. Instead we post OUR OWN editable shadow comment on
    the same line and from then on edit THAT comment for every re-raise of the
    finding (#1327). The foreign comment is left untouched.

    Findings on fresh lines — including lines whose only prior comment is a
    RESOLVED thread — are returned unchanged for posting. Dedup is deliberately
    NOT done against resolved history (#1152 reversed #1116): a resolved thread
    is supposed to mean the finding was *fixed and verified*, so if the reviewer
    re-raises it the resolution was wrong and the finding MUST re-surface as a
    fresh thread — otherwise a force-resolved-but-unaddressed finding is
    silently suppressed, leaving the GO gate with zero unresolved threads and
    letting the PR converge with the issue still unfixed.

    Fails open: an empty index returns *comments* unchanged, and an edit that
    raises falls back to posting that comment fresh.
    """
    editable_index = gh_pr_inline_comment_index(pr_number)
    wont_fix_lines = gh_pr_wont_fix_line_index(pr_number)
    if not editable_index and not wont_fix_lines:
        return comments
    fresh: list[dict[str, Any]] = []
    for c in comments:
        path = c.get("path") or ""
        line = c.get("line")
        side = c.get("side") or "RIGHT"
        body = c.get("body") or ""

        # WON'T-FIX suppression (#1163): the finding's line was dismissed as
        # intentional-by-design, so do NOT re-post it (that is the whole point of
        # the dismissal — otherwise a re-raised finding stacks a new thread every
        # run since the dismissed thread is resolved, not unresolved).
        if (path, line, side) in wont_fix_lines or (path, line) in wont_fix_lines:
            logger.info(
                "PR #%s: skipped re-raise of won't-fix (intentional-design) finding on %s:%s (%s)",
                pr_number,
                path,
                line,
                side,
            )
            continue

        editable = editable_index.get((path, line, side)) or editable_index.get((path, line))
        if editable is None:
            fresh.append(c)
            continue
        existing_id, existing_body, can_update = editable
        if _review_comment_already_covers(existing_body, body):
            logger.info(
                "PR #%s: skipped duplicate same-line review comment on %s:%s (%s)",
                pr_number,
                path,
                line,
                side,
            )
            continue

        # #1327: the existing comment belongs to another app/account and is not
        # editable. Post our OWN editable shadow comment on the same line, index
        # it, and edit THAT comment on every later re-raise. Leave the foreign
        # comment untouched. ``viewerCanUpdate`` is the primary signal.
        if not can_update:
            shadow = _post_shadow_review_comment(pr_number, c)
            if shadow is None:
                fresh.append(c)
                continue
            new_id, new_body = shadow
            editable_index[(path, line, side)] = (new_id, new_body, True)
            editable_index[(path, line)] = (new_id, new_body, True)
            logger.info(
                "PR #%s: posted editable shadow comment on %s:%s (%s); foreign comment left intact",
                pr_number,
                path,
                line,
                side,
            )
            continue

        # #1085: updatePullRequestReviewComment REPLACES the body, so concatenate
        # the existing body + the new note. Passing only the suffix would destroy
        # the original comment.
        combined = f"{existing_body}{_ADDITIONAL_REVIEW_NOTE_DELIMITER}{body}"
        try:
            gh_pr_update_review_comment(existing_id, combined)
            editable_index[(path, line, side)] = (existing_id, combined, True)
            editable_index[(path, line)] = (existing_id, combined, True)
            logger.info(
                "PR #%s: edited existing comment on %s:%s (%s) instead of duplicating",
                pr_number,
                path,
                line,
                side,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # A deterministic "Body is not editable" means viewerCanUpdate was
            # stale/unavailable; recover by posting our own editable shadow
            # comment (#1327). Any other edit error falls back to posting fresh.
            if _BODY_NOT_EDITABLE_MARKER in str(exc).lower():
                shadow = _post_shadow_review_comment(pr_number, c)
                if shadow is not None:
                    new_id, new_body = shadow
                    editable_index[(path, line, side)] = (new_id, new_body, True)
                    editable_index[(path, line)] = (new_id, new_body, True)
                    logger.info(
                        "PR #%s: edit reported not-editable on %s:%s (%s); posted editable "
                        "shadow comment instead",
                        pr_number,
                        path,
                        line,
                        side,
                    )
                    continue
            logger.warning(
                "PR #%s: edit-in-place failed for %s:%s (%s): %s; posting fresh",
                pr_number,
                path,
                line,
                side,
                exc,
            )
            fresh.append(c)
    return fresh


def gh_pr_review_post(
    pr_number: int,
    comments: list[dict[str, Any]],
    summary: str,
    event: str = "COMMENT",
    dry_run: bool = False,
    dedupe_existing: bool = False,
) -> list[str]:
    """Post a PR review with inline comments via GitHub GraphQL API.

    Args:
        pr_number: PR number
        comments: List of dicts with keys: path (str), line (int), side (str), body (str)
        summary: Overall review summary body
        event: Review event type: COMMENT, APPROVE, or REQUEST_CHANGES
        dry_run: If True, log intent and return empty list without posting
        dedupe_existing: When True (#1083), a comment whose ``(path, line)``
            already has an unresolved bot review comment is EDITED in place
            (the new body is appended) rather than posted as a duplicate thread.
            Only the genuinely new comments are posted as fresh threads. If
            dedupe/editing consumes every inline comment, no summary-only review
            is posted; a duplicate-only re-review should be a no-op, not another
            review submission. Fails open: if the existing-comment index cannot
            be fetched, every comment is posted as before.

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

    # #1083: edit-in-place instead of duplicating. If a comment targets a line
    # that already has an unresolved bot comment, append the new body to that
    # comment and drop it from the to-post set. Fails open (posts everything) if
    # the existing-comment index can't be fetched.
    had_inline_comments = bool(comments)
    if comments and dedupe_existing:
        comments = _edit_or_keep_comments(pr_number, comments)
        if had_inline_comments and not comments:
            logger.info(
                "PR #%s: skipped duplicate-only PR review after existing-line dedupe",
                pr_number,
            )
            return []

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
        List of thread dicts with keys: id (str), path (str), line (int | None),
        side (str), body (str), author (str — the first comment's author login,
        ``""`` if unknown), authors (list[str]), comments (list[dict]).

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
        "        nodes{ id isResolved path line side:diffSide "
        "comments(first:20){ nodes{ body author{ login } } } }"
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
        comment_nodes = node.get("comments", {}).get("nodes", [])
        first_comment = comment_nodes[0] if comment_nodes else {}
        body = first_comment.get("body", "")
        comments: list[dict[str, str]] = []
        authors: list[str] = []
        for comment in comment_nodes:
            comment_author = ""
            author_node = comment.get("author")
            if isinstance(author_node, dict):
                comment_author = author_node.get("login") or ""
            if comment_author:
                authors.append(comment_author)
            comments.append({"body": comment.get("body") or "", "author": comment_author})
        author = authors[0] if authors else ""
        threads.append(
            {
                "id": node["id"],
                "path": node.get("path", ""),
                "line": node.get("line"),
                "side": node.get("side") or "RIGHT",
                "body": body,
                "author": author,
                "authors": authors,
                "comments": comments,
            }
        )

    logger.debug("Found %s unresolved thread(s) on PR #%s", len(threads), pr_number)
    return threads


def gh_pr_resolve_thread(
    thread_id: str,
    reply_body: str | None = None,
    dry_run: bool = False,
) -> None:
    """Resolve a PR review thread, optionally adding a reply first.

    Args:
        thread_id: GraphQL node ID of the review thread
        reply_body: Optional reply comment text. When omitted, the thread is
            resolved without adding another review comment.
        dry_run: If True, log intent without posting

    """
    if dry_run:
        if reply_body:
            logger.info("[dry_run] Would resolve thread %r with reply: %r", thread_id, reply_body)
        else:
            logger.info("[dry_run] Would resolve thread %r without reply", thread_id)
        return

    if reply_body:
        # Step 1: post a reply to the thread via GraphQL addPullRequestReviewThreadReply.
        # NOTE (#999): the deprecated ``addPullRequestReviewComment`` input type has no
        # ``pullRequestReviewThreadId`` field, so it failed on every call. The reply-to-
        # thread surface is ``addPullRequestReviewThreadReply``, whose input accepts
        # ``pullRequestReviewThreadId`` + ``body``.
        reply_mutation = """
mutation AddReply($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
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

    # Resolve the thread.
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
        # #1587: "no checks reported" is the expected empty state right after a
        # push. ``log_on_error=False`` suppresses the spurious ERROR log for that
        # case; the "no checks reported" non-transient pattern (github.client)
        # makes _gh_call fail FAST (no exponential-backoff retry) so we reach the
        # except below immediately. A genuine failure still raises after retries.
        result = _gh_call(
            ["pr", "checks", str(pr_number), "--json", "name,state,bucket,workflow"],
            log_on_error=False,
        )
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
