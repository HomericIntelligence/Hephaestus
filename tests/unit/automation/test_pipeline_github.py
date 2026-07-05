"""PipelineGitHub adapter tests: mapping + dry-run log-and-skip (#1817).

The adapter is the ONE place coordinator-neutral mutator names map onto the
real ``github_api`` / ``pr_manager`` / ``_review_utils`` helpers, and the
place the ``StageGitHub`` protocol's dry-run contract is honored.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.github_api as github_api_mod
import hephaestus.automation.pipeline_github as pg
import hephaestus.automation.pr_manager as pr_manager_mod
from hephaestus.automation.pipeline.stages.base import StageGitHub
from hephaestus.automation.protocol import PLAN_COMMENT_MARKER


@pytest.fixture
def adapter(tmp_path: Path) -> pg.PipelineGitHub:
    """Live-mutator adapter anchored at a temp repo root."""
    return pg.PipelineGitHub("org", dry_run=False, repo_root=tmp_path)


@pytest.fixture
def dry_adapter(tmp_path: Path) -> pg.PipelineGitHub:
    """Dry-run adapter: every mutator must log-and-skip."""
    return pg.PipelineGitHub("org", dry_run=True, repo_root=tmp_path)


def test_adapter_satisfies_stage_github_protocol(adapter: pg.PipelineGitHub) -> None:
    """Runtime protocol conformance (mypy checks it statically too)."""
    assert isinstance(adapter, StageGitHub)


# ---------------------------------------------------------------------------
# Mutator mapping matrix: (method, args, patch-owner, underlying-name)
# 'module' = a function bound into pipeline_github's namespace at import.
# ---------------------------------------------------------------------------
_MUTATOR_CASES = [
    ("add_labels", (5, ["x"]), "github_api", "gh_issue_add_labels"),
    ("remove_labels", (5, ["x"]), "github_api", "gh_issue_remove_labels"),
    ("close_issue_as_covered", (5, 7), "module", "close_issue_as_covered"),
    ("upsert_plan_comment", (5, "body"), "github_api", "gh_issue_upsert_comment"),
    ("post_pr_comment", (7, "why"), "github_api", "gh_issue_comment"),
    ("mark_pr_implementation_go", (7,), "pr_manager", "mark_pr_implementation_go"),
    ("mark_pr_implementation_no_go", (7,), "pr_manager", "mark_pr_implementation_no_go"),
    ("defer_auto_merge", (7,), "pr_manager", "ensure_pr_auto_merge_deferred"),
    ("arm_auto_merge", (7,), "pr_manager", "enable_auto_merge_after_implementation_go"),
    ("post_review_threads", (7, [], "sum"), "github_api", "gh_pr_review_post"),
    ("skip_epics", ({5: ["epic"]},), "github_api", "skip_epics"),
    ("ensure_state_labels", (), "github_api", "_ensure_labels_exist"),
]


_OWNERS = {"github_api": github_api_mod, "pr_manager": pr_manager_mod}


def _patch_target(monkeypatch: pytest.MonkeyPatch, owner: str, name: str) -> MagicMock:
    mock = MagicMock(return_value=[] if name == "gh_pr_review_post" else None)
    if owner == "module":
        monkeypatch.setattr(pg, name, mock)
    else:
        monkeypatch.setattr(_OWNERS[owner], name, mock)
    return mock


class TestMutatorMapping:
    """Each coordinator-neutral mutator hits exactly its documented backer."""

    @pytest.mark.parametrize(("method", "args", "owner", "name"), _MUTATOR_CASES)
    def test_mutator_delegates(
        self,
        adapter: pg.PipelineGitHub,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        args: tuple[Any, ...],
        owner: str,
        name: str,
    ) -> None:
        mock = _patch_target(monkeypatch, owner, name)

        getattr(adapter, method)(*args)

        assert mock.call_count == 1

    @pytest.mark.parametrize(("method", "args", "owner", "name"), _MUTATOR_CASES)
    def test_dry_run_logs_and_skips(
        self,
        dry_adapter: pg.PipelineGitHub,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        method: str,
        args: tuple[Any, ...],
        owner: str,
        name: str,
    ) -> None:
        """StageGitHub contract: dry-run honored INSIDE the accessor."""
        mock = _patch_target(monkeypatch, owner, name)

        with caplog.at_level("INFO"):
            getattr(dry_adapter, method)(*args)

        mock.assert_not_called()
        assert any("[dry-run] would" in record.message for record in caplog.records)

    def test_upsert_plan_comment_keys_on_marker(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = _patch_target(monkeypatch, "github_api", "gh_issue_upsert_comment")

        adapter.upsert_plan_comment(5, "# Implementation Plan\n\nbody")

        mock.assert_called_once_with(5, PLAN_COMMENT_MARKER, "# Implementation Plan\n\nbody")


class TestCreatePr:
    """create_pr: idempotent reuse, given-body create, dry-run neutral."""

    def test_reuses_existing_open_pr(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pg, "find_pr_for_issue", lambda issue: 77)
        create = _patch_target(monkeypatch, "github_api", "gh_pr_create")

        assert adapter.create_pr(5, "branch", "t", "b") == 77
        create.assert_not_called()

    def test_creates_with_given_title_and_body(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NOT pr_manager.ensure_pr_created — the stage's composed body wins."""
        monkeypatch.setattr(pg, "find_pr_for_issue", lambda issue: None)
        create = MagicMock(return_value=88)
        monkeypatch.setattr(github_api_mod, "gh_pr_create", create)

        assert adapter.create_pr(5, "branch", "title", "body\n\nCloses #5") == 88
        create.assert_called_once_with("branch", "title", "body\n\nCloses #5")

    def test_dry_run_returns_zero(
        self, dry_adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pg, "find_pr_for_issue", lambda issue: None)
        create = _patch_target(monkeypatch, "github_api", "gh_pr_create")

        assert dry_adapter.create_pr(5, "b", "t", "x") == 0
        create.assert_not_called()


