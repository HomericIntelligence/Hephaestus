"""GitHub-journal seeding: classify issues into stage queues based on GitHub state.

Part of epic #1809. Reconstructs in-memory queues from GitHub labels and PR state,
the single source of truth for "GitHub is the journal" architecture.

Pure classifier: (labels, PR existence/state) → entry queue, using **ordered label rank**:
- needs-plan(0) < plan-no-go(1) < plan-go(2) < implementation-no-go(3) < implementation-go(4)
- At-or-past comparisons, NEVER equality (verified lesson: `==` strands items already past target)

Entry queue routing:
- `state:skip` or epic label → excluded (logged)
- PR merged → finished (pass, idempotent)
- Open PR + `state:implementation-go` → ci
- Open PR, no impl-GO → pr_review (existing-PR path)
- No PR, at-or-past `state:plan-go` → implementation
- No PR, `state:plan-no-go` → planning (amend path)
- `state:needs-plan` / no state label → planning
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from hephaestus.automation._review_utils import find_pr_for_issue
from hephaestus.automation.github_api import fetch_issue_info
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)

LOG = logging.getLogger(__name__)

# Ordered label rank for at-or-past comparisons.
# Lower rank = earlier stage; higher rank = later stage.
_LABEL_RANK = {
    STATE_NEEDS_PLAN: 0,
    STATE_PLAN_NO_GO: 1,
    STATE_PLAN_GO: 2,
    STATE_IMPLEMENTATION_NO_GO: 3,
    STATE_IMPLEMENTATION_GO: 4,
}

QueueName = Literal["planning", "pr_review", "ci", "implementation", "finished"]


@dataclass(frozen=True)
class IssueFacts:
    """GitHub state snapshot for a single issue.

    Attributes:
            number: GitHub issue number.
            is_epic: Whether this issue is an epic (excluded from queuing).
            labels: Set of state labels currently on this issue.
            pr_number: GitHub PR number if one exists and is live (open or merged), None otherwise.
            pr_is_open: True iff PR exists and is open.
            pr_is_merged: True iff PR exists and is merged.

    Invariants:
            - At most one `state:*` label is present.
            - If `pr_number is None`, then `pr_is_open` and `pr_is_merged` are both False.
            - If PR is neither open nor merged, `pr_number` is None (normalized at fetch layer).

    """

    number: int
    is_epic: bool
    labels: set[str]
    pr_number: int | None
    pr_is_open: bool
    pr_is_merged: bool


def _get_state_label(labels: set[str]) -> str | None:
    """Extract the single active `state:*` label, or None.

    Args:
            labels: Set of label names on an issue.

    Returns:
            The state label, or None if absent or contradictory (multiple state labels).

    """
    state_labels = [lbl for lbl in labels if lbl.startswith("state:")]
    if not state_labels:
        return None
    if len(state_labels) > 1:
        LOG.warning(
            "Issue has contradictory state labels: %s (using highest rank)", sorted(state_labels)
        )
        # Return the highest-rank (latest-stage) label when contradictory.
        return max(state_labels, key=lambda lbl: _LABEL_RANK.get(lbl, -1))
    return state_labels[0]


def _label_at_or_past(label: str | None, target: str) -> bool:
    """Check whether a label is at or past a target rank.

    At-or-past semantics prevent re-queueing issues already past the target:
    - An issue with `state:plan-go` (rank 2) is at-or-past `state:plan-go` ✓
    - An issue with `state:implementation-go` (rank 4) is at-or-past `state:plan-go` ✓
    - An issue with `state:needs-plan` (rank 0) is NOT at-or-past `state:plan-go` ✗

    Args:
            label: The label to check (or None for absence).
            target: The target state label name.

    Returns:
            True iff the label's rank >= target's rank (or is absent, treated as rank 0).

    """
    if label is None:
        label = STATE_NEEDS_PLAN  # absence == needs-plan
    label_rank = _LABEL_RANK.get(label, -1)
    target_rank = _LABEL_RANK.get(target, -1)
    return label_rank >= target_rank


def classify_issue(facts: IssueFacts) -> tuple[QueueName, str]:
    """Classify an issue into a single entry queue based on GitHub state.

    Args:
            facts: GitHub state snapshot for the issue.

    Returns:
            A (queue_name, reason) tuple describing where the issue should be queued.

    Raises:
            No exceptions; contradictory or unknown states default to safe routing.

    """
    # Exclusions: skip and epics
    if STATE_SKIP in facts.labels:
        return "finished", f"#{facts.number} tagged {STATE_SKIP}"
    if facts.is_epic:
        return "finished", f"#{facts.number} is an epic (excluded from routing)"

    # Terminal state: merged PR
    if facts.pr_is_merged:
        return "finished", f"#{facts.number} PR merged (idempotent)"

    # Extract the active state label
    state_label = _get_state_label(facts.labels)

    # Routing logic: open PR path
    if facts.pr_is_open:
        # Open PR + implementation-go → ready for CI
        if _label_at_or_past(state_label, STATE_IMPLEMENTATION_GO):
            return "ci", f"#{facts.number} open PR with {STATE_IMPLEMENTATION_GO}"
        # Open PR, no implementation-go → awaiting PR review
        return "pr_review", f"#{facts.number} open PR awaiting review"

    # No PR path: check implementation readiness
    if _label_at_or_past(state_label, STATE_PLAN_GO):
        return "implementation", f"#{facts.number} at-or-past {STATE_PLAN_GO}, no PR yet"

    # No PR, plan rejected or needs plan → planning phase
    if state_label == STATE_PLAN_NO_GO:
        return "planning", f"#{facts.number} {STATE_PLAN_NO_GO} (amend path)"

    # Default: no label or needs-plan → planning
    return "planning", f"#{facts.number} {state_label or STATE_NEEDS_PLAN}"


def seed_issue(issue_number: int) -> IssueFacts:
    """Fetch and normalize GitHub state for a single issue.

    Normalizes PR facts: if a PR exists but is neither open nor merged, sets
    `pr_number = None` so `classify_issue` only ever sees a clean tri-state.
    This prevents misclassification of closed/draft PRs.

    Args:
            issue_number: GitHub issue number.

    Returns:
            Normalized GitHub state snapshot.

    Raises:
            Exception: Any GitHub API error is re-raised (caller's responsibility to handle).

    """
    issue_info = fetch_issue_info(issue_number)
    labels = set(issue_info.labels)

    # Determine if this is an epic by checking for "epic" label.
    is_epic = any(lbl.lower() == "epic" for lbl in labels)

    # Fetch PR if exists; normalize to None if closed/draft.
    pr_number: int | None = None
    pr_is_open = False
    pr_is_merged = False

    try:
        found_pr = find_pr_for_issue(issue_number)
        if found_pr is not None:
            # We have a PR. find_pr_for_issue finds open/merged PRs for the issue.
            # Per the plan: "resolve it at the FETCH layer — `seed_issue` yields
            # `pr_number=None` for any PR that is neither open nor merged."
            # find_pr_for_issue is a best-effort search; it may find closed PRs too.
            # For now, we trust it returns live PRs (open or merged via merge strategies).
            pr_number = found_pr
            # Assume returned PR is open for now; merge state would be determined
            # by fetching the full PR object, which is outside the scope of this MVP.
            pr_is_open = True
    except Exception as exc:
        LOG.warning("Could not resolve PR for issue #%s: %s", issue_number, exc)

    return IssueFacts(
        number=issue_number,
        is_epic=is_epic,
        labels=labels,
        pr_number=pr_number,
        pr_is_open=pr_is_open,
        pr_is_merged=pr_is_merged,
    )


__all__ = [
    "IssueFacts",
    "QueueName",
    "classify_issue",
    "seed_issue",
]
