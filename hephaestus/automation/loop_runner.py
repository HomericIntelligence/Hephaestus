"""Multi-repo automation loop CLI — a thin wrapper over the queue pipeline.

This module is the ``hephaestus-automation-loop`` console-script entry point.
It has three responsibilities and nothing more:

1. **CLI parsing** — build the argparse parser (flag-compatible with the
   historical bash script so operator muscle memory and pinned callers keep
   working) and validate the selected phases.
2. **Scope + config construction** — resolve the ``(org, repos)`` scope from
   ``--org`` / ``--repos`` / cwd detection, then translate the parsed args and
   the derived :class:`LoopConfig` into a
   :class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig`.
3. **Dispatch** — run a repo-token preflight and hand off to
   :func:`hephaestus.automation.pipeline.coordinator.run_pipeline`.

All execution — repo cloning, issue seeding, admission control, and the
plan → implement → review → drive-green → merge-wait stage graph — lives in the
:mod:`hephaestus.automation.pipeline` package. This module owns no loop body,
no per-phase subprocess machinery, and no post-loop stage sequencing; the
legacy subprocess-per-phase path (the pre-pipeline rollback story) was removed
once the pipeline became the default automation-loop path (epic #1809, cutover
#1818, cleanup #1819).

``--phase-timeout`` bounds each agent job the pipeline runs. Repo discovery
helpers are re-exported from :mod:`hephaestus.automation.loop_repo_manager`
(#1360 / #1179).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.coordinator import PipelineConfig
    from hephaestus.automation.pipeline.routing import PipelineScope

from hephaestus.agents.runtime import resolve_agent
from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.automation.agent_config import (
    fallback_model as default_fallback_model,
    implementer_model as default_implementer_model,
    planner_model as default_planner_model,
    reviewer_model as default_reviewer_model,
)
from hephaestus.automation.event_log_retention import (
    DEFAULT_EVENT_LOG_RETENTION_COUNT,
    DEFAULT_EVENT_LOG_RETENTION_DAYS,
    event_log_lifecycle,
)
from hephaestus.automation.loop_repo_manager import (
    _clone_missing_repos as _clone_missing_repos,
    _detect_cwd_repo as _detect_cwd_repo,
    _gh_list_repos as _gh_list_repos,
    _iter_gh_repos as _iter_gh_repos,
    _resolve_repo_dir as _resolve_repo_dir,
    _sort_repos_by_open_count as _sort_repos_by_open_count,
)
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.cli.utils import (
    configure_cli_logging,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.config.paths import DEFAULT_PROJECTS_DIR, resolve_projects_dir
from hephaestus.github.client import gh_call
from hephaestus.utils.helpers import get_repo_root

LOG = logging.getLogger(__name__)


# The two non-blocking iteration phases. Plan-review, PR-review, and
# address-review fold into plan/implement (#455/#468/#484).
ALL_PHASES: tuple[str, ...] = (
    "plan",
    "implement",
)

# drive-green is the terminal blocking stage — selectable per issue, kept as a
# distinct tuple so ``--phases drive-green`` operator re-runs keep working.
ALL_POST_LOOP_STAGES: tuple[str, ...] = ("drive-green",)

# Per-phase sequence, in order: plan → implement → drive-green. Operators select
# any subset via --phases; unselected phases are skipped.
ALL_SELECTABLE: tuple[str, ...] = ALL_PHASES + ALL_POST_LOOP_STAGES

LOOP_DEFAULT_MAX_WORKERS = 6

# DEFAULT_PROJECTS_DIR is re-exported from hephaestus.config.paths so existing
# tests that patch this module-level name continue to work. See #704: the
# projects root is now resolved at runtime via resolve_projects_dir().

# Sentinel for ``--org`` invoked with no argument (auto-detect from cwd).
# Module-level identity guarantees ``args.org is _ORG_AUTODETECT`` is the
# unambiguous test for "user passed --org but gave no value".
_ORG_AUTODETECT = object()


def _parse_repo_list(value: str) -> list[str]:
    """Split a comma-separated repo list, stripping whitespace and empties.

    Example: ``"foo, bar,baz"`` → ``["foo", "bar", "baz"]``. Empty input
    returns an empty list, which the caller treats as "user didn't pass
    --repos".
    """
    return [s.strip() for s in value.split(",") if s.strip()]


def _parse_positive_int(value: str) -> int:
    """Parse one strictly positive integer for a bounded CLI setting."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {number}")
    return number


