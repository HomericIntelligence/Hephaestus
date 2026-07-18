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

from hephaestus.agents.runtime import (
    agent_display_name,
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.automation.prompts.catalog import PromptCatalog
from hephaestus.github.auto_merge import defer_auto_merge, defer_auto_merge_batch

from .agent_config import DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT
from .ci_check_inspector import FAILING_CHECK_CONCLUSIONS
from .claude_invoke import invoke_claude_with_session
from .claude_models import git_message_model, implementer_model
from .git_utils import get_repo_slug, issue_ref, run
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
from .prompts import get_pr_description
from .session_naming import AGENT_COMMIT_MESSAGE, AGENT_PR_MESSAGE
from .state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    has_label,
    is_implementation_go,
)
from .status_tracker import StatusTracker

logger = logging.getLogger(__name__)

# Shared secret-file detection constants. These patterns identify files that
# should never be staged or committed during automated workflows.
# Exact basenames that are always considered secrets regardless of extension.
SECRET_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".secret",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

# File extensions whose presence indicates a cryptographic key or certificate.
SECRET_FILE_EXTENSIONS: frozenset[str] = frozenset({".key", ".pem", ".pfx", ".p12"})

# Valid ``git status --porcelain=v1`` status pairs for this command. ``!!``
# is intentionally excluded because this call does not request ignored paths.
_PORCELAIN_STATUS_PAIRS: frozenset[str] = frozenset(
    {"D ", "DD", "AU", "UD", "UA", "DU", "AA", "UU", "??"}
    | {f" {worktree_status}" for worktree_status in "AMDTRC"}
    | {f"{index_status}{worktree_status}" for index_status in "MTARC" for worktree_status in " MTD"}
)

_RESERVED_MESSAGE_LINE = re.compile(
    r"^\s*(?:Closes\s+#\d+|Implemented-By:|Co-Authored-By:)",
    re.IGNORECASE,
)
_GIT_MESSAGE_MODEL_ENV = "HEPH_GIT_MESSAGE_MODEL"
_AGENT_COMMIT_IDENTITIES = {
    "claude": ("Claude Code", "noreply@anthropic.com"),
    "codex": ("Codex", "noreply@openai.com"),
    "pi": ("Pi", "noreply@earendil.works"),
}

# Conventional-commit types the ``pr-policy`` CI gate accepts. MUST stay in sync
# with ``ALLOWED_TYPES`` in ``scripts/check_conventional_commit.py`` (the gate's
# source of truth); ``tests/.../test_pr_manager.py`` asserts the two sets match,
# so drift fails CI rather than silently re-introducing #1587 (the automation
# generating a ``security(audit):`` prefix the gate then rejects). The script is
# not imported here to avoid inverting the scripts→library dependency direction.
ALLOWED_CONVENTIONAL_TYPES = frozenset(
    {"feat", "fix", "docs", "refactor", "test", "chore", "ci", "build", "perf", "style", "revert"}
)

# Leading ``type(scope)?: `` token of a conventional-commit subject. Scope is
# optional; only the bare type is validated against the allowlist.
_CONVENTIONAL_PREFIX = re.compile(r"^(?P<type>[a-z]+)(?P<scope>\([^)]*\))?(?P<bang>!)?:\s")


def _git_timeout_kw(timeout: int | None) -> dict[str, Any]:
    """Return a ``run`` kwargs fragment only when a git timeout was provided."""
    return {} if timeout is None else {"timeout": timeout}


def _clear_local_committer_identity(worktree_path: Path, git_timeout: int | None) -> None:
    """Drop any worktree/local ``user.email``/``user.name`` override before committing.

    A sub-agent that ran ``git config user.email <fabricated>`` in the worktree
    would otherwise poison the orchestrator's commit, since git resolves local
    config ahead of the operator's global, GitHub-verifiable identity — producing
    ``no_user`` commits that fail the signed-commit policy gate (issue #2110).
    ``git config --unset --local`` returns exit 5 when the key is absent, the
    expected no-op case, so ``check=False``; ``log_errors=False`` keeps that
    common case from emitting spurious ERROR logs on every commit.
    """
    for key in ("user.email", "user.name"):
        run(
            ["git", "config", "--unset", "--local", key],
            cwd=worktree_path,
            check=False,
            log_errors=False,
            **_git_timeout_kw(git_timeout),
        )


