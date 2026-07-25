"""PipelineGitHub adapter tests: mapping + dry-run log-and-skip (#1817).

The adapter is the ONE place coordinator-neutral mutator names map onto the
real ``github_api`` / ``pr_manager`` / ``_review_utils`` helpers, and the
place the ``StageGitHub`` protocol's dry-run contract is honored.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from time import sleep
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

import hephaestus.automation.github_api as github_api_mod
import hephaestus.automation.pipeline_github as pg
import hephaestus.automation.pr_manager as pr_manager_mod
from hephaestus.automation.pipeline.stages.base import StageGitHub
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_PREFIX,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    render_current_plan,
    render_current_review,
)
from hephaestus.utils.file_lock import LockUnavailableError


def _claim_drive_green_learn_from_process(repo_root: str, start_barrier: Any, results: Any) -> None:
    """Race one real adapter claim from a separate process for lock coverage."""
    adapter = pg.PipelineGitHub("org", dry_run=False, repo_root=Path(repo_root))
    original_save = adapter._arming.save

    def delayed_save(issue_number: int, record: dict[str, Any]) -> bool:
        sleep(0.1)
        return original_save(issue_number, record)

    with patch.object(adapter._arming, "save", side_effect=delayed_save):
        start_barrier.wait()
        results.put(adapter.claim_drive_green_learn(33, 703))


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
    ("post_pr_comment", (7, "why"), "github_api", "gh_issue_comment"),
    (
        "upsert_pr_comment",
        (7, "<!-- marker -->", "<!-- marker -->\nbody"),
        "github_api",
        "gh_issue_upsert_comment",
    ),
    ("mark_pr_implementation_go", (7,), "pr_manager", "mark_pr_implementation_go"),
    ("mark_pr_implementation_no_go", (7,), "pr_manager", "mark_pr_implementation_no_go"),
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
        if method == "mark_pr_implementation_go":
            monkeypatch.setattr(
                adapter, "pr_has_implementation_state_label", lambda _pr: (True, False)
            )
        elif method == "mark_pr_implementation_no_go":
            monkeypatch.setattr(
                adapter, "pr_has_implementation_state_label", lambda _pr: (False, True)
            )

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

    def test_dry_run_pr_comment_upsert_reports_not_written(
        self,
        dry_adapter: pg.PipelineGitHub,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dry-run artifacts must be reported as absent in durable stage events."""
        mock = _patch_target(monkeypatch, "github_api", "gh_issue_upsert_comment")

        written = dry_adapter.upsert_pr_comment(7, "<!-- marker -->", "body")

        assert written is False
        mock.assert_not_called()

    def test_upsert_plan_comment_keys_on_marker(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = MagicMock(return_value=[])
        post = MagicMock()
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)
        body = render_current_plan("body")

        adapter.upsert_plan_comment(5, body)

        assert fetch.call_args_list == [call(5), call(5)]
        post.assert_called_once_with(5, body)

    def test_upsert_ignores_foreign_canonical_marker_and_creates_owned_comment(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Foreign marker text is inert and cannot deny service to the journal."""
        body = render_current_plan("safe plan")
        fetch = MagicMock(
            side_effect=[
                [
                    {
                        "body": f"{PLAN_CANONICAL_MARKER}\nforeign",
                        "databaseId": 99,
                        "viewerDidAuthor": False,
                    }
                ],
                [
                    {
                        "body": f"{PLAN_CANONICAL_MARKER}\nforeign",
                        "databaseId": 99,
                        "viewerDidAuthor": False,
                    },
                    {
                        "body": body,
                        "databaseId": 100,
                        "viewerDidAuthor": True,
                    },
                ],
            ]
        )
        monkeypatch.setattr(adapter, "_repo_issue_comments", fetch)
        post = MagicMock()
        monkeypatch.setattr(github_api_mod, "gh_issue_comment", post)

        adapter.upsert_plan_comment(5, body)

        post.assert_called_once_with(5, body)
        assert fetch.call_count == 2

    def test_canonical_create_converges_owned_race_duplicates(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A post-create reconciliation leaves one actor-owned canonical pointer."""
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

        adapter.upsert_plan_comment(5, body)

        delete.assert_called_once_with(100)

    def test_ensure_blocked_audit_repairs_missing_explanation_without_label_write(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restart repair writes only the canonical audit record."""
        monkeypatch.setattr(
            adapter,
            "issue_comments",
            MagicMock(
                return_value=[
                    IssueComment(body=render_current_plan("Plan"), viewer_did_author=True)
                ]
            ),
        )
        upsert = MagicMock()
        monkeypatch.setattr(adapter, "upsert_issue_comment", upsert)

        adapter.ensure_blocked_audit(5)

        assert upsert.call_args.args[:2] == (5, PLAN_REVIEW_CANONICAL_MARKER)
        assert upsert.call_args.args[2].endswith("state:plan-blocked")
        assert upsert.call_args.kwargs == {"legacy_marker": PLAN_REVIEW_PREFIX}

    def test_ensure_blocked_audit_preserves_existing_detailed_review(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid BLOCKED explanation is never replaced by the recovery text."""
        monkeypatch.setattr(
            adapter,
            "issue_comments",
            MagicMock(
                return_value=[
                    IssueComment(body=render_current_plan("Plan"), viewer_did_author=True),
                    IssueComment(
                        body=render_current_review(
                            "Waiting for API ownership.\n\nstate:plan-blocked",
                            revision=1,
                        ),
                        viewer_did_author=True,
                    ),
                ]
            ),
        )
        upsert = MagicMock()
        monkeypatch.setattr(adapter, "upsert_issue_comment", upsert)

        adapter.ensure_blocked_audit(5)

        upsert.assert_not_called()

    def test_dry_run_blocked_audit_repair_is_read_only(
        self,
        dry_adapter: pg.PipelineGitHub,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dry-run may inspect a missing audit but cannot mutate comments or labels."""
        read = MagicMock(
            return_value=[IssueComment(body=render_current_plan("Plan"), viewer_did_author=True)]
        )
        monkeypatch.setattr(dry_adapter, "issue_comments", read)
        post = MagicMock()
        delete = MagicMock()
        edit_labels = MagicMock()
        gh = MagicMock()
        monkeypatch.setattr(dry_adapter, "_post_issue_comment", post)
        monkeypatch.setattr(dry_adapter, "_delete_issue_comment", delete)
        monkeypatch.setattr(dry_adapter, "edit_labels", edit_labels)
        monkeypatch.setattr(dry_adapter, "_gh", gh)

        with caplog.at_level("INFO"):
            dry_adapter.ensure_blocked_audit(5)

        read.assert_called_once_with(5)
        post.assert_not_called()
        delete.assert_not_called()
        edit_labels.assert_not_called()
        gh.assert_not_called()
        assert any("[dry-run] would upsert" in record.message for record in caplog.records)

    def test_immutable_append_ignores_foreign_collision(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
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

    def test_immutable_append_is_replay_safe_and_conflict_detecting(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
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

    def test_immutable_append_never_deletes_identical_owned_duplicates(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
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

        adapter.append_issue_comment(5, marker, body)

        post.assert_not_called()
        delete.assert_not_called()


class TestRepoScoping:
    """PipelineGitHub must target its configured repository explicitly."""

    def test_issue_comments_returns_bodies_in_adapter_order(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [
                {"body": "plan", "databaseId": 1},
                {"body": "review", "databaseId": 2},
            ],
        )

        assert adapter.issue_comments(7) == [
            IssueComment(body="plan", database_id=1),
            IssueComment(body="review", database_id=2),
        ]

    def test_issue_reads_include_repo_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        assert (
            pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).gh_issue_json(5)["number"]
            == 5
        )

        assert calls == [
            [
                "issue",
                "view",
                "5",
                "--json",
                "number,title,state,labels,body",
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
    ) -> None:
        """Fork heads are readable but cannot receive a base-origin address push."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps(payload))

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)
        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)

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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["label", "list"]:
                return SimpleNamespace(stdout="[]")
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).add_labels(5, ["state:x"])

        assert calls[-1] == [
            "issue",
            "edit",
            "5",
            "--add-label",
            "state:x",
            "--repo",
            "org/repo-a",
        ]

    def test_plan_presence_does_not_backfill_from_review_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["issue", "view"]:
                payload = {
                    "comments": [
                        {
                            "body": f"{PLAN_REVIEW_PREFIX}\n\nstate:plan-go",
                            "viewerDidAuthor": True,
                        }
                    ],
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        assert not pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).has_existing_plan(5)

        assert calls == [
            [
                "issue",
                "view",
                "5",
                "--json",
                "comments",
                "--repo",
                "org/repo-a",
            ],
        ]

    def test_repo_scoped_pr_comment_upsert_reads_pr_comments_via_rest_issue_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR numbers are valid issue-comment REST targets but not GraphQL issue nodes."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv == [
                "api",
                "/repos/org/repo-a/issues/1001/comments?per_page=100&page=1",
            ]:
                payload = [{"id": 42, "body": "<!-- marker -->\nstale"}]
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).upsert_pr_comment(
            1001, "<!-- marker -->", "<!-- marker -->\nupdated"
        )

        assert calls[0] == [
            "api",
            "/repos/org/repo-a/issues/1001/comments?per_page=100&page=1",
        ]
        assert all("graphql" not in call for call in calls)

    def test_repo_scoped_has_existing_plan_detects_plan_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["issue", "view"]:
                payload = {
                    "labels": [],
                    "comments": [
                        {
                            "body": f"{PLAN_COMMENT_MARKER}\n\nDo the thing.",
                            "viewerDidAuthor": True,
                        }
                    ],
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        assert pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).has_existing_plan(5)

    def test_repo_scoped_has_existing_plan_ignores_foreign_plan_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Foreign marker text is inert and cannot impersonate the plan artifact."""

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["issue", "view"]:
                payload = {
                    "comments": [
                        {
                            "body": f"{PLAN_COMMENT_MARKER}\n\nSpoofed plan.",
                            "viewerDidAuthor": False,
                        }
                    ],
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        assert not pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).has_existing_plan(5)

    def test_repo_scoped_has_existing_plan_ignores_review_state_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Artifact presence is independent from the authoritative state label."""

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["issue", "view"]:
                payload = {
                    "labels": [],
                    "comments": [
                        {
                            "body": f"{PLAN_COMMENT_MARKER}\n\nOld rejected plan.",
                            "viewerDidAuthor": True,
                        },
                        {
                            "body": f"{PLAN_REVIEW_PREFIX}\n\nstate:plan-no-go",
                            "viewerDidAuthor": True,
                        },
                    ],
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        assert pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).has_existing_plan(5)

    def test_repo_scoped_pr_lookup_raises_on_gh_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repo-scoped seeding must fail closed instead of inventing no-PR state."""

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            raise RuntimeError("gh unavailable")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        with pytest.raises(RuntimeError, match="gh unavailable"):
            pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).find_pr_for_issue(5)

    def test_repo_scoped_pr_lookup_uses_shared_branch_formatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The head-branch lookup should consult the shared branch-name formatter."""
        calls: list[list[str]] = []

        monkeypatch.setattr(
            pg,
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

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        pr_number = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).find_pr_for_issue(7)

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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        assert adapter.find_pr_for_issue(5) == 5
        assert len(calls) == 1

    def test_repo_scoped_pr_lookup_returns_all_siblings_without_auto_merge_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery is read-only; a sibling cannot trigger a merge mutation."""
        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)

        monkeypatch.setattr(
            github_api_mod,
            "_find_open_prs_for_head",
            lambda _branch, _runner: [(5, "main"), (6, "release")],
        )

        assert adapter._open_prs_for_branch("branch") == [(5, "main"), (6, "release")]

    def test_repo_scoped_lookup_contains_valid_prs_before_rejecting_malformed_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)
        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            adapter._open_prs_for_branch("branch")

        assert len(calls) == 1

    def test_repo_scoped_closing_pr_lookup_contains_every_fallback_head_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        assert adapter.find_pr_for_issue(7) == 8

        assert not any("state,autoMergeRequest" in call for call in calls)

    def test_repo_scoped_pr_lookup_rejects_empty_successful_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank discovery output cannot become an invented no-PR state."""
        monkeypatch.setattr(pg, "gh_call", lambda _argv, **_kwargs: SimpleNamespace(stdout=""))

        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            adapter.find_pr_for_issue(5)

    def test_repo_scoped_merged_pr_lookup_preserves_head_branch_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merged lookup still finds a PR on the canonical issue branch."""
        monkeypatch.setattr(
            pg,
            "gh_call",
            lambda _argv, **_kwargs: SimpleNamespace(stdout=json.dumps([{"number": 5}])),
        )

        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        assert adapter.find_merged_pr_for_issue(5) == 5

    def test_repo_scoped_unresolved_threads_counts_automation_and_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            payload = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
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
                                ]
                            }
                        }
                    }
                }
            }
            return SimpleNamespace(stdout=json.dumps(payload))

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)
        monkeypatch.setattr(github_api_mod, "gh_current_login", lambda: "ci-bot")

        assert pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).count_unresolved_threads(
            7
        ) == (1, 1)

        assert calls[0][:2] == ["api", "graphql"]
        assert "-F" in calls[0]
        assert "owner=org" in calls[0]
        assert "name=repo-a" in calls[0]

    def test_repo_scoped_fetch_error_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1868: repo-scoped path must propagate GraphQL errors, matching legacy."""

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            raise RuntimeError("gh: GraphQL: Head sha can't be blank")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        with pytest.raises(RuntimeError, match="Head sha"):
            adapter.count_unresolved_threads(7)

    def test_repo_scoped_upsert_plan_comment_updates_marker_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv == [
                "api",
                "/repos/org/repo-a/issues/5/comments?per_page=100&page=1",
            ]:
                payload = [
                    {
                        "id": 9,
                        "body": f"{PLAN_COMMENT_MARKER}\nold",
                        "viewerDidAuthor": True,
                    }
                ]
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).upsert_plan_comment(
            5, render_current_plan("new")
        )

        assert any(call[:3] == ["api", "--method", "PATCH"] for call in calls)
        assert any("/repos/org/repo-a/issues/comments/9" in call for call in calls)
        assert not any(call[:2] == ["issue", "comment"] for call in calls)

    def test_repo_scoped_upsert_pr_comment_updates_marker_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        marker = "<!-- hephaestus-pr-review-go -->"

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv == [
                "api",
                "/repos/org/repo-a/issues/7/comments?per_page=100&page=1",
            ]:
                payload = [{"id": 12, "body": f"{marker}\nold"}]
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).upsert_pr_comment(
            7, marker, f"{marker}\nnew"
        )

        assert any(call[:3] == ["api", "--method", "PATCH"] for call in calls)
        assert any("/repos/org/repo-a/issues/comments/12" in call for call in calls)
        assert not any(call[:2] == ["issue", "comment"] for call in calls)

    def test_repo_scoped_review_post_uses_repo_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["api", "graphql"]:
                payload = {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "thread-1",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {"pullRequestReview": {"id": "review-node"}}
                                                ]
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            if "repos/org/repo-a/pulls/7/reviews" in argv:
                return SimpleNamespace(stdout=json.dumps({"node_id": "review-node"}))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        posted = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).post_review_threads(
            7, [], "summary"
        )

        assert posted == ["thread-1"]
        assert any("repos/org/repo-a/pulls/7/reviews" in call for call in calls)

    def test_repo_scoped_review_post_warns_on_zero_matched_threads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A posted review with comments that matches no GraphQL thread logs a warning.

        The ``pr diff`` call below is unmatched by ``fake_gh_call`` and returns
        empty stdout, so ``_filter_comments_to_diff`` fails open (diff.py:95-96)
        and ``review_comments`` stays non-empty — required for the warning branch
        (posted comments but zero matched threads) to trigger.
        """

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["api", "graphql"]:
                payload = {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "thread-1",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "pullRequestReview": {
                                                            "id": "other-review-node"
                                                        }
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            if "repos/org/repo-a/pulls/7/reviews" in argv:
                return SimpleNamespace(stdout=json.dumps({"id": 999, "node_id": "review-node"}))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        with caplog.at_level("WARNING", logger=pg.__name__):
            posted = pg.PipelineGitHub(
                "org", repo="repo-a", repo_root=tmp_path
            ).post_review_threads(7, [{"path": "a.py", "line": 1, "body": "x"}], "summary")

        assert posted == []
        assert any("matched zero review threads" in r.message for r in caplog.records)


class TestRepoReviewThreadsForReview:
    """_repo_review_threads_for_review: REST node_id vs GraphQL pullRequestReview.id."""

    def test_round_trips_rest_node_id_against_graphql_review_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the domain-equality invariant: REST node_id IS the GraphQL id.

        A realistic REST review POST response's ``node_id`` is used as the
        GraphQL ``pullRequestReview.id`` filter against two independently
        constructed thread fixtures — one whose review id matches, one whose
        review id is unrelated. ``result == ["PRRT_matching"]`` only holds if
        production's ``review.get("id") != review_id`` comparison
        (pipeline_github.py:482) correctly equates the REST and GraphQL id
        domains and correctly rejects the unrelated one; this IS the
        "assert the id domains match" check the issue asks for — it exercises
        real production comparison logic, not a restated literal.
        """
        rest_review_response: dict[str, Any] = {"id": 4242, "node_id": "PRR_kwDOA1b2c3M"}

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["api", "graphql"]:
                payload = {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "PRRT_matching",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "pullRequestReview": {
                                                            "id": rest_review_response["node_id"]
                                                        }
                                                    }
                                                ]
                                            },
                                        },
                                        {
                                            "id": "PRRT_other_review",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {"pullRequestReview": {"id": "PRR_unrelated"}}
                                                ]
                                            },
                                        },
                                    ]
                                }
                            }
                        }
                    }
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        gh = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        result = gh._repo_review_threads_for_review(7, str(rest_review_response["node_id"]))

        assert result == ["PRRT_matching"]

    def test_resolved_thread_from_same_review_is_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["api", "graphql"]:
                payload = {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "PRRT_resolved",
                                            "isResolved": True,
                                            "comments": {
                                                "nodes": [
                                                    {"pullRequestReview": {"id": "review-node"}}
                                                ]
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
                return SimpleNamespace(stdout=json.dumps(payload))
            return SimpleNamespace(stdout="")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        gh = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        result = gh._repo_review_threads_for_review(7, "review-node")

        assert result == []


class TestRepoScopedAutoMerge:
    """The pipeline adapter intentionally exposes no auto-merge mutators."""

    def test_pipeline_adapter_has_no_auto_merge_mutation_surface(self, tmp_path: Path) -> None:
        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)

        assert not hasattr(adapter, "arm_auto_merge")
        assert not hasattr(adapter, "defer_auto_merge")


class TestCreatePr:
    """create_pr: idempotent reuse, given-body create, dry-run neutral."""

    def test_repo_scoped_reuses_existing_open_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        monkeypatch.setattr(adapter, "_open_prs_for_branch", lambda branch: [])
        monkeypatch.setattr(adapter, "find_pr_for_issue", lambda issue: 77)
        create = _patch_target(monkeypatch, "github_api", "gh_pr_create")

        assert adapter.create_pr(5, "branch", "t", "b") == 77
        create.assert_not_called()

    def test_unscoped_create_fails_closed_without_legacy_helper(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR creation cannot delegate to the legacy auto-merge-capable path."""
        create = MagicMock()
        monkeypatch.setattr(github_api_mod, "gh_pr_create", create)

        with pytest.raises(RuntimeError, match="repo-scoped"):
            adapter.create_pr(5, "branch", "title", "body\n\nCloses #5")

        create.assert_not_called()

    def test_dry_run_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        dry_adapter = pg.PipelineGitHub("org", repo="repo-a", dry_run=True, repo_root=tmp_path)
        monkeypatch.setattr(dry_adapter, "_open_prs_for_branch", lambda branch: [])
        monkeypatch.setattr(dry_adapter, "find_pr_for_issue", lambda issue: None)
        create = _patch_target(monkeypatch, "github_api", "gh_pr_create")

        assert dry_adapter.create_pr(5, "b", "t", "x") == 0
        create.assert_not_called()

    def test_repo_scoped_create_pr_parses_pull_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        monkeypatch.setattr(pg.PipelineGitHub, "find_pr_for_issue", lambda self, issue: None)
        monkeypatch.setattr(
            github_api_mod, "_assert_branch_commits_signed", lambda branch, base: None
        )

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "list"]:
                return SimpleNamespace(stdout="[]")
            return SimpleNamespace(stdout="https://github.com/org/repo-a/pull/1888\n")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        pr_number = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).create_pr(
            1887, "1887-auto-impl", "title", "body\n\nCloses #1887"
        )

        assert pr_number == 1888
        assert calls[0][-2:] == ["--repo", "org/repo-a"]

    def test_repo_scoped_create_pr_contains_all_existing_head_prs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom branch cannot bypass all-head containment before PR reuse."""
        calls: list[list[str]] = []
        monkeypatch.setattr(pg.PipelineGitHub, "find_pr_for_issue", lambda self, issue: None)
        monkeypatch.setattr(
            github_api_mod, "_assert_branch_commits_signed", lambda branch, base: None
        )

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

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
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
    ) -> None:
        monkeypatch.setattr(pg.PipelineGitHub, "find_pr_for_issue", lambda self, issue: None)
        monkeypatch.setattr(
            github_api_mod, "_assert_branch_commits_signed", lambda branch, base: None
        )

        def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["pr", "list"]:
                return SimpleNamespace(stdout="[]")
            return SimpleNamespace(stdout=stdout)

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)

        with caplog.at_level("ERROR", logger=pg.__name__):
            with pytest.raises(RuntimeError, match="Failed to parse PR number") as excinfo:
                adapter.create_pr(1887, "1887-auto-impl", "title", "body\n\nCloses #1887")

        assert stdout in str(excinfo.value)
        assert any(stdout in record.message for record in caplog.records)


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
        monkeypatch.setattr(adapter, "_find_pr_for_issue", lambda n, state: 2)
        monkeypatch.setattr(pg, "get_pr_head_branch", lambda n: "head")
        monkeypatch.setattr(
            pg,
            "gh_call",
            lambda argv, **kwargs: SimpleNamespace(
                stdout=json.dumps(
                    {
                        "comments": [
                            {
                                "body": f"{PLAN_COMMENT_MARKER}\nPlan",
                                "viewerDidAuthor": True,
                            }
                        ]
                    }
                )
            ),
        )

        assert adapter.find_merged_closing_pr(9) == 1
        assert adapter.find_pr_for_issue(9) == 2
        assert adapter.get_pr_head_branch(9) == "head"
        assert adapter.has_existing_plan(9) is True

    def test_unscoped_pr_lookup_contains_every_same_head_pr(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The optional unscoped accessor discovers siblings read-only."""
        monkeypatch.setattr(
            pg,
            "gh_call",
            lambda argv, **_kwargs: SimpleNamespace(
                stdout=json.dumps(
                    [
                        {"number": 5, "state": "OPEN", "baseRefName": "main"},
                        {"number": 6, "state": "OPEN", "baseRefName": "release"},
                    ]
                )
            ),
        )

        assert adapter.find_pr_for_issue(5) == 5

    def test_find_issue_for_pr_parses_exact_closes_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps({"body": "Summary\n\nCloses #1899\n"}))

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        issue = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).find_issue_for_pr(1984)

        assert issue == 1899
        assert calls == [["pr", "view", "1984", "--json", "body", "--repo", "org/repo-a"]]

    @pytest.mark.parametrize("body", ["Fixes #1899\n", "Closes #1899, #1900\n", ""])
    def test_find_issue_for_pr_rejects_non_policy_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
    ) -> None:
        monkeypatch.setattr(
            pg,
            "gh_call",
            lambda argv, **kwargs: SimpleNamespace(stdout=json.dumps({"body": body})),
        )

        issue = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).find_issue_for_pr(1984)

        assert issue is None

    def test_pr_review_context_reads_metadata_for_a_checkout_bound_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The checkout barrier, not mutable GitHub diff output, supplies the diff."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if argv[:2] == ["pr", "view"]:
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "body": "Closes #1899\n",
                            "headRefOid": "a" * 40,
                            "baseRefName": "main",
                        }
                    )
                )
            raise AssertionError(f"unexpected gh invocation: {argv}")

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        context = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).pr_review_context(
            1984
        )

        assert context == {
            "pr_description": "Closes #1899\n",
            "pr_head_sha": "a" * 40,
            "pr_base_branch": "main",
        }
        assert calls == [
            [
                "pr",
                "view",
                "1984",
                "--json",
                "body,headRefOid,baseRefName",
                "--repo",
                "org/repo-a",
            ],
        ]

    def test_pr_review_context_does_not_request_mutable_remote_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An A -> B -> A race cannot pair B's remote diff with proof for A."""
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "body": "Closes #1899",
                        "headRefOid": "a" * 40,
                        "baseRefName": "main",
                    }
                )
            )

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        context = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path).pr_review_context(
            1984
        )

        assert context is not None
        assert context["pr_head_sha"] == "a" * 40
        assert "pr_diff" not in context
        assert all(argv[:2] != ["pr", "diff"] for argv in calls)

    def test_pr_manager_implementation_label_read(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pr_manager_mod, "pr_has_implementation_state_label", lambda n: (True, False)
        )
        assert adapter.pr_has_implementation_state_label(7) == (True, False)


