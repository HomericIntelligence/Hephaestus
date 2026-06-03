"""GitHub-issue state-label vocabulary for the automation pipeline.

The automation pipeline uses three mutually-exclusive ``state:*`` labels as the
single source of truth for an issue's plan-review status. This module is the
authoritative definition of those labels and the small helpers that interpret
them; the reviewer, planner, implementer, and the org-wide provisioning script
all import from here.

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

from collections.abc import Iterable

# Single source of truth for the three plan-state labels. All label-aware code
# in the pipeline imports these constants — do not hard-code the names.
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