def _parse_non_negative_int(value: str) -> int:
    """Parse one non-negative integer for an optional retention limit."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {number}")
    return number


def _parse_gh_extra_path_root(value: str) -> Path:
    """Validate an explicit root whose only admitted executable is ``bin/gh``."""
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise argparse.ArgumentTypeError("--gh-extra-path-root must be an absolute path")
    try:
        resolved_root = root.resolve(strict=True)
        executable = (resolved_root / "bin" / "gh").resolve(strict=True)
    except OSError as exc:
        raise argparse.ArgumentTypeError(
            "--gh-extra-path-root must contain an executable bin/gh"
        ) from exc
    if (
        not resolved_root.is_dir()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
        or not executable.is_relative_to(resolved_root)
    ):
        raise argparse.ArgumentTypeError(
            "--gh-extra-path-root must contain an executable bin/gh without symlink escapes"
        )
    return resolved_root


def _parse_positive_int_list(value: str, label: str) -> list[int]:
    """Split a comma-separated list into positive integers."""
    numbers: list[int] = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            number = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"expected comma-separated {label} numbers, got {item!r}"
            ) from exc
        if number <= 0:
            raise argparse.ArgumentTypeError(
                f"{label} numbers must be positive integers, got {number}"
            )
        numbers.append(number)
    return numbers


def _parse_issue_list(value: str) -> list[int]:
    """Split a comma-separated issue list into positive integers."""
    return _parse_positive_int_list(value, "issue")


def _parse_pr_list(value: str) -> list[int]:
    """Split a comma-separated PR list into positive integers."""
    return _parse_positive_int_list(value, "PR")


def _parse_metrics_port(value: str) -> int:
    """Parse a TCP port while rejecting values outside the socket range."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"metrics port must be an integer, got {value!r}") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("metrics port must be in 0..65535")
    return port


def _default_phase_timeout_s() -> float:
    """Return the default per-agent-job timeout in seconds.

    An agent job that shells out to an external coding agent can stall
    indefinitely on a network hang; a non-``None`` default keeps every job
    bounded even when the operator does not pass ``--phase-timeout``.
    The 7800s default lets the outer job guard safely exceed the longest
    in-agent timeout (2h) so a healthy job never trips it.
    """
    return 7800.0


def _resolve_model_option(role_value: str, global_value: str, default: str) -> str:
    """Resolve model precedence once at the CLI boundary."""
    return role_value or global_value or default