def _normalize_conventional_type(subject: str, *, default: str = "chore") -> str:
    """Rewrite a subject's leading type to an allowlisted one if it is not already.

    The commit/PR-message agent can emit a ``type(scope):`` whose type the
    pr-policy gate forbids (e.g. ``security(audit):``), which blocks the PR and
    triggers an expensive self-cleanup (#1587). This normalizes ONLY the leading
    type token — scope, ``!``, and the description are preserved — to ``default``
    when the type is not in :data:`ALLOWED_CONVENTIONAL_TYPES`. A subject with no
    recognizable conventional prefix is prefixed with ``f"{default}: "`` so the
    gate always sees a valid type.

    Args:
        subject: The one-line commit/PR subject from the agent.
        default: The fallback type (must be in the allowlist).

    Returns:
        A subject whose leading type is allowlisted.

    """
    match = _CONVENTIONAL_PREFIX.match(subject)
    if match is None:
        return f"{default}: {subject.strip()}" if subject.strip() else f"{default}: update"
    if match.group("type") in ALLOWED_CONVENTIONAL_TYPES:
        return subject
    scope = match.group("scope") or ""
    bang = match.group("bang") or ""
    rest = subject[match.end() :]
    return f"{default}{scope}{bang}: {rest}"


@dataclass(frozen=True)
class _CommitMessageParts:
    """Agent-proposed commit message content before policy trailers."""

    subject: str
    body: str


@dataclass(frozen=True)
class _CommitPaths:
    """Paths classified for ordinary and index-update staging."""

    add_paths: tuple[str, ...]
    update_paths: tuple[str, ...]


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


def _coauthor_for_agent(agent: str) -> tuple[str, str]:
    """Return the co-author identity for fallback commits made by automation.

    Returns a stable, human-shaped (name, email) pair suitable for the
    ``Co-Authored-By:`` git trailer. Model identifiers are intentionally NOT
    placed in the name slot — see ``_provenance_for_agent`` for that.
    """
    return _AGENT_COMMIT_IDENTITIES.get(agent, _AGENT_COMMIT_IDENTITIES["claude"])


def _provenance_for_agent(agent: str) -> str:
    """Return the value for an ``Implemented-By:`` trailer.

    For Claude agents this is the resolved model id (honoring
    ``HEPH_IMPLEMENTER_MODEL``); for direct agents it is the provider display name.
    """
    if uses_direct_agent_runner(agent):
        return agent_display_name(agent)
    return implementer_model()


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


def _git_output(worktree_path: Path, args: list[str], *, timeout: int | None = None) -> str:
    """Return best-effort git output for message-agent context."""
    try:
        result = run(
            ["git", *args],
            cwd=worktree_path,
            capture_output=True,
            check=False,
            **_git_timeout_kw(timeout),
        )
    except Exception as exc:
        logger.debug("Could not collect git message context for %s: %s", args, exc)
        return ""
    return (result.stdout or "").strip()


def _staged_change_context(worktree_path: Path, *, timeout: int | None = None) -> tuple[str, str]:
    """Return staged changed files and diff stat for commit-message generation."""
    return (
        _git_output(worktree_path, ["diff", "--cached", "--name-status"], timeout=timeout),
        _git_output(worktree_path, ["diff", "--cached", "--stat"], timeout=timeout),
    )


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


def _commit_message_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    changed_files: str,
    diff_stat: str,
) -> str:
    """Build a read-only prompt for the commit-message agent."""
    return PromptCatalog.current().render(
        "pr_management/commit_message.j2",
        allowed_types=", ".join(sorted(ALLOWED_CONVENTIONAL_TYPES)),
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body or "(empty)",
        changed_files=changed_files or "(none reported)",
        diff_stat=diff_stat or "(none reported)",
    )


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
    return PromptCatalog.current().render(
        "pr_management/pr_message.j2",
        allowed_types=", ".join(sorted(ALLOWED_CONVENTIONAL_TYPES)),
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body or "(empty)",
        changed_files=changed_files or "(none reported)",
        diff_stat=diff_stat or "(none reported)",
        commits=commits or "(none reported)",
    )


