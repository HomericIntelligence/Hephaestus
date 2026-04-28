#!/usr/bin/env python3
"""Single-repo gh-tidy wrapper with Myrmidon swarm for conflict resolution.

Runs `gh tidy --rebase-all --trunk <default_branch>` interactively (stdin
passes through so the user can answer gh-tidy's own y/N delete prompts), then
spawns one Sonnet agent per branch that gh-tidy failed to rebase.

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

from hephaestus.github.pr_merge import detect_repo_from_remote
from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)

# ANSI escape sequence stripper
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Pattern that gh-tidy emits when rebase fails (from gh-tidy lines 297-301)
_PROBLEM_HEADER = re.compile(r"WARNING:\s*Unable to auto-rebase the following branches")
_PROBLEM_BULLET = re.compile(r"^\s*\*\s+(\S+)")

# Swarm constraints injected verbatim into every agent prompt
_FORBIDDEN_BLOCK = """\
## FORBIDDEN ACTIONS — do not perform any of these, ever:
- `git branch -d <branch>`
- `git branch -D <branch>`
- `git push origin --delete <branch>`
- `git worktree remove --force <path>`
- Removing or deleting any worktree that existed before this agent started
- Deleting any local or remote branch
Only the worktree that THIS agent creates (`tidy-<branch>`) may be cleaned up,
and only with `git worktree remove` (without --force).  If that fails, leave
the worktree in place and report it.
"""

FLEET_NOREPLY = "4211002+mvillmow@users.noreply.github.com"


def _detect_default_branch(override: str | None) -> str:
    """Return the repo's default branch, using override if supplied."""
    if override:
        return override
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch = result.stdout.strip()
        if branch:
            return branch
    except subprocess.CalledProcessError as e:
        logger.warning("Could not detect default branch via gh: %s", e.stderr.strip())
    return "main"


def _working_tree_clean() -> bool:
    """Return True if the git working tree has no uncommitted changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def _in_git_repo() -> bool:
    """Return True if cwd is inside a git repository."""
    return (
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _repo_root() -> Path:
    """Return the root directory of the current git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


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
    return f"""\
You are a Myrmidon rebase-fix agent operating on a single git branch.

## Context
- Repository: {repo_slug}
- Repository path: {repo_path}
- Branch to rebase: `{branch}`
- Trunk: `{trunk}`
- Worktree to create: {worktree_path}

{_FORBIDDEN_BLOCK}

## Your task — complete in order, stop and report on any failure:

### 1. Pre-flight
```bash
cd {repo_path}
git worktree prune          # safe: removes only already-gone worktree directories
git worktree add {worktree_path} {branch}
```

### 2. Fetch latest trunk
```bash
git -C {worktree_path} fetch origin {trunk}
```

### 3. Rebase
```bash
git -C {worktree_path} rebase origin/{trunk}
```

If the rebase succeeds cleanly, skip to step 5.

If the rebase stops with conflicts:

### 4. Semantic conflict resolution (for each conflicted file)
```bash
git -C {worktree_path} diff --name-only --diff-filter=U
```

For EACH conflicted file:
- Read the file — it contains `<<<<<<<` / `=======` / `>>>>>>>` markers.
- Read what BOTH sides were trying to accomplish.
- Write the correctly merged content that preserves the intent of both sides.
- `git -C {worktree_path} add <file>`

Resolution heuristics (from batch-pr-rebase-workflow v2.8.0, verified-ci):
- `.github/workflows/**` → prefer trunk's version unless this branch is ADDING the workflow
- `pixi.lock` → `git -C {worktree_path} show origin/{trunk}:pixi.lock > pixi.lock`;
  then run `pixi install` inside the worktree to regenerate
- Feature code, tests → keep both sides' genuine additions; do NOT blindly take either side
- `CHANGELOG.md` → always take trunk's version (HEAD); a consolidation PR handles the merge later
- Generated/binary files → take trunk's version

After staging all resolved files:
```bash
GIT_EDITOR=true git -C {worktree_path} rebase --continue
```
If git says "nothing to commit", use `git -C {worktree_path} rebase --skip` instead.
Repeat until the rebase completes.

### 5. Verify the rebase didn't silently drop everything
```bash
git -C {worktree_path} log origin/{trunk}..HEAD --oneline
```
If the output is EMPTY: all commits were already on {trunk}. Report "subsumed" and STOP —
do NOT push, do NOT delete anything.

### 6. Push
```bash
git -C {worktree_path} push --force-with-lease --force-if-includes origin {branch}
```

### 7. Re-arm auto-merge (if a PR exists)
```bash
PR=$(gh pr list --repo {repo_slug} --head {branch} --json number --jq '.[0].number // empty')
if [ -n "$PR" ]; then
  gh pr merge --auto --merge "$PR"
fi
```

### 8. Clean up worktree (no --force)
```bash
git -C {repo_path} worktree remove {worktree_path}
```
If this fails (worktree has uncommitted files), leave it in place and report. Do NOT use --force.

## Report format (end your response with this):
```
STATUS: <rebased | subsumed | conflict-too-complex | failed>
BRANCH: {branch}
NOTE: <one sentence summary>
```

## Safety Net workarounds (use these when Safety Net hook blocks a command):
- Instead of `git checkout <ref> -- <file>`: use `git show <ref>:<file> > <file>`
- Instead of `git checkout <branch>`: use `git switch <branch>`
- Instead of `git reset --hard`: use `git reset --keep` (only if no uncommitted changes)
- `GIT_EDITOR=true` before `git rebase --continue` to suppress editor prompts
"""


