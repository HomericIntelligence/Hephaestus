"""GitHub-journal seeding: classification of issues into stage queues.

Tests the classifier that maps GitHub state (labels, PR, epic) → entry stage,
the tri-state fetch layer (open / merged / closed-normalized / no PR), epic
detection, and the CLI seed mapping.
"""

from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import state_labels
from hephaestus.automation.implementation_go_audit_receipt import PendingImplementationGoAudit
from hephaestus.automation.models import IssueState
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.seeding import (
    IssueFacts,
    SeedEntry,
    _label_at_or_past,
    classify_issue,
    seed_entry_from_facts,
    seed_from_cli,
    seed_issue,
    seed_issue_from_github,
)
from hephaestus.automation.review_audit import ReviewAudit
from hephaestus.automation.state_labels import (
    ATHENA_FINALIZED_PLAN_LABEL,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)


def _finalized_body() -> str:
    placeholder = (
        "## Why\n\nUse the reviewed implementation plan.\n\n"
        f"<!-- athena:finalize-plan R={'a' * 64} P=123456789:{'b' * 64} "
        f"V=987654321:{'c' * 64} F=<F> -->"
    )
    digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
    return placeholder.replace("F=<F>", f"F={digest}")


def _facts(
    *,
    number: int = 1,
    title: str = "A task",
    body: str = "",
    is_epic: bool = False,
    labels: set[str] | None = None,
    pr_number: int | None = None,
    pr_is_open: bool = False,
    pr_is_merged: bool = False,
    issue_is_closed: bool = False,
    pr_has_implementation_go: bool = False,
    pr_has_implementation_no_go: bool = False,
    authority_sanitized: bool = False,
) -> IssueFacts:
    """Build IssueFacts with defaults for classifier-matrix tests."""
    return IssueFacts(
        number=number,
        title=title,
        is_epic=is_epic,
        labels=labels or set(),
        pr_number=pr_number,
        pr_is_open=pr_is_open,
        pr_is_merged=pr_is_merged,
        issue_is_closed=issue_is_closed,
        pr_has_implementation_go=pr_has_implementation_go,
        pr_has_implementation_no_go=pr_has_implementation_no_go,
        body=body,
        authority_sanitized=authority_sanitized,
    )


