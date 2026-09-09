"""Parse current queue commands and build one pipeline configuration."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from hephaestus._version_lookup import get_version
from hephaestus.agents.runtime import resolve_agent
from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.automation.event_log_retention import (
    DEFAULT_EVENT_LOG_RETENTION_COUNT,
    DEFAULT_EVENT_LOG_RETENTION_DAYS,
    event_log_lifecycle,
)
from hephaestus.automation.github_api import gh_call
from hephaestus.automation.loop_repo_manager import _detect_cwd_repo, _iter_gh_repos
from hephaestus.automation.models import DEFAULT_STATE_DIR, DEFAULT_WORKER_COUNT
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.host_verification_pyxis import (
    DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE,
)
from hephaestus.automation.pipeline.routing import ROUTES, PipelineScope, StageName
from hephaestus.automation.podman_machine_supervisor import (
    PodmanMachineError,
    prepare_podman_machine,
)
from hephaestus.automation.role_selection import resolve_role_agents
from hephaestus.cli.utils import (
    MODEL_REFERENCE_HELP,
    add_host_verification_pyxis_image_arg,
    add_role_agent_args,
    configure_cli_logging,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.config.paths import resolve_projects_dir
from hephaestus.utils.git import _is_full_commit_sha, run_git
from hephaestus.utils.helpers import get_repo_root

LOG = logging.getLogger(__name__)
_ORG_AUTODETECT = object()
MAIN_STAGES = tuple(
    stage for stage in ROUTES if stage not in {StageName.LEARNING, StageName.FINISHED}
)
_PROFILES = {
    "full": ("hephaestus-automation-loop", MAIN_STAGES),
    "planning": ("hephaestus-plan-issues", (StageName.PLANNING, StageName.PLAN_REVIEW)),
    "implementation": (
        "hephaestus-implement-issues",
        (StageName.IMPLEMENTATION, StageName.PR_REVIEW, StageName.MERGE_WAIT),
    ),
    "review": ("hephaestus-review-prs", (StageName.PR_REVIEW,)),
}


def _source_revision(source_root: Path | None = None) -> str | None:
    """Return the exact revision of an editable source checkout, if available."""
    source_root = source_root or Path(__file__).resolve().parents[2]
    if not (source_root / ".git").exists():
        return None
    try:
        revision = run_git(
            ["rev-parse", "--verify", "HEAD"],
            cwd=source_root,
            timeout=10,
            log_on_error=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return revision if _is_full_commit_sha(revision) else None


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
        detail = (exc.stderr or "").strip()
        if "HTTP 404" in detail:
            raise SystemExit(
                f"ERROR: GitHub returned HTTP 404 for {org}/{probe_repo}.\n"
                "  GitHub cannot confirm whether the repository exists.\n"
                "  Confirm that the repository name is correct and that the current account "
                "has access.\n"
                f"  Repository check: gh repo view {org}/{probe_repo}\n"
                "  Authentication check: gh auth status\n"
                f"  GitHub response: {detail}"
            ) from exc
        raise SystemExit(
            f"ERROR: `gh` cannot read {org}/{probe_repo} with the current token.\n"
            f"  {detail}\n"
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


def _setup_logging(
    verbose: bool,
    log_format: str = "text",
    *,
    quiet: bool = False,
    log_file: str | None = None,
) -> None:
    try:
        configure_cli_logging(
            verbose=verbose,
            log_format=log_format,
            quiet=quiet,
            log_file=log_file,
        )
    except OSError as exc:
        raise SystemExit(
            f"Cannot open log file {log_file!r}: {exc}. Check the parent directory and permissions."
        ) from exc


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


def _parse_stages(value: str) -> tuple[StageName, ...]:
    """Require an ordered, contiguous set of main queue stages."""
    names = tuple(part.strip() for part in value.split(","))
    try:
        stages = tuple(StageName(name) for name in names)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use current main queue stage names") from exc
    if not stages or len(set(stages)) != len(stages):
        raise argparse.ArgumentTypeError("Stage names must be nonempty and unique")
    if any(stage not in MAIN_STAGES for stage in stages):
        raise argparse.ArgumentTypeError("Learning and finished are implicit auxiliary stages")
    indexes = [MAIN_STAGES.index(stage) for stage in stages]
    if indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise argparse.ArgumentTypeError("Stage names must be contiguous and in queue order")
    return stages


def build_parser(*, profile: str = "full") -> argparse.ArgumentParser:
    """Build the common parser with the selected command scope."""
    prog, stages = _PROFILES[profile]
    workers = 6 if profile == "full" else DEFAULT_WORKER_COUNT
    parser = build_automation_parser(
        prog=prog,
        description="Run the queue-owned automation pipeline.",
        max_workers_default=workers,
        max_workers_help=f"Main worker capacity, 1-32 (default: {workers}).",
        add_github_throttle=True,
        add_gh_extra_path_root=True,
        dry_run_prefix="Preview queue work without agent calls, GitHub writes, or Git pushes.",
    )
    parser.allow_abbrev = False
    parser.set_defaults(profile=profile, stages=stages, force=False)
    parser.set_defaults(
        podman_machine=None,
        podman_start_timeout=120,
        podman_health_timeout=60,
    )
    add_role_agent_args(parser)
    add_host_verification_pyxis_image_arg(parser)
    if profile == "full":
        parser.add_argument(
            "--stages",
            type=_parse_stages,
            help="Comma-separated main stage names in queue order: " + ",".join(MAIN_STAGES),
        )
        parser.add_argument(
            "--podman-machine",
            metavar="NAME",
            help=(
                "Start and verify one AppleHV Podman machine in this host process before "
                "pipeline dispatch. The loop never stops, removes, or recreates the machine."
            ),
        )
        parser.add_argument(
            "--podman-start-timeout",
            type=_parse_positive_int,
            default=120,
            metavar="SECONDS",
            help="Maximum Podman machine start time (default: 120).",
        )
        parser.add_argument(
            "--podman-health-timeout",
            type=_parse_positive_int,
            default=60,
            metavar="SECONDS",
            help="Maximum named-connection health-check time (default: 60).",
        )
    if profile in {"full", "planning"}:
        parser.add_argument("--force", action="store_true", help="Plan the selected issues again.")
    parser.add_argument(
        "--update-plan",
        action="store_true",
        help="Update each selected issue plan from current origin/main, then continue the queue.",
    )
    parser.add_argument(
        "--rebase",
        action="store_true",
        help="Rebase each selected worktree against origin/main, then continue the queue.",
    )
    parser.add_argument(
        "--reset-plan-review-session",
        action="store_true",
        help="Reset the reviewer conversation for explicit issues.",
    )
    parser.add_argument(
        "--issues",
        type=_parse_issue_list,
        default=None,
        help="Comma-separated issue numbers in one repository.",
    )
    parser.add_argument(
        "--prs",
        type=_parse_pr_list,
        default=None,
        help="Comma-separated PR numbers in one repository.",
    )
    parser.add_argument(
        "--repos", type=_parse_repo_list, default=None, help="Comma-separated repository names."
    )
    parser.add_argument(
        "--org",
        nargs="?",
        const=_ORG_AUTODETECT,
        default=None,
        help="Read repositories from this organization; omit NAME to detect the current owner.",
    )
    parser.add_argument(
        "--projects-dir",
        type=str,
        default=None,
        help="Directory that contains repository checkouts.",
    )
    for name, default, help_text in (
        ("loops", 5 if profile == "full" else 1, "Repository discovery passes."),
        ("merge-attempts", 5, "Maximum merge attempts for one item."),
        ("parallel-repos", 1, "Concurrent repositories."),
        ("learning-workers", 1, "Independent host learning workers."),
        ("learning-queue-capacity", 1, "Maximum pending learning items."),
        ("review-iterations", None, "Override plan and PR review round budgets."),
        ("issue-limit", None, "Maximum eligible issues in the next stored issue wave."),
        ("poll-max-wait", 1200, "Maximum wait for a poll, in seconds."),
        ("rate-guard-threshold", 200, "Park jobs below this remaining GraphQL budget."),
    ):
        parser.add_argument(f"--{name}", type=_parse_positive_int, default=default, help=help_text)
    for name, help_text in (
        ("no-advise", "Skip host advice before planning or implementation."),
        ("no-learn", "Do not create or execute learning intents."),
        ("nitpick", "Include nitpick findings in review."),
        (
            "run-pre-pr-tests",
            "Run the configurable pre-PR test gate for repositories without an automatic "
            "required-check profile; Hephaestus runs its required checks "
            "before initial PR creation.",
        ),
    ):
        parser.add_argument(f"--{name}", action="store_true", help=help_text)
    parser.add_argument(
        "--no-serialize-file-overlap",
        action="store_false",
        dest="serialize_file_overlap",
        default=True,
        help="Allow concurrent items whose planned files overlap.",
    )
    parser.add_argument(
        "--rate-guard",
        action="store_true",
        dest="rate_guard_enabled",
        default=True,
        help="Enable the GraphQL budget guard.",
    )
    parser.add_argument(
        "--no-rate-guard",
        action="store_false",
        dest="rate_guard_enabled",
        help="Disable the GraphQL budget guard.",
    )
    for name in ("model", "planner-model", "reviewer-model", "implementer-model", "fallback-model"):
        parser.add_argument(
            f"--{name}", default="", metavar="MODEL[:EFFORT]", help=MODEL_REFERENCE_HELP
        )
    for name, default in (
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
        ("pre-pr-test", None),
    ):
        parser.add_argument(
            f"--{name}-timeout", type=_parse_positive_int, default=default, metavar="SECONDS"
        )
    parser.add_argument(
        "--phase-timeout",
        type=float,
        default=7800.0,
        help="Timeout for each agent job in seconds; "
        "a nonpositive value disables this outer bound.",
    )
    parser.add_argument(
        "--metrics-port",
        type=_parse_metrics_port,
        default=0,
        help="Local metrics and health port; 0 disables the listener.",
    )
    for name, default in (
        ("days", DEFAULT_EVENT_LOG_RETENTION_DAYS),
        ("count", DEFAULT_EVENT_LOG_RETENTION_COUNT),
    ):
        parser.add_argument(
            f"--event-log-retention-{name}",
            type=_parse_non_negative_int,
            default=default,
            help=f"Inactive event-log retention {name}; 0 disables this limit.",
        )
    parser.add_argument(
        "--plugin-skills-dir",
        type=Path,
        default=None,
        help="Directory of installed automation skills.",
    )
    parser.add_argument(
        "--evidence-receipt-dir",
        type=Path,
        default=None,
        help="Directory for private queue-job evidence receipts.",
    )
    return parser


def parse_args(argv: list[str] | None = None, *, profile: str = "full") -> argparse.Namespace:
    """Parse one current command and reject conflicting issue scopes."""
    parser = build_parser(profile=profile)
    args = parser.parse_args(argv)
    if args.issue_limit is not None and (args.issues is not None or args.prs is not None):
        parser.error("--issue-limit cannot be combined with --issues or --prs")
    if args.update_plan and not args.issues:
        parser.error("--update-plan requires explicit --issues")
    if args.update_plan and StageName.PLANNING not in args.stages:
        parser.error("--update-plan requires the planning stage")
    if args.rebase and not (args.issues or args.prs):
        parser.error("--rebase requires explicit --issues or --prs")
    if args.rebase and StageName.IMPLEMENTATION not in args.stages:
        parser.error("--rebase requires the implementation stage")
    if args.reset_plan_review_session and not args.issues:
        parser.error("--reset-plan-review-session requires explicit --issues")
    return args


def build_config(
    args: argparse.Namespace,
    org: str,
    repos: list[str],
    *,
    root_repos: list[str] | None = None,
    repo_source_factory: Callable[[Event], Iterator[str]] | None = None,
) -> PipelineConfig:
    """Build the queue configuration directly from current command options."""
    projects_dir = resolve_projects_dir(args.projects_dir, prefer_cwd_parent=True)
    stages = tuple(args.stages)
    scope = None if stages == MAIN_STAGES else PipelineScope(frozenset(stages))
    budgets = {"merge": args.merge_attempts}
    if args.review_iterations is not None:
        budgets.update(
            dict.fromkeys(
                ("plan_review_iter", "pr_review_iter", "pr_review_hard"), args.review_iterations
            )
        )
    common_fields = (
        "loops",
        "max_workers",
        "parallel_repos",
        "learning_workers",
        "learning_queue_capacity",
        "dry_run",
        "disable_pi_automation",
        "auth_status_timeout",
        "pi_isolation_adapter",
        "pi_dir",
        "codex_isolation_adapter",
        "codex_isolation_deployment_lock",
        "codex_isolation_deployment_lock_sha256",
        "model",
        "gh_extra_path_root",
        "rate_guard_enabled",
        "rate_guard_threshold",
        "plugin_skills_dir",
        "planner_timeout",
        "reviewer_timeout",
        "implementer_timeout",
        "address_review_timeout",
        "git_message_timeout",
        "poll_max_wait",
        "clone_timeout",
        "network_timeout",
        "gh_timeout",
        "metadata_timeout",
        "rebase_timeout",
        "diff_collect_timeout",
        "pre_pr_test_timeout",
        "no_advise",
        "nitpick",
        "run_pre_pr_tests",
        "serialize_file_overlap",
        "metrics_port",
        "evidence_receipt_dir",
        "force",
        "rebase",
        "update_plan",
        "issue_limit",
        "host_verification_pyxis_sha256",
        "host_verification_pyxis_authority",
        "host_verification_pyxis_quota_root",
        "podman_machine",
    )
    options = {name: getattr(args, name) for name in common_fields}
    agent = args.agent or "claude"
    for role in ("planner", "implementer", "reviewer"):
        options[f"{role}_agent"] = getattr(args, f"{role}_agent") or agent
        options[f"{role}_model"] = getattr(args, f"{role}_model") or args.model
    breaker_snapshots = None
    if args.metrics_port:
        from hephaestus.resilience import all_circuit_breaker_snapshots

        breaker_snapshots = all_circuit_breaker_snapshots
    return PipelineConfig(
        org=org,
        repos=repos,
        repo_source_factory=repo_source_factory,
        package_version=get_version(),
        source_revision=_source_revision(),
        issues=list(dict.fromkeys(args.issues or [])),
        prs=list(dict.fromkeys(args.prs or [])),
        reset_plan_review_sessions=frozenset(args.issues)
        if args.reset_plan_review_session
        else frozenset(),
        agent=agent,
        fallback_model=args.fallback_model,
        phase_timeout_s=args.phase_timeout if args.phase_timeout > 0 else None,
        enable_learn=not args.no_learn,
        budget_overrides=budgets,
        circuit_breaker_snapshot_provider=breaker_snapshots,
        event_log_path=_pipeline_event_log_path(
            projects_dir, repos, has_repo_source=repo_source_factory is not None
        ),
        host_verification_pyxis_image=args.host_verification_pyxis_image
        or DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE,
        projects_dir=projects_dir,
        repo_roots=_current_checkout_repo_roots(
            args, org, repos if root_repos is None else root_repos, projects_dir
        ),
        json_out=args.json,
        scope=scope,
        explicit_pr_review=args.profile == "review",
        **options,
    )


def main(argv: list[str] | None = None, *, profile: str = "full") -> int:
    """Admit the selected roles and run the queue with one configuration."""
    args = parse_args(argv, profile=profile)
    configure_github_throttle_from_args(args)
    _setup_logging(args.verbose, args.log_format, quiet=args.quiet, log_file=args.log_file)
    from hephaestus.automation.runtime_diagnostics import (
        require_virtual_environment,
        runtime_identity,
    )

    identity = runtime_identity()
    LOG.info("Runtime identity: %s", identity, extra={"runtime_identity": identity})
    if args.podman_machine:
        try:
            prepare_podman_machine(
                args.podman_machine,
                start_timeout_s=args.podman_start_timeout,
                health_timeout_s=args.podman_health_timeout,
            )
        except PodmanMachineError as exc:
            return _error_exit(args, str(exc), "Podman machine preflight failed.")
    selected = set(args.stages)
    if (
        not args.dry_run
        and sys.platform == "darwin"
        and selected.intersection({StageName.IMPLEMENTATION, StageName.MERGE_WAIT})
    ):
        try:
            require_virtual_environment(Path(sys.prefix))
        except RuntimeError as exc:
            return _error_exit(args, str(exc))
    active_roles = tuple(
        role
        for role, needed in (
            ("planner", {StageName.PLANNING, StageName.PLAN_REVIEW}),
            ("implementer", {StageName.IMPLEMENTATION}),
            ("reviewer", {StageName.PLAN_REVIEW, StageName.PR_REVIEW}),
        )
        if selected.intersection(needed)
    )
    try:
        args.agent, role_agents = resolve_role_agents(args, active_roles, resolver=resolve_agent)
    except ValueError as exc:
        build_parser(profile=profile).error(str(exc))
    for role, provider in role_agents.items():
        setattr(args, f"{role}_agent", provider)
    org, repos, error = _resolve_org_and_repos(args)
    if error:
        return _error_exit(args, error)
    streaming = args.org is not None and not args.repos and not (args.issues or args.prs)
    root_repos = repos
    if streaming:
        cwd_org, cwd_repo = _detect_cwd_repo(metadata_timeout=args.metadata_timeout)
        if cwd_org and cwd_repo and cwd_org.casefold() == org.casefold():
            root_repos = [cwd_repo]
    config = build_config(
        args,
        org,
        repos,
        root_repos=root_repos,
        repo_source_factory=(
            lambda shutdown: _iter_gh_repos(
                org, network_timeout=args.network_timeout, shutdown=shutdown
            )
        )
        if streaming
        else None,
    )
    if not repos and not streaming:
        return _error_exit(args, "Repo list is empty; nothing to do.", "empty repo list")
    if not args.dry_run and repos:
        _preflight_token_scopes(org, repos[0], timeout=args.gh_timeout)
    LOG.info("Queue stages: %s", ",".join(args.stages))
    from hephaestus.automation.pipeline.coordinator import run_pipeline
    from hephaestus.utils.terminal import install_sigtstp_only

    install_sigtstp_only()
    try:
        with event_log_lifecycle(
            config.event_log_path,
            retention_days=args.event_log_retention_days,
            retention_count=args.event_log_retention_count,
            dry_run=args.dry_run,
        ):
            return run_pipeline(config)
    except KeyboardInterrupt:
        if args.json:
            emit_json_status(130, message="interrupted")
        return 130