@dataclass
class LoopConfig:
    """Top-level CLI-derived configuration.

    Carries the parsed scope/model/throttle knobs from :func:`main` into
    :func:`_build_pipeline_config`, which maps them onto the coordinator's
    :class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig`.
    """

    loops: int = 5
    # Optional exact per-cycle cap shared by plan review and implementation
    # review. None preserves the routing table's established 3/3/6 defaults.
    review_iterations: int | None = None
    max_workers: int = LOOP_DEFAULT_MAX_WORKERS
    parallel_repos: int = 1
    learning_workers: int = 1
    learning_queue_capacity: int = 1
    # Dataclass default covers ONLY the iteration phases (``ALL_PHASES`` =
    # plan, implement), deliberately excluding drive-green — a bare
    # ``LoopConfig()`` gets a quiet plan+implement run. The CLI ``--phases``
    # default is ``ALL_SELECTABLE`` (set in the parser), so an operator opts
    # into the blocking drive-green by default.
    phases: tuple[str, ...] = ALL_PHASES
    # Compatibility bound retained for the historical drive-green CLI. The
    # current merge-wait stage conditionally merges only a freshly admitted
    # reviewed head and never manages native auto-merge; the default remains 5
    # for stable CLI/config compatibility.
    drive_green_loops: int = 5
    # When True (default), never dispatch two issues whose plans touch the same
    # file concurrently — defer the later one (#1623).
    serialize_file_overlap: bool = True
    agent: str = "claude"
    disable_pi_automation: bool = False
    auth_status_timeout: int = 10
    pi_isolation_adapter: str | None = None
    pi_dir: Path | None = None
    issues: list[int] = field(default_factory=list)
    reset_plan_review_session: bool = False
    prs: list[int] = field(default_factory=list)
    dry_run: bool = False
    no_advise: bool = False
    no_learn: bool = False
    nitpick: bool = False
    # Retained CLI/config compatibility option. It does not expand the queue's
    # linked-issue repository discovery into an unrelated open-PR sweep.
    drive_green_all: bool = False
    run_pre_pr_tests: bool = False
    # ``model`` is the catch-all applied to every phase when set; per-phase
    # fields below take precedence over it.
    model: str = ""
    planner_model: str = ""
    reviewer_model: str = ""
    implementer_model: str = ""
    fallback_model: str = ""
    planner_reasoning_effort: str = ""
    reviewer_reasoning_effort: str = ""
    implementer_reasoning_effort: str = ""
    # Explicit, CLI-only extension to the system ``gh`` installation roots.
    # The parser admits only ``<root>/bin/gh`` and never consults an env var.
    gh_extra_path_root: Path | None = None
    gh_global_rate: float = 10.0
    gh_global_burst: float = 30.0
    rate_guard_enabled: bool = True
    rate_guard_threshold: int = 200
    plugin_skills_dir: Path | None = None
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
    pre_pr_test_timeout: int = 600
    # Org is resolved at runtime from --org / --repos / cwd detection; no
    # hardcoded fallback. Always set by main() before dispatch.
    org: str = ""
    projects_dir: Path = DEFAULT_PROJECTS_DIR
    # The loop can be launched from a checkout whose directory name does not
    # match its remote repository.  Keep that exceptional path explicit while
    # retaining ``projects_dir / repo`` as the normal multi-repo convention.
    repo_roots: dict[str, Path] = field(default_factory=dict)
    # Per-agent-job timeout in seconds. ``--phase-timeout`` overrides it and a
    # non-positive value disables the bound (``None``).
    phase_timeout_s: float | None = field(default_factory=_default_phase_timeout_s)
    # Prometheus text + JSON health endpoint. Zero deliberately disables the
    # listener rather than selecting an ephemeral port, so the CLI remains
    # opt-in and operators know which port is exposed.
    metrics_port: int = 0
    # The optional staged issue-wave selector. Explicit issue/PR lists remain
    # identifier scopes and are rejected together with this value at parsing.
    issue_limit: int | None = None
    event_log_retention_days: int = DEFAULT_EVENT_LOG_RETENTION_DAYS
    event_log_retention_count: int = DEFAULT_EVENT_LOG_RETENTION_COUNT


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the loop runner."""
    p = build_automation_parser(
        prog="hephaestus-automation-loop",
        description=("Run the queue-based automation pipeline across HomericIntelligence repos."),
        max_workers_help=(
            "Parallel workers per repo per phase (1-32, default: 6). Passes to child phases."
        ),
        max_workers_default=LOOP_DEFAULT_MAX_WORKERS,
        add_github_throttle=True,
        dry_run_prefix=(
            "Forward --dry-run to every phase (suppresses GitHub mutations and git pushes)."
        ),
        verbose_help="Enable DEBUG logging",
    )
    p.add_argument(
        "--loops",
        type=_parse_positive_int,
        default=5,
        help="Repository discovery reseed passes; does not change review budgets (default: 5)",
    )
    p.add_argument(
        "--reset-plan-review-session",
        action="store_true",
        help="Explicitly discard reviewer conversation state for the selected --issues",
    )
    p.add_argument(
        "--review-iterations",
        type=_parse_positive_int,
        default=None,
        metavar="N",
        help=(
            "Exact per-cycle cap for both plan-review and implementation-review rounds. "
            "Omit to preserve the routing defaults (plan 3; implementation soft 3/hard 6)."
        ),
    )
    p.add_argument(
        "--drive-green-loops",
        type=_parse_positive_int,
        default=5,
        help=(
            "Compatibility iteration bound for the historical drive-green CLI; current "
            "merge-wait conditionally merges reviewed heads and does not manage native auto-merge "
            "(default: 5; replaces --max-merge-attempts)."
        ),
    )
    p.add_argument(
        "--parallel-repos",
        type=_parse_positive_int,
        default=1,
        help="Repos processed in parallel per loop iteration (default: 1)",
    )
    p.add_argument(
        "--learning-workers",
        type=_parse_positive_int,
        default=1,
        help="Independent host-learning workers (default: 1)",
    )
    p.add_argument(
        "--learning-queue-capacity",
        type=_parse_positive_int,
        default=1,
        help="Bounded auxiliary learning queue capacity (default: 1)",
    )
    p.add_argument(
        "--issue-limit",
        type=_parse_positive_int,
        default=None,
        metavar="N",
        help=(
            "Run the next checkpointed issue wave with at most N eligible issues. "
            "The staged rollout advances 1, 2, 4, 8, then all eligible issues."
        ),
    )
    p.add_argument(
        "--phases",
        default=",".join(ALL_SELECTABLE),
        help=(
            "Comma-separated subset of phases/stages to run. "
            f"Valid: {','.join(ALL_SELECTABLE)} "
            "(plan/implement are loop-body phases; drive-green runs per issue "
            "when selected and also does one final repo-level catch-up sweep)."
        ),
    )
    p.add_argument(
        "--issues",
        type=_parse_issue_list,
        default=None,
        help=(
            "Comma-separated issue numbers to pass to issue-scoped phases "
            "(plan, implement, drive-green). Default: phase auto-discovery."
        ),
    )
    p.add_argument(
        "--prs",
        type=_parse_pr_list,
        default=None,
        help=(
            "Comma-separated PR numbers to seed directly into pipeline PR stages. "
            "Default: no direct PR scope."
        ),
    )
    p.add_argument(
        "--no-advise",
        action="store_true",
        help="Pass --no-advise to phases that support the advise preflight",
    )
    p.add_argument(
        "--no-learn",
        action="store_true",
        help="Do not create or execute auxiliary learning intents",
    )
    p.add_argument(
        "--no-serialize-file-overlap",
        action="store_false",
        dest="serialize_file_overlap",
        default=True,
        help=(
            "Disable file-overlap serialization; dispatch all issues in a round"
            " concurrently even when their plans touch the same file (#1623)"
        ),
    )
    p.add_argument(
        "--nitpick",
        action="store_true",
        help="Pass --nitpick to review phases (reviewer emits nitpick comments)",
    )
    p.add_argument(
        "--drive-green-all",
        action="store_true",
        help=(
            "Compatibility option for the retired broad drive-green sweep. "
            "Repository discovery remains linked-issue based and never scans "
            "unrelated open PRs; use --prs for an explicit PR scope."
        ),
    )
    p.add_argument(
        "--run-pre-pr-tests",
        action="store_true",
        help=(
            "Run the configurable pre-PR test gate for repositories without an automatic "
            "required-check profile; Hephaestus runs its required checks before initial "
            "PR creation."
        ),
    )
    p.add_argument(
        "--gh-extra-path-root",
        type=_parse_gh_extra_path_root,
        default=None,
        metavar="ROOT",
        help=(
            "Explicitly allow only ROOT/bin/gh in addition to system gh locations. "
            "ROOT must be absolute and contain an executable bin/gh that does not escape ROOT."
        ),
    )
    p.add_argument(
        "--model",
        default="",
        help=(
            "Model ID applied to every phase (planner, reviewer, implementer, advise) "
            "for child processes. The /learn "
            "step inherits its parent phase's model automatically. A per-phase flag below "
            "overrides this for that phase."
        ),
    )
    p.add_argument("--planner-model", default="", help="Model ID for planner child processes")
    reasoning_help = (
        "Explicit Codex reasoning effort for this role. Use default to omit "
        "model_reasoning_effort; when omitted, the selected model alias keeps its default."
    )
    p.add_argument(
        "--planner-reasoning-effort",
        choices=("default", "low", "medium", "high", "xhigh"),
        default="",
        help=reasoning_help,
    )
    p.add_argument(
        "--reviewer-model",
        default="",
        help=(
            "Model ID for reviewer child processes (plan-review + PR-review); "
            "use terra:default to select GPT-5.6 Terra without an explicit reasoning override"
        ),
    )
    p.add_argument(
        "--implementer-model",
        default="",
        help="Model ID for implementer child processes (implement, address-review, drive-green)",
    )
    p.add_argument("--fallback-model", default="", help="Claude quota fallback model ID")
    p.add_argument(
        "--reviewer-reasoning-effort",
        choices=("default", "low", "medium", "high", "xhigh"),
        default="",
        help=reasoning_help,
    )
    p.add_argument(
        "--implementer-reasoning-effort",
        choices=("default", "low", "medium", "high", "xhigh"),
        default="",
        help=reasoning_help,
    )
    p.add_argument(
        "--org",
        nargs="?",
        const=_ORG_AUTODETECT,
        default=None,
        help=(
            "Enumerate non-fork, non-archived repos in a GitHub org. "
            "Pass `--org NAME` for a specific org, or `--org` alone to auto-detect "
            "the org from the current repo's git remote. "
            "With --issues or --prs, also pass exactly one --repos REPO. "
            "Default (no flag): run only for the current repo."
        ),
    )
    p.add_argument(
        "--projects-dir",
        type=str,
        default=None,
        help=(
            "Local directory containing repo clones. When omitted, resolved from "
            "the current checkout parent when available, then "
            f"``{DEFAULT_PROJECTS_DIR}``."
        ),
    )
    p.add_argument(
        "--phase-timeout",
        type=float,
        default=_default_phase_timeout_s(),
        help=(
            f"Per-phase timeout in seconds (default: {int(_default_phase_timeout_s())}s). "
            "Pass 0 or a negative value to disable. "
            "This bounds each AGENT JOB the pipeline runs, not a whole phase subprocess."
        ),
    )
    p.add_argument(
        "--rate-guard",
        action="store_true",
        dest="rate_guard_enabled",
        default=True,
        help="Enable the GraphQL remaining-budget guard (default).",
    )
    p.add_argument(
        "--no-rate-guard",
        action="store_false",
        dest="rate_guard_enabled",
        help="Disable the GraphQL remaining-budget guard.",
    )
    timeout_defaults = (
        ("planner", 1200),
        ("reviewer", 1200),
        ("implementer", 1800),
        ("address-review", 7200),
        ("git-message", 1200),
        ("clone", 120),
        ("network", 120),
        ("gh", 120),
        ("metadata", 10),
        ("rebase", 2400),
        ("diff-collect", 60),
        ("pre-pr-test", 600),
    )
    for timeout_name, timeout_default in timeout_defaults:
        p.add_argument(
            f"--{timeout_name}-timeout",
            dest=f"{timeout_name.replace('-', '_')}_timeout",
            type=_parse_positive_int,
            default=timeout_default,
            metavar="SECONDS",
        )
    p.add_argument(
        "--poll-max-wait",
        type=_parse_positive_int,
        default=1200,
        metavar="SECONDS",
    )
    p.add_argument(
        "--rate-guard-threshold",
        type=_parse_positive_int,
        default=200,
        metavar="N",
        help="Park agent jobs below this GraphQL remaining budget (default: 200).",
    )
    p.add_argument(
        "--plugin-skills-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Explicit root containing installed automation skills.",
    )
    p.add_argument(
        "--metrics-port",
        type=_parse_metrics_port,
        default=0,
        metavar="PORT",
        help=(
            "Loopback-only port for the local Prometheus /metrics and /health server "
            "(0 disables it)."
        ),
    )
    p.add_argument(
        "--event-log-retention-days",
        type=_parse_non_negative_int,
        default=DEFAULT_EVENT_LOG_RETENTION_DAYS,
        help=(
            "Delete inactive pipeline event logs older than this many days; 0 disables age cleanup."
        ),
    )
    p.add_argument(
        "--event-log-retention-count",
        type=_parse_non_negative_int,
        default=DEFAULT_EVENT_LOG_RETENTION_COUNT,
        help=(
            "Retain at most this many pipeline event logs when inactive logs permit; "
            "0 disables the count limit."
        ),
    )
    p.add_argument(
        "--repos",
        type=_parse_repo_list,
        default=None,
        help=(
            "Comma-separated repo list (e.g. `--repos foo,bar`). Overrides org "
            "enumeration. Space-separated input is NOT accepted."
        ),
    )
    return p


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the loop runner."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.issue_limit is not None and (args.issues is not None or args.prs is not None):
        parser.error("--issue-limit cannot be combined with --issues or --prs")
    if args.reset_plan_review_session and not args.issues:
        parser.error("--reset-plan-review-session requires explicit --issues")
    return args


def _validate_phases(phases_csv: str) -> tuple[str, ...]:
    selected = tuple(p.strip() for p in phases_csv.split(",") if p.strip())
    invalid = [p for p in selected if p not in ALL_SELECTABLE]
    if invalid:
        raise SystemExit(f"Unknown phase(s): {invalid}. Valid: {','.join(ALL_SELECTABLE)}")
    return selected


def _phase_order_warnings(cfg: LoopConfig) -> list[str]:
    """Return phase-order warnings.

    Queue stages own their prerequisites: a selected late stage either acts on
    a satisfied item or routes it to the prerequisite queue. Therefore a phase
    subset is an entry hint, not an unsafe ordering contract.
    """
    del cfg
    return []


def _pipeline_scope_for_phases(phases: tuple[str, ...]) -> PipelineScope | None:
    """Translate top-level phase names into a contiguous pipeline scope.

    ``None`` preserves the default full pipeline, including repo discovery.
    Partial selections use the same stage ownership as the focused wrapper
    CLIs: plan = planning+plan_review, implement = implementation+pr_review+
    merge_wait, drive-green = pr_review+merge_wait. The overlap
    lets either operational entry point resume an already-eligible PR through
    merge-wait, where the loop re-reads its eligibility label, live PR head,
    and separate operator authorization before conditional merge; it still
    requires the ephemeral current-process review proof.
    """
    selected = set(phases)
    if selected == set(ALL_SELECTABLE):
        return None

    from hephaestus.automation.pipeline.routing import PipelineScope, StageName

    stage_sets = {
        "plan": (StageName.PLANNING, StageName.PLAN_REVIEW),
        "implement": (
            StageName.IMPLEMENTATION,
            StageName.PR_REVIEW,
            StageName.MERGE_WAIT,
        ),
        "drive-green": (
            StageName.PR_REVIEW,
            StageName.MERGE_WAIT,
        ),
    }
    stages = frozenset(
        stage for phase in ALL_SELECTABLE if phase in selected for stage in stage_sets[phase]
    )
    try:
        return PipelineScope(stages)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _pipeline_event_log_path(
    projects_dir: Path, repos: list[str], *, has_repo_source: bool = False
) -> Path | None:
    """Return the default durable event-log path for a loop invocation.

    The coordinator writes ``run_start`` before repo discovery. Keeping the
    default log under the local automation state dir avoids creating
    ``projects_dir / repo`` early, which would look like a cloned checkout to
    the repo stage.
    """
    if not repos and not has_repo_source:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(DEFAULT_STATE_DIR) / f"pipeline-events-{stamp}-{os.getpid()}.jsonl"


# ---------------------------------------------------------------------------
# Repo discovery — re-exported from loop_repo_manager (refs #1360 / #1179).
# The helpers above are imported at module level with explicit ``as`` aliases,
# keeping ``patch.object(loop_runner, "_fn")`` working.
# ---------------------------------------------------------------------------


def _preflight_token_scopes(org: str, probe_repo: str, *, timeout: int = 120) -> None:
    """Verify the gh token can read ``org/probe_repo`` before dispatch."""
    try:
        out = gh_call(
            [
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{org}/{probe_repo}",
                "--jq",
                ".permissions",
            ],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"ERROR: `gh` token preflight for {org}/{probe_repo} timed out after {exc.timeout}s."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"ERROR: `gh` cannot read {org}/{probe_repo} with the current token.\n"
            f"  {(exc.stderr or '').strip()}\n"
            "  Required scopes: repo (classic) OR "
            "Issues+PRs+Contents Read & Write (fine-grained).\n"
            "  Check with: gh auth status"
        ) from exc
    except (RuntimeError, OSError) as exc:
        raise SystemExit(
            f"ERROR: `gh` token preflight for {org}/{probe_repo} failed: {exc}"
        ) from exc
    if out.stdout.strip() in {"null", "{}"}:
        LOG.warning(
            "Token permissions on %s/%s are empty; PR/issue writes will fail.",
            org,
            probe_repo,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool, log_format: str = "text") -> None:
    configure_cli_logging(verbose=verbose, log_format=log_format)


def _resolve_org_and_repos(
    args: argparse.Namespace,
) -> tuple[str, list[str], str | None]:
    """Resolve ``(org, repos, error_message)`` from CLI args + cwd detection.

    Precedence:
      1. ``--repos`` given → use it; org from cwd (preferred) or ``--org NAME``.
      2. ``--org NAME`` (explicit) → stream non-fork repos in NAME.
      3. ``--org`` (no arg) → detect org from cwd; stream non-fork repos.
      4. (no flags) → use only the cwd repo + its org.

    Returns ``("", [], "<reason>")`` on error so ``main()`` can log and exit.
    """
    # Branch 1: explicit --repos
    if args.repos:
        if (args.issues or args.prs) and len(args.repos) != 1:
            return (
                "",
                [],
                "--issues/--prs require exactly one repository via --repos REPO.",
            )
        detected_org, _ = _detect_cwd_repo(metadata_timeout=args.metadata_timeout)
        explicit_org = args.org if isinstance(args.org, str) else None
        org = explicit_org or detected_org
        if not org:
            return (
                "",
                [],
                "--repos requires being run inside a github.com repo or passing --org NAME.",
            )
        return (org, list(args.repos), None)

    # Branches 2 + 3: --org variants
    if args.org is not None:
        if args.org is _ORG_AUTODETECT:
            detected_org, _ = _detect_cwd_repo(metadata_timeout=args.metadata_timeout)
            if not detected_org:
                return (
                    "",
                    [],
                    "--org with no argument requires being run inside a github.com repo.",
                )
            org = detected_org
        else:
            org = args.org
        # The coordinator owns a resettable, paged source for an org-wide
        # run. Do not enumerate every repository here merely to construct the
        # pipeline configuration; that would recreate the eager O(org) spill
        # this wrapper is meant to avoid.
        if not args.issues and not args.prs:
            LOG.info("Streaming repositories in %s through the bounded pipeline source ...", org)
            return (org, [], None)

        # Issue and PR numbers are repository-local.  Refuse the ambiguous
        # combination instead of materializing an entire organization and
        # silently choosing its first repository as the direct-scope target.
        return (
            org,
            [],
            "--org with --issues/--prs requires exactly one --repos REPO scope.",
        )

    # Branch 4: no flags — default to cwd repo
    detected_org, detected_repo = _detect_cwd_repo(metadata_timeout=args.metadata_timeout)
    if not (detected_org and detected_repo):
        return (
            "",
            [],
            "No repo specified and cwd is not a github.com repo. "
            "Pass --repos foo,bar or --org [NAME].",
        )
    LOG.info("Defaulting to current repo: %s/%s", detected_org, detected_repo)
    return (detected_org, [detected_repo], None)


def _build_pipeline_config(
    args: argparse.Namespace,
    cfg: LoopConfig,
    org: str,
    repos: list[str],
    *,
    repo_source_factory: Callable[[], Iterator[str]] | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig from the parsed args and LoopConfig.

    Args:
        args: Parsed argparse Namespace.
        cfg: The LoopConfig.
        org: The organization name.
        repos: List of repository names.

    Returns:
        A PipelineConfig instance compatible with pipeline.run_pipeline.

    """
    from hephaestus.automation.pipeline.coordinator import PipelineConfig

    circuit_breaker_snapshot_provider = None
    if cfg.metrics_port:
        # Keep this capability out of the pure pipeline coordinator. It is
        # supplied only for the explicitly enabled observability path.
        from hephaestus.resilience import all_circuit_breaker_snapshots

        circuit_breaker_snapshot_provider = all_circuit_breaker_snapshots

    budget_overrides = {"merge": cfg.drive_green_loops}
    if cfg.review_iterations is not None:
        budget_overrides.update(
            {
                "plan_review_iter": cfg.review_iterations,
                "pr_review_iter": cfg.review_iterations,
                "pr_review_hard": cfg.review_iterations,
            }
        )

    return PipelineConfig(
        org=org,
        repos=repos,
        repo_source_factory=repo_source_factory,
        issues=cfg.issues,
        reset_plan_review_sessions=(
            frozenset(cfg.issues) if cfg.reset_plan_review_session else frozenset()
        ),
        prs=cfg.prs,
        issue_limit=cfg.issue_limit,
        loops=cfg.loops,
        max_workers=cfg.max_workers,
        parallel_repos=cfg.parallel_repos,
        learning_workers=cfg.learning_workers,
        learning_queue_capacity=cfg.learning_queue_capacity,
        dry_run=cfg.dry_run,
        grace_s=30.0,  # Default grace period
        phase_timeout_s=cfg.phase_timeout_s,
        agent=cfg.agent,
        disable_pi_automation=cfg.disable_pi_automation,
        auth_status_timeout=cfg.auth_status_timeout,
        pi_isolation_adapter=cfg.pi_isolation_adapter,
        pi_dir=cfg.pi_dir,
        model=cfg.model,
        planner_model=cfg.planner_model,
        reviewer_model=cfg.reviewer_model,
        implementer_model=cfg.implementer_model,
        fallback_model=cfg.fallback_model,
        planner_reasoning_effort=cfg.planner_reasoning_effort,
        reviewer_reasoning_effort=cfg.reviewer_reasoning_effort,
        implementer_reasoning_effort=cfg.implementer_reasoning_effort,
        gh_extra_path_root=cfg.gh_extra_path_root,
        rate_guard_enabled=cfg.rate_guard_enabled,
        rate_guard_threshold=cfg.rate_guard_threshold,
        plugin_skills_dir=cfg.plugin_skills_dir,
        planner_timeout=cfg.planner_timeout,
        reviewer_timeout=cfg.reviewer_timeout,
        implementer_timeout=cfg.implementer_timeout,
        address_review_timeout=cfg.address_review_timeout,
        git_message_timeout=cfg.git_message_timeout,
        poll_max_wait=cfg.poll_max_wait,
        clone_timeout=cfg.clone_timeout,
        network_timeout=cfg.network_timeout,
        gh_timeout=cfg.gh_timeout,
        metadata_timeout=cfg.metadata_timeout,
        rebase_timeout=cfg.rebase_timeout,
        diff_collect_timeout=cfg.diff_collect_timeout,
        pre_pr_test_timeout=cfg.pre_pr_test_timeout,
        no_advise=cfg.no_advise,
        enable_learn=not cfg.no_learn,
        nitpick=cfg.nitpick,
        drive_green_all=cfg.drive_green_all,
        include_bot_prs=True,
        include_all_authors=cfg.drive_green_all,
        run_pre_pr_tests=cfg.run_pre_pr_tests,
        budget_overrides=budget_overrides,
        serialize_file_overlap=cfg.serialize_file_overlap,
        metrics_port=cfg.metrics_port,
        circuit_breaker_snapshot_provider=circuit_breaker_snapshot_provider,
        event_log_path=_pipeline_event_log_path(
            cfg.projects_dir, repos, has_repo_source=repo_source_factory is not None
        ),
        projects_dir=cfg.projects_dir,
        repo_roots=cfg.repo_roots,
        json_out=args.json,
        scope=_pipeline_scope_for_phases(cfg.phases),
    )