class TestClassifyIssue:
    """Classifier routing matrix: GitHub state → entry stage."""

    def test_skip_label_excluded_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Issue tagged state:skip is excluded (stage None) and the exclusion is logged."""
        with caplog.at_level(logging.INFO, logger="hephaestus.automation.pipeline.seeding"):
            stage, reason = classify_issue(_facts(labels={STATE_SKIP}))
        assert stage is None
        assert "state:skip" in reason
        assert any("excluded" in record.message for record in caplog.records)

    def test_labeled_epic_routes_to_independent_planning_review(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="hephaestus.automation.pipeline.seeding"):
            stage, reason = classify_issue(_facts(is_epic=True, labels={"epic"}))
        assert stage is StageName.PLANNING
        assert "semantic disposition review" in reason
        assert not any("excluded" in record.message for record in caplog.records)

    def test_title_inferred_epic_routes_to_independent_planning_review(self) -> None:
        stage, reason = classify_issue(_facts(title="Epic: reliability", is_epic=True))

        assert stage is StageName.PLANNING
        assert "semantic disposition review" in reason

    def test_contaminated_plan_go_body_routes_back_to_planning(self) -> None:
        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO},
                body="<!-- hephaestus-plan:canonical -->\nStale plan",
            )
        )

        assert stage is StageName.PLANNING
        assert "requirements recovery" in reason

    def test_finalized_plan_without_evidence_routes_to_authentication(self) -> None:
        stage, reason = classify_issue(_facts(labels={STATE_PLAN_GO}, body=_finalized_body()))

        assert stage is StageName.PLANNING
        assert "authentication" in reason

    def test_indented_invalid_finalization_cannot_use_stale_plan_go(self) -> None:
        body = _finalized_body().replace(
            "<!-- athena:finalize-plan ",
            "  <!-- athena:finalize-plan ",
        )

        stage, reason = classify_issue(_facts(labels={STATE_PLAN_GO}, body=body))

        assert stage is StageName.PLANNING
        assert "requirements recovery" in reason

    def test_invalid_backtick_fence_cannot_hide_claim_from_stale_plan_go(self) -> None:
        marker = _finalized_body().splitlines()[-1]
        body = f"```bad`info\n  {marker}\n```"

        stage, reason = classify_issue(_facts(labels={STATE_PLAN_GO}, body=body))

        assert stage is StageName.PLANNING
        assert "requirements recovery" in reason

    def test_list_fence_cannot_hide_dedented_claim_from_stale_plan_go(self) -> None:
        marker = _finalized_body().splitlines()[-1]
        body = f"- example\n\n  ```text\n  fenced text\n{marker}"

        stage, reason = classify_issue(_facts(labels={STATE_PLAN_GO}, body=body))

        assert stage is StageName.PLANNING
        assert "requirements recovery" in reason

    def test_unicode_separator_cannot_hide_claim_from_stale_plan_go(self) -> None:
        marker = _finalized_body().splitlines()[-1]
        body = f"prefix\u2028same CommonMark line\n{marker}"

        stage, reason = classify_issue(_facts(labels={STATE_PLAN_GO}, body=body))

        assert stage is StageName.PLANNING
        assert "requirements recovery" in reason

    def test_nested_raw_html_marker_invalidates_finalized_epoch_evidence(self) -> None:
        placeholder = (
            "<!--\n"
            f"<!-- athena:finalize-plan R={'a' * 64} P=123456789:{'b' * 64} "
            f"V=987654321:{'c' * 64} F=<F> -->\n"
            "-->"
        )
        digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
        body = placeholder.replace("F=<F>", f"F={digest}")

        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL},
                body=body,
            )
        )

        assert stage is StageName.PLANNING
        assert "finalized planning epoch changed" in reason

    def test_shared_plan_review_comment_invalidates_finalized_epoch_evidence(self) -> None:
        placeholder = (
            "## Why\n\nPreserve independently reviewed authority.\n\n"
            f"<!-- athena:finalize-plan R={'a' * 64} P=123456789:{'b' * 64} "
            f"V=123456789:{'c' * 64} F=<F> -->"
        )
        digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
        body = placeholder.replace("F=<F>", f"F={digest}")

        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL},
                body=body,
            )
        )

        assert stage is StageName.PLANNING
        assert "finalized planning epoch changed" in reason

    def test_finalized_plan_with_evidence_routes_to_no_model_authentication(self) -> None:
        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL},
                body=_finalized_body(),
            )
        )

        assert stage is StageName.PLANNING
        assert "authentication" in reason

    def test_finalized_plan_with_open_pr_still_routes_to_authentication(self) -> None:
        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL},
                body=_finalized_body(),
                pr_number=88,
                pr_is_open=True,
                pr_has_implementation_go=True,
            )
        )

        assert stage is StageName.PLANNING
        assert "authentication" in reason

    def test_finalized_plan_supersedes_blocked_only_after_planning_authentication(self) -> None:
        stage, reason = classify_issue(_facts(labels={STATE_PLAN_BLOCKED}, body=_finalized_body()))

        assert stage is StageName.PLANNING
        assert "authentication" in reason

    def test_finalized_plan_with_stale_sibling_labels_routes_to_authentication(self) -> None:
        stage, reason = classify_issue(
            _facts(
                labels={STATE_NEEDS_PLAN, STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL},
                body=_finalized_body(),
            )
        )

        assert stage is StageName.PLANNING
        assert "authentication" in reason

    def test_removed_finalized_marker_invalidates_stale_plan_go(self) -> None:
        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL},
                body="## Revised requirements\n\nThe behavior materially changed.",
            )
        )

        assert stage is StageName.PLANNING
        assert "finalized planning epoch changed" in reason

    def test_drifted_finalized_marker_with_open_pr_still_routes_to_recovery(self) -> None:
        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL},
                body=f"{_finalized_body()}\nchanged",
                pr_number=88,
                pr_is_open=True,
            )
        )

        assert stage is StageName.PLANNING
        assert "finalized planning epoch changed" in reason

    @pytest.mark.parametrize(
        ("pr_number", "pr_is_open", "pr_has_implementation_go"),
        [
            (None, False, False),
            (88, True, True),
        ],
    )
    def test_sanitized_plan_go_authority_always_reenters_planning(
        self,
        pr_number: int | None,
        pr_is_open: bool,
        pr_has_implementation_go: bool,
    ) -> None:
        """Sanitized issue text cannot authorize implementation or merge-wait."""
        stage, reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO},
                body="Human requirements with a stripped transport byte",
                authority_sanitized=True,
                pr_number=pr_number,
                pr_is_open=pr_is_open,
                pr_has_implementation_go=pr_has_implementation_go,
            )
        )

        assert stage is StageName.PLANNING
        assert "sanitized authority" in reason

    def test_epic_already_tagged_skip_needs_no_retag(self) -> None:
        """An epic that already carries state:skip excludes via skip — no tag flag."""
        facts = _facts(is_epic=True, labels={STATE_SKIP})
        stage, reason = classify_issue(facts)
        assert stage is None
        assert STATE_SKIP in reason

    def test_skip_wins_over_plan_go(self) -> None:
        """state:skip + state:plan-go → excluded; skip is absolute and never ranked."""
        stage, reason = classify_issue(_facts(labels={STATE_SKIP, STATE_PLAN_GO}))
        assert stage is None
        assert STATE_SKIP in reason

    def test_plan_blocked_is_excluded_until_external_input_arrives(self) -> None:
        stage, reason = classify_issue(_facts(labels={state_labels.STATE_PLAN_BLOCKED}))

        assert stage is None
        assert state_labels.STATE_PLAN_BLOCKED in reason

    def test_plan_blocked_cannot_be_revived_by_comment_text(self) -> None:
        stage, reason = classify_issue(_facts(labels={state_labels.STATE_PLAN_BLOCKED}))

        assert stage is None
        assert "external intervention" in reason

    def test_pr_merged_finished(self) -> None:
        """Merged PR is genuinely finished (pass, idempotent) — NOT an exclusion."""
        stage, reason = classify_issue(
            _facts(
                labels={STATE_IMPLEMENTATION_GO},
                pr_number=42,
                pr_is_merged=True,
                issue_is_closed=True,
            )
        )
        assert stage is StageName.FINISHED
        assert "merged" in reason

    def test_open_issue_with_historical_merged_pr_routes_by_current_label(self) -> None:
        """An open/reopened issue must not be completed by a historic merged PR."""
        stage, reason = classify_issue(
            _facts(labels={STATE_PLAN_GO}, pr_number=42, pr_is_merged=True)
        )

        assert stage is StageName.IMPLEMENTATION
        assert reason == f"#1 at-or-past {STATE_PLAN_GO}, no PR yet"

    def test_open_pr_with_issue_impl_go_without_plan_go_routes_to_planning(self) -> None:
        """A downstream issue label cannot replace exact plan approval."""
        stage, _reason = classify_issue(
            _facts(labels={STATE_IMPLEMENTATION_GO}, pr_number=42, pr_is_open=True)
        )
        assert stage is StageName.PLANNING

    def test_open_pr_with_pr_impl_go_routes_to_merge_wait(self) -> None:
        """Only the PR-level loop approval routes directly to merge wait."""
        stage, _reason = classify_issue(
            _facts(
                labels={STATE_PLAN_GO},
                pr_number=42,
                pr_is_open=True,
                pr_has_implementation_go=True,
            )
        )
        assert stage is StageName.MERGE_WAIT

    def test_open_pr_without_impl_go_routes_to_pr_review(self) -> None:
        """Open PR without implementation-go awaits PR review."""
        stage, reason = classify_issue(
            _facts(labels={STATE_PLAN_GO}, pr_number=42, pr_is_open=True)
        )
        assert stage is StageName.PR_REVIEW
        assert "review" in reason

    @pytest.mark.parametrize(
        "labels",
        [set(), {STATE_NEEDS_PLAN}, {STATE_PLAN_NO_GO}, {STATE_IMPLEMENTATION_NO_GO}],
    )
    def test_open_pr_without_exclusive_plan_go_routes_to_planning(self, labels: set[str]) -> None:
        """An implementation PR cannot substitute for an approved issue plan."""
        stage, reason = classify_issue(
            _facts(
                labels=labels,
                pr_number=42,
                pr_is_open=True,
                pr_has_implementation_go=True,
            )
        )

        assert stage is StageName.PLANNING
        assert "plan" in reason

    def test_open_pr_with_impl_no_go_without_plan_go_routes_to_planning(self) -> None:
        """Issue implementation rejection cannot replace exact plan approval."""
        stage, reason = classify_issue(
            _facts(labels={STATE_IMPLEMENTATION_NO_GO}, pr_number=42, pr_is_open=True)
        )
        assert stage is StageName.PLANNING
        assert "plan" in reason

    def test_pr_impl_no_go_without_issue_plan_go_routes_to_planning(self) -> None:
        """A PR rejection cannot bypass the missing issue-plan approval."""
        stage, _reason = classify_issue(
            _facts(
                labels={STATE_IMPLEMENTATION_GO},
                pr_number=42,
                pr_is_open=True,
                pr_has_implementation_no_go=True,
            )
        )

        assert stage is StageName.PLANNING

    def test_no_pr_at_plan_go_routes_to_implementation(self) -> None:
        """No PR, at-or-past state:plan-go → ready for implementation."""
        stage, reason = classify_issue(_facts(labels={STATE_PLAN_GO}))
        assert stage is StageName.IMPLEMENTATION
        assert "at-or-past" in reason

    def test_no_pr_past_plan_go_routes_to_implementation(self) -> None:
        """A legacy issue-level implementation label cannot substitute for plan-go."""
        stage, _reason = classify_issue(_facts(labels={STATE_IMPLEMENTATION_NO_GO}))
        assert stage is StageName.PLANNING

    def test_no_pr_plan_no_go_routes_to_planning(self) -> None:
        """No PR, state:plan-no-go → planning (amend path)."""
        stage, reason = classify_issue(_facts(labels={STATE_PLAN_NO_GO}))
        assert stage is StageName.PLANNING
        assert "amend" in reason

    def test_needs_plan_routes_to_planning(self) -> None:
        """state:needs-plan → planning."""
        stage, _reason = classify_issue(_facts(labels={STATE_NEEDS_PLAN}))
        assert stage is StageName.PLANNING

    def test_no_label_defaults_to_planning(self) -> None:
        """No state label → planning (needs-plan by default)."""
        stage, _reason = classify_issue(_facts())
        assert stage is StageName.PLANNING

    def test_contradictory_labels_fail_closed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Target presence cannot route while a mutually exclusive sibling remains."""
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.pipeline.seeding"):
            stage, reason = classify_issue(_facts(labels={STATE_NEEDS_PLAN, STATE_PLAN_GO}))
        assert stage is None
        assert "contradictory" in reason
        assert any("contradictory state labels" in record.message for record in caplog.records)

    def test_unknown_state_labels_are_ignored_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown-only state labels behave like no known state label."""
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.pipeline.seeding"):
            stage, reason = classify_issue(_facts(labels={"state:foo", "state:bar"}))

        assert stage is StageName.PLANNING
        assert STATE_NEEDS_PLAN in reason
        assert any("unknown state labels ignored" in record.message for record in caplog.records)
        assert any(
            "state:foo" in record.message and "state:bar" in record.message
            for record in caplog.records
        )

    def test_unknown_state_label_does_not_displace_known_rank(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known state labels remain the only candidates for highest-rank routing."""
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.pipeline.seeding"):
            stage, _reason = classify_issue(_facts(labels={STATE_PLAN_GO, "state:zzz"}))

        assert stage is StageName.IMPLEMENTATION
        assert any("unknown state labels ignored" in record.message for record in caplog.records)
        assert not any("contradictory state labels" in record.message for record in caplog.records)

    def test_legacy_in_progress_warning_is_emitted_once_per_process(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stale legacy label cannot flood an autonomous multi-pass run."""
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.pipeline.seeding"):
            classify_issue(_facts(labels={"state:in-progress"}))
            classify_issue(_facts(labels={"state:in-progress"}))

        matching = [record for record in caplog.records if "state:in-progress" in record.message]
        assert len(matching) == 1


_STATE_LABEL_SETS: tuple[frozenset[str], ...] = (
    frozenset(),
    frozenset({STATE_NEEDS_PLAN}),
    frozenset({STATE_PLAN_NO_GO}),
    frozenset({STATE_PLAN_GO}),
    frozenset({STATE_IMPLEMENTATION_NO_GO}),
    frozenset({STATE_IMPLEMENTATION_GO}),
    frozenset({STATE_SKIP}),
    frozenset({STATE_SKIP, STATE_PLAN_GO}),
    frozenset({STATE_NEEDS_PLAN, STATE_IMPLEMENTATION_GO}),  # contradictory
)
_PR_STATES: tuple[dict[str, Any], ...] = (
    {"pr_number": None, "pr_is_open": False, "pr_is_merged": False},
    {"pr_number": 42, "pr_is_open": True, "pr_is_merged": False},
    {"pr_number": 42, "pr_is_open": False, "pr_is_merged": True},
)


class TestClassificationIsStageNameSSOT:
    """Every non-excluded classifier output is a routing.StageName member (SSOT guard)."""

    @pytest.mark.parametrize("labels", _STATE_LABEL_SETS)
    @pytest.mark.parametrize("pr_state", _PR_STATES)
    @pytest.mark.parametrize("is_epic", [False, True])
    def test_stage_is_none_or_stagename_member(
        self, labels: set[str], pr_state: dict[str, Any], is_epic: bool
    ) -> None:
        """The classifier never mints a queue name outside routing.StageName."""
        stage, reason = classify_issue(_facts(labels=set(labels), is_epic=is_epic, **pr_state))
        assert stage is None or stage in set(StageName), f"non-StageName output: {stage!r}"
        assert isinstance(reason, str) and reason

    def test_excluded_for_skip_or_contradictory_state(self) -> None:
        """Conflicting durable state labels fail closed alongside explicit skips."""
        for labels in _STATE_LABEL_SETS:
            for pr_state in _PR_STATES:
                stage, _ = classify_issue(_facts(labels=set(labels), **pr_state))
                known = {
                    label
                    for label in labels
                    if label in {STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO}
                }
                if STATE_SKIP in labels or len(known) > 1:
                    assert stage is None
                else:
                    assert stage is not None


def _fake_gh_backend(prs: list[dict[str, Any]]) -> Any:
    """Build a ``_gh_call`` side-effect simulating ``gh pr list`` state filters.

    Each PR dict carries ``number``, ``state`` (OPEN/MERGED/CLOSED),
    ``headRefName``, and ``body``. The fake honors ``--state`` and ``--head``
    args the way GitHub does, so a CLOSED PR is invisible to both the open
    and merged lookups — exactly the normalization seed_issue relies on.
    """

    def _gh_call(args: list[str], **_kw: Any) -> SimpleNamespace:
        state = args[args.index("--state") + 1] if "--state" in args else "open"
        head = args[args.index("--head") + 1] if "--head" in args else None
        rows = [pr for pr in prs if pr["state"].lower() == state.lower()]
        if head is not None:
            rows = [pr for pr in rows if pr["headRefName"] == head]
        payload = [{"number": pr["number"], "body": pr.get("body", "")} for pr in rows]
        return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

    return _gh_call


def _issue_info(
    number: int,
    labels: list[str],
    title: str = "A task",
    state: IssueState = IssueState.OPEN,
    *,
    authority_sanitized: bool = False,
) -> MagicMock:
    info = MagicMock()
    info.number = number
    info.labels = labels
    info.title = title
    info.state = state
    info.body = ""
    info.authority_sanitized = authority_sanitized
    return info


class TestSeedIssueFetchLayer:
    """Tri-state fetch: {open, merged, closed, none} against mocked gh responses."""

    def _seed(
        self,
        issue: int,
        labels: list[str],
        prs: list[dict[str, Any]],
        *,
        pr_labels: list[str] | None = None,
        issue_state: IssueState = IssueState.OPEN,
        authority_sanitized: bool = False,
    ) -> IssueFacts:
        with (
            patch(
                "hephaestus.automation.pipeline.seeding.fetch_issue_info",
                return_value=_issue_info(
                    issue,
                    labels,
                    state=issue_state,
                    authority_sanitized=authority_sanitized,
                ),
            ),
            patch(
                "hephaestus.automation._review_utils._gh_call",
                side_effect=_fake_gh_backend(prs),
            ),
            patch(
                "hephaestus.automation.pipeline.seeding.gh_pr_label_names",
                return_value=pr_labels or [],
            ),
        ):
            return seed_issue(issue)

    def test_open_pr(self) -> None:
        """An OPEN PR on the issue branch → pr_is_open, not merged."""
        facts = self._seed(
            7,
            [STATE_PLAN_GO],
            [{"number": 42, "state": "OPEN", "headRefName": "7-auto-impl", "body": "Closes #7"}],
        )
        assert facts.pr_number == 42
        assert facts.pr_is_open is True
        assert facts.pr_is_merged is False

    def test_open_pr_reads_implementation_state_labels(self) -> None:
        """An OPEN PR carrying implementation-go records the PR-level GO fact."""
        facts = self._seed(
            7,
            [STATE_PLAN_GO],
            [{"number": 42, "state": "OPEN", "headRefName": "7-auto-impl", "body": "Closes #7"}],
            pr_labels=[STATE_IMPLEMENTATION_GO],
        )
        assert facts.pr_has_implementation_go is True
        assert facts.pr_has_implementation_no_go is False
        stage, _ = classify_issue(facts)
        assert stage is StageName.MERGE_WAIT

    def test_merged_pr_found_when_open_lookup_misses(self) -> None:
        """A MERGED PR is surfaced by the merged lookup → pr_is_merged (finished row)."""
        facts = self._seed(
            7,
            [STATE_IMPLEMENTATION_GO],
            [{"number": 43, "state": "MERGED", "headRefName": "7-auto-impl", "body": "Closes #7"}],
            issue_state=IssueState.CLOSED,
        )
        assert facts.pr_number == 43
        assert facts.pr_is_open is False
        assert facts.pr_is_merged is True
        # And the classifier reaches the doc's "PR merged → finished" row.
        stage, _ = classify_issue(facts)
        assert stage is StageName.FINISHED

    def test_merged_pr_without_exact_closes_line_is_not_terminal(self) -> None:
        """A merged auto-implementation branch alone cannot complete an issue."""
        facts = self._seed(
            7,
            [STATE_PLAN_GO],
            [{"number": 43, "state": "MERGED", "headRefName": "7-auto-impl", "body": ""}],
        )

        assert facts.pr_number is None
        assert facts.pr_is_merged is False
        assert classify_issue(facts)[0] is StageName.IMPLEMENTATION

    def test_merged_pr_found_via_body_search(self) -> None:
        """A merged PR on a NON-canonical branch is still found via Closes-body search."""
        facts = self._seed(
            7,
            [STATE_IMPLEMENTATION_GO],
            [
                {
                    "number": 44,
                    "state": "MERGED",
                    "headRefName": "some-manual-branch",
                    "body": "Fix things.\n\nCloses #7\n",
                }
            ],
        )
        assert facts.pr_number == 44
        assert facts.pr_is_merged is True

    def test_closed_pr_normalized_to_none(self) -> None:
        """A CLOSED (abandoned) PR is invisible to both lookups → normalized to no PR.

        Prevents the dead-PR fall-through: with plan-go the issue re-enters
        implementation (a fresh PR is legitimate), never a phantom PR path.
        """
        facts = self._seed(
            7,
            [STATE_PLAN_GO],
            [{"number": 45, "state": "CLOSED", "headRefName": "7-auto-impl", "body": "Closes #7"}],
        )
        assert facts.pr_number is None
        assert facts.pr_is_open is False
        assert facts.pr_is_merged is False
        stage, _ = classify_issue(facts)
        assert stage is StageName.IMPLEMENTATION

    def test_no_pr_at_all(self) -> None:
        """No PR anywhere → clean no-PR facts."""
        facts = self._seed(7, [STATE_NEEDS_PLAN], [])
        assert facts.pr_number is None
        assert facts.pr_is_open is False
        assert facts.pr_is_merged is False

    def test_labels_and_number_threaded(self) -> None:
        """seed_issue carries number and the full label set into IssueFacts."""
        facts = self._seed(101, [STATE_PLAN_GO, "other-label"], [])
        assert facts.number == 101
        assert {STATE_PLAN_GO, "other-label"} <= facts.labels

    def test_global_issue_seed_preserves_sanitized_authority_flag(self) -> None:
        facts = self._seed(101, [STATE_NEEDS_PLAN], [], authority_sanitized=True)

        assert facts.authority_sanitized is True

    def test_blocked_comment_without_blocked_label_cannot_control_restart(self) -> None:
        """Seeding never infers BLOCKED from review prose after a label-write failure."""
        github = MagicMock()
        github.gh_issue_json.return_value = {
            "number": 102,
            "title": "A task",
            "body": "",
            "state": "OPEN",
            "labels": [{"name": STATE_NEEDS_PLAN}],
            "authoritySanitized": True,
        }
        github.issue_comments.side_effect = AssertionError("comments are not state authority")
        github.find_pr_for_issue.return_value = None
        github.find_merged_pr_for_issue.return_value = None

        facts = seed_issue_from_github(102, github)
        stage, _ = classify_issue(facts)

        assert stage is StageName.PLANNING
        assert STATE_PLAN_BLOCKED not in facts.labels
        assert facts.authority_sanitized is True
        github.issue_comments.assert_not_called()

    def test_pending_go_audit_receipt_routes_restart_back_to_pr_review(self) -> None:
        """A durable receipt outranks the GO label until publication completes."""

        class RestartGitHub:
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                return {
                    "number": issue_number,
                    "title": "A task",
                    "body": "",
                    "state": "OPEN",
                    "labels": [{"name": STATE_PLAN_GO}],
                }

            def find_pr_for_issue(self, issue_number: int) -> int:
                del issue_number
                return 44

            def find_merged_pr_for_issue(self, issue_number: int) -> None:
                del issue_number
                return None

            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                del pr_number
                return True, False

            def pending_implementation_go_audit(
                self, pr_number: int
            ) -> PendingImplementationGoAudit:
                return PendingImplementationGoAudit(
                    pr_number=pr_number,
                    head_sha="a" * 40,
                    audit=ReviewAudit("A", "clean", (), "", valid=True),
                )

        facts = seed_issue_from_github(102, RestartGitHub())
        entry = seed_entry_from_facts(facts)

        assert entry.stage is StageName.PR_REVIEW
        assert entry.pending_implementation_go_audit is not None
        assert entry.pending_implementation_go_label_confirmed is True


class TestSeedIssueEpicDetection:
    """Epic detection uses state_labels.is_epic (labels + title markers, #1669)."""

    def _seed_no_pr(self, info: MagicMock) -> IssueFacts:
        with (
            patch("hephaestus.automation.pipeline.seeding.fetch_issue_info", return_value=info),
            patch("hephaestus.automation.pipeline.seeding.find_pr_for_issue", return_value=None),
            patch(
                "hephaestus.automation.pipeline.seeding.find_merged_pr_for_issue",
                return_value=None,
            ),
        ):
            return seed_issue(info.number)

    def test_epic_label_detected(self) -> None:
        """An 'epic' label marks the issue as an epic."""
        facts = self._seed_no_pr(_issue_info(103, ["epic", STATE_NEEDS_PLAN]))
        assert facts.is_epic is True

    def test_roadmap_label_detected(self) -> None:
        """A 'roadmap' label ALSO marks the issue as an epic (EPIC_LABELS)."""
        facts = self._seed_no_pr(_issue_info(104, ["roadmap"]))
        assert facts.is_epic is True

    def test_title_marker_detected(self) -> None:
        """A title marker ('[Epic] ...') marks an unlabeled issue as an epic."""
        facts = self._seed_no_pr(_issue_info(105, [], title="[Epic] Queue-based pipeline"))
        assert facts.is_epic is True
        assert facts.title == "[Epic] Queue-based pipeline"

    def test_plain_issue_not_epic(self) -> None:
        """No epic label and no title marker → not an epic."""
        facts = self._seed_no_pr(_issue_info(106, [STATE_PLAN_GO], title="Fix the widget"))
        assert facts.is_epic is False


class TestSeedIssueFailClosed:
    """PR-probe failures re-raise: never misclassify toward implementation."""

    def test_open_pr_lookup_failure_raises(self) -> None:
        """find_pr_for_issue raising propagates (fail-closed, no PR-less fallback)."""
        with (
            patch(
                "hephaestus.automation.pipeline.seeding.fetch_issue_info",
                return_value=_issue_info(104, [STATE_PLAN_GO]),
            ),
            patch(
                "hephaestus.automation.pipeline.seeding.find_pr_for_issue",
                side_effect=RuntimeError("API error"),
            ),
            pytest.raises(RuntimeError, match="API error"),
        ):
            seed_issue(104)

    def test_merged_pr_lookup_failure_raises(self) -> None:
        """find_merged_pr_for_issue raising propagates too."""
        with (
            patch(
                "hephaestus.automation.pipeline.seeding.fetch_issue_info",
                return_value=_issue_info(104, [STATE_PLAN_GO]),
            ),
            patch("hephaestus.automation.pipeline.seeding.find_pr_for_issue", return_value=None),
            patch(
                "hephaestus.automation.pipeline.seeding.find_merged_pr_for_issue",
                side_effect=RuntimeError("merged probe down"),
            ),
            pytest.raises(RuntimeError, match="merged probe down"),
        ):
            seed_issue(104)

    def test_issue_fetch_failure_raises(self) -> None:
        """fetch_issue_info raising propagates (parity with the PR probes)."""
        with (
            patch(
                "hephaestus.automation.pipeline.seeding.fetch_issue_info",
                side_effect=RuntimeError("issue fetch down"),
            ),
            pytest.raises(RuntimeError, match="issue fetch down"),
        ):
            seed_issue(104)


class TestSeedFromCli:
    """CLI mapping: repos → repo queue; issues → classified; prs → review/merge."""

    def test_repos_arm(self) -> None:
        """Each repo becomes a StageName.REPO discovery seed."""
        entries = seed_from_cli(["RepoA", "RepoB"], [], [])
        assert [e.kind for e in entries] == ["repo", "repo"]
        assert [e.identifier for e in entries] == ["RepoA", "RepoB"]
        assert all(e.stage is StageName.REPO for e in entries)

    def test_issues_arm_classified_via_seed_issue(self) -> None:
        """Issues run through seed_issue + classify_issue."""
        facts = _facts(
            number=9,
            title="Hydrate planner context",
            body="Use the real issue body.",
            labels={STATE_PLAN_GO},
        )
        with patch(
            "hephaestus.automation.pipeline.seeding.seed_issue", return_value=facts
        ) as mock_seed:
            entries = seed_from_cli([], [9], [])
        mock_seed.assert_called_once_with(9)
        assert entries == [
            SeedEntry(
                kind="issue",
                identifier=9,
                stage=StageName.IMPLEMENTATION,
                reason=f"#9 at-or-past {STATE_PLAN_GO}, no PR yet",
                issue_title="Hydrate planner context",
                issue_body="Use the real issue body.",
            )
        ]

    def test_issues_arm_open_pr_preserves_pr_number(self) -> None:
        """Direct --issues seeding must keep the open PR number for PR stages."""
        facts = _facts(
            number=9,
            labels={STATE_PLAN_GO},
            pr_number=77,
            pr_is_open=True,
            pr_has_implementation_go=True,
        )
        with patch("hephaestus.automation.pipeline.seeding.seed_issue", return_value=facts):
            entries = seed_from_cli([], [9], [])

        assert entries == [
            SeedEntry(
                kind="issue",
                identifier=9,
                stage=StageName.MERGE_WAIT,
                reason=f"#9 open PR with {STATE_IMPLEMENTATION_GO}",
                pr_number=77,
                issue_title="A task",
            )
        ]

    def test_issues_arm_excluded_issue_maps_to_none_stage(self) -> None:
        """An excluded (skip/epic) issue surfaces stage=None to the caller."""
        facts = _facts(number=10, labels={STATE_SKIP})
        with patch("hephaestus.automation.pipeline.seeding.seed_issue", return_value=facts):
            entries = seed_from_cli([], [10], [])
        assert entries[0].stage is None

    def test_labeled_epic_has_no_skip_obligation_before_model_review(self) -> None:
        facts = _facts(number=10, is_epic=True, labels={"epic"})
        with patch("hephaestus.automation.pipeline.seeding.seed_issue", return_value=facts):
            entry = seed_from_cli([], [10], [])[0]

        assert entry.stage is StageName.PLANNING

    def test_title_inferred_epic_has_no_skip_obligation_before_model_review(self) -> None:
        facts = _facts(number=10, title="Epic: queue work", is_epic=True)
        with patch("hephaestus.automation.pipeline.seeding.seed_issue", return_value=facts):
            entry = seed_from_cli([], [10], [])[0]

        assert entry.stage is StageName.PLANNING

    def test_prs_with_impl_go_route_to_merge_wait_for_fresh_admission(self) -> None:
        """The durable label routes work; merge_wait still requires both proofs."""
        with (
            patch("hephaestus.automation.pipeline.seeding.gh_pr_state", return_value=None),
            patch(
                "hephaestus.automation.pipeline.seeding.gh_pr_label_names",
                return_value=[STATE_IMPLEMENTATION_GO],
            ),
        ):
            entries = seed_from_cli([], [], [77])
        assert entries == [
            SeedEntry(
                kind="pr",
                identifier=77,
                stage=StageName.MERGE_WAIT,
                reason=f"PR #77 carries {STATE_IMPLEMENTATION_GO}",
                pr_number=77,
            )
        ]

    def test_prs_arm_no_go_routes_to_pr_review(self) -> None:
        """A PR without impl-GO (e.g. NO-GO) seeds into pr_review."""
        with (
            patch("hephaestus.automation.pipeline.seeding.gh_pr_state", return_value=None),
            patch(
                "hephaestus.automation.pipeline.seeding.gh_pr_label_names",
                return_value=[STATE_IMPLEMENTATION_NO_GO],
            ),
        ):
            entries = seed_from_cli([], [], [78])
        assert entries[0].stage is StageName.PR_REVIEW

    def test_prs_arm_label_fetch_failure_reads_as_not_reviewed(self) -> None:
        """An empty label fetch (best-effort failure) → pr_review.

        Mirrors _review_existing_pr's (False, False) "not yet reviewed" semantics.
        """
        with (
            patch("hephaestus.automation.pipeline.seeding.gh_pr_state", return_value=None),
            patch("hephaestus.automation.pipeline.seeding.gh_pr_label_names", return_value=[]),
        ):
            entries = seed_from_cli([], [], [79])
        assert entries[0].stage is StageName.PR_REVIEW

    def test_prs_arm_uses_repo_scoped_accessor_when_given(self) -> None:
        """A repo-scoped github accessor routes the --prs state/label reads through it.

        Not the ambient gh_pr_state/gh_pr_label_names, closing the multi-repo
        misclassification (#1864).
        """
        github = MagicMock()
        github.gh_pr_state.return_value = None
        github.pr_has_implementation_state_label.return_value = (True, False)
        with (
            patch(
                "hephaestus.automation.pipeline.seeding.gh_pr_state",
                side_effect=AssertionError("must not call ambient state read when github is given"),
            ),
            patch(
                "hephaestus.automation.pipeline.seeding.gh_pr_label_names",
                side_effect=AssertionError("must not call ambient label read when github is given"),
            ),
        ):
            entries = seed_from_cli([], [], [77], github=github)
        github.gh_pr_state.assert_called_once_with(77)
        github.pr_has_implementation_state_label.assert_called_once_with(77)
        assert entries == [
            SeedEntry(
                kind="pr",
                identifier=77,
                stage=StageName.MERGE_WAIT,
                reason=f"PR #77 carries {STATE_IMPLEMENTATION_GO}",
                pr_number=77,
            )
        ]

    def test_prs_arm_repo_scoped_no_go_routes_to_pr_review(self) -> None:
        """Repo-scoped accessor without impl-GO seeds into pr_review."""
        github = MagicMock()
        github.gh_pr_state.return_value = None
        github.pr_has_implementation_state_label.return_value = (False, True)
        entries = seed_from_cli([], [], [78], github=github)
        assert entries[0].stage is StageName.PR_REVIEW

    def test_prs_arm_repo_scoped_merged_pr_routes_to_finished(self) -> None:
        """Repo-scoped accessor: a merged PR classifies FINISHED via github.gh_pr_state (#1865)."""
        github = MagicMock()
        github.gh_pr_state.return_value = {"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}
        entries = seed_from_cli([], [], [80], github=github)
        assert entries == [
            SeedEntry(
                kind="pr",
                identifier=80,
                stage=StageName.FINISHED,
                reason="PR #80 merged (idempotent)",
                pr_number=80,
            )
        ]
        github.pr_has_implementation_state_label.assert_not_called()

    def test_prs_arm_repo_scoped_closed_pr_excluded(self) -> None:
        """Repo-scoped accessor: a closed PR is excluded via github.gh_pr_state (#1865)."""
        github = MagicMock()
        github.gh_pr_state.return_value = {"state": "CLOSED", "mergedAt": None}
        entries = seed_from_cli([], [], [81], github=github)
        assert entries[0].stage is None
        github.pr_has_implementation_state_label.assert_not_called()

    def test_prs_arm_merged_pr_routes_to_finished(self) -> None:
        """A merged PR passed via --prs classifies FINISHED, not PR_REVIEW (#1865)."""
        with patch(
            "hephaestus.automation.pipeline.seeding.gh_pr_state",
            return_value={"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"},
        ):
            entries = seed_from_cli([], [], [80])
        assert entries == [
            SeedEntry(
                kind="pr",
                identifier=80,
                stage=StageName.FINISHED,
                reason="PR #80 merged (idempotent)",
                pr_number=80,
            )
        ]

    def test_prs_arm_closed_pr_excluded(self) -> None:
        """A closed (unmerged) PR passed via --prs is excluded, not re-reviewed (#1865)."""
        with patch(
            "hephaestus.automation.pipeline.seeding.gh_pr_state",
            return_value={"state": "CLOSED", "mergedAt": None},
        ):
            entries = seed_from_cli([], [], [81])
        assert entries[0].stage is None

    def test_prs_arm_state_fetch_failure_falls_through_to_labels(self) -> None:
        """A gh_pr_state read failure (None) falls through to label routing, not FINISHED."""
        with (
            patch("hephaestus.automation.pipeline.seeding.gh_pr_state", return_value=None),
            patch(
                "hephaestus.automation.pipeline.seeding.gh_pr_label_names",
                return_value=[STATE_IMPLEMENTATION_GO],
            ),
        ):
            entries = seed_from_cli([], [], [82])
        assert entries[0].stage is StageName.MERGE_WAIT

    def test_order_repos_then_issues_then_prs(self) -> None:
        """Entries preserve CLI order: repos, then issues, then prs."""
        facts = _facts(number=5)
        with (
            patch("hephaestus.automation.pipeline.seeding.seed_issue", return_value=facts),
            patch("hephaestus.automation.pipeline.seeding.gh_pr_state", return_value=None),
            patch("hephaestus.automation.pipeline.seeding.gh_pr_label_names", return_value=[]),
        ):
            entries = seed_from_cli(["R"], [5], [6])
        assert [e.kind for e in entries] == ["repo", "issue", "pr"]


class TestLabelRank:
    """At-or-past label rank comparisons prevent re-queueing."""

    def test_unknown_target_label_raises(self) -> None:
        """A typo in a compile-time target label must fail closed."""
        with pytest.raises(ValueError, match="Unknown target state label: state:typo"):
            _label_at_or_past(STATE_PLAN_GO, "state:typo")

    def test_issue_already_past_plan_go_not_requeued_to_planning(self) -> None:
        """A legacy issue implementation-go label is ignored during seeding."""
        stage, _ = classify_issue(_facts(labels={STATE_IMPLEMENTATION_GO}))
        assert stage is StageName.PLANNING

    def test_reconstruction_is_idempotent(self) -> None:
        """Classifying the same facts twice yields the same result (restart safety)."""
        facts = _facts(labels={STATE_PLAN_GO}, pr_number=42, pr_is_open=True)
        assert classify_issue(facts) == classify_issue(facts)
