"""Real :class:`~hephaestus.automation.pipeline.stages.base.StageGitHub` adapter.

Coordinator-owned GitHub accessor (epic #1809, coordinator slice #1817). This
module is the ONE place where the pipeline's coordinator-neutral mutator names
(``add_labels``, ``upsert_plan_comment``, ``create_pr``, ...) are mapped onto
the real ``github_api`` / ``pr_manager`` / ``_review_utils`` helpers.

It deliberately lives OUTSIDE ``hephaestus/automation/pipeline/``: the
architecture guard (``tests/unit/automation/pipeline/test_pipeline_architecture``)
forbids ``github_api`` mutator imports in any ``pipeline/*`` module, so the
adapter is coordinator-side by construction — stages only ever see it through
``StageContext.github``.

Dry-run contract (``stages/base.py`` :class:`StageGitHub` docstring): dry-run
is honored INSIDE this accessor. Every mutator logs ``[dry-run] would ...``
and skips the underlying ``gh`` call when the adapter was built with
``dry_run=True``; reads always hit GitHub so classification stays truthful.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hephaestus.automation import github_api, pr_manager
from hephaestus.automation._review_phase import _is_automation_owned_thread
from hephaestus.automation._review_utils import (
    close_issue_as_covered,
    ensure_state_dir,
    find_merged_closing_pr,
    find_pr_for_issue,
    get_pr_head_branch,
)
from hephaestus.automation.arming_state import ArmingStateStore
from hephaestus.automation.ci_check_inspector import CICheckInspector
from hephaestus.automation.protocol import PLAN_COMMENT_MARKER
from hephaestus.automation.review_state import is_plan_review_go
from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ALL_STATE_LABELS,
    STATE_SKIP,
)
from hephaestus.constants import read_timeout_env
from hephaestus.github.client import gh_call

logger = logging.getLogger(__name__)


def rate_limit_remaining() -> tuple[int, int] | None:
    """Return ``(remaining, reset_epoch)`` for the GraphQL budget, or ``None``.

    Ported from ``loop_runner._rate_limit_remaining`` for the coordinator's
    non-blocking rate gate (the legacy helper feeds a *sleeping* guard, which
    is fatal for a single coordinator thread — the pipeline timer-parks
    instead, see ``coordinator._rate_budget_ok``).
    """
    try:
        out = gh_call(["api", "rate_limit"])
    except (subprocess.SubprocessError, RuntimeError, OSError):
        return None
    try:
        data = json.loads(out.stdout)
        gql = data["resources"]["graphql"]
        return int(gql["remaining"]), int(gql["reset"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def rate_budget_ok(now_epoch: float | None = None) -> tuple[bool, float]:
    """Non-blocking port of ``loop_runner._maybe_sleep_for_rate_budget``.

    Args:
        now_epoch: Current epoch seconds (injectable for tests).

    Returns:
        ``(ok, park_delay_s)``. ``ok`` is False when the GraphQL budget is
        below ``HEPHAESTUS_RATE_GUARD_THRESHOLD`` (default 200) and the
        ``HEPHAESTUS_RATE_GUARD`` env gate is enabled; ``park_delay_s`` is the
        seconds until the upstream reset (+5s slack, mirroring the legacy
        guard), 0.0 when ``ok``.

    """
    if os.environ.get("HEPHAESTUS_RATE_GUARD", "1") == "0":
        return True, 0.0
    threshold = read_timeout_env("HEPHAESTUS_RATE_GUARD_THRESHOLD", 200)
    rl = rate_limit_remaining()
    if rl is None:
        return True, 0.0
    remaining, reset_epoch = rl
    if remaining >= threshold:
        return True, 0.0
    now = time.time() if now_epoch is None else now_epoch
    return False, max(0.0, reset_epoch - now + 5.0)


class PipelineGitHub:
    """Coordinator-owned GitHub accessor implementing ``StageGitHub``.

    Read surface delegates to the existing helpers verbatim; the mutator
    surface maps the coordinator-neutral names onto ``github_api`` /
    ``pr_manager`` / ``_review_utils`` mutators, honoring dry-run inside each
    mutator (log-and-skip) per the ``StageGitHub`` protocol docstring.
    """

    def __init__(self, org: str, *, dry_run: bool = False, repo_root: Path | None = None) -> None:
        """Initialize the accessor.

        Args:
            org: GitHub organization (used for logging context only; the
                underlying helpers resolve the repo from their cwd exactly as
                the legacy phases do).
            dry_run: When True, every mutator logs-and-skips.
            repo_root: Repo checkout root anchoring the drive-green arming
                state dir (defaults to the current working directory).

        """
        self.org = org
        self.dry_run = dry_run
        self._repo_root = repo_root or Path.cwd()
        self._arming = ArmingStateStore(lambda: ensure_state_dir(self._repo_root))
        self._inspector = CICheckInspector(
            get_pr_branch=lambda pr: get_pr_head_branch(pr) or "",
            # Reads stay live even under pipeline dry-run so CI classification
            # is truthful; only mutators log-and-skip.
            options_provider=lambda: SimpleNamespace(dry_run=False),
        )

    def _skip(self, what: str) -> bool:
        """Return True (and log) when dry-run should skip a mutation."""
        if self.dry_run:
            logger.info("[dry-run] would %s", what)
            return True
        return False

    # -- read surface --------------------------------------------------------

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue JSON (``github_api.issues.gh_issue_json``)."""
        return github_api.gh_issue_json(issue_number)

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Return the merged PR closing this issue (``_review_utils``)."""
        return find_merged_closing_pr(issue_number)

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return an open PR covering this issue (``_review_utils``)."""
        return find_pr_for_issue(issue_number)

    def has_existing_plan(self, issue_number: int) -> bool:
        """Labels-first plan gate incl. comment-scan backfill (``is_plan_review_go``)."""
        return bool(is_plan_review_go(issue_number))

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Return the PR's head branch (``_review_utils.get_pr_head_branch``)."""
        return get_pr_head_branch(pr_number)

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return ``(has_go, has_no_go)`` (``pr_manager``)."""
        return pr_manager.pr_has_implementation_state_label(pr_number)

    def count_unresolved_threads(self, pr_number: int) -> tuple[int, int]:
        """Return ``(automation_unresolved, human_unresolved)`` thread counts.

        Mirrors ``_review_phase._count_unresolved_threads_blocking_go``
        (#1152): resolves nothing; fails open to ``(0, 0)`` on a fetch error
        so a transient API blip cannot strand a GO.
        """
        try:
            threads = github_api.gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        except Exception as exc:
            logger.warning("PR #%s: could not list unresolved threads: %s", pr_number, exc)
            return (0, 0)
        if not threads:
            return (0, 0)
        current_login = github_api.gh_current_login()
        automation = sum(1 for t in threads if _is_automation_owned_thread(t, current_login))
        return (automation, len(threads) - automation)

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Return the merge_wait PR-state read, or ``None`` on failure.

        Re-housed from ``ci_driver.CIDriver._gh_pr_state``: one ``gh pr view``
        returning ``{state, headRefOid, mergedAt, mergeStateStatus,
        baseRefName}``.
        """
        try:
            result = gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "state,headRefOid,mergedAt,mergeStateStatus,baseRefName",
                ]
            )
            data = json.loads(result.stdout or "{}")
            return data if isinstance(data, dict) else None
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            logger.warning("PR #%s: gh_pr_state read failed: %s", pr_number, exc)
            return None

    def failing_required_check_names(self, pr_number: int) -> list[str]:
        """Names of required checks currently failing (``CICheckInspector``)."""
        return self._inspector.failing_required_check_names(pr_number)

    def pending_required_check_names(self, pr_number: int) -> list[str]:
        """Names of required checks still in flight (``CICheckInspector``)."""
        return self._inspector.pending_required_check_names(pr_number)

    def pr_checks(self, pr_number: int) -> list[dict[str, Any]]:
        """All checks for the PR (``gh_pr_checks``)."""
        return github_api.gh_pr_checks(pr_number, dry_run=False)

    def pr_is_genuinely_stuck(self, pr_number: int) -> bool:
        """Return True iff the PR cannot merge without manual action (``pr_manager``)."""
        return pr_manager.pr_is_genuinely_stuck(pr_number)

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Return True when the post-merge ``/learn`` is already terminal.

        Mirrors ``ci_driver.CIDriver._learn_record_terminal`` over the issue's
        arming record: captured/succeeded timestamps or a terminal
        ``learn_status`` mean ``/learn`` must never fire again (#848).
        """
        record = self._arming.load(issue_number) or {}
        if record.get("learn_captured_at") or record.get("learn_succeeded_at"):
            return True
        return str(record.get("learn_status") or "").lower() in {"succeeded", "failed"}

    # -- mutator surface (dry-run honored here) -------------------------------

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably add labels (``gh_issue_add_labels``)."""
        if self._skip(f"add labels {labels} to #{issue_number}"):
            return
        github_api.gh_issue_add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably remove labels (``gh_issue_remove_labels``)."""
        if self._skip(f"remove labels {labels} from #{issue_number}"):
            return
        github_api.gh_issue_remove_labels(issue_number, labels)

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Close the issue as covered by a merged PR (``_review_utils``)."""
        if self._skip(f"close #{issue_number} as covered by PR #{pr_number}"):
            return
        close_issue_as_covered(issue_number, pr_number)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Upsert the single plan comment keyed on ``PLAN_COMMENT_MARKER``."""
        if self._skip(f"upsert plan comment on #{issue_number}"):
            return
        github_api.gh_issue_upsert_comment(issue_number, PLAN_COMMENT_MARKER, body)

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        ``find_pr_for_issue`` first (reuse an existing open PR), then
        ``gh_pr_create`` with the *given* title/body — NOT
        ``pr_manager.ensure_pr_created``, which would discard the stage's
        composed body (protocol docstring). Dry-run returns 0 (no PR).
        """
        existing = find_pr_for_issue(issue_number)
        if existing:
            return existing
        if self._skip(f"create PR for #{issue_number} from {branch!r}"):
            return 0
        return github_api.gh_pr_create(branch, title, body)

    def post_pr_comment(self, pr_number: int, body: str) -> None:
        """Post an explanatory PR comment (``gh_issue_comment`` channel)."""
        if self._skip(f"post comment on PR #{pr_number}"):
            return
        github_api.gh_issue_comment(pr_number, body)

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Apply ``state:implementation-no-go`` (``pr_manager``)."""
        if self._skip(f"mark PR #{pr_number} implementation-no-go"):
            return
        pr_manager.mark_pr_implementation_no_go(pr_number)

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Apply ``state:implementation-go`` (``pr_manager``)."""
        if self._skip(f"mark PR #{pr_number} implementation-go"):
            return
        pr_manager.mark_pr_implementation_go(pr_number)

    def defer_auto_merge(self, pr_number: int) -> None:
        """Keep auto-merge disabled until implementation GO (``pr_manager``)."""
        if self._skip(f"defer auto-merge on PR #{pr_number}"):
            return
        pr_manager.ensure_pr_auto_merge_deferred(pr_number)

    def arm_auto_merge(self, pr_number: int) -> None:
        """Arm squash auto-merge after implementation GO (``pr_manager``)."""
        if self._skip(f"arm auto-merge on PR #{pr_number}"):
            return
        pr_manager.enable_auto_merge_after_implementation_go(pr_number)

    def post_review_threads(
        self, pr_number: int, threads: list[dict[str, Any]], summary: str
    ) -> list[str]:
        """Post surviving review threads (``gh_pr_review_post``)."""
        if self._skip(f"post {len(threads)} review thread(s) on PR #{pr_number}"):
            return []
        return github_api.gh_pr_review_post(pr_number, threads, summary)

    def arm_drive_green(self, issue_number: int, pr_number: int, head_sha: str) -> None:
        """Persist the drive-green arming record (``ArmingStateStore.save``).

        Record shape mirrors ``ci_driver.CIDriver._arm_drive_green``; an
        already-terminal record is never overwritten (its learn evidence is
        the /learn dedupe key).
        """
        if self._skip(f"arm drive-green record for #{issue_number} (PR #{pr_number})"):
            return
        if self.drive_green_learn_terminal(issue_number):
            return
        self._arming.save(
            issue_number,
            {
                "pr_number": pr_number,
                "pr_head_branch": get_pr_head_branch(pr_number) or "",
                "head_sha_at_arming": head_sha,
                "armed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "learn_attempted_at": None,
                "learn_captured_at": None,
                "learn_status": None,
                "learn_succeeded_at": None,
            },
        )

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Record the post-merge ``/learn`` outcome on the arming record.

        Mirrors ``post_merge_processor.mark_drive_green_learn_result`` (minus
        the session-evidence enrichment, which stays with the legacy driver
        until the cutover issue): written before FINISH_PASS so a restart can
        never replay ``/learn`` for the same merged PR.
        """
        if self._skip(f"record drive-green learn result for #{issue_number}"):
            return
        record = self._arming.load(issue_number) or {}
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["learn_attempted_at"] = timestamp
        if succeeded:
            record["learn_status"] = "succeeded"
            record["learn_succeeded_at"] = timestamp
            record["learn_captured_at"] = timestamp
        else:
            record["learn_status"] = "failed"
            record["learn_succeeded_at"] = None
            record["learn_captured_at"] = None
        self._arming.save(issue_number, record)

    # -- repo-stage surface (#1817) -------------------------------------------

    def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
        """Tag epics ``state:skip`` via the sanctioned chokepoint.

        The ONE seeding write (doc row "Epic tagging is the one seeding
        write; done BEFORE excluding"), executed by the coordinator through
        ``github_api.skip_epics``.
        """
        if self._skip(f"tag epics {sorted(epics_labels)} {STATE_SKIP}"):
            return
        github_api.skip_epics(epics_labels)

    def ensure_state_labels(self) -> None:
        """Ensure the ``state:*`` label vocabulary exists on the repo.

        Repo-stage step 1 [M] (doc section 1): idempotent
        ``_ensure_labels_exist`` over the full ``state_labels`` vocabulary.
        """
        wanted = [*ALL_STATE_LABELS, *ALL_IMPLEMENTATION_STATE_LABELS, STATE_SKIP]
        if self._skip(f"ensure state labels exist: {wanted}"):
            return
        github_api._ensure_labels_exist(wanted)