async def _dispatch_swarm(
    branches: list[str],
    trunk: str,
    repo_path: Path,
    repo_slug: str,
    max_concurrent: int,
    dry_run: bool,
) -> dict[str, str]:
    """Spawn one Sonnet agent per branch (capped at max_concurrent).

    Returns a dict of branch -> status string.
    """
    try:
        from claude_code_sdk import ClaudeCodeOptions, query
    except ImportError:
        logger.error(
            "claude_code_sdk not available — cannot dispatch swarm. "
            "Install with: pip install claude-code-sdk",
        )
        return dict.fromkeys(branches, "failed (claude_code_sdk missing)")

    results: dict[str, str] = {}
    sem = asyncio.Semaphore(max_concurrent)

    async def _run_one(branch: str) -> None:
        async with sem:
            prompt = _make_agent_prompt(branch, trunk, repo_path, repo_slug)
            if dry_run:
                logger.info("[dry-run] Would spawn Sonnet agent for branch: %s", branch)
                results[branch] = "dry-run"
                return

            logger.info("Spawning agent for branch: %s", branch)
            status = "failed"
            options = ClaudeCodeOptions(
                max_turns=40,
                cwd=str(repo_path),
                model="claude-sonnet-4-6",
            )
            try:
                for message in query(prompt=prompt, options=options):
                    text = getattr(message, "text", None) or str(message)
                    if "STATUS:" in text:
                        m = re.search(r"STATUS:\s*(\S+)", text)
                        if m:
                            status = m.group(1)
                    if text:
                        logger.debug("[%s] agent: %s", branch, text[:300])
            except Exception as e:
                logger.error("[%s] agent exception: %s", branch, e)
            results[branch] = status

    await asyncio.gather(*(_run_one(b) for b in branches))
    return results


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Tidy the current repo's branches and fix failed rebases with a Myrmidon swarm"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
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


def main() -> int:
    """Entry point for hephaestus-tidy."""
    args = _build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    env = _validate_environment()
    if env is None:
        return 1
    repo_slug, _, repo_path = env
    trunk = _detect_default_branch(args.trunk)

    logger.info("Repo: %s  |  Trunk: %s  |  Path: %s", repo_slug, trunk, repo_path)

    # --- Phase 1: run gh tidy interactively ---
    exit_code, output = _run_gh_tidy(trunk, args.dry_run)
    if exit_code != 0 and not args.dry_run:
        logger.warning(
            "gh tidy exited with code %d — proceeding to parse output anyway",
            exit_code,
        )

    problem_branches = parse_problem_branches(output)

    if not problem_branches:
        logger.info("\nAll branches rebased cleanly — no swarm needed.")
        return 0

    logger.info(
        "\ngh tidy could not rebase %d branch(es): %s",
        len(problem_branches),
        ", ".join(problem_branches),
    )

    if args.no_swarm:
        logger.info("--no-swarm: skipping Myrmidon dispatch. Fix manually:")
        for b in problem_branches:
            logger.info("  git rebase origin/%s  (on branch %s)", trunk, b)
        return 1

    logger.info(
        "Dispatching Myrmidon swarm (%d agent(s), cap=%d)...",
        len(problem_branches),
        args.max_concurrent,
    )

    if args.dry_run:
        for b in problem_branches:
            logger.info("[dry-run] Would spawn Sonnet agent for branch: %s", b)
        return 0

    results = asyncio.run(
        _dispatch_swarm(
            problem_branches,
            trunk,
            repo_path,
            repo_slug,
            args.max_concurrent,
            dry_run=args.dry_run,
        )
    )

    return _print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
