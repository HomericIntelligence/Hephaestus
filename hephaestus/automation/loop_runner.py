"""Multi-repo, multi-stage automation loop driver.

Replaces ``scripts/run_automation_loop.sh``. Iterates over all
non-archived HomericIntelligence repos, running the 3-stage pipeline
(plan → implement → drive-green) ``--loops`` times per repo. Plan-review,
PR-review, and address-review are no longer standalone phases — the planner
owns its review loop and the implementer absorbs PR-review + thread-addressing
in-loop (#455/#468/#484).

The key correctness invariant — and the reason this replaces the bash
version — is that each phase is a plain ``subprocess.run`` call inside a
Python ``for`` loop. Phase N failing returns a ``PhaseResult(rc=N)`` and
control unconditionally proceeds to phase N+1. No shell-option landmine
(``set -e`` / ``set -m`` / subshell exec-optimization) can silently skip
the rest of the pipeline.

CLI is flag-compatible with the previous bash script so operator muscle
memory and any pinned callers keep working.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from hephaestus.agents.runtime import add_agent_argument, resolve_agent
from hephaestus.automation.ci_driver import _pr_is_failing
from hephaestus.automation.claude_timeouts import gh_cli_timeout
from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status
from hephaestus.config.paths import DEFAULT_PROJECTS_DIR, resolve_projects_dir
from hephaestus.constants import scripts_dir as _scripts_dir
from hephaestus.resilience.subprocess_resilience import resilient_call
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

LOG = logging.getLogger(__name__)


def _default_phase_timeout_s() -> float:
    """Return the default per-phase timeout in seconds.

    A phase that shells out to an external coding agent can stall indefinitely
    on a network hang; a non-``None`` default ensures the worker thread is
    always bounded even when the operator does not pass ``--phase-timeout``.
    Overridable via ``HEPH_PHASE_TIMEOUT`` (seconds). Mirrors the
    graceful-fallback contract of :mod:`hephaestus.automation.claude_timeouts`:
    a malformed env value logs a warning and falls back to the default rather
    than crashing at startup.

    The 7800s default lets the outer phase guard safely exceed the longest
    in-phase agent timeout (2h) so a healthy phase never trips it.
    """
    default = 7800
    raw = os.environ.get("HEPH_PHASE_TIMEOUT")
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        LOG.warning("Ignoring non-numeric HEPH_PHASE_TIMEOUT=%r — using default %ds", raw, default)
        return float(default)


# Canonical phase ordering. The pipeline collapsed from 6 phases to 3
# session-stable stages (#455/#468/#484): the former standalone review-plans,
# review-prs, and address-review phases are now in-loop steps of plan/implement
# (the planner owns its review loop; the implementer absorbs PR-review +
# thread-addressing). Index 0 = stage 1 (plan), index 2 = stage 3 (drive-green).
ALL_PHASES: tuple[str, ...] = (
    "plan",
    "implement",
    "drive-green",
)

FINAL_LOOP_ONLY_PHASES: frozenset[str] = frozenset({"drive-green"})

# DEFAULT_PROJECTS_DIR is re-exported from hephaestus.config.paths so existing
# tests that patch this module-level name continue to work. See #704: the
# projects root is now resolved at runtime via resolve_projects_dir() so that
# the ``PROJECTS_ROOT`` env var can override the historical ``~/Projects``
# default without code changes here.

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


def _parse_issue_list(value: str) -> list[int]:
    """Split a comma-separated issue list into positive integers."""
    issues: list[int] = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            issue = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"expected comma-separated issue numbers, got {item!r}"
            ) from exc
        if issue <= 0:
            raise argparse.ArgumentTypeError(
                f"issue numbers must be positive integers, got {issue}"
            )
        issues.append(issue)
    return issues


# drive-green used to be issue-gated, but #819 inverted its discovery to
# "any failing open PR" via a separate _count_failing_prs gate below. Set
# is kept (currently empty) so any future phase that genuinely needs an
# issue list has one place to opt in.
PHASES_REQUIRING_ISSUES: frozenset[str] = frozenset()

# Sentinel for cooperative shutdown on SIGINT/SIGTERM. Worker threads
# check this between phases so an in-flight subprocess can still finish
# but the next phase is skipped. threading.Event provides atomic, GIL-safe
# set/check semantics without the global-mutation footgun.
_SHUTDOWN_EVENT = threading.Event()


def _shutdown_requested() -> bool:
    return _SHUTDOWN_EVENT.is_set()


def _request_shutdown(signum: int, _frame: object) -> None:
    _SHUTDOWN_EVENT.set()
    LOG.warning("Signal %s received — requesting cooperative shutdown", signum)


@dataclass
class PhaseResult:
    """Outcome of a single phase invocation for a single repo+loop."""

    name: str
    rc: int = 0
    elapsed_s: float = 0.0
    skipped: bool = False
    skip_reason: str | None = None
    # If subprocess.run itself raised (OSError, TimeoutExpired, …), the
    # exception text lands here. The phase is treated as rc=1.
    error: str | None = None
    # Work units produced by this phase (e.g., issues planned or reviewed).
    # None means unknown (phase did not report). Used for loop convergence (#613).
    work_units: int | None = None

    @property
    def failed(self) -> bool:
        """True when the phase ran and returned a non-zero exit code."""
        return not self.skipped and self.rc != 0

    @property
    def produced_work(self) -> bool:
        """Whether this phase did convergence-relevant work.

        Unknown (un-instrumented) phases return True conservatively so the
        loop never early-exits on a phase it can't measure.
        """
        if self.skipped:
            return False
        if self.work_units is None:
            return True
        return self.work_units > 0


# Phases that count toward loop convergence. Future phases opting into
# convergence must be added here AND must call write_work_report. The former
# review-plans phase folded into ``plan`` (the planner now owns its review
# loop), so ``plan`` is the sole convergence signal.
_CONVERGENCE_PHASES: frozenset[str] = frozenset({"plan"})


@dataclass
class RepoResult:
    """Per-repo, per-loop outcome — collection of phase results."""

    repo: str
    loop_idx: int
    phases: list[PhaseResult] = field(default_factory=list)
    # Populated when the WORKER itself crashed (not a phase failure).
    runner_error: str | None = None

    @property
    def any_failure(self) -> bool:
        """True when any phase failed or the worker itself crashed."""
        return self.runner_error is not None or any(p.failed for p in self.phases)

    @property
    def produced_work(self) -> bool:
        """Whether any convergence-relevant phase produced work."""
        return any(p.produced_work for p in self.phases if p.name in _CONVERGENCE_PHASES)


def _summarize_loop(loop_results: list[RepoResult], loop_idx: int, elapsed_s: float) -> str:
    """Generate a one-line summary of loop execution for logs.

    Counts: planned (non-skipped plan stages), implemented (non-skipped
    implement stages), skipped (all skipped stages). Plan-review and PR-review
    are now in-loop steps of plan/implement, so they no longer have their own
    count.

    Args:
        loop_results: Results from all repos in this loop iteration.
        loop_idx: Loop iteration number (1-indexed).
        elapsed_s: Wall-clock seconds elapsed for the loop.

    Returns:
        A summary string like "loop 1: planned=5 implemented=3 skipped=2 elapsed=45s".

    """
    total_planned = 0
    total_implemented = 0
    total_skipped = 0

    for result in loop_results:
        for phase in result.phases:
            if phase.skipped:
                total_skipped += 1
            elif phase.name == "plan":
                total_planned += 1
            elif phase.name == "implement":
                total_implemented += 1

    elapsed = f"{elapsed_s:.0f}s"
    return (
        f"loop {loop_idx}: planned={total_planned} implemented={total_implemented} "
        f"skipped={total_skipped} elapsed={elapsed}"
    )


@dataclass
class LoopConfig:
    """Top-level CLI-derived configuration."""

    loops: int = 5
    max_workers: int = 3
    parallel_repos: int = 1
    phases: tuple[str, ...] = ALL_PHASES
    agent: str = "claude"
    issues: list[int] = field(default_factory=list)
    dry_run: bool = False
    no_advise: bool = False
    nitpick: bool = False
    allow_unsafe_phase_order: bool = False
    # ``model`` is the catch-all applied to every phase when set; per-phase
    # fields below take precedence over it. The /learn step is not a separate
    # knob — it inherits its parent phase's model at the call site.
    model: str = ""
    planner_model: str = ""
    reviewer_model: str = ""
    implementer_model: str = ""
    # Org is resolved at runtime from --org / --repos / cwd detection; no
    # hardcoded fallback. Always set by main() before ``run_loop``.
    org: str = ""
    projects_dir: Path = DEFAULT_PROJECTS_DIR
    # Per-phase timeout in seconds. Defaults to an env-overridable bound
    # (``HEPH_PHASE_TIMEOUT``) so a stalled subprocess can never hang a worker
    # thread indefinitely (#684). Passing ``--phase-timeout`` overrides it;
    # ``None`` explicitly disables the bound.
    phase_timeout_s: float | None = field(default_factory=_default_phase_timeout_s)


# ---------------------------------------------------------------------------
# Work report helpers (#613)
# ---------------------------------------------------------------------------


def _make_work_report_path(build_dir: str) -> str:
    """Create a temp work report file path under build/.

    Args:
        build_dir: Path to the build directory.

    Returns:
        Path to a new temp file for work reporting.

    """
    import tempfile

    build_path = Path(build_dir)
    build_path.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="work_report_", dir=str(build_path))
    os.close(fd)
    return path


def _read_work_report(path: str) -> int | None:
    """Read and parse work-unit count from a work report file.

    Args:
        path: Path to the work report file.

    Returns:
        The work-unit count, or None if the file is missing, empty, or malformed.

    """
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
        if not content:
            return None
        return int(content)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="hephaestus-automation-loop",
        description="Run the 3-stage automation pipeline across HomericIntelligence repos.",
    )
    p.add_argument("--dry-run", action="store_true", help="Pass --dry-run to every phase")
    p.add_argument("--loops", type=int, default=5, help="Number of loop iterations (default: 5)")
    p.add_argument(
        "--max-workers",
        type=int,
        default=3,
        help="Parallel workers per repo per phase (default: 3). Passed to each phase binary.",
    )
    p.add_argument(
        "--parallel-repos",
        type=int,
        default=1,
        help="Repos processed in parallel per loop iteration (default: 1)",
    )
    p.add_argument(
        "--phases",
        default=",".join(ALL_PHASES),
        help=f"Comma-separated subset of phases to run. Valid: {','.join(ALL_PHASES)}",
    )
    add_agent_argument(p)
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
        "--no-advise",
        action="store_true",
        help="Pass --no-advise to phases that support the advise preflight",
    )
    p.add_argument(
        "--nitpick",
        action="store_true",
        help="Pass --nitpick to review phases (reviewer emits nitpick comments)",
    )
    p.add_argument(
        "--allow-unsafe-phase-order",
        action="store_true",
        help="Silence dependency-ordering warnings when --phases skips a recommended predecessor",
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
            "the ``PROJECTS_ROOT`` env var (if set and existing), otherwise "
            f"falls back to ``{DEFAULT_PROJECTS_DIR}``."
        ),
    )
    p.add_argument(
        "--phase-timeout",
        type=float,
        default=_default_phase_timeout_s(),
        help=(
            "Per-phase timeout in seconds (default: HEPH_PHASE_TIMEOUT or "
            f"{int(_default_phase_timeout_s())}s). Pass 0 or a negative value to disable."
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
    p.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging")
    add_json_arg(p)
    add_version_arg(p)
    return p.parse_args(argv)


def _validate_phases(phases_csv: str) -> tuple[str, ...]:
    selected = tuple(p.strip() for p in phases_csv.split(",") if p.strip())
    invalid = [p for p in selected if p not in ALL_PHASES]
    if invalid:
        raise SystemExit(f"Unknown phase(s): {invalid}. Valid: {','.join(ALL_PHASES)}")
    return selected


def _phase_order_warnings(cfg: LoopConfig) -> list[str]:
    """Dependency-ordering safety warnings for the 3-stage pipeline.

    Plan-review and PR-review/address-review are no longer separate phases —
    the planner owns its review loop and the implementer absorbs PR-review +
    thread-addressing in-loop, so the only cross-stage ordering hazard left is
    running drive-green without implement (it would poll PRs not produced this
    invocation).
    """
    warnings: list[str] = []
    selected = set(cfg.phases)
    if "plan" in selected and "implement" not in selected:
        warnings.append(
            "--phases includes 'plan' but not 'implement'; this is planning-only "
            "and will not create implementation PRs"
        )
    if "drive-green" in selected and "implement" not in selected:
        warnings.append(
            "--phases includes 'drive-green' but not 'implement'; "
            "drive-green will run against PRs not touched this invocation"
        )
    return warnings


def _has_pending_drive_green_work(cfg: LoopConfig, repos: list[str]) -> bool:
    """Return True when drive-green still has PRs to drive across any repo.

    drive-green runs as the terminal phase AFTER the implement loop converges
    (it is in :data:`FINAL_LOOP_ONLY_PHASES`). Early-exit must therefore not
    fire while there is still drive-green work to do — but the mere fact that
    drive-green is *selected* is not, by itself, pending work. We gate on
    whether any open PR actually needs CI/merge attention, using the same
    ``_count_failing_prs`` predicate the driver uses so the gate cannot drift.

    When drive-green is not selected, there is no terminal work to wait for.
    Returns ``True`` (conservatively keep looping) when at least one repo has a
    failing/un-green PR.
    """
    if "drive-green" not in cfg.phases:
        return False
    # An explicit --issues scope pins the work set to specific PRs/issues, but
    # ``_count_failing_prs`` scans ALL open repo PRs (it has no issue filter), so
    # it's the wrong signal here — a clean repo-wide scan would wrongly say "no
    # pending work" and let the loop converge before the terminal drive-green
    # pass runs against the pinned issues. Keep looping (defer to --loops as the
    # bound) rather than early-exiting and abandoning the operator's explicit
    # drive-green work.
    if cfg.issues:
        return True
    return any(_count_failing_prs(cfg.org, repo) > 0 for repo in repos)


# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------


def _detect_cwd_repo() -> tuple[str | None, str | None]:
    """Return ``(org, repo_name)`` for the current working directory.

    Returns ``(None, None)`` when cwd is not inside a git repo or has no
    parseable github.com origin remote. ``org`` is parsed from
    ``git remote get-url origin``; ``repo_name`` is the basename of
    ``git rev-parse --show-toplevel``.
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return (None, None)
    repo: str | None = Path(top).name or None

    org: str | None = None
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        url = ""

    host = ""
    path = ""
    parsed = urlparse(url)
    if parsed.scheme:
        host = (parsed.hostname or "").rstrip(".").lower()
        path = parsed.path.lstrip("/")
    elif "@" in url and ":" in url:
        # SCP-like git remote, e.g. git@github.com:org/repo.git
        after_at = url.split("@", 1)[1]
        host_part, path_part = after_at.split(":", 1)
        host = host_part.rstrip(".").lower()
        path = path_part.lstrip("/")

    if host == "github.com":
        parts = path.split("/", 1)
        if len(parts) == 2:
            org = parts[0] or None

    return (org, repo)


