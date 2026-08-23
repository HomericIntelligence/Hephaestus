"""GitHub-journal seeding: classify issues into stage queues based on GitHub state.

Part of epic #1809. Reconstructs in-memory queues from GitHub labels and PR state,
the single source of truth for "GitHub is the journal" architecture.

Pure classifier: (labels, PR existence/state) → entry stage, using **ordered label rank**:
- needs-plan(0) < plan-no-go(1) < plan-go(2) < implementation-no-go(3) < implementation-go(4)
- At-or-past comparisons, NEVER equality (verified lesson: `==` strands items already past target)

Entry routing (the binding contract is the classification table in
``docs/architecture.md`` §7 "Seeding and restart reconstruction"):

- ``state:skip`` → excluded (stage ``None``, logged)
- Closed issue + merged PR carrying an exact ``Closes #N`` line → finished
  (pass, idempotent)
- Open PR without an exclusive issue-level ``state:plan-go`` → planning
- Open PR + issue plan-GO + PR-level ``state:implementation-go`` → merge_wait
- Any other open PR with issue plan-GO → pr_review
- No PR, at-or-past ``state:plan-go`` → implementation
- No PR, ``state:plan-no-go`` → planning (amend path)
- No PR, ``state:plan-blocked`` → excluded until an external operator resolves
  the block and replaces the label with exactly one eligible plan state
- ``state:needs-plan`` / no state label → planning

Tracker labels and title inference are candidates, not skip authority. Seeding
routes both through planning so the independent semantic reviewer owns the only
automatic ``state:skip`` transition.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

from hephaestus.automation._review_utils import find_merged_pr_for_issue, find_pr_for_issue
from hephaestus.automation.github_api import (
    fetch_issue_info,
    gh_pr_label_names,
    gh_pr_state,
)
from hephaestus.automation.implementation_go_audit_receipt import PendingImplementationGoAudit
from hephaestus.automation.models import IssueState
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.requirements_recovery import (
    has_contaminated_issue_body,
    is_semantic_disposition_candidate,
    verified_finalized_plan,
)
from hephaestus.automation.state_labels import (
    ATHENA_FINALIZED_PLAN_LABEL,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
    has_label,
    is_epic,
    is_implementation_go,
)

LOG = logging.getLogger(__name__)
_WARNED_UNKNOWN_STATE_LABELS: set[str] = set()
_UNKNOWN_LABEL_WARNING_LOCK = Lock()

# Ordered label rank for at-or-past comparisons.
# Lower rank = earlier stage; higher rank = later stage.
_LABEL_RANK = {
    STATE_NEEDS_PLAN: 0,
    STATE_PLAN_NO_GO: 1,
    STATE_PLAN_GO: 2,
    STATE_IMPLEMENTATION_NO_GO: 3,
    STATE_IMPLEMENTATION_GO: 4,
}
_ISSUE_PLAN_STATE_LABELS = frozenset({STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO})


def read_pending_implementation_go_audit(
    github: Any, pr_number: int
) -> PendingImplementationGoAudit | None:
    """Read a typed pending receipt only from adapters that implement the API."""
    reader = getattr(type(github), "pending_implementation_go_audit", None)
    if not callable(reader):
        return None
    receipt = reader(github, pr_number)
    if receipt is not None and not isinstance(receipt, PendingImplementationGoAudit):
        raise TypeError("pending implementation-go audit receipt has invalid type")
    return receipt


#: Classification result: ``(stage, reason)``. ``stage is None`` means the
#: issue is EXCLUDED from the pipeline (state:skip) — exclusion is NOT
#: completion, so it is deliberately distinct from ``StageName.FINISHED``.
Classification = tuple[StageName | None, str]


@dataclass(frozen=True)
class IssueFacts:
    """GitHub state snapshot for a single issue.

    Attributes:
        number: GitHub issue number.
        title: Issue title (feeds the epic title-marker signal, #1669).
        is_epic: Whether this issue is an epic/roadmap semantic-review candidate.
        labels: Set of labels currently on this issue.
        body: Issue body used to hydrate downstream task prompts.
        pr_number: GitHub PR number if one exists and is live (open or
            merged), None otherwise.
        pr_is_open: True iff PR exists and is open.
        pr_is_merged: True iff PR exists and is merged.
        issue_is_closed: True iff GitHub currently reports the issue closed.
        pr_has_implementation_go: True iff the open PR carries
            ``state:implementation-go``.
        pr_has_implementation_no_go: True iff the open PR carries
            ``state:implementation-no-go``.
        authority_sanitized: Whether title/body text required prompt-safe
            sanitization and therefore cannot serve as exact authority.

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
    issue_is_closed: bool = False
    pr_has_implementation_go: bool = False
    pr_has_implementation_no_go: bool = False
    pending_implementation_go_audit: PendingImplementationGoAudit | None = None
    pending_implementation_go_label_confirmed: bool = False
    body: str = ""
    authority_sanitized: bool = False


@dataclass(frozen=True)
class EpicSkipTagObligation:
    """A required durable ``state:skip`` write before an epic is excluded."""

    issue: int


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
        issue_number: Linked issue number for directly-seeded PR entries, when
            the PR body carries the repo-policy ``Closes #N`` line.
        issue_title: Issue title copied into the issue WorkItem payload for
            planner/reviewer/implementer prompts.
        issue_body: Issue body copied into the issue WorkItem payload for
            planner/reviewer/implementer prompts.
        pr_description: PR body copied into a direct PR review payload.
        passed: Terminal result for entries clamped directly to ``finished``.
        non_code: Whether a passing terminal entry was semantically confirmed
            as non-code and therefore needs no merge receipt.
        non_code_labels: Supplemental labels required by a pending reviewed
            non-code disposition; ``state:skip`` is implicit.
        non_code_evidence_digest: Digest binding a pending non-code disposition
            to its independently reviewed issue and repository evidence.
        non_code_repository_revision: Exact repository revision included in
            the pending non-code evidence binding.
        non_code_explanation: Actor-owned rationale that must be restored
            before an obsolete disposition may apply ``state:skip``.
        non_code_retired: Whether the durable intent has been revoked and is
            retained only to finish provenance-bound skip cleanup.

    """

    kind: Literal["repo", "issue", "pr"]
    identifier: int | str
    stage: StageName | None
    reason: str
    pr_number: int | None = None
    issue_number: int | None = None
    issue_title: str = ""
    issue_body: str = ""
    pr_description: str = ""
    passed: bool = True
    skip_tag_obligation: EpicSkipTagObligation | None = None
    pending_implementation_go_audit: PendingImplementationGoAudit | None = None
    pending_implementation_go_label_confirmed: bool = False
    non_code: bool = False
    non_code_labels: tuple[str, ...] = ()
    non_code_evidence_digest: str = ""
    non_code_repository_revision: str = ""
    non_code_explanation: str = ""
    non_code_retired: bool = False


def _get_state_label(labels: set[str]) -> str | None:
    """Extract the single active issue plan-state label, or None when absent.

    Contradictory plan-state combinations are rejected by
    :func:`classify_issue` before this helper is called. Legacy issue-scoped
    implementation labels are ignored because implementation authority lives
    on pull requests. Unknown ``state:*`` labels are ignored after warning.

    Args:
        labels: Set of label names on an issue.

    Returns:
        The known state label, or None when no known label is present.

    """
    state_labels = sorted(lbl for lbl in labels if lbl.startswith("state:"))
    if not state_labels:
        return None

    known_state_labels = [lbl for lbl in state_labels if lbl in _ISSUE_PLAN_STATE_LABELS]
    unknown_state_labels = [
        lbl
        for lbl in state_labels
        if lbl not in _LABEL_RANK and lbl not in _ISSUE_PLAN_STATE_LABELS
    ]
    with _UNKNOWN_LABEL_WARNING_LOCK:
        newly_seen = sorted(set(unknown_state_labels) - _WARNED_UNKNOWN_STATE_LABELS)
        _WARNED_UNKNOWN_STATE_LABELS.update(newly_seen)
    if newly_seen:
        LOG.warning("Issue has unknown state labels ignored: %s", newly_seen)

    if not known_state_labels:
        return None

    if len(known_state_labels) > 1:
        raise ValueError(f"contradictory state labels: {known_state_labels}")
    return known_state_labels[0]


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


def _requirements_recovery_reason(
    facts: IssueFacts,
) -> str | None:
    """Return the planning-admission reason for semantic recovery, if any."""
    if facts.authority_sanitized:
        # GitHub transport normalization may make the body safe to pass to
        # subprocesses, but that projection is not planning authority. Route
        # through planning's fresh fail-closed snapshot before a stale
        # plan-GO or PR verdict can select implementation or merge-wait.
        return f"#{facts.number} requires unsanitized authority authentication"
    issue_body = facts.body if isinstance(facts.body, str) else ""
    issue_title = facts.title if isinstance(facts.title, str) else ""
    finalized = verified_finalized_plan(issue_body)
    has_finalized_evidence = ATHENA_FINALIZED_PLAN_LABEL in facts.labels
    if finalized is not None:
        # Seeding cannot authenticate the latest issue-body editor. Always
        # enter planning's no-model verification fast path, even after an
        # earlier observation added the metadata label.
        return f"#{facts.number} requires finalized-plan authentication"
    if finalized is None and has_finalized_evidence:
        return f"#{facts.number} finalized planning epoch changed"
    if has_contaminated_issue_body(issue_body):
        return f"#{facts.number} requires autonomous requirements recovery"
    if facts.is_epic or is_semantic_disposition_candidate(issue_title, issue_body):
        return f"#{facts.number} requires semantic disposition review"
    return None


def _issue_exclusion_reason(facts: IssueFacts) -> str | None:
    """Return an operator/external exclusion reason before state routing."""
    if STATE_SKIP in facts.labels:
        return f"#{facts.number} tagged {STATE_SKIP}"
    if STATE_PLAN_BLOCKED in facts.labels and verified_finalized_plan(facts.body) is None:
        return f"#{facts.number} tagged {STATE_PLAN_BLOCKED} awaiting external intervention"
    return None


def _classify_open_pr(facts: IssueFacts, state_label: str | None) -> Classification:
    """Route one open PR; only an approved issue plan reaches special routes."""
    # An implementation artifact cannot substitute for an approved issue
    # plan.  Keep planning authoritative even when a PR already exists or
    # carries a downstream implementation verdict.
    if state_label != STATE_PLAN_GO:
        return StageName.PLANNING, f"#{facts.number} open PR missing {STATE_PLAN_GO}"
    # The loop-owned approval label records review eligibility, not durable
    # merge authority.  A restart sends it to merge_wait, which confirms an
    # unarmed PR before returning to review; a matching current-process
    # proof attempts one ordinary conditional merge. No queue stage
    # creates, disables, adopts, or polls automatic merge.
    if facts.pending_implementation_go_audit is not None:
        return StageName.PR_REVIEW, f"#{facts.number} pending implementation-go audit"
    if facts.pr_has_implementation_go:
        return (
            StageName.MERGE_WAIT,
            f"#{facts.number} open PR with {STATE_IMPLEMENTATION_GO}",
        )
    if facts.pr_has_implementation_no_go:
        return StageName.PR_REVIEW, f"#{facts.number} open PR awaiting review"
    # Only the PR-level label above has approval semantics.  Issue labels
    # never select a special open-PR route.
    return StageName.PR_REVIEW, f"#{facts.number} open PR awaiting review"


def classify_issue(facts: IssueFacts) -> Classification:
    """Classify an issue into a single entry stage based on GitHub state.

    Exclusion (``state:skip``) is distinct from completion: excluded
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
    if reason := _issue_exclusion_reason(facts):
        LOG.info("issue excluded: %s", reason)
        return None, reason

    if verified_finalized_plan(facts.body) is not None:
        # A self-verifying finalized body must reach planning's authenticated
        # no-model path even when a stale sibling label survived an earlier
        # interrupted normalization. Planning owns the atomic label repair.
        return StageName.PLANNING, f"#{facts.number} requires finalized-plan authentication"

    active_states = sorted(label for label in facts.labels if label in _ISSUE_PLAN_STATE_LABELS)
    if len(active_states) > 1:
        reason = f"#{facts.number} has contradictory state labels: {active_states}"
        LOG.warning("issue excluded: %s", reason)
        return None, reason

    # Completion requires the current issue state as well as a merged PR that
    # carries the exact closing reference. A reopened issue may retain the
    # historical PR relationship, but it is actionable again.
    if facts.issue_is_closed and facts.pr_is_merged:
        return StageName.FINISHED, f"#{facts.number} PR merged (idempotent)"

    if recovery_reason := _requirements_recovery_reason(facts):
        return StageName.PLANNING, recovery_reason

    # Extract the active state label
    state_label = _get_state_label(facts.labels)

    # Routing logic: open PR path
    if facts.pr_is_open:
        return _classify_open_pr(facts, state_label)

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
        issue_is_closed=issue_info.state == IssueState.CLOSED,
        pr_has_implementation_go=pr_has_implementation_go,
        pr_has_implementation_no_go=pr_has_implementation_no_go,
        authority_sanitized=issue_info.authority_sanitized,
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
    if not isinstance(issue_data, dict):
        raise TypeError("issue snapshot must be a JSON object")
    if issue_data.get("number") != issue_number:
        raise ValueError("issue snapshot number does not match the requested issue")
    state = issue_data.get("state")
    if not isinstance(state, str) or state.upper() not in {
        IssueState.OPEN.value,
        IssueState.CLOSED.value,
    }:
        raise ValueError("issue snapshot state must be exactly OPEN or CLOSED")
    raw_labels = issue_data.get("labels")
    if not isinstance(raw_labels, list):
        raise ValueError("issue snapshot labels must be a list")
    if any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str)
        for label in raw_labels
    ):
        raise ValueError("issue snapshot labels must contain string names")
    labels = {label["name"] for label in raw_labels if label["name"]}
    title = str(issue_data.get("title") or "")
    body = str(issue_data.get("body") or "")
    issue_is_closed = state.upper() == IssueState.CLOSED.value
    epic = is_epic(sorted(labels), title)
    pr_is_open = False
    pr_is_merged = False
    pr_has_implementation_go = False
    pr_has_implementation_no_go = False
    pending_implementation_go_audit = None
    pr_number: int | None = github.find_pr_for_issue(issue_number)
    if pr_number is not None:
        pr_is_open = True
        pr_has_implementation_go, pr_has_implementation_no_go = (
            github.pr_has_implementation_state_label(pr_number)
        )
        pending_implementation_go_audit = read_pending_implementation_go_audit(github, pr_number)
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
        issue_is_closed=issue_is_closed,
        pr_has_implementation_go=pr_has_implementation_go,
        pr_has_implementation_no_go=pr_has_implementation_no_go,
        pending_implementation_go_audit=pending_implementation_go_audit,
        authority_sanitized=issue_data.get("authoritySanitized") is True,
    )


