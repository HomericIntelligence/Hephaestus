"""Tests for the shared rebase-GitJob builder (issue #1861)."""

from __future__ import annotations

from hephaestus.automation.pipeline.stages.base import (
    GIT_JOB_TIMEOUT_S,
    _build_rebase_job,
)


class TestBuildRebaseJob:
    """Tests for ``_build_rebase_job``, the shared rebase-onto-base builder."""

    def test_defaults_base_branch_to_main(self, make_ctx, make_work_item):
        ctx = make_ctx()
        item = make_work_item(repo="o/r")
        item.payload.pop("base_branch", None)
        job = _build_rebase_job(item, ctx, descr="x")
        assert job.op == "rebase"
        assert job.kwargs["base_branch"] == "main"
        assert job.timeout_s == GIT_JOB_TIMEOUT_S

    def test_uses_captured_base_branch(self, make_ctx, make_work_item):
        ctx = make_ctx()
        item = make_work_item(repo="o/r")
        item.payload["base_branch"] = "develop"
        job = _build_rebase_job(item, ctx, descr="x")
        assert job.kwargs["base_branch"] == "develop"

    def test_descr_passthrough(self, make_ctx, make_work_item):
        ctx = make_ctx()
        item = make_work_item(repo="o/r")
        job = _build_rebase_job(item, ctx, descr="resolve_dirty_rebase")
        assert job.descr == "resolve_dirty_rebase"

    def test_cwd_uses_item_worktree(self, make_ctx, make_work_item):
        ctx = make_ctx()
        item = make_work_item(repo="o/r")
        item.worktree = "/tmp/repo/item-worktree"
        job = _build_rebase_job(item, ctx, descr="x")
        assert str(job.kwargs["cwd"]) == "/tmp/repo/item-worktree"

    def test_repo_passthrough(self, make_ctx, make_work_item):
        ctx = make_ctx()
        item = make_work_item(repo="o/r")
        job = _build_rebase_job(item, ctx, descr="x")
        assert job.repo == "o/r"