def _gh_list_repos(org: str) -> list[str]:
    """Return non-archived, non-fork repos for ``org``."""
    try:
        out = subprocess.run(
            [
                "gh",
                "repo",
                "list",
                org,
                "--no-archived",
                "--json",
                "name,isArchived,isFork",
                "--limit",
                "200",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"gh repo list {org} timed out after {exc.timeout}s") from exc
    if out.returncode != 0:
        raise SystemExit(f"gh repo list {org} failed (rc={out.returncode}): {out.stderr.strip()}")
    try:
        entries = json.loads(out.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"gh repo list returned invalid JSON: {exc}") from exc
    return [
        e["name"] for e in entries if not e.get("isArchived", False) and not e.get("isFork", False)
    ]


def _list_open_issue_numbers(org: str, repo: str) -> list[int]:
    """Return ALL open issue numbers in ``org/repo``, sorted ascending.

    This is the loop's single canonical issue-discovery call: the result is
    passed down to the plan/implement child phases via ``--issues`` so they do
    NOT each re-run their own ``gh issue list``. The scope is ALL open issues
    (no ``@me`` author/assignee filter) so it matches the child phases'
    ``gh_list_open_issues`` semantics exactly — the loop's convergence and
    failing-PR gates then agree with the work the phases actually do.

    Sorted ascending so the implementer phase processes oldest-first. Returns
    an empty list on any failure (rate limit, auth error, timeout) so callers
    fall back safely.
    """
    try:
        out = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                f"{org}/{repo}",
                "--state",
                "open",
                "--limit",
                "500",
                "--json",
                "number",
                "--jq",
                ".[].number",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except subprocess.TimeoutExpired:
        return []
    if out.returncode != 0:
        return []
    return sorted(int(x) for x in out.stdout.split() if x.strip().isdigit())


def _count_open_issues(org: str, repo: str) -> int:
    """Return count of ``@me``-authored OR ``@me``-assigned open issues."""
    return len(_list_open_issue_numbers(org, repo))


def _count_failing_prs(org: str, repo: str) -> int:
    """Return count of open PRs that need drive-green attention.

    Uses the same gh pr list shape and the same _pr_is_failing predicate
    that ci_driver._discover_failing_prs uses, so the loop runner's SKIP
    gate cannot drift from the driver's work list. Bounded by gh's
    --limit 1000; cap-hit is logged but still treated as "has work" since
    the actual driver discovery handles the same cap consistently.

    Returns 0 on any gh / parse / timeout failure so the SKIP gate is
    fail-closed (we don't run the driver when we can't confirm work).
    """
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                f"{org}/{repo}",
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,isDraft,statusCheckRollup,mergeStateStatus",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0
    if out.returncode != 0:
        return 0
    try:
        pulls = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return 0
    if len(pulls) >= 1000:
        LOG.warning(
            "[%s] _count_failing_prs hit gh's 1000-PR cap; gate may undercount",
            repo,
        )
    return sum(1 for pr in pulls if _pr_is_failing(pr))


def _sort_repos_by_open_count(org: str, repos: list[str]) -> list[str]:
    """Order repos ascending by open-issue count (smallest backlog first)."""
    counted: list[tuple[int, int, str]] = []
    for idx, repo in enumerate(repos):
        counted.append((_count_open_issues(org, repo), idx, repo))
    counted.sort()
    return [name for _, _, name in counted]


def _resolve_repo_dir(projects_dir: Path, repo: str) -> Path:
    return projects_dir / repo


def _ensure_clone(org: str, repo: str, dest: Path) -> None:
    """Clone the repo into ``dest`` if not already present."""
    if (dest / ".git").exists():
        return
    LOG.info("Cloning %s/%s -> %s", org, repo, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Route the clone through resilient_call: a network blip retries with
    # backoff, while a true hang is bounded by NETWORK_TIMEOUT (#684).
    completed = resilient_call(
        subprocess.run,
        ["gh", "repo", "clone", f"{org}/{repo}", str(dest)],
        check=False,
        timeout=NETWORK_TIMEOUT,
        circuit_breaker_name="gh-repo-clone",
    )
    rc = completed.returncode
    if rc != 0:
        raise RuntimeError(f"gh repo clone {org}/{repo} failed (rc={rc})")


def _clone_missing_repos(org: str, repos: list[str], projects_dir: Path) -> None:
    """Sequentially clone any repos not already present.

    Done upfront — before any worker thread starts — so two threads with
    ``--parallel-repos > 1`` can never race on a missing clone. Matches
    the bash version's pre-loop clone pass at
    scripts/run_automation_loop.sh:326-336.
    """
    LOG.info("Cloning missing repos ...")
    for repo in repos:
        dest = projects_dir / repo
        if (dest / ".git").exists():
            LOG.debug("[%s] already cloned at %s", repo, dest)
            continue
        try:
            _ensure_clone(org, repo, dest)
        except Exception as exc:
            LOG.error("[%s] clone failed: %s — repo will be marked failed", repo, exc)


def _detect_remote_base_ref(repo: str, repo_dir: Path) -> str:
    """Return the remote default branch ref for ``repo_dir``."""
    try:
        symbolic = subprocess.run(
            ["git", "-C", str(repo_dir), "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            capture_output=True,
            text=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
        detected = symbolic.stdout.strip()
        if symbolic.returncode == 0 and detected:
            return detected
    except subprocess.TimeoutExpired:
        LOG.warning("[%s] default-branch detection timed out; trying fallback refs", repo)

    for candidate in ("origin/main", "origin/master"):
        try:
            verified = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--verify", candidate],
                capture_output=True,
                text=True,
                check=False,
                timeout=METADATA_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            continue
        if verified.returncode == 0:
            LOG.warning("[%s] using fallback base ref %s", repo, candidate)
            return candidate
    LOG.warning("[%s] could not detect base ref; falling back to origin/main", repo)
    return "origin/main"


def _local_ahead_count(repo: str, repo_dir: Path, base_ref: str) -> int:
    """Return the number of commits on HEAD that are not in ``base_ref``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", f"{base_ref}..HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("[%s] timed out checking local commits ahead of %s", repo, base_ref)
        return 0
    if result.returncode != 0:
        LOG.warning("[%s] could not check local commits ahead of %s", repo, base_ref)
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        LOG.warning("[%s] invalid ahead count for %s: %r", repo, base_ref, result.stdout)
        return 0


def _rebase_main(repo: str, repo_dir: Path) -> tuple[str, bool]:
    """Fetch + rebase the remote default branch.

    Returns ``(short_sha, fetch_ok)`` — a 7-char SHA and a flag indicating
    whether the network refresh succeeded. When ``fetch_ok`` is False the
    rebase ran against whatever the local clone already had; callers should
    surface the staleness in operator-facing logs but the SHA value itself
    remains a clean git hash (no suffix) because it is exported to phase
    subprocesses as ``HEPH_TRUNK_GITHASH`` and used for session naming
    (``hephaestus/automation/session_naming.py:181``). Adding a suffix
    would propagate the "stale" marker into every child session label and
    would also break any future caller that consumed the env var as a git
    ref. The staleness is conveyed via the second return value instead.
    """
    fetch_ok = True
    try:
        fetch_result = resilient_call(
            subprocess.run,
            ["git", "-C", str(repo_dir), "fetch", "origin", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=NETWORK_TIMEOUT,
            circuit_breaker_name="git-fetch",
        )
    except subprocess.TimeoutExpired:
        LOG.warning("[%s] git fetch timed out; rebasing against stale remote base", repo)
        fetch_ok = False
    else:
        # subprocess.run(check=False) does NOT raise on non-zero rc. macOS
        # sandbox denials surface here as rc=1 with stderr "cannot open
        # .git/FETCH_HEAD: Operation not permitted" (#993). Without this
        # inspection the loop logs the resulting trunk SHA as if the refresh
        # succeeded, masking the permission problem.
        rc = getattr(fetch_result, "returncode", 0)
        if rc != 0:
            stderr = (getattr(fetch_result, "stderr", "") or "").strip()
            LOG.warning(
                "[%s] git fetch failed (rc=%s); rebasing against stale remote base: %s",
                repo,
                rc,
                stderr or "<no stderr>",
            )
            fetch_ok = False
    base_ref = _detect_remote_base_ref(repo, repo_dir)
    local_ahead = _local_ahead_count(repo, repo_dir, base_ref)
    rb = subprocess.run(
        ["git", "-C", str(repo_dir), "rebase", base_ref, "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=METADATA_TIMEOUT,
    )
    if rb.returncode != 0:
        subprocess.run(
            ["git", "-C", str(repo_dir), "rebase", "--abort"],
            capture_output=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
        if local_ahead > 0:
            LOG.warning(
                "[%s] rebase failed with %s local commit(s) ahead of %s; preserving HEAD",
                repo,
                local_ahead,
                base_ref,
            )
        else:
            # No local commits are at risk, so restore a clean remote-base trunk
            # and keep the loop moving.
            LOG.warning("[%s] rebase failed, hard-resetting to %s", repo, base_ref)
            subprocess.run(
                ["git", "-C", str(repo_dir), "reset", "--hard", base_ref, "--quiet"],
                capture_output=True,
                check=False,
                timeout=METADATA_TIMEOUT,
            )
    sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--short=7", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=METADATA_TIMEOUT,
    )
    return (sha.stdout.strip() or "unknown", fetch_ok)


# ---------------------------------------------------------------------------
# Phase execution
# ---------------------------------------------------------------------------


def _resolve_phase_bin(phase: str) -> tuple[str, list[str]] | None:
    """Return ``(executable, leading_args)`` for ``phase``.

    Known phases fall back to this source checkout when console scripts are
    not installed on PATH. Unknown phases still return ``None``.
    """
    script_dir = _scripts_dir()
    if phase == "plan":
        bin_path = shutil.which("hephaestus-plan-issues")
        if bin_path:
            return (bin_path, [])
        return (sys.executable, ["-m", "hephaestus.automation.planner"])
    if phase == "implement":
        bin_path = shutil.which("hephaestus-implement-issues")
        if bin_path:
            return (bin_path, [])
        return (sys.executable, ["-m", "hephaestus.automation.implementer"])
    if phase == "drive-green":
        py = sys.executable
        return (py, [str(script_dir / "drive_prs_green.py")])
    return None


# Per-phase argv flag matrix. Mirrors the bash script's per-phase blocks
# at scripts/run_automation_loop.sh:444-576. Encoded as a table so each
# phase's flags are obvious at a glance and impossible to duplicate.
#
# - "worker_arg": pass this worker-count flag with N, or None for no worker flag
# - "no_ui":       pass `--no-ui`
# - "issues":      "explicit" passes loop-level --issues only when set;
#                  "open" passes the repo open-issue list when non-empty
# - "advise":      pass `--no-advise` when the loop-level flag is set
# - "follow_up_loop_threshold": if non-None, pass `--no-follow-up` when
#                  loop_idx >= threshold (bash equivalent: FOLLOW_UP_FLAG
#                  set on loop ≥ 3, scripts/run_automation_loop.sh:415-418)
_PHASE_FLAGS: dict[str, dict[str, object]] = {
    # plan/implement use "open": forward the loop-discovered open-issue list so
    # the child phase does NOT re-run its own ``gh issue list`` every phase /
    # every loop. drive-green stays "explicit" — #819 made it discover failing
    # PRs directly (not via the issue list), so it must NOT receive open_issues.
    "plan": {"worker_arg": "--parallel", "no_ui": False, "issues": "open", "advise": True},
    "implement": {
        "worker_arg": "--max-workers",
        "no_ui": True,
        "issues": "open",
        "advise": True,
        "nitpick": True,
        "follow_up_loop_threshold": 3,
    },
    "drive-green": {
        "worker_arg": "--max-workers",
        "no_ui": True,
        "issues": "explicit",
        "advise": True,
        "nitpick": True,
    },
}


def _build_phase_argv(
    phase: str,
    cfg: LoopConfig,
    open_issues: list[int],
    loop_idx: int = 1,
) -> list[str] | None:
    """Construct the full argv for ``phase``; ``None`` when binary unresolved."""
    resolved = _resolve_phase_bin(phase)
    if resolved is None:
        return None
    executable, leading = resolved
    argv: list[str] = [executable, *leading]

    flags = _PHASE_FLAGS[phase]

    # All phases support -v / --dry-run uniformly.
    argv.append("-v")
    argv.extend(["--agent", cfg.agent])
    if cfg.dry_run:
        argv.append("--dry-run")
    if cfg.no_advise and flags.get("advise"):
        argv.append("--no-advise")
    if cfg.nitpick and flags.get("nitpick"):
        argv.append("--nitpick")

    issue_mode = flags.get("issues")
    issue_numbers = cfg.issues if issue_mode == "explicit" else open_issues
    if issue_mode and issue_numbers:
        argv.append("--issues")
        argv.extend(str(n) for n in issue_numbers)

    worker_arg = flags.get("worker_arg")
    if isinstance(worker_arg, str):
        argv.extend([worker_arg, str(cfg.max_workers)])

    if flags["no_ui"]:
        argv.append("--no-ui")

    threshold = flags.get("follow_up_loop_threshold")
    if isinstance(threshold, int) and loop_idx >= threshold:
        argv.append("--no-follow-up")

    return argv


def _phase_env(
    cfg: LoopConfig,
    loop_idx: int,
    trunk_sha: str,
    phase: str,
) -> dict[str, str]:
    """Build the environment dict for a phase subprocess.

    ``HEPH_LOOP_INDEX`` / ``HEPH_TOTAL_LOOPS`` are scoped to the
    ``drive-green`` phase only — matching the bash script which set these
    inline solely for the drive-green subshell (scripts/run_automation_loop.sh:570).
    The ci_driver.py defense-in-depth check uses these to refuse to run
    outside the final loop; injecting them into other phases' envs is
    benign but a behavioral divergence from the bash version.
    """
    env = os.environ.copy()
    # Precedence per phase: explicit per-phase flag > catch-all --model > any
    # ambient HEPH_*_MODEL the operator exported > the phase default resolved
    # in the child. Only export when we have a value so we never clobber an
    # ambient env var with an empty string. --model also covers advise, so an
    # all-one-model run needs no env vars; /learn inherits its parent phase
    # downstream (see claude_models / the learn call sites).
    if planner := (cfg.planner_model or cfg.model):
        env["HEPH_PLANNER_MODEL"] = planner
    if reviewer := (cfg.reviewer_model or cfg.model):
        env["HEPH_REVIEWER_MODEL"] = reviewer
    if implementer := (cfg.implementer_model or cfg.model):
        env["HEPH_IMPLEMENTER_MODEL"] = implementer
    if cfg.model:
        env["HEPH_ADVISE_MODEL"] = cfg.model
    env["HEPH_TRUNK_GITHASH"] = trunk_sha
    project_root = str(Path(__file__).resolve().parents[2])
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"] = f"{project_root}{os.pathsep}{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = project_root
    if phase == "drive-green":
        env["HEPH_LOOP_INDEX"] = str(loop_idx)
        env["HEPH_TOTAL_LOOPS"] = str(cfg.loops)
    return env


def run_phase(
    repo: str,
    repo_dir: Path,
    phase: str,
    cfg: LoopConfig,
    loop_idx: int,
    open_issues: list[int],
    trunk_sha: str,
) -> PhaseResult:
    """Run one phase as a subprocess. Never raises — always returns a result.

    Exit codes, timeouts, and OS errors are normalized into ``PhaseResult``
    so the caller can unconditionally proceed to the next phase.
    """
    t0 = time.monotonic()
    argv = _build_phase_argv(phase, cfg, open_issues, loop_idx=loop_idx)
    if argv is None:
        return PhaseResult(
            name=phase,
            rc=127,
            skipped=False,
            error=f"could not resolve binary for phase {phase!r}",
            elapsed_s=time.monotonic() - t0,
        )

    LOG.info("[%s] phase %s START", repo, phase)
    env = _phase_env(cfg, loop_idx, trunk_sha, phase)

    # Create work report file path and inject into env (#613)
    build_dir = repo_dir / "build"
    work_report_path = _make_work_report_path(str(build_dir))
    env["HEPH_WORK_REPORT"] = work_report_path

    try:
        completed = subprocess.run(
            argv,
            cwd=str(repo_dir),
            env=env,
            timeout=cfg.phase_timeout_s,
            check=False,
        )
        rc = completed.returncode
    except subprocess.TimeoutExpired as exc:
        LOG.error("[%s] phase %s TIMEOUT after %.0fs", repo, phase, exc.timeout or 0.0)
        return PhaseResult(
            name=phase,
            rc=124,
            elapsed_s=time.monotonic() - t0,
            error=f"timeout after {exc.timeout}s",
        )
    except OSError as exc:
        LOG.error("[%s] phase %s OSError: %s", repo, phase, exc)
        return PhaseResult(
            name=phase,
            rc=126,
            elapsed_s=time.monotonic() - t0,
            error=f"OSError: {exc}",
        )
    finally:
        # Read work report and clean up
        work_units = None
        try:
            work_units = _read_work_report(work_report_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(work_report_path)

    elapsed = time.monotonic() - t0
    LOG.info("[%s] phase %s done in %.1fs (rc=%d)", repo, phase, elapsed, rc)
    return PhaseResult(name=phase, rc=rc, elapsed_s=elapsed, work_units=work_units)


# ---------------------------------------------------------------------------
# Per-repo orchestration
# ---------------------------------------------------------------------------


def process_repo(
    repo: str,
    loop_idx: int,
    cfg: LoopConfig,
) -> RepoResult:
    """Run the 3-stage pipeline for one repo. Never raises.

    Any exception inside the function (filesystem error, gh API explosion,
    unexpected programming bug) is caught and stashed in
    ``RepoResult.runner_error`` so the outer loop never sees a thread
    crash. Per-phase failures live in ``RepoResult.phases``.
    """
    result = RepoResult(repo=repo, loop_idx=loop_idx)
    try:
        return _process_repo_inner(repo, loop_idx, cfg, result)
    except Exception as exc:
        tb = traceback.format_exc()
        result.runner_error = f"{type(exc).__name__}: {exc}\n{tb}"
        LOG.error("[%s] runner crashed: %s", repo, exc)
        return result


def _process_repo_inner(
    repo: str,
    loop_idx: int,
    cfg: LoopConfig,
    result: RepoResult,
) -> RepoResult:
    # Clones are done in an upfront sequential pass in main() — see
    # _clone_missing_repos. process_repo runs concurrently across repos
    # (--parallel-repos > 1), so doing the clone here would race two
    # workers on the same gh-clone call when both target the same missing
    # repo. Bash equivalent: scripts/run_automation_loop.sh:326-336.
    repo_dir = _resolve_repo_dir(cfg.projects_dir, repo)
    if not (repo_dir / ".git").exists():
        result.runner_error = f"repo {repo} not cloned at {repo_dir}"
        return result

    LOG.info("── %s (loop %d) ──", repo, loop_idx)
    trunk_sha, fetch_ok = _rebase_main(repo, repo_dir)
    stale_suffix = "" if fetch_ok else " (stale)"
    LOG.info("[%s] trunk=%s%s", repo, trunk_sha, stale_suffix)

    # Open-issue discovery happens once per repo per loop. When the operator
    # scopes the loop explicitly, reuse that bounded list for child phases.
    open_issues = cfg.issues or _list_open_issue_numbers(cfg.org, repo)

    for phase in ALL_PHASES:
        if _shutdown_requested():
            LOG.warning("[%s] phase %s SKIP (shutdown requested)", repo, phase)
            result.phases.append(
                PhaseResult(name=phase, skipped=True, skip_reason="shutdown requested")
            )
            continue

        if phase not in cfg.phases:
            LOG.info("[%s] phase %s SKIP (disabled by --phases)", repo, phase)
            result.phases.append(
                PhaseResult(name=phase, skipped=True, skip_reason="disabled by --phases")
            )
            continue

        # Some phases only run on the configured final loop. Mirrors bash behavior.
        if phase in FINAL_LOOP_ONLY_PHASES and loop_idx != cfg.loops:
            LOG.info("[%s] phase %s SKIP (not final loop)", repo, phase)
            result.phases.append(
                PhaseResult(name=phase, skipped=True, skip_reason="not final loop")
            )
            continue

        if phase in PHASES_REQUIRING_ISSUES and not open_issues:
            LOG.info("[%s] phase %s SKIP (no open issues)", repo, phase)
            result.phases.append(
                PhaseResult(name=phase, skipped=True, skip_reason="no open issues")
            )
            continue

        if phase == "drive-green" and not cfg.issues:
            failing = _count_failing_prs(cfg.org, repo)
            if failing == 0:
                LOG.info("[%s] phase %s SKIP (no failing PRs)", repo, phase)
                result.phases.append(
                    PhaseResult(name=phase, skipped=True, skip_reason="no failing PRs")
                )
                continue
            LOG.info("[%s] phase %s has %d failing PR(s) — running", repo, phase, failing)

        phase_result = run_phase(
            repo=repo,
            repo_dir=repo_dir,
            phase=phase,
            cfg=cfg,
            loop_idx=loop_idx,
            open_issues=open_issues,
            trunk_sha=trunk_sha,
        )
        result.phases.append(phase_result)
        if phase_result.failed:
            LOG.warning(
                "[%s] phase %s FAILED rc=%d — continuing to next phase",
                repo,
                phase,
                phase_result.rc,
            )

    return result


# ---------------------------------------------------------------------------
# Outer loop
# ---------------------------------------------------------------------------


def _preflight_token_scopes(org: str, probe_repo: str) -> None:
    """Mirror the bash script's gh-token preflight."""
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{org}/{probe_repo}",
                "--jq",
                ".permissions",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"ERROR: `gh` token preflight for {org}/{probe_repo} timed out after {exc.timeout}s."
        ) from exc
    if out.returncode != 0:
        raise SystemExit(
            f"ERROR: `gh` cannot read {org}/{probe_repo} with the current token.\n"
            f"  {out.stderr.strip()}\n"
            "  Required scopes: repo (classic) OR "
            "Issues+PRs+Contents Read & Write (fine-grained).\n"
            "  Check with: gh auth status"
        )
    if out.stdout.strip() in {"null", "{}"}:
        LOG.warning(
            "Token permissions on %s/%s are empty; PR/issue writes will fail.",
            org,
            probe_repo,
        )


def _rate_limit_remaining() -> tuple[int, int] | None:
    """Return ``(remaining, reset_epoch)`` for the GraphQL budget, or None."""
    try:
        out = subprocess.run(
            ["gh", "api", "rate_limit"],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
        gql = data["resources"]["graphql"]
        return int(gql["remaining"]), int(gql["reset"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _maybe_sleep_for_rate_budget(loop_idx: int, total_loops: int) -> None:
    """Sleep until the upstream reset when GraphQL budget would be exhausted."""
    if os.environ.get("HEPHAESTUS_RATE_GUARD", "1") == "0":
        return
    if loop_idx >= total_loops:
        return
    threshold = int(os.environ.get("HEPHAESTUS_RATE_GUARD_THRESHOLD", "200"))
    rl = _rate_limit_remaining()
    if rl is None:
        return
    remaining, reset_epoch = rl
    if remaining >= threshold:
        return
    wait_s = max(0, reset_epoch - int(time.time()) + 5)
    if wait_s <= 0:
        return
    LOG.info(
        "Rate budget low (%d/%d GraphQL remaining); sleeping %ds until reset",
        remaining,
        threshold,
        wait_s,
    )
    # Cooperatively cancellable sleep so SIGINT during the wait still works.
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _shutdown_requested():
            LOG.warning("Rate-budget sleep cancelled by shutdown request")
            return
        time.sleep(min(1.0, deadline - time.monotonic()))


def run_loop(cfg: LoopConfig, repos: list[str]) -> list[RepoResult]:
    """Drive ``cfg.loops`` iterations across ``repos``. Returns flat result list.

    Early-exits when a full loop produces no convergence-relevant work (#613).

    Any single thread raising is contained — ``process_repo`` already
    swallows all exceptions, and ``Future.exception()`` is the second-line
    safety net for the rare case where the work submission itself dies.
    """
    all_results: list[RepoResult] = []

    for loop_idx in range(1, cfg.loops + 1):
        if _shutdown_requested():
            LOG.warning("Shutdown requested before loop %d — stopping", loop_idx)
            break

        LOG.info("━" * 60)
        LOG.info("▶ LOOP %d / %d", loop_idx, cfg.loops)
        LOG.info("━" * 60)

        loop_t0 = time.monotonic()
        loop_results: list[RepoResult] = []

        with ThreadPoolExecutor(
            max_workers=max(1, cfg.parallel_repos),
            thread_name_prefix="repo-",
        ) as pool:
            futures: dict[Future[RepoResult], str] = {
                pool.submit(process_repo, repo, loop_idx, cfg): repo for repo in repos
            }
            for fut, repo in futures.items():
                try:
                    result = fut.result()
                except Exception as exc:
                    LOG.error("[%s] future raised: %s", repo, exc)
                    result = RepoResult(
                        repo=repo,
                        loop_idx=loop_idx,
                        runner_error=f"future raised: {type(exc).__name__}: {exc}",
                    )
                loop_results.append(result)
                all_results.append(result)
                if result.any_failure:
                    LOG.warning(
                        "[%s] loop %d had failures (see phase rcs above)",
                        repo,
                        loop_idx,
                    )

        elapsed_s = time.monotonic() - loop_t0
        LOG.info("%s", _summarize_loop(loop_results, loop_idx, elapsed_s))
        LOG.info("Loop %d complete.", loop_idx)

        # Early-exit: the pipeline has converged when the full pass across ALL
        # repos produced zero new plan work, no failures, AND there is no
        # remaining drive-green work (every open PR is green / already
        # state:implementation-go). drive-green is the terminal phase that runs
        # after the implement loop converges, so we stop as soon as there are no
        # plans to make and no PRs left to drive — rather than spinning out the
        # full --loops just because drive-green is selected. --loops stays an
        # upper bound. (#614; convergence-on-no-pending-PR refinement.)
        if (
            loop_idx < cfg.loops
            and not _has_pending_drive_green_work(cfg, repos)
            and not any(r.any_failure for r in loop_results)
            and not any(r.produced_work for r in loop_results)
        ):
            LOG.info(
                "Early exit after loop %d/%d: full pass produced 0 new plans"
                " across all %d repo(s) and no open PR needs drive-green."
                " Remaining loops skipped.",
                loop_idx,
                cfg.loops,
                len(repos),
            )
            break

        _maybe_sleep_for_rate_budget(loop_idx, cfg.loops)

    # Report actual loops run (may be less than cfg.loops due to early exit)
    actual_loops = max((r.loop_idx for r in all_results), default=0)
    LOG.info(
        "✓ Completed %d of %d loop(s) across %d repo(s).",
        actual_loops,
        cfg.loops,
        len(repos),
    )
    return all_results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    from hephaestus.logging import setup_logging

    setup_logging(level=logging.DEBUG if verbose else logging.INFO)


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


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    """Console-script entry point. Returns the process exit code."""
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    agent = resolve_agent(args.agent)

    phases = _validate_phases(args.phases)

    # Resolve org + repos using a 4-branch precedence ladder. Org is
    # always set explicitly here — there is no silent fallback to a
    # hardcoded default.
    org, repos, err = _resolve_org_and_repos(args)
    if err:
        LOG.error("%s", err)
        if args.json:
            emit_json_status(1, message=err)
        return 1

    cfg = LoopConfig(
        loops=args.loops,
        max_workers=args.max_workers,
        parallel_repos=args.parallel_repos,
        phases=phases,
        agent=agent,
        issues=args.issues or [],
        dry_run=args.dry_run,
        no_advise=args.no_advise,
        nitpick=args.nitpick,
        allow_unsafe_phase_order=args.allow_unsafe_phase_order,
        model=args.model,
        planner_model=args.planner_model,
        reviewer_model=args.reviewer_model,
        implementer_model=args.implementer_model,
        org=org,
        projects_dir=resolve_projects_dir(args.projects_dir),
        # A non-positive --phase-timeout explicitly disables the bound; any
        # positive value (including the env-overridable default) applies it.
        phase_timeout_s=(
            args.phase_timeout if args.phase_timeout and args.phase_timeout > 0 else None
        ),
    )

    if not cfg.allow_unsafe_phase_order:
        for w in _phase_order_warnings(cfg):
            LOG.warning("%s (pass --allow-unsafe-phase-order to silence)", w)

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)
    # SIGHUP missing on Windows; not the target platform but be tolerant.
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGHUP, _request_shutdown)

    if not repos:
        LOG.error("Repo list is empty; nothing to do.")
        if args.json:
            emit_json_status(1, message="empty repo list")
        return 1

    if not cfg.dry_run:
        _preflight_token_scopes(cfg.org, repos[0])

    _clone_missing_repos(cfg.org, repos, cfg.projects_dir)

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
    LOG.info(
        "Models: planner=%s reviewer=%s implementer=%s advise=%s",
        cfg.planner_model or cfg.model or "<default>",
        cfg.reviewer_model or cfg.model or "<default>",
        cfg.implementer_model or cfg.model or "<default>",
        cfg.model or "<default>",
    )

    results = run_loop(cfg, repos)

    # Compute actual loops run (may be less than cfg.loops due to early exit)
    loops_run = max((r.loop_idx for r in results), default=0)

    failures = [r for r in results if r.any_failure]
    if failures:
        LOG.warning("%d/%d repo-loop results had failures", len(failures), len(results))
        if args.json:
            emit_json_status(
                1,
                repos=repos,
                loops_run=loops_run,
                failed_repos=[r.repo for r in failures],
            )
        return 1

    exit_code = 130 if _shutdown_requested() else 0
    if args.json:
        emit_json_status(
            exit_code,
            repos=repos,
            loops_run=loops_run,
            failed_repos=[],
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
