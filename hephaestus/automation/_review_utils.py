"""Shared helpers for the PR / plan reviewer trio.

Extracts utilities that were previously duplicated across
``pr_reviewer.py`` and ``address_review.py``.

Provides:
- ``parse_json_block``: Extract the last ```json``` block from Claude output.
- ``find_pr_for_issue``: Locate the open PR for a GitHub issue (two or three
  lookup strategies depending on the caller's needs).
- ``setup_review_logging``: Standard logging configuration for the reviewer
  CLIs (#599 dedupe).
- ``build_review_parser``: Argparse parser builder shared by ``pr_reviewer``
  and ``address_review`` (#599 dedupe).
- ``instance_log``: Shared body of the per-instance ``_log`` helper used by
  the reviewer classes (#599 dedupe).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
from typing import Any

from hephaestus.agents.runtime import add_agent_argument

from .github_api import _gh_call

logger = logging.getLogger(__name__)


def setup_review_logging(verbose: bool = False) -> None:
    """Configure root logging for the reviewer CLIs.

    Identical to the previously-duplicated ``_setup_logging`` helpers in
    ``pr_reviewer.py`` and ``address_review.py``.

    Args:
        verbose: Enable DEBUG-level logging (otherwise INFO).

    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_review_parser(
    description: str,
    epilog: str | None = None,
    *,
    issues_help: str,
    dry_run_help: str,
) -> argparse.ArgumentParser:
    """Build the argparse parser shared by ``pr_reviewer`` and ``address_review``.

    The two CLIs differ only in their ``description``/``epilog`` text and in
    the help strings for ``--issues`` / ``--dry-run``. Every other option
    (``--agent``, ``--max-workers``, ``--no-ui``, ``-v/--verbose``) is
    identical.

    Args:
        description: Parser description text.
        epilog: Parser epilog text (typically an Examples block).
        issues_help: Help text for the ``--issues`` argument.
        dry_run_help: Help text for the ``--dry-run`` argument.

    Returns:
        Configured ``argparse.ArgumentParser`` — caller invokes ``parse_args``.

    """
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        required=True,
        help=issues_help,
    )
    add_agent_argument(parser)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=3,
        choices=range(1, 33),
        metavar="N",
        help="Maximum number of parallel workers, 1-32 (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=dry_run_help,
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable curses UI (use plain logging instead)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


def instance_log(
    log_manager: Any,
    level: str,
    msg: str,
    thread_id: int | None = None,
    *,
    caller_logger: logging.Logger | None = None,
) -> None:
    """Log to both the caller's module logger and the per-thread UI buffer.

    Shared body of the previously-duplicated ``PRReviewer._log`` and
    ``AddressReviewer._log`` instance methods. ``caller_logger`` defaults
    to this module's logger so callers that don't care about provenance
    can omit it, but the reviewer classes pass their own module logger to
    preserve the pre-refactor log-record source.

    Args:
        log_manager: A ``ThreadLogManager`` exposing ``.log(thread_id, msg)``.
        level: Log level name — ``"error"``, ``"warning"``, or ``"info"``.
        msg: Message to log.
        thread_id: Thread ID for the UI buffer (defaults to current thread).
        caller_logger: Logger used for the stdlib log record. Defaults to
            this module's logger.

    """
    target_logger = caller_logger if caller_logger is not None else logger
    getattr(target_logger, level)(msg)
    tid = thread_id or threading.get_ident()
    prefix = {"error": "ERROR", "warning": "WARN", "info": ""}.get(level, "")
    ui_msg = f"{prefix}: {msg}" if prefix else msg
    log_manager.log(tid, ui_msg)


def parse_json_block(text: str) -> dict[str, Any]:
    """Extract the last ```json ... ``` block from Claude's response.

    Args:
        text: Claude's full response text.

    Returns:
        Parsed dict, or a default dict with empty collections on failure.

    """
    matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not matches:
        return {"comments": [], "summary": "No structured output from analysis"}
    try:
        return dict(json.loads(matches[-1]))
    except json.JSONDecodeError:
        return {"comments": [], "summary": "Failed to parse structured output from analysis"}


def find_pr_for_issue(
    issue_number: int,
    *,
    extra_strategies: bool = False,
    _load_review_state_fn: Any = None,
) -> int | None:
    """Find the open PR for a single issue.

    Always tries two strategies:

    1. Branch name lookup (``{issue}-auto-impl``).
    2. PR-body text search (``#{issue} in:body``).

    When ``extra_strategies=True`` a third strategy is attempted between 1
    and 2: the stored ``pr_number`` from the on-disk review state is
    checked via ``gh pr view``.  The caller supplies ``_load_review_state_fn``
    (a zero-arg callable that returns a ``ReviewState | None``) to keep this
    module free of circular imports.

    Args:
        issue_number: GitHub issue number.
        extra_strategies: When True, also check the on-disk review state.
        _load_review_state_fn: Callable ``() -> ReviewState | None`` used
            when ``extra_strategies=True``.

    Returns:
        PR number if found, ``None`` otherwise.

    """
    # Strategy 1: branch-name lookup
    branch_name = f"{issue_number}-auto-impl"
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

    # Strategy 2 (optional): on-disk review state
    if extra_strategies and _load_review_state_fn is not None:
        review_state = _load_review_state_fn()
        if review_state is not None and review_state.pr_number:
            try:
                result = _gh_call(
                    [
                        "pr",
                        "view",
                        str(review_state.pr_number),
                        "--json",
                        "number,state",
                    ],
                    check=False,
                )
                pr_data = json.loads(result.stdout or "{}")
                if pr_data.get("state", "").upper() == "OPEN":
                    pr_number = int(review_state.pr_number)
                    logger.info(
                        "Found PR #%d for issue #%d via review state",
                        pr_number,
                        issue_number,
                    )
                    return pr_number
            except Exception as e:
                logger.debug("Review state PR lookup failed for issue #%d: %s", issue_number, e)

    # Strategy 3: PR-body text search.
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
        closes_pattern = re.compile(rf"^Closes #{issue_number}\b", re.MULTILINE)
        for candidate in pr_data:
            body = candidate.get("body") or ""
            if closes_pattern.search(body):
                pr_number = int(candidate["number"])
                logger.info("Found PR #%d for issue #%d via body search", pr_number, issue_number)
                return pr_number
    except Exception as e:
        logger.debug("Body search failed for issue #%d: %s", issue_number, e)

    return None