def _current_checkout_repo_roots(
    args: argparse.Namespace, org: str, repos: list[str], projects_dir: Path
) -> dict[str, Path]:
    """Return an explicit root only for an eligible noncanonical cwd checkout.

    A user-supplied projects root (either the CLI flag or a valid
    ``--projects-dir``) is an authoritative request to use conventional
    ``projects_dir / repo`` locations.  The automatic exception exists solely
    for running the loop from a differently named checkout, such as a swarm
    worktree.  Automation's own ``build/.worktrees/issue-N`` checkouts are
    already represented by the conventional base checkout and remain so.
    """
    if args.projects_dir is not None:
        return {}

    detected_org, detected_repo = _detect_cwd_repo(metadata_timeout=args.metadata_timeout)
    if not detected_repo or not detected_org or detected_org.casefold() != org.casefold():
        return {}

    repo = next((name for name in repos if name.casefold() == detected_repo.casefold()), None)
    if repo is None:
        return {}

    checkout = get_repo_root()
    conventional_root = projects_dir / repo
    if checkout == conventional_root:
        return {}

    # An automation issue worktree always has the structural form
    # ``<base checkout>/build/.worktrees/<issue>``.  Do not assume that the
    # base checkout has the conventional ``projects_dir / repo`` name: swarm
    # and manually renamed checkouts are valid.  In that noncanonical case the
    # base checkout itself is the explicit root; using the issue worktree here
    # would make a later implementation create nested worktrees beneath it.
    if checkout.parent.name == ".worktrees" and checkout.parent.parent.name == "build":
        base_checkout = checkout.parent.parent.parent
        return {} if base_checkout == conventional_root else {repo: base_checkout}

    return {repo: checkout}


