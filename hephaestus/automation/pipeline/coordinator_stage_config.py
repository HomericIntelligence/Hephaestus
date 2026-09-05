"""Stage-facing configuration DTO kept separate from coordinator ownership types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .stages.implementation import PRE_PR_TEST_ARGV


@dataclass
class _StageRunConfig:
    """PlannerOptions-like config injected as ``StageContext.config``."""

    enable_advise: bool = True
    enable_learn: bool = True
    enable_follow_up: bool = True
    run_pre_pr_tests: bool = False
    force: bool = False
    agent: str = "claude"
    disable_pi_automation: bool = False
    auth_status_timeout: int = 10
    pi_isolation_adapter: str | None = None
    pi_dir: Path | None = None
    model: str = ""
    planner_model: str = ""
    reviewer_model: str = ""
    implementer_model: str = ""
    fallback_model: str = ""
    rate_guard_enabled: bool = True
    rate_guard_threshold: int = 200
    planner_timeout: int = 1200
    reviewer_timeout: int = 1200
    implementer_timeout: int = 1800
    address_review_timeout: int = 7200
    git_message_timeout: int = 1200
    poll_max_wait: int = 1200
    clone_timeout: int = 120
    network_timeout: int = 120
    gh_timeout: int = 120
    metadata_timeout: int = 10
    rebase_timeout: int = 2400
    diff_collect_timeout: int = 60
    pre_pr_test_timeout: int | None = None
    dry_run: bool = False
    nitpick: bool = False
    drive_green_all: bool = False
    include_bot_prs: bool = True
    include_all_authors: bool = False
    pre_pr_test_argv: tuple[str, ...] = PRE_PR_TEST_ARGV
    issue_limit: int | None = None
    reset_plan_review_sessions: set[int] = field(default_factory=set)
