"""Keep publication authority separate from coordinator overlap reservations."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.stages import JobRequest
from hephaestus.automation.pipeline.stages.implementation import (
    _capture_codex_publication_scope,
    _codex_publication_kwargs,
    _remediation_prepare_request,
)
from hephaestus.automation.review_journal import PlanDiscoveryResult


def test_coordinator_claims_do_not_authorize_publication(
    make_ctx: Callable[..., Any], make_work_item: Callable[..., Any]
) -> None:
    """Accept plan paths and reject paths from the review diff inventory."""
    ctx = make_ctx(
        config_overrides={
            "agent": "codex",
            "codex_isolation_adapter": "test-adapter",
            "codex_isolation_deployment_lock": Path("/deployment/lock.json"),
            "codex_isolation_deployment_lock_sha256": "a" * 64,
        }
    )
    repo = (ctx.org, "test-repo")
    item = make_work_item(
        payload={
            "_implementation_file_claims": {
                (repo, "hephaestus/example.py"),
                (repo, "docs/unrelated.md"),
            },
            "review_changed_paths": ["docs/unrelated.md"],
        }
    )
    plan = "## Files to Modify\n- `hephaestus/example.py`\n- `pyproject.toml`\n"
    with patch.object(ctx.github, "discover_plan", return_value=PlanDiscoveryResult.found(plan)):
        assert _capture_codex_publication_scope(item, ctx) is None

    scope = _codex_publication_kwargs(item, ctx, "b" * 40)
    assert isinstance(scope, dict)
    assert scope["allowed_paths"] == ("hephaestus/example.py", "pyproject.toml")

    item.payload["_implementation_file_claims"].add((repo, "docs/later.md"))
    assert _codex_publication_kwargs(item, ctx, "b" * 40) == scope

    item.pr = 10
    item.branch = "codex/test"
    item.worktree = "/worktree"
    diff = "diff --git a/pyproject.toml b/pyproject.toml\n"
    item.payload.update(
        {
            "issue_title": "A task",
            "issue_body": "Update the planned files.",
            "_impl_source_revision": "b" * 40,
            "scope_retraction_paths": ("docs/unrelated.md",),
            "reviewed_pr_base_sha": "c" * 40,
            "remediation_thread_snapshots": [{"id": "thread-1"}],
            "remediation_writer_inspection": {
                "head_sha": "b" * 40,
                "content_snapshot": {
                    "index_sha256": "1" * 64,
                    "worktree_sha256": "2" * 64,
                    "untracked_sha256": "3" * 64,
                },
                "candidate_tree_sha": "c" * 40,
                "candidate_add_paths": ["pyproject.toml"],
                "candidate_update_paths": [],
                "diff": diff,
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            },
        }
    )
    prepared = _remediation_prepare_request(item, ctx)
    assert isinstance(prepared, JobRequest)
    assert isinstance(prepared.job, GitJob)
    assert prepared.job.kwargs["allowed_paths"] == scope["allowed_paths"]
    assert prepared.job.kwargs["scope_history_base_sha"] == "b" * 40
    assert prepared.job.kwargs["scope_retraction_paths"] == ("docs/unrelated.md",)
    assert prepared.job.kwargs["scope_retraction_base_sha"] == "c" * 40
