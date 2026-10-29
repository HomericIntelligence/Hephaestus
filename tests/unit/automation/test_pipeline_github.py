"""PipelineGitHub adapter tests: mapping + dry-run log-and-skip (#1817).

The adapter is the ONE place coordinator-neutral mutator names map onto the
real ``github_api`` / ``_review_utils`` helpers, and the
place the ``StageGitHub`` protocol's dry-run contract is honored.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest

import hephaestus.automation.git_runtime as git_runtime
import hephaestus.automation.github_api as github_api_mod
import hephaestus.automation.github_api.prs as prs_mod
import hephaestus.automation.pipeline_github_mutations as mutations_mod
import hephaestus.automation.pipeline_github_queries as queries_mod
import hephaestus.automation.pipeline_github_reviews as reviews_mod
import hephaestus.automation.pipeline_github_transport as transport_mod
from hephaestus.automation.dependency_parser import DependencyFact
from hephaestus.automation.github_api.graphql import GraphQLSpec
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.stages.base import StageGitHub
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_PREFIX,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    PlanDiscoveryStatus,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
)
from hephaestus.github.client import GitHubRateLimitError


def _assert_bounded_command_kwargs(kwargs: Mapping[str, Any], started_at: float) -> None:
    """Check the deadline and retry bounds for one command."""
    assert 0 < kwargs["timeout"] <= 120
    assert started_at < kwargs["deadline_s"] <= time.monotonic() + 120
    assert kwargs["max_retries"] == 1
    assert kwargs["retry_on_rate_limit"] is False


def _viewer_login_command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Return the current actor for the expected login command."""
    assert argv == ["api", "user", "--jq", ".login"]
    return subprocess.CompletedProcess(argv, 0, stdout="bot\n", stderr="")


class PipelineGitHubForTest(PipelineGitHub):
    """Production adapter with an explicit test-only operation nonce default."""


@pytest.fixture
def command_runner() -> MagicMock:
    """Reject each command that a test does not supply."""
    return MagicMock(side_effect=AssertionError("unexpected GitHub command"))


@pytest.fixture
def adapter(tmp_path: Path, command_runner: MagicMock) -> PipelineGitHubForTest:
    """Live-mutator adapter anchored at a temp repo root."""
    return PipelineGitHubForTest(
        "org", dry_run=False, repo_root=tmp_path, command_runner=command_runner
    )


@pytest.fixture
def dry_adapter(tmp_path: Path, command_runner: MagicMock) -> PipelineGitHubForTest:
    """Dry-run adapter: every mutator must log-and-skip."""
    return PipelineGitHubForTest(
        "org", dry_run=True, repo_root=tmp_path, command_runner=command_runner
    )


def test_adapter_satisfies_stage_github_protocol(adapter: PipelineGitHub) -> None:
    """Runtime protocol conformance (mypy checks it statically too)."""
    assert isinstance(adapter, StageGitHub)


def _open_pr_identity(*, head_sha: str = "a" * 40) -> dict[str, Any]:
    """Return the complete PR identity required by stable thread reads."""
    return {
        "id": "pull-request",
        "state": "OPEN",
        "headRefOid": head_sha,
        "baseRefOid": "b" * 40,
        "baseRefName": "main",
        "autoMergeRequest": None,
    }


class TestAllThreadReplyAndReviewerResolution:
    """The implementation/reviewer split applies to every open thread author."""


def test_unscoped_adapter_rejects_legacy_review_thread_fallback(adapter: PipelineGitHub) -> None:
    """Review-thread lifecycle needs complete repo-scoped GraphQL snapshots."""
    with pytest.raises(RuntimeError, match="repo-scoped"):
        adapter.list_unresolved_review_threads(7)


class TestConditionalMerge:
    """The conditional REST merge seam preserves the server's exact outcome."""


# ---------------------------------------------------------------------------
# Mutator mapping matrix: (method, args, patch-owner, underlying-name)
# Each owner is the namespace that calls the dependency.
# ---------------------------------------------------------------------------
_MUTATOR_CASES = [
    ("add_labels", (5, ["x"]), "github_api", "gh_issue_add_labels"),
    ("remove_labels", (5, ["x"]), "github_api", "gh_issue_remove_labels"),
    ("close_issue_as_covered", (5, 7), "mutations", "close_issue_as_covered"),
    ("ensure_state_labels", (), "github_api", "_ensure_labels_exist"),
]


_OWNERS = {"github_api": github_api_mod, "mutations": mutations_mod}


def _patch_target(monkeypatch: pytest.MonkeyPatch, owner: str, name: str) -> MagicMock:
    mock = MagicMock(return_value=[] if name == "gh_pr_review_post" else None)
    monkeypatch.setattr(_OWNERS[owner], name, mock)
    return mock