def _invoke_git_message_agent(
    *,
    issue_number: int,
    agent_kind: str,
    prompt: str,
    worktree_path: Path,
    agent: str,
    timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
) -> str:
    """Run the lightweight message agent in a separate read-only session."""
    if uses_direct_agent_runner(agent):
        result = run_agent_text(
            agent=agent,
            prompt=prompt,
            cwd=worktree_path,
            timeout=timeout,
            model=direct_agent_model(agent, _GIT_MESSAGE_MODEL_ENV),
            sandbox="read-only",
        )
        return (result.stdout or "").strip()

    stdout, _ = invoke_claude_with_session(
        repo=get_repo_slug(worktree_path),
        issue=issue_number,
        agent=agent_kind,
        prompt=prompt,
        model=git_message_model(),
        cwd=worktree_path,
        timeout=timeout,
        output_format="text",
        allowed_tools="Read,Glob,Grep",
    )
    return (stdout or "").strip()


def _format_commit_message(
    *,
    issue_number: int,
    agent: str,
    subject: str,
    body: str,
) -> str:
    """Render the final commit message with orchestrator-owned policy trailers."""
    coauthor_name, coauthor_email = _coauthor_for_agent(agent)
    provenance = _provenance_for_agent(agent)
    clean_body = _strip_reserved_lines(body)
    body_block = f"\n\n{clean_body}" if clean_body else ""
    return f"""{subject}{body_block}

Closes #{issue_number}

Implemented-By: {provenance}
Co-Authored-By: {coauthor_name} <{coauthor_email}>
"""


def _fallback_commit_message(issue_number: int, issue_title: str, agent: str) -> str:
    """Return the deterministic commit message used when agent output is invalid."""
    return _format_commit_message(
        issue_number=issue_number,
        agent=agent,
        subject=f"feat: Implement #{issue_number}",
        body=issue_title,
    )


def _generate_commit_message(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    worktree_path: Path,
    agent: str,
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    git_timeout: int | None = None,
) -> str:
    """Generate a commit message via a lightweight agent with deterministic fallback."""
    changed_files, diff_stat = _staged_change_context(worktree_path, timeout=git_timeout)
    prompt = _commit_message_prompt(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        changed_files=changed_files,
        diff_stat=diff_stat,
    )
    try:
        raw = _invoke_git_message_agent(
            issue_number=issue_number,
            agent_kind=AGENT_COMMIT_MESSAGE,
            prompt=prompt,
            worktree_path=worktree_path,
            agent=agent,
            timeout=git_message_timeout,
        )
        data = _parse_agent_json(raw)
        if data is None:
            raise ValueError("message agent returned no JSON object")
        subject = _single_line(
            data.get("subject"),
            fallback=f"feat: Implement #{issue_number}",
            max_len=120,
        )
        # #1587: the agent can emit a type the pr-policy gate forbids
        # (e.g. ``security(audit):``). Normalize it locally so the automation
        # never produces a commit that fails its own required CI.
        subject = _normalize_conventional_type(subject)
        body = _strip_reserved_lines(_message_text(data.get("body")))
        parts = _CommitMessageParts(subject=subject, body=body)
        return _format_commit_message(
            issue_number=issue_number,
            agent=agent,
            subject=parts.subject,
            body=parts.body,
        )
    except Exception as exc:
        logger.warning(
            "Commit-message agent failed for %s; using fallback message (%s)",
            issue_ref(issue_number),
            exc,
        )
        return _fallback_commit_message(issue_number, issue_title, agent)


