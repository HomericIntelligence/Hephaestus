"""Order implementation candidates and read their repository-specific plan files.

The coordinator owns overlap reservations and per-repository worker limits.
This module orders dependencies and filters closed explicit issues.
"""

from __future__ import annotations

import logging
import re
import subprocess
from concurrent.futures import CancelledError
from typing import TYPE_CHECKING

from hephaestus.automation.comment_identity import CommentAliasConflictError
from hephaestus.automation.dependency_resolver import CyclicDependencyError, DependencyResolver
from hephaestus.automation.models import IssueInfo
from hephaestus.automation.pipeline.scope_retraction import is_safe_scope_retraction_path
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    PlanDiscoveryStatus,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from threading import Event

    from .stages import StageGitHub

LOG = logging.getLogger(__name__)

# Backticked repo-relative path inside a plan's Files sections, e.g.
# `hephaestus/automation/pipeline/stages/pr_review.py`. Requires a slash so bare tokens
# like `pyproject.toml` or symbol refs like `os.replace` are not treated as
# in-tree paths (over-match → needless deferral; the slash requirement keeps
# the key tight to actual source paths).
# NOTE: Bare top-level file paths without a directory prefix (e.g., `errors.py`)
# are intentionally NOT captured — overlap goes undetected and both plans dispatch
# concurrently, falling back to pre-#1623 behavior (acceptable tradeoff for regex tightness).
_PLAN_FILE_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)`")
_PLAN_FILE_SECTION_RE = re.compile(r"^#{2,}\s+Files to (Modify|Create)\b", re.IGNORECASE)

# A source path only conflicts with work in the same repository.  The
# implementation queue is shared across repositories, so a bare path string
# would incorrectly serialize independent ``repo-a`` and ``repo-b`` changes.
type PlanFileClaim = tuple[tuple[str, str] | None, str]


def _parse_planned_files(plan_body: str) -> set[str]:
    """Return the repo-relative paths a plan intends to touch.

    Scans the ``## Files to Modify`` and ``## Files to Create`` sections of an
    ``# Implementation Plan`` comment (either or both may be present) and
    collects every backticked in-tree path until the next top-level ``## ``
    heading. Empty set when neither section exists.

    Args:
        plan_body: The full body of the plan comment.

    Returns:
        The set of backticked repo-relative paths found in the Files sections.

    """
    files: set[str] = set()
    in_section = False
    for line in plan_body.splitlines():
        if _PLAN_FILE_SECTION_RE.match(line):
            in_section = True
            continue
        # A new top-level ``## `` heading (not a ``### `` sub-header inside the
        # section) ends the scan region.
        if line.startswith("## "):
            in_section = False
        if in_section:
            files.update(_PLAN_FILE_RE.findall(line))
    return files


def parse_publication_scope_files(plan_body: str) -> set[str]:
    """Return the complete file declarations from exact plan file sections.

    Read backticked paths at the start of list entries, table rows, and file
    subheadings. Ignore prose references and fenced examples. Return an empty
    set if a declared path is invalid. Keep this parser separate from the
    file-overlap parser because publication requires complete paths.
    """
    files: set[str] = set()
    in_section = False
    fence = ""
    for line in plan_body.split("\n"):
        stripped = line.strip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence:
            if stripped.startswith(fence) and not stripped[len(fence) :].strip(fence[0]):
                fence = ""
            continue
        if fence_match:
            fence = fence_match[1]
            continue
        if re.match(r"^#{1,2}(?:[ \t]|$)", line):
            in_section = bool(
                re.fullmatch(
                    r"##[ \t]+(?:Files to (?:Modify|Create)|File Changes)[ \t]*",
                    line,
                    re.IGNORECASE,
                )
            )
            continue
        if not in_section:
            continue
        declaration = re.sub(
            r"^(?:(?:[-*+]|[0-9]+[.)]|#{3,6})[ \t]+|\|[ \t]*)", "", stripped
        ).lstrip()
        if not declaration.startswith("`"):
            continue
        token = re.match(r"`([^`]*)`", declaration)
        if token is None or not is_safe_scope_retraction_path(token[1]):
            return set()
        files.add(token[1])
    return files


