"""Conflict detection and agent-assisted resolution for fleet sync."""

from __future__ import annotations

import contextlib
from pathlib import Path

from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.constants import agent_rebase_timeout
from hephaestus.github.fleet_sync.git_ops import (
    _git,
    add_pr_worktree,
    ensure_repo_clone,
    remove_worktree,
)
from hephaestus.github.fleet_sync.gpg import get_resign_email, get_resign_exec
from hephaestus.github.fleet_sync.models import UNICODE_SYMBOLS, PRInfo, Symbols
from hephaestus.github.git_ops import (
    git_ls_remote_contains,
    git_rev_list_count,
    git_unmerged_files,
    run_git,
)
from hephaestus.logging.utils import get_logger
from hephaestus.utils.helpers import NETWORK_TIMEOUT

logger = get_logger(__name__)


def _run_conflict_agent(agent: str, prompt: str, work: Path, pr_number: int) -> bool:
    """Run the selected conflict-resolution agent."""
    if uses_direct_agent_runner(agent):
        result = run_agent_text(
            agent=agent,
            prompt=prompt,
            cwd=work,
            timeout=agent_rebase_timeout(),
            model=direct_agent_model(agent, "HEPH_IMPLEMENTER_MODEL"),
            sandbox="workspace-write",
        )
        if result.stdout:
            logger.debug("  agent: %s", result.stdout[:200])
        return True

    try:
        from claude_code_sdk import ClaudeCodeOptions, query
    except ImportError:
        logger.warning(
            "claude_code_sdk not available — skipping agent resolution for PR #%d. "
            "Install with: pip install claude-code-sdk",
            pr_number,
        )
        return False

    options = ClaudeCodeOptions(max_turns=30, cwd=str(work))

    async def _drain() -> None:
        async for message in query(prompt=prompt, options=options):
            text = getattr(message, "text", None) or str(message)
            if text:
                logger.debug("  agent: %s", text[:200])

    import asyncio

    asyncio.run(_drain())
    return True


def resolve_conflict_with_agent(
    pr: PRInfo,
    org: str,
    repo_clone: Path,
    dry_run: bool = False,
    agent: str = "claude",
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
) -> bool:
    """Spawn the selected agent to semantically resolve merge conflicts, then re-sign."""
    branch = pr.head_ref
    base = pr.base_ref
    work = repo_clone.parent / f"{pr.repo}-{pr.number}-conflict"

    try:
        repo_clone = ensure_repo_clone(pr.repo, org, repo_clone.parent, dry_run=False)
        add_pr_worktree(repo_clone, work, branch, base, dry_run=False)

        run_git(
            ["rebase", f"origin/{base}"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            timeout=NETWORK_TIMEOUT,
        )

        conflict_files = git_unmerged_files(work)

        if not conflict_files:
            _git(["rebase", "--continue"], cwd=work, dry_run=False, check=False)
        else:
            pr.conflict_files = conflict_files
            logger.info("  Conflicted files: %s", ", ".join(conflict_files))

            if dry_run:
                logger.info(
                    "  [dry-run] Would spawn agent to resolve conflicts in %s",
                    conflict_files,
                )
                _git(["rebase", "--abort"], cwd=work, dry_run=False, check=False)
                return False

            conflict_list = "\n".join(f"- {f}" for f in conflict_files)
            commit_count = str(git_rev_list_count(work, f"origin/{base}..HEAD"))
            resign_email = get_resign_email()
            resign_exec = get_resign_exec()

            prompt = f"""You are resolving merge conflicts in a git rebase.

Repository: {org}/{pr.repo}
PR: #{pr.number} — "{pr.title}"
Branch `{branch}` is being rebased onto `origin/{base}`.
Working directory: {work}

Conflicted files:
{conflict_list}

For each conflicted file:
1. Read the file — it contains conflict markers (<<<<<<<, =======, >>>>>>>)
2. Understand BOTH sides semantically — do not simply pick one side
3. Write the correctly merged content preserving the intent of both sides
4. Stage the file: git add <file>

After ALL conflicts are resolved:
1. Continue the rebase: git -c user.email={resign_email} rebase --continue
   (repeat if more conflicts appear)
2. Re-sign all commits:
   git rebase HEAD~{commit_count} --exec '{resign_exec}'
3. Push: git push --force-with-lease origin {branch}

Rules:
- Never use `git rebase --skip` or discard either side without understanding it
- Never use `git checkout --ours/--theirs` without reading both sides first
- For generated/lock files, prefer the incoming (theirs) side
- All commits must be GPG-signed (-S flag)
"""
            logger.info(
                "  Spawning %s agent to resolve %d conflict(s)...",
                agent,
                len(conflict_files),
            )
            if not _run_conflict_agent(agent, prompt, work, pr.number):
                return False

        if git_ls_remote_contains(work, "origin", branch):
            logger.info("  %s Conflict resolved and pushed for PR #%d", symbols.check, pr.number)
            return True

        logger.warning("  Agent did not push branch for PR #%d", pr.number)
        return False

    except Exception as e:
        logger.error("  Conflict resolution failed for PR #%d: %s", pr.number, e)
        with contextlib.suppress(Exception):
            _git(["rebase", "--abort"], cwd=work, dry_run=False, check=False)
        return False
    finally:
        remove_worktree(repo_clone, work, dry_run=dry_run)
