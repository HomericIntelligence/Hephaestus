"""Characterization tests for automation CLI parser option surfaces."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from hephaestus.automation import (
    address_review,
    audit_reviewer,
    ci_driver,
    ensure_state_labels,
    implementer,
    loop_runner,
    plan_reviewer,
    planner,
    pr_reviewer,
)
from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.cli.utils import DRY_RUN_HELP_CAVEAT
from hephaestus.config.paths import DEFAULT_PROJECTS_DIR

AGENT_CHOICES = ("claude", "codex", "pi")
WORKER_CHOICES = tuple(range(1, 33))
SUPPRESS_DEFAULT = "==SUPPRESS=="

AGENT_HELP = (
    "Agent backend to invoke for model-driven steps "
    "(default: auto-detect authenticated backend, preferring claude when authenticated)"
)
JSON_HELP = "Emit machine-readable JSON output instead of human-readable text"
NO_UI_HELP = "Disable curses UI (use plain logging instead)"
THROTTLE_RATE_HELP = (
    "Global gh token-bucket refill rate in calls/sec (default: 10.0). "
    "Pass 0 to disable the global throttle."
)
THROTTLE_BURST_HELP = "Global gh token-bucket burst size (default: 30.0)."
VERSION_HELP = "show program's version number and exit"


@dataclass(frozen=True)
class ActionSpec:
    """Stable subset of argparse action configuration relevant to CLI parity."""

    option_strings: tuple[str, ...]
    dest: str
    action: str
    default: Any
    required: bool
    nargs: Any
    choices: tuple[Any, ...] | None
    help: str | None


def _action_spec(
    option_strings: tuple[str, ...],
    dest: str,
    action: str,
    default: Any,
    required: bool = False,
    nargs: Any = None,
    choices: tuple[Any, ...] | None = None,
    help_text: str | None = None,
) -> ActionSpec:
    """Build an expected argparse action spec with readable call sites."""
    return ActionSpec(
        option_strings=option_strings,
        dest=dest,
        action=action,
        default=default,
        required=required,
        nargs=nargs,
        choices=choices,
        help=help_text,
    )


def _dry_help(prefix: str) -> str:
    """Return the exact canonical dry-run help produced by add_dry_run_arg."""
    return f"{prefix} {DRY_RUN_HELP_CAVEAT}"


def _agent_spec() -> ActionSpec:
    """Return the common --agent action spec."""
    return _action_spec(
        ("--agent",),
        "agent",
        "_StoreAction",
        None,
        choices=AGENT_CHOICES,
        help_text=AGENT_HELP,
    )


def _max_workers_spec(help_text: str) -> ActionSpec:
    """Return the common --max-workers action spec."""
    return _action_spec(
        ("--max-workers",),
        "max_workers",
        "_StoreAction",
        3,
        choices=WORKER_CHOICES,
        help_text=help_text,
    )


def _dry_run_spec(help_text: str) -> ActionSpec:
    """Return a --dry-run action spec."""
    return _action_spec(
        ("--dry-run",),
        "dry_run",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=help_text,
    )


def _verbose_spec(help_text: str) -> ActionSpec:
    """Return a -v/--verbose action spec."""
    return _action_spec(
        ("-v", "--verbose"),
        "verbose",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=help_text,
    )


def _no_ui_spec() -> ActionSpec:
    """Return the common --no-ui action spec."""
    return _action_spec(
        ("--no-ui",),
        "no_ui",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=NO_UI_HELP,
    )


def _github_throttle_specs() -> tuple[ActionSpec, ActionSpec]:
    """Return the GitHub global-throttle action specs."""
    return (
        _action_spec(
            ("--gh-global-rate",),
            "gh_global_rate",
            "_StoreAction",
            10.0,
            help_text=THROTTLE_RATE_HELP,
        ),
        _action_spec(
            ("--gh-global-burst",),
            "gh_global_burst",
            "_StoreAction",
            30.0,
            help_text=THROTTLE_BURST_HELP,
        ),
    )


def _json_spec() -> ActionSpec:
    """Return the common --json action spec."""
    return _action_spec(
        ("--json",),
        "json",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=JSON_HELP,
    )


def _version_spec() -> ActionSpec:
    """Return the common -V/--version action spec."""
    return _action_spec(
        ("-V", "--version"),
        "version",
        "_VersionAction",
        SUPPRESS_DEFAULT,
        nargs=0,
        help_text=VERSION_HELP,
    )


def _store_true(option: str, dest: str, help_text: str) -> ActionSpec:
    """Return a single-option store_true spec."""
    return _action_spec(
        (option,),
        dest,
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=help_text,
    )


def _timeout_spec(flag: str, dest: str, help_text: str) -> ActionSpec:
    """Return a timeout integer option spec (default=None)."""
    return _action_spec(
        (flag,),
        dest,
        "_StoreAction",
        None,
        help_text=help_text,
    )


def _specs(parser: argparse.ArgumentParser) -> tuple[ActionSpec, ...]:
    """Return comparable action specs for a parser, excluding argparse help."""
    return tuple(
        ActionSpec(
            option_strings=tuple(action.option_strings),
            dest=action.dest,
            action=type(action).__name__,
            default=SUPPRESS_DEFAULT if action.default is argparse.SUPPRESS else action.default,
            required=getattr(action, "required", False),
            nargs=action.nargs,
            choices=tuple(action.choices) if action.choices is not None else None,
            help=action.help,
        )
        for action in parser._actions
        if action.option_strings != ["-h", "--help"]
    )


def _sorted_specs(specs: tuple[ActionSpec, ...]) -> list[ActionSpec]:
    """Sort specs so tests assert parser surface without pinning help order."""
    return sorted(specs, key=lambda spec: spec.option_strings)


COMMON_REVIEW_MAX_WORKERS = "Maximum number of parallel workers, 1-32 (default: 3)"

EXPECTED_SPECS: dict[str, tuple[ActionSpec, ...]] = {
    "planner": (
        _action_spec(
            ("--issues",),
            "issues",
            "_StoreAction",
            None,
            nargs="+",
            help_text="Issue numbers to plan (default: all open issues)",
        ),
        _agent_spec(),
        _dry_run_spec(
            _dry_help("Suppress GitHub mutations and agent calls (classify + preview only).")
        ),
        _store_true(
            "--force",
            "force",
            "Force re-planning even when the issue is already at-or-past state:plan-go",
        ),
        _action_spec(
            ("--parallel",),
            "parallel",
            "_StoreAction",
            3,
            choices=WORKER_CHOICES,
            help_text="Number of parallel workers, 1-32 (maps to the pipeline worker pool)",
        ),
        _action_spec(
            ("--system-prompt",),
            "system_prompt",
            "_StoreAction",
            None,
            help_text="(Deprecated, ignored) system prompt file path; kept for CLI compatibility",
        ),
        _store_true(
            "--no-skip-closed",
            "no_skip_closed",
            "(Deprecated, ignored) kept for CLI compatibility; closed issues never queue",
        ),
        _store_true(
            "--no-advise",
            "no_advise",
            "Skip the advise step (don't search team knowledge base before planning)",
        ),
        _timeout_spec(
            "--agent-timeout",
            "agent_timeout",
            "Agent subprocess timeout in seconds (default: 7200).",
        ),
        _timeout_spec(
            "--advise-timeout",
            "advise_timeout",
            "Timeout for the advise sub-agent in seconds (default: 7200).",
        ),
        _timeout_spec(
            "--git-message-timeout",
            "git_message_timeout",
            "Timeout for the lightweight commit/PR message agent (default: 1200).",
        ),
        _verbose_spec("Enable verbose logging"),
        *_github_throttle_specs(),
        _json_spec(),
        _version_spec(),
    ),
    "plan_reviewer": (
        _action_spec(
            ("--issues",),
            "issues",
            "_StoreAction",
            None,
            required=True,
            nargs="+",
            help_text="Issue numbers whose plans should be reviewed",
        ),
        _agent_spec(),
        _max_workers_spec(COMMON_REVIEW_MAX_WORKERS),
        _dry_run_spec(_dry_help("Suppress GitHub mutations (no review comments posted).")),
        _no_ui_spec(),
        _timeout_spec(
            "--agent-timeout",
            "agent_timeout",
            "Agent subprocess timeout in seconds (default: 7200).",
        ),
        _verbose_spec("Enable verbose logging"),
        _json_spec(),
    ),
    "pr_reviewer": (
        _action_spec(
            ("--issues",),
            "issues",
            "_StoreAction",
            None,
            required=True,
            nargs="+",
            help_text="Issue numbers whose linked PRs should be reviewed",
        ),
        _agent_spec(),
        _max_workers_spec(COMMON_REVIEW_MAX_WORKERS),
        *_github_throttle_specs(),
        _dry_run_spec(
            _dry_help("Show what would be done without actually posting any review comments.")
        ),
        _no_ui_spec(),
        _timeout_spec(
            "--agent-timeout",
            "agent_timeout",
            "Agent subprocess timeout in seconds (default: 7200).",
        ),
        _verbose_spec("Enable verbose logging"),
        _json_spec(),
        _version_spec(),
    ),
    "address_review": (
        _action_spec(
            ("--issues",),
            "issues",
            "_StoreAction",
            None,
            required=True,
            nargs="+",
            help_text="Issue numbers whose linked PRs should have review threads addressed",
        ),
        _agent_spec(),
        _max_workers_spec(COMMON_REVIEW_MAX_WORKERS),
        *_github_throttle_specs(),
        _dry_run_spec(
            _dry_help("Show what would be done without actually resolving threads or pushing code.")
        ),
        _no_ui_spec(),
        _timeout_spec(
            "--agent-timeout",
            "agent_timeout",
            "Agent subprocess timeout in seconds (default: 7200).",
        ),
        _timeout_spec(
            "--advise-timeout",
            "advise_timeout",
            "Timeout for the advise sub-agent in seconds (default: 7200).",
        ),
        _verbose_spec("Enable verbose logging"),
        _json_spec(),
    ),
    "ci_driver": (
        _action_spec(
            ("--issues",),
            "issues",
            "_StoreAction",
            [],
            nargs="+",
            help_text=(
                "Scope to these issue numbers' PRs. Requires at least one issue number when given. "
                "Omit the flag entirely to drive every failing open PR discovered via gh "
                "(issue-linked PRs plus bot-authored PRs)."
            ),
        ),
        _action_spec(
            ("--prs",),
            "prs",
            "_StoreAction",
            [],
            nargs="*",
            help_text=(
                "PR numbers to drive directly, bypassing issue-to-PR discovery (#918). "
                "Use when the PR body uses 'Refs #N' or the PR is otherwise not reachable "
                "via the strict Closes-link lookup. May be combined with --issues; "
                "duplicate PRs are deduped."
            ),
        ),
        _agent_spec(),
        _max_workers_spec(COMMON_REVIEW_MAX_WORKERS),
        _dry_run_spec(
            _dry_help("Suppress GitHub writes and git pushes (no comments, no merges, no pushes).")
        ),
        _no_ui_spec(),
        _store_true("--no-advise", "no_advise", "Skip the advise step before CI fixing"),
        _verbose_spec("Enable verbose logging"),
        _action_spec(
            ("--no-include-bot-prs",),
            "include_bot_prs",
            "_StoreFalseAction",
            True,
            nargs=0,
            help_text=(
                "Suppress the union of open bot-authored PRs (Dependabot, github-actions, "
                "etc.) into the work set. By default the driver unions every open "
                "is_bot=true PR with the issue-driven list so Dependabot PRs are not "
                "architecturally invisible (#848). Pass this flag only when you explicitly "
                "want issue-driven scope."
            ),
        ),
        _store_true(
            "--all",
            "include_all_authors",
            "Include PRs opened by other actors (teammates, bots). Without this flag, "
            "only PRs authored by the authenticated viewer (`gh api user`) are driven "
            "(#821). NOTE: when scoped to issues (--issues N), the resolved PR is processed "
            "regardless of author — issue-scoped takes precedence.",
        ),
        _action_spec(
            ("--no-mechanical-rebase",),
            "enable_mechanical_rebase",
            "_StoreFalseAction",
            True,
            nargs=0,
            help_text=(
                "Disable the mechanical git rebase that runs before the CI-fix agent. "
                "By default a PR that is behind/conflicting with its base is rebased and "
                "pushed with no agent spend; only PRs whose rebase hits real conflicts "
                "fall through to the agent (#871). Pass this flag to require the agent "
                "for all behind/conflicting PRs."
            ),
        ),
        _action_spec(
            ("--max-fix-iterations",),
            "max_fix_iterations",
            "_StoreAction",
            1,
            help_text=(
                "Number of CI-fix attempts per failing PR before giving up (default: 1). "
                "The issue-major loop passes its --max-merge-attempts here so a PR that "
                "will not go green is abandoned after N tries."
            ),
        ),
        _timeout_spec(
            "--agent-timeout",
            "agent_timeout",
            "Agent subprocess timeout in seconds (default: 7200).",
        ),
        _timeout_spec(
            "--advise-timeout",
            "advise_timeout",
            "Timeout for the advise sub-agent in seconds (default: 7200).",
        ),
        _timeout_spec(
            "--learn-timeout",
            "learn_timeout",
            "Timeout for the /learn agent session (default: 7200).",
        ),
        _timeout_spec(
            "--poll-max-wait",
            "poll_max_wait",
            "Max wall-clock seconds to poll CI before backing off (default: 1200).",
        ),
        *_github_throttle_specs(),
        _json_spec(),
    ),
    "implementer": (
        _action_spec(
            ("--epic",),
            "epic",
            "_StoreAction",
            None,
            help_text="Epic issue number containing sub-issues",
        ),
        _action_spec(
            ("--issues",),
            "issues",
            "_StoreAction",
            None,
            nargs="+",
            help_text="Specific issue numbers to implement (alternative to --epic)",
        ),
        _agent_spec(),
        _store_true(
            "--analyze",
            "analyze",
            "(Deprecated, ignored) kept for CLI compatibility; analysis lives in the pipeline",
        ),
        _store_true(
            "--health-check",
            "health_check",
            "Run health check of dependencies and environment",
        ),
        _store_true(
            "--resume",
            "resume",
            "(Deprecated, ignored) kept for CLI compatibility; the pipeline resumes from state",
        ),
        _max_workers_spec(COMMON_REVIEW_MAX_WORKERS),
        _store_true(
            "--no-skip-closed",
            "no_skip_closed",
            "Implement closed issues (default: skip closed issues)",
        ),
        _store_true(
            "--no-auto-merge",
            "no_auto_merge",
            "Don't enable auto-merge after implementation-review GO",
        ),
        _dry_run_spec(
            _dry_help("Suppress GitHub mutations and git pushes (no PR creation, no commits).")
        ),
        _store_true(
            "--no-learn",
            "no_learn",
            "Disable /learn after implementation (enabled by default)",
        ),
        _store_true(
            "--no-follow-up",
            "no_follow_up",
            "Disable automatic filing of follow-up issues (enabled by default)",
        ),
        _store_true("--no-advise", "no_advise", "Skip the advise step before implementation"),
        _store_true(
            "--nitpick",
            "nitpick",
            "Let the reviewer emit nitpick-severity comments (suppressed by default)",
        ),
        _timeout_spec(
            "--agent-timeout",
            "agent_timeout",
            "Agent subprocess timeout in seconds (default: 7200).",
        ),
        _timeout_spec(
            "--advise-timeout",
            "advise_timeout",
            "Timeout for the advise sub-agent in seconds (default: 7200).",
        ),
        _timeout_spec(
            "--git-message-timeout",
            "git_message_timeout",
            "Timeout for the lightweight commit/PR message agent (default: 1200).",
        ),
        _timeout_spec(
            "--learn-timeout",
            "learn_timeout",
            "Timeout for the /learn agent session (default: 7200).",
        ),
        _timeout_spec(
            "--follow-up-timeout",
            "follow_up_timeout",
            "Timeout for the follow-up-issue agent session (default: 7200).",
        ),
        _no_ui_spec(),
        _verbose_spec("Enable verbose logging"),
        *_github_throttle_specs(),
        _json_spec(),
        _version_spec(),
    ),
    "loop_runner": (
        _dry_run_spec(
            _dry_help(
                "Forward --dry-run to every phase (suppresses GitHub mutations and git pushes)."
            )
        ),
        _action_spec(
            ("--loops",),
            "loops",
            "_StoreAction",
            5,
            help_text="Number of loop iterations (default: 5)",
        ),
        _max_workers_spec(
            "Parallel workers per repo per phase (1-32, default: 3). Passes to child phases."
        ),
        _action_spec(
            ("--max-merge-attempts",),
            "max_merge_attempts",
            "_StoreAction",
            1,
            help_text=(
                "Per-issue drive-green merge attempts before the issue is tagged "
                "state:skip and the worker moves on (default: 1, matching the prior "
                "drive-green retry budget)."
            ),
        ),
        _action_spec(
            ("--parallel-repos",),
            "parallel_repos",
            "_StoreAction",
            1,
            help_text="Repos processed in parallel per loop iteration (default: 1)",
        ),
        _action_spec(
            ("--phases",),
            "phases",
            "_StoreAction",
            "plan,implement,drive-green",
            help_text=(
                "Comma-separated subset of phases/stages to run. Valid: "
                "plan,implement,drive-green (plan/implement are loop-body phases; "
                "drive-green runs per issue when selected and also does one final "
                "repo-level catch-up sweep)."
            ),
        ),
        _agent_spec(),
        _action_spec(
            ("--issues",),
            "issues",
            "_StoreAction",
            None,
            help_text=(
                "Comma-separated issue numbers to pass to issue-scoped phases "
                "(plan, implement, drive-green). Default: phase auto-discovery."
            ),
        ),
        _store_true(
            "--no-advise",
            "no_advise",
            "Pass --no-advise to phases that support the advise preflight",
        ),
        _action_spec(
            ("--no-serialize-file-overlap",),
            "serialize_file_overlap",
            "_StoreFalseAction",
            True,
            nargs=0,
            help_text=(
                "Disable file-overlap serialization; dispatch all issues in a round"
                " concurrently even when their plans touch the same file (#1623)"
            ),
        ),
        _store_true(
            "--nitpick",
            "nitpick",
            "Pass --nitpick to review phases (reviewer emits nitpick comments)",
        ),
        _store_true(
            "--drive-green-all",
            "drive_green_all",
            "Pass --all to the drive-green phase: drive every open PR, including those "
            "opened by teammates and bots. By default drive-green operates only on PRs "
            "authored by the authenticated viewer (#821).",
        ),
        _store_true(
            "--allow-unsafe-phase-order",
            "allow_unsafe_phase_order",
            "Silence dependency-ordering warnings when --phases skips a recommended predecessor",
        ),
        _action_spec(
            ("--model",),
            "model",
            "_StoreAction",
            "",
            help_text=(
                "Model ID applied to every phase (planner, reviewer, implementer, advise) "
                "for child processes, so no HEPH_*_MODEL env vars are required. The /learn "
                "step inherits its parent phase's model automatically. A per-phase flag below "
                "overrides this for that phase."
            ),
        ),
        _action_spec(
            ("--planner-model",),
            "planner_model",
            "_StoreAction",
            "",
            help_text="HEPH_PLANNER_MODEL for child processes",
        ),
        _action_spec(
            ("--reviewer-model",),
            "reviewer_model",
            "_StoreAction",
            "",
            help_text="HEPH_REVIEWER_MODEL for child processes (plan-review + PR-review)",
        ),
        _action_spec(
            ("--implementer-model",),
            "implementer_model",
            "_StoreAction",
            "",
            help_text=(
                "HEPH_IMPLEMENTER_MODEL for child processes (implement, address-review, ci-driver)"
            ),
        ),
        _action_spec(
            ("--org",),
            "org",
            "_StoreAction",
            None,
            nargs="?",
            help_text=(
                "Enumerate non-fork, non-archived repos in a GitHub org. Pass `--org NAME` "
                "for a specific org, or `--org` alone to auto-detect the org from the "
                "current repo's git remote. Default (no flag): run only for the current repo."
            ),
        ),
        _action_spec(
            ("--projects-dir",),
            "projects_dir",
            "_StoreAction",
            None,
            help_text=(
                "Local directory containing repo clones. When omitted, resolved from the "
                "``PROJECTS_ROOT`` env var (if set and existing), otherwise the current "
                "checkout parent when available, then "
                f"``{DEFAULT_PROJECTS_DIR}``."
            ),
        ),
        _action_spec(
            ("--phase-timeout",),
            "phase_timeout",
            "_StoreAction",
            7800.0,
            help_text=(
                "Per-phase timeout in seconds (default: HEPH_PHASE_TIMEOUT or 7800s). "
                "Pass 0 or a negative value to disable. This bounds each AGENT JOB the "
                "pipeline runs, not a whole phase subprocess."
            ),
        ),
        _action_spec(
            ("--repos",),
            "repos",
            "_StoreAction",
            None,
            help_text=(
                "Comma-separated repo list (e.g. `--repos foo,bar`). Overrides org "
                "enumeration. Space-separated input is NOT accepted."
            ),
        ),
        *_github_throttle_specs(),
        _verbose_spec("Enable DEBUG logging"),
        _json_spec(),
        _version_spec(),
    ),
    "audit_reviewer": (
        _action_spec(
            ("--pr-numbers",),
            "pr_numbers",
            "_StoreAction",
            [],
            nargs="+",
            help_text="Audit only these PR numbers (default: all open).",
        ),
        _agent_spec(),
        _store_true("--codex", "codex", "Deprecated alias for --agent codex."),
        _dry_run_spec("Skip the agent call and the GitHub posting step."),
        _verbose_spec("DEBUG-level logging."),
        *_github_throttle_specs(),
        _json_spec(),
        _version_spec(),
    ),
    "ensure_state_labels": (
        _action_spec(
            ("--repo",),
            "repo",
            "_StoreAction",
            None,
            help_text="Single target repo (default: the current git checkout's origin).",
        ),
        _action_spec(
            ("--org",),
            "org",
            "_StoreAction",
            None,
            help_text="Apply to every non-archived, non-fork repo in the org.",
        ),
        _dry_run_spec("Print what would happen; mutate nothing."),
        _verbose_spec("Enable DEBUG logging."),
        *_github_throttle_specs(),
        _json_spec(),
        _version_spec(),
    ),
}


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("planner", planner._build_parser),
        ("plan_reviewer", plan_reviewer._build_parser),
        ("pr_reviewer", pr_reviewer._build_parser),
        ("address_review", address_review._build_parser),
        ("ci_driver", ci_driver._build_parser),
        ("implementer", implementer._build_parser),
        ("loop_runner", loop_runner._build_parser),
        ("audit_reviewer", audit_reviewer._build_parser),
        ("ensure_state_labels", ensure_state_labels._build_parser),
    ],
)
def test_parser_action_specs_are_preserved(
    name: str,
    factory: Callable[[], argparse.ArgumentParser],
) -> None:
    """Affected parsers keep their current action specs after common-helper extraction."""
    assert _sorted_specs(_specs(factory())) == _sorted_specs(EXPECTED_SPECS[name])


def test_build_automation_parser_does_not_add_throttle_by_default() -> None:
    """The shared helper does not expose GitHub throttle flags unless opted in."""
    flags = {
        flag for spec in _specs(build_automation_parser("demo")) for flag in spec.option_strings
    }

    assert "--gh-global-rate" not in flags
    assert "--gh-global-burst" not in flags


def test_plan_reviewer_still_has_no_throttle_or_version_flags() -> None:
    """Plan review keeps its historical absence of throttle and version flags."""
    flags = {flag for spec in _specs(plan_reviewer._build_parser()) for flag in spec.option_strings}

    assert "--gh-global-rate" not in flags
    assert "--gh-global-burst" not in flags
    assert "--version" not in flags
    assert "-V" not in flags