def _fallback_pr_message(issue_number: int, issue_title: str, agent: str) -> _PrMessageParts:
    """Return deterministic PR text when the message agent is unavailable."""
    return _PrMessageParts(
        title=f"feat: {issue_title}",
        summary=f"Implements #{issue_number}",
        changes=f"- Automated implementation via {_agent_display_name(agent)}",
        testing="- Automated tests included",
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
        )
        data = _parse_agent_json(raw)
        if data is None:
            raise ValueError("message agent returned no JSON object")
        return _PrMessageParts(
            # #1587: normalize the PR title's conventional-commit type too — a
            # squash merge uses the PR title as the commit subject, so a forbidden
            # type here fails pr-policy exactly like a commit subject does.
            title=_normalize_conventional_type(
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
    """Refuse legacy direct arming; MergeWaitStage is the sole armer."""
    ensure_pr_auto_merge_deferred(pr_number)
    raise RuntimeError(
        f"refusing to arm auto-merge for PR #{pr_number}: only MergeWaitStage may arm"
    )


def _read_porcelain_status(worktree_path: Path, git_timeout: int | None) -> str:
    """Return stable, NUL-delimited porcelain-v1 worktree status."""
    try:
        result = run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=worktree_path,
            capture_output=True,
            **_git_timeout_kw(git_timeout),
        )
    except UnicodeDecodeError as exc:
        raise RuntimeError("Could not decode git status --porcelain=v1 -z output") from exc
    return result.stdout or ""


def _parse_porcelain_status(output: str) -> tuple[tuple[str, str], ...]:
    """Return ``(status, target_path)`` entries from porcelain-v1 ``-z`` output."""
    if not output:
        return ()
    if not output.endswith("\0"):
        raise RuntimeError("Malformed git status --porcelain=v1 -z output")

    records = output[:-1].split("\0")
    if any(not record for record in records):
        raise RuntimeError("Malformed git status --porcelain=v1 -z output")

    entries: list[tuple[str, str]] = []
    index = 0

    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2] != " " or not record[3:]:
            raise RuntimeError("Malformed git status --porcelain=v1 -z output")

        status = record[:2]
        if status not in _PORCELAIN_STATUS_PAIRS:
            raise RuntimeError("Malformed git status --porcelain=v1 -z output")
        entries.append((status, record[3:]))
        index += 1

        if "R" in status or "C" in status:
            if index >= len(records):
                raise RuntimeError("Malformed renamed path in git porcelain output")
            index += 1

    return tuple(entries)


def _is_secret_path(path: str) -> bool:
    """Return whether a repository-relative path matches secret-file policy."""
    filename = Path(path).name
    return filename in SECRET_FILE_NAMES or any(
        filename.endswith(extension) for extension in SECRET_FILE_EXTENSIONS
    )


def _select_commit_paths(
    entries: Collection[tuple[str, str]],
    allowed_paths: Collection[str] | None,
) -> _CommitPaths:
    """Apply exact allowlist and secret policy to parsed status entries."""
    allowed = set(allowed_paths) if allowed_paths is not None else None
    add_paths: list[str] = []
    update_paths: list[str] = []

    for status, path in entries:
        if allowed is not None and path not in allowed:
            logger.debug("Skipping non-allowlisted file: %r", path)
            continue
        if _is_secret_path(path):
            logger.warning("Skipping potential secret file: %r", path)
            continue
        if "D" in status:
            update_paths.append(path)
        else:
            add_paths.append(path)

    return _CommitPaths(tuple(add_paths), tuple(update_paths))


def _stage_commit_paths(
    paths: _CommitPaths,
    worktree_path: Path,
    git_timeout: int | None,
) -> None:
    """Stage selected paths without broad or implicit pathspecs."""
    if paths.update_paths:
        run(
            ["git", "add", "-u", "--", *paths.update_paths],
            cwd=worktree_path,
            **_git_timeout_kw(git_timeout),
        )
    if paths.add_paths:
        run(
            ["git", "add", "--", *paths.add_paths],
            cwd=worktree_path,
            **_git_timeout_kw(git_timeout),
        )


def _commit_with_signature(
    commit_message: str,
    worktree_path: Path,
    git_timeout: int | None,
) -> None:
    """Create the repository-policy signed and DCO-signed commit."""
    run(
        ["git", "commit", "-S", "-s", "-m", commit_message],
        cwd=worktree_path,
        **_git_timeout_kw(git_timeout),
    )


