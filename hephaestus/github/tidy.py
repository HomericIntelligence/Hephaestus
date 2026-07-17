#!/usr/bin/env python3
"""Single-repo gh-tidy wrapper with agent conflict resolution.

Runs `gh tidy --rebase-all --trunk <default_branch>` interactively (stdin
passes through so the user can answer gh-tidy's own y/N delete prompts), then
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
    resolve_agent,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    configure_github_throttle_from_args,
    create_parser,
    emit_json_status,
)
from hephaestus.constants import agent_rebase_timeout
from hephaestus.github.client import gh_call
from hephaestus.github.git_ops import (
    in_git_repo as _shared_in_git_repo,
    repo_root as _shared_repo_root,
    working_tree_clean as _shared_working_tree_clean,
)
from hephaestus.github.pr_merge import detect_repo_from_remote
from hephaestus.logging.utils import setup_logging
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


def _detect_default_branch(override: str | None) -> str:
    """Return the repo's default branch, using override if supplied."""
    if override:
        return override
    try:
        result = gh_call(
            ["repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
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
    """Run gh tidy interactively, tee output to terminal + buffer.

    Returns (exit_code, combined_output_buffer).
    Stdin is connected to the user's terminal so gh-tidy's y/N prompts work.
    """
    cmd = ["gh", "tidy", "--rebase-all", "--trunk", trunk, "--skip-gc"]
    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return 0, ""

    logger.info("Running: %s", " ".join(cmd))
    buf: list[str] = []

    # Use Popen so we can tee output while keeping stdin connected to the TTY.
    # Intentionally NOT routed through hephaestus.github.client.gh_call: that
    # adapter captures stdout/stderr and detaches stdin, which would break
    # gh-tidy's interactive y/N delete prompts (the whole point of this call).
    with subprocess.Popen(
        cmd,
        stdin=sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
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
                )
                return

            results[branch] = await _run_claude_rebase_agent(
                prompt, branch, repo_path, claude_swarm
            )

    await asyncio.gather(*(_run_one(b) for b in branches))
    return results


def _run_direct_rebase_agent(agent: str, prompt: str, branch: str, repo_path: Path) -> str:
    """Run one direct rebase-fix agent and return its status marker."""
    try:
        result = run_agent_text(
            agent=agent,
            prompt=prompt,
            cwd=repo_path,
            timeout=agent_rebase_timeout(),
            model=direct_agent_model(agent, "HEPH_IMPLEMENTER_MODEL"),
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
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
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


def _configure_logging(verbose: bool) -> None:
    """Configure CLI logging for tidy output."""
    setup_logging(
        level=logging.DEBUG if verbose else logging.INFO,
        format_string="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        primary_stream="stderr",
    )


def _emit_tidy_environment_failure(json_output: bool) -> int:
    if json_output:
        emit_json_status(1, message="environment validation failed")
    return 1


def _run_tidy_and_find_problem_branches(trunk: str, dry_run: bool) -> list[str]:
    exit_code, output = _run_gh_tidy(trunk, dry_run)
    if exit_code != 0 and not dry_run:
        logger.warning(
            "gh tidy exited with code %d — proceeding to parse output anyway",
            exit_code,
        )
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
    agent = resolve_agent(args.agent)
    _configure_logging(args.verbose)

    env = _validate_environment()
    if env is None:
        return _emit_tidy_environment_failure(args.json)
    repo_slug, _, repo_path = env
    trunk = _detect_default_branch(args.trunk)

    logger.info("Repo: %s  |  Trunk: %s  |  Path: %s", repo_slug, trunk, repo_path)

    problem_branches = _run_tidy_and_find_problem_branches(trunk, args.dry_run)
    if not problem_branches:
        return _handle_no_problem_branches(args.json)

    return _handle_problem_branches(args, problem_branches, trunk, repo_path, repo_slug, agent)


if __name__ == "__main__":
    sys.exit(main())