class TestMutatorMapping:
    """Each coordinator-neutral mutator hits exactly its documented backer."""

    @pytest.mark.parametrize(("method", "args", "owner", "name"), _MUTATOR_CASES)
    def test_mutator_delegates(
        self,
        adapter: PipelineGitHub,
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
        dry_adapter: PipelineGitHub,
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
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = render_current_plan("body")
        assert body.startswith("<!-- HomericIntelligence:plan-issue -->\n")
        fetch = MagicMock(
            side_effect=[
                [],
                [{"body": body, "databaseId": 100, "viewerDidAuthor": True}],
            ]
        )
        post = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        adapter.upsert_plan_comment(5, body)

        assert fetch.call_args_list == [call(5), call(5)]
        post.assert_called_once_with(5, body)

    def test_upsert_plan_comment_migrates_owned_legacy_marker_in_place(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One-write migration upgrades an owned legacy plan without a duplicate."""
        legacy_body = "# Implementation Plan\n\n<!-- hephaestus-plan:canonical -->\n\nOld plan"
        body = "<!-- HomericIntelligence:plan-issue -->\n# Implementation Plan\n\nNew plan"
        legacy = {"body": legacy_body, "databaseId": 100, "viewerDidAuthor": True}
        migrated = {"body": body, "databaseId": 100, "viewerDidAuthor": True}
        fetch = MagicMock(side_effect=[[legacy], [migrated]])
        patch_comment = MagicMock()
        post = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(adapter, "_patch_issue_comment", patch_comment)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        adapter.upsert_plan_comment(5, body)

        post.assert_not_called()
        patch_comment.assert_called_once()
        assert patch_comment.call_args.args == (100, body)
        assert fetch.call_args_list == [call(5), call(5)]

    def test_upsert_plan_comment_rejects_mixed_current_and_legacy_aliases_without_mutation(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A current and legacy actor-owned plan are an identity conflict, not duplicates."""
        body = render_current_plan("new plan")
        legacy = {
            "body": "<!-- hephaestus-plan:canonical -->\n# Implementation Plan\nold plan",
            "databaseId": 100,
            "viewerDidAuthor": True,
        }
        current = {
            "body": render_current_plan("old current plan"),
            "databaseId": 101,
            "viewerDidAuthor": True,
        }
        patch_comment = MagicMock()
        delete_comment = MagicMock()
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            MagicMock(return_value=[legacy, current]),
        )
        monkeypatch.setattr(adapter, "_patch_issue_comment", patch_comment)
        monkeypatch.setattr(adapter, "_delete_issue_comment", delete_comment)

        with pytest.raises(RuntimeError, match="ambiguous actor-owned comment aliases"):
            adapter.upsert_plan_comment(5, body)

        patch_comment.assert_not_called()
        delete_comment.assert_not_called()

    def test_upsert_create_requires_owned_exact_body_readback(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful transport write is not a durable publication receipt."""
        body = render_current_plan("body")
        monkeypatch.setattr(adapter, "_repo_issue_comments", MagicMock(side_effect=[[], []]))
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", MagicMock())

        with pytest.raises(RuntimeError, match="owned comment publication was not confirmed"):
            adapter.upsert_plan_comment(5, body)

    def test_upsert_patch_requires_owned_exact_body_readback(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """PATCH success cannot advance until GitHub returns the requested body."""
        old = render_current_plan("old")
        new = render_current_plan("new")
        stale = {"body": old, "databaseId": 100, "viewerDidAuthor": True}
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            MagicMock(side_effect=[[stale], [stale]]),
        )
        command_runner.side_effect = MagicMock()

        with pytest.raises(RuntimeError, match="owned comment publication was not confirmed"):
            adapter.upsert_plan_comment(5, new)

    def test_upsert_rejects_foreign_canonical_marker_without_shadow_comment(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A foreign planning marker is an identity conflict, not an absence."""
        body = render_current_plan("safe plan")
        fetch = MagicMock(
            return_value=[
                {
                    "body": f"{PLAN_CANONICAL_MARKER}\nforeign",
                    "databaseId": 99,
                    "viewerDidAuthor": False,
                }
            ]
        )
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        post = MagicMock()
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(RuntimeError, match="foreign or unverifiable"):
            adapter.upsert_plan_comment(5, body)

        post.assert_not_called()
        assert fetch.call_count == 1

    def test_upsert_rejects_one_owned_comment_with_plan_and_review_markers(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shared comment cannot be patched once for each planning role."""
        body = render_current_plan("safe plan")
        mixed = {
            "body": (
                f"{PLAN_CANONICAL_MARKER}\n# Implementation Plan\n\nPlan\n\n"
                f"{PLAN_REVIEW_CANONICAL_MARKER}\n## Plan Review\n\nReview"
            ),
            "databaseId": 99,
            "viewerDidAuthor": True,
        }
        fetch = MagicMock(return_value=[mixed])
        patch_comment = MagicMock()
        post = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(adapter, "_patch_issue_comment", patch_comment)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(RuntimeError, match="plan and review"):
            adapter.upsert_plan_comment(5, body)

        patch_comment.assert_not_called()
        post.assert_not_called()

    def test_upsert_rejects_cross_role_outgoing_body_before_any_write(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed candidate body stops before it is posted or patched."""
        body = (
            f"{PLAN_CANONICAL_MARKER}\n# Implementation Plan\n\nPlan\n\n"
            f"{PLAN_REVIEW_CANONICAL_MARKER}\n## Plan Review\n\nReview"
        )
        fetch = MagicMock()
        post = MagicMock()
        patch_comment = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(adapter, "_patch_issue_comment", patch_comment)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(RuntimeError, match="plan and review"):
            adapter.upsert_plan_comment(5, body)

        fetch.assert_not_called()
        post.assert_not_called()
        patch_comment.assert_not_called()

    def test_upsert_rejects_planning_marker_in_nonplanning_outgoing_body(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrelated pipeline comment cannot carry a shared planning marker."""
        marker = "<!-- unrelated:status -->"
        body = f"{marker}\nordinary text\n\n{PLAN_CANONICAL_MARKER}"
        fetch = MagicMock()
        post = MagicMock()
        patch_comment = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(adapter, "_patch_issue_comment", patch_comment)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(RuntimeError, match="nonplanning comment body claims"):
            adapter.upsert_issue_comment(5, marker, body)

        fetch.assert_not_called()
        post.assert_not_called()
        patch_comment.assert_not_called()

    def test_upsert_rejects_cross_role_legacy_marker_without_mutation(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plan upsert cannot use a review alias as a legacy migration key."""
        body = render_current_plan("safe plan")
        fetch = MagicMock()
        post = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(ValueError, match="does not belong"):
            adapter.upsert_issue_comment(
                5,
                PLAN_CANONICAL_MARKER,
                body,
                legacy_marker=PLAN_REVIEW_CANONICAL_MARKER,
            )

        fetch.assert_not_called()
        post.assert_not_called()

    def test_upsert_rejects_legacy_marker_as_a_write_target(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy alias may be read for migration but cannot be written."""
        marker = "<!-- hephaestus-plan:canonical -->"
        fetch = MagicMock()
        post = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(ValueError, match="shared HomericIntelligence marker"):
            adapter.upsert_issue_comment(5, marker, f"{marker}\n# Implementation Plan\nold")

        fetch.assert_not_called()
        post.assert_not_called()

    def test_canonical_create_rejects_owned_race_duplicates(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A post-create duplicate requires manual recovery before any delete."""
        body = render_current_plan("safe plan")
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            MagicMock(
                side_effect=[
                    [],
                    [
                        {"body": body, "databaseId": 100, "viewerDidAuthor": True},
                        {"body": body, "databaseId": 101, "viewerDidAuthor": True},
                    ],
                ]
            ),
        )
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", MagicMock())
        delete = MagicMock()
        monkeypatch.setattr(adapter, "_delete_issue_comment", delete)

        with pytest.raises(RuntimeError, match="ambiguous actor-owned comment aliases"):
            adapter.upsert_plan_comment(5, body)

        delete.assert_not_called()

    def test_immutable_append_ignores_foreign_collision(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A foreign immutable marker does not establish replay identity or block append."""
        marker = "<!-- hephaestus-plan-history:revision=1:kind=plan -->"
        body = f"{marker}\narchive"
        fetch = MagicMock(
            side_effect=[
                [
                    {
                        "body": f"{marker}\nforeign",
                        "databaseId": 99,
                        "viewerDidAuthor": False,
                    }
                ],
                [
                    {
                        "body": f"{marker}\nforeign",
                        "databaseId": 99,
                        "viewerDidAuthor": False,
                    },
                    {"body": body, "databaseId": 100, "viewerDidAuthor": True},
                ],
            ]
        )
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        post = MagicMock()
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        adapter.append_issue_comment(5, marker, body)

        post.assert_called_once_with(5, body)
        assert fetch.call_count == 2

    def test_immutable_append_requires_positive_post_write_visibility(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing post-write journal cannot authorize later cleanup."""
        marker = "<!-- hephaestus-plan-history:revision=1:kind=plan -->"
        body = f"{marker}\narchive"
        fetch = MagicMock(side_effect=[[], []])
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        post = MagicMock()
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(RuntimeError, match="immutable journal is not visible"):
            adapter.append_issue_comment(5, marker, body)

        post.assert_called_once_with(5, body)
        assert fetch.call_count == 2

    def test_immutable_append_rejects_embedded_planning_marker_before_post(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An immutable nonplanning artifact must not claim a planning role."""
        marker = "<!-- hephaestus-plan-history:revision=1:kind=plan -->"
        body = f"{marker}\narchive\n\n{PLAN_CANONICAL_MARKER}\n# Embedded plan"
        fetch = MagicMock()
        post = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        with pytest.raises(RuntimeError, match="nonplanning comment body claims"):
            adapter.append_issue_comment(5, marker, body)

        fetch.assert_not_called()
        post.assert_not_called()

    def test_immutable_append_is_replay_safe_and_conflict_detecting(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = "<!-- hephaestus-plan-history:revision=1:kind=plan -->"
        body = f"{marker}\narchive"
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [{"body": body, "databaseId": 42, "viewerDidAuthor": True}],
        )
        post = MagicMock()
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        adapter.append_issue_comment(5, marker, body)
        with pytest.raises(RuntimeError, match="immutable journal conflict"):
            adapter.append_issue_comment(5, marker, f"{marker}\ndifferent")

        post.assert_not_called()

    def test_immutable_append_rejects_identical_owned_duplicates(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Append-only history remains immutable even after a create race."""
        marker = "<!-- hephaestus-plan-history:revision=1:kind=plan -->"
        body = f"{marker}\narchive"
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {"body": body, "databaseId": 41, "viewerDidAuthor": True},
                {"body": body, "databaseId": 42, "viewerDidAuthor": True},
            ],
        )
        post = MagicMock()
        delete = MagicMock()
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)
        monkeypatch.setattr(adapter, "_delete_issue_comment", delete)

        with pytest.raises(RuntimeError, match="duplicate immutable journal"):
            adapter.append_issue_comment(5, marker, body)

        post.assert_not_called()
        delete.assert_not_called()

    def test_immutable_append_serializes_two_runner_create_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Two fresh accessors can create only one immutable journal entry."""
        marker = "<!-- hephaestus-plan-history:revision=1:kind=plan -->"
        body = f"{marker}\narchive"
        adapters = [
            PipelineGitHub("org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner),
            PipelineGitHub("org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner),
        ]
        comments: list[dict[str, object]] = []
        state_lock = threading.Lock()

        def read(_issue: int) -> list[dict[str, object]]:
            with state_lock:
                snapshot = [dict(comment) for comment in comments]
            if not snapshot:
                time.sleep(0.05)
            return snapshot

        def post(_issue: int, posted_body: str) -> None:
            with state_lock:
                comments.append(
                    {
                        "body": posted_body,
                        "databaseId": len(comments) + 1,
                        "viewerDidAuthor": True,
                    }
                )

        for adapter in adapters:
            monkeypatch.setattr(adapter, "_repo_issue_comments", read)
            monkeypatch.setattr(adapter, "_post_issue_comment", post)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                pool.submit(adapter.append_issue_comment, 5, marker, body) for adapter in adapters
            ]
            for result in results:
                result.result(timeout=5)

        assert comments == [{"body": body, "databaseId": 1, "viewerDidAuthor": True}]


def test_mark_go_uses_adapter_labels_and_readback(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GO transitions use the adapter-owned label primitives for every scope."""
    add = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(adapter, "_add_labels", add)
    monkeypatch.setattr(adapter, "_remove_labels", remove)
    monkeypatch.setattr(adapter, "pr_has_implementation_state_label", lambda _pr: (True, False))

    adapter.mark_pr_implementation_go(7)

    add.assert_called_once_with(7, [STATE_IMPLEMENTATION_GO])
    remove.assert_called_once_with(7, [STATE_IMPLEMENTATION_NO_GO])


def test_mark_no_go_uses_adapter_labels_and_readback(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO-GO transitions use the adapter-owned label primitives for every scope."""
    add = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(adapter, "_add_labels", add)
    monkeypatch.setattr(adapter, "_remove_labels", remove)
    disable = MagicMock()
    monkeypatch.setattr(adapter, "disable_auto_merge", disable)
    monkeypatch.setattr(adapter, "pr_has_implementation_state_label", lambda _pr: (False, True))

    adapter.mark_pr_implementation_no_go(7)

    add.assert_called_once_with(7, [STATE_IMPLEMENTATION_NO_GO])
    remove.assert_called_once_with(7, [STATE_IMPLEMENTATION_GO])
    disable.assert_called_once_with(7)


def test_unscoped_mark_go_uses_transport_without_repo_selector(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    command_runner: MagicMock,
) -> None:
    """Unscoped implementation labels use transport commands without ``--repo``."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_gh_call(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"labels": [{"name": STATE_IMPLEMENTATION_GO}]}),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(adapter, "_label_names", lambda: set(ALL_IMPLEMENTATION_STATE_LABELS))
    command_runner.side_effect = fake_gh_call

    started_at = time.monotonic()
    adapter.mark_pr_implementation_go(7)

    assert [argv for argv, _kwargs in calls] == [
        ["issue", "edit", "7", "--add-label", STATE_IMPLEMENTATION_GO],
        ["issue", "edit", "7", "--remove-label", STATE_IMPLEMENTATION_NO_GO],
        ["pr", "view", "7", "--json", "labels"],
    ]
    assert calls[-1][1]["check"] is False
    for _argv, kwargs in calls:
        _assert_bounded_command_kwargs(kwargs, started_at)


def test_scoped_mark_no_go_appends_repo_selector(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    command_runner: MagicMock,
) -> None:
    """The same state transition remains explicitly repository scoped."""
    adapter.repo = "repo-a"
    calls: list[list[str]] = []

    def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return SimpleNamespace(
                stdout=json.dumps({"labels": [{"name": STATE_IMPLEMENTATION_NO_GO}]})
            )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(adapter, "_label_names", lambda: set(ALL_IMPLEMENTATION_STATE_LABELS))
    command_runner.side_effect = fake_gh_call

    adapter.mark_pr_implementation_no_go(7)

    assert calls == [
        [
            "issue",
            "edit",
            "7",
            "--add-label",
            STATE_IMPLEMENTATION_NO_GO,
            "--repo",
            "org/repo-a",
        ],
        [
            "issue",
            "edit",
            "7",
            "--remove-label",
            STATE_IMPLEMENTATION_GO,
            "--repo",
            "org/repo-a",
        ],
        ["pr", "view", "7", "--json", "labels", "--repo", "org/repo-a"],
        ["pr", "merge", "7", "--disable-auto"],
    ]


def test_unscoped_label_read_transport_error_fails_closed(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    command_runner: MagicMock,
) -> None:
    """Treat unscoped transport failures as an absence of valid state."""
    command_runner.side_effect = MagicMock(side_effect=RuntimeError("gh unavailable"))

    assert adapter.pr_has_implementation_state_label(7) == (False, False)


@pytest.mark.parametrize("stdout", ["not-json", "[]", "{}"])  # malformed or non-object payloads
def test_unscoped_label_read_malformed_response_fails_closed(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    command_runner: MagicMock,
) -> None:
    """Treat malformed label payloads as an absence of valid state."""
    call_mock = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))
    command_runner.side_effect = call_mock

    started_at = time.monotonic()
    assert adapter.pr_has_implementation_state_label(7) == (False, False)
    call_mock.assert_called_once()
    assert call_mock.call_args.args == (["pr", "view", "7", "--json", "labels"],)
    assert call_mock.call_args.kwargs["check"] is False
    _assert_bounded_command_kwargs(call_mock.call_args.kwargs, started_at)


@pytest.mark.parametrize("state", [(False, False), (True, True)])
def test_mark_go_rejects_nonexclusive_readback(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    state: tuple[bool, bool],
) -> None:
    """Reject GO readbacks that are missing or share the NO-GO label."""
    monkeypatch.setattr(adapter, "_add_labels", MagicMock())
    monkeypatch.setattr(adapter, "_remove_labels", MagicMock())
    monkeypatch.setattr(adapter, "pr_has_implementation_state_label", lambda _pr: state)

    with pytest.raises(RuntimeError, match="implementation-go label read-back failed"):
        adapter.mark_pr_implementation_go(7)


@pytest.mark.parametrize("state", [(True, True), (False, False)])
def test_mark_no_go_rejects_nonexclusive_readback(
    adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    state: tuple[bool, bool],
) -> None:
    """Reject NO-GO readbacks that retain GO or omit NO-GO."""
    monkeypatch.setattr(adapter, "_add_labels", MagicMock())
    monkeypatch.setattr(adapter, "_remove_labels", MagicMock())
    monkeypatch.setattr(adapter, "pr_has_implementation_state_label", lambda _pr: state)

    with pytest.raises(RuntimeError, match="implementation-no-go label read-back failed"):
        adapter.mark_pr_implementation_no_go(7)


def test_dry_run_state_transitions_do_not_reach_transport(
    dry_adapter: PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    command_runner: MagicMock,
) -> None:
    """Dry-run state transitions issue no GitHub transport calls."""
    call_mock = MagicMock()
    command_runner.side_effect = call_mock
    monkeypatch.setattr(dry_adapter, "_add_labels", MagicMock())
    monkeypatch.setattr(dry_adapter, "_remove_labels", MagicMock())

    dry_adapter.mark_pr_implementation_go(7)

    call_mock.assert_not_called()


class TestRepoScoping:
    """PipelineGitHub must target its configured repository explicitly."""

    def test_issue_comments_returns_bodies_in_adapter_order(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {"body": "plan", "databaseId": 1, "user": {"login": "bot"}},
                {"body": "review", "databaseId": 2, "user": {"login": "bot"}},
            ],
        )
        command_runner.side_effect = _viewer_login_command

        assert adapter.issue_comments(7) == [
            IssueComment(body="plan", author_login="bot", viewer_did_author=True, database_id=1),
            IssueComment(body="review", author_login="bot", viewer_did_author=True, database_id=2),
        ]

    def test_issue_reads_include_repo_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            payload = {
                "number": 5,
                "title": "t",
                "state": "OPEN",
                "labels": [],
                "body": "",
            }
            return SimpleNamespace(stdout=json.dumps(payload))

        command_runner.side_effect = fake_gh_call

        assert (
            PipelineGitHub(
                "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
            ).gh_issue_json(5)["number"]
            == 5
        )

        assert calls == [
            [
                "issue",
                "view",
                "5",
                "--json",
                "id,number,title,state,labels,body,url",
                "--repo",
                "org/repo-a",
            ]
        ]

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (
                {
                    "headRepository": {"name": "repo-a"},
                    "headRepositoryOwner": {"login": "org"},
                },
                True,
            ),
            (
                {
                    "headRepository": {"name": "repo-a"},
                    "headRepositoryOwner": {"login": "contributor"},
                },
                False,
            ),
        ],
    )
    def test_pr_head_writable_requires_base_repository_identity(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict[str, object],
        expected: bool,
        command_runner: MagicMock,
    ) -> None:
        """Fork heads are readable but cannot receive a base-origin address push."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        assert adapter.pr_head_is_writable(17) is expected
        assert calls == [
            [
                "pr",
                "view",
                "17",
                "--json",
                "headRepository,headRepositoryOwner",
                "--repo",
                "org/repo-a",
            ]
        ]

    def test_label_mutators_include_repo_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["label", "list"]:
                return SimpleNamespace(stdout="[]")
            return SimpleNamespace(stdout="")

        command_runner.side_effect = fake_gh_call

        PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).add_labels(5, ["state:x"])

        assert calls[-1] == [
            "issue",
            "edit",
            "5",
            "--add-label",
            "state:x",
            "--repo",
            "org/repo-a",
        ]

    @pytest.mark.parametrize(
        "failure",
        [
            subprocess.CalledProcessError(1, "gh"),
            subprocess.TimeoutExpired("gh", 30),
            OSError("transport unavailable"),
        ],
    )
    def test_edit_labels_normalizes_transport_failures(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
        command_runner: MagicMock,
    ) -> None:
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(adapter, "_label_names", lambda: {"state:plan-go"})
        monkeypatch.setattr(adapter, "_gh", MagicMock(side_effect=failure))

        with pytest.raises(RuntimeError, match="failed to edit labels"):
            adapter.edit_labels(5, add=["state:plan-go"], remove=["state:plan-no-go"])

    def test_plan_presence_does_not_backfill_from_review_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {
                    "body": f"{PLAN_REVIEW_PREFIX}\n\nstate:plan-go",
                    "user": {"login": "bot"},
                }
            ],
        )
        command_runner.side_effect = _viewer_login_command

        assert adapter.discover_plan(5).status is PlanDiscoveryStatus.ABSENT

    def test_repo_scoped_discover_plan_detects_plan_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {
                    "body": (
                        "<!-- HomericIntelligence:plan-issue -->\n"
                        "# Implementation Plan\n\nDo the thing."
                    ),
                    "user": {"login": "bot"},
                }
            ],
        )
        command_runner.side_effect = _viewer_login_command

        assert adapter.discover_plan(5).status is PlanDiscoveryStatus.FOUND

    @pytest.mark.parametrize(
        "body",
        [
            f"{PLAN_COMMENT_MARKER} appendix\n\nNot canonical.",
            f"{PLAN_CANONICAL_MARKER} appendix\n{PLAN_COMMENT_MARKER}\nNot canonical.",
        ],
    )
    def test_repo_scoped_discover_plan_rejects_marker_prefixes(
        self,
        body: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command_runner: MagicMock,
    ) -> None:
        """Only an exact first-line marker identifies a canonical plan."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [{"body": body, "user": {"login": "bot"}}],
        )
        command_runner.side_effect = _viewer_login_command

        assert adapter.discover_plan(5).status is PlanDiscoveryStatus.ABSENT

    def test_repo_scoped_discover_plan_ignores_foreign_plan_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Foreign marker text is inert and cannot impersonate the plan artifact."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {
                    "body": f"{PLAN_COMMENT_MARKER}\n\nSpoofed plan.",
                    "user": {"login": "other"},
                }
            ],
        )
        command_runner.side_effect = _viewer_login_command

        assert adapter.discover_plan(5).status is PlanDiscoveryStatus.ABSENT

    def test_repo_scoped_discover_plan_ignores_review_state_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Artifact presence is independent from the authoritative state label."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {
                    "body": render_current_plan("Old rejected plan."),
                    "user": {"login": "bot"},
                },
                {
                    "body": render_current_review("state:plan-no-go", revision=1),
                    "user": {"login": "bot"},
                },
            ],
        )
        command_runner.side_effect = _viewer_login_command

        assert adapter.discover_plan(5).status is PlanDiscoveryStatus.FOUND

    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("gh unavailable"), GitHubRateLimitError("rate limited", reset_epoch=123)],
    )
    def test_repo_scoped_discover_plan_maps_read_failure(
        self,
        failure: Exception,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command_runner: MagicMock,
    ) -> None:
        """Transport and rate-limit failures remain READ_ERROR outcomes."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter, "_repo_issue_comments", lambda issue: (_ for _ in ()).throw(failure)
        )

        assert adapter.discover_plan(5).status is PlanDiscoveryStatus.READ_ERROR

    def test_repo_scoped_pr_lookup_raises_on_gh_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Repo-scoped seeding must fail closed instead of inventing no-PR state."""

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            raise RuntimeError("gh unavailable")

        command_runner.side_effect = fake_gh_call

        with pytest.raises(RuntimeError, match="gh unavailable"):
            PipelineGitHub(
                "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
            ).find_pr_for_issue(5)

    def test_repo_scoped_pr_lookup_uses_shared_branch_formatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """The head-branch lookup should consult the shared branch-name formatter."""
        calls: list[list[str]] = []

        monkeypatch.setattr(
            queries_mod,
            "issue_auto_impl_branch_name",
            lambda issue_number: f"branch-{issue_number}",
            raising=False,
        )

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "list"]:
                return SimpleNamespace(
                    stdout=json.dumps([{"number": 5, "state": "OPEN", "baseRefName": "main"}])
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": None}),
                stderr="",
            )

        command_runner.side_effect = fake_gh_call

        pr_number = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).find_pr_for_issue(7)

        assert pr_number == 5
        assert calls == [
            [
                "pr",
                "list",
                "--head",
                "branch-7",
                "--json",
                "number,state,baseRefName",
                "--limit",
                "1000",
                "--repo",
                "org/repo-a",
            ],
        ]

    def test_repo_scoped_pr_lookup_reads_all_head_prs_without_mutating_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Repo-scoped discovery contains every head PR before selecting main."""
        calls: list[list[str]] = []
        responses = iter(
            [
                SimpleNamespace(
                    stdout=json.dumps(
                        [
                            {"number": 5, "state": "OPEN", "baseRefName": "main"},
                            {"number": 6, "state": "OPEN", "baseRefName": "release"},
                        ]
                    )
                )
            ]
        )

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return next(responses)

        command_runner.side_effect = fake_gh_call

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        assert adapter.find_pr_for_issue(5) == 5
        assert len(calls) == 1

    def test_repo_scoped_pr_lookup_returns_all_siblings_without_auto_merge_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Discovery is read-only; a sibling cannot trigger a merge mutation."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        monkeypatch.setattr(
            github_api_mod,
            "_find_open_prs_for_head",
            lambda _branch, _runner: [(5, "main"), (6, "release")],
        )

        assert adapter._open_prs_for_branch("branch") == [(5, "main"), (6, "release")]

    def test_repo_scoped_lookup_contains_valid_prs_before_rejecting_malformed_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Malformed discovery still fails closed without mutating a valid PR."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "list"]:
                return SimpleNamespace(
                    stdout=json.dumps(
                        [
                            {"number": 5, "state": "OPEN", "baseRefName": "main"},
                            "malformed",
                        ]
                    )
                )
            raise AssertionError(f"unexpected gh invocation: {argv}")

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            adapter._open_prs_for_branch("branch")

        assert len(calls) == 1

    def test_repo_scoped_closing_pr_lookup_contains_every_fallback_head_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A noncanonical ``Closes`` fallback selects a sibling without arm changes."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "list"] and "--head" in argv:
                head = argv[argv.index("--head") + 1]
                if head == "7-auto-impl":
                    return SimpleNamespace(stdout="[]")
                assert head == "legacy-7-head"
                return SimpleNamespace(
                    stdout=json.dumps(
                        [
                            {"number": 8, "state": "OPEN", "baseRefName": "release"},
                            {"number": 9, "state": "OPEN", "baseRefName": "main"},
                        ]
                    )
                )
            if argv[:2] == ["pr", "list"] and "--search" in argv:
                return SimpleNamespace(stdout=json.dumps([{"number": 8, "body": "Closes #7\n"}]))
            if argv[:3] == ["pr", "view", "8"] and "headRefName" in argv:
                return SimpleNamespace(stdout=json.dumps({"headRefName": "legacy-7-head"}))
            raise AssertionError(f"unexpected gh invocation: {argv}")

        command_runner.side_effect = fake_gh_call

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        assert adapter.find_pr_for_issue(7) == 8

        assert not any("state,autoMergeRequest" in call for call in calls)

    def test_repo_scoped_pr_lookup_rejects_empty_successful_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Blank discovery output cannot become an invented no-PR state."""
        command_runner.side_effect = lambda _argv, **_kwargs: SimpleNamespace(stdout="")

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            adapter.find_pr_for_issue(5)

    def test_repo_scoped_merged_pr_lookup_requires_exact_closing_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A merged PR is linked only by the exact policy closing line."""
        command_runner.side_effect = lambda _argv, **_kwargs: SimpleNamespace(
            stdout=json.dumps([{"number": 5, "body": "Closes #5\r\n"}])
        )

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        assert adapter.find_merged_pr_for_issue(5) == 5

    @pytest.mark.parametrize(
        "body",
        [
            "Closes #5, #6\r\n",
            "Closes #5-extra\r\n",
            "Closes #5: reason\r\n",
            "Closes #5 trailing\r\n",
        ],
    )
    def test_repo_scoped_merged_pr_lookup_rejects_trailing_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, command_runner: MagicMock
    ) -> None:
        """Repo-scoped merged lookup requires the complete canonical line."""
        command_runner.side_effect = lambda _argv, **_kwargs: SimpleNamespace(
            stdout=json.dumps([{"number": 5, "body": body}])
        )

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        assert adapter.find_merged_pr_for_issue(5) is None

    def test_repo_scoped_unresolved_threads_returns_every_open_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            payload = {
                "data": {
                    "repository": {
                        "name": "repo-a",
                        "owner": {"login": "org"},
                        "pullRequest": {
                            "id": "PR_7",
                            "number": 7,
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "path": "a.py",
                                        "line": 1,
                                        "side": "RIGHT",
                                        "comments": {
                                            "nodes": [
                                                {"body": "bot", "author": {"login": "ci-bot"}}
                                            ]
                                        },
                                    },
                                    {
                                        "id": "T2",
                                        "isResolved": False,
                                        "path": "b.py",
                                        "line": 2,
                                        "side": "RIGHT",
                                        "comments": {
                                            "nodes": [
                                                {"body": "human", "author": {"login": "reviewer"}}
                                            ]
                                        },
                                    },
                                    {
                                        "id": "T3",
                                        "isResolved": True,
                                        "comments": {"nodes": []},
                                    },
                                ],
                            },
                        },
                    }
                }
            }
            return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        snapshots = {
            "T1": {
                "id": "T1",
                "isResolved": False,
                "path": "a.py",
                "line": 1,
                "side": "RIGHT",
                "comments": [
                    {
                        "id": "C1",
                        "body": "bot",
                        "author": "ci-bot",
                        "author_type": "Bot",
                        "viewer_did_author": False,
                        "review_id": "R1",
                        "review_body": "review",
                        "review_commit_sha": "a" * 40,
                    }
                ],
            },
            "T2": {
                "id": "T2",
                "isResolved": False,
                "path": "b.py",
                "line": 2,
                "side": "RIGHT",
                "comments": [
                    {
                        "id": "C2",
                        "body": "human",
                        "author": "reviewer",
                        "author_type": "User",
                        "viewer_did_author": False,
                        "review_id": "R2",
                        "review_body": "review",
                        "review_commit_sha": "a" * 40,
                    }
                ],
            },
            "T3": {
                "id": "T3",
                "isResolved": True,
                "path": "c.py",
                "line": 3,
                "side": "RIGHT",
                "comments": [
                    {
                        "id": "C3",
                        "body": "resolved",
                        "author": "reviewer",
                        "author_type": "User",
                        "viewer_did_author": False,
                        "review_id": "R3",
                        "review_body": "review",
                        "review_commit_sha": "a" * 40,
                    }
                ],
            },
        }
        monkeypatch.setattr(
            adapter, "_review_thread_snapshot", lambda _pr, thread_id: dict(snapshots[thread_id])
        )
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        threads = adapter.list_unresolved_review_threads(7)

        assert [thread["id"] for thread in threads] == ["T1", "T2"]

        assert calls[0][:2] == ["api", "graphql"]
        assert calls[0][calls[0].index("owner=org") - 1] == "-f"
        assert calls[0][calls[0].index("name=repo-a") - 1] == "-f"
        assert calls[0][calls[0].index("number=7") - 1] == "-F"

    @pytest.mark.parametrize("detail", ["@/etc/passwd", "@relative-secret"])
    def test_reviewer_feedback_is_sent_as_text_not_as_a_gh_file_reference(
        self,
        tmp_path: Path,
        detail: str,
        command_runner: MagicMock,
    ) -> None:
        """A leading @ in reviewer feedback stays text: gh reads -F values as files."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(stdout="{}", returncode=0)

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        adapter.apply_review_thread_decisions(
            7, resolved_thread_ids=("T0",), feedback={"T1": detail}
        )

        reply = next(argv for argv in calls if any(v.startswith("body=") for v in argv))
        body = next(v for v in reply if v.startswith("body="))
        assert reply[reply.index(body) - 1] == "-f"
        assert detail in body
        assert len(calls) == 2

    def test_graphql_preserves_typed_integer_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """GraphQL Int variables retain gh's typed-field encoding."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "org"},
                                "name": "repo-a",
                                "pullRequest": {
                                    "id": "PR_7",
                                    "number": 7,
                                    "reviewThreads": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [],
                                    },
                                },
                            }
                        }
                    }
                ),
                returncode=0,
            )

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        adapter._graphql(
            github_api_mod.unresolved_review_threads_page_query("org", "repo-a", 7),
            number=7,
        )

        argv = calls[0]
        assert argv[argv.index("owner=org") - 1] == "-f"
        assert argv[argv.index("name=repo-a") - 1] == "-f"
        assert argv[argv.index("number=7") - 1] == "-F"

    def test_pipeline_graphql_marks_guarded_transport_internal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """The typed pipeline adapter is the only caller allowed past the GraphQL guard."""
        call_kwargs: list[dict[str, object]] = []

        def fake_gh_call(_argv: list[str], **kwargs: object) -> SimpleNamespace:
            call_kwargs.append(dict(kwargs))
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "name": "repo-a",
                                "owner": {"login": "org"},
                                "pullRequest": {
                                    "id": "PR_7",
                                    "number": 7,
                                    "reviewThreads": {
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                        "nodes": [],
                                    },
                                },
                            }
                        }
                    }
                ),
                returncode=0,
            )

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        adapter._graphql(
            github_api_mod.unresolved_review_threads_page_query("org", "repo-a", 7),
            number=7,
        )

        assert call_kwargs[0]["_graphql_internal"] is True

    @pytest.mark.parametrize(
        "review_threads",
        [
            {"pageInfo": {"hasNextPage": False, "endCursor": None}},
            {"nodes": [], "pageInfo": {}},
            {"nodes": [], "pageInfo": {"hasNextPage": "false"}},
            {"nodes": [], "pageInfo": {"hasNextPage": None}},
        ],
    )
    def test_repo_scoped_unresolved_threads_rejects_incomplete_page_facts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        review_threads: dict[str, Any],
        command_runner: MagicMock,
    ) -> None:
        """Missing or malformed pagination never turns into an empty thread set."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "data": {"repository": {"pullRequest": {"reviewThreads": review_threads}}}
            },
        )

        with pytest.raises(RuntimeError, match="could not fetch all PR review threads"):
            adapter.list_unresolved_review_threads(7)

    def test_thread_snapshot_accepts_deleted_author_and_outdated_side(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Valid nullable GitHub fields cannot wedge an otherwise open thread."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "pr_node_id": "PR1",
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": None,
                },
                "thread": {
                    "id": "T1",
                    "isResolved": False,
                    "path": "a.py",
                    "line": None,
                    "side": None,
                    "pullRequest": {
                        "id": "PR1",
                        "number": 7,
                        "repository": {"name": "repo-a", "owner": {"login": "org"}},
                    },
                },
                "comments": {
                    "nodes": [
                        {
                            "id": "C1",
                            "body": "Please fix this.",
                            "viewerDidAuthor": False,
                            "author": None,
                            "pullRequestReview": None,
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
        )

        snapshot = adapter._review_thread_snapshot(7, "T1")

        assert snapshot is not None
        assert snapshot["side"] is None
        assert snapshot["comments"][0]["author"] == ""

    def test_multi_page_thread_snapshot_requires_a_stable_reread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A long conversation is reread before it can authorize a mutation."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        comments = [
            {
                "id": f"C{index}",
                "body": f"reply {index}",
                "viewerDidAuthor": False,
                "author": {"login": "reviewer", "__typename": "User"},
                "pullRequestReview": None,
            }
            for index in range(101)
        ]
        calls: list[str | None] = []

        def graphql(_query: str, **fields: str | int) -> dict[str, Any]:
            after = fields.get("after")
            calls.append(after if isinstance(after, str) else None)
            last_page = after == "cursor-100"
            return {
                "pr_node_id": "PR1",
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": None,
                },
                "thread": {
                    "id": "T1",
                    "isResolved": False,
                    "path": "a.py",
                    "line": 1,
                    "side": "RIGHT",
                    "pullRequest": {
                        "id": "PR1",
                        "number": 7,
                        "repository": {"name": "repo-a", "owner": {"login": "org"}},
                    },
                },
                "comments": {
                    "nodes": comments[100:] if last_page else comments[:100],
                    "pageInfo": {
                        "hasNextPage": not last_page,
                        "endCursor": None if last_page else "cursor-100",
                    },
                },
            }

        monkeypatch.setattr(adapter, "_graphql", graphql)
        snapshot = adapter._review_thread_snapshot(7, "T1")

        assert snapshot is not None
        assert snapshot["comments"][-1]["id"] == "C100"
        assert calls == [None, "cursor-100", None, "cursor-100"]

    def test_multi_page_thread_snapshot_fails_closed_when_later_page_arms_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A changed PR state on a later connection page invalidates the whole read."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        def graphql(_query: str, **fields: str | int) -> dict[str, Any]:
            after = fields.get("after") == "cursor-1"
            return {
                "pr_node_id": "PR1",
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": {"enabledAt": "now"} if after else None,
                },
                "thread": {
                    "id": "T1",
                    "isResolved": False,
                    "path": "a.py",
                    "line": 1,
                    "side": "RIGHT",
                    "pullRequest": {
                        "id": "PR1",
                        "number": 7,
                        "repository": {"name": "repo-a", "owner": {"login": "org"}},
                    },
                },
                "comments": {
                    "nodes": [
                        {
                            "id": "C2" if after else "C1",
                            "body": "later" if after else "first",
                            "viewerDidAuthor": False,
                            "author": {"login": "reviewer", "__typename": "User"},
                            "pullRequestReview": None,
                        }
                    ],
                    "pageInfo": {
                        "hasNextPage": not after,
                        "endCursor": None if after else "cursor-1",
                    },
                },
            }

        monkeypatch.setattr(adapter, "_graphql", graphql)
        assert adapter._review_thread_snapshot(7, "T1") is None

    def test_thread_snapshot_rejects_comment_cap_plus_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Hydration stops before it retains comment 2,001."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        recovery_mod = reviews_mod
        monkeypatch.setattr(recovery_mod, "THREAD_COMMENT_MAX", 2_000)
        monkeypatch.setattr(recovery_mod, "THREAD_SNAPSHOT_MAX_BYTES", 10**9)
        comments = [
            {
                "id": f"C{index}",
                "body": "",
                "viewerDidAuthor": False,
                "author": None,
                "pullRequestReview": None,
            }
            for index in range(2_001)
        ]
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "pr_node_id": "PR1",
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": None,
                },
                "thread": {
                    "id": "T1",
                    "isResolved": False,
                    "path": "a.py",
                    "line": 1,
                    "side": "RIGHT",
                    "pullRequest": {
                        "id": "PR1",
                        "number": 7,
                        "repository": {"name": "repo-a", "owner": {"login": "org"}},
                    },
                },
                "comments": {
                    "nodes": comments,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
        )

        assert adapter._review_thread_snapshot(7, "T1") is None

    def test_thread_snapshot_rejects_hydration_byte_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Hydration stops before it retains an over-limit comment payload."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        recovery_mod = reviews_mod
        monkeypatch.setattr(recovery_mod, "THREAD_SNAPSHOT_MAX_BYTES", 10)
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "pr_node_id": "PR1",
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": None,
                },
                "thread": {
                    "id": "T1",
                    "isResolved": False,
                    "path": "a.py",
                    "line": 1,
                    "side": "RIGHT",
                    "pullRequest": {
                        "id": "PR1",
                        "number": 7,
                        "repository": {"name": "repo-a", "owner": {"login": "org"}},
                    },
                },
                "comments": {
                    "nodes": [
                        {
                            "id": "C1",
                            "body": "too large",
                            "viewerDidAuthor": False,
                            "author": None,
                            "pullRequestReview": None,
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
        )

        assert adapter._review_thread_snapshot(7, "T1") is None

    def test_thread_snapshot_rejects_pathological_empty_comment_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Unique cursors cannot bypass the per-thread comment page limit."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        recovery_mod = reviews_mod
        monkeypatch.setattr(recovery_mod, "THREAD_COMMENT_PAGE_MAX", 2)
        calls = 0

        def graphql(_query: str, **_fields: str | int) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {
                "pr_node_id": "PR1",
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": None,
                },
                "thread": {
                    "id": "T1",
                    "isResolved": False,
                    "path": "a.py",
                    "line": 1,
                    "side": "RIGHT",
                    "pullRequest": {
                        "id": "PR1",
                        "number": 7,
                        "repository": {"name": "repo-a", "owner": {"login": "org"}},
                    },
                },
                "comments": {
                    "nodes": [],
                    "pageInfo": {
                        "hasNextPage": True,
                        "endCursor": f"cursor-{calls}",
                    },
                },
            }

        monkeypatch.setattr(adapter, "_graphql", graphql)
        with pytest.raises(RuntimeError, match="could not fetch all comments"):
            adapter._review_thread_snapshot(7, "T1")
        assert calls == 2

    def test_repo_scoped_unresolved_threads_fetches_page_after_first_hundred(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Every open thread is returned even when it appears after page one."""
        calls: list[list[str]] = []
        first_page_nodes = [
            {
                "id": f"T{index}",
                "isResolved": False,
                "comments": {"nodes": [{"body": "bot", "author": {"login": "ci-bot"}}]},
            }
            for index in range(100)
        ]
        second_page_nodes = [
            {
                "id": "T100",
                "isResolved": False,
                "comments": {"nodes": [{"body": "human", "author": {"login": "reviewer"}}]},
            }
        ]

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            after_first_page = "after=cursor-1" in argv
            payload = {
                "data": {
                    "repository": {
                        "owner": {"login": "org"},
                        "name": "repo-a",
                        "pullRequest": {
                            "id": "PR_7",
                            "number": 7,
                            "reviewThreads": {
                                "nodes": (
                                    second_page_nodes if after_first_page else first_page_nodes
                                ),
                                "pageInfo": {
                                    "hasNextPage": not after_first_page,
                                    "endCursor": None if after_first_page else "cursor-1",
                                },
                            },
                        },
                    }
                }
            }
            return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        def snapshot(_pr: int, thread_id: str) -> dict[str, Any]:
            return {
                "id": thread_id,
                "isResolved": False,
                "path": "a.py",
                "line": 1,
                "side": "RIGHT",
                "comments": [
                    {
                        "id": f"C-{thread_id}",
                        "body": thread_id,
                        "author": "ci-bot",
                        "author_type": "Bot",
                        "viewer_did_author": False,
                        "review_id": "R1",
                        "review_body": "review",
                        "review_commit_sha": "a" * 40,
                    }
                ],
            }

        monkeypatch.setattr(adapter, "_review_thread_snapshot", snapshot)
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        threads = adapter.list_unresolved_review_threads(7)

        assert len(threads) == 101
        assert len(calls) == 4
        assert "after=cursor-1" in calls[1]
        assert "after=cursor-1" in calls[3]

    def test_repo_scoped_unresolved_threads_rejects_an_unstable_page_traversal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A thread set changing between complete reads cannot authorize a clean state."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        reads = 0

        def graphql(_query: str, **_fields: str | int) -> dict[str, Any]:
            nonlocal reads
            reads += 1
            thread_id = "T1" if reads == 1 else "T2"
            return {
                "nodes": [{"id": thread_id, "isResolved": False}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }

        monkeypatch.setattr(adapter, "_graphql", graphql)
        monkeypatch.setattr(
            adapter,
            "_review_thread_snapshot",
            lambda _pr, thread_id: {
                "id": thread_id,
                "isResolved": False,
                "path": "a.py",
                "line": 1,
                "side": "RIGHT",
                "comments": [{"id": f"C-{thread_id}", "author": "dev", "body": "Fix."}],
            },
        )
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        with pytest.raises(RuntimeError, match="could not stabilize all PR review threads"):
            adapter.list_unresolved_review_threads(7)

    def test_repo_scoped_unresolved_threads_rejects_changed_hydrated_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Two complete traversals must include every comment body and author."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "nodes": [{"id": "T1", "isResolved": False}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        )
        reads = 0

        def snapshot(_pr: int, thread_id: str) -> dict[str, Any]:
            nonlocal reads
            reads += 1
            return {
                "id": thread_id,
                "isResolved": False,
                "path": "a.py",
                "line": 1,
                "side": "RIGHT",
                "comments": [
                    {
                        "id": "C1",
                        "body": "first" if reads == 1 else "changed",
                        "author": "reviewer",
                        "author_type": "User",
                        "viewer_did_author": False,
                        "review_id": "R1",
                        "review_body": "review",
                        "review_commit_sha": "a" * 40,
                    }
                ],
            }

        monkeypatch.setattr(adapter, "_review_thread_snapshot", snapshot)
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())

        with pytest.raises(RuntimeError, match="could not stabilize all PR review threads"):
            adapter.list_unresolved_review_threads(7)

    def test_repo_scoped_threads_reject_resolution_during_hydration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A resolution between enumeration and hydration invalidates the pass."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "nodes": [{"id": "T1", "isResolved": False}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        )
        monkeypatch.setattr(
            adapter,
            "_review_thread_snapshot",
            lambda _pr, _thread: {
                "id": "T1",
                "isResolved": True,
                "path": "a.py",
                "line": 1,
                "side": "RIGHT",
                "comments": [{"id": "C1", "author": "dev", "body": "Fixed."}],
            },
        )

        with pytest.raises(RuntimeError, match="could not stabilize all PR review threads"):
            adapter.list_unresolved_review_threads(7)

    def test_repo_scoped_empty_threads_reject_final_head_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """An empty thread set still needs the final exact PR identity proof."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        )
        reads = 0

        def pr_state(_pr: int) -> dict[str, Any]:
            nonlocal reads
            reads += 1
            return _open_pr_identity(head_sha=("c" if reads == 5 else "a") * 40)

        monkeypatch.setattr(adapter, "gh_pr_state", pr_state)

        with pytest.raises(RuntimeError, match="could not stabilize all PR review threads"):
            adapter.list_unresolved_review_threads(7)
        assert reads == 5

    def test_repo_scoped_unresolved_threads_rejects_a_cursor_cycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A non-adjacent repeated cursor cannot loop forever."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        def graphql(_query: str, **fields: str | int) -> dict[str, Any]:
            after = fields.get("after")
            cursor = "cursor-a" if after == "cursor-b" else "cursor-b" if after else "cursor-a"
            return {
                "nodes": [{"id": f"T-{after or 'first'}", "isResolved": False}],
                "pageInfo": {"hasNextPage": True, "endCursor": cursor},
            }

        monkeypatch.setattr(adapter, "_graphql", graphql)
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        with pytest.raises(RuntimeError, match="could not fetch all PR review threads"):
            adapter.list_unresolved_review_threads(7)

    def test_repo_scoped_threads_reject_pathological_empty_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Unique cursors cannot bypass the all-thread page limit."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        monkeypatch.setattr(queries_mod, "_THREAD_PAGE_MAX", 2)
        calls = 0

        def graphql(_query: str, **_fields: str | int) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": f"cursor-{calls}"},
            }

        monkeypatch.setattr(adapter, "_graphql", graphql)
        with pytest.raises(RuntimeError, match="could not fetch all PR review threads"):
            adapter.list_unresolved_review_threads(7)
        assert calls == 2

    def test_repo_scoped_threads_reject_thread_cap_plus_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Enumeration stops before it retains thread 10,001."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        monkeypatch.setattr(queries_mod, "THREAD_SNAPSHOT_MAX_BYTES", 10**9)
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "nodes": [{"id": f"T{index}", "isResolved": False} for index in range(10_001)],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        )
        monkeypatch.setattr(adapter, "_review_thread_snapshot", pytest.fail)

        with pytest.raises(RuntimeError, match="could not fetch all PR review threads"):
            adapter.list_unresolved_review_threads(7)

    def test_repo_scoped_threads_reject_aggregate_hydration_byte_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """Many valid small threads cannot exceed the complete snapshot budget."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        monkeypatch.setattr(queries_mod, "THREAD_SNAPSHOT_MAX_BYTES", 600)
        monkeypatch.setattr(
            adapter,
            "_graphql",
            lambda _query, **_fields: {
                "nodes": [{"id": f"T{index}", "isResolved": True} for index in range(10)],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        )
        monkeypatch.setattr(
            adapter,
            "_review_thread_snapshot",
            lambda _pr, thread_id: {
                "id": thread_id,
                "isResolved": True,
                "path": "module.py",
                "line": 1,
                "side": "RIGHT",
                "comments": [{"id": f"C-{thread_id}", "author": "dev", "body": "Done."}],
            },
        )

        with pytest.raises(RuntimeError, match="complete PR review thread snapshot is too large"):
            adapter.list_unresolved_review_threads(7)

    def test_thread_snapshot_validator_rejects_duplicate_comment_ids(self) -> None:
        """A replayed comment page cannot pass the final snapshot proof."""
        thread = {
            "comments": [
                {"id": "C1", "author": "reviewer", "body": "first"},
                {"id": "C1", "author": "reviewer", "body": "duplicate"},
            ]
        }
        assert PipelineGitHub._thread_comment_snapshot(thread) is None

    def test_repo_scoped_unresolved_threads_fail_closed_on_truncated_comments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A truncated comment page cannot produce an incomplete thread snapshot."""
        all_comments = [
            {"body": f"bot reply {index}", "author": {"login": "ci-bot"}} for index in range(20)
        ]
        all_comments.append({"body": "human reply", "author": {"login": "reviewer"}})
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        def graphql(query: GraphQLSpec[Any], **_fields: str | int) -> dict[str, Any]:
            if "node(id:$threadId)" in query.query:
                return {
                    "pr_node_id": "PR1",
                    "pr_state": {
                        "state": "OPEN",
                        "headRefOid": "a" * 40,
                        "autoMergeRequest": None,
                    },
                    "thread": {
                        "id": "T1",
                        "isResolved": False,
                        "path": "a.py",
                        "line": 1,
                        "side": "RIGHT",
                        "pullRequest": {
                            "id": "PR1",
                            "number": 7,
                            "repository": {"name": "repo-a", "owner": {"login": "org"}},
                        },
                    },
                    "comments": {
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "comment-cursor-20",
                        },
                        "nodes": all_comments[:20],
                    },
                }
            return {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "T1", "isResolved": False}],
            }

        monkeypatch.setattr(adapter, "_graphql", graphql)
        monkeypatch.setattr(adapter, "gh_pr_state", lambda _pr: _open_pr_identity())
        with pytest.raises(RuntimeError, match=r"could not fetch all comments.*T1"):
            adapter.list_unresolved_review_threads(7)

    def test_repo_scoped_fetch_error_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A repo-scoped GraphQL failure must not hide open review threads."""

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            raise RuntimeError("gh: GraphQL: Head sha can't be blank")

        command_runner.side_effect = fake_gh_call

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        with pytest.raises(RuntimeError, match="could not verify PR identity"):
            adapter.list_unresolved_review_threads(7)

    def test_repo_scoped_upsert_plan_comment_updates_marker_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        calls: list[list[str]] = []
        current_body = render_current_plan("old")

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            nonlocal current_body
            calls.append(argv)
            if argv[:3] == ["api", "--method", "PATCH"]:
                current_body = render_current_plan("new")
                return SimpleNamespace(stdout="")
            if argv == [
                "api",
                "/repos/org/repo-a/issues/5/comments?per_page=100&page=1",
            ]:
                payload = [
                    {
                        "id": 9,
                        "body": current_body,
                        "viewerDidAuthor": True,
                    }
                ]
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        command_runner.side_effect = fake_gh_call

        PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).upsert_plan_comment(5, render_current_plan("new"))

        assert any(call[:3] == ["api", "--method", "PATCH"] for call in calls)
        assert any("/repos/org/repo-a/issues/comments/9" in call for call in calls)
        assert not any(call[:2] == ["issue", "comment"] for call in calls)


class TestRepoScopedAutoMerge:
    """The pipeline adapter intentionally exposes no auto-merge mutators."""

    def test_pipeline_adapter_has_no_auto_merge_mutation_surface(
        self, tmp_path: Path, command_runner: MagicMock
    ) -> None:
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        assert not hasattr(adapter, "arm_auto_merge")
        assert not hasattr(adapter, "defer_auto_merge")

    def test_pipeline_adapter_has_no_unanchored_pr_comment_surface(
        self, tmp_path: Path, command_runner: MagicMock
    ) -> None:
        """PR responses must be review-thread replies, never issue comments."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        assert not hasattr(adapter, "post_pr_comment")
        assert not hasattr(adapter, "upsert_pr_comment")