def commit_changes(
    issue_number: int,
    worktree_path: Path,
    agent: str = "claude",
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    allowed_paths: Collection[str] | None = None,
    git_timeout: int | None = None,
) -> None:
    """Commit changes in worktree, filtering out secret files.

    Args:
        issue_number: Issue number (used in commit message and error text)
        worktree_path: Path to git worktree
        agent: Selected implementation agent. Defaults to Claude for backwards
            compatibility with existing direct callers.
        git_message_timeout: Timeout in seconds for the lightweight commit-message
            agent. Defaults to :data:`DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT`.
        allowed_paths: Optional exact set of porcelain paths allowed to be
            staged. Secret filtering still applies.
        git_timeout: Optional timeout in seconds for each local git command.

    Raises:
        RuntimeError: If there are no changes, or all changes are secret files.

    """
    porcelain = _read_porcelain_status(worktree_path, git_timeout)
    if not porcelain:
        raise RuntimeError(
            f"No changes to commit for issue {issue_ref(issue_number)}. "
            "Check if the implementation was successful or if the plan needs revision."
        )

    paths = _select_commit_paths(_parse_porcelain_status(porcelain), allowed_paths)
    if not paths.add_paths and not paths.update_paths:
        raise RuntimeError(
            f"No non-secret files to commit for issue {issue_ref(issue_number)}. "
            "All changes appear to be secret files."
        )

    _stage_commit_paths(paths, worktree_path, git_timeout)

    # Generate commit message
    issue = fetch_issue_info(issue_number)
    commit_message = _generate_commit_message(
        issue_number=issue_number,
        issue_title=issue.title,
        issue_body=_issue_body(issue),
        worktree_path=worktree_path,
        agent=agent,
        git_message_timeout=git_message_timeout,
        git_timeout=git_timeout,
    )

    # Defense-in-depth: strip any fabricated worktree-local identity a sub-agent
    # may have set, so the commit inherits the operator's verifiable identity (#2110).
    _clear_local_committer_identity(worktree_path, git_timeout)

    _commit_with_signature(commit_message, worktree_path, git_timeout)


def ensure_pr_created(
    issue_number: int,
    branch_name: str,
    worktree_path: Path,
    auto_merge: bool = False,
    status_tracker: StatusTracker | None = None,
    slot_id: int | None = None,
    agent: str = "claude",
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
) -> int:
    """Ensure the implementation commit is pushed and a PR exists.

    Args:
        issue_number: Issue number
        branch_name: Git branch name
        worktree_path: Path to worktree
        auto_merge: Deprecated compatibility flag; ignored because only
            MergeWaitStage may conditionally arm after strict review.
        status_tracker: StatusTracker instance for slot updates (optional)
        slot_id: Worker slot ID for status updates
        agent: Selected implementation agent for generated PR metadata.
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
            "Keeping auto-merge disabled for branch %s until the strict-review gate exists",
            branch_name,
        )
    pr_number = create_pr(
        issue_number,
        branch_name,
        auto_merge=False,
        agent=agent,
        base=base_branch,
        worktree_path=worktree_path,
        git_message_timeout=git_message_timeout,
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
) -> int:
    """Create pull request for issue.

    Args:
        issue_number: Issue number
        branch_name: Git branch name
        auto_merge: Deprecated compatibility flag; ignored because only
            MergeWaitStage may conditionally arm after strict review.
        agent: Selected implementation agent for generated PR metadata.
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
        git_message_timeout=git_message_timeout,
    )
    pr_title = pr_message.title
    pr_body = get_pr_description(
        issue_number=issue_number,
        summary=pr_message.summary,
        changes=pr_message.changes,
        testing=pr_message.testing,
        generated_by=f"{_agent_display_name(agent)} via Hephaestus automation.",
    )

    if auto_merge:
        logger.warning(
            "Ignoring auto_merge=True for branch %s while the strict-review gate is unavailable",
            branch_name,
        )
    return gh_pr_create(
        branch=branch_name,
        title=pr_title,
        body=pr_body,
        auto_merge=False,
        base=base,
    )