class TestUnresolvedThreads:
    """count_unresolved_threads mirrors #1152: counts only, resolves nothing."""

    def test_count_unresolved_threads_uses_split_helper(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count_unresolved_threads delegates ownership splitting to _split_threads."""
        threads = [{"id": "a"}, {"id": "b"}]
        monkeypatch.setattr(adapter, "_unresolved_threads", lambda n: threads)
        split = MagicMock(return_value=(3, 4))
        monkeypatch.setattr(pg, "_split_threads", split)

        assert adapter.count_unresolved_threads(7) == (3, 4)
        split.assert_called_once_with(threads)

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

    def test_empty_result_returns_zero(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_api_mod, "gh_pr_list_unresolved_threads", lambda n, dry_run: [])
        assert adapter.count_unresolved_threads(7) == (0, 0)

    def test_legacy_fetch_error_fails_closed(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1868: legacy path must propagate fetch errors, not fail open to (0, 0)."""

        def boom(n: int, dry_run: bool) -> list[dict[str, Any]]:
            raise RuntimeError("api")

        monkeypatch.setattr(github_api_mod, "gh_pr_list_unresolved_threads", boom)
        with pytest.raises(RuntimeError, match="api"):
            adapter.count_unresolved_threads(7)


class TestGhPrState:
    """The merge_wait single PR-state read (re-housed CIDriver._gh_pr_state)."""

    def test_success_parses_json(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"state": "OPEN", "headRefOid": "abc", "mergedAt": None}
        calls: list[list[str]] = []

        def fake_gh_call(argv: list[str]) -> Any:
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps(payload))

        monkeypatch.setattr(pg, "gh_call", fake_gh_call)

        assert adapter.gh_pr_state(7) == payload
        assert calls == [
            [
                "pr",
                "view",
                "7",
                "--json",
                "state,headRefOid,mergedAt,baseRefName,autoMergeRequest",
            ]
        ]

    def test_failure_returns_none(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(argv: list[str]) -> Any:
            raise RuntimeError("gh down")

        monkeypatch.setattr(pg, "gh_call", boom)

        assert adapter.gh_pr_state(7) is None


class TestDriveGreenLearning:
    """Post-merge learning state remains independent of merge arming."""

    def test_learn_claim_is_durable_and_never_replayable(self, adapter: pg.PipelineGitHub) -> None:
        """A crash after dispatch claim is an explicit unknown, never a replay."""
        assert adapter.claim_drive_green_learn(31, 701) is True
        assert adapter.drive_green_learn_inflight(31) is True
        assert adapter.claim_drive_green_learn(31, 701) is False
        assert adapter.drive_green_learn_terminal(31) is False

    def test_concurrent_learn_claims_allow_exactly_one_dispatch(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two coordinators racing on one issue cannot both claim /learn."""
        original_save = adapter._arming.save

        def delayed_save(issue_number: int, record: dict[str, Any]) -> bool:
            # Without the stable claim lock both workers load an unclaimed
            # record during this delay and would each report a successful
            # claim. The lock holds the second worker outside the read.
            sleep(0.05)
            return original_save(issue_number, record)

        monkeypatch.setattr(adapter._arming, "save", delayed_save)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda _unused: adapter.claim_drive_green_learn(32, 702),
                    range(2),
                )
            )

        assert outcomes.count(True) == 1
        assert outcomes.count(False) == 1

    def test_process_racing_learn_claims_allow_exactly_one_dispatch(self, tmp_path: Path) -> None:
        """The claim lock coordinates separate automation-loop processes."""
        pytest.importorskip("fcntl")
        context = get_context("spawn")
        start_barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_claim_drive_green_learn_from_process,
                args=(str(tmp_path), start_barrier, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        outcomes = [results.get(timeout=1) for _ in processes]
        assert outcomes.count(True) == 1
        assert outcomes.count(False) == 1

    def test_learn_claim_fails_closed_without_an_exclusive_lock(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pipeline refuses an external /learn action if locking is absent."""
        unavailable_lock = MagicMock(side_effect=LockUnavailableError("exclusive lock unsupported"))
        monkeypatch.setattr(pg, "file_lock", unavailable_lock)

        with pytest.raises(LockUnavailableError, match="exclusive lock unsupported"):
            adapter.claim_drive_green_learn(34, 704)

        unavailable_lock.assert_called_once_with(
            adapter._arming.learn_claim_lock_path(34),
            require_exclusive=True,
        )
        assert adapter._arming.load(34) is None

    def test_failed_learn_is_also_terminal(self, adapter: pg.PipelineGitHub) -> None:
        adapter.mark_drive_green_learn_result(4, succeeded=False)

        assert adapter.drive_green_learn_terminal(4) is True


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


class TestSeverityMarker:
    """Severity marker embedding and classification for the GO gate (#1856)."""

    def test_with_severity_marker_embeds(self) -> None:
        """_with_severity_marker embeds severity marker in body."""
        comment = {
            "severity": "minor",
            "body": "Fix this",
        }
        result = pg._with_severity_marker(comment)
        assert result.startswith("<!-- hephaestus-severity: minor -->")
        assert "Fix this" in result

    def test_with_severity_marker_defaults_absent_to_major(self) -> None:
        """_with_severity_marker defaults absent severity to major (fail-safe)."""
        comment = {
            "body": "Fix this",
        }
        result = pg._with_severity_marker(comment)
        assert result.startswith("<!-- hephaestus-severity: major -->")

    def test_with_severity_marker_is_idempotent(self) -> None:
        """_with_severity_marker does not double-stamp already-marked bodies."""
        comment = {
            "body": "<!-- hephaestus-severity: critical -->\nAlready marked",
            "severity": "minor",
        }
        result = pg._with_severity_marker(comment)
        # Should return the body unchanged because it already has the marker
        assert result == comment["body"]

    def test_thread_severity_is_blocking_critical(self) -> None:
        """_thread_severity_is_blocking returns True for critical severity."""
        thread = {"body": "<!-- hephaestus-severity: critical -->\nSome issue"}
        assert pg._thread_severity_is_blocking(thread) is True

    def test_thread_severity_is_blocking_major(self) -> None:
        """_thread_severity_is_blocking returns True for major severity."""
        thread = {"body": "<!-- hephaestus-severity: major -->\nSome issue"}
        assert pg._thread_severity_is_blocking(thread) is True

    def test_thread_severity_is_blocking_minor_false(self) -> None:
        """_thread_severity_is_blocking returns False for minor severity."""
        thread = {"body": "<!-- hephaestus-severity: minor -->\nSome issue"}
        assert pg._thread_severity_is_blocking(thread) is False

    def test_thread_severity_is_blocking_nitpick_false(self) -> None:
        """_thread_severity_is_blocking returns False for nitpick severity."""
        thread = {"body": "<!-- hephaestus-severity: nitpick -->\nSome issue"}
        assert pg._thread_severity_is_blocking(thread) is False

    def test_thread_severity_is_blocking_missing_defaults_true(self) -> None:
        """_thread_severity_is_blocking returns True (blocking) for missing marker."""
        thread = {"body": "No marker here\nJust plain text"}
        assert pg._thread_severity_is_blocking(thread) is True

    def test_thread_severity_anchors_on_marker_line(self) -> None:
        """_thread_severity_is_blocking anchors on marker line, not substring."""
        thread = {
            "body": "Some text mentioning minor\n<!-- hephaestus-severity: critical -->\nMore text"
        }
        # Should find 'critical' in marker, not 'minor' in prose
        assert pg._thread_severity_is_blocking(thread) is True

    def test_count_unresolved_threads_by_severity_classifies(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count_unresolved_threads_by_severity classifies threads by severity."""
        automation_thread_critical = {
            "body": "<!-- hephaestus-severity: critical -->\nIssue",
            "author": "automation-bot",
        }
        automation_thread_minor = {
            "body": "<!-- hephaestus-severity: minor -->\nNit",
            "author": "automation-bot",
        }
        human_thread = {
            "body": "Human comment",
            "author": "reviewer",
        }

        threads = [automation_thread_critical, automation_thread_minor, human_thread]

        monkeypatch.setattr(adapter, "_unresolved_threads", lambda pr: threads)
        monkeypatch.setattr(github_api_mod, "gh_current_login", lambda: "automation-bot")

        blocking, minor, human = adapter.count_unresolved_threads_by_severity(42)

        assert blocking == 1
        assert minor == 1
        assert human == 1

    def test_count_unresolved_threads_by_severity_unmarked_is_blocking(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count_unresolved_threads_by_severity treats unmarked automation as blocking."""
        automation_thread_unmarked = {
            "body": "No marker",
            "author": "automation-bot",
        }

        threads = [automation_thread_unmarked]

        monkeypatch.setattr(adapter, "_unresolved_threads", lambda pr: threads)
        monkeypatch.setattr(github_api_mod, "gh_current_login", lambda: "automation-bot")

        blocking, minor, human = adapter.count_unresolved_threads_by_severity(42)

        assert blocking == 1
        assert minor == 0
        assert human == 0

    def test_resolve_automation_threads_skips_human(
        self, adapter: pg.PipelineGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_automation_threads skips human-owned threads."""
        automation_thread = {
            "id": "auto_thread_id",
            "author": "automation-bot",
        }
        human_thread = {
            "id": "human_thread_id",
            "author": "reviewer",
        }

        threads = [automation_thread, human_thread]
        resolved_ids = []

        def capture_resolve(thread_id: str, dry_run: bool = False) -> None:
            resolved_ids.append(thread_id)

        monkeypatch.setattr(adapter, "_unresolved_threads", lambda pr: threads)
        monkeypatch.setattr(github_api_mod, "gh_current_login", lambda: "automation-bot")
        monkeypatch.setattr(github_api_mod, "gh_pr_resolve_thread", capture_resolve)

        count = adapter.resolve_automation_threads(42)

        assert count == 1
        assert "auto_thread_id" in resolved_ids
        assert "human_thread_id" not in resolved_ids
