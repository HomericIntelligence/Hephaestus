#!/usr/bin/env python3
"""Display the agent prompt for a given GitHub issue and pipeline stage.

Usage::

    python scripts/show_prompt.py --issue 1170 --stage planning
    python scripts/show_prompt.py --issue 1170 --stage plan-review
    python scripts/show_prompt.py --issue 1170 --stage implementation

Supported stages: planning, plan-review, plan-loop-review, implementation,
impl-review, impl-resume, pr-review, address-review, follow-up, advise.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Ensure the repo root is on sys.path so ``hephaestus`` resolves without a
# dev-install (``pip install -e .``).  Must precede any ``hephaestus`` import.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

STAGES = (
    "planning",
    "plan-review",
    "plan-loop-review",
    "implementation",
    "impl-review",
    "impl-resume",
    "pr-review",
    "address-review",
    "follow-up",
    "advise",
)


# ---------------------------------------------------------------------------
# GitHub data fetching
# ---------------------------------------------------------------------------


def _gh(args: list[str], *, parse_json: bool = False) -> Any:
    """Run a gh CLI command and return stdout as text or parsed JSON."""
    result = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return json.loads(result.stdout) if parse_json else result.stdout


def fetch_issue(repo: str, issue_number: int) -> dict[str, Any]:
    """Fetch issue title, body, and comments from GitHub."""
    return _gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "title,body,comments",
        ],
        parse_json=True,
    )


_PLAN_MARKERS = (
    "# Implementation Plan",
    "## Implementation Plan",
    "## Approach",
    "### Approach",
    "## Proposed Solution",
    "## Design",
)


def _extract_plan_from_issue_data(issue_data: dict[str, Any] | None) -> str | None:
    """Extract the latest plan comment from already-fetched issue data.

    Searches for comments whose body contains any of the recognised plan
    heading markers and returns the most recent match.
    """
    if issue_data is None:
        return None
    comments = issue_data.get("comments", [])
    for comment in reversed(comments):
        body = comment.get("body", "")
        if any(marker in body for marker in _PLAN_MARKERS):
            return body
    return None


def fetch_pr_diff(repo: str, pr_number: int) -> str:
    """Fetch the diff for a PR."""
    return _gh(["pr", "diff", str(pr_number), "--repo", repo])


def fetch_pr_threads(repo: str, pr_number: int) -> str:
    """Fetch review threads from a PR as JSON."""
    try:
        data = _gh(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "reviewThreads,body",
            ],
            parse_json=True,
        )
        return json.dumps(data, indent=2)
    except RuntimeError:
        return "[]"


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_prompt(
    stage: str,
    issue_number: int,
    repo: str,
    *,
    branch_name: str = "",
    worktree_path: str = "",
    pr_number: int = 0,
    iteration: int = 0,
) -> str:
    """Build the prompt for the given stage.

    Calls the appropriate Hephaestus prompt builder function from
    ``hephaestus.automation.prompts``.
    """
    from hephaestus.automation.prompts.advise import get_advise_prompt
    from hephaestus.automation.prompts.follow_up import get_follow_up_prompt
    from hephaestus.automation.prompts.implementation import (
        get_impl_loop_review_prompt,
        get_impl_resume_feedback_prompt,
        get_implementation_prompt,
    )
    from hephaestus.automation.prompts.planning import (
        get_plan_loop_review_prompt,
        get_plan_prompt,
        get_plan_review_prompt,
    )

    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage!r}. Supported stages: {', '.join(STAGES)}")

    # -- Planning stage (no issue data needed) ------------------------------
    if stage == "planning":
        return get_plan_prompt(issue_number)

    issue_data = fetch_issue(repo, issue_number)
    issue_title = issue_data.get("title", "")
    issue_body = issue_data.get("body", "")

    # -- Planning stages ----------------------------------------------------

    if stage == "plan-review":
        plan_text = (
            _extract_plan_from_issue_data(issue_data)
            or issue_body
            or issue_title
            or "(no plan found)"
        )
        return get_plan_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
        )

    if stage == "plan-loop-review":
        plan_text = (
            _extract_plan_from_issue_data(issue_data)
            or issue_body
            or issue_title
            or "(no plan found)"
        )
        return get_plan_loop_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_text=plan_text,
            learnings="",
            iteration=iteration,
            prior_review=None,
        )

    # -- Implementation stages ----------------------------------------------
    if stage == "implementation":
        return get_implementation_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            branch_name=branch_name,
            worktree_path=worktree_path,
        )

    if stage == "impl-review":
        diff_text = ""
        files_changed = ""
        if pr_number:
            with contextlib.suppress(RuntimeError):
                diff_text = fetch_pr_diff(repo, pr_number)
        return get_impl_loop_review_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            diff_text=diff_text,
            files_changed=files_changed,
            iteration=iteration,
            prior_review=None,
        )

    if stage == "impl-resume":
        return get_impl_resume_feedback_prompt(
            issue_number=issue_number,
            prev_iteration=iteration,
            verdict="NOGO",
            review_text="(previous review text)",
        )

    # -- PR review stages ---------------------------------------------------
    if stage == "pr-review":
        from hephaestus.automation.prompts.pr_review import get_pr_review_analysis_prompt

        diff_text = ""
        if pr_number:
            with contextlib.suppress(RuntimeError):
                diff_text = fetch_pr_diff(repo, pr_number)
        return get_pr_review_analysis_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            pr_diff=diff_text,
            issue_body=issue_body,
        )

    if stage == "address-review":
        from hephaestus.automation.prompts.address_review import get_address_review_prompt

        threads_json = "[]"
        if pr_number:
            threads_json = fetch_pr_threads(repo, pr_number)
        return get_address_review_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            worktree_path=worktree_path,
            threads_json=threads_json,
        )

    # -- Other stages -------------------------------------------------------
    if stage == "follow-up":
        return get_follow_up_prompt(issue_number)

    if stage == "advise":
        return get_advise_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            marketplace_path="",
        )

    raise AssertionError(f"Unhandled stage: {stage!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the show-prompt CLI."""
    parser = argparse.ArgumentParser(
        description="Display the agent prompt for a given GitHub issue and pipeline stage.",
    )
    parser.add_argument("--issue", type=int, required=True, help="GitHub issue number")
    parser.add_argument(
        "--stage",
        required=True,
        choices=STAGES,
        help="Pipeline stage name",
    )
    parser.add_argument(
        "--repo",
        default="HomericIntelligence/ProjectHephaestus",
        help="GitHub repo (owner/name)",
    )
    parser.add_argument(
        "--pr", type=int, default=0, help="PR number (for pr-review, address-review, impl-review)"
    )
    parser.add_argument("--branch", default="", help="Branch name (for implementation stage)")
    parser.add_argument("--worktree", default="", help="Worktree path (for implementation stage)")
    parser.add_argument("--iteration", type=int, default=0, help="Review iteration number")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the show-prompt CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        prompt = build_prompt(
            stage=args.stage,
            issue_number=args.issue,
            repo=args.repo,
            branch_name=args.branch,
            worktree_path=args.worktree,
            pr_number=args.pr,
            iteration=args.iteration,
        )
        print(prompt)
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