class TestReadSurface:
    """Reads delegate verbatim (and stay LIVE even under dry-run)."""

    def test_gh_issue_json(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_api_mod, "gh_issue_json", lambda n: {"number": n})

        assert adapter.gh_issue_json(4) == {"number": 4}

    def test_module_bound_reads(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pg, "find_merged_closing_pr", lambda n: 1)
        monkeypatch.setattr(pg, "find_pr_for_issue", lambda n: 2)
        monkeypatch.setattr(pg, "get_pr_head_branch", lambda n: "head")
        monkeypatch.setattr(pg, "is_plan_review_go", lambda n: True)

        assert adapter.find_merged_closing_pr(9) == 1
        assert adapter.find_pr_for_issue(9) == 2
        assert adapter.get_pr_head_branch(9) == "head"
        assert adapter.has_existing_plan(9) is True

    def test_pr_manager_reads(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pr_manager_mod, "pr_has_implementation_state_label", lambda n: (True, False)
        )
        monkeypatch.setattr(pr_manager_mod, "pr_is_genuinely_stuck", lambda n: True)

        assert adapter.pr_has_implementation_state_label(7) == (True, False)
        assert adapter.pr_is_genuinely_stuck(7) is True

    def test_pr_checks_reads_live_even_in_dry_run(
        self, dry_adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checks = [{"name": "ci"}]
        mock = MagicMock(return_value=checks)
        monkeypatch.setattr(github_api_mod, "gh_pr_checks", mock)

        assert dry_adapter.pr_checks(7) == checks
        mock.assert_called_once_with(7, dry_run=False)

    def test_check_inspector_delegation(self, adapter: pg.PipelineGitHub) -> None:
        adapter._inspector = MagicMock()
        adapter._inspector.failing_required_check_names.return_value = ["lint"]
        adapter._inspector.pending_required_check_names.return_value = ["test"]

        assert adapter.failing_required_check_names(7) == ["lint"]
        assert adapter.pending_required_check_names(7) == ["test"]


class TestUnresolvedThreads:
    """count_unresolved_threads mirrors #1152: counts only, resolves nothing."""

    def test_counts_by_ownership(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        threads = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        monkeypatch.setattr(
            github_api_mod, "gh_pr_list_unresolved_threads", lambda n, dry_run: threads
        )
        monkeypatch.setattr(github_api_mod, "gh_current_login", lambda: "bot")
        monkeypatch.setattr(pg, "_is_automation_owned_thread", lambda t, login: t["id"] == "a")

        assert adapter.count_unresolved_threads(7) == (1, 2)

    def test_empty_and_error_fail_open(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_api_mod, "gh_pr_list_unresolved_threads", lambda n, dry_run: [])
        assert adapter.count_unresolved_threads(7) == (0, 0)

        def boom(n: int, dry_run: bool) -> list[dict[str, Any]]:
            raise RuntimeError("api")

        monkeypatch.setattr(github_api_mod, "gh_pr_list_unresolved_threads", boom)
        assert adapter.count_unresolved_threads(7) == (0, 0)


class TestGhPrState:
    """The merge_wait single PR-state read (re-housed CIDriver._gh_pr_state)."""

    def test_success_parses_json(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"state": "OPEN", "headRefOid": "abc", "mergedAt": None}
        monkeypatch.setattr(pg, "gh_call", lambda argv: SimpleNamespace(stdout=json.dumps(payload)))

        assert adapter.gh_pr_state(7) == payload

    def test_failure_returns_none(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(argv: list[str]) -> Any:
            raise RuntimeError("gh down")

        monkeypatch.setattr(pg, "gh_call", boom)

        assert adapter.gh_pr_state(7) is None


class TestDriveGreenArmingRecords:
    """arm_drive_green / learn-terminal / learn-result over ArmingStateStore."""

    def test_arm_then_terminal_roundtrip(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pg, "get_pr_head_branch", lambda n: "1817-auto-impl")
        assert adapter.drive_green_learn_terminal(3) is False

        adapter.arm_drive_green(3, 70, "deadbeef")
        assert adapter.drive_green_learn_terminal(3) is False  # armed, not terminal

        adapter.mark_drive_green_learn_result(3, succeeded=True)
        assert adapter.drive_green_learn_terminal(3) is True

    def test_failed_learn_is_also_terminal(self, adapter: pg.PipelineGitHub) -> None:
        adapter.mark_drive_green_learn_result(4, succeeded=False)

        assert adapter.drive_green_learn_terminal(4) is True

    def test_arm_never_overwrites_terminal_record(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pg, "get_pr_head_branch", lambda n: "b")
        adapter.mark_drive_green_learn_result(5, succeeded=True)

        adapter.arm_drive_green(5, 71, "cafe")

        record = adapter._arming.load(5)
        assert record is not None
        assert record["learn_status"] == "succeeded"  # evidence preserved


class TestRateBudget:
    """The non-blocking port of the legacy rate guard."""

    def test_guard_disabled_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPHAESTUS_RATE_GUARD", "0")

        assert pg.rate_budget_ok() == (True, 0.0)

    def test_unknown_budget_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPHAESTUS_RATE_GUARD", raising=False)
        monkeypatch.setattr(pg, "rate_limit_remaining", lambda: None)

        assert pg.rate_budget_ok() == (True, 0.0)

    def test_high_budget_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPHAESTUS_RATE_GUARD", raising=False)
        monkeypatch.setattr(pg, "rate_limit_remaining", lambda: (5000, 0))

        assert pg.rate_budget_ok() == (True, 0.0)

    def test_low_budget_returns_park_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Low budget: (False, seconds-until-reset + 5s slack) — never a sleep."""
        monkeypatch.delenv("HEPHAESTUS_RATE_GUARD", raising=False)
        monkeypatch.setattr(pg, "rate_limit_remaining", lambda: (10, 1_000_000))

        ok, delay = pg.rate_budget_ok(now_epoch=999_995.0)

        assert ok is False
        assert delay == pytest.approx(10.0)  # (reset - now) + 5

    def test_rate_limit_remaining_parses_graphql_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"resources": {"graphql": {"remaining": 42, "reset": 123}}}
        monkeypatch.setattr(pg, "gh_call", lambda argv: SimpleNamespace(stdout=json.dumps(payload)))

        assert pg.rate_limit_remaining() == (42, 123)

    def test_rate_limit_remaining_none_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(argv: list[str]) -> Any:
            raise RuntimeError("gh down")

        monkeypatch.setattr(pg, "gh_call", boom)

        assert pg.rate_limit_remaining() is None

    def test_rate_limit_remaining_none_on_malformed_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pg, "gh_call", lambda argv: SimpleNamespace(stdout="not json"))
        assert pg.rate_limit_remaining() is None

        monkeypatch.setattr(pg, "gh_call", lambda argv: SimpleNamespace(stdout="{}"))
        assert pg.rate_limit_remaining() is None
