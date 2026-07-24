"""RepoStage tests: label ensure, clone, discovery walk, epic-tag order (#1817).

Doc section "1. repo" is the binding contract: ENTER -> CLONE_WAIT ->
DISCOVER -> SEEDED; budget clone=2; epics tagged ``state:skip`` [durable]
BEFORE exclusion; orphan PRs without a linked issue are skipped; the repo item
is terminal (FINISH_PASS ``seeded:N``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.loop_repo_manager as loop_repo_manager_mod
import hephaestus.automation.pr_discovery as pr_discovery_mod
from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.stages.base import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.repo import RepoStage, product_to_work_item
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

from .conftest import FakeStageGitHub


class _RepoPaths:
    """Paths stub exposing a projects root and an optional explicit checkout."""

    def __init__(self, projects_dir: Path, *, repo_root: Path | None = None) -> None:
        self.projects_dir = projects_dir
        self.repo_root = repo_root or projects_dir
        self.worktree = self.repo_root


@pytest.fixture
def repo_item() -> WorkItem:
    """Fresh repo-kind work item at ENTER."""
    return WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO, state="ENTER")


@pytest.fixture
def repo_ctx(tmp_path: Path, make_ctx: Callable[..., Any]) -> Any:
    """StageContext whose paths expose a temp projects_dir."""
    return make_ctx(paths=_RepoPaths(tmp_path))


def _facts(
    number: int,
    *,
    title: str | None = None,
    body: str = "",
    labels: set[str] | None = None,
    pr: int | None = None,
    pr_open: bool = False,
    pr_merged: bool = False,
) -> IssueFacts:
    return IssueFacts(
        number=number,
        title=title or f"task {number}",
        is_epic=False,
        labels=labels or set(),
        pr_number=pr,
        pr_is_open=pr_open,
        pr_is_merged=pr_merged,
        body=body,
    )


class TestOnEnterAndCloneStates:
    """Steps 1-2: ensure_state_labels [M], clone [W:G] with budget 2."""

    def test_on_enter_ensures_state_labels(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        stage = RepoStage()

        assert stage.on_enter(repo_item, repo_ctx) is None

        assert ("ensure_state_labels", ()) in repo_ctx.github.mutation_log

    def test_enter_state_advances_to_clone_wait(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "CLONE_WAIT"

    def test_clone_wait_submits_git_clone_job(
        self, repo_item: WorkItem, repo_ctx: Any, tmp_path: Path
    ) -> None:
        """A missing checkout submits GitJob(op="clone") with repo/dest kwargs."""
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "clone"
        assert result.job.kwargs == {
            "repo": "test-org/repo-a",
            "dest": str(tmp_path / "repo-a"),
        }
        assert result.on_done_state == "DISCOVER"

    def test_existing_checkout_submits_sync_job_before_discovery(
        self, repo_item: WorkItem, repo_ctx: Any, tmp_path: Path
    ) -> None:
        """A pre-existing checkout is synchronized before its issues are read."""
        (tmp_path / "repo-a").mkdir()
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "sync_checkout"
        assert result.job.kwargs == {
            "repo": "test-org/repo-a",
            "dest": str(tmp_path / "repo-a"),
        }
        assert result.on_done_state == "DISCOVER"

    def test_explicit_existing_repo_root_submits_sync_job(
        self, repo_item: WorkItem, tmp_path: Path, make_ctx: Callable[..., Any]
    ) -> None:
        """An isolated existing checkout is synchronized at its explicit path."""
        projects_dir = tmp_path / "projects"
        checkout = tmp_path / "isolated" / "repo-a-worktree"
        checkout.mkdir(parents=True)
        ctx = make_ctx(paths=_RepoPaths(projects_dir, repo_root=checkout))
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "sync_checkout"
        assert result.job.kwargs == {
            "repo": "test-org/repo-a",
            "dest": str(checkout),
        }

    def test_clone_skipped_in_dry_run(
        self, repo_item: WorkItem, tmp_path: Path, make_ctx: Callable[..., Any]
    ) -> None:
        """[dry-run] logs the would-clone and proceeds — no job submitted."""
        ctx = make_ctx(dry_run=True, paths=_RepoPaths(tmp_path))
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "DISCOVER"

    def test_existing_checkout_is_not_synchronized_in_dry_run(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dry runs report a reusable checkout sync without submitting a GitJob."""
        (tmp_path / "repo-a").mkdir()
        ctx = make_ctx(dry_run=True, paths=_RepoPaths(tmp_path))
        repo_item.state = "CLONE_WAIT"
        caplog.set_level(logging.INFO, logger="hephaestus.automation.pipeline.stages.repo")

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "DISCOVER"
        assert "[dry-run] would synchronize test-org/repo-a" in caplog.text

    def test_clone_failure_retries_within_budget(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        """First failure (attempt 1 < budget 2) re-submits the clone."""
        stage = RepoStage()
        repo_item.state = "CLONE_WAIT"
        stage.on_job_done(repo_item, JobResult(ok=False, error="network"), repo_ctx)
        assert repo_item.attempts["clone"] == 1

        result = stage.step(repo_item, repo_ctx)

        assert isinstance(result, JobRequest)  # retry
        assert isinstance(result.job, GitJob) and result.job.op == "clone"

    def test_clone_exhaustion_finishes_failed(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        """Budget clone=2 exhausted -> finished(fail) per the ROUTES row."""
        stage = RepoStage()
        repo_item.state = "CLONE_WAIT"
        for _ in range(2):
            stage.on_job_done(repo_item, JobResult(ok=False, error="network"), repo_ctx)

        result = stage.step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert "clone exhausted" in result.note

    def test_clone_success_records_nothing(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        stage = RepoStage()
        repo_item.state = "CLONE_WAIT"

        stage.on_job_done(repo_item, JobResult(ok=True), repo_ctx)

        assert repo_item.attempts["clone"] == 0
        assert "clone_failed" not in repo_item.payload


class TestDiscover:
    """Step 3 [M]: list, dedup, epic-tag-before-exclude, classify, orphans."""

    def _patch_discovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        meta: list[dict[str, Any]],
        facts: dict[int, IssueFacts],
        classifications: dict[int, tuple[StageName | None, str]],
        open_prs: list[dict[str, Any]] | None = None,
    ) -> list[int]:
        """Patch the repo-stage read seams; returns the classify-call order."""
        classified: list[int] = []
        monkeypatch.setattr(loop_repo_manager_mod, "_list_open_issue_meta", lambda org, repo: meta)
        monkeypatch.setattr(
            loop_repo_manager_mod, "_list_open_pr_meta", lambda org, repo: open_prs or []
        )
        monkeypatch.setattr(seeding_mod, "seed_issue", lambda num: facts[num])
        monkeypatch.setattr(seeding_mod, "seed_issue_from_github", lambda num, github: facts[num])

        def fake_classify(f: IssueFacts) -> tuple[StageName | None, str]:
            classified.append(f.number)
            return classifications[f.number]

        monkeypatch.setattr(seeding_mod, "classify_issue", fake_classify)
        return classified

    def test_discover_classifies_through_repo_scoped_github(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repo discovery must not fall back to current-repo seeding helpers."""

        class RepoScopedGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(open_pr=44)
                self.issue_reads: list[int] = []

            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                self.issue_reads.append(issue_number)
                return {
                    "number": issue_number,
                    "title": "repo-specific task",
                    "state": "OPEN",
                    "body": "",
                    "labels": [{"name": "state:implementation-go"}],
                }

            def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
                return None

        github = RepoScopedGitHub()
        ctx = make_ctx(github=github, paths=_RepoPaths(tmp_path))
        monkeypatch.setattr(
            loop_repo_manager_mod,
            "_list_open_issue_meta",
            lambda org, repo: [{"number": 8, "labels": ["state:implementation-go"], "title": "x"}],
        )
        monkeypatch.setattr(loop_repo_manager_mod, "_list_open_pr_meta", lambda org, repo: [])
        monkeypatch.setattr(
            seeding_mod,
            "seed_issue",
            lambda num: (_ for _ in ()).throw(AssertionError("used current-repo seed_issue")),
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, Continue)
        assert github.issue_reads == [8]
        # Issue-level implementation-go on an open PR is a legacy compatibility
        # label (#2140): post-#2280 it routes back to review, not merge_wait.
        assert repo_item.payload["products"][0]["stage"] is StageName.PR_REVIEW
        assert repo_item.payload["products"][0]["pr"] == 44

    def test_discover_failure_finishes_fail(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discovery failures are not converted into a successful empty run."""
        monkeypatch.setattr(
            loop_repo_manager_mod,
            "_list_open_issue_meta",
            lambda org, repo: (_ for _ in ()).throw(RuntimeError("gh failed")),
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert "discovery failed" in result.note

    def test_discover_classifies_dedups_and_stages_products(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N issues (with a duplicate) classify into entry-queue products."""
        meta = [
            {"number": 1, "labels": [], "title": "one"},
            {"number": 2, "labels": ["state:plan-go"], "title": "two"},
            {"number": 1, "labels": [], "title": "one (dup)"},  # deduped
        ]
        classified = self._patch_discovery(
            monkeypatch,
            meta=meta,
            facts={1: _facts(1), 2: _facts(2, labels={"state:plan-go"})},
            classifications={
                1: (StageName.PLANNING, "needs plan"),
                2: (StageName.IMPLEMENTATION, "plan approved"),
            },
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, Continue) and result.next_state == "SEEDED"
        assert classified == [1, 2]  # dedup: issue 1 classified once
        products = repo_item.payload["products"]
        stages = {p["number"]: p["stage"] for p in products}
        assert stages == {1: StageName.PLANNING, 2: StageName.IMPLEMENTATION}
        assert repo_item.payload["seeded_count"] == 2

    def test_epics_tagged_durably_before_exclusion(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The skip_epics write lands BEFORE the epic is excluded (order test)."""
        meta = [
            {"number": 5, "labels": ["epic"], "title": "Epic: umbrella"},
            {"number": 6, "labels": [], "title": "real work"},
        ]
        classified = self._patch_discovery(
            monkeypatch,
            meta=meta,
            facts={6: _facts(6)},
            classifications={6: (StageName.PLANNING, "needs plan")},
        )
        gh: FakeStageGitHub = repo_ctx.github
        repo_item.state = "DISCOVER"

        RepoStage().step(repo_item, repo_ctx)

        # Durable tag written through the sanctioned chokepoint...
        assert ("skip_epics", ((5,),)) in gh.mutation_log
        assert "state:skip" in gh.labels[5]
        # ...BEFORE the exclusion was materialized: the epic never reached
        # the classifier, and its product records the exclusion.
        assert classified == [6]
        epic_products = [p for p in repo_item.payload["products"] if p["number"] == 5]
        assert epic_products[0]["stage"] is None

    def test_skip_epics_failure_blocks_exclusion_until_reseed(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed skip write leaves no excluded product; a reseed retries it."""
        meta = [
            {"number": 5, "labels": ["epic"], "title": "Epic: umbrella"},
            {"number": 6, "labels": [], "title": "real work"},
        ]
        classified = self._patch_discovery(
            monkeypatch,
            meta=meta,
            facts={6: _facts(6)},
            classifications={6: (StageName.PLANNING, "needs plan")},
        )
        gh: FakeStageGitHub = repo_ctx.github
        original_skip_epics = gh.skip_epics
        monkeypatch.setattr(
            gh,
            "skip_epics",
            lambda _epics_labels: (_ for _ in ()).throw(RuntimeError("label write failed")),
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert "products" not in repo_item.payload
        assert classified == []
        assert 5 not in gh.labels

        monkeypatch.setattr(gh, "skip_epics", original_skip_epics)
        reseed_item = WorkItem(
            repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO, state="DISCOVER"
        )

        reseed = RepoStage().step(reseed_item, repo_ctx)

        assert isinstance(reseed, Continue) and reseed.next_state == "SEEDED"
        assert "state:skip" in gh.labels[5]
        assert [p["number"] for p in reseed_item.payload["products"]] == [5, 6]

    @pytest.mark.parametrize(
        ("include_bot_prs", "include_all_authors", "expected"),
        [
            pytest.param(True, False, [], id="viewer-only-including-bots"),
            pytest.param(False, False, [], id="viewer-only-without-bots"),
            pytest.param(True, True, [], id="all-authors-including-bots"),
            pytest.param(False, True, [], id="all-authors-without-bots"),
        ],
    )
    def test_drive_green_all_honors_author_and_bot_filters(
        self,
        include_bot_prs: bool,
        include_all_authors: bool,
        expected: list[int],
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = type(
            "Cfg",
            (),
            {
                "drive_green_all": True,
                "include_bot_prs": include_bot_prs,
                "include_all_authors": include_all_authors,
                "dry_run": False,
            },
        )()
        ctx = make_ctx(config=config, paths=_RepoPaths(tmp_path))
        self._patch_discovery(
            monkeypatch,
            meta=[{"number": 1, "labels": [], "title": "covered"}],
            facts={1: _facts(1)},
            classifications={1: (StageName.PLANNING, "needs plan")},
            open_prs=[
                {"number": 66, "user": {"login": "alice", "type": "User"}},
                {"number": 67, "user": {"login": "bob", "type": "User"}},
                {"number": 68, "user": {"login": "alice", "type": "Bot"}},
                {"number": 69, "user": {"login": "depbot", "type": "Bot"}},
            ],
        )
        monkeypatch.setattr(
            pr_discovery_mod,
            "_resolve_viewer_login",
            lambda: "alice",
        )
        repo_item.state = "DISCOVER"

        RepoStage().step(repo_item, ctx)

        orphan = [p for p in repo_item.payload["products"] if p.get("kind") == "pr"]
        assert [p["number"] for p in orphan] == expected

    def test_drive_green_all_skips_viewer_resolution_for_all_authors(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = type(
            "Cfg",
            (),
            {"drive_green_all": True, "include_all_authors": True, "dry_run": False},
        )()
        ctx = make_ctx(config=config, paths=_RepoPaths(tmp_path))
        self._patch_discovery(
            monkeypatch,
            meta=[],
            facts={},
            classifications={},
            open_prs=[],
        )
        resolver = MagicMock(return_value="x")
        monkeypatch.setattr(pr_discovery_mod, "_resolve_viewer_login", resolver)
        repo_item.state = "DISCOVER"

        RepoStage().step(repo_item, ctx)

        resolver.assert_not_called()

    def test_drive_green_all_pr_discovery_failure_finishes_fail(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Orphan-PR discovery failures are visible, not successful empty runs."""
        config = type(
            "Cfg",
            (),
            {"drive_green_all": True, "include_all_authors": True, "dry_run": False},
        )()
        ctx = make_ctx(config=config, paths=_RepoPaths(tmp_path))
        self._patch_discovery(
            monkeypatch,
            meta=[{"number": 1, "labels": [], "title": "covered"}],
            facts={1: _facts(1, pr=55, pr_open=True)},
            classifications={1: (StageName.PR_REVIEW, "open PR")},
        )
        monkeypatch.setattr(
            loop_repo_manager_mod,
            "_list_open_pr_meta",
            lambda org, repo: (_ for _ in ()).throw(RuntimeError("pr list failed")),
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert "discovery failed" in result.note

    def test_seeded_finishes_pass_with_cached_count(
        self, repo_item: WorkItem, repo_ctx: Any
    ) -> None:
        """Terminal: FINISH_PASS(seeded:N) uses the cached discovery count."""
        repo_item.state = "SEEDED"
        repo_item.payload["products"] = [
            {"number": 1, "stage": StageName.PLANNING, "reason": "r"},
            {"number": 2, "stage": None, "reason": "excluded"},
            {"number": 3, "stage": StageName.PR_REVIEW, "reason": "r"},
        ]
        repo_item.payload["seeded_count"] = 1

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_PASS
        assert result.note == "seeded:1"

    def test_unknown_state_finishes_failed(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        repo_item.state = "BOGUS"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL


class TestProductToWorkItem:
    """Coordinator-side product materialization."""

    def test_issue_product(self) -> None:
        item = product_to_work_item(
            "repo-a",
            {
                "kind": "issue",
                "number": 9,
                "stage": StageName.PLANNING,
                "reason": "r",
                "labels": ["state:needs-plan"],
            },
        )

        assert item is not None
        assert item.kind is ItemKind.ISSUE and item.issue == 9 and item.pr is None
        assert item.stage is StageName.PLANNING and item.state == "ENTER"
        assert item.labels_cache == {"state:needs-plan": True}
        assert item.payload["entry_stage"] == "planning"

    def test_issue_product_hydrates_issue_context_payload(self) -> None:
        item = product_to_work_item(
            "repo-a",
            {
                "kind": "issue",
                "number": 9,
                "stage": StageName.PLANNING,
                "reason": "r",
                "labels": ["state:needs-plan"],
                "title": "Repo-discovered task",
                "body": "Repo-discovered body.",
            },
        )

        assert item is not None
        assert item.payload["issue_title"] == "Repo-discovered task"
        assert item.payload["issue_body"] == "Repo-discovered body."

    def test_issue_product_with_open_pr(self) -> None:
        item = product_to_work_item(
            "repo-a",
            {"kind": "issue", "number": 9, "pr": 77, "stage": StageName.PR_REVIEW, "reason": "r"},
        )

        assert item is not None
        assert item.issue == 9 and item.pr == 77

    def test_pr_product(self) -> None:
        item = product_to_work_item(
            "repo-a", {"kind": "pr", "number": 66, "stage": StageName.PR_REVIEW, "reason": "orphan"}
        )

        assert item is not None
        assert item.kind is ItemKind.PR and item.pr == 66 and item.issue is None

    def test_excluded_product_returns_none(self) -> None:
        assert product_to_work_item("repo-a", {"kind": "issue", "number": 5, "stage": None}) is None
