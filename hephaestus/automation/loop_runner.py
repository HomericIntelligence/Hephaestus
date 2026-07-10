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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.coordinator import PipelineConfig
    from hephaestus.automation.pipeline.routing import PipelineScope

from hephaestus.agents.runtime import resolve_agent
from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.automation.loop_repo_manager import (
    _clone_missing_repos as _clone_missing_repos,
    _detect_cwd_repo as _detect_cwd_repo,
    _gh_list_repos as _gh_list_repos,
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


def _default_phase_timeout_s() -> float:
    """Return the default per-agent-job timeout in seconds.

    An agent job that shells out to an external coding agent can stall
    indefinitely on a network hang; a non-``None`` default keeps every job
    bounded even when the operator does not pass ``--phase-timeout``.
    Overridable via ``HEPH_PHASE_TIMEOUT`` (seconds). A malformed env value logs
    a warning and falls back to the default rather than crashing at startup.

    The 7800s default lets the outer job guard safely exceed the longest
    in-agent timeout (2h) so a healthy job never trips it.
    """
    import os

    default = 7800
    raw = os.environ.get("HEPH_PHASE_TIMEOUT")
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        LOG.warning("Ignoring non-numeric HEPH_PHASE_TIMEOUT=%r — using default %ds", raw, default)
        return float(default)


@dataclass
class LoopConfig:
    """Top-level CLI-derived configuration.

    Carries the parsed scope/model/throttle knobs from :func:`main` into
    :func:`_build_pipeline_config`, which maps them onto the coordinator's
    :class:`~hephaestus.automation.pipeline.coordinator.PipelineConfig`.
    """

    loops: int = 5
    max_workers: int = LOOP_DEFAULT_MAX_WORKERS
    parallel_repos: int = 1
    # Dataclass default covers ONLY the iteration phases (``ALL_PHASES`` =
    # plan, implement), deliberately excluding drive-green — a bare
    # ``LoopConfig()`` gets a quiet plan+implement run. The CLI ``--phases``
    # default is ``ALL_SELECTABLE`` (set in the parser), so an operator opts
    # into the blocking drive-green by default.
    phases: tuple[str, ...] = ALL_PHASES
    # Bound on per-issue drive-green merge attempts before the issue is tagged
    # ``state:skip`` (#1560). Defaults to 1, matching the CIDriver default.
    max_merge_attempts: int = 1
    # When True (default), never dispatch two issues whose plans touch the same
    # file concurrently — defer the later one (#1623).
    serialize_file_overlap: bool = True
    agent: str = "claude"
    issues: list[int] = field(default_factory=list)
    prs: list[int] = field(default_factory=list)
    dry_run: bool = False
    no_advise: bool = False
    nitpick: bool = False
    drive_green_all: bool = False
    run_pre_pr_tests: bool = False
    # ``model`` is the catch-all applied to every phase when set; per-phase
    # fields below take precedence over it.
    model: str = ""
    planner_model: str = ""
    reviewer_model: str = ""
    implementer_model: str = ""
    gh_global_rate: float = 10.0
    gh_global_burst: float = 30.0
    # Org is resolved at runtime from --org / --repos / cwd detection; no
    # hardcoded fallback. Always set by main() before dispatch.
    org: str = ""
    projects_dir: Path = DEFAULT_PROJECTS_DIR
    # Per-agent-job timeout in seconds. Defaults to an env-overridable bound
    # (``HEPH_PHASE_TIMEOUT``); ``--phase-timeout`` overrides it and a
    # non-positive value disables the bound (``None``).
    phase_timeout_s: float | None = field(default_factory=_default_phase_timeout_s)


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
    p.add_argument("--loops", type=int, default=5, help="Number of loop iterations (default: 5)")
    p.add_argument(
        "--max-merge-attempts",
        type=int,
        default=1,
        help=(
            "Per-issue drive-green merge attempts before the issue is tagged "
            "state:skip and the worker moves on (default: 1, matching the prior "
            "drive-green retry budget)."
        ),
    )
    p.add_argument(
        "--parallel-repos",
        type=int,
        default=1,
        help="Repos processed in parallel per loop iteration (default: 1)",
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
            "Pass --all to the drive-green phase: drive every open PR, "
            "including those opened by teammates and bots. By default "
            "drive-green operates only on PRs authored by the authenticated "
            "viewer (#821)."
        ),
    )
    p.add_argument(
        "--run-pre-pr-tests",
        action="store_true",
        help=(
            "Run the implementation-stage pre-PR unit-test gate before committing and creating PRs."
        ),
    )
    p.add_argument(
        "--model",
        default="",
        help=(
            "Model ID applied to every phase (planner, reviewer, implementer, advise) "
            "for child processes, so no HEPH_*_MODEL env vars are required. The /learn "
            "step inherits its parent phase's model automatically. A per-phase flag below "
            "overrides this for that phase."
        ),
    )
    p.add_argument("--planner-model", default="", help="HEPH_PLANNER_MODEL for child processes")
    p.add_argument(
        "--reviewer-model",
        default="",
        help="HEPH_REVIEWER_MODEL for child processes (plan-review + PR-review)",
    )
    p.add_argument(
        "--implementer-model",
        default="",
        help="HEPH_IMPLEMENTER_MODEL for child processes (implement, address-review, ci-driver)",
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
            "Default (no flag): run only for the current repo."
        ),
    )
    p.add_argument(
        "--projects-dir",
        type=str,
        default=None,
        help=(
            "Local directory containing repo clones. When omitted, resolved from "
            "the ``PROJECTS_ROOT`` env var (if set and existing), otherwise the "
            "current checkout parent when available, then "
            f"``{DEFAULT_PROJECTS_DIR}``."
        ),
    )
    p.add_argument(
        "--phase-timeout",
        type=float,
        default=_default_phase_timeout_s(),
        help=(
            "Per-phase timeout in seconds (default: HEPH_PHASE_TIMEOUT or "
            f"{int(_default_phase_timeout_s())}s). Pass 0 or a negative value to disable. "
            "This bounds each AGENT JOB the pipeline runs, not a whole phase subprocess."
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
    return _build_parser().parse_args(argv)


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
    CLIs: plan = planning+plan_review, implement = implementation+pr_review,
    drive-green = ci+merge_wait.
    """
    selected = set(phases)
    if selected == set(ALL_SELECTABLE):
        return None

    from hephaestus.automation.pipeline.routing import PipelineScope, StageName

    stage_sets = {
        "plan": (StageName.PLANNING, StageName.PLAN_REVIEW),
        "implement": (StageName.IMPLEMENTATION, StageName.PR_REVIEW),
        "drive-green": (StageName.CI, StageName.MERGE_WAIT),
    }
    stages = frozenset(
        stage for phase in ALL_SELECTABLE if phase in selected for stage in stage_sets[phase]
    )
    try:
        return PipelineScope(stages)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _pipeline_event_log_path(projects_dir: Path, repos: list[str]) -> Path | None:
    """Return the default durable event-log path for a loop invocation.

    The coordinator writes ``run_start`` before repo discovery. Keeping the
    default log under the local automation state dir avoids creating
    ``projects_dir / repo`` early, which would look like a cloned checkout to
    the repo stage.
    """
    if not repos:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(DEFAULT_STATE_DIR) / f"pipeline-events-{stamp}-{os.getpid()}.jsonl"


# ---------------------------------------------------------------------------
# Repo discovery — re-exported from loop_repo_manager (refs #1360 / #1179).
# The helpers above are imported at module level with explicit ``as`` aliases,
# keeping ``patch.object(loop_runner, "_fn")`` working.
# ---------------------------------------------------------------------------


def _preflight_token_scopes(org: str, probe_repo: str) -> None:
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


def _setup_logging(verbose: bool) -> None:
    configure_cli_logging(verbose=verbose)


def _resolve_org_and_repos(
    args: argparse.Namespace,
) -> tuple[str, list[str], str | None]:
    """Resolve ``(org, repos, error_message)`` from CLI args + cwd detection.

    Precedence:
      1. ``--repos`` given → use it; org from cwd (preferred) or ``--org NAME``.
      2. ``--org NAME`` (explicit) → enumerate non-fork repos in NAME.
      3. ``--org`` (no arg) → detect org from cwd; enumerate non-fork repos.
      4. (no flags) → use only the cwd repo + its org.

    Returns ``("", [], "<reason>")`` on error so ``main()`` can log and exit.
    """
    # Branch 1: explicit --repos
    if args.repos:
        detected_org, _ = _detect_cwd_repo()
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
            detected_org, _ = _detect_cwd_repo()
            if not detected_org:
                return (
                    "",
                    [],
                    "--org with no argument requires being run inside a github.com repo.",
                )
            org = detected_org
        else:
            org = args.org
        LOG.info("Discovering repos in %s ...", org)
        candidates = _gh_list_repos(org)
        if not candidates:
            return (org, [], "No repos returned from gh repo list — possible rate limit.")
        LOG.info("Sorting %d repos by open-issue count ...", len(candidates))
        return (org, _sort_repos_by_open_count(org, candidates), None)

    # Branch 4: no flags — default to cwd repo
    detected_org, detected_repo = _detect_cwd_repo()
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
    args: argparse.Namespace, cfg: LoopConfig, org: str, repos: list[str]
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

    return PipelineConfig(
        org=org,
        repos=repos,
        issues=cfg.issues,
        prs=cfg.prs,
        loops=cfg.loops,
        max_workers=cfg.max_workers,
        parallel_repos=cfg.parallel_repos,
        dry_run=cfg.dry_run,
        grace_s=30.0,  # Default grace period
        phase_timeout_s=cfg.phase_timeout_s,
        agent=cfg.agent,
        model=cfg.model,
        planner_model=cfg.planner_model,
        reviewer_model=cfg.reviewer_model,
        implementer_model=cfg.implementer_model,
        no_advise=cfg.no_advise,
        nitpick=cfg.nitpick,
        drive_green_all=cfg.drive_green_all,
        include_bot_prs=True,
        include_all_authors=cfg.drive_green_all,
        run_pre_pr_tests=cfg.run_pre_pr_tests,
        budget_overrides={"merge": cfg.max_merge_attempts},
        serialize_file_overlap=cfg.serialize_file_overlap,
        event_log_path=_pipeline_event_log_path(cfg.projects_dir, repos),
        projects_dir=cfg.projects_dir,
        json_out=args.json,
        scope=_pipeline_scope_for_phases(cfg.phases),
    )


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
    args: argparse.Namespace, cfg: LoopConfig, org: str, repos: list[str]
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
    if not cfg.dry_run:
        _preflight_token_scopes(cfg.org, repos[0])
    from hephaestus.automation.pipeline.coordinator import run_pipeline

    return run_pipeline(_build_pipeline_config(args, cfg, org, repos))


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    args = _parse_args(argv)
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose)
    agent = resolve_agent(args.agent)

    phases = _validate_phases(args.phases)

    # Resolve org + repos using a 4-branch precedence ladder. Org is
    # always set explicitly here — there is no silent fallback to a
    # hardcoded default.
    org, repos, err = _resolve_org_and_repos(args)
    if err:
        return _error_exit(args, err)

    cfg = LoopConfig(
        loops=args.loops,
        max_workers=args.max_workers,
        max_merge_attempts=args.max_merge_attempts,
        serialize_file_overlap=args.serialize_file_overlap,
        parallel_repos=args.parallel_repos,
        phases=phases,
        agent=agent,
        issues=args.issues or [],
        prs=args.prs or [],
        dry_run=args.dry_run,
        no_advise=args.no_advise,
        nitpick=args.nitpick,
        drive_green_all=args.drive_green_all,
        run_pre_pr_tests=args.run_pre_pr_tests,
        model=args.model,
        planner_model=args.planner_model,
        reviewer_model=args.reviewer_model,
        implementer_model=args.implementer_model,
        gh_global_rate=args.gh_global_rate,
        gh_global_burst=args.gh_global_burst,
        org=org,
        projects_dir=resolve_projects_dir(args.projects_dir, prefer_cwd_parent=True),
        # A non-positive --phase-timeout explicitly disables the bound; any
        # positive value (including the env-overridable default) applies it.
        phase_timeout_s=(
            args.phase_timeout if args.phase_timeout and args.phase_timeout > 0 else None
        ),
    )

    if not repos:
        return _error_exit(args, "Repo list is empty; nothing to do.", "empty repo list")

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

    return _dispatch_pipeline(args, cfg, org, repos)


if __name__ == "__main__":
    sys.exit(main())