def _fetch_planned_files(
    issue: int,
    *,
    github: StageGitHub,
    deadline_s: float,
    shutdown: Event,
) -> set[str] | None:
    """Read one authenticated plan within its deadline and cancellation scope.

    Return None only after a complete journal proves that no plan exists.
    Keep identity conflicts separate from temporary read failures.
    """
    try:
        with github.operation_deadline(deadline_s, shutdown=shutdown):
            discovered = github.discover_plan(issue)
    except CommentAliasConflictError:
        raise
    except (CancelledError, subprocess.SubprocessError, OSError, RuntimeError) as error:
        raise CommentJournalReadError(str(error)) from error
    if discovered.status is PlanDiscoveryStatus.IDENTITY_CONFLICT:
        raise CommentAliasConflictError(f"plan marker identity conflict: {discovered.error}")
    if discovered.status is PlanDiscoveryStatus.READ_ERROR:
        raise CommentJournalReadError(discovered.error or "plan admission read failed")
    if discovered.status is PlanDiscoveryStatus.ABSENT:
        return None
    if discovered.plan_text is None:
        raise CommentJournalReadError("plan admission returned no plan text")
    return _parse_planned_files(discovered.plan_text)


def order_for_implementation(issue_infos: Sequence[IssueInfo]) -> list[int]:
    """Order implementation-queue issues so dependencies come first.

    Topological-order gating via ``DependencyResolver.topological_sort``:
    builds a graph over exactly the given issues, keeping only dependency
    edges whose target is ALSO in the set — an edge to an issue outside the
    implementation queue cannot be ordered here and is dropped (fail-open;
    that dependency's own classification decides when it runs). Kahn's
    algorithm preserves the input order among issues at equal depth, so the
    result is deterministic.

    On a dependency cycle the original order is returned unchanged with a
    warning (fail-open: never wedge the queue over bad metadata).

    Args:
        issue_infos: Issue metadata (``number`` + ``dependencies``) for every
            issue currently admitted to the implementation queue.

    Returns:
        The issue numbers reordered so every in-set dependency precedes its
        dependents.

    """
    in_set = {info.number for info in issue_infos}
    queued_infos = [
        IssueInfo(
            number=info.number,
            title=info.title,
            dependencies=[dep for dep in info.dependencies if dep in in_set],
        )
        for info in issue_infos
    ]
    resolver = DependencyResolver(skip_closed=False)
    for info in queued_infos:
        resolver.add_issue(info)
    for info in queued_infos:
        for dep in info.dependencies:
            resolver.add_dependency(info.number, dep)
    try:
        return resolver.topological_sort()
    except CyclicDependencyError:
        LOG.warning(
            "dependency cycle among implementation-queue issues %s — keeping input order",
            sorted(in_set),
        )
        return [info.number for info in issue_infos]


def _filter_open_issues(
    repo: tuple[str, str],
    issue_numbers: list[int],
    *,
    github: StageGitHub,
    deadline_s: float,
    shutdown: Event,
) -> list[int]:
    """Exclude only confirmed closed issues through the bounded repository accessor.

    Keep a row when its state read fails or returns an unknown identity.
    Later classification must obtain complete issue facts before dispatch.
    """
    slug = f"{repo[0]}/{repo[1]}"
    kept: list[int] = []
    for num in issue_numbers:
        try:
            with github.operation_deadline(deadline_s, shutdown=shutdown):
                snapshot = github.gh_issue_json(num)
        except Exception as error:
            LOG.warning("[%s] issue #%s state is unknown: %s", slug, num, error)
            kept.append(num)
            continue
        if snapshot.get("number") == num and snapshot.get("state") == "CLOSED":
            LOG.info("[%s] issue #%s is closed; exclude it from this run", slug, num)
            continue
        kept.append(num)
    return kept


__all__ = [
    "PlanFileClaim",
    "_fetch_planned_files",
    "_filter_open_issues",
    "_parse_planned_files",
    "order_for_implementation",
]
