"""Provide current queue parsing, state directories, and GitHub query helpers."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import add_agent_argument
from hephaestus.automation.prompts.catalog import add_prompt_dir_argument
from hephaestus.cli.utils import (
    add_dry_run_arg,
    add_github_throttle_args,
    add_json_arg,
    add_logging_args,
    add_version_arg,
)

from .git_utils import issue_auto_impl_branch_name
from .github_api import _gh_call
from .models import DEFAULT_STATE_DIR as DEFAULT_STATE_DIR, DEFAULT_WORKER_COUNT

logger = logging.getLogger(__name__)


_JSON_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n^[ \t]*```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
_REVIEW_PARSE_MISSING = {"comments": [], "summary": "No structured output from analysis"}
_REVIEW_PARSE_FAILED = {
    "comments": [],
    "summary": "Failed to parse structured output from analysis",
}


def has_exact_closing_line(body: str, issue_number: int) -> bool:
    """Return whether ``body`` contains the canonical ``Closes #N`` policy line.

    The optional carriage return admits CRLF bodies while rejecting grouped and
    suffixed issue references that GitHub's text search can otherwise return.
    """
    return re.search(rf"^Closes #{issue_number}\r?$", body, re.MULTILINE) is not None


def ensure_state_dir(repo_root: Path, subdir: str = DEFAULT_STATE_DIR) -> Path:
    """Create and return the automation state directory under ``repo_root``."""
    state_dir = repo_root / subdir
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def add_max_workers_arg(
    parser: argparse.ArgumentParser,
    *,
    default: int = DEFAULT_WORKER_COUNT,
    help_text: str = f"Maximum number of parallel workers, 1-32 (default: {DEFAULT_WORKER_COUNT})",
) -> None:
    """Add a validated ``--max-workers`` argument to ``parser``.

    Centralises the validation used by every automation CLI so that
    ``hephaestus-automation-loop`` cannot accept a value (e.g. ``0`` or ``-1``)
    that a child phase will later reject — see #723.

    Args:
        parser: Parser to mutate.
        default: Default worker count when the flag is omitted.
        help_text: Help string. Callers that pass workers through to child
            binaries (e.g. ``loop_runner``) override the default phrasing.

    """
    parser.add_argument(
        "--max-workers",
        type=int,
        default=default,
        choices=range(1, 33),
        metavar="N",
        help=help_text,
    )


def _parse_gh_extra_path_root(value: str) -> Path:
    """Validate an explicit root whose only admitted executable is ``bin/gh``."""
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise argparse.ArgumentTypeError("--gh-extra-path-root must be an absolute path")
    try:
        resolved_root = root.resolve(strict=True)
        executable = (resolved_root / "bin" / "gh").resolve(strict=True)
    except OSError as exc:
        raise argparse.ArgumentTypeError(
            "--gh-extra-path-root must contain an executable bin/gh"
        ) from exc
    if (
        not resolved_root.is_dir()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
        or not executable.is_relative_to(resolved_root)
    ):
        raise argparse.ArgumentTypeError(
            "--gh-extra-path-root must contain an executable bin/gh without symlink escapes"
        )
    return resolved_root


def add_gh_extra_path_root_arg(parser: argparse.ArgumentParser) -> None:
    """Add the explicit, narrowly validated ``gh`` root argument to ``parser``."""
    parser.add_argument(
        "--gh-extra-path-root",
        type=_parse_gh_extra_path_root,
        default=None,
        metavar="ROOT",
        help=(
            "Explicitly allow only ROOT/bin/gh in addition to system gh locations. "
            "ROOT must be absolute and contain an executable bin/gh that does not escape ROOT."
        ),
    )


def _automation_parser_kwargs(
    description: str,
    epilog: str | None,
    prog: str | None,
    formatter_class: type[argparse.HelpFormatter] | None,
) -> dict[str, Any]:
    """Build ArgumentParser kwargs while omitting unset optional parameters."""
    kwargs: dict[str, Any] = {"description": description}
    if prog is not None:
        kwargs["prog"] = prog
    if formatter_class is not None:
        kwargs["formatter_class"] = formatter_class
    if epilog is not None:
        kwargs["epilog"] = epilog
    return kwargs


def build_automation_parser(
    description: str,
    epilog: str | None = None,
    *,
    prog: str | None = None,
    formatter_class: type[argparse.HelpFormatter] | None = None,
    add_agent: bool = True,
    add_max_workers: bool = True,
    max_workers_default: int = DEFAULT_WORKER_COUNT,
    max_workers_help: str = "Maximum number of parallel workers, 1-32 (default: 3)",
    add_github_throttle: bool = False,
    add_gh_extra_path_root: bool = False,
    add_dry_run: bool = True,
    dry_run_prefix: str | None = None,
    dry_run_help: str | None = None,
    add_json: bool = True,
    add_version: bool = True,
    add_verbose: bool = True,
    verbose_help: str = "Enable verbose logging",
) -> argparse.ArgumentParser:
    """Build an automation CLI parser with configurable common options.

    Args:
        description: Parser description text.
        epilog: Optional parser epilog, typically an examples block.
        prog: Optional program name override.
        formatter_class: Optional argparse formatter class.
        add_agent: Add the common ``--agent`` provider selector.
        add_max_workers: Add the common validated ``--max-workers`` flag.
        max_workers_default: Default worker count when ``--max-workers`` is omitted.
        max_workers_help: Help text for ``--max-workers``.
        add_github_throttle: Add GitHub global-throttle flags.
        add_gh_extra_path_root: Add the explicit trusted ``ROOT/bin/gh`` selector.
        add_dry_run: Add ``--dry-run``.
        dry_run_prefix: Prefix passed to the canonical dry-run helper.
        dry_run_help: Raw ``--dry-run`` help text; bypasses the canonical caveat.
        add_json: Add ``--json``.
        add_version: Add ``-V`` / ``--version``.
        add_verbose: Add ``-v`` / ``--verbose``.
        verbose_help: Help text for ``-v`` / ``--verbose``.

    Returns:
        Configured ``argparse.ArgumentParser``.

    """
    parser = argparse.ArgumentParser(
        **_automation_parser_kwargs(description, epilog, prog, formatter_class)
    )

    if add_agent:
        add_agent_argument(parser)
    add_prompt_dir_argument(parser)
    if add_max_workers:
        add_max_workers_arg(parser, default=max_workers_default, help_text=max_workers_help)
    if add_github_throttle:
        add_github_throttle_args(parser)
    if add_gh_extra_path_root:
        add_gh_extra_path_root_arg(parser)
    if add_dry_run:
        if dry_run_help is not None:
            parser.add_argument("--dry-run", action="store_true", help=dry_run_help)
        else:
            add_dry_run_arg(parser, prefix=dry_run_prefix)
    if add_verbose:
        add_logging_args(parser)
    if add_json:
        add_json_arg(parser)
    if add_version:
        add_version_arg(parser)

    return parser


def _copy_default(default: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(dict(default))


def parse_json_block(text: str, *, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read the last JSON object in fenced agent output.

    Return a copy of the supplied default when the object is absent or
    malformed. Without a supplied default, return the review error shape.
    """
    matches = _JSON_BLOCK_RE.findall(text)
    if not matches:
        return _copy_default(_REVIEW_PARSE_MISSING if default is None else default)
    try:
        return dict(json.loads(matches[-1]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return _copy_default(_REVIEW_PARSE_FAILED if default is None else default)


def find_pr_for_issue(issue_number: int) -> int | None:
    """Find an open PR by its branch or exact issue-closing line."""
    # Strategy 1: branch-name lookup
    branch_name = issue_auto_impl_branch_name(issue_number)
    try:
        result = _gh_call(
            [
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                "open",
                "--json",
                "number",
                "--limit",
                "1",
            ],
            check=False,
        )
        pr_data = json.loads(result.stdout or "[]")
        if pr_data:
            pr_number = int(pr_data[0]["number"])
            logger.info("Found PR #%d for issue #%d via branch name", pr_number, issue_number)
            return pr_number
    except Exception as e:
        logger.debug("Branch-name lookup failed for issue #%d: %s", issue_number, e)

    # Use an exact closing line to reject substring and grouped matches.
    # Search for the canonical "Closes #N" link, then *verify* via regex that
    # the matching PR's body really contains ``Closes #N`` on its own line —
    # GitHub's full-text search returns substring matches, so a PR whose body
    # says ``Closes #1234`` would be returned for ``Closes #12`` queries, and
    # a grouped audit PR with body ``Closes #12, #18, #28`` would be returned
    # for *each* of those numbers. The post-filter mirrors the ``pr-policy``
    # CI gate's exact-line check (``^Closes #<N>$`` per line).
    try:
        result = _gh_call(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--search",
                f"Closes #{issue_number} in:body",
                "--json",
                "number,body",
                "--limit",
                "10",
            ],
            check=False,
        )
        pr_data = json.loads(result.stdout or "[]")
        # ``Closes #<N>`` on its own line, capital C, no colon. Anchored to
        # line boundaries (re.MULTILINE) so ``Closes #1234`` cannot match a
        # query for #12, and grouped ``Closes #12, #18`` cannot match either
        # — only PRs that follow ``pr-policy``'s exact-line format match.
        for candidate in pr_data:
            body = candidate.get("body") or ""
            if has_exact_closing_line(body, issue_number):
                pr_number = int(candidate["number"])
                logger.info("Found PR #%d for issue #%d via body search", pr_number, issue_number)
                return pr_number
    except Exception as e:
        logger.debug("Body search failed for issue #%d: %s", issue_number, e)

    return None


def find_merged_closing_pr(issue_number: int) -> int | None:
    """Find a MERGED PR that closes ``issue_number`` via an exact ``Closes #N`` line.

    Mirrors Strategy 3 of :func:`find_pr_for_issue` but searches *merged* PRs
    instead of open ones. This catches the failure mode where a closing PR has
    already merged with a valid ``Closes #N`` line yet the issue stayed OPEN
    (GitHub does not always auto-close), causing the loop to re-plan and
    re-implement an issue whose work has already landed.

    The same exact-line regex discipline as :func:`find_pr_for_issue` applies:
    GitHub's full-text search returns substring matches, so a merged PR whose
    body says ``Closes #1234`` must NOT match a query for #12, and a grouped
    ``Closes #12, #18`` must not match either — only PRs that follow the
    ``pr-policy`` exact-line format (``^Closes #<N>`` on its own line) match.

    Args:
        issue_number: GitHub issue number.

    Returns:
        The merged PR number if one genuinely closes the issue, ``None``
        otherwise.

    """
    try:
        result = _gh_call(
            [
                "pr",
                "list",
                "--state",
                "merged",
                "--search",
                f"Closes #{issue_number} in:body",
                "--json",
                "number,body",
                "--limit",
                "10",
            ],
            check=False,
        )
        pr_data = json.loads(result.stdout or "[]")
        for candidate in pr_data:
            body = candidate.get("body") or ""
            if has_exact_closing_line(body, issue_number):
                pr_number = int(candidate["number"])
                logger.info(
                    "Found merged PR #%d closing issue #%d via body search",
                    pr_number,
                    issue_number,
                )
                return pr_number
    except Exception as e:
        logger.debug("Merged-PR body search failed for issue #%d: %s", issue_number, e)

    return None


def find_merged_pr_for_issue(issue_number: int) -> int | None:
    """Find the MERGED PR for a single issue (tri-state fetch layer, epic #1809).

    Only an exact ``Closes #N`` body line establishes the relationship. A
    branch name is not completion evidence: branches may be reused and a
    merged PR without the required closing line must not terminalize an issue.

    Args:
        issue_number: GitHub issue number.

    Returns:
        The merged PR number if found, ``None`` otherwise.

    """
    return find_merged_closing_pr(issue_number)


def close_issue_as_covered(issue_number: int, pr_number: int) -> bool:
    """Close an OPEN issue already covered by a merged closing PR (idempotent).

    Used after :func:`find_merged_closing_pr` confirms ``pr_number`` merged with
    an exact ``Closes #N`` line but the issue stayed OPEN. ``gh issue close`` is
    a no-op when the issue is already closed, so this is safe to call
    unconditionally.

    Args:
        issue_number: GitHub issue number to close.
        pr_number: The merged PR that closes it (cited in the close comment).

    Returns:
        True if the close command ran without error, False otherwise.

    """
    try:
        _gh_call(
            [
                "issue",
                "close",
                str(issue_number),
                "--comment",
                f"Closed by merged PR #{pr_number} (Closes #{issue_number}).",
            ],
            check=False,
        )
        logger.info(
            "Closed issue #%d — covered by merged PR #%d",
            issue_number,
            pr_number,
        )
        return True
    except Exception as e:
        logger.warning("Failed to close issue #%d (merged PR #%d): %s", issue_number, pr_number, e)
        return False


def get_pr_head_branch(pr_number: int) -> str | None:
    """Return the real head branch of ``pr_number`` via ``gh pr view``.

    The automation loop must operate on the PR's ACTUAL head branch, never an
    assumed ``{issue}-auto-impl`` name: ``find_pr_for_issue`` can resolve a PR
    via PR-body ``Closes #N`` search, in which case the head branch may be named
    after a different issue (or a bundle). Using the assumed name makes
    ``git fetch origin <assumed-branch>`` fail with ``exit 128`` (no such ref).

    Args:
        pr_number: GitHub PR number.

    Returns:
        The PR's ``headRefName``, or ``None`` if it cannot be determined
        (gh failure, parse error, or an empty field) so the caller can fall
        back safely rather than crash.

    """
    try:
        result = _gh_call(["pr", "view", str(pr_number), "--json", "headRefName"])
        data = json.loads(result.stdout or "{}")
        branch = data.get("headRefName") or None
        return str(branch) if branch else None
    except Exception as e:
        logger.warning("Could not fetch head branch for PR #%d: %s", pr_number, e)
        return None
