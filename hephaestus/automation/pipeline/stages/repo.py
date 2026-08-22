"""Repo stage: discover and classify issues from a GitHub repository (epic #1809).

Binding contract: docs/architecture.md §5.1 "repo".

States: ENTER -> CLONE_WAIT -> LABELS -> DISCOVER -> SOURCE.

Steps:

1. [W:G] CLONE_WAIT: ``GitJob(op="clone")`` when the checkout is missing, then
   ``GitJob(op="sync_checkout")``; or ``GitJob(op="sync_checkout")`` directly
   when it already exists. Synchronization validates the expected remote and
   fast-forwards only a clean default-branch checkout. Both operations are
   logged-skipped under dry-run — the
   coordinator's ``_submit`` asserts no job is ever submitted in dry-run.
   Budget ``clone`` = 2; exhaustion -> finished(fail).
2. [M] LABELS: ``ctx.github.ensure_state_labels()`` only after checkout
   synchronization succeeds.
3. [M] DISCOVER: initialize one page-at-a-time metadata cursor. It never
   materializes a list of classified products.
4. [M] SOURCE: the coordinator transfers the bounded cursor to its fair
   C-capped registry, then admits one metadata row only when an issue can own
   a live-work permit. Epics are tagged ``state:skip`` [durable, BEFORE
   excluding].

Discovery seams (``_repo_manager`` / ``_seeding`` module attributes) mirror
the ``loop_runner._admission`` seam pattern so unit tests patch the reads
without network I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any, TypeGuard

import hephaestus.automation.loop_repo_manager as _repo_manager
import hephaestus.automation.pipeline.seeding as _seeding
from hephaestus.automation.arming_state import LearningJournalStore
from hephaestus.automation.issue_waves import (
    WAVE_LEASE_PAYLOAD,
    IssueWaveError,
    IssueWaveStore,
    WaveAdmissionPlan,
    WaveLease,
    is_full_commit_sha as is_wave_commit_sha,
)

from .base import (
    GIT_JOB_TIMEOUT_S,
    Continue,
    Disposition,
    GitJob,
    ItemKind,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageName,
    StageOutcome,
    StepResult,
    WorkItem,
    stage_timeout,
)

logger = logging.getLogger(__name__)

# Direct CLI scopes bootstrap one reusable checkout before any source reads.
# The SHA travels only with that bootstrap's cursors; normal repository
# discovery retains per-issue fresh-trunk semantics.
DIRECT_SCOPE_BOOTSTRAP_KEY = "_direct_scope_bootstrap"
DIRECT_SCOPE_BASE_SHA_KEY = "_direct_scope_base_sha"
# The coordinator creates one UUID4 hex value per explicit issue cursor.  A
# fresh direct run carries it through to its writer worktree so a preserved
# failed checkout cannot block a later run for the same issue.
DIRECT_SCOPE_WORKTREE_NONCE_KEY = "_direct_scope_worktree_nonce"
# A direct-scope worker returns this receipt only after it has atomically
# reserved the remote implementation branch.  It remains on the item until a
# coordinator-owned push publishes a commit or the Finished stage releases the
# still-unused reservation.
DIRECT_SCOPE_RESERVATION_KEY = "_direct_scope_reservation"
# Typed result retained only between the direct worktree reservation worker
# and implementation's next step.  A confirmed remote collision is terminal
# rather than a generic infrastructure retry: retrying cannot make a branch
# owned by another run safe to overwrite.
DIRECT_SCOPE_RESERVATION_COLLISION_KEY = "_direct_scope_reservation_collision"
# The remote reservation is already released after a direct no-op.  Finished
# uses this receipt to compare-and-delete the now-detached local branch only
# after removing its worktree.
DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY = "_direct_scope_local_branch_cleanup"
SYNCED_MAIN_SHA_KEY = "_synced_default_branch_sha"
WAVE_PLAN_KEY = "_issue_wave_admission_plan"
WAVE_ANCESTRY_VERIFIED_KEY = "_issue_wave_ancestry_verified"
WAVE_ANCESTRY_ERROR_KEY = "_issue_wave_ancestry_error"


def is_full_commit_sha(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is a full SHA-1 or SHA-256 commit id."""
    return bool(
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


def is_direct_scope_worktree_nonce(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is the coordinator's UUID4 hex token."""
    return bool(
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _repo_checkout_path(item: WorkItem, ctx: StageContext) -> Path:
    """Return the effective local checkout path for the repo item.

    Coordinator contexts always provide a per-repository ``repo_root``.  The
    projects-root fallback keeps legacy lightweight stage contexts compatible
    while making an explicit noncanonical root authoritative for clone checks.
    """
    repo_root = Path(str(ctx.paths.repo_root))
    projects_dir = Path(str(ctx.paths.projects_dir))
    return projects_dir / item.repo if repo_root == projects_dir else repo_root


@dataclass
class RepoIssueSource:
    """Bounded discovery cursor retained by the coordinator after repo setup.

    ``pending`` holds at most the one metadata row whose possible issue entry
    is waiting for a coordinator-owned queue slot.  The iterator itself holds
    at most one fetched GitHub page.  In particular, this object never stores
    a classified ``SeedEntry``/``WorkItem`` product list.
    """

    metadata: Iterator[dict[str, Any]]
    pending: dict[str, Any] | None = None
    seeded_count: int = 0
    wave_lease: WaveLease | None = None
    one_pass: bool = False


class RepoStage(Stage):
    """Repo discovery stage that initializes the coordinator-owned source."""

    kind = StageName.REPO

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Proceed to checkout preparation before any durable label work.

        Args:
            item: The repo work item.
            ctx: Stage context with the GitHub accessor.

        Returns:
            None to proceed with step().

        """
        del item, ctx
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next step for the current repo-item state.

        Args:
            item: The repo work item.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if item.state in ("", "ENTER"):
            return Continue(next_state="CLONE_WAIT")

        if item.state == "CLONE_WAIT":
            return self._clone_or_skip(item, ctx)

        if item.state == "WAVE_ADMIT":
            return self._wave_admit(item, ctx)

        if item.state == "WAVE_ADMIT_AFTER_VERIFY":
            return self._wave_admit_after_verify(item, ctx)

        if item.state == "LABELS":
            # Direct recovery validates its active-wave membership in the
            # coordinator before this state is allowed to mutate labels.
            if item.payload.get(DIRECT_SCOPE_BOOTSTRAP_KEY, False):
                return StageOutcome(
                    Disposition.FINISH_PASS,
                    note="direct scope checkout synchronized",
                )
            ctx.github.ensure_state_labels()
            return Continue(next_state="DISCOVER")

        if item.state == "DISCOVER":
            if "_repo_issue_source" in item.payload:
                return Continue(next_state="SOURCE")
            return self._discover(item, ctx)

        if item.state == "SOURCE":
            # Coordinator._run_item externalizes this cursor into its fair
            # registry. Returning Continue retains stage-protocol compatibility
            # for direct unit calls while avoiding an eager product list.
            return Continue(next_state="SOURCE")

        return StageOutcome(Disposition.FINISH_FAIL, note=f"unknown state: {item.state}")

    def _wave_store(self, item: WorkItem, ctx: StageContext) -> IssueWaveStore:
        """Build the repository-scoped checkpoint accessor."""
        return IssueWaveStore(Path(str(ctx.paths.repo_root)), ctx.org, item.repo)

    @staticmethod
    def _wave_metadata(issue_numbers: tuple[int, ...]) -> Iterator[dict[str, Any]]:
        """Yield the exact sealed identifiers without re-discovering GitHub."""
        for number in issue_numbers:
            yield {"number": number, "labels": [], "title": ""}

    def _wave_admit(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Read the checkpoint and, when required, request ancestry proof."""
        if item.payload.get(DIRECT_SCOPE_BOOTSTRAP_KEY, False):
            return Continue(next_state="LABELS")
        main_sha = item.payload.get(SYNCED_MAIN_SHA_KEY)
        requested = getattr(ctx.config, "issue_limit", None)
        repo_root = Path(str(ctx.paths.repo_root))
        if (
            ctx.dry_run
            and not is_wave_commit_sha(main_sha)
            and requested is None
            and not repo_root.is_dir()
        ):
            # A preview without a materialized checkout cannot inspect a
            # repository checkpoint. Preserve ordinary dry-run behavior, but
            # fail closed when the operator explicitly requested a wave.
            return Continue(next_state="LABELS")
        try:
            store = self._wave_store(item, ctx)
            if ctx.dry_run and not is_wave_commit_sha(main_sha):
                # A dry-run has no truthful checkout SHA.  It may preserve the
                # ordinary absent-checkpoint behavior, but cannot bypass an
                # existing staged rollout.
                if store.load() is not None:
                    raise IssueWaveError(
                        "dry-run cannot verify an issue-wave checkpoint without synchronized main"
                    )
                return Continue(next_state="LABELS")
            plan = store.plan_admission(str(main_sha or ""), requested)
        except IssueWaveError as exc:
            return StageOutcome(Disposition.FINISH_FAIL, note=str(exc))
        item.payload[WAVE_PLAN_KEY] = plan
        if plan.mode == "ordinary":
            return Continue(next_state="LABELS")
        if plan.requires_ancestry:
            if ctx.dry_run:
                return StageOutcome(
                    Disposition.FINISH_FAIL,
                    note="[dry-run] would verify prior issue-wave ancestry before advancing",
                )
            item.payload["_issue_wave_ancestry_job"] = True
            return JobRequest(
                job=GitJob(
                    repo=item.repo,
                    op="verify_issue_wave_ancestry",
                    timeout_s=stage_timeout(ctx, "metadata", GIT_JOB_TIMEOUT_S),
                    kwargs={
                        "repo_root": str(ctx.paths.repo_root),
                        "main_sha": str(main_sha),
                        "ancestor_shas": plan.ancestor_shas,
                    },
                    descr=f"verify issue-wave ancestry for {ctx.org}/{item.repo}",
                ),
                on_done_state="WAVE_ADMIT_AFTER_VERIFY",
            )
        return self._wave_admit_after_verify(item, ctx)

    def _wave_admit_after_verify(  # noqa: C901
        self, item: WorkItem, ctx: StageContext
    ) -> StepResult:
        """Validate prior-wave facts and seal or resume the exact source."""
        plan = item.payload.get(WAVE_PLAN_KEY)
        if not isinstance(plan, WaveAdmissionPlan):
            return StageOutcome(Disposition.FINISH_FAIL, note="issue-wave admission plan missing")
        error = item.payload.pop(WAVE_ANCESTRY_ERROR_KEY, None)
        if error:
            return StageOutcome(
                Disposition.FINISH_FAIL,
                note=str(error or "issue-wave ancestry verification failed"),
            )
        ancestry_verified = bool(item.payload.pop(WAVE_ANCESTRY_VERIFIED_KEY, False))
        if plan.requires_ancestry and not ancestry_verified:
            return StageOutcome(
                Disposition.FINISH_FAIL,
                note="issue-wave ancestry verification proof is missing",
            )
        main_sha = str(item.payload.get(SYNCED_MAIN_SHA_KEY) or plan.current_main_sha)
        try:
            store = self._wave_store(item, ctx)
            if plan.mode == "audit":
                if plan.lease is None or plan.checkpoint is None:
                    raise IssueWaveError("completed issue-wave audit lease is missing")
                record = plan.checkpoint.current_wave
                facts = {
                    number: _seeding.seed_issue_from_github(number, ctx.github)
                    for number in record.issue_numbers
                }
                store.validate_prior_wave_facts(plan.lease, facts)
                if plan.requires_ancestry:
                    store.verify_prior_wave(
                        self._require_lease(plan),
                        current_main_sha=main_sha,
                        ancestry_verified=True,
                        facts_by_issue=facts,
                    )
                if plan.checkpoint.status == "active":
                    store.complete_rollout(plan.lease, current_main_sha=main_sha)
                return StageOutcome(Disposition.FINISH_PASS, note=plan.diagnostic or "audit-only")
            if plan.mode == "resume":
                lease = self._require_lease(plan)
                if plan.requires_ancestry:
                    if plan.checkpoint is None:
                        raise IssueWaveError("resumed issue-wave checkpoint is missing")
                    receipt_numbers = tuple(
                        receipt.issue_number
                        for receipt in plan.checkpoint.current_wave.merge_receipts
                    )
                    facts = {
                        number: _seeding.seed_issue_from_github(number, ctx.github)
                        for number in receipt_numbers
                    }
                    store.validate_active_wave_facts(lease, facts)
            else:
                prior = plan.checkpoint.current_wave if plan.checkpoint is not None else None
                if prior is not None:
                    facts = {
                        number: _seeding.seed_issue_from_github(number, ctx.github)
                        for number in prior.issue_numbers
                    }
                    store.validate_prior_wave_facts(prior.lease(ctx.org, item.repo), facts)
                    if plan.requires_ancestry:
                        store.verify_prior_wave(
                            prior.lease(ctx.org, item.repo),
                            current_main_sha=main_sha,
                            ancestry_verified=True,
                            facts_by_issue=facts,
                        )
                selected = self._select_wave_issues(item, ctx, plan.requested_limit)
                if ctx.dry_run:
                    lease = WaveLease(
                        org=ctx.org,
                        repo=item.repo,
                        wave_index=plan.wave_index or 0,
                        limit=plan.requested_limit,
                        issue_numbers=selected,
                        base_main_sha=main_sha,
                        nonce=f"dryrun-{item.repo}-{plan.wave_index or 0}",
                    )
                else:
                    lease = store.seal_selection(plan, selected)
            item.payload[WAVE_LEASE_PAYLOAD] = lease
            item.payload["_repo_issue_source"] = RepoIssueSource(
                metadata=self._wave_metadata(lease.issue_numbers),
                wave_lease=lease,
                one_pass=True,
            )
            return Continue(next_state="LABELS")
        except IssueWaveError as exc:
            return StageOutcome(Disposition.FINISH_FAIL, note=str(exc))
        except Exception as exc:
            logger.warning("repo:%s: issue-wave admission failed: %s", item.repo, exc)
            return StageOutcome(Disposition.FINISH_FAIL, note=f"issue-wave admission failed: {exc}")

    @staticmethod
    def _require_lease(plan: WaveAdmissionPlan) -> WaveLease:
        """Return a plan lease or raise a precise internal admission error."""
        if plan.lease is None:
            raise IssueWaveError("issue-wave admission lease missing")
        return plan.lease

    def _select_wave_issues(
        self, item: WorkItem, ctx: StageContext, limit: int | None
    ) -> tuple[int, ...]:
        """Select the first eligible open issues in the repository's source order."""
        selected: list[int] = []
        for metadata in _repo_manager._iter_open_issue_meta(ctx.org, item.repo):
            number = int(metadata["number"])
            facts = _seeding.seed_issue_from_github(number, ctx.github)
            entry = _seeding.seed_entry_from_facts(facts)
            if facts.issue_is_closed or entry.stage is None or entry.stage is StageName.FINISHED:
                continue
            selected.append(number)
            if limit is not None and len(selected) >= limit:
                break
        return tuple(selected)

    def _clone_or_skip(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Prepare a checkout by cloning or safely synchronizing it."""
        # Checkout preparation failure handling (budget clone=2): on_job_done
        # records the failure while retaining CLONE_WAIT, so retry cannot fall
        # through into label work or discovery after a failed fetch.
        if item.payload.pop("clone_failed", False):
            if item.attempts.get("clone", 0) >= ctx.budget("clone"):
                return StageOutcome(
                    Disposition.FINISH_FAIL,
                    note=f"clone exhausted after {item.attempts['clone']} attempts",
                )
            logger.warning(
                "repo:%s: clone failed (attempt %d/%d); retrying",
                item.repo,
                item.attempts.get("clone", 0),
                ctx.budget("clone"),
            )

        if item.payload.pop("checkout_verified", False):
            return Continue(
                next_state="WAVE_ADMIT" if hasattr(ctx.config, "issue_limit") else "LABELS"
            )

        dest = _repo_checkout_path(item, ctx)
        if ctx.dry_run:
            if dest.exists():
                logger.info("[dry-run] would synchronize %s/%s at %s", ctx.org, item.repo, dest)
                return Continue(
                    next_state="WAVE_ADMIT" if hasattr(ctx.config, "issue_limit") else "LABELS"
                )
            logger.info("[dry-run] would clone %s/%s to %s", ctx.org, item.repo, dest)
            return Continue(
                next_state="WAVE_ADMIT" if hasattr(ctx.config, "issue_limit") else "LABELS"
            )

        if item.payload.pop("checkout_cloned", False) or dest.exists():
            item.payload["checkout_op"] = "sync_checkout"
            job = GitJob(
                repo=item.repo,
                op="sync_checkout",
                timeout_s=stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S),
                kwargs={"repo": f"{ctx.org}/{item.repo}", "dest": str(dest)},
                descr=f"synchronize {ctx.org}/{item.repo}",
            )
            return JobRequest(job=job, on_done_state="CLONE_WAIT")

        item.payload["checkout_op"] = "clone"
        job = GitJob(
            repo=item.repo,
            op="clone",
            timeout_s=stage_timeout(ctx, "clone", GIT_JOB_TIMEOUT_S),
            # worker_pool._dispatch_git_op clone contract: 'repo' (org/name
            # slug for gh repo clone) + 'dest' (checkout path).
            kwargs={"repo": f"{ctx.org}/{item.repo}", "dest": str(dest)},
            descr=f"clone {ctx.org}/{item.repo}",
        )
        return JobRequest(job=job, on_done_state="CLONE_WAIT")

    def _discover(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """[M] Initialize a bounded metadata source; do not classify eagerly."""
        try:
            open_issues = _repo_manager._iter_open_issue_meta(ctx.org, item.repo)
            recovered = self._iter_closed_learning_meta(item.repo, ctx)
            source = RepoIssueSource(chain(open_issues, recovered))
        except Exception as exc:
            logger.warning("repo:%s: discovery failed: %s", item.repo, exc)
            return StageOutcome(Disposition.FINISH_FAIL, note=f"discovery failed: {exc}")

        # ``--drive-green-all`` does not pre-scan unrelated PRs here. Orphan
        # PRs have no linked issue requirements, and consuming their complete
        # page stream would add unbounded latency without producing an
        # admissible source item. Linked PR review context enters only through
        # this repository's bounded issue cursor.

        item.payload["_repo_issue_source"] = source
        return Continue(next_state="SOURCE")

    @staticmethod
    def _iter_closed_learning_meta(repo: str, ctx: StageContext) -> Iterator[dict[str, object]]:
        """Yield closed issues that still own durable learning work."""
        journal = ctx.learning_journal
        if not isinstance(journal, LearningJournalStore):
            return
        for record in journal.incomplete_for_repo(repo=repo):
            issue = record.get("issue")
            if isinstance(issue, bool) or not isinstance(issue, int):
                continue
            issue_data = ctx.github.gh_issue_json(issue)
            if str(issue_data.get("state") or "").upper() != "CLOSED":
                continue
            yield {
                "number": issue,
                "labels": [
                    str(label.get("name") or "")
                    for label in issue_data.get("labels", [])
                    if isinstance(label, dict)
                ],
                "title": str(issue_data.get("title") or "durable learning recovery"),
            }

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Record checkout preparation success/failure (state still CLONE_WAIT).

        Args:
            item: The repo work item.
            result: The clone or checkout-synchronization job result.
            ctx: Stage context.

        """
        if item.payload.pop("_issue_wave_ancestry_job", False):
            if result.ok and isinstance(result.value, dict):
                item.payload[WAVE_ANCESTRY_VERIFIED_KEY] = True
            else:
                item.payload[WAVE_ANCESTRY_ERROR_KEY] = (
                    result.error or "issue-wave ancestry verification failed"
                )
            return
        if item.state != "CLONE_WAIT":
            return
        if result.ok:
            operation = item.payload.get("checkout_op")
            if operation == "clone":
                item.payload["checkout_cloned"] = True
                logger.info("repo:%s: clone completed; verifying checkout", item.repo)
            elif operation == "sync_checkout":
                if not is_full_commit_sha(result.value):
                    # Lightweight isolated stage fixtures predate the
                    # synchronized-main contract and do not materialize a
                    # checkout. Preserve their characterization behavior;
                    # real coordinator contexts always provide a checkout.
                    if (
                        not hasattr(ctx.config, "issue_limit")
                        or not Path(str(ctx.paths.repo_root)).is_dir()
                    ):
                        item.payload["checkout_verified"] = True
                        return
                    item.attempts["clone"] = item.attempts.get("clone", 0) + 1
                    item.payload["clone_failed"] = True
                    logger.warning(
                        "repo:%s: sync returned no validated default-branch SHA",
                        item.repo,
                    )
                    return
                item.payload[SYNCED_MAIN_SHA_KEY] = result.value
                if item.payload.get(DIRECT_SCOPE_BOOTSTRAP_KEY, False):
                    item.payload[DIRECT_SCOPE_BASE_SHA_KEY] = result.value
                item.payload["checkout_verified"] = True
                logger.info("repo:%s: checkout preparation completed", item.repo)
            else:  # pragma: no cover - every checkout JobRequest records its operation
                item.payload["clone_failed"] = True
                item.attempts["clone"] = item.attempts.get("clone", 0) + 1
                logger.warning("repo:%s: checkout operation identity missing", item.repo)
            return
        item.attempts["clone"] = item.attempts.get("clone", 0) + 1
        item.payload["clone_failed"] = True
        logger.warning("repo:%s: checkout preparation failed: %s", item.repo, result.error)


def product_to_work_item(repo: str, product: dict[str, Any]) -> WorkItem | None:
    """Turn one repo-stage product into a queue-ready :class:`WorkItem`.

    Coordinator-side helper (queue ownership stays with the coordinator):
    excluded products (``stage is None``) return ``None`` and are only
    logged by the caller.

    Args:
        repo: Repository name the product belongs to.
        product: One entry of ``item.payload["products"]``.

    Returns:
        A WorkItem parked at the product's entry stage, or ``None`` when the
        product is excluded from the pipeline.

    """
    stage = product.get("stage")
    if stage is None:
        return None
    kind = ItemKind.PR if product.get("kind") == "pr" else ItemKind.ISSUE
    number = int(product["number"])
    item = WorkItem(
        repo=repo,
        kind=kind,
        # A PR number never supplies issue requirements. Linked issue context
        # is required before a PR can enter the review stage.
        issue=(
            number
            if kind is ItemKind.ISSUE
            else (int(product["issue"]) if product.get("issue") is not None else None)
        ),
        pr=int(product["pr"]) if product.get("pr") else (number if kind is ItemKind.PR else None),
        stage=stage,
        state="ENTER",
    )
    labels = product.get("labels") or []
    if labels:
        item.labels_cache = dict.fromkeys(labels, True)
    if kind is ItemKind.ISSUE:
        item.payload["issue_title"] = str(product.get("title") or "")
        item.payload["issue_body"] = str(product.get("body") or "")
    item.payload["entry_stage"] = stage.value
    item.payload["entry_reason"] = product.get("reason", "")
    return item
