"""GitHub issue/PR state-label vocabulary for the automation pipeline.

The automation pipeline uses three mutually-exclusive ``state:*`` labels as the
single source of truth for an issue's plan-review status. This module is the
authoritative definition of those labels, the PR-scoped implementation-review
labels, and the small helpers that interpret them; the reviewer, planner,
implementer, and the org-wide provisioning script all import from here.

State machine
-------------

::

    [issue opened]
        │
        ▼
    state:needs-plan ──(planner+reviewer run)──▶ state:plan-no-go ─┐
                                                     ▲             │
                                                     │             │ (next iteration GO)
                                                     └────(NOGO)───┘
                                                                   │
                                                                   ▼
                                                            state:plan-go
                                                            (terminal — implementer trusts
                                                             this exclusively; never
                                                             re-plans or re-reviews)

At most one of the three labels should be present on an issue at any time;
each apply-state helper removes the other two as it sets its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

# Single source of truth for the three plan-state labels. All label-aware code
# in the pipeline imports these constants; do not hard-code the names.
STATE_NEEDS_PLAN = "state:needs-plan"
STATE_PLAN_NO_GO = "state:plan-no-go"
STATE_PLAN_GO = "state:plan-go"

#: All three state labels in one tuple — useful for "ensure none of these are
#: present" / "remove all of these" operations.
ALL_STATE_LABELS = (STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO)

# PR-scoped implementation-review state labels. These deliberately live outside
# ALL_STATE_LABELS so issue-level plan state remains independent from PR review
# state.
STATE_IMPLEMENTATION_NO_GO = "state:implementation-no-go"
STATE_IMPLEMENTATION_GO = "state:implementation-go"
ALL_IMPLEMENTATION_STATE_LABELS = (STATE_IMPLEMENTATION_NO_GO, STATE_IMPLEMENTATION_GO)

#: Manual override: when present on an issue OR its PR, automation normally
#: skips that work item entirely (#1083). Unlike the plan/implementation state
#: labels this is operator-applied (or auto-applied by the review loop when it
#: exhausts its review budget without a GO — the budget starts at
#: MAX_REVIEW_ITERATIONS and extends up to MAX_REVIEW_ITERATIONS_HARD_CAP while
#: the loop keeps making progress, #1554), and it is independent of all
#: other state labels — so it deliberately lives outside the tuples above.
#: The implementer has one narrow stale-state recovery path for explicitly
#: selected issues that also carry ``state:plan-go`` and have no open PR.
STATE_SKIP = "state:skip"

#: Label names that mark a TRACKING issue (an epic / roadmap) rather than a code
#: task. Epics and roadmaps are checklists of child work; the planning loop must
#: not plan or implement them directly (their deliverable is a body edit, not a
#: PR). Matched case-insensitively. Native GitHub issue types are not exposed by
#: the installed ``gh`` CLI, so a label name or a title substring is the only
#: available signal — see :func:`is_epic`.
EPIC_LABELS = ("epic", "roadmap")

#: Per-label colour (hex without leading ``#``) and short description. The
#: provisioning script (``hephaestus-ensure-state-labels``) uses these when
#: creating the labels on a repo so they have a consistent look across the org.
STATE_LABEL_SPECS: dict[str, dict[str, str]] = {
    STATE_NEEDS_PLAN: {
        "color": "fbca04",  # amber — attention needed
        "description": "Issue has no plan yet; planner should run.",
    },
    STATE_PLAN_NO_GO: {
        "color": "d93f0b",  # red — blocked
        "description": (
            "Plan-reviewer's latest verdict was NOGO (or NOGO-exhausted); re-plan next loop."
        ),
    },
    STATE_PLAN_GO: {
        "color": "0e8a16",  # green — approved
        "description": "Plan approved by reviewer; implementer may proceed.",
    },
    STATE_IMPLEMENTATION_NO_GO: {
        "color": "d93f0b",  # red — blocked
        "description": (
            "Implementation-reviewer's latest verdict was NOGO; implementer should revise."
        ),
    },
    STATE_IMPLEMENTATION_GO: {
        "color": "0e8a16",  # green — approved
        "description": "Implementation approved by reviewer; drive-green may proceed.",
    },
    STATE_SKIP: {
        "color": "ededed",  # grey — intentionally inert
        "description": "Automation normally skips this issue/PR in every phase.",
    },
}


def has_label(labels: Iterable[str], target: str) -> bool:
    """Return ``True`` iff ``target`` is in the iterable of label names.

    Thin convenience wrapper so callers don't import ``set`` semantics. Useful
    when reading the labels list off an issue dict.
    """
    return target in set(labels)


def is_plan_go(labels: Iterable[str]) -> bool:
    """Return ``True`` iff the issue is in the terminal ``state:plan-go`` state.

    This is the gate the implementer trusts: once GO, no further planning or
    review iterations are performed.
    """
    return has_label(labels, STATE_PLAN_GO)


def is_plan_no_go(labels: Iterable[str]) -> bool:
    """Return ``True`` iff the issue currently carries ``state:plan-no-go``.

    Indicates a NOGO verdict from the most recent reviewer pass (or
    NOGO-exhausted after MAX_REVIEW_ITERATIONS). The planner will re-plan it
    on the next loop.
    """
    return has_label(labels, STATE_PLAN_NO_GO)


def is_implementation_go(labels: Iterable[str]) -> bool:
    """Return ``True`` iff a PR carries the implementation-review GO label."""
    return has_label(labels, STATE_IMPLEMENTATION_GO)


def is_skipped(labels: Iterable[str]) -> bool:
    """Return ``True`` iff the issue/PR carries the ``state:skip`` override.

    When set, automation normally skips the work item (#1083). Honored on both
    issues and their PRs; callers may still add narrower stale-state recovery
    policy where they have enough context.
    """
    return has_label(labels, STATE_SKIP)


def is_epic(labels: Iterable[str], title: str = "") -> bool:
    """Return ``True`` iff the issue is an epic/roadmap tracking issue.

    An issue is treated as an epic when **either** signal matches (both
    case-insensitive):

    * it carries a label whose name is in :data:`EPIC_LABELS`
      (``epic`` / ``roadmap``), **or**
    * its ``title`` contains one of those markers as a substring.

    The title fallback catches the convention even when the label was not
    applied. Pure function (no I/O) so both discovery paths and unit tests can
    call it directly.

    Args:
        labels: Label names on the issue.
        title: The issue title (optional; empty disables the title signal).

    Returns:
        ``True`` if the issue should be excluded from the planning loop.

    """
    if {label.lower() for label in labels} & set(EPIC_LABELS):
        return True
    lowered_title = title.lower()
    return any(marker in lowered_title for marker in EPIC_LABELS)


def partition_epics(
    issues_meta: Sequence[dict[str, Any]],
) -> tuple[list[int], list[int]]:
    """Split issue metadata into ``(kept, epics)`` by :func:`is_epic`.

    Pure helper shared by both discovery chokepoints so the loop and the
    standalone planner exclude epics identically (DRY). Each element is a
    metadata dict with ``number`` and the optional ``labels`` / ``title``
    signals; missing keys are treated as absent (an issue with no labels and
    no title is simply kept).

    Args:
        issues_meta: Issue metadata dicts, each with at least ``number`` and
            optionally ``labels`` (list of names) and ``title``.

    Returns:
        ``(kept_numbers, epic_numbers)``, both sorted ascending. ``kept`` are
        the real work items the loop should plan; ``epics`` are the tracking
        issues to exclude (and tag ``state:skip``).

    """
    kept: list[int] = []
    epics: list[int] = []
    for meta in issues_meta:
        number = int(meta["number"])
        labels = meta.get("labels") or []
        title = meta.get("title") or ""
        (epics if is_epic(labels, title) else kept).append(number)
    return sorted(kept), sorted(epics)


def needs_plan(labels: Iterable[str]) -> bool:
    """Return ``True`` iff the issue carries ``state:needs-plan`` (or no state label).

    Issues with no state label are treated as needing a plan — the auto-label
    workflow tags freshly-opened issues with ``state:needs-plan``, and the
    absence of any state label is functionally equivalent (planner runs).

    Terminal states win: if ``state:plan-go`` or ``state:plan-no-go`` is also
    present (e.g. mid label-churn during the reviewer's apply/remove sequence),
    the issue is NOT in the needs-plan state regardless of whether
    ``state:needs-plan`` was already removed.
    """
    label_set = set(labels)
    return STATE_PLAN_GO not in label_set and STATE_PLAN_NO_GO not in label_set


def apply_plan_verdict(*, is_go: bool) -> tuple[str, list[str]]:
    """Compute the atomic plan-state transition for a reviewer verdict.

    Pure: returns (label_to_add, labels_to_remove). The caller performs the
    GitHub writes and any logging. Shared (#1814) so the plan_review stage and
    seeding compute the transition identically.

    On GO   → add state:plan-go,    remove [state:plan-no-go, state:needs-plan].
    On NOGO → add state:plan-no-go, remove [state:plan-go,     state:needs-plan].

    Args:
        is_go: ``True`` when the reviewer's verdict is GO; ``False`` for NOGO.

    Returns:
        A tuple (label_to_add, labels_to_remove) where label_to_add is the
        single label to apply and labels_to_remove is the list of labels to
        remove.

    """
    if is_go:
        return STATE_PLAN_GO, [STATE_PLAN_NO_GO, STATE_NEEDS_PLAN]
    return STATE_PLAN_NO_GO, [STATE_PLAN_GO, STATE_NEEDS_PLAN]


def enter_planning_transition() -> tuple[list[str], list[str]]:
    """Compute the atomic label swap for (re-)entering the planning stage.

    Pure: returns (labels_to_add, labels_to_remove). The caller performs the
    single atomic GitHub write. When plan_review exhausts its iteration budget
    it ADDS ``state:plan-no-go`` and fails back to planning (routing.py "nogo"
    route). Re-entry must restore ``state:needs-plan`` AND remove the sibling
    rejection/terminal labels in ONE transition, or (a) the mutually-exclusive
    label invariant is transiently violated and (b) the labels-first
    ``has_existing_plan`` gate stays False forever, so VERIFY can never ADVANCE
    and the re-plan cycle deadlocks (#1857).

    Restores the documented state-machine edge
    ``state:plan-no-go ──re-plan──▶ needs-plan`` (module docstring lifecycle
    diagram): add [state:needs-plan]; remove [state:plan-no-go, state:plan-go].

    Returns:
        A tuple (labels_to_add, labels_to_remove): ``[STATE_NEEDS_PLAN]`` and
        the two sibling labels to clear.

    """
    return [STATE_NEEDS_PLAN], [STATE_PLAN_NO_GO, STATE_PLAN_GO]
