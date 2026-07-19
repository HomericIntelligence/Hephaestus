"""Repo stage: discover and classify issues from a GitHub repository (epic #1809).

Binding contract: docs/architecture.md §5.1 "repo".

States: ENTER -> CLONE_WAIT -> DISCOVER -> SEEDED.

Steps:

1. [M] ``on_enter``: ``ctx.github.ensure_state_labels()`` — idempotent label
   vocabulary setup.
2. [W:G] CLONE_WAIT: ``GitJob(op="clone")`` when the checkout is missing
   (skipped when already cloned, and logged-skipped under dry-run — the
   coordinator's ``_submit`` asserts no job is ever submitted in dry-run).
   Budget ``clone`` = 2; exhaustion -> finished(fail).
3. [M] DISCOVER: list issues (read-only ``_list_open_issue_meta``), dedup,
   ``partition_epics`` -> tag epics ``state:skip`` via
   ``ctx.github.skip_epics`` [durable, BEFORE excluding], classify each kept
   issue via the REUSED :func:`~..seeding.seed_issue` /
   :func:`~..seeding.classify_issue`, and (``--drive-green-all``) route
   open PRs with a linked tracked issue to PR review.
4. [M] SEEDED: expose the classified products in
   ``item.payload["products"]`` — the coordinator (queue owner) performs the
   actual queue pushes when routing the repo item — and finish
   ``FINISH_PASS(seeded:N)``. The repo item is terminal.

Discovery seams (``_repo_manager`` / ``_seeding`` module attributes) mirror
the ``loop_runner._admission`` seam pattern so unit tests patch the reads
without network I/O.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hephaestus.automation import loop_repo_manager as _repo_manager, pr_discovery as _pr_discovery
from hephaestus.automation.pipeline import seeding as _seeding
from hephaestus.automation.state_labels import partition_epics

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


def _tag_epics(repo: str, ctx: StageContext, epics_labels: dict[int, list[str]]) -> None:
    """Write epic skip labels and log the resulting durable exclusions."""
    ctx.github.skip_epics(epics_labels)
    for number in epics_labels:
        logger.info("repo:%s: #%d is an epic; tagged state:skip, excluded", repo, number)


def _partition_and_tag_epics(
    repo: str, ctx: StageContext, issues_meta: list[dict[str, Any]]
) -> tuple[list[int], list[int]]:
    """Return issue partitions after durably tagging every discovered epic."""
    kept, epics = partition_epics(issues_meta)
    if not epics:
        return kept, epics
    epic_set = set(epics)
    epics_labels = {
        int(metadata["number"]): list(metadata.get("labels") or [])
        for metadata in issues_meta
        if int(metadata["number"]) in epic_set
    }
    _tag_epics(repo, ctx, epics_labels)
    return kept, epics


class RepoStage(Stage):
    """Repo discovery and classification stage (the pipeline's sole producer).

    Products are ``(kind, identifier, stage, reason, facts)`` tuples staged
    in ``item.payload["products"]``; the coordinator turns them into
    :class:`WorkItem` pushes because stage code never touches queues.
    """

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

        if item.state == "SEEDED":
            seeded_count = item.payload.get("seeded_count")
            if seeded_count is None:
                seeded_count = sum(
                    1 for p in item.payload.get("products", []) if p.get("stage") is not None
                )
            seeded = int(seeded_count)
            return StageOutcome(Disposition.FINISH_PASS, note=f"seeded:{seeded}")

        return StageOutcome(Disposition.FINISH_FAIL, note=f"unknown state: {item.state}")

    def _clone_or_skip(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit the clone GitJob, or skip when present / dry-run / exhausted."""
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
        if dest.exists():
            logger.info("repo:%s: already cloned at %s", item.repo, dest)
            return Continue(next_state="DISCOVER")
        if ctx.dry_run:
            logger.info("[dry-run] would clone %s/%s to %s", ctx.org, item.repo, dest)
            return Continue(next_state="DISCOVER")

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
        """[M] List, dedup, tag-then-exclude epics, classify, stage products."""
        try:
            meta = _repo_manager._list_open_issue_meta(ctx.org, item.repo)
        except Exception as exc:
            logger.warning("repo:%s: discovery failed: %s", item.repo, exc)
            return StageOutcome(Disposition.FINISH_FAIL, note=f"discovery failed: {exc}")

        # Dedup while preserving listing order.
        seen: set[int] = set()
        deduped: list[dict[str, Any]] = []
        for entry in meta:
            number = int(entry["number"])
            if number in seen:
                continue
            seen.add(number)
            deduped.append(entry)

        # Epic tagging: the ONE sanctioned seeding write, durable and BEFORE
        # the exclusion is final (doc row "Epic tagging is the one seeding
        # write; done BEFORE excluding").
        try:
            kept, epics = _partition_and_tag_epics(item.repo, ctx, deduped)
        except Exception as exc:
            logger.warning("repo:%s: could not tag excluded epics state:skip: %s", item.repo, exc)
            return StageOutcome(Disposition.FINISH_FAIL, note=f"epic skip tag failed: {exc}")

        products: list[dict[str, Any]] = [
            {
                "kind": "issue",
                "number": num,
                "stage": None,
                "reason": f"epic #{num} excluded",
            }
            for num in epics
        ]
        covered_prs: set[int] = set()
        for num in kept:
            facts = _seeding.seed_issue_from_github(num, ctx.github)
            stage, reason = _seeding.classify_issue(facts)
            if facts.pr_number is not None:
                covered_prs.add(facts.pr_number)
            products.append(
                {
                    "kind": "issue",
                    "number": num,
                    "stage": stage,
                    "reason": reason,
                    "pr": facts.pr_number if facts.pr_is_open else None,
                    "labels": sorted(facts.labels),
                    "title": facts.title,
                    "body": facts.body,
                }
            )

        # --drive-green-all: only linked PRs can be reviewed. Orphans have no
        # requirements context and must remain outside the automation loop.
        if getattr(ctx.config, "drive_green_all", False):
            include_bot_prs = bool(getattr(ctx.config, "include_bot_prs", True))
            include_all_authors = bool(getattr(ctx.config, "include_all_authors", False))
            try:
                open_prs = _repo_manager._list_open_pr_meta(ctx.org, item.repo)
                viewer_login = "" if include_all_authors else _pr_discovery._resolve_viewer_login()
            except Exception as exc:
                logger.warning("repo:%s: PR discovery failed: %s", item.repo, exc)
                return StageOutcome(Disposition.FINISH_FAIL, note=f"discovery failed: {exc}")
            for pr in open_prs:
                pr_number = int(pr["number"])
                if pr_number in covered_prs:
                    continue
                if not _drive_green_pr_is_in_scope(
                    pr, include_bot_prs=include_bot_prs, viewer_login=viewer_login
                ):
                    continue
                logger.info(
                    "repo:%s: skipping orphan PR #%d; no linked issue supplies requirements",
                    item.repo,
                    pr_number,
                )

        item.payload["products"] = products
        item.payload["seeded_count"] = sum(1 for p in products if p.get("stage") is not None)
        return Continue(next_state="SEEDED")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Record clone success/failure (state still CLONE_WAIT).

        Args:
            item: The repo work item.
            result: The clone job result.
            ctx: Stage context.

        """
        if item.state != "CLONE_WAIT":
            return
        if result.ok:
            logger.info("repo:%s: clone completed", item.repo)
            return
        item.attempts["clone"] = item.attempts.get("clone", 0) + 1
        item.payload["clone_failed"] = True
        logger.warning("repo:%s: clone failed: %s", item.repo, result.error)


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