class TestCreatePr:
    """create_pr: idempotent reuse, given-body create, dry-run neutral."""

    def test_create_pr_uses_checkout_for_signatures_and_explicit_state_root_for_journals(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command_runner: MagicMock,
    ) -> None:
        """A separate state root cannot replace the Git checkout for signature reads."""
        checkout = tmp_path / "intake" / "worktree"
        state_root = tmp_path / "intake"
        checkout.mkdir(parents=True)
        signature_cwds: list[Path] = []

        monkeypatch.setattr(PipelineGitHub, "_open_prs_for_branch", lambda self, branch: [])
        monkeypatch.setattr(PipelineGitHub, "find_pr_for_issue", lambda self, issue: None)

        def assert_signed(branch: str, **kwargs: Any) -> None:
            del branch
            kwargs["run_git"](["git", "log"])

        def run_git(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            signature_cwds.append(kwargs["cwd"])
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(prs_mod, "_assert_branch_commits_signed", assert_signed)
        monkeypatch.setattr(git_runtime, "run", run_git)
        command_runner.side_effect = lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, "https://github.com/org/repo-a/pull/8\n", ""
        )
        adapter = PipelineGitHub(
            "org",
            repo="repo-a",
            repo_root=checkout,
            state_root=state_root,
            command_runner=command_runner,
        )

        assert adapter.create_pr(7, "feature", "title", "Closes #7") == 8
        assert signature_cwds == [checkout]
        assert adapter._implementation_reply_lock_path(7).parent == (
            state_root / DEFAULT_STATE_DIR / "locks"
        )

    def test_repo_scoped_reuses_existing_open_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(adapter, "_open_prs_for_branch", lambda branch: [])
        monkeypatch.setattr(adapter, "find_pr_for_issue", lambda issue: 77)

        assert adapter.create_pr(5, "branch", "t", "b") == 77
        command_runner.assert_not_called()

    def test_unscoped_create_fails_before_transport(
        self, adapter: PipelineGitHub, command_runner: MagicMock
    ) -> None:
        """PR creation requires an explicit repository before any command."""
        with pytest.raises(RuntimeError, match="repo-scoped"):
            adapter.create_pr(5, "branch", "title", "body\n\nCloses #5")

        command_runner.assert_not_called()

    def test_dry_run_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        dry_adapter = PipelineGitHub(
            "org", repo="repo-a", dry_run=True, repo_root=tmp_path, command_runner=command_runner
        )
        monkeypatch.setattr(dry_adapter, "_open_prs_for_branch", lambda branch: [])
        monkeypatch.setattr(dry_adapter, "find_pr_for_issue", lambda issue: None)

        assert dry_adapter.create_pr(5, "b", "t", "x") == 0
        command_runner.assert_not_called()

    def test_repo_scoped_create_pr_parses_pull_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        calls: list[list[str]] = []

        monkeypatch.setattr(PipelineGitHub, "find_pr_for_issue", lambda self, issue: None)
        monkeypatch.setattr(prs_mod, "_assert_branch_commits_signed", lambda branch, **kwargs: None)

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "list"]:
                return SimpleNamespace(stdout="[]")
            return SimpleNamespace(stdout="https://github.com/org/repo-a/pull/1888\n")

        command_runner.side_effect = fake_gh_call

        pr_number = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).create_pr(1887, "1887-auto-impl", "title", "body\n\nCloses #1887")

        assert pr_number == 1888
        assert calls[0][-2:] == ["--repo", "org/repo-a"]

    def test_repo_scoped_create_pr_contains_all_existing_head_prs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """A custom branch cannot bypass all-head containment before PR reuse."""
        calls: list[list[str]] = []
        monkeypatch.setattr(PipelineGitHub, "find_pr_for_issue", lambda self, issue: None)
        monkeypatch.setattr(prs_mod, "_assert_branch_commits_signed", lambda branch, **kwargs: None)

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "list"] and "--head" in argv:
                assert argv[argv.index("--head") + 1] == "custom-branch"
                return SimpleNamespace(
                    stdout=json.dumps(
                        [
                            {"number": 8, "state": "OPEN", "baseRefName": "release"},
                            {"number": 9, "state": "OPEN", "baseRefName": "main"},
                        ]
                    )
                )
            raise AssertionError(f"unexpected gh invocation: {argv}")

        command_runner.side_effect = fake_gh_call

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )
        assert adapter.create_pr(7, "custom-branch", "title", "body\n\nCloses #7") == 9
        assert not any("state,autoMergeRequest" in call for call in calls)

    @pytest.mark.parametrize(
        "stdout",
        [
            "gh: GraphQL: Head sha can't be blank",
            "https://github.com/org/repo-a/123?foo=bar",
        ],
    )
    def test_repo_scoped_create_pr_parse_miss_logs_and_raises_runtime_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        stdout: str,
        command_runner: MagicMock,
    ) -> None:
        monkeypatch.setattr(PipelineGitHub, "find_pr_for_issue", lambda self, issue: None)
        monkeypatch.setattr(prs_mod, "_assert_branch_commits_signed", lambda branch, **kwargs: None)

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["pr", "list"]:
                return SimpleNamespace(stdout="[]")
            return SimpleNamespace(stdout=stdout)

        command_runner.side_effect = fake_gh_call

        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        with caplog.at_level("ERROR", logger=transport_mod.__name__):
            with pytest.raises(RuntimeError, match="Failed to parse PR number") as excinfo:
                adapter.create_pr(1887, "1887-auto-impl", "title", "body\n\nCloses #1887")

        assert stdout in str(excinfo.value)
        assert any(stdout in record.message for record in caplog.records)


