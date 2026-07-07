"""Shared helpers for the PR / plan reviewer trio.

Extracts utilities that were previously duplicated across
``pr_reviewer.py`` and ``address_review.py``.

Provides:
- ``DEFAULT_STATE_DIR`` / ``ensure_state_dir``: Canonical automation state
  directory path and creation helper.
- ``parse_json_block``: Extract the last ```json``` block from Claude output.
- ``_discover_prs_simple``: Shared issue-to-PR discovery loop for reviewer
  callers that supply their own single-issue lookup function.
- ``find_pr_for_issue``: Locate the open PR for a GitHub issue (two or three
  lookup strategies depending on the caller's needs).
- ``setup_review_logging``: Standard logging configuration for the reviewer
  CLIs (#599 dedupe).
- ``print_worker_summary``: Standard worker-run summary logging for reviewer
  and driver classes (#1381 dedupe).
- ``drain_completed_futures``: Shared concurrent-futures drain loop with
  exponential backoff and WARNING logging on ``wait()`` failure (#1463 dedupe).
- ``ensure_state_dir``: Create and return the canonical automation state directory.
- ``build_automation_parser``: Argparse parser builder for automation CLIs
  with opt-in common flags (#1392 dedupe).
- ``build_review_parser``: Argparse parser builder shared by ``pr_reviewer``
  and ``address_review`` (#599 dedupe).
- ``instance_log``: Shared body of the per-instance ``_log`` helper used by
  the reviewer classes (#599 dedupe).
- ``load_impl_session_id``: Shared implementer-session state loader for
  drive-green and address-review.
- ``log_file_path``: Standard per-issue automation log filename builder.
- ``load_state_file``: Generic state file loader (raw dict or Pydantic model).
- ``save_state_file``: Generic secure state file writer.
- ``write_work_report``: Write a phase's work-unit count to $HEPH_WORK_REPORT.
- ``work_report_context``: Context manager that writes a work report on exit
  when the loop runner requested one (#613).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, wait
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, overload

from pydantic import BaseModel

from hephaestus.agents.runtime import add_agent_argument, session_agent_matches
from hephaestus.cli.utils import (
    add_dry_run_arg,
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
)
from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT
from hephaestus.io.utils import write_secure

from .github_api import _gh_call
from .models import DEFAULT_STATE_DIR as DEFAULT_STATE_DIR, DEFAULT_WORKER_COUNT

if TYPE_CHECKING:
    from .models import WorkerResult

logger = logging.getLogger(__name__)

ParseJsonErrorCallback = Callable[[str, Path | None, OSError | None], None]

_JSON_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n^[ \t]*```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
_REVIEW_PARSE_MISSING = {"comments": [], "summary": "No structured output from analysis"}
_REVIEW_PARSE_FAILED = {
    "comments": [],
    "summary": "Failed to parse structured output from analysis",
}

_StateModelT = TypeVar("_StateModelT", bound=BaseModel)


@overload
def load_state_file(
    state_dir: Path,
    prefix: str,
    issue_number: int,
    model_class: type[_StateModelT],
    *,
    state_logger: logging.Logger | None = None,
) -> _StateModelT | None:
    pass


@overload
def load_state_file(
    state_dir: Path,
    prefix: str,
    issue_number: int,
    model_class: None = None,
    *,
    state_logger: logging.Logger | None = None,
) -> dict[str, Any] | None:
    pass


def load_state_file(
    state_dir: Path,
    prefix: str,
    issue_number: int,
    model_class: type[_StateModelT] | None = None,
    *,
    state_logger: logging.Logger | None = None,
) -> _StateModelT | dict[str, Any] | None:
    """Load ``<prefix>-<issue_number>.json`` as a JSON object or Pydantic model.

    Args:
        state_dir: Directory containing state files.
        prefix: File prefix, such as ``"issue"`` or ``"review"``.
        issue_number: GitHub issue number used in the filename.
        model_class: Optional Pydantic model class used to validate the JSON object.
        state_logger: Optional logger for malformed-file warnings.

    Returns:
        A raw JSON object dict, a validated Pydantic model, or ``None`` when the
        file is absent or invalid.

    """
    target_logger = state_logger or logger
    state_file = state_dir / f"{prefix}-{issue_number}.json"
    if not state_file.exists():
        return None

    try:
        payload = json.loads(state_file.read_text())
    except (OSError, ValueError) as exc:
        target_logger.warning(
            "Malformed %s state for issue #%d at %s: %s",
            prefix,
            issue_number,
            state_file,
            exc,
        )
        return None

    if not isinstance(payload, dict):
        target_logger.warning(
            "Malformed %s state for issue #%d at %s: expected JSON object, got %s",
            prefix,
            issue_number,
            state_file,
            type(payload).__name__,
        )
        return None

    if model_class is None:
        return payload

    try:
        return model_class.model_validate(payload)
    except ValueError as exc:
        target_logger.warning(
            "Malformed %s state for issue #%d at %s: %s",
            prefix,
            issue_number,
            state_file,
            exc,
        )
        return None


def save_state_file(state_dir: Path, prefix: str, issue_number: int, state: BaseModel) -> None:
    """Securely persist ``state`` to ``<prefix>-<issue_number>.json``.

    Args:
        state_dir: Directory where the state file should be written.
        prefix: File prefix, such as ``"issue"`` or ``"review"``.
        issue_number: GitHub issue number used in the filename.
        state: Pydantic model to serialize.

    """
    state_file = state_dir / f"{prefix}-{issue_number}.json"
    write_secure(state_file, state.model_dump_json(indent=2))


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
        format=AUTOMATION_LOG_FORMAT,
        datefmt=LOG_DATEFMT,
    )


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


def print_worker_summary(
    title: str,
    results: dict[int, WorkerResult],
    *,
    count_noun: str = "issues",
    failed_header: str = "Failed issues:",
) -> None:
    """Log the standard worker-run summary.

    Args:
        title: Summary banner to log between separator lines.
        results: Mapping of issue or PR number to worker result.
        count_noun: Noun used in the total-count line.
        failed_header: Header logged before the per-failure list.

    """
    total = len(results)
    successful = sum(1 for result in results.values() if result.success)
    failed = total - successful

    logger.info("=" * 60)
    logger.info(title)
    logger.info("=" * 60)
    logger.info("Total %s: %s", count_noun, total)
    logger.info("Successful: %s", successful)
    logger.info("Failed: %s", failed)

    if failed > 0:
        logger.info(failed_header)
        for issue_num, result in results.items():
            if not result.success:
                logger.info("  #%s: %s", issue_num, result.error)


def drain_completed_futures(
    futures: Mapping[Future[Any], int],
    *,
    timeout: float = 1.0,
) -> Iterator[Future[Any]]:
    """Yield completed futures, backing off on repeated ``wait()`` failures.

    Encapsulates the drain scaffold previously duplicated across the four
    worker loops (#1463). The canonical exponential-backoff-with-logging
    behavior is taken from ``address_review.py`` — the prior bare
    ``except Exception: time.sleep(0.1); continue`` variants in
    ``ci_driver``/``pr_reviewer``/``plan_reviewer`` silently busy-looped.

    The caller retains ownership of ``futures`` and MUST ``pop`` each yielded
    future from its own dict; this generator stops when ``futures`` is empty,
    so the caller's pops drive termination exactly as before.

    Args:
        futures: Live mapping of in-flight ``Future`` to issue number. The
            caller mutates this (popping completed futures) between yields.
        timeout: Per-``wait()`` poll timeout in seconds.

    Yields:
        Each ``Future`` reported done by ``concurrent.futures.wait``.

    """
    # Backoff on repeated wait() failures so a flapping condition doesn't
    # busy-loop silently. Resets to 0.1s on the first successful wait().
    wait_backoff = 0.1
    while futures:
        try:
            done, _pending = wait(futures.keys(), timeout=timeout, return_when=FIRST_COMPLETED)
            wait_backoff = 0.1
        except Exception as exc:
            logger.warning(
                "futures.wait() raised %s: %s — backing off %.1fs",
                type(exc).__name__,
                exc,
                wait_backoff,
            )
            time.sleep(wait_backoff)
            wait_backoff = min(wait_backoff * 2, 5.0)
            continue

        yield from done


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
    add_parallel: bool = False,
    parallel_help: str = "Number of parallel workers, 1-32 (default: 3)",
    add_github_throttle: bool = False,
    add_dry_run: bool = True,
    dry_run_prefix: str | None = None,
    dry_run_help: str | None = None,
    add_no_ui: bool = False,
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
        add_parallel: Add the planner-style ``--parallel`` worker flag.
        parallel_help: Help text for ``--parallel``.
        add_github_throttle: Add GitHub global-throttle flags.
        add_dry_run: Add ``--dry-run``.
        dry_run_prefix: Prefix passed to the canonical dry-run helper.
        dry_run_help: Raw ``--dry-run`` help text; bypasses the canonical caveat.
        add_no_ui: Add ``--no-ui``.
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
    if add_max_workers:
        add_max_workers_arg(parser, default=max_workers_default, help_text=max_workers_help)
    if add_parallel:
        parser.add_argument(
            "--parallel",
            type=int,
            default=3,
            choices=range(1, 33),
            metavar="N",
            help=parallel_help,
        )
    if add_github_throttle:
        add_github_throttle_args(parser)
    if add_dry_run:
        if dry_run_help is not None:
            parser.add_argument("--dry-run", action="store_true", help=dry_run_help)
        else:
            add_dry_run_arg(parser, prefix=dry_run_prefix)
    if add_no_ui:
        parser.add_argument(
            "--no-ui",
            action="store_true",
            help="Disable curses UI (use plain logging instead)",
        )
    if add_verbose:
        parser.add_argument("-v", "--verbose", action="store_true", help=verbose_help)
    if add_json:
        add_json_arg(parser)
    if add_version:
        add_version_arg(parser)

    return parser


def build_review_parser(
    description: str,
    epilog: str | None = None,
    *,
    issues_help: str,
    dry_run_prefix: str,
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
        dry_run_prefix: Prefix string for the ``--dry-run`` help text, appended
            with ``DRY_RUN_HELP_CAVEAT``.

    Returns:
        Configured ``argparse.ArgumentParser`` — caller invokes ``parse_args``.

    """
    parser = build_automation_parser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_github_throttle=True,
        dry_run_prefix=dry_run_prefix,
        add_no_ui=True,
        add_version=False,
    )

    parser.add_argument(
        "--issues",
        type=int,
        nargs="+",
        required=True,
        help=issues_help,
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


def log_file_path(
    state_dir: Path,
    prefix: str,
    issue_number: int,
    *,
    iteration: int | None = None,
) -> Path:
    """Return the standard per-issue automation log path."""
    stem = f"{prefix}-{issue_number}"
    if iteration is not None:
        stem = f"{stem}-r{iteration}"
    return state_dir / f"{stem}.log"


def _copy_default(default: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(dict(default))


def _format_parse_error(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"json.JSONDecodeError: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _write_json_parse_trace(
    *,
    trace_dir: Path,
    trace_name: str,
    reason: str,
    text: str,
    last_block: str | None,
) -> tuple[Path, OSError | None]:
    trace_path = trace_dir / trace_name
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        write_secure(
            trace_path,
            "\n".join(
                [
                    f"reason: {reason}",
                    "",
                    "=== last fenced block (if any) ===",
                    last_block or "(none)",
                    "",
                    "=== full response ===",
                    text,
                ]
            ),
        )
        return trace_path, None
    except OSError as exc:
        return trace_path, exc


def parse_json_block(
    text: str,
    *,
    default: Mapping[str, Any] | None = None,
    parse_error_default: Mapping[str, Any] | None = None,
    trace_dir: Path | None = None,
    trace_name: str = "parse-error.log",
    raw_json_fallback: bool = False,
    use_last_block: bool = True,
    on_error: ParseJsonErrorCallback | None = None,
) -> dict[str, Any]:
    """Extract a JSON object from an agent response.

    Args:
        text: Agent response text.
        default: Result shape for missing JSON, and for parse errors unless
            ``parse_error_default`` is supplied.
        parse_error_default: Result shape for malformed/non-object JSON.
        trace_dir: Optional directory for parse-failure diagnostics.
        trace_name: Diagnostic filename when ``trace_dir`` is supplied.
        raw_json_fallback: Try parsing the full response as raw JSON.
        use_last_block: When true, parse the last fenced block; otherwise the
            first. Reviewers use the last block, CI repair uses the first.
        on_error: Optional callback receiving the reason, trace path, and trace
            write error.

    Returns:
        Parsed dict, or a caller-provided/default dict on failure.

    """
    missing_default = _REVIEW_PARSE_MISSING if default is None else default
    if parse_error_default is not None:
        failed_default = parse_error_default
    elif default is None:
        failed_default = _REVIEW_PARSE_FAILED
    else:
        failed_default = missing_default

    def record_error(reason: str, last_block: str | None) -> None:
        trace_path: Path | None = None
        trace_error: OSError | None = None
        if trace_dir is not None:
            trace_path, trace_error = _write_json_parse_trace(
                trace_dir=trace_dir,
                trace_name=trace_name,
                reason=reason,
                text=text,
                last_block=last_block,
            )
        if on_error is not None:
            on_error(reason, trace_path, trace_error)

    matches = _JSON_BLOCK_RE.findall(text)
    if matches:
        block = matches[-1 if use_last_block else 0]
        try:
            return dict(json.loads(block))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            if raw_json_fallback:
                with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
                    return dict(json.loads(text))
            record_error(_format_parse_error(exc), block)
            return _copy_default(failed_default)

    if raw_json_fallback:
        try:
            return dict(json.loads(text))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            record_error(_format_parse_error(exc), None)
            return _copy_default(failed_default)

    record_error("no fenced ```json block found in response", None)
    return _copy_default(missing_default)


def _discover_prs_simple(
    issue_numbers: list[int],
    find_fn: Callable[[int], int | None],
    *,
    on_missing: Callable[[int], None] | None = None,
) -> dict[int, int]:
    """Map issue numbers to open PR numbers using ``find_fn``.

    Args:
        issue_numbers: Issue numbers to resolve.
        find_fn: Callable that returns a PR number for one issue, or ``None``.
        on_missing: Optional callback invoked for each issue without an open PR.

    Returns:
        Mapping of issue number to PR number for found PRs.

    """
    pr_map: dict[int, int] = {}
    for issue_num in issue_numbers:
        pr_number = find_fn(issue_num)
        if pr_number is not None:
            pr_map[issue_num] = pr_number
        elif on_missing is not None:
            on_missing(issue_num)
    return pr_map


def load_impl_session_id(state_dir: Path, issue_number: int, agent: str) -> str | None:
    """Load the implementer's agent session ID from on-disk state.

    The implementer persists its state to ``issue-<n>.json`` (see
    ``ImplementationStateManager.save``), not ``state-<n>.json``. A stored
    session is only returned when its ``session_agent`` is compatible with the
    selected ``agent``; legacy files with no ``session_agent`` are treated as
    Claude sessions by ``session_agent_matches``.

    Args:
        state_dir: Directory holding the implementer state files.
        issue_number: GitHub issue number.
        agent: Selected agent for the current run.

    Returns:
        Session ID string, or ``None`` if the file is absent, unreadable, has
        no ``session_id``, or belongs to a different agent.

    """
    state_file = state_dir / f"issue-{issue_number}.json"
    if not state_file.exists():
        logger.debug("No implementer state file for issue #%s", issue_number)
        return None

    try:
        data = json.loads(state_file.read_text())
        session_id: str | None = data.get("session_id")
        session_agent: str | None = data.get("session_agent")
        if session_id and not session_agent_matches(session_agent, agent):
            logger.info(
                "Skipping impl session for issue #%s: session belongs to %s, selected agent is %s",
                issue_number,
                session_agent or "claude",
                agent,
            )
            return None
        return session_id
    except Exception as e:
        logger.warning("Could not load impl session for #%s: %s", issue_number, e)
        return None


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
        closes_pattern = re.compile(rf"^Closes #{issue_number}\b", re.MULTILINE)
        for candidate in pr_data:
            body = candidate.get("body") or ""
            if closes_pattern.search(body):
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

    Mirrors :func:`find_pr_for_issue`'s strategies with ``--state merged``:

    1. Branch name lookup (``{issue}-auto-impl``, merged PRs).
    2. Exact ``Closes #N`` body search via :func:`find_merged_closing_pr`.

    Used by pipeline seeding so the "PR merged → finished" classification row
    is reachable: when the open-PR lookup misses, this lookup distinguishes
    merged work (finished, idempotent) from closed/abandoned PRs (invisible —
    normalized to "no PR") and from genuinely PR-less issues.

    Args:
        issue_number: GitHub issue number.

    Returns:
        The merged PR number if found, ``None`` otherwise.

    """
    branch_name = f"{issue_number}-auto-impl"
    try:
        result = _gh_call(
            [
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                "merged",
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
            logger.info(
                "Found merged PR #%d for issue #%d via branch name", pr_number, issue_number
            )
            return pr_number
    except Exception as e:
        logger.debug("Merged branch-name lookup failed for issue #%d: %s", issue_number, e)

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


def write_work_report(work_units: int) -> None:
    """Write the phase's work-unit count to the path in $HEPH_WORK_REPORT.

    The loop runner injects HEPH_WORK_REPORT (a temp file path) into subprocess
    envs. Phases that understand the contract write their work-unit count to that
    file; the runner reads it after the subprocess returns to measure
    convergence (#613).

    Args:
        work_units: The number of work units (e.g., issues planned or reviewed).

    Note:
        No-op when the env var is unset (phase run outside the loop runner).

    """
    path = os.environ.get("HEPH_WORK_REPORT")
    if not path:
        return
    # best-effort; absence ⇒ "unknown" ⇒ treated as work
    with contextlib.suppress(OSError):
        write_secure(Path(path), str(int(work_units)))


@contextlib.contextmanager
def work_report_context(work_units_fn: Callable[[], int]) -> Iterator[None]:
    """Write a work report when the loop runner requested one.

    The report env var remains optional so phases still run outside the loop
    runner. When it is present on entry, the work-unit callback is evaluated on
    exit and written through write_work_report().

    Args:
        work_units_fn: Callback returning the work-unit count to report.

    """
    if not os.environ.get("HEPH_WORK_REPORT"):
        yield
        return

    try:
        yield
    finally:
        # Best-effort reporting: suppress reporting failures without masking the block's exception.
        with contextlib.suppress(Exception):
            write_work_report(work_units_fn())
