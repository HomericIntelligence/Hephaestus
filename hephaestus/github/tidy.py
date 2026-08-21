#!/usr/bin/env python3
"""Single-repo gh-tidy wrapper with agent conflict resolution.

Runs `gh tidy --rebase-all --auto-delete-merged --trunk <default_branch>`, then
spawns the selected coding agent per branch that gh-tidy failed to rebase.

The swarm is constrained: it MUST NOT delete any branch or any worktree that
existed before the run.

Usage:
    hephaestus-tidy [--dry-run] [--trunk BRANCH] [--no-swarm] [--max-concurrent N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    add_agent_argument,
    direct_agent_model,
    reject_pi_unsupported_surface,
    resolve_agent,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_logging_args,
    configure_cli_logging,
    configure_github_throttle_from_args,
    create_parser,
    emit_json_status,
)
from hephaestus.config.child_environments import build_gh_child_env
from hephaestus.github.client import DEFAULT_GH_TIMEOUT, gh_call, positive_timeout
from hephaestus.github.git_ops import (
    in_git_repo as _shared_in_git_repo,
    repo_root as _shared_repo_root,
    run_git,
    working_tree_clean as _shared_working_tree_clean,
)
from hephaestus.github.pr_merge import detect_repo_from_remote
from hephaestus.prompts import PromptCatalog, add_prompt_dir_argument

logger = logging.getLogger(__name__)

# Model the tidy conflict-resolution swarm runs on. Defined locally rather than
# imported from hephaestus.automation.claude_models because hephaestus.github
# must not depend on hephaestus.automation (one-way layering boundary enforced
# by tests/unit/utils/test_no_import_cycles.py). Keep this in sync with the
# SONNET constant there.
_TIDY_SWARM_MODEL = "claude-sonnet-4-6"

# ANSI escape sequence stripper
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Pattern that gh-tidy emits when rebase fails (from gh-tidy lines 297-301)
_PROBLEM_HEADER = re.compile(r"WARNING:\s*Unable to auto-rebase the following branches")
_PROBLEM_BULLET = re.compile(r"^\s*\*\s+(\S+)")


def _detect_default_branch(override: str | None, *, gh_timeout: int = DEFAULT_GH_TIMEOUT) -> str:
    """Return the repo's default branch, using override if supplied."""
    if override:
        return override
    try:
        result = gh_call(
            ["repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
            timeout=gh_timeout,
        )
        branch = result.stdout.strip()
        if branch:
            return branch
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as e:
        logger.warning("Could not detect default branch via gh: %s", getattr(e, "stderr", str(e)))
    return "main"


def _working_tree_clean() -> bool:
    """Return True if the git working tree has no uncommitted changes."""
    try:
        return _shared_working_tree_clean()
    except subprocess.TimeoutExpired as e:
        logger.error("git status timed out: %s", e)
        raise


def _in_git_repo() -> bool:
    """Return True if cwd is inside a git repository."""
    try:
        return _shared_in_git_repo()
    except subprocess.TimeoutExpired as e:
        logger.error("git rev-parse --git-dir timed out: %s", e)
        raise


def _repo_root() -> Path:
    """Return the root directory of the current git repository.

    Note: TimeoutExpired propagates to the sole caller (_validate_environment),
    which invokes this bare without a try/except. This is consistent with the
    CalledProcessError path: both failures propagate as unhandled exceptions to
    the CLI entrypoint.
    """
    return _shared_repo_root()


def _worktree_porcelain() -> str:
    """Return the current repository's NUL-delimited worktree inventory."""
    return run_git(["worktree", "list", "--porcelain", "-z"]).stdout


def _parse_worktree_porcelain(output: str, root: Path) -> list[tuple[Path, str]]:
    """Return attached, non-primary worktrees from NUL-delimited output."""
    worktrees: list[tuple[Path, str]] = []
    primary: Path | None = None
    path: Path | None = None
    branch: str | None = None
    for field in [*output.split("\0"), ""]:
        if field.startswith("worktree "):
            path = Path(field.removeprefix("worktree "))
            if primary is None:
                primary = path
            branch = None
        elif field.startswith("branch refs/heads/"):
            branch = field.removeprefix("branch refs/heads/")
        elif not field:
            if path is not None and branch is not None and path not in {primary, root}:
                worktrees.append((path, branch))
            path = None
            branch = None
    return worktrees


def _issue_is_closed(issue: int, *, gh_timeout: int = DEFAULT_GH_TIMEOUT) -> bool:
    """Return whether *issue* is closed, treating lookup failures as unsafe."""
    try:
        return (
            gh_call(
                ["issue", "view", str(issue), "--json", "state", "--jq", ".state"],
                timeout=gh_timeout,
            ).stdout.strip()
            == "CLOSED"
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as e:
        logger.warning("Could not determine state for issue #%d: %s", issue, e)
        return False


def _branch_is_merged(branch: str, trunk: str) -> bool:
    """Return whether *branch* is already an ancestor of *trunk*."""
    return (
        run_git(
            ["merge-base", "--is-ancestor", branch, trunk],
            check=False,
            log_on_error=False,
        ).returncode
        == 0
    )


def _worktree_is_dirty(path: Path) -> bool:
    """Return whether a worktree has uncommitted changes."""
    return bool(run_git(["status", "--porcelain"], cwd=path).stdout.strip())


def _worktree_is_locked(path: Path, porcelain: str) -> bool:
    """Return whether *path* is locked in the supplied worktree inventory."""
    stanza = False
    for field in [*porcelain.split("\0"), ""]:
        if field.startswith("worktree "):
            stanza = Path(field.removeprefix("worktree ")) == path
        elif not field:
            stanza = False
        elif stanza and field.startswith("locked"):
            return True
    return False


def _remove_worktree(path: Path, branch: str) -> None:
    """Remove a worktree and its local branch after operator confirmation."""
    run_git(["worktree", "remove", str(path)])
    run_git(["branch", "-d", branch], check=False, log_on_error=False)


def _cleanup_stale_worktrees(
    root: Path,
    trunk: str,
    dry_run: bool,
    *,
    gh_timeout: int = DEFAULT_GH_TIMEOUT,
) -> int:
    """Interactively remove clean worktrees for closed issues or merged branches."""
    porcelain = _worktree_porcelain()
    candidates = _parse_worktree_porcelain(porcelain, root)
    stale_count = 0
    for path, branch in candidates:
        match = re.match(r"(\d+)", branch)
        issue = int(match.group(1)) if match else None
        closed_issue = issue is not None and (
            _issue_is_closed(issue)
            if gh_timeout == DEFAULT_GH_TIMEOUT
            else _issue_is_closed(issue, gh_timeout=gh_timeout)
        )
        merged_branch = _branch_is_merged(branch, trunk)
        if not closed_issue and not merged_branch:
            continue

        stale_count += 1
        reason = f"issue #{issue} is closed" if closed_issue else f"merged into {trunk}"
        if _worktree_is_dirty(path):
            logger.warning("Skipping dirty worktree %s (%s)", path, reason)
            continue
        if _worktree_is_locked(path, porcelain):
            logger.warning("Skipping locked worktree %s (%s)", path, reason)
            continue
        if dry_run:
            logger.info("Would remove stale worktree %s (branch %s; %s)", path, branch, reason)
            continue
        prompt = f"Remove stale worktree {path} (branch {branch}; {reason})? [y/N] "
        if input(prompt).lower() == "y":
            _remove_worktree(path, branch)
            logger.info("Removed stale worktree %s", path)
        else:
            logger.info("Kept worktree %s", path)

    if stale_count == 0:
        logger.info("No stale worktrees found.")
    return 0


def parse_problem_branches(output: str) -> list[str]:
    """Extract failed-rebase branch names from gh-tidy stdout.

    gh-tidy emits (lines 297–301 of its source):
        WARNING: Unable to auto-rebase the following branches:
            * branch-a
            * branch-b
    """
    clean = _ANSI.sub("", output)
    branches: list[str] = []
    in_block = False
    for line in clean.splitlines():
        if _PROBLEM_HEADER.search(line):
            in_block = True
            continue
        if in_block:
            m = _PROBLEM_BULLET.match(line)
            if m:
                branches.append(m.group(1))
            elif line.strip() and not line.strip().startswith("*"):
                # Non-bullet non-empty line ends the block
                in_block = False
    return branches


def _run_gh_tidy(trunk: str, dry_run: bool) -> tuple[int, str]:
    """Run gh tidy with unattended merged-branch cleanup.

    Returns (exit_code, combined_output_buffer).
    Output is streamed to the terminal while also being retained for parsing.
    """
    cmd = [
        "gh",
        "tidy",
        "--rebase-all",
        "--auto-delete-merged",
        "--trunk",
        trunk,
        "--skip-gc",
    ]
    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return 0, ""

    logger.info("Running: %s", " ".join(cmd))
    buf: list[str] = []

    # Use Popen so output can be tee'd to the terminal and retained for parsing.
    # Intentionally NOT routed through hephaestus.github.client.gh_call: that
    # adapter captures stdout/stderr and would prevent live progress output.
    with subprocess.Popen(
        cmd,
        stdin=sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=build_gh_child_env(),
    ) as proc:
        assert proc.stdout is not None  # noqa: S101 — Popen with PIPE always sets this
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            buf.append(line)
        proc.wait()

    return proc.returncode, "".join(buf)


def _make_agent_prompt(branch: str, trunk: str, repo_path: Path, repo_slug: str) -> str:
    """Build the per-branch Myrmidon agent prompt."""
    worktree_path = repo_path / ".git" / "worktrees" / f"tidy-{branch}"
    return PromptCatalog.current().render(
        "tidy/rebase_fix.j2",
        branch=branch,
        trunk=trunk,
        repo_path=repo_path,
        repo_slug=repo_slug,
        worktree_path=worktree_path,
    )


def _status_from_agent_text(text: str) -> str | None:
    """Extract a Myrmidon status marker from agent output."""
    if "STATUS:" not in text:
        return None
    match = re.search(r"STATUS:\s*(\S+)", text)
    return match.group(1) if match else None


def _load_claude_swarm() -> tuple[Any, Any] | None:
    """Load Claude SDK objects for swarm dispatch."""
    try:
        from claude_code_sdk import ClaudeCodeOptions, query
    except ImportError:
        logger.error(
            "claude_code_sdk not available — cannot dispatch swarm. "
            "Install with: pip install claude-code-sdk",
        )
        return None
    return ClaudeCodeOptions, query


def _claude_options(options_factory: Any, repo_path: Path) -> object:
    """Construct Claude SDK options without leaking SDK names into callers."""
    return options_factory(
        max_turns=40,
        cwd=str(repo_path),
        model=_TIDY_SWARM_MODEL,
    )


async def _dispatch_swarm(
    branches: list[str],
    trunk: str,
    repo_path: Path,
    repo_slug: str,
    max_concurrent: int,
    dry_run: bool,
    agent: str,
    rebase_timeout: int = 2400,
) -> dict[str, str]:
    """Spawn the selected coding agent per branch (capped at max_concurrent).

    Returns a dict of branch -> status string.
    """
    claude_swarm = None if uses_direct_agent_runner(agent) else _load_claude_swarm()
    if not uses_direct_agent_runner(agent) and claude_swarm is None:
        return dict.fromkeys(branches, "failed (claude_code_sdk missing)")

    results: dict[str, str] = {}
    sem = asyncio.Semaphore(max_concurrent)

    async def _run_one(branch: str) -> None:
        async with sem:
            prompt = _make_agent_prompt(branch, trunk, repo_path, repo_slug)
            if dry_run:
                logger.info("[dry-run] Would spawn %s agent for branch: %s", agent, branch)
                results[branch] = "dry-run"
                return

            logger.info("Spawning agent for branch: %s", branch)
            if uses_direct_agent_runner(agent):
                results[branch] = await asyncio.to_thread(
                    _run_direct_rebase_agent,
                    agent,
                    prompt,
                    branch,
                    repo_path,
                    rebase_timeout,
                )
                return

            results[branch] = await _run_claude_rebase_agent(
                prompt, branch, repo_path, claude_swarm
            )

    await asyncio.gather(*(_run_one(b) for b in branches))
    return results


def _run_direct_rebase_agent(
    agent: str,
    prompt: str,
    branch: str,
    repo_path: Path,
    timeout: int = 2400,
) -> str:
    """Run one direct rebase-fix agent and return its status marker."""
    try:
        reject_pi_unsupported_surface(
            agent,
            "tidy rebase agents are Pi N/A until Git mutation is host-owned",
        )
        result = run_agent_text(
            agent=agent,
            prompt=prompt,
            cwd=repo_path,
            timeout=timeout,
            model=direct_agent_model(
                agent,
                _TIDY_SWARM_MODEL,
                codex_default=_TIDY_SWARM_MODEL,
            ),
            sandbox="workspace-write",
        )
        text = result.stdout or ""
        logger.debug("[%s] agent: %s", branch, text[:300])
        return _status_from_agent_text(text) or "failed"
    except Exception as e:
        logger.error("[%s] agent exception: %s", branch, e)
        return "failed"


async def _run_claude_rebase_agent(
    prompt: str,
    branch: str,
    repo_path: Path,
    claude_swarm: tuple[Any, Any] | None,
) -> str:
    """Run one Claude SDK rebase-fix agent and return its status marker."""
    if claude_swarm is None:
        return "failed"
    options_factory, query = claude_swarm
    options = _claude_options(options_factory, repo_path)
    status = "failed"
    try:
        async for message in query(prompt=prompt, options=options):
            text = getattr(message, "text", None) or str(message)
            status = _status_from_agent_text(text) or status
            if text:
                logger.debug("[%s] agent: %s", branch, text[:300])
    except Exception as e:
        logger.error("[%s] agent exception: %s", branch, e)
    return status


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = create_parser(
        prog_name="hephaestus-tidy",
        description=(
            "Tidy the current repo's branches and fix failed rebases with a Myrmidon swarm"
        ),
        epilog=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without executing",
    )
    parser.add_argument(
        "--cleanup-stale-worktrees",
        action="store_true",
        help="Interactively remove clean worktrees for closed issues or merged branches",
    )
    parser.add_argument(
        "--trunk",
        metavar="BRANCH",
        help="Trunk branch (default: auto-detected)",
    )
    parser.add_argument(
        "--no-swarm",
        action="store_true",
        help="Skip swarm dispatch; only report failures",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=5,
        metavar="N",
        help="Max parallel swarm agents (default: 5)",
    )
    add_agent_argument(parser)
    add_prompt_dir_argument(parser)
    parser.add_argument(
        "--gh-timeout",
        type=positive_timeout,
        default=DEFAULT_GH_TIMEOUT,
        metavar="SECONDS",
        help=f"per-call GitHub CLI timeout (default: {DEFAULT_GH_TIMEOUT})",
    )
    parser.add_argument(
        "--rebase-timeout",
        type=positive_timeout,
        default=2400,
        metavar="SECONDS",
        help="direct rebase-agent timeout (default: 2400)",
    )
    add_logging_args(parser)
    add_github_throttle_args(parser)
    add_json_arg(parser)
    return parser


def _validate_environment() -> tuple[str, str, Path] | None:
    """Validate cwd is a clean git repo with a detectable GitHub remote.

    Returns (repo_slug, trunk, repo_path) or None on failure.
    """
    if not _in_git_repo():
        logger.error(
            "Not inside a git repository. Run hephaestus-tidy from within a repo clone.",
        )
        return None

    if not _working_tree_clean():
        logger.error(
            "Working tree has uncommitted changes. "
            "Commit or stash them before running hephaestus-tidy.",
        )
        return None

    repo_slug = detect_repo_from_remote()
    if not repo_slug:
        logger.error(
            "Could not detect GitHub repo from git remote. Is 'origin' set to a GitHub URL?",
        )
        return None

    return repo_slug, "", _repo_root()


def _print_summary(results: dict[str, str]) -> int:
    logger.info("\n%s", "=" * 60)
    logger.info("Tidy swarm complete")
    rebased = [b for b, s in results.items() if s == "rebased"]
    subsumed = [b for b, s in results.items() if s == "subsumed"]
    failed = [b for b, s in results.items() if s not in ("rebased", "subsumed", "dry-run")]

    if rebased:
        logger.info("  Rebased (%d): %s", len(rebased), ", ".join(rebased))
    if subsumed:
        logger.info(
            "  Subsumed/already on trunk (%d): %s",
            len(subsumed),
            ", ".join(subsumed),
        )
    if failed:
        logger.warning(
            "  Still failing (%d) — fix manually: %s",
            len(failed),
            ", ".join(failed),
        )
    return 0 if not failed else 1


def _configure_logging(verbose: bool, log_format: str = "text") -> None:
    """Configure CLI logging for tidy output."""
    configure_cli_logging(verbose=verbose, log_format=log_format)


def _emit_tidy_environment_failure(json_output: bool) -> int:
    if json_output:
        emit_json_status(1, message="environment validation failed")
    return 1


class TidyExecutionError(RuntimeError):
    """The underlying `gh tidy` command failed (non-zero exit).

    Raised instead of parsing output that cannot be trusted. A failed rebase
    with a branch checked out in another worktree is the canonical case
    (Athena #103): parsing the partial output and claiming a clean result
    fabricates a successful cleanup.
    """

    def __init__(self, exit_code: int) -> None:
        """Record the failed command's exit code for the caller."""
        super().__init__(f"gh tidy exited with code {exit_code}")
        self.exit_code = exit_code


def _run_tidy_and_find_problem_branches(trunk: str, dry_run: bool) -> list[str]:
    exit_code, output = _run_gh_tidy(trunk, dry_run)
    if exit_code != 0 and not dry_run:
        raise TidyExecutionError(exit_code)
    return parse_problem_branches(output)


def _handle_no_problem_branches(json_output: bool) -> int:
    logger.info("\nAll branches rebased cleanly — no swarm needed.")
    if json_output:
        emit_json_status(0, problem_branches=0)
    return 0


def _handle_no_swarm(problem_branches: list[str], trunk: str, json_output: bool) -> int:
    logger.info("--no-swarm: skipping Myrmidon dispatch. Fix manually:")
    for branch in problem_branches:
        logger.info("  git rebase origin/%s  (on branch %s)", trunk, branch)
    if json_output:
        emit_json_status(1, problem_branches=problem_branches, swarm="skipped")
    return 1


def _handle_dry_run_problem_branches(problem_branches: list[str], json_output: bool) -> int:
    for branch in problem_branches:
        logger.info("[dry-run] Would spawn selected agent for branch: %s", branch)
    if json_output:
        emit_json_status(0, dry_run=True, problem_branches=problem_branches)
    return 0


def _dispatch_tidy_swarm(
    args: argparse.Namespace,
    problem_branches: list[str],
    trunk: str,
    repo_path: Path,
    repo_slug: str,
    agent: str,
) -> int:
    results = asyncio.run(
        _dispatch_swarm(
            problem_branches,
            trunk,
            repo_path,
            repo_slug,
            args.max_concurrent,
            dry_run=args.dry_run,
            agent=agent,
            **({"rebase_timeout": args.rebase_timeout} if args.rebase_timeout != 2400 else {}),
        )
    )
    exit_code = _print_summary(results)
    if args.json:
        emit_json_status(exit_code, results=results)
    return exit_code


def _handle_problem_branches(
    args: argparse.Namespace,
    problem_branches: list[str],
    trunk: str,
    repo_path: Path,
    repo_slug: str,
    agent: str,
) -> int:
    logger.info(
        "\ngh tidy could not rebase %d branch(es): %s",
        len(problem_branches),
        ", ".join(problem_branches),
    )

    if args.no_swarm:
        return _handle_no_swarm(problem_branches, trunk, args.json)

    logger.info(
        "Dispatching Myrmidon swarm (%d agent(s), cap=%d)...",
        len(problem_branches),
        args.max_concurrent,
    )

    if args.dry_run:
        return _handle_dry_run_problem_branches(problem_branches, args.json)

    return _dispatch_tidy_swarm(args, problem_branches, trunk, repo_path, repo_slug, agent)


def _handle_tidy_problem_branches(
    args: argparse.Namespace,
    agent: str,
    problem_branches: list[str],
    trunk: str,
    repo_path: Path,
    repo_slug: str,
) -> int:
    """Compatibility wrapper for the pre-extraction tidy handler name."""
    return _handle_problem_branches(args, problem_branches, trunk, repo_path, repo_slug, agent)


def main() -> int:
    """Entry point for hephaestus-tidy."""
    args = _build_arg_parser().parse_args()
    configure_github_throttle_from_args(args)
    agent = resolve_agent(
        args.agent,
        disable_pi_automation=args.disable_pi_automation,
        auth_status_timeout=args.auth_status_timeout,
        pi_isolation_adapter=args.pi_isolation_adapter,
        pi_dir=args.pi_dir,
    )
    _configure_logging(args.verbose, args.log_format)

    env = _validate_environment()
    if env is None:
        return _emit_tidy_environment_failure(args.json)
    repo_slug, _, repo_path = env
    trunk = (
        _detect_default_branch(args.trunk)
        if args.gh_timeout == DEFAULT_GH_TIMEOUT
        else _detect_default_branch(args.trunk, gh_timeout=args.gh_timeout)
    )

    logger.info("Repo: %s  |  Trunk: %s  |  Path: %s", repo_slug, trunk, repo_path)

    if args.cleanup_stale_worktrees:
        return _cleanup_stale_worktrees(
            repo_path,
            trunk,
            args.dry_run,
            **({"gh_timeout": args.gh_timeout} if args.gh_timeout != DEFAULT_GH_TIMEOUT else {}),
        )

    try:
        problem_branches = _run_tidy_and_find_problem_branches(trunk, args.dry_run)
    except TidyExecutionError as error:
        logger.error(
            "gh tidy failed with exit code %d — cleanup state is unknown; "
            "fix the underlying failures before claiming a clean result",
            error.exit_code,
        )
        if args.json:
            emit_json_status(1, message="gh tidy failed; cleanup state unknown")
        return 1

    if not problem_branches:
        return _handle_no_problem_branches(args.json)

    return _handle_problem_branches(args, problem_branches, trunk, repo_path, repo_slug, agent)


if __name__ == "__main__":
    sys.exit(main())
