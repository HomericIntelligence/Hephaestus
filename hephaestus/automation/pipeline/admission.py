"""Admission control for the implementation queue.

Part of epic #1809. Provides:

- File-overlap serialization via greedy first-fit partitioning
  (:func:`_select_non_overlapping`, re-housed from ``loop_runner.py``, #1623)
- Dependency-based execution ordering via
  ``DependencyResolver.topological_sort`` (:func:`order_for_implementation`)
- Closed-issue filtering for explicit ``--issues`` lists
  (:func:`_filter_open_issues`, #1576)

The file-overlap guard (#1623) prevents concurrent plan execution on the same
source files, which would lead to merge conflicts when the first PR lands.

Dropped deliverable (documented): the per-repo in-flight cap helper
(``within_repo_cap``) is intentionally NOT implemented. The issue #1813
"# Implementation Plan" comment sanctions the drop: "justify or drop
``within_repo_cap`` (YAGNI — no named consumer)" — the cap is owned by
:meth:`~hephaestus.automation.pipeline.coordinator.Coordinator._admit`, where
the coordinator slice tracks per-repo worker slots and applies the cap at
dispatch time.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from hephaestus.automation.comment_identity import has_marker_alias
from hephaestus.automation.dependency_resolver import CyclicDependencyError, DependencyResolver
from hephaestus.automation.github_api import (
    fetch_issue_comments_metadata,
    gh_current_login,
    is_issue_closed,
    prefetch_issue_states,
)
from hephaestus.automation.models import IssueInfo
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    comment_marker_aliases,
)
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    PlanDiscoveryStatus,
    discover_plan_from_comments,
    normalize_issue_comments,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

LOG = logging.getLogger(__name__)

# Backticked repo-relative path inside a plan's Files sections, e.g.
# `hephaestus/automation/pipeline/stages/pr_review.py` or `pyproject.toml`.
# This is both the overlap reservation and the immutable publication manifest,
# so a valid top-level plan path must not be dropped.
_PLAN_FILE_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*(?:/[A-Za-z0-9_./-]+)*\.[A-Za-z0-9_]+)`")
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


def _fetch_planned_files(issue: int, repo: tuple[str, str] | None = None) -> set[str] | None:
    """Return the file set an issue's plan claims, or None if unknown.

    No planning marker or an empty fetch returns ``None``. The caller then
    dispatches the issue this round. If a shared plan or review marker exists,
    the full journal must prove its ownership and one unambiguous plan role.
    A conflict raises instead of making a foreign marker look like no plan.

    Args:
        issue: GitHub issue number.
        repo: ``(owner, name)`` of the repo owning *issue*. Required in the
            multi-repo loop; omitting it resolves the repo from the ambient
            working directory (#1795).

    Returns:
        The parsed plan file set, or None when no plan comment is present.

    """
    metadata = fetch_issue_comments_metadata(issue, repo=repo)
    planning_aliases = (
        *comment_marker_aliases(PLAN_CANONICAL_MARKER),
        *comment_marker_aliases(PLAN_REVIEW_CANONICAL_MARKER),
    )
    if not any(
        has_marker_alias(str(comment.get("body", "")), planning_aliases) for comment in metadata
    ):
        return None

    try:
        comments = normalize_issue_comments(metadata, viewer_login=gh_current_login() or "")
    except CommentJournalReadError as exc:
        raise RuntimeError(f"plan marker identity conflict: {exc}") from exc

    discovered = discover_plan_from_comments(comments)
    if discovered.status is PlanDiscoveryStatus.IDENTITY_CONFLICT:
        raise RuntimeError(f"plan marker identity conflict: {discovered.error}")
    if discovered.status is not PlanDiscoveryStatus.FOUND or discovered.plan_text is None:
        return None
    return _parse_planned_files(discovered.plan_text)


def _select_non_overlapping(
    issues: list[int],
    repo_of: Mapping[int, tuple[str, str]] | None = None,
    *,
    initial_claims: set[PlanFileClaim] | None = None,
    selected_claims: dict[int, set[PlanFileClaim]] | None = None,
) -> tuple[list[int], list[int]]:
    """Partition *issues* into (dispatch_now, defer_next_round).

    Greedy first-fit in the given order: an issue whose parsed plan file set
    intersects the union of already-claimed files in the same repository is
    deferred. ``initial_claims`` carries plans belonging to implementation
    items that were dispatched by an earlier drain and are still in flight.
    Unknown file set (no plan / parse failure) claims NO files and is always dispatched
    (fail-open). When active work already owns a conflicting known path, every
    queued item may defer until that work completes. Performs one serial
    GraphQL comment fetch per issue; only invoked in multi-worker rounds
    (guarded at the call site), so the cost is bounded by the issue count
    already being processed that round.

    Args:
        issues: The issue numbers to partition, in dispatch-priority order.
        repo_of: Maps each issue number to the ``(owner, name)`` that owns it.
            Resolved PER ISSUE, not per batch: the implementation queue is keyed
            by stage rather than by repo, so one round can legitimately hold
            issues from several repositories. An issue missing from the mapping
            falls back to ambient-CWD resolution (#1795).
        initial_claims: Repository-scoped plan-file claims owned by in-flight
            implementation jobs.  They reserve paths before queued jobs are
            considered, closing the gap between drain rounds.
        selected_claims: Optional host-owned sink populated with the exact
            plan snapshot that admitted each dispatched issue.  The
            coordinator reuses those claims when submitting the job, so an
            editable plan comment cannot change the reservation after the
            overlap decision.

    Returns:
        A ``(dispatch, defer)`` tuple of issue-number lists (order preserved).

    """
    repo_of = repo_of or {}
    claimed: set[PlanFileClaim] = set(initial_claims or ())
    dispatch: list[int] = []
    defer: list[int] = []
    for issue in issues:
        repo = repo_of.get(issue)
        planned = _fetch_planned_files(issue, repo=repo)
        claims = {(repo, path) for path in planned} if planned else set()
        if claims and (claims & claimed):
            LOG.info(
                "issue #%s deferred: plan files %s overlap in-flight peers",
                issue,
                sorted(path for _repo, path in claims & claimed),
            )
            defer.append(issue)
            continue
        if claims:
            claimed |= claims
        if selected_claims is not None:
            # Preserve even an empty snapshot.  It proves this drain selected
            # the issue fail-open and prevents submission from re-reading a
            # mutable plan comment after the overlap decision.
            selected_claims[issue] = set(claims)
        dispatch.append(issue)
    return dispatch, defer


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


def _filter_open_issues(repo: str, issue_numbers: list[int]) -> list[int]:
    """Drop CLOSED issues from an explicit ``--issues`` list (#1576).

    An operator-pinned ``cfg.issues`` list bypasses the ``--state open`` filter
    that auto-discovery applies, so a closed issue would otherwise be driven
    every loop and wrongly tagged ``state:skip`` by drive-green. States are
    fetched once via :func:`prefetch_issue_states` and checked with
    :func:`is_issue_closed`. On any lookup failure an issue is KEPT (fail-open:
    never silently drop work over a transient API blip).

    Args:
        repo: Repository name (for logging).
        issue_numbers: The explicit issue list.

    Returns:
        The subset that is not closed (order preserved).

    """
    try:
        cached_states = prefetch_issue_states(issue_numbers)
    except Exception as exc:  # transient API failure → keep all, don't drop work
        LOG.warning("[%s] could not prefetch issue states for closed-filter: %s", repo, exc)
        return issue_numbers
    kept: list[int] = []
    for num in issue_numbers:
        if is_issue_closed(num, cached_states):
            LOG.info("[%s] issue #%s is closed — excluding from phase loop", repo, num)
            continue
        kept.append(num)
    return kept


__all__ = [
    "PlanFileClaim",
    "_fetch_planned_files",
    "_filter_open_issues",
    "_parse_planned_files",
    "_select_non_overlapping",
    "order_for_implementation",
]
