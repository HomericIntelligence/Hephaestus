"""Pull request management functions for issue implementation.

Provides:
- Committing changes with secret file filtering
- Ensuring PR is created (fallback when Claude doesn't do it)
- Creating pull requests via GitHub CLI
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.runtime import (
    agent_display_name,
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.automation.prompts.catalog import PromptCatalog
from hephaestus.github.auto_merge import defer_auto_merge, defer_auto_merge_batch

from . import commit_runtime as _commit_runtime
from .agent_config import DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT, HAIKU, implementer_model
from .ci_check_inspector import FAILING_CHECK_CONCLUSIONS
from .claude_invoke import invoke_claude_with_session
from .commit_policy import (
    ALLOWED_CONVENTIONAL_TYPES,
    normalize_conventional_type,
    normalize_strict_conventional_title,
)
from .git_runtime import get_repo_slug, issue_ref, run
from .github_api import (
    OpenPrDiscoveryIncompleteError,
    _find_open_prs_for_head,
    _gh_call,
    _select_open_pr_for_base,
    fetch_issue_info,
    gh_issue_add_labels,
    gh_issue_remove_labels,
    gh_pr_create,
)
from .prompts import fence_content, get_pr_description
from .session_naming import AGENT_COMMIT_MESSAGE, AGENT_PR_MESSAGE
from .state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    has_label,
    is_implementation_go,
)
from .status_tracker import StatusTracker

logger = logging.getLogger(__name__)

# Preserve public secret-file names for callers and policy tests.
SECRET_FILE_NAMES = _commit_runtime.SECRET_FILE_NAMES
SECRET_FILE_EXTENSIONS = _commit_runtime.SECRET_FILE_EXTENSIONS

_RESERVED_MESSAGE_LINE = re.compile(
    r"^\s*(?:Closes\s+#\d+|Implemented-By:|Co-Authored-By:)",
    re.IGNORECASE,
)


def _git_timeout_kw(timeout: int | None) -> dict[str, Any]:
    """Return a ``run`` kwargs fragment only when a git timeout was provided."""
    return {} if timeout is None else {"timeout": timeout}


@dataclass(frozen=True)
class _PrMessageParts:
    """Agent-proposed PR text before policy/footer rendering."""

    title: str
    summary: str
    changes: str
    testing: str


def _agent_display_name(agent: str) -> str:
    """Return a short human-facing name for generated commits/PR bodies."""
    return agent_display_name(agent)


def _issue_body(issue: Any) -> str:
    """Return an issue body only when the fetched object carries a string body."""
    body = getattr(issue, "body", "")
    return body if isinstance(body, str) else ""


def _single_line(value: object, *, fallback: str, max_len: int = 120) -> str:
    """Normalize an agent-provided title/subject into one non-empty line."""
    text = str(value or "").strip().splitlines()[0].strip() if value else ""
    if not text:
        return fallback
    return text[:max_len].rstrip()


def _strip_reserved_lines(text: str) -> str:
    """Remove policy/trailer lines that the orchestrator must own."""
    lines = []
    for line in text.splitlines():
        if _RESERVED_MESSAGE_LINE.match(line):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _message_text(value: object) -> str:
    """Normalize an agent JSON string/list field into markdown text."""
    if isinstance(value, list):
        cleaned = [str(item).strip().lstrip("- ").strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in cleaned)
    if isinstance(value, str):
        return value.strip()
    return ""


def _parse_agent_json(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from raw agent output, tolerating fenced prose."""
    raw = (text or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _git_output(
    worktree_path: Path,
    args: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Return best-effort git output for message-agent context."""
    try:
        run_kwargs: dict[str, Any] = {
            "capture_output": True,
            "check": False,
            **_git_timeout_kw(timeout),
        }
        if env is not None:
            run_kwargs["env"] = env
        result = run(
            ["git", *args],
            cwd=worktree_path,
            **run_kwargs,
        )
    except Exception as exc:
        logger.debug("Could not collect git message context for %s: %s", args, exc)
        return ""
    return (result.stdout or "").strip()


def _branch_change_context(worktree_path: Path, base: str) -> tuple[str, str, str]:
    """Return changed files, diff stat, and commits for PR-message generation."""
    ranges = (f"origin/{base}..HEAD", f"{base}..HEAD")
    for revision_range in ranges:
        changed_files = _git_output(worktree_path, ["diff", "--name-status", revision_range])
        diff_stat = _git_output(worktree_path, ["diff", "--stat", revision_range])
        commits = _git_output(worktree_path, ["log", "--oneline", revision_range])
        if changed_files or diff_stat or commits:
            return changed_files, diff_stat, commits
    return "", "", ""


def _pr_message_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    changed_files: str,
    diff_stat: str,
    commits: str,
) -> str:
    """Build a read-only prompt for the PR-message agent."""
    fenced = fence_content()
    return PromptCatalog.current().render(
        "pr_management/pr_message.j2",
        allowed_types=", ".join(sorted(ALLOWED_CONVENTIONAL_TYPES)),
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body or "(empty)"),
        changed_files_block=fenced.fence("CHANGED_FILES", changed_files or "(none reported)"),
        diff_stat_block=fenced.fence("DIFF_STAT", diff_stat or "(none reported)"),
        commits_block=fenced.fence("COMMITS", commits or "(none reported)"),
        untrusted_notice=fenced.untrusted_notice,
    )


def _invoke_git_message_agent(
    *,
    issue_number: int,
    agent_kind: str,
    prompt: str,
    worktree_path: Path,
    agent: str,
    timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    model_override: str | None = None,
    pi_dir: Path | None = None,
) -> str:
    """Run the lightweight message agent in a separate read-only session.

    Pipeline callers must provide their CLI-resolved role model.  Claude and
    Codex use the deterministic lightweight-message default when a standalone
    caller omits one; Pi retains its provider-specific default in that case.
    """
    model = (
        model_override
        if model_override is not None
        else direct_agent_model(agent, codex_default=HAIKU)
    )
    if uses_direct_agent_runner(agent):
        result = run_agent_text(
            agent=agent,
            prompt=prompt,
            cwd=worktree_path,
            timeout=timeout,
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER, AgentOperation.GIT_MESSAGE, SessionLifecycle.ONE_SHOT
            ),
            model=model,
            sandbox="read-only",
            pi_dir=pi_dir,
        )
        return (result.stdout or "").strip()

    stdout, _ = invoke_claude_with_session(
        repo=get_repo_slug(worktree_path),
        issue=issue_number,
        agent=agent_kind,
        prompt=prompt,
        model=model,
        cwd=worktree_path,
        timeout=timeout,
        output_format="text",
        allowed_tools="Read,Glob,Grep",
    )
    return (stdout or "").strip()


def _invoke_claude_commit_message(
    issue_number: int,
    prompt: str,
    worktree_path: Path,
    agent: str,
    timeout: int,
    model: str,
    pi_dir: Path | None,
) -> str:
    """Adapt a product Claude session to the neutral commit contract."""
    return _invoke_git_message_agent(
        issue_number=issue_number,
        agent_kind=AGENT_COMMIT_MESSAGE,
        prompt=prompt,
        worktree_path=worktree_path,
        agent=agent,
        timeout=timeout,
        model_override=model,
        pi_dir=pi_dir,
    )


def _fallback_pr_message(issue_number: int, issue_title: str, agent: str) -> _PrMessageParts:
    """Return deterministic PR text when the message agent is unavailable."""
    return _PrMessageParts(
        title=f"feat: {issue_title}",
        summary=f"Implements #{issue_number}",
        changes=f"- Automated implementation via {_agent_display_name(agent)}",
        testing="- Test commands were not recorded by automation.",
    )


def _generate_pr_message(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    branch_name: str,
    base: str,
    worktree_path: Path | None,
    agent: str,
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    agent_model: str | None = None,
    pi_dir: Path | None = None,
) -> _PrMessageParts:
    """Generate PR text via a lightweight agent with deterministic fallback."""
    fallback = _fallback_pr_message(issue_number, issue_title, agent)
    if worktree_path is None:
        return fallback

    changed_files, diff_stat, commits = _branch_change_context(worktree_path, base)
    prompt = _pr_message_prompt(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        changed_files=changed_files,
        diff_stat=diff_stat,
        commits=commits,
    )
    try:
        raw = _invoke_git_message_agent(
            issue_number=issue_number,
            agent_kind=AGENT_PR_MESSAGE,
            prompt=prompt,
            worktree_path=worktree_path,
            agent=agent,
            timeout=git_message_timeout,
            model_override=agent_model,
            pi_dir=pi_dir,
        )
        data = _parse_agent_json(raw)
        if data is None:
            raise ValueError("message agent returned no JSON object")
        return _PrMessageParts(
            # #1587: normalize the PR title's conventional-commit type too — a
            # squash merge uses the PR title as the commit subject, so a forbidden
            # type here fails pr-policy exactly like a commit subject does.
            title=normalize_conventional_type(
                _single_line(data.get("title"), fallback=fallback.title, max_len=120)
            ),
            summary=_strip_reserved_lines(_message_text(data.get("summary"))) or fallback.summary,
            changes=_strip_reserved_lines(_message_text(data.get("changes"))) or fallback.changes,
            testing=_strip_reserved_lines(_message_text(data.get("testing"))) or fallback.testing,
        )
    except Exception as exc:
        logger.warning(
            "PR-message agent failed for %s on branch %s; using fallback body (%s)",
            issue_ref(issue_number),
            branch_name,
            exc,
        )
        return fallback


def _detect_default_base_branch(worktree_path: Path) -> str:
    """Return the repo default branch name for PR/signature ranges."""
    result = run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        cwd=worktree_path,
        capture_output=True,
        check=False,
    )
    ref = (result.stdout or "").strip()
    if ref.startswith("origin/") and len(ref) > len("origin/"):
        return ref.removeprefix("origin/")
    return "main"


def _branch_has_commits_vs_base(branch_name: str, base: str, worktree_path: Path) -> bool:
    """Return True if *branch_name* has at least one commit not on *base*.

    ``git log -1`` only proves a commit exists somewhere on the branch; it stays
    green when the agent's commits are identical to (or already merged into)
    base, in which case ``gh pr create`` fails with the opaque
    ``No commits between main and <branch>`` GraphQL error — six times, since the
    caller retries. Counting ``origin/<base>..<branch>`` (falling back to a local
    ``<base>..<branch>`` range when ``origin/<base>`` is unknown) lets us detect
    the empty-diff case up front and report it cleanly instead.
    """
    for ref in (f"origin/{base}..{branch_name}", f"{base}..{branch_name}"):
        result = run(
            ["git", "rev-list", "--count", ref],
            cwd=worktree_path,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return int((result.stdout or "0").strip() or "0") > 0
    # Could not evaluate either range (e.g. base ref missing locally). Be
    # permissive: let PR creation proceed rather than block on a check failure.
    return True


def ensure_pr_auto_merge_deferred(pr_number: int) -> None:
    """Disable auto-merge and verify it remains disabled while the PR is open."""
    if not defer_auto_merge(pr_number, lambda args: _gh_call(args, check=False)):
        raise RuntimeError(f"could not verify auto-merge disabled for PR #{pr_number}")


def _find_and_contain_open_prs(branch_name: str) -> list[tuple[int, str]]:
    """Contain known head PRs before rejecting an incomplete discovery result."""
    discovery_error: OpenPrDiscoveryIncompleteError | None = None
    try:
        open_prs = _find_open_prs_for_head(branch_name)
    except OpenPrDiscoveryIncompleteError as exc:
        open_prs = exc.open_prs
        discovery_error = exc
    except Exception as exc:
        raise RuntimeError(
            f"could not verify existing PR state for branch {branch_name!r}"
        ) from exc

    containment_failures = defer_auto_merge_batch(
        (open_pr_number for open_pr_number, _open_pr_base in open_prs),
        ensure_pr_auto_merge_deferred,
    )
    if containment_failures:
        raise RuntimeError(
            "could not verify auto-merge disabled for existing PR(s): "
            + "; ".join(containment_failures)
        )
    if discovery_error is not None:
        raise RuntimeError(
            f"could not verify existing PR state for branch {branch_name!r}"
        ) from discovery_error
    return open_prs


def mark_pr_implementation_go(pr_number: int) -> None:
    """Mark a PR as implementation-review GO."""
    gh_issue_add_labels(pr_number, [STATE_IMPLEMENTATION_GO])
    gh_issue_remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])


def mark_pr_implementation_no_go(pr_number: int) -> None:
    """Mark a PR as implementation-review not-GO."""
    gh_issue_add_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
    gh_issue_remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])


def pr_has_implementation_go_label(pr: dict[str, object]) -> bool:
    """Return True when a PR dictionary carries the implementation-GO label."""
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return False
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return is_implementation_go(names)


def pr_is_genuinely_stuck(pr_number: int) -> bool:
    """Return True iff a PR genuinely cannot merge without manual/agent action.

    "Genuinely stuck" means a merge CONFLICT (``mergeStateStatus`` DIRTY or
    CONFLICTING, or ``mergeable`` CONFLICTING) OR a red required check
    (any ``statusCheckRollup`` conclusion in
    :data:`~hephaestus.automation.ci_check_inspector.FAILING_CHECK_CONCLUSIONS`).

    Crucially, a PR that is merely **pending implementation review** — green CI,
    unarmed, ``mergeStateStatus == "BLOCKED"`` only because branch protection
    requires a review that has not happened yet — is NOT stuck. Returning False
    for that case is what stops the automation loop from wrongly tagging an
    awaiting-review PR ``state:skip`` (#1576). ``BLOCKED`` alone is deliberately
    NOT treated as stuck here (unlike ``_pr_is_failing`` in the CI driver, which
    intentionally picks BLOCKED PRs up to drive).

    This is the single source of truth shared by the CI driver's needs-action
    partition and the loop runner's skip-ownership guard, fetched LIVE from
    GitHub. Any query/parse failure yields ``False`` (safe default: never
    misclassify an unknown PR as stuck and strand it).

    Args:
        pr_number: GitHub PR number.

    Returns:
        True if the PR is conflicting or has a red required check; False for a
        green/pending/awaiting-review PR or on any lookup failure.

    """
    try:
        result = _gh_call(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "mergeStateStatus,mergeable,statusCheckRollup",
            ],
            check=False,
        )
        pr = cast(dict[str, object], json.loads(result.stdout or "{}"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not fetch PR #%s state for stuck-check: %s", pr_number, exc)
        return False

    merge_state = str(pr.get("mergeStateStatus") or "").upper()
    mergeable = str(pr.get("mergeable") or "").upper()
    if merge_state in {"DIRTY", "CONFLICTING"} or mergeable == "CONFLICTING":
        return True

    rollup = pr.get("statusCheckRollup")
    if isinstance(rollup, list):
        return any(
            isinstance(check, dict) and check.get("conclusion") in FAILING_CHECK_CONCLUSIONS
            for check in rollup
        )
    return False


def _pr_label_names(pr_number: int) -> list[str]:
    """Return the label names on a PR by number, best-effort.

    Fetches ``gh pr view <n> --json labels`` and normalizes the ``labels``
    array (each entry is a ``{"name": ...}`` dict) to a flat list of names.
    Any subprocess or JSON failure yields an empty list so callers can treat a
    fetch error as "no labels" without raising.
    """
    try:
        result = _gh_call(["pr", "view", str(pr_number), "--json", "labels"], check=False)
        pr = cast(dict[str, object], json.loads(result.stdout or "{}"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not fetch PR #%s labels: %s", pr_number, exc)
        return []
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def pr_has_implementation_state_label(pr_number: int) -> tuple[bool, bool]:
    """Return ``(has_go, has_no_go)`` for a PR's implementation-review labels.

    Used to decide whether an existing PR has already been settled by a prior
    implementation-review pass (either terminal label) so the in-loop review is
    not re-run every loop. Best-effort: a fetch failure yields ``(False, False)``
    so the caller treats it as "not yet reviewed" and proceeds.
    """
    names = _pr_label_names(pr_number)
    return is_implementation_go(names), has_label(names, STATE_IMPLEMENTATION_NO_GO)


def enable_auto_merge_after_implementation_go(pr_number: int) -> None:
    """Refuse legacy direct arming; merge-wait owns conditional merging."""
    ensure_pr_auto_merge_deferred(pr_number)
    raise RuntimeError(
        f"refusing to arm auto-merge for PR #{pr_number}: "
        "native auto-merge is prohibited; merge-wait uses a conditional normal merge"
    )


def commit_changes(
    issue_number: int,
    worktree_path: Path,
    agent: str = "claude",
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    allowed_paths: Collection[str] | None = None,
    git_timeout: int | None = None,
    agent_model: str | None = None,
    *,
    expected_tree_sha: str | None = None,
    return_commit_sha: bool = False,
    signing_env: dict[str, str] | None = None,
    git_env: dict[str, str] | None = None,
    expected_add_paths: tuple[str, ...] | None = None,
    expected_update_paths: tuple[str, ...] | None = None,
    disable_hooks: bool = False,
    pi_dir: Path | None = None,
) -> str | None:
    """Commit changes in worktree, filtering out secret files.

    Args:
        issue_number: Issue number (used in commit message and error text)
        worktree_path: Path to git worktree
        agent: Selected implementation agent. Defaults to Claude for backwards
            compatibility with existing direct callers.
        pi_dir: Operator-global Pi directory for message generation.
        git_message_timeout: Timeout in seconds for the lightweight commit-message
            agent. Defaults to :data:`DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT`.
        allowed_paths: Optional exact set of porcelain paths allowed to be
            staged. Secret filtering still applies.
        git_timeout: Optional timeout in seconds for each local git command.
        agent_model: Explicit model and reasoning effort selected by the
            command line for the message-generation session. When omitted,
            the deterministic lightweight-message default is used.
        signing_env: Optional validated Git environment for commit signing.
        git_env: Optional isolated Git environment for status and staging.
        expected_add_paths: Bounded inspected paths to add without re-enumeration.
        expected_update_paths: Bounded inspected paths to update without re-enumeration.
        disable_hooks: Disable hooks for a host-validated recovery commit.
        expected_tree_sha: Optional immutable tree that staging must produce.
        return_commit_sha: Return the exact new commit SHA after the commit.

    Raises:
        RuntimeError: If there are no changes, or all changes are secret files.

    Returns:
        The exact commit SHA when ``return_commit_sha`` is true. Otherwise,
        return ``None`` after the commit is created.

    """
    issue = fetch_issue_info(issue_number)
    resolved_agent_model = (
        implementer_model() if agent == "claude" and agent_model is None else agent_model
    )
    return _commit_runtime.commit_changes(
        _commit_runtime.CommitIssueMetadata(issue_number, issue.title, _issue_body(issue)),
        worktree_path,
        agent,
        git_message_timeout,
        allowed_paths,
        git_timeout,
        resolved_agent_model,
        expected_tree_sha=expected_tree_sha,
        return_commit_sha=return_commit_sha,
        signing_env=signing_env,
        git_env=git_env,
        expected_add_paths=expected_add_paths,
        expected_update_paths=expected_update_paths,
        disable_hooks=disable_hooks,
        pi_dir=pi_dir,
        claude_message_agent=_invoke_claude_commit_message,
    )


def ensure_pr_created(
    issue_number: int,
    branch_name: str,
    worktree_path: Path,
    auto_merge: bool = False,
    status_tracker: StatusTracker | None = None,
    slot_id: int | None = None,
    agent: str = "claude",
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    agent_model: str | None = None,
    pi_dir: Path | None = None,
) -> int:
    """Ensure the implementation commit is pushed and a PR exists.

    Args:
        issue_number: Issue number
        branch_name: Git branch name
        worktree_path: Path to worktree
        auto_merge: Deprecated compatibility flag; ignored. Native auto-merge
            is prohibited; merge-wait conditionally merges reviewed heads.
        status_tracker: StatusTracker instance for slot updates (optional)
        slot_id: Worker slot ID for status updates
        agent: Selected implementation agent for generated PR metadata.
        agent_model: Explicit model and reasoning effort for generated PR
            metadata. When omitted, the deterministic message default is used.
        pi_dir: Operator-global Pi directory for generated PR metadata.
        git_message_timeout: Timeout in seconds for the lightweight PR-message
            agent. Defaults to :data:`DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT`.

    Returns:
        PR number

    Raises:
        RuntimeError: If commit doesn't exist or PR creation fails

    """

    def _update_slot(msg: str) -> None:
        if slot_id is not None and status_tracker is not None:
            status_tracker.update_slot(slot_id, msg)

    # Check if commit exists
    _update_slot(f"{issue_ref(issue_number)}: Checking commit")
    result = run(
        ["git", "log", "-1", "--oneline"],
        cwd=worktree_path,
        capture_output=True,
    )
    if not result.stdout.strip():
        raise RuntimeError(
            f"No commit found for issue {issue_ref(issue_number)}. "
            "No implementation changes were committed."
        )

    logger.info("Commit exists: %s", result.stdout.strip()[:80])

    # Guard against an empty diff vs base. A commit existing on the branch does
    # not mean it differs from base (the agent may have produced no net change,
    # or its work already landed). Opening a PR in that case fails with
    # "No commits between <base> and <branch>" and the caller retries six times.
    # Detect it here and fail with an actionable message instead.
    base_branch = _detect_default_base_branch(worktree_path)
    if not _branch_has_commits_vs_base(branch_name, base_branch, worktree_path):
        raise RuntimeError(
            f"No changes produced for issue {issue_ref(issue_number)}: branch "
            f"{branch_name!r} has no commits vs {base_branch!r}. Skipping PR "
            "creation (the implementation session made no net change)."
        )

    # Check if branch was pushed, if not push it
    _update_slot(f"{issue_ref(issue_number)}: Pushing branch")
    result = run(
        ["git", "ls-remote", "--heads", "origin", branch_name],
        cwd=worktree_path,
        capture_output=True,
        check=False,
    )
    if not result.stdout.strip():
        logger.warning("Branch %s not pushed, pushing now...", branch_name)
        run(["git", "push", "-u", "origin", branch_name], cwd=worktree_path)
        logger.info("Pushed branch %s to origin", branch_name)
    else:
        logger.info("Branch %s already on origin", branch_name)

    # Check if PR exists, if not create it
    _update_slot(f"{issue_ref(issue_number)}: Creating PR")
    open_prs = _find_and_contain_open_prs(branch_name)
    try:
        pr_number = _select_open_pr_for_base(open_prs, base_branch)
    except Exception as e:
        raise RuntimeError(f"could not verify existing PR state for branch {branch_name!r}") from e
    if pr_number is not None:
        logger.info("PR #%s already exists", pr_number)
        return pr_number

    # PR doesn't exist, create it
    logger.warning("No PR found for branch %s, creating one...", branch_name)
    if auto_merge:
        logger.info(
            "Keeping auto-merge disabled for branch %s until the PR-review gate exists",
            branch_name,
        )
    if agent_model is None:
        pr_number = create_pr(
            issue_number,
            branch_name,
            auto_merge=False,
            agent=agent,
            base=base_branch,
            worktree_path=worktree_path,
            git_message_timeout=git_message_timeout,
            pi_dir=pi_dir,
        )
    else:
        pr_number = create_pr(
            issue_number,
            branch_name,
            auto_merge=False,
            agent=agent,
            base=base_branch,
            worktree_path=worktree_path,
            git_message_timeout=git_message_timeout,
            agent_model=agent_model,
            pi_dir=pi_dir,
        )
    logger.info("Created PR #%s", pr_number)
    return pr_number


def create_pr(
    issue_number: int,
    branch_name: str,
    auto_merge: bool = False,
    agent: str = "claude",
    base: str = "main",
    worktree_path: Path | None = None,
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    agent_model: str | None = None,
    pi_dir: Path | None = None,
) -> int:
    """Create pull request for issue.

    Args:
        issue_number: Issue number
        branch_name: Git branch name
        auto_merge: Deprecated compatibility flag; ignored. Native auto-merge
            is prohibited; merge-wait conditionally merges reviewed heads.
        agent: Selected implementation agent for generated PR metadata.
        agent_model: Explicit model and reasoning effort for generated PR
            metadata. When omitted, the deterministic message default is used.
        pi_dir: Operator-global Pi directory for generated PR metadata.
        base: Base branch used for changed-file and commit context.
        worktree_path: Optional worktree path used to invoke the lightweight
            PR-message agent. When omitted, deterministic fallback text is used.
        git_message_timeout: Timeout in seconds for the lightweight PR-message
            agent. Defaults to :data:`DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT`.

    Returns:
        PR number

    """
    issue = fetch_issue_info(issue_number)

    pr_message = _generate_pr_message(
        issue_number=issue_number,
        issue_title=issue.title,
        issue_body=_issue_body(issue),
        branch_name=branch_name,
        base=base,
        worktree_path=worktree_path,
        agent=agent,
        agent_model=agent_model,
        pi_dir=pi_dir,
        git_message_timeout=git_message_timeout,
    )
    pr_title = normalize_strict_conventional_title(pr_message.title)
    pr_body = get_pr_description(
        issue_number=issue_number,
        summary=pr_message.summary,
        changes=pr_message.changes,
        testing=pr_message.testing,
        generated_by=f"{_agent_display_name(agent)} via Hephaestus automation.",
    )

    if auto_merge:
        logger.warning(
            "Ignoring auto_merge=True for branch %s while the PR-review gate is unavailable",
            branch_name,
        )
    return gh_pr_create(
        branch=branch_name,
        title=pr_title,
        body=pr_body,
        auto_merge=False,
        base=base,
    )
