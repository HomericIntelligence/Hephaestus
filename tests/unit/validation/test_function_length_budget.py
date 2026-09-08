"""Enforce the source-line budget for package functions."""

from __future__ import annotations

import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "hephaestus"
FUNCTION_LINE_LIMIT = 90

# These are the functions that exceeded the limit before issue #2809.  Keep
# their current lengths as individual caps while the decomposition work is
# completed.  A new function must meet the common limit unless it is added to
# this map as a deliberate follow-up.
LEGACY_FUNCTION_CAPS: dict[str, int] = {
    "hephaestus/agents/pi_plugins.py::preflight_pi_environment": 97,
    "hephaestus/agents/runtime.py::_run_pi_command": 92,
    "hephaestus/automation/_implement_phase.py::_run_claude_impl_session": 95,
    "hephaestus/automation/_review_utils.py::build_automation_parser": 92,
    "hephaestus/automation/_review_utils.py::find_pr_for_issue": 119,
    "hephaestus/automation/ci_driver.py::_build_parser": 117,
    "hephaestus/automation/ci_driver.py::main": 105,
    "hephaestus/automation/claude_invoke.py::invoke_claude_with_session": 134,
    "hephaestus/automation/claude_invoke.py::_invoke_claude_once": 99,
    "hephaestus/automation/follow_up.py::run_follow_up_issues": 169,
    "hephaestus/automation/git_utils.py::push_head_to_branch": 96,
    "hephaestus/automation/git_utils.py::push_current_branch_with_lease_on_divergence": 111,
    "hephaestus/automation/github_api/graphql.py::pipeline_thread_snapshot_page_query": 96,
    "hephaestus/automation/github_api/issues.py::gh_issue_upsert_comment": 111,
    "hephaestus/automation/github_api/issues.py::gh_issue_upsert_owned_comment": 104,
    "hephaestus/automation/github_api/prs.py::gh_pr_create": 121,
    "hephaestus/automation/github_api/reviews.py::_edit_or_keep_comments": 106,
    "hephaestus/automation/github_api/reviews.py::gh_pr_review_post": 139,
    "hephaestus/automation/github_api/threads.py::_complete_thread_snapshot": 116,
    "hephaestus/automation/github_api/threads.py::read_once": 91,
    "hephaestus/automation/implementer.py::_build_parser": 125,
    "hephaestus/automation/implementer.py::main": 139,
    "hephaestus/automation/issue_timeline.py::plan_issue_timeline_compaction": 103,
    "hephaestus/automation/issue_waves.py::wave_entry_from_facts": 95,
    "hephaestus/automation/issue_waves.py::_plan_locked": 124,
    "hephaestus/automation/learn.py::run_learn": 100,
    "hephaestus/automation/loop_repo_manager.py::_rebase_main": 98,
    "hephaestus/automation/loop_runner.py::_build_pipeline_config": 108,
    "hephaestus/automation/loop_runner.py::main": 142,
    "hephaestus/automation/merge_authorization.py::resolve_merge_authorization": 147,
    "hephaestus/automation/pipeline/coordinator_runtime.py::_emit_observability_tick": 96,
    "hephaestus/automation/pipeline/coordinator_runtime.py::run": 94,
    "hephaestus/automation/pipeline/coordinator_runtime.py::_route": 125,
    "hephaestus/automation/pipeline/coordinator_runtime.py::_route_direct_scope_bootstrap": 91,
    "hephaestus/automation/pipeline/coordinator_sources.py::_drain_repo_issue_source": 110,
    "hephaestus/automation/pipeline/coordinator_sources.py::_seed_direct_pr_scope": 124,
    "hephaestus/automation/pipeline/git_cleanup.py::run_cleanup_job": 116,
    "hephaestus/automation/pipeline/plan_journal.py::publish_plan_revision": 161,
    "hephaestus/automation/pipeline/reply_handoff.py::"
    "retry_pending_implementation_reply_handoff": 131,
    "hephaestus/automation/pipeline/seeding.py::seed_from_cli": 117,
    "hephaestus/automation/pipeline/stages/finished.py::_cleanup": 123,
    "hephaestus/automation/pipeline/stages/implementation.py::_dirty_decision_wait": 95,
    "hephaestus/automation/pipeline/stages/implementation.py::_implement_wait": 159,
    "hephaestus/automation/pipeline/stages/implementation.py::on_job_done": 112,
    "hephaestus/automation/pipeline/stages/implementation.py::_on_worktree_done": 107,
    "hephaestus/automation/pipeline/stages/implementation.py::_create_pr": 114,
    "hephaestus/automation/pipeline/stages/plan_review.py::on_enter": 109,
    "hephaestus/automation/pipeline/stages/planning.py::_refresh_requirements_recovery_context": 91,
    "hephaestus/automation/pipeline/stages/planning.py::_resume_wave_non_code_intent": 105,
    "hephaestus/automation/pipeline/stages/planning.py::_requirements_recovery_step": 99,
    "hephaestus/automation/pipeline/stages/planning.py::on_enter": 247,
    "hephaestus/automation/pipeline/stages/pr_review_jobs.py::_validate_wait": 98,
    "hephaestus/automation/pipeline/stages/pr_review_jobs.py::on_job_done": 100,
    "hephaestus/automation/pipeline/stages/pr_review_jobs.py::_post": 98,
    "hephaestus/automation/pipeline/stages/repo.py::_wave_admit_after_verify": 96,
    "hephaestus/automation/pipeline/summary.py::print_summary": 127,
    "hephaestus/automation/pipeline/worker_pool.py::_prepare_immutable_git_metadata": 91,
    "hephaestus/automation/pipeline/worker_pool.py::_run": 91,
    "hephaestus/automation/pipeline/worker_pool.py::_persist_evidence_receipt": 103,
    "hephaestus/automation/pipeline/worker_pool.py::_run_immutable_build_test": 109,
    "hephaestus/automation/pipeline/worker_pool.py::_git_rebase": 114,
    "hephaestus/automation/pipeline/worker_pool.py::_fast_forward_checkout": 107,
    "hephaestus/automation/pipeline/worker_pool.py::_finalize_created_worktree": 103,
    "hephaestus/automation/pipeline/worker_pool.py::_git_verify_pr_review_checkout": 102,
    "hephaestus/automation/pipeline_github_comments.py::_upsert_issue_comment": 92,
    "hephaestus/automation/pipeline_github_jobs.py::_reconcile_pr_review": 130,
    "hephaestus/automation/pipeline_github_jobs.py::_run_merge_wait_cycle": 248,
    "hephaestus/automation/pipeline_github_queries.py::_repo_unresolved_threads": 107,
    "hephaestus/automation/pipeline_github_reviews.py::"
    "_repo_review_thread_receipts_for_review": 100,
    "hephaestus/automation/pipeline_github_reviews.py::reconcile_reviewer_validated_threads": 214,
    "hephaestus/automation/plan_reviewer.py::_review_issue": 117,
    "hephaestus/automation/plan_reviewer.py::_run_claude_analysis": 96,
    "hephaestus/automation/planner.py::_build_parser": 100,
    "hephaestus/automation/planner.py::main": 124,
    "hephaestus/automation/post_merge_processor.py::run_drive_green_learnings": 92,
    "hephaestus/automation/pr_manager.py::ensure_pr_created": 123,
    "hephaestus/automation/pr_review_core.py::run_pr_review_analysis": 134,
    "hephaestus/automation/worktree_manager.py::create_worktree": 179,
    "hephaestus/automation/worktree_manager.py::_add_worktree_for_branch": 98,
    "hephaestus/benchmarks/compare.py::format_markdown_report": 93,
    "hephaestus/ci/workflows.py::validate_workflow_checkout_main": 113,
    "hephaestus/forensics/gdb_runner.py::run_under_gdb": 105,
    "hephaestus/github/client.py::_gh_call_impl": 112,
    "hephaestus/github/fleet_sync/cli.py::_build_parser": 97,
    "hephaestus/github/fleet_sync/conflict_resolver.py::resolve_conflict_with_agent": 93,
    "hephaestus/nats/subscriber.py::_subscribe_loop": 115,
    "hephaestus/utils/retry.py::retry_with_backoff": 111,
    "hephaestus/validation/complexity.py::run_ruff_complexity_check": 110,
    "hephaestus/validation/schema.py::main": 119,
}


def _function_records() -> dict[str, int]:
    """Return source-line counts for every function in the package."""
    records: dict[str, int] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                key = f"{relative_path}::{node.name}"
                records[key] = node.end_lineno - node.lineno + 1
    return records


def test_function_lengths_stay_within_budget() -> None:
    """Reject new over-long functions and growth in legacy functions."""
    records = _function_records()
    assert set(LEGACY_FUNCTION_CAPS) <= set(records), "legacy budget contains stale entries"
    failures = {
        key: (length, LEGACY_FUNCTION_CAPS.get(key, FUNCTION_LINE_LIMIT))
        for key, length in records.items()
        if length > LEGACY_FUNCTION_CAPS.get(key, FUNCTION_LINE_LIMIT)
    }
    assert not failures, f"functions exceed their line budgets: {failures}"
