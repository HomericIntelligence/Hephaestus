"""Tests for ``hephaestus.automation.state_labels``.

Pure-function module — the helpers interpret a labels iterable from a GitHub
issue and report which state the issue is in. The state machine is documented
in the module docstring; these tests cover every transition.
"""

from __future__ import annotations

import pytest

from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ALL_STATE_LABELS,
    EPIC_LABELS,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_LABEL_SPECS,
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
    apply_plan_verdict,
    has_label,
    is_epic,
    is_implementation_go,
    is_plan_go,
    is_plan_no_go,
    is_skipped,
    needs_plan,
    partition_epics,
)


class TestLabelVocabulary:
    """The three state-label names + the ALL tuple are stable identifiers."""

    def test_three_distinct_state_labels(self) -> None:
        assert len({STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO}) == 3

    def test_all_state_labels_covers_each(self) -> None:
        assert set(ALL_STATE_LABELS) == {
            STATE_NEEDS_PLAN,
            STATE_PLAN_NO_GO,
            STATE_PLAN_GO,
        }

    def test_state_prefix(self) -> None:
        """Every state label uses the ``state:`` family prefix."""
        for label in ALL_STATE_LABELS:
            assert label.startswith("state:")

    def test_label_specs_cover_every_label(self) -> None:
        """The provisioning script needs a colour+description for each label.

        Specs must cover every plan-state label, every PR-scoped
        implementation-review label, and the independent ``state:skip``
        override (#1083).
        """
        assert set(ALL_STATE_LABELS) <= set(STATE_LABEL_SPECS.keys())
        assert set(ALL_IMPLEMENTATION_STATE_LABELS) <= set(STATE_LABEL_SPECS.keys())
        assert STATE_SKIP in STATE_LABEL_SPECS
        for spec in STATE_LABEL_SPECS.values():
            assert "color" in spec
            assert "description" in spec
            # Hex colour without leading '#'.
            assert len(spec["color"]) == 6
            int(spec["color"], 16)

    def test_skip_label_is_independent_of_plan_state(self) -> None:
        """``state:skip`` is an override, not a plan-state label."""
        assert STATE_SKIP not in ALL_STATE_LABELS
        assert STATE_SKIP.startswith("state:")

    def test_implementation_labels_are_independent_of_plan_state(self) -> None:
        """Implementation-review state is PR-scoped, not part of issue plan state."""
        assert set(ALL_IMPLEMENTATION_STATE_LABELS) == {
            STATE_IMPLEMENTATION_NO_GO,
            STATE_IMPLEMENTATION_GO,
        }
        assert set(ALL_IMPLEMENTATION_STATE_LABELS).isdisjoint(ALL_STATE_LABELS)

    def test_is_skipped(self) -> None:
        assert is_skipped(["bug", STATE_SKIP]) is True
        assert is_skipped([STATE_PLAN_GO]) is False
        assert is_skipped([]) is False

    def test_is_implementation_go(self) -> None:
        assert is_implementation_go([STATE_IMPLEMENTATION_GO]) is True
        assert is_implementation_go([STATE_IMPLEMENTATION_NO_GO]) is False
        assert is_implementation_go([]) is False


class TestHasLabel:
    """``has_label`` is a thin convenience wrapper around ``in``."""

    def test_present(self) -> None:
        assert has_label(["bug", STATE_PLAN_GO], STATE_PLAN_GO) is True

    def test_absent(self) -> None:
        assert has_label(["bug", "enhancement"], STATE_PLAN_GO) is False

    def test_empty(self) -> None:
        assert has_label([], STATE_PLAN_GO) is False


class TestIsPlanGo:
    """``state:plan-go`` is the terminal-approved state."""

    def test_label_present_returns_true(self) -> None:
        assert is_plan_go([STATE_PLAN_GO, "bug"]) is True

    def test_label_absent_returns_false(self) -> None:
        assert is_plan_go(["bug", "enhancement"]) is False

    def test_no_go_label_does_not_imply_go(self) -> None:
        assert is_plan_go([STATE_PLAN_NO_GO]) is False

    def test_needs_plan_label_does_not_imply_go(self) -> None:
        assert is_plan_go([STATE_NEEDS_PLAN]) is False


class TestIsPlanNoGo:
    """``state:plan-no-go`` indicates the latest reviewer pass was NOGO."""

    def test_label_present_returns_true(self) -> None:
        assert is_plan_no_go([STATE_PLAN_NO_GO]) is True

    def test_label_absent_returns_false(self) -> None:
        assert is_plan_no_go(["bug"]) is False

    def test_go_label_does_not_imply_no_go(self) -> None:
        assert is_plan_no_go([STATE_PLAN_GO]) is False


