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
from pathlib import Path
from typing import Any

from hephaestus.automation import loop_repo_manager as _repo_manager

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
)

logger = logging.getLogger(__name__)


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

        if item.state == "LABELS":
            ctx.github.ensure_state_labels()
            if item.payload.get("_direct_scope_bootstrap", False):
                return StageOutcome(
                    Disposition.FINISH_PASS,
                    note="direct scope checkout synchronized",
                )
            return Continue(next_state="DISCOVER")

        if item.state == "DISCOVER":
            return self._discover(item, ctx)

        if item.state == "SOURCE":
            # Coordinator._run_item externalizes this cursor into its fair
            # registry. Returning Continue retains stage-protocol compatibility
            # for direct unit calls while avoiding an eager product list.
            return Continue(next_state="SOURCE")

        return StageOutcome(Disposition.FINISH_FAIL, note=f"unknown state: {item.state}")

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
            return Continue(next_state="LABELS")

        dest = _repo_checkout_path(item, ctx)
        if ctx.dry_run:
            if dest.exists():
                logger.info("[dry-run] would synchronize %s/%s at %s", ctx.org, item.repo, dest)
                return Continue(next_state="LABELS")
            logger.info("[dry-run] would clone %s/%s to %s", ctx.org, item.repo, dest)
            return Continue(next_state="LABELS")

        if item.payload.pop("checkout_cloned", False) or dest.exists():
            item.payload["checkout_op"] = "sync_checkout"
            job = GitJob(
                repo=item.repo,
                op="sync_checkout",
                timeout_s=GIT_JOB_TIMEOUT_S,
                kwargs={"repo": f"{ctx.org}/{item.repo}", "dest": str(dest)},
                descr=f"synchronize {ctx.org}/{item.repo}",
            )
            return JobRequest(job=job, on_done_state="CLONE_WAIT")

        item.payload["checkout_op"] = "clone"
        job = GitJob(
            repo=item.repo,
            op="clone",
            timeout_s=GIT_JOB_TIMEOUT_S,
            # worker_pool._dispatch_git_op clone contract: 'repo' (org/name
            # slug for gh repo clone) + 'dest' (checkout path).
            kwargs={"repo": f"{ctx.org}/{item.repo}", "dest": str(dest)},
            descr=f"clone {ctx.org}/{item.repo}",
        )
        return JobRequest(job=job, on_done_state="CLONE_WAIT")

    def _discover(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """[M] Initialize a bounded metadata source; do not classify eagerly."""
        try:
            source = RepoIssueSource(_repo_manager._iter_open_issue_meta(ctx.org, item.repo))
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

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Record checkout preparation success/failure (state still CLONE_WAIT).

        Args:
            item: The repo work item.
            result: The clone or checkout-synchronization job result.
            ctx: Stage context.

        """
        if item.state != "CLONE_WAIT":
            return
        if result.ok:
            operation = item.payload.get("checkout_op")
            if operation == "clone":
                item.payload["checkout_cloned"] = True
                logger.info("repo:%s: clone completed; verifying checkout", item.repo)
            elif operation == "sync_checkout":
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
