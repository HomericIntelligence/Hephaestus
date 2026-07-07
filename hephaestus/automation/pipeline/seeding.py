"""GitHub-journal seeding: classify issues into stage queues based on GitHub state.

Part of epic #1809. Reconstructs in-memory queues from GitHub labels and PR state,
the single source of truth for "GitHub is the journal" architecture.

Pure classifier: (labels, PR existence/state) → entry stage, using **ordered label rank**:
- needs-plan(0) < plan-no-go(1) < plan-go(2) < implementation-no-go(3) < implementation-go(4)
- At-or-past comparisons, NEVER equality (verified lesson: `==` strands items already past target)

Entry routing (the binding contract is the classification table in
``docs/AUTOMATION_LOOP_ARCHITECTURE.md`` "Seeding and reconstruction"):

- ``state:skip`` or epic → excluded (stage ``None``, logged)
- PR merged → finished (pass, idempotent)
- Open PR + at-or-past ``state:implementation-go`` → ci
- Open PR, no impl-GO → pr_review (existing-PR path)
- No PR, at-or-past ``state:plan-go`` → implementation
- No PR, ``state:plan-no-go`` → planning (amend path)
- ``state:needs-plan`` / no state label → planning

Write-path boundary (epic tagging)
----------------------------------
Per the doc row "Epic tagging is the one seeding write; done BEFORE excluding",
an untagged epic must receive ``state:skip``. The pipeline mutator guard
(``tests/unit/automation/pipeline/test_pipeline_architecture.py``) forbids
GitHub mutations in this module, so seeding only SURFACES the tag need: an
epic without ``state:skip`` is excluded with reason prefix
:data:`EPIC_NEEDS_SKIP_TAG`, and the caller — the coordinator slice (#1817) —
executes the actual label write via the existing ``github_api.skip_epics``
chokepoint before honoring the exclusion.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from hephaestus.automation._review_utils import find_merged_pr_for_issue, find_pr_for_issue
from hephaestus.automation.github_api import fetch_issue_info, gh_pr_label_names
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
    has_label,
    is_epic,
    is_implementation_go,
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

#: Exclusion-reason prefix surfacing the one sanctioned seeding write to the
#: caller: an epic that still lacks ``state:skip`` must be tagged (via the
#: existing ``github_api.skip_epics`` chokepoint, executed by the coordinator,
#: #1817) BEFORE the exclusion is final. See module docstring.
EPIC_NEEDS_SKIP_TAG = "epic_needs_skip_tag"

#: Classification result: ``(stage, reason)``. ``stage is None`` means the
#: issue is EXCLUDED from the pipeline (state:skip / epic) — exclusion is NOT
#: completion, so it is deliberately distinct from ``StageName.FINISHED``.
Classification = tuple[StageName | None, str]


@dataclass(frozen=True)
class IssueFacts:
    """GitHub state snapshot for a single issue.

    Attributes:
        number: GitHub issue number.
        title: Issue title (feeds the epic title-marker signal, #1669).
        is_epic: Whether this issue is an epic/roadmap (excluded from queuing).
        labels: Set of labels currently on this issue.
        body: Issue body used to hydrate downstream task prompts.
        pr_number: GitHub PR number if one exists and is live (open or
            merged), None otherwise.
        pr_is_open: True iff PR exists and is open.
        pr_is_merged: True iff PR exists and is merged.
        pr_has_implementation_go: True iff the open PR carries
            ``state:implementation-go``.
        pr_has_implementation_no_go: True iff the open PR carries
            ``state:implementation-no-go``.

    Invariants (established by :func:`seed_issue`'s tri-state fetch):
        - Exactly one of {no live PR, open PR, merged PR} holds:
          ``pr_number is None`` ⇔ ``not pr_is_open and not pr_is_merged``,
          and ``pr_is_open``/``pr_is_merged`` are mutually exclusive.
        - A PR that is neither open nor merged (closed/abandoned) is
          normalized to ``pr_number = None`` at the fetch layer, so the
          classifier can never fall through on a dead PR.

    """

    number: int
    title: str
    is_epic: bool
    labels: set[str]
    pr_number: int | None
    pr_is_open: bool
    pr_is_merged: bool
    pr_has_implementation_go: bool = False
    pr_has_implementation_no_go: bool = False
    body: str = ""


@dataclass(frozen=True)
class SeedEntry:
    """One planned queue push produced by :func:`seed_from_cli`.

    Attributes:
        kind: Source CLI scope of the entry (``repo`` / ``issue`` / ``pr``).
        identifier: Repo name, issue number, or PR number.
        stage: Entry stage, or ``None`` when the item is excluded.
        reason: Human-readable classification reason (logged by the caller).
        pr_number: Open PR number for directly-seeded issue entries, when one
            exists. Repo discovery carries this in products; direct ``--issues``
            seeding needs the same value so downstream PR stages have context.
        issue_title: Issue title copied into the issue WorkItem payload for
            planner/reviewer/implementer prompts.
        issue_body: Issue body copied into the issue WorkItem payload for
            planner/reviewer/implementer prompts.

    """

    kind: Literal["repo", "issue", "pr"]
    identifier: int | str
    stage: StageName | None
    reason: str
    pr_number: int | None = None
    issue_title: str = ""
    issue_body: str = ""


def _get_state_label(labels: set[str]) -> str | None:
    """Extract the single active ``state:*`` label, or None when absent.

    Contradictory combinations (multiple ``state:*`` labels) resolve
    deterministically to the HIGHEST-rank (latest-stage) label and emit a
    warning — they never return None.

    Args:
        labels: Set of label names on an issue.

    Returns:
        The state label; the highest-rank one when contradictory; None only
        when no ``state:*`` label is present.

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
    - An issue with ``state:plan-go`` (rank 2) is at-or-past ``state:plan-go``
    - An issue with ``state:implementation-go`` (rank 4) is at-or-past
      ``state:plan-go``
    - An issue with ``state:needs-plan`` (rank 0) is NOT at-or-past
      ``state:plan-go``

    Args:
        label: The label to check (or None for absence).
        target: The target state label name.

    Returns:
        True iff the label's rank >= target's rank (absence == needs-plan,
        rank 0).

    Raises:
        ValueError: If ``target`` is not a known ordered state label.

    """
    if label is None:
        label = STATE_NEEDS_PLAN  # absence == needs-plan
    if target not in _LABEL_RANK:
        raise ValueError(f"Unknown target state label: {target}")
    label_rank = _LABEL_RANK.get(label, -1)
    target_rank = _LABEL_RANK[target]
    return label_rank >= target_rank


def classify_issue(facts: IssueFacts) -> Classification:
    """Classify an issue into a single entry stage based on GitHub state.

    Exclusion (``state:skip`` / epic) is distinct from completion: excluded
    issues return ``stage=None`` (and are logged), while genuinely finished
    work (merged PR) returns :attr:`StageName.FINISHED`.

    Args:
        facts: GitHub state snapshot for the issue.

    Returns:
        A ``(stage, reason)`` :data:`Classification`; ``stage is None`` means
        excluded.

    """
    # Exclusions: skip wins over everything (operator-only, absolute — it
    # carries no rank and never enters the rank comparison).
    if STATE_SKIP in facts.labels:
        reason = f"#{facts.number} tagged {STATE_SKIP}"
        LOG.info("issue excluded: %s", reason)
        return None, reason
    if facts.is_epic:
        # Untagged epic: surface the sanctioned write to the caller (see
        # module docstring — the state:skip write executes at the coordinator
        # via the existing skip_epics chokepoint, #1817).
        reason = f"{EPIC_NEEDS_SKIP_TAG}: #{facts.number} is an epic without {STATE_SKIP}"
        LOG.info("issue excluded: %s", reason)
        return None, reason

    # Terminal state: merged PR
    if facts.pr_is_merged:
        return StageName.FINISHED, f"#{facts.number} PR merged (idempotent)"

    # Extract the active state label
    state_label = _get_state_label(facts.labels)

    # Routing logic: open PR path
    if facts.pr_is_open:
        # Open PR + PR-level implementation-go → ready for CI. The
        # issue-label fallback preserves compatibility with pre-PR-label
        # snapshots while PR-level no-go remains authoritative.
        if facts.pr_has_implementation_go:
            return StageName.CI, f"#{facts.number} open PR with {STATE_IMPLEMENTATION_GO}"
        if facts.pr_has_implementation_no_go:
            return StageName.PR_REVIEW, f"#{facts.number} open PR awaiting review"
        if _label_at_or_past(state_label, STATE_IMPLEMENTATION_GO):
            return StageName.CI, f"#{facts.number} open PR with {STATE_IMPLEMENTATION_GO}"
        # Open PR, no implementation-go → awaiting PR review
        return StageName.PR_REVIEW, f"#{facts.number} open PR awaiting review"

    # No PR path: check implementation readiness
    if _label_at_or_past(state_label, STATE_PLAN_GO):
        return StageName.IMPLEMENTATION, f"#{facts.number} at-or-past {STATE_PLAN_GO}, no PR yet"

    # No PR, plan rejected or needs plan → planning phase
    if state_label == STATE_PLAN_NO_GO:
        return StageName.PLANNING, f"#{facts.number} {STATE_PLAN_NO_GO} (amend path)"

    # Default: no label or needs-plan → planning
    return StageName.PLANNING, f"#{facts.number} {state_label or STATE_NEEDS_PLAN}"


def seed_issue(issue_number: int) -> IssueFacts:
    """Fetch and normalize GitHub state for a single issue (tri-state PR).

    PR facts are a real tri-state fetch: the open-PR lookup
    (:func:`find_pr_for_issue`) runs first; on a miss, the merged-PR lookup
    (:func:`find_merged_pr_for_issue`) runs so merged work classifies as
    finished instead of being re-queued after a restart. A PR that is neither
    open nor merged (closed/abandoned) is invisible to both lookups and is
    normalized to ``pr_number = None``, so :func:`classify_issue` only ever
    sees a clean {no live PR | open PR | merged PR} tri-state.

    Fail-closed: any GitHub error — from the issue fetch or either PR lookup —
    propagates. Swallowing a PR-probe failure would misclassify toward
    IMPLEMENTATION and cause duplicate-PR churn.

    Args:
        issue_number: GitHub issue number.

    Returns:
        Normalized GitHub state snapshot.

    Raises:
        Exception: Any GitHub API error from the issue fetch or the PR
            lookups is re-raised (caller's responsibility to handle).

    """
    issue_info = fetch_issue_info(issue_number)
    labels = set(issue_info.labels)

    # Epic detection: label (epic/roadmap) OR title marker, per #1669.
    epic = is_epic(issue_info.labels, issue_info.title)

    # Tri-state PR fetch: open first, then merged; closed PRs surface in
    # neither lookup (normalized to "no live PR"). No try/except: fail-closed.
    pr_is_open = False
    pr_is_merged = False
    pr_has_implementation_go = False
    pr_has_implementation_no_go = False
    pr_number: int | None = find_pr_for_issue(issue_number)
    if pr_number is not None:
        pr_is_open = True
        pr_labels = gh_pr_label_names(pr_number)
        pr_has_implementation_go = is_implementation_go(pr_labels)
        pr_has_implementation_no_go = has_label(pr_labels, STATE_IMPLEMENTATION_NO_GO)
    else:
        pr_number = find_merged_pr_for_issue(issue_number)
        if pr_number is not None:
            pr_is_merged = True

    return IssueFacts(
        number=issue_number,
        title=issue_info.title,
        is_epic=epic,
        labels=labels,
        body=issue_info.body,
        pr_number=pr_number,
        pr_is_open=pr_is_open,
        pr_is_merged=pr_is_merged,
        pr_has_implementation_go=pr_has_implementation_go,
        pr_has_implementation_no_go=pr_has_implementation_no_go,
    )


def seed_issue_from_github(issue_number: int, github: Any) -> IssueFacts:
    """Fetch and normalize repo-scoped GitHub state for a single issue (tri-state PR).

    PR facts are a real tri-state fetch through the provided StageGitHub
    accessor: ``github.find_pr_for_issue`` runs first; on a miss,
    ``github.find_merged_pr_for_issue`` runs so merged work classifies as
    finished instead of being re-queued after a restart. A PR that is neither
    open nor merged (closed/abandoned) is invisible to both lookups and is
    normalized to ``pr_number = None``, so :func:`classify_issue` only ever
    sees a clean {no live PR | open PR | merged PR} tri-state.

    Fail-closed: any GitHub error -- from the issue fetch or either PR lookup
    -- propagates. Swallowing a PR-probe failure would misclassify toward
    IMPLEMENTATION and cause duplicate-PR churn.

    Args:
        issue_number: GitHub issue number.
        github: Repo-scoped StageGitHub accessor.

    Returns:
        Normalized GitHub state snapshot.

    Raises:
        Exception: Any GitHub API error from the issue fetch or the PR
            lookups is re-raised (caller's responsibility to handle).

    """
    issue_data = github.gh_issue_json(issue_number)
    raw_labels = issue_data.get("labels", []) if isinstance(issue_data, dict) else []
    labels = {
        str(label.get("name", ""))
        for label in raw_labels
        if isinstance(label, dict) and label.get("name")
    }
    title = str(issue_data.get("title") or "") if isinstance(issue_data, dict) else ""
    body = str(issue_data.get("body") or "") if isinstance(issue_data, dict) else ""
    epic = is_epic(sorted(labels), title)

    pr_is_open = False
    pr_is_merged = False
    pr_has_implementation_go = False
    pr_has_implementation_no_go = False
    pr_number: int | None = github.find_pr_for_issue(issue_number)
    if pr_number is not None:
        pr_is_open = True
        pr_has_implementation_go, pr_has_implementation_no_go = (
            github.pr_has_implementation_state_label(pr_number)
        )
    else:
        pr_number = github.find_merged_pr_for_issue(issue_number)
        if pr_number is not None:
            pr_is_merged = True

    return IssueFacts(
        number=issue_number,
        title=title,
        is_epic=epic,
        labels=labels,
        body=body,
        pr_number=pr_number,
        pr_is_open=pr_is_open,
        pr_is_merged=pr_is_merged,
        pr_has_implementation_go=pr_has_implementation_go,
        pr_has_implementation_no_go=pr_has_implementation_no_go,
    )


def seed_from_cli(
    repos: Sequence[str],
    issues: Sequence[int],
    prs: Sequence[int],
) -> list[SeedEntry]:
    """Map CLI scope args (``--repos`` / ``--issues`` / ``--prs``) to queue pushes.

    Pure planning plus thin fetch — no mutations:

    - ``repos`` → one :attr:`StageName.REPO` entry each (discovery seeds).
    - ``issues`` → :func:`seed_issue` + :func:`classify_issue` per issue.
    - ``prs`` → :attr:`StageName.CI` when the PR carries
      ``state:implementation-go``, else :attr:`StageName.PR_REVIEW` —
      mirroring the legacy existing-PR review semantics: GO short-circuits
      review, and a failed label fetch reads as "not yet reviewed" (→ pr_review).

    Args:
        repos: Repository names to seed for discovery.
        issues: Issue numbers to classify directly.
        prs: PR numbers to route by implementation-review label.

    Returns:
        Planned queue pushes, in the given order (repos, issues, prs).

    """
    entries: list[SeedEntry] = [
        SeedEntry(
            kind="repo", identifier=repo, stage=StageName.REPO, reason=f"{repo} CLI repo seed"
        )
        for repo in repos
    ]
    for issue in issues:
        facts = seed_issue(issue)
        stage, reason = classify_issue(facts)
        entries.append(
            SeedEntry(
                kind="issue",
                identifier=issue,
                stage=stage,
                reason=reason,
                pr_number=facts.pr_number if facts.pr_is_open else None,
                issue_title=facts.title,
                issue_body=facts.body,
            )
        )
    for pr in prs:
        labels = gh_pr_label_names(pr)
        if is_implementation_go(labels):
            entries.append(
                SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=StageName.CI,
                    reason=f"PR #{pr} carries {STATE_IMPLEMENTATION_GO}",
                )
            )
        else:
            entries.append(
                SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=StageName.PR_REVIEW,
                    reason=f"PR #{pr} without {STATE_IMPLEMENTATION_GO} — awaiting review",
                )
            )
    return entries


__all__ = [
    "EPIC_NEEDS_SKIP_TAG",
    "Classification",
    "IssueFacts",
    "SeedEntry",
    "classify_issue",
    "seed_from_cli",
    "seed_issue",
]