class TestNeedsPlan:
    """Issues need a plan when ``state:needs-plan`` is set OR no state label is set."""

    def test_explicit_needs_plan_label(self) -> None:
        assert needs_plan([STATE_NEEDS_PLAN, "bug"]) is True

    def test_no_state_label_at_all(self) -> None:
        """Absence of any state label is functionally 'needs a plan'."""
        assert needs_plan(["bug", "enhancement"]) is True

    def test_empty_labels_needs_plan(self) -> None:
        assert needs_plan([]) is True

    def test_plan_go_does_not_need_plan(self) -> None:
        assert needs_plan([STATE_PLAN_GO]) is False

    def test_plan_no_go_does_not_need_plan(self) -> None:
        """A NOGO issue has a plan; it's just being re-iterated. Not needs-plan."""
        assert needs_plan([STATE_PLAN_NO_GO]) is False

    @pytest.mark.parametrize(
        "labels",
        [
            [STATE_PLAN_GO, STATE_NEEDS_PLAN],
            [STATE_PLAN_NO_GO, STATE_NEEDS_PLAN],
        ],
    )
    def test_terminal_state_wins_over_needs_plan_when_both_present(self, labels: list[str]) -> None:
        """Terminal label wins over needs-plan when both are present.

        Defensive against label churn during the reviewer's apply/remove
        sequence: if a terminal label is present, ``needs_plan`` reports
        False even when ``state:needs-plan`` was not yet removed.
        """
        assert needs_plan(labels) is False


class TestIsEpic:
    """``is_epic`` excludes epic/roadmap TRACKING issues from the planning loop.

    An issue is an epic iff it carries an ``epic``/``roadmap`` label
    (case-insensitive) OR its title contains ``epic``/``roadmap``. Native
    GitHub issue types are not exposed by the installed ``gh``, so label +
    title are the only available signals.
    """

    def test_epic_label_marks_epic(self) -> None:
        assert is_epic(["epic"]) is True

    def test_roadmap_label_marks_epic(self) -> None:
        assert is_epic(["roadmap"]) is True

    def test_label_match_is_case_insensitive(self) -> None:
        assert is_epic(["Epic"]) is True
        assert is_epic(["ROADMAP"]) is True

    def test_title_substring_marks_epic(self) -> None:
        """Belt-and-suspenders: catch the convention even when unlabelled."""
        assert is_epic([], title="Epic: ship the new pipeline") is True
        assert is_epic([], title="Q3 Roadmap tracking") is True

    def test_title_match_is_case_insensitive(self) -> None:
        assert is_epic([], title="EPIC umbrella issue") is True

    def test_plain_bug_is_not_epic(self) -> None:
        assert is_epic(["bug", "severity:major"], title="Fix crash in parser") is False

    def test_empty_inputs_not_epic(self) -> None:
        assert is_epic([]) is False
        assert is_epic([], title="") is False

    def test_other_state_labels_not_epic(self) -> None:
        assert is_epic([STATE_NEEDS_PLAN, STATE_PLAN_GO]) is False

    def test_epic_labels_constant_contents(self) -> None:
        assert set(EPIC_LABELS) == {"epic", "roadmap"}


class TestApplyPlanVerdict:
    """``apply_plan_verdict`` returns the (add, remove) labels for a reviewer verdict.

    Pure function: no I/O, no logging, no GitHub calls. Both the legacy
    planner_review_loop and the new plan_review stage call it to ensure
    identical transitions (#1814).
    """

    def test_go_verdict_adds_go_removes_others(self) -> None:
        add, remove = apply_plan_verdict(is_go=True)
        assert add == STATE_PLAN_GO
        assert set(remove) == {STATE_PLAN_NO_GO, STATE_NEEDS_PLAN}

    def test_nogo_verdict_adds_nogo_removes_others(self) -> None:
        add, remove = apply_plan_verdict(is_go=False)
        assert add == STATE_PLAN_NO_GO
        assert set(remove) == {STATE_PLAN_GO, STATE_NEEDS_PLAN}


class TestPartitionEpics:
    """``partition_epics`` splits issue metadata into (kept, epics).

    Pure function shared by both discovery chokepoints (the loop's
    ``_list_open_issue_numbers`` and the planner's ``filter``). Input is a list
    of ``{"number", "labels", "title"}`` dicts; output is two ascending number
    lists so callers stay deterministic.
    """

    def test_keeps_real_issues_and_extracts_epics(self) -> None:
        meta = [
            {"number": 3, "labels": ["bug"], "title": "Fix crash"},
            {"number": 1, "labels": ["epic"], "title": "Umbrella"},
            {"number": 2, "labels": ["feature"], "title": "Q3 Roadmap"},
        ]
        kept, epics = partition_epics(meta)
        assert kept == [3]
        assert epics == [1, 2]

    def test_no_epics_returns_all_kept(self) -> None:
        meta = [
            {"number": 5, "labels": ["bug"], "title": "a"},
            {"number": 4, "labels": [], "title": "b"},
        ]
        kept, epics = partition_epics(meta)
        assert kept == [4, 5]
        assert epics == []

    def test_results_sorted_ascending(self) -> None:
        meta = [
            {"number": 30, "labels": [], "title": "x"},
            {"number": 10, "labels": ["epic"], "title": "y"},
            {"number": 20, "labels": [], "title": "z"},
            {"number": 5, "labels": ["roadmap"], "title": "w"},
        ]
        kept, epics = partition_epics(meta)
        assert kept == [20, 30]
        assert epics == [5, 10]

    def test_empty_input(self) -> None:
        assert partition_epics([]) == ([], [])

    def test_missing_keys_default_safely(self) -> None:
        """Missing labels/title must not raise — treat as absent signals."""
        meta = [{"number": 7}]
        kept, epics = partition_epics(meta)
        assert kept == [7]
        assert epics == []