class TestPrBodyWrite:
    """Read one PR body and rewrite it without losing the Closes link."""

    def test_body_read_returns_the_stored_text(
        self, tmp_path: Path, command_runner: MagicMock
    ) -> None:
        """The adapter returns exactly the body GitHub stores."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps({"body": "## Testing\nold\n"}))

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        assert adapter.get_pr_body(12) == "## Testing\nold\n"
        assert calls == [["pr", "view", "12", "--json", "body", "--repo", "org/repo-a"]]

    def test_body_read_failure_raises_for_the_caller(
        self, tmp_path: Path, command_runner: MagicMock
    ) -> None:
        """An answer that is not a PR object raises instead of inventing a body."""
        command_runner.side_effect = lambda argv, **_kwargs: SimpleNamespace(stdout="[]")
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        with pytest.raises(RuntimeError, match="body read returned no object"):
            adapter.get_pr_body(12)

    def test_body_write_sends_the_edited_body_file(
        self, tmp_path: Path, command_runner: MagicMock
    ) -> None:
        """The write carries the whole edited body through ``--body-file``."""
        body = "## Summary\nS\n\n## Testing\n`uv run pytest tests` — passed\n\nCloses #7\n"
        calls: list[list[str]] = []
        written: list[str] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            written.append(Path(argv[argv.index("--body-file") + 1]).read_text())
            _assert_bounded_command_kwargs(kwargs, time.monotonic())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        command_runner.side_effect = fake_gh_call
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        adapter.update_pr_body(12, body)

        assert written == [body]
        assert calls[0][:4] == ["pr", "edit", "12", "--body-file"]
        assert calls[0][-2:] == ["--repo", "org/repo-a"]

    def test_body_write_rejects_a_body_without_the_closes_line(
        self, tmp_path: Path, command_runner: MagicMock
    ) -> None:
        """A partial read must not drop the link that closes the issue."""
        adapter = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        )

        with pytest.raises(ValueError, match="Closes #"):
            adapter.update_pr_body(12, "## Testing\n`uv run pytest tests` — passed\n")

        command_runner.assert_not_called()

    def test_dry_run_body_write_reaches_no_transport(
        self, tmp_path: Path, command_runner: MagicMock
    ) -> None:
        """A dry run logs the write and sends nothing."""
        call_mock = MagicMock()
        command_runner.side_effect = call_mock
        adapter = PipelineGitHub(
            "org",
            repo="repo-a",
            dry_run=True,
            repo_root=tmp_path,
            command_runner=command_runner,
        )

        adapter.update_pr_body(12, "## Testing\nnew\n\nCloses #7\n")

        call_mock.assert_not_called()


class TestReadSurface:
    """Reads delegate verbatim (and stay LIVE even under dry-run)."""

    def test_repo_scoped_issue_read_exposes_exact_body_digest(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_body = "raw\x00body"
        adapter.repo = "repo-a"
        monkeypatch.setattr(
            adapter,
            "_gh",
            MagicMock(
                return_value=SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "number": 4,
                            "title": "title",
                            "state": "OPEN",
                            "labels": [],
                            "body": raw_body,
                        }
                    )
                )
            ),
        )

        result = adapter.gh_issue_json(4)

        assert result["body"] == "rawbody"
        assert result["bodyDigest"] == hashlib.sha256(raw_body.encode()).hexdigest()
        assert result["authoritySanitized"] is True

    def test_gh_issue_json(self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(github_api_mod, "gh_issue_json", lambda n: {"number": n})

        assert adapter.gh_issue_json(4) == {"number": 4}

    @pytest.mark.parametrize(
        "failure",
        [
            subprocess.CalledProcessError(1, "gh"),
            subprocess.TimeoutExpired("gh", 30),
            OSError("transport unavailable"),
            None,
        ],
    )
    def test_repo_scoped_issue_read_normalizes_transport_and_json_failures(
        self,
        adapter: PipelineGitHub,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException | None,
    ) -> None:
        adapter.repo = "repo-a"
        if failure is not None:
            monkeypatch.setattr(adapter, "_gh", MagicMock(side_effect=failure))
        else:
            monkeypatch.setattr(
                adapter,
                "_gh",
                MagicMock(return_value=SimpleNamespace(stdout="not-json")),
            )

        with pytest.raises(RuntimeError, match="Failed to fetch issue"):
            adapter.gh_issue_json(4)

    def test_module_bound_reads(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        monkeypatch.setattr(queries_mod, "find_merged_closing_pr", lambda n: 1)
        monkeypatch.setattr(adapter, "_find_pr_for_issue", lambda n, state: 2)
        monkeypatch.setattr(queries_mod, "get_pr_head_branch", lambda n: "head")
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {
                    "body": render_current_plan("Plan"),
                    "user": {"login": "bot"},
                }
            ],
        )
        command_runner.side_effect = _viewer_login_command

        assert adapter.find_merged_closing_pr(9) == 1
        assert adapter.find_pr_for_issue(9) == 2
        assert adapter.get_pr_head_branch(9) == "head"
        assert adapter.discover_plan(9).status is PlanDiscoveryStatus.FOUND

    def test_unscoped_pr_lookup_contains_every_same_head_pr(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """The optional unscoped accessor discovers siblings read-only."""
        command_runner.side_effect = lambda argv, **_kwargs: SimpleNamespace(
            stdout=json.dumps(
                [
                    {"number": 5, "state": "OPEN", "baseRefName": "main"},
                    {"number": 6, "state": "OPEN", "baseRefName": "release"},
                ]
            )
        )

        assert adapter.find_pr_for_issue(5) == 5

    def test_find_issue_for_pr_parses_exact_closes_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps({"body": "Summary\n\nCloses #1899\n"}))

        command_runner.side_effect = fake_gh_call

        issue = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).find_issue_for_pr(1984)

        assert issue == 1899
        assert calls == [["pr", "view", "1984", "--json", "body", "--repo", "org/repo-a"]]

    @pytest.mark.parametrize("body", ["Fixes #1899\n", "Closes #1899, #1900\n", ""])
    def test_find_issue_for_pr_rejects_non_policy_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, command_runner: MagicMock
    ) -> None:
        command_runner.side_effect = lambda argv, **kwargs: SimpleNamespace(
            stdout=json.dumps({"body": body})
        )

        issue = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).find_issue_for_pr(1984)

        assert issue is None

    def test_pr_review_context_reads_metadata_for_a_checkout_bound_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """The checkout barrier, not mutable GitHub diff output, supplies the diff."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "view"]:
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "id": "PR_exact",
                            "title": "docs(policy): current metadata",
                            "body": "Closes #1899\n",
                            "headRefOid": "a" * 40,
                            "baseRefOid": "b" * 40,
                            "baseRefName": "main",
                        }
                    )
                )
            raise AssertionError(f"unexpected gh invocation: {argv}")

        command_runner.side_effect = fake_gh_call

        context = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).pr_review_context(1984)

        assert context == {
            "pr_node_id": "PR_exact",
            "pr_title": "docs(policy): current metadata",
            "pr_description": "Closes #1899\n",
            "pr_head_sha": "a" * 40,
            "pr_base_sha": "b" * 40,
            "pr_base_branch": "main",
        }
        assert calls == [
            [
                "pr",
                "view",
                "1984",
                "--json",
                "id,title,body,headRefOid,baseRefOid,baseRefName",
                "--repo",
                "org/repo-a",
            ],
        ]

    def test_pr_review_context_does_not_request_mutable_remote_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        """An A -> B -> A race cannot pair B's remote diff with proof for A."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "title": "current title",
                        "body": "Closes #1899",
                        "headRefOid": "a" * 40,
                        "baseRefOid": "b" * 40,
                        "baseRefName": "main",
                    }
                )
            )

        command_runner.side_effect = fake_gh_call

        context = PipelineGitHub(
            "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
        ).pr_review_context(1984)

        assert context is not None
        assert context["pr_head_sha"] == "a" * 40
        assert "pr_diff" not in context
        assert all(argv[:2] != ["pr", "diff"] for argv in calls)

    def test_unscoped_implementation_label_read_uses_adapter_transport(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        call_mock = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"labels": [{"name": STATE_IMPLEMENTATION_GO}]}), stderr=""
            )
        )
        command_runner.side_effect = call_mock

        started_at = time.monotonic()
        assert adapter.pr_has_implementation_state_label(7) == (True, False)
        call_mock.assert_called_once()
        assert call_mock.call_args.args == (["pr", "view", "7", "--json", "labels"],)
        assert call_mock.call_args.kwargs["check"] is False
        _assert_bounded_command_kwargs(call_mock.call_args.kwargs, started_at)


class TestGhPrState:
    """The merge_wait single PR-state read (re-housed CIDriver._gh_pr_state)."""

    def test_success_parses_json(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        payload = {"state": "OPEN", "headRefOid": "abc", "mergedAt": None}
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **_kwargs: Any) -> Any:
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps(payload))

        command_runner.side_effect = fake_gh_call

        assert adapter.gh_pr_state(7) == payload
        assert calls == [
            [
                "pr",
                "view",
                "7",
                "--json",
                "id,state,headRefOid,baseRefOid,mergedAt,mergeCommit,baseRefName,autoMergeRequest",
            ]
        ]

    def test_failure_returns_none(
        self, adapter: PipelineGitHub, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        def boom(argv: list[str], **_kwargs: Any) -> Any:
            raise RuntimeError("gh down")

        command_runner.side_effect = boom

        assert adapter.gh_pr_state(7) is None


class TestRateLimitParsing:
    """Read the current quota through the injected command runner."""

    def test_rate_limit_remaining_parses_graphql_budget(
        self, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        payload = {"resources": {"graphql": {"remaining": 42, "reset": 123}}}
        command_runner.side_effect = lambda argv, **_: SimpleNamespace(stdout=json.dumps(payload))

        assert transport_mod.rate_limit_remaining(call=command_runner) == (42, 123)

    def test_rate_limit_remaining_none_on_error(
        self, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        def boom(argv: list[str], **_: Any) -> Any:
            raise RuntimeError("gh down")

        command_runner.side_effect = boom

        assert transport_mod.rate_limit_remaining(call=command_runner) is None

    def test_rate_limit_remaining_none_on_malformed_payload(
        self, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
    ) -> None:
        command_runner.side_effect = lambda argv, **_: SimpleNamespace(stdout="not json")
        assert transport_mod.rate_limit_remaining(call=command_runner) is None

        command_runner.side_effect = lambda argv, **_: SimpleNamespace(stdout="{}")
        assert transport_mod.rate_limit_remaining(call=command_runner) is None


@pytest.mark.parametrize("node_id", ["PR_exact", "PR_other"])
def test_review_terminal_read_uses_node_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, node_id: str, command_runner: MagicMock
) -> None:
    """The review lifecycle read selects and validates the captured node."""
    calls: list[list[str]] = []

    def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "data": {
                        "node": {
                            "id": node_id,
                            "state": "MERGED",
                            "headRefOid": "a" * 40,
                            "mergedAt": "2026-09-07T05:47:12Z",
                        }
                    }
                }
            ),
        )

    command_runner.side_effect = fake_gh_call
    state = PipelineGitHub(
        "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
    ).reviewed_pr_state("PR_exact")
    assert (state is not None) == (node_id == "PR_exact")
    assert len(calls) == 1
    assert "id=PR_exact" in calls[0]
    assert any("node(id:$id)" in arg for arg in calls[0])


def test_dirty_direct_strict_create_rejects_existing_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
) -> None:
    """A strict continuation cannot adopt an existing branch PR."""
    adapter = PipelineGitHub(
        "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
    )
    monkeypatch.setattr(adapter, "_open_prs_for_branch", lambda branch: [(77, "main")])
    monkeypatch.setattr(adapter, "find_pr_for_issue", lambda issue: None)
    with pytest.raises(RuntimeError, match="absence"):
        adapter.create_pr(
            5, "5-auto-impl-direct-" + "a" * 32, "title", "Closes #5", strict_absence=True
        )


@pytest.mark.parametrize("existing", ["branch-other-base", "issue"])
def test_dirty_direct_strict_create_rejects_all_open_pr_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str, command_runner: MagicMock
) -> None:
    """Strict creation rejects both branch and issue-linked PR reuse."""
    adapter = PipelineGitHub(
        "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
    )
    monkeypatch.setattr(
        adapter,
        "_open_prs_for_branch",
        lambda branch: [(77, "release")] if existing == "branch-other-base" else [],
    )
    monkeypatch.setattr(
        adapter, "find_pr_for_issue", lambda issue: 78 if existing == "issue" else None
    )
    create = MagicMock()
    monkeypatch.setattr(adapter, "_gh", create)
    with pytest.raises(RuntimeError, match="absence violated"):
        adapter.create_pr(
            5, "5-auto-impl-direct-" + "a" * 32, "title", "Closes #5", strict_absence=True
        )
    create.assert_not_called()


@pytest.mark.parametrize("race", ["branch", "issue", "none", "incomplete"])
def test_dirty_direct_ambiguous_create_reads_once_without_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str, command_runner: MagicMock
) -> None:
    """An ambiguous create gets one complete read and never adopts a PR."""
    adapter = PipelineGitHub(
        "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
    )
    reads: list[str] = []

    def branches(branch: str) -> list[tuple[int, str]]:
        reads.append("branch")
        if reads.count("branch") == 2:
            if race == "incomplete":
                raise RuntimeError("incomplete read")
            if race == "branch":
                return [(77, "main")]
        return []

    def issue(number: int) -> int | None:
        reads.append("issue")
        return 78 if reads.count("issue") == 2 and race == "issue" else None

    monkeypatch.setattr(adapter, "_open_prs_for_branch", branches)
    monkeypatch.setattr(adapter, "find_pr_for_issue", issue)
    monkeypatch.setattr(prs_mod, "_assert_branch_commits_signed", lambda branch, **kwargs: None)
    create = MagicMock(side_effect=RuntimeError("ambiguous transport"))
    monkeypatch.setattr(adapter, "_gh", create)
    with pytest.raises(RuntimeError, match="strict PR"):
        adapter.create_pr(
            5, "5-auto-impl-direct-" + "a" * 32, "title", "Closes #5", strict_absence=True
        )
    assert create.call_count == 1
    assert reads.count("branch") == 2
    assert reads.count("issue") == (1 if race == "incomplete" else 2)


def test_dirty_direct_complete_branch_cap_blocks_strict_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
) -> None:
    """A capped branch result cannot prove absence or permit creation."""
    adapter = PipelineGitHub(
        "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
    )
    create = MagicMock(
        return_value=SimpleNamespace(
            stdout=json.dumps(
                [{"number": i + 1, "state": "CLOSED", "baseRefName": "main"} for i in range(1000)]
            )
        )
    )
    monkeypatch.setattr(adapter, "_gh", create)
    with pytest.raises(RuntimeError, match="absence read failed"):
        adapter.create_pr(
            5, "5-auto-impl-direct-" + "a" * 32, "title", "Closes #5", strict_absence=True
        )
    assert create.call_count == 1
    assert create.call_args.args[0][:2] == ["pr", "list"]


def test_dirty_direct_strict_create_submits_only_one_new_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_runner: MagicMock
) -> None:
    """Complete absence permits one normal signed-branch PR creation."""
    adapter = PipelineGitHub(
        "org", repo="repo-a", repo_root=tmp_path, command_runner=command_runner
    )
    monkeypatch.setattr(adapter, "_open_prs_for_branch", lambda branch: [])
    monkeypatch.setattr(adapter, "find_pr_for_issue", lambda issue: None)
    signed = MagicMock()
    monkeypatch.setattr(prs_mod, "_assert_branch_commits_signed", signed)
    create = MagicMock(return_value=SimpleNamespace(stdout="https://github.com/org/repo-a/pull/79"))
    monkeypatch.setattr(adapter, "_gh", create)
    branch = "5-auto-impl-direct-" + "a" * 32
    assert adapter.create_pr(5, branch, "title", "Closes #5", strict_absence=True) == 79
    signed.assert_called_once()
    assert signed.call_args.args == (branch,)
    assert signed.call_args.kwargs["base"] == "main"
    assert callable(signed.call_args.kwargs["run_git"])
    assert callable(signed.call_args.kwargs["verify_commit"])
    assert create.call_count == 1
    assert create.call_args.args[0][:2] == ["pr", "create"]


_MISSING_NODE = object()


def _dependency_node(
    number: int,
    typename: str,
    state: str,
    *,
    merged: object = _MISSING_NODE,
) -> dict[str, object]:
    """Build one raw ``issueOrPullRequest`` node for the batch contract."""
    node: dict[str, object] = {
        "number": number,
        "__typename": typename,
        "state": state,
    }
    if merged is not _MISSING_NODE:
        node["merged"] = merged
    return node


def _dependency_batch_adapter(
    tmp_path: Path,
    issue_numbers: tuple[int, ...],
    nodes: tuple[dict[str, object] | None, ...],
    *,
    graphql_error: bool = False,
) -> tuple[tuple[DependencyFact, ...], list[list[str]]]:
    """Call the future batch boundary with a deterministic GraphQL transport fake."""
    calls: list[list[str]] = []

    def command_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if graphql_error:
            payload: dict[str, object] = {
                "errors": [{"message": "dependency query failed"}],
            }
        else:
            query_argument = next(argument for argument in argv if argument.startswith("query="))
            aliases = re.findall(
                r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*issueOrPullRequest",
                query_argument,
            )
            assert len(aliases) == len(nodes)
            repository: dict[str, object] = {
                "owner": {"login": "owner"},
                "name": "repo",
            }
            for alias, node in zip(aliases, nodes, strict=True):
                if node is not None:
                    repository[alias] = node
            payload = {"data": {"repository": repository}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    adapter = PipelineGitHub(
        "owner", repo="repo", repo_root=tmp_path, command_runner=command_runner
    )
    facts = adapter.batch_dependency_facts(
        issue_numbers,
        deadline_s=time.monotonic() + 10.0,
        shutdown=threading.Event(),
    )
    return tuple(facts), calls


@pytest.mark.parametrize(
    ("node", "expected", "satisfied"),
    [
        (
            _dependency_node(10, "Issue", "CLOSED"),
            (10, "Issue", "CLOSED", None),
            True,
        ),
        (
            _dependency_node(10, "PullRequest", "MERGED", merged=True),
            (10, "PullRequest", "MERGED", True),
            True,
        ),
        (_dependency_node(10, "Issue", "OPEN"), (10, "Issue", "OPEN", None), False),
        (
            _dependency_node(10, "PullRequest", "OPEN", merged=False),
            (10, "PullRequest", "OPEN", False),
            False,
        ),
        (
            _dependency_node(10, "PullRequest", "CLOSED", merged=False),
            (10, "PullRequest", "CLOSED", False),
            False,
        ),
    ],
    ids=[
        "closed-issue",
        "merged-pr",
        "open-issue",
        "open-pr",
        "closed-unmerged-pr",
    ],
)
def test_batch_dependency_facts_preserves_typed_lifecycle(
    tmp_path: Path,
    node: dict[str, object],
    expected: tuple[object, ...],
    satisfied: bool,
) -> None:
    """One repository read returns the node kind and lifecycle without guessing."""
    facts, calls = _dependency_batch_adapter(tmp_path, (10,), (node,))

    assert len(calls) == 1
    fact = facts[0]
    assert (
        fact.number,
        fact.typename,
        fact.state,
        fact.merged,
    ) == expected
    assert fact.satisfied is satisfied


def test_batch_dependency_facts_returns_canonical_request_order(tmp_path: Path) -> None:
    """Mixed issue and PR facts stay in the sorted dependency order."""
    numbers = (10, 20, 30)
    nodes = (
        _dependency_node(10, "Issue", "CLOSED"),
        _dependency_node(20, "PullRequest", "MERGED", merged=True),
        _dependency_node(30, "Issue", "OPEN"),
    )

    facts, calls = _dependency_batch_adapter(tmp_path, numbers, nodes)

    assert len(calls) == 1
    assert "owner=owner" in calls[0]
    assert "name=repo" in calls[0]
    query_argument = next(argument for argument in calls[0] if argument.startswith("query="))
    assert "... on Issue { number state }" in query_argument
    assert "... on PullRequest { number state merged }" in query_argument
    assert [fact.number for fact in facts] == list(numbers)


@pytest.mark.parametrize(
    "nodes",
    [
        (None,),
        (_dependency_node(999, "Issue", "OPEN"),),
        (_dependency_node(10, "Repository", "CLOSED"),),
        (_dependency_node(10, "Issue", "MERGED"),),
        (_dependency_node(10, "Issue", "CLOSED", merged=True),),
        (_dependency_node(10, "PullRequest", "OPEN", merged=True),),
        (_dependency_node(10, "PullRequest", "CLOSED", merged=True),),
        (_dependency_node(10, "PullRequest", "MERGED", merged=False),),
        (_dependency_node(10, "PullRequest", "CLOSED", merged="yes"),),
    ],
    ids=[
        "missing-node",
        "number-mismatch",
        "unsupported-typename",
        "invalid-issue-state",
        "issue-merged-field",
        "open-pr-merged",
        "closed-pr-merged",
        "merged-pr-unmerged",
        "nonboolean-merged",
    ],
)
def test_batch_dependency_facts_rejects_malformed_or_ambiguous_nodes(
    tmp_path: Path,
    nodes: tuple[dict[str, object] | None, ...],
) -> None:
    """A malformed node cannot become a satisfied dependency by omission."""
    with pytest.raises((ValueError, RuntimeError)):
        _dependency_batch_adapter(tmp_path, (10,), nodes)


def test_batch_dependency_facts_rejects_partial_and_duplicate_responses(tmp_path: Path) -> None:
    """Every requested number must have one exact node in the batch response."""
    with pytest.raises((ValueError, RuntimeError)):
        _dependency_batch_adapter(
            tmp_path,
            (10, 20),
            (_dependency_node(10, "Issue", "OPEN"), None),
        )
    with pytest.raises((ValueError, RuntimeError)):
        _dependency_batch_adapter(
            tmp_path,
            (10, 20),
            (
                _dependency_node(10, "Issue", "OPEN"),
                _dependency_node(10, "Issue", "CLOSED"),
            ),
        )


def test_batch_dependency_facts_rejects_duplicate_requests(tmp_path: Path) -> None:
    """Duplicate dependency numbers are rejected before ambiguous admission."""
    with pytest.raises((ValueError, RuntimeError)):
        _dependency_batch_adapter(
            tmp_path,
            (10, 10),
            (
                _dependency_node(10, "Issue", "OPEN"),
                _dependency_node(10, "Issue", "OPEN"),
            ),
        )


def test_batch_dependency_facts_propagates_graphql_failures(tmp_path: Path) -> None:
    """A GraphQL error fails the read instead of authorizing implementation."""
    with pytest.raises(RuntimeError, match="dependency query failed"):
        _dependency_batch_adapter(
            tmp_path,
            (10,),
            (_dependency_node(10, "Issue", "CLOSED"),),
            graphql_error=True,
        )