def _error_exit(args: argparse.Namespace, message: str, json_message: str | None = None) -> int:
    """Log *message*, emit the JSON error envelope under --json, and return 1.

    Args:
        args: Parsed argparse Namespace (for the ``--json`` gate).
        message: Human-readable error logged at ERROR level.
        json_message: Envelope message override (defaults to *message*) —
            preserves the legacy envelope strings exactly.

    Returns:
        The process exit code 1.

    """
    LOG.error("%s", message)
    if args.json:
        emit_json_status(1, message=json_message if json_message is not None else message)
    return 1


def _dispatch_pipeline(
    args: argparse.Namespace,
    cfg: LoopConfig,
    org: str,
    repos: list[str],
    *,
    repo_source_factory: Callable[[], Iterator[str]] | None = None,
) -> int:
    """Run the queue-based pipeline and return its exit code.

    The repo token preflight happens before dispatch; the repo stage owns
    cloning, so this branch does not clone. ``--phase-timeout`` bounds each
    agent job.

    Args:
        args: Parsed argparse Namespace.
        cfg: The LoopConfig.
        org: The organization name.
        repos: List of repository names.

    Returns:
        The pipeline's exit code.

    """
    if not cfg.dry_run and repos:
        _preflight_token_scopes(cfg.org, repos[0], timeout=cfg.gh_timeout)
    from hephaestus.automation.pipeline.coordinator import run_pipeline

    config = _build_pipeline_config(
        args,
        cfg,
        org,
        repos,
        repo_source_factory=repo_source_factory,
    )
    with event_log_lifecycle(
        config.event_log_path,
        retention_days=cfg.event_log_retention_days,
        retention_count=cfg.event_log_retention_count,
        dry_run=cfg.dry_run,
    ):
        return run_pipeline(config)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    args = _parse_args(argv)
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose, args.log_format)
    agent = resolve_agent(
        args.agent,
        disable_pi_automation=args.disable_pi_automation,
        auth_status_timeout=args.auth_status_timeout,
        pi_isolation_adapter=args.pi_isolation_adapter,
        pi_dir=args.pi_dir,
    )

    phases = _validate_phases(args.phases)

    # Resolve org + repos using a 4-branch precedence ladder. Org is
    # always set explicitly here — there is no silent fallback to a
    # hardcoded default.
    org, repos, err = _resolve_org_and_repos(args)
    if err:
        return _error_exit(args, err)

    projects_dir = resolve_projects_dir(args.projects_dir, prefer_cwd_parent=True)
    streaming_org_scope = args.org is not None and not args.repos and not (args.issues or args.prs)
    root_scope_repos = repos
    if streaming_org_scope:
        cwd_org, cwd_repo = _detect_cwd_repo(metadata_timeout=args.metadata_timeout)
        if cwd_repo and cwd_org and cwd_org.casefold() == org.casefold():
            # Preserve the current checkout for its matching repository even
            # though the streamed source has not yet yielded that name.
            root_scope_repos = [cwd_repo]
    cfg = LoopConfig(
        loops=args.loops,
        review_iterations=args.review_iterations,
        max_workers=args.max_workers,
        learning_workers=args.learning_workers,
        learning_queue_capacity=args.learning_queue_capacity,
        drive_green_loops=args.drive_green_loops,
        serialize_file_overlap=args.serialize_file_overlap,
        parallel_repos=args.parallel_repos,
        phases=phases,
        agent=agent,
        disable_pi_automation=args.disable_pi_automation,
        auth_status_timeout=args.auth_status_timeout,
        pi_isolation_adapter=args.pi_isolation_adapter,
        pi_dir=args.pi_dir,
        issues=args.issues or [],
        reset_plan_review_session=args.reset_plan_review_session,
        prs=args.prs or [],
        issue_limit=args.issue_limit,
        dry_run=args.dry_run,
        no_advise=args.no_advise,
        no_learn=args.no_learn,
        nitpick=args.nitpick,
        drive_green_all=args.drive_green_all,
        run_pre_pr_tests=args.run_pre_pr_tests,
        model=args.model,
        planner_model=_resolve_model_option(
            args.planner_model, args.model, default_planner_model()
        ),
        reviewer_model=_resolve_model_option(
            args.reviewer_model, args.model, default_reviewer_model()
        ),
        implementer_model=_resolve_model_option(
            args.implementer_model, args.model, default_implementer_model()
        ),
        fallback_model=_resolve_model_option(
            args.fallback_model, args.model, default_fallback_model()
        ),
        planner_reasoning_effort=args.planner_reasoning_effort,
        reviewer_reasoning_effort=args.reviewer_reasoning_effort,
        implementer_reasoning_effort=args.implementer_reasoning_effort,
        gh_extra_path_root=args.gh_extra_path_root,
        gh_global_rate=args.gh_global_rate,
        gh_global_burst=args.gh_global_burst,
        rate_guard_enabled=args.rate_guard_enabled,
        rate_guard_threshold=args.rate_guard_threshold,
        plugin_skills_dir=args.plugin_skills_dir,
        planner_timeout=args.planner_timeout,
        reviewer_timeout=args.reviewer_timeout,
        implementer_timeout=args.implementer_timeout,
        address_review_timeout=args.address_review_timeout,
        git_message_timeout=args.git_message_timeout,
        poll_max_wait=args.poll_max_wait,
        clone_timeout=args.clone_timeout,
        network_timeout=args.network_timeout,
        gh_timeout=args.gh_timeout,
        metadata_timeout=args.metadata_timeout,
        rebase_timeout=args.rebase_timeout,
        diff_collect_timeout=args.diff_collect_timeout,
        pre_pr_test_timeout=args.pre_pr_test_timeout,
        org=org,
        projects_dir=projects_dir,
        repo_roots=_current_checkout_repo_roots(args, org, root_scope_repos, projects_dir),
        # A non-positive --phase-timeout explicitly disables the bound; any
        # positive value applies it.
        phase_timeout_s=(
            args.phase_timeout if args.phase_timeout and args.phase_timeout > 0 else None
        ),
        metrics_port=args.metrics_port,
        event_log_retention_days=args.event_log_retention_days,
        event_log_retention_count=args.event_log_retention_count,
    )

    org_repo_source = (
        (lambda: _iter_gh_repos(org, network_timeout=cfg.network_timeout))
        if streaming_org_scope
        else None
    )

    if not repos and org_repo_source is None:
        return _error_exit(args, "Repo list is empty; nothing to do.", "empty repo list")

    if org_repo_source is not None:
        LOG.info("Repos to process: streamed from organization %s", org)
    else:
        LOG.info("Repos to process: %s", " ".join(repos))
    LOG.info(
        "Loops: %d | Max workers: %d | Parallel repos: %d | Agent: %s | Dry run: %s",
        cfg.loops,
        cfg.max_workers,
        cfg.parallel_repos,
        cfg.agent,
        cfg.dry_run,
    )
    LOG.info("Phases: %s", ",".join(cfg.phases))
    if cfg.issues:
        LOG.info("Issues: %s", ",".join(str(n) for n in cfg.issues))
    if cfg.prs:
        LOG.info("PRs: %s", ",".join(str(n) for n in cfg.prs))

    from hephaestus.utils.terminal import install_sigtstp_only

    install_sigtstp_only()
    return _dispatch_pipeline(
        args,
        cfg,
        org,
        repos,
        repo_source_factory=org_repo_source,
    )


if __name__ == "__main__":
    sys.exit(main())