def seed_entry_from_facts(facts: IssueFacts) -> SeedEntry:
    """Build one issue seed entry and its required durable-write obligation.

    Seeding remains pure: it can declare, but never execute, the write that
    makes an epic exclusion durable.  Callers must discharge the typed
    obligation before they consume the resulting ``stage=None`` entry.

    Args:
        facts: Normalized GitHub state for one issue.

    Returns:
        The classified entry, with an epic skip-tag obligation only when the
        issue is an untagged epic.

    """
    stage, reason = classify_issue(facts)
    obligation = (
        EpicSkipTagObligation(issue=facts.number)
        if facts.is_epic and STATE_SKIP not in facts.labels
        else None
    )
    return SeedEntry(
        kind="issue",
        identifier=facts.number,
        stage=stage,
        reason=reason,
        pr_number=facts.pr_number if facts.pr_is_open else None,
        issue_title=facts.title,
        issue_body=facts.body,
        skip_tag_obligation=obligation,
        pending_implementation_go_audit=facts.pending_implementation_go_audit,
        pending_implementation_go_label_confirmed=bool(
            facts.pending_implementation_go_audit is not None and facts.pr_has_implementation_go
        ),
    )


def seed_from_cli(
    repos: Sequence[str],
    issues: Sequence[int],
    prs: Sequence[int],
    github: Any | None = None,
) -> list[SeedEntry]:
    """Map CLI scope args (``--repos`` / ``--issues`` / ``--prs``) to queue pushes.

    Pure planning plus thin fetch — no mutations:

    - ``repos`` → one :attr:`StageName.REPO` entry each (discovery seeds).
    - ``issues`` → :func:`seed_issue` + :func:`classify_issue` per issue.
    - ``prs`` → tri-state classification mirroring ``classify_issue``'s
      open-PR routing: merged direct PR -> FINISHED (idempotent), closed PR ->
      excluded, open PR with ``state:implementation-go`` -> MERGE_WAIT, open PR
      without it -> PR_REVIEW. A failed state/label fetch reads as
      "open, not yet reviewed" (-> pr_review), matching the existing
      ``_review_existing_pr`` fail-open-to-review semantics.
      When *github* is given (a repo-scoped accessor, e.g.
      :class:`~hephaestus.automation.pipeline_github.PipelineGitHub`), both
      the state read and the label read are scoped to that repo via
      ``github.gh_pr_state`` / ``github.pr_has_implementation_state_label``
      — the same accessor :func:`seed_issue_from_github` uses for
      ``--issues`` — instead of the module-level
      :func:`~hephaestus.automation.github_api.gh_pr_state` /
      :func:`~hephaestus.automation.github_api.gh_pr_label_names`, which
      resolve against the ambient/current repo and can misclassify a PR
      number that collides across repos in a multi-repo run.

    Args:
        repos: Repository names to seed for discovery.
        issues: Issue numbers to classify directly.
        prs: PR numbers to route by merge/close state, then
            implementation-review label.
        github: Optional repo-scoped GitHub accessor for the ``prs`` state
            and label reads. When ``None``, falls back to the ambient
            :func:`~hephaestus.automation.github_api.gh_pr_state` /
            :func:`~hephaestus.automation.github_api.gh_pr_label_names`.

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
        entries.append(seed_entry_from_facts(facts))
    for pr in prs:
        pr_state = github.gh_pr_state(pr) if github is not None else gh_pr_state(pr)
        state = str((pr_state or {}).get("state") or "").upper()
        if state == "MERGED" or (pr_state or {}).get("mergedAt"):
            entries.append(
                SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=StageName.FINISHED,
                    reason=f"PR #{pr} merged (idempotent)",
                    pr_number=pr,
                )
            )
            continue
        if state == "CLOSED":
            entries.append(
                SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=None,
                    reason=f"PR #{pr} closed (not merged) — excluded",
                    pr_number=pr,
                )
            )
            continue

        pending_audit = None
        if github is not None:
            has_go, _has_no_go = github.pr_has_implementation_state_label(pr)
            pending_audit = read_pending_implementation_go_audit(github, pr)
        else:
            has_go = is_implementation_go(gh_pr_label_names(pr))
        if pending_audit is not None:
            entries.append(
                SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=StageName.PR_REVIEW,
                    reason=f"PR #{pr} has a pending implementation-go audit",
                    pr_number=pr,
                    pending_implementation_go_audit=pending_audit,
                    pending_implementation_go_label_confirmed=has_go,
                )
            )
        elif has_go:
            entries.append(
                SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=StageName.MERGE_WAIT,
                    reason=f"PR #{pr} carries {STATE_IMPLEMENTATION_GO}",
                    pr_number=pr,
                )
            )
        else:
            entries.append(
                SeedEntry(
                    kind="pr",
                    identifier=pr,
                    stage=StageName.PR_REVIEW,
                    reason=f"PR #{pr} without {STATE_IMPLEMENTATION_GO} — awaiting review",
                    pr_number=pr,
                )
            )
    return entries


__all__ = [
    "Classification",
    "IssueFacts",
    "SeedEntry",
    "classify_issue",
    "seed_entry_from_facts",
    "seed_from_cli",
    "seed_issue",
]
