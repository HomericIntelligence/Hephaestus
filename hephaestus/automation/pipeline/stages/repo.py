"""Repo stage: discover and classify issues from a GitHub repository (epic #1809).

Binding contract: docs/architecture.md §5.1 "repo".

States: ENTER -> CLONE_WAIT -> DISCOVER -> SOURCE.

Steps:

1. [M] ``on_enter``: ``ctx.github.ensure_state_labels()`` — idempotent label
   vocabulary setup.
2. [W:G] CLONE_WAIT: ``GitJob(op="clone")`` when the checkout is missing, or
   ``GitJob(op="sync_checkout")`` when it already exists. Synchronization
   validates the expected remote and fast-forwards only a clean default-branch
   checkout. Both operations are logged-skipped under dry-run — the
   coordinator's ``_submit`` asserts no job is ever submitted in dry-run.
   Budget ``clone`` = 2; exhaustion -> finished(fail).
3. [M] DISCOVER: initialize one page-at-a-time metadata cursor and perform
   the independent ``--drive-green-all`` read.  It never materializes a
   list of classified products.
4. [M] SOURCE: the coordinator owns source admission.  It takes one metadata
   row only when it can transfer the repo source's live-work permit to the
   classified issue, tagging epics ``state:skip`` [durable, BEFORE excluding].
   Once the cursor is exhausted the repo item finishes ``FINISH_PASS``.

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

from hephaestus.automation import loop_repo_manager as _repo_manager, pr_discovery as _pr_discovery

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


def _drive_green_pr_is_in_scope(
    pr: dict[str, Any], *, include_bot_prs: bool, viewer_login: str
) -> bool:
    """Return whether an orphan PR is eligible and belongs to author scope."""
    if not _pr_discovery.pr_needs_loop_review(pr):
        return False
    if not include_bot_prs and _pr_discovery._is_bot_pr_author(pr):
        return False
    if not _pr_discovery._is_viewer_authored(pr, viewer_login):
        if (pr.get("user") or {}).get("login") is None:
            logger.warning(
                "PR #%s has no user.login; skipping under author filter (#821)",
                pr.get("number"),
            )
        return False
    return True


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
    """Bounded discovery cursor retained by one in-flight repo item.

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
        """Ensure the state-label vocabulary exists (idempotent, durable).

        Args:
            item: The repo work item.
            ctx: Stage context with the GitHub accessor.

        Returns:
            None to proceed with step().

        """
        ctx.github.ensure_state_labels()
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

        if item.state == "DISCOVER":
            return self._discover(item, ctx)

        if item.state == "SOURCE":
            # The coordinator parks the source lease after DISCOVER and is
            # its only consumer.  Returning Continue retains stage-protocol
            # compatibility for direct unit calls; Coordinator._run_item
            # recognizes this state and yields rather than spinning.
            return Continue(next_state="SOURCE")

        return StageOutcome(Disposition.FINISH_FAIL, note=f"unknown state: {item.state}")

    def _clone_or_skip(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Prepare a checkout by cloning or safely synchronizing it."""
        # Clone failure handling (budget clone=2): on_job_done recorded the
        # failure; classify it here so retry re-submits and exhaustion fails.
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

        dest = _repo_checkout_path(item, ctx)
        if ctx.dry_run:
            if dest.exists():
                logger.info("[dry-run] would synchronize %s/%s at %s", ctx.org, item.repo, dest)
                return Continue(next_state="DISCOVER")
            logger.info("[dry-run] would clone %s/%s to %s", ctx.org, item.repo, dest)
            return Continue(next_state="DISCOVER")

        if dest.exists():
            job = GitJob(
                repo=item.repo,
                op="sync_checkout",
                timeout_s=GIT_JOB_TIMEOUT_S,
                kwargs={"repo": f"{ctx.org}/{item.repo}", "dest": str(dest)},
                descr=f"synchronize {ctx.org}/{item.repo}",
            )
            return JobRequest(job=job, on_done_state="DISCOVER")

        job = GitJob(
            repo=item.repo,
            op="clone",
            timeout_s=GIT_JOB_TIMEOUT_S,
            # worker_pool._dispatch_git_op clone contract: 'repo' (org/name
            # slug for gh repo clone) + 'dest' (checkout path).
            kwargs={"repo": f"{ctx.org}/{item.repo}", "dest": str(dest)},
            descr=f"clone {ctx.org}/{item.repo}",
        )
        return JobRequest(job=job, on_done_state="DISCOVER")

    def _discover(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """[M] Initialize a bounded metadata source; do not classify eagerly."""
        try:
            source = RepoIssueSource(_repo_manager._iter_open_issue_meta(ctx.org, item.repo))
        except Exception as exc:
            logger.warning("repo:%s: discovery failed: %s", item.repo, exc)
            return StageOutcome(Disposition.FINISH_FAIL, note=f"discovery failed: {exc}")

        # --drive-green-all: only linked PRs can be reviewed. Orphans have no
        # requirements context and must remain outside the automation loop.
        if getattr(ctx.config, "drive_green_all", False):
            include_bot_prs = bool(getattr(ctx.config, "include_bot_prs", True))
            include_all_authors = bool(getattr(ctx.config, "include_all_authors", False))
            try:
                open_prs = _repo_manager._iter_open_pr_meta(ctx.org, item.repo)
                viewer_login = "" if include_all_authors else _pr_discovery._resolve_viewer_login()
                # Issue metadata now arrives lazily, so this preflight cannot
                # know which PRs are linked without retaining an unbounded
                # set. The loop never queues orphan PRs from this path; retain
                # that safety boundary without materializing a coverage spill.
                for pr in open_prs:
                    if _drive_green_pr_is_in_scope(
                        pr, include_bot_prs=include_bot_prs, viewer_login=viewer_login
                    ):
                        logger.info(
                            "repo:%s: leaving PR #%d outside repository discovery "
                            "until linked issue source provides requirements",
                            item.repo,
                            int(pr["number"]),
                        )
            except Exception as exc:
                logger.warning("repo:%s: PR discovery failed: %s", item.repo, exc)
                return StageOutcome(Disposition.FINISH_FAIL, note=f"discovery failed: {exc}")

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
            logger.info("repo:%s: checkout preparation completed", item.repo)
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
