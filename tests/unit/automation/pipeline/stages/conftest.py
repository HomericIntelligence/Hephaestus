"""Fixtures and fakes for pipeline stage tests.

``FakeStageGitHub`` extends the canonical pipeline ``FakeGitHub`` (see
``tests/unit/automation/pipeline/conftest.py``) with the read surface the
planning/plan_review stages use (``gh_issue_json``, PR-coverage lookups,
tri-state plan discovery), so mutator call sites and the ``mutation_log``
format stay identical to what coordinator tests (#1817) will assert.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from hephaestus.automation.implementation_go_audit_receipt import PendingImplementationGoAudit
from hephaestus.automation.merge_authorization import MERGE_AUTHORIZATION_MARKER, MergeAuthorization
from hephaestus.automation.pipeline.coordinator import PipelineConfig
from hephaestus.automation.pipeline.events import StageEvent
from hephaestus.automation.pipeline.routing import ROUTES, StageName
from hephaestus.automation.pipeline.stages import (
    ConditionalMergeResult,
    ImplementationThreadReplyResult,
    ReviewerThreadReconciliationResult,
    StageContext,
    StageGitHub,
)
from hephaestus.automation.pipeline.stages.base import BranchWorktreeOwnerStatus
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.review_audit import ReviewAudit, render_implementation_go_audit
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    IssueComment,
    PlanDiscoveryResult,
    blocked_audit_recovery_body,
    render_current_plan,
    render_current_review,
    render_pending_review,
)
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_PLAN_NO_GO,
)
from tests.unit.automation.pipeline.conftest import FakeGitHub


class _DefaultPrState:
    """Sentinel that distinguishes a default open PR from an explicit failure."""


_DEFAULT_PR_STATE = _DefaultPrState()


class FakeStageGitHub(FakeGitHub):
    """Canonical FakeGitHub plus the stage read queries.

    Implements the :class:`StageGitHub` protocol (mypy-checked below).
    Reads mirror the real helper names the stages call through
    ``ctx.github``: ``gh_issue_json`` (github_api.issues),
    ``find_merged_closing_pr`` / ``find_pr_for_issue`` /
    ``close_issue_as_covered`` (_review_utils), and ``discover_plan``
    (PlannerStateManager).
    """

    def __init__(
        self,
        *,
        labels: list[str] | None = None,
        issue_title: str = "A task",
        issue_body: str = "",
        issue_state: str = "OPEN",
        merged_pr: int | None = None,
        open_pr: int | None = None,
        pr_issue: int | None = None,
        has_plan: bool = False,
        pr_head_branch: str | None = None,
        pr_head_writable: bool = True,
        pr_impl_state: tuple[bool, bool] = (False, False),
        pr_impl_readbacks: list[tuple[bool, bool] | Exception] | None = None,
        unresolved: list[tuple[int, int]] | None = None,
        by_severity: list[tuple[int, int, int]] | None = None,
        pr_state: dict[str, Any] | _DefaultPrState | None = _DEFAULT_PR_STATE,
        conversation_resolution: bool = True,
        pr_review_context: dict[str, str] | None = None,
        learn_terminal: bool = False,
        plan_read_error: str | None = None,
        journal_read_error: str | None = None,
        authorization_reviews: tuple[dict[str, object], ...] | None = None,
        actor_permissions: dict[str, str] | None = None,
    ) -> None:
        """Initialize the fake with canned read answers.

        Args:
            labels: Seed labels applied to any issue on first read/mutation.
            issue_title: Canned issue title returned by gh_issue_json.
            issue_body: Canned issue body returned by gh_issue_json.
            issue_state: Canned current GitHub issue state.
            merged_pr: Canned answer for find_merged_closing_pr.
            open_pr: Canned answer for find_pr_for_issue.
            pr_issue: Canned answer for find_issue_for_pr.
            has_plan: Canned answer for plan discovery.
            pr_head_branch: Canned answer for get_pr_head_branch.
            pr_head_writable: Whether the PR head belongs to the base origin
                and may receive coordinator-owned address commits.
            pr_impl_state: Canned (has_go, has_no_go) answer for
                pr_has_implementation_state_label.
            pr_impl_readbacks: Optional FIFO of independent label readbacks;
                entries may be contradictory, absent, or exceptions. When
                empty, the current post-mutation state is returned.
            unresolved: FIFO of ``(automation, external)`` review-thread
                shapes.  Each shape is consumed per fresh snapshot, with the
                final shape repeating; this concise form treats automation
                threads as blocking.
            by_severity: FIFO of ``(blocking, advisory, external)``
                review-thread shapes.  Supply this form when a test needs to
                distinguish blocking and advisory automation threads.
            pr_state: Canned answer for gh_pr_state (merge_wait's single
                PR-state read); ``None`` mirrors a transient read failure.
            conversation_resolution: Whether the admitted PR base has the
                server-enforced required-conversation-resolution protection.
            learn_terminal: Seed answer for drive_green_learn_terminal —
                True mirrors an issue whose post-merge /learn already ran
                terminally (the #848 dedupe record).
            plan_read_error: Optional failure returned by plan-comment
                discovery.
            journal_read_error: Optional failure returned by review-journal
                discovery.
            authorization_reviews: Exact-head native reviews; ``None`` uses
                one stable trusted operator approval, while an empty tuple
                models absent authorization.
            actor_permissions: Current collaborator permissions for review
                authors.

        """
        super().__init__()
        self._seed_labels = list(labels or [])
        self._issue_title = issue_title
        self._issue_body = issue_body
        self._issue_state = issue_state
        self._merged_pr = merged_pr
        self._open_pr = open_pr
        self._pr_issue = pr_issue
        self._has_plan = has_plan
        self._plan_read_error = plan_read_error
        self._journal_read_error = journal_read_error
        self._pr_head_branch = pr_head_branch
        self._pr_head_writable = pr_head_writable
        self._pr_impl_state = pr_impl_state
        self._pr_impl_readbacks = deque(pr_impl_readbacks or [])
        self._unresolved: list[tuple[int, int]] = list(unresolved or [(0, 0)])
        self._by_severity = (
            list(by_severity)
            if by_severity is not None
            else [(a, 0, h) for (a, h) in self._unresolved]
        )
        self._pr_state = (
            {
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "autoMergeRequest": None,
            }
            if isinstance(pr_state, _DefaultPrState)
            else pr_state
        )
        default_head = (
            self._pr_state.get("headRefOid") if isinstance(self._pr_state, dict) else "a" * 40
        )
        if not isinstance(default_head, str) or not default_head:
            default_head = "a" * 40
        self._authorization_reviews = (
            authorization_reviews
            if authorization_reviews is not None
            else (
                {
                    "id": "R1",
                    "fullDatabaseId": 1,
                    "body": MERGE_AUTHORIZATION_MARKER,
                    "state": "APPROVED",
                    "submittedAt": "2026-08-08T00:00:00Z",
                    "updatedAt": "2026-08-08T00:00:00Z",
                    "includesCreatedEdit": False,
                    "lastEditedAt": None,
                    "viewerDidAuthor": False,
                    "author": {"login": "operator", "__typename": "User"},
                    "commit": {"oid": default_head},
                },
            )
        )
        self._actor_permissions = actor_permissions or {"operator": "WRITE"}
        self.merge_attempts: list[tuple[int, str, str]] = []
        self._conversation_resolution = conversation_resolution
        self.conversation_resolution_checks: list[tuple[int, str]] = []
        self._pr_review_context = (
            pr_review_context
            if pr_review_context is not None
            else {
                "pr_title": "A current PR title",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
                "pr_base_branch": "main",
            }
        )
        self._learn_terminal = learn_terminal
        self._posted_thread_ids: dict[int, list[str]] = {}
        self._thread_replies: dict[str, list[dict[str, str]]] = {}
        self.learn_results: dict[int, bool] = {}
        self.learn_claims: set[int] = set()
        self.pending_go_audits: dict[int, PendingImplementationGoAudit] = {}

    def _issue_labels(self, issue_number: int) -> set[str]:
        """Return the issue's label set, seeding it on first access."""
        if issue_number not in self.labels:
            self.labels[issue_number] = set(self._seed_labels)
        return self.labels[issue_number]

    # -- read surface used by the stages -----------------------------------
    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Mirror github_api.issues.gh_issue_json (issue context plus labels)."""
        return {
            "number": issue_number,
            "title": self._issue_title,
            "body": self._issue_body,
            "state": self._issue_state,
            "labels": [{"name": name} for name in sorted(self._issue_labels(issue_number))],
        }

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_merged_closing_pr."""
        return self._merged_pr

    def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_merged_pr_for_issue."""
        return self._merged_pr

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_pr_for_issue (open PR lookup)."""
        return self._open_pr

    def find_issue_for_pr(self, pr_number: int) -> int | None:
        """Mirror PipelineGitHub.find_issue_for_pr (PR body Closes lookup)."""
        return self._pr_issue

    def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
        """Return a canned tri-state plan-discovery result."""
        if self._plan_read_error is not None:
            return PlanDiscoveryResult.read_error(self._plan_read_error)
        if self._has_plan:
            return PlanDiscoveryResult.found(render_current_plan("Fake plan"))
        return PlanDiscoveryResult.absent()

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return fake issue comments in their append order with ownership metadata."""
        if self._journal_read_error is not None:
            raise CommentJournalReadError(self._journal_read_error)
        comments = self.comments.get(issue_number, [])
        if not comments and self._has_plan:
            comments = [render_current_plan("Fake plan")]
            if STATE_PLAN_NO_GO in self._issue_labels(issue_number):
                comments.append(
                    render_current_review(
                        "Rejected fake plan\n\nstate:plan-no-go",
                        revision=1,
                    )
                )
            else:
                comments.append(render_pending_review(revision=1))
        return [
            comment
            if isinstance(comment, IssueComment)
            else IssueComment(
                body=comment,
                author_login="hephaestus[bot]",
                viewer_did_author=True,
            )
            for comment in comments
        ]

    def ensure_blocked_audit(self, issue_number: int) -> None:
        """Mirror recovery of a missing actor-owned BLOCKED explanation."""
        body = blocked_audit_recovery_body(self.issue_comments(issue_number))
        if body is not None:
            self.upsert_issue_comment(
                issue_number,
                PLAN_REVIEW_CANONICAL_MARKER,
                body,
            )

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Mirror _review_utils.get_pr_head_branch (canned answer)."""
        return self._pr_head_branch

    def pr_head_is_writable(self, pr_number: int) -> bool:
        """Mirror PipelineGitHub.pr_head_is_writable (canned answer)."""
        return self._pr_head_writable

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return a scripted independent label readback, when available."""
        del pr_number
        if self._pr_impl_readbacks:
            readback = self._pr_impl_readbacks.popleft()
            if isinstance(readback, Exception):
                raise readback
            return readback
        return self._pr_impl_state

    def pr_review_context(self, pr_number: int) -> dict[str, str] | None:
        """Mirror PipelineGitHub's atomic PR-review input read."""
        del pr_number
        return dict(self._pr_review_context) if self._pr_review_context is not None else None

    def _next_unresolved_thread_counts(self) -> tuple[int, int, int]:
        """Return the next scripted open-thread shape for this test double."""
        if len(self._by_severity) > 1:
            return self._by_severity.pop(0)
        return self._by_severity[0]

    def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """Return scripted fresh review-thread facts for label-gate tests."""
        blocking, advisory, external = self._next_unresolved_thread_counts()
        posted = list(self._posted_thread_ids.get(pr_number, []))
        posted_comments = (
            list(self.reviews.get(pr_number, [{}])[-1].get("comments", []))
            if self.reviews.get(pr_number)
            else []
        )
        threads: list[dict[str, Any]] = []
        cursor = 0
        for count, author, severity in (
            (blocking, False, "major"),
            (advisory, False, "nitpick"),
            (external, True, "major"),
        ):
            for _ in range(count):
                posted_comment = posted_comments[cursor] if cursor < len(posted_comments) else {}
                thread_id = (
                    posted[cursor] if cursor < len(posted) else f"live-thread-{pr_number}-{cursor}"
                )
                cursor += 1
                threads.append(
                    {
                        "id": thread_id,
                        "path": posted_comment.get("path") or "a.py",
                        "line": posted_comment.get("line") or cursor,
                        "side": "RIGHT",
                        "severity": severity,
                        "body": (
                            f"<!-- hephaestus-severity: {severity} -->\n"
                            f"{posted_comment.get('body') or 'finding'}"
                        ),
                        "author": "reviewer" if author else "hephaestus[bot]",
                        "authors": ["reviewer" if author else "hephaestus[bot]"],
                        "review_id": f"review-{pr_number}-{cursor}",
                        "comments": [
                            {
                                "id": f"comment-{thread_id}",
                                "author": "reviewer" if author else "hephaestus[bot]",
                                "body": posted_comment.get("body") or "finding",
                            },
                            *self._thread_replies.get(thread_id, []),
                        ],
                    }
                )
        return threads

    # -- mutator surface used by the stages ----------------------------------
    # Coordinator-neutral names (the pipeline architecture guard forbids
    # github_api mutator names inside pipeline modules); each delegates to
    # the canonical gh_* recorder so mutation_log keeps the canonical format.
    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Mirror _review_utils.close_issue_as_covered (records mutation)."""
        self._log("close_issue_as_covered", issue_number, pr_number)

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Coordinator-neutral label add (delegates to gh_issue_add_labels)."""
        self._issue_labels(issue_number)
        self.gh_issue_add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Coordinator-neutral label remove (delegates to gh_issue_remove_labels)."""
        self._issue_labels(issue_number)
        has_go, has_no_go = self._pr_impl_state
        if STATE_IMPLEMENTATION_GO in labels:
            has_go = False
        if STATE_IMPLEMENTATION_NO_GO in labels:
            has_no_go = False
        self._pr_impl_state = (has_go, has_no_go)
        self.gh_issue_remove_labels(issue_number, labels)

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Atomic add+remove recorded as ONE mutation (mirrors gh issue edit)."""
        labels = self._issue_labels(issue_number)
        labels.update(add)
        labels.difference_update(remove)
        self._log("edit_labels", issue_number, tuple(add), tuple(remove))

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Mirror the generic marker-keyed comment upsert (#2256)."""
        comments = self.comments.setdefault(issue_number, [])
        matches = [
            index
            for index, comment in enumerate(comments)
            if (comment.body if isinstance(comment, IssueComment) else comment)
            .lstrip()
            .startswith(marker)
        ]
        if not matches and legacy_marker is not None:
            matches = [
                index
                for index, comment in enumerate(comments)
                if (comment.body if isinstance(comment, IssueComment) else comment)
                .lstrip()
                .startswith(legacy_marker)
            ]
        if matches:
            comments[matches[-1]] = body
        else:
            comments.append(body)
        self._log("gh_issue_upsert_comment", issue_number, marker)

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Mirror the immutable replay-safe journal append."""
        comments = self.comments.setdefault(issue_number, [])
        matching = [
            comment.body if isinstance(comment, IssueComment) else comment
            for comment in comments
            if (comment.body if isinstance(comment, IssueComment) else comment)
            .lstrip()
            .startswith(marker)
        ]
        if matching and any(existing != body for existing in matching):
            raise RuntimeError(f"immutable journal conflict for marker {marker!r}")
        if not matching:
            comments.append(body)
            self._log("append_issue_comment", issue_number, marker)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Mirror the coordinator plan-comment upsert (PLAN_COMMENT_MARKER-keyed).

        Delegates to the canonical ``gh_issue_upsert_comment`` recorder so
        the mutation_log keeps the canonical format, and flips the
        plan-discovery answer to FOUND — the posted comment IS the durable
        plan artifact the verify step reads back.
        """
        self._has_plan = True
        self.upsert_issue_comment(
            issue_number,
            PLAN_CANONICAL_MARKER,
            body,
        )

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Mirror the coordinator PR-ensure (delegates to gh_pr_create)."""
        return self.gh_pr_create(branch, title, body)

    def post_review_threads(
        self,
        pr_number: int,
        threads: list[dict[str, Any]],
        *,
        expected_head_sha: str,
        review_diff: str | None = None,
    ) -> list[dict[str, Any]]:
        """Mirror a post-time immutable receipt returned by the coordinator."""
        del expected_head_sha, review_diff
        ids = self.gh_pr_review_post(pr_number, threads, "")
        self._posted_thread_ids[pr_number] = list(ids)
        review_id = f"review-{pr_number}-{len(self.reviews.get(pr_number, []))}"
        return [
            {
                "id": thread_id,
                "path": str(thread.get("path") or "a.py"),
                "line": thread.get("line"),
                "side": str(thread.get("side") or "RIGHT"),
                "body": str(thread.get("body") or "finding"),
                "author": "hephaestus[bot]",
                "authors": ["hephaestus[bot]"],
                "comments": [
                    {
                        "id": f"comment-{thread_id}",
                        "author": "hephaestus[bot]",
                        "body": str(thread.get("body") or "finding"),
                        "review_id": review_id,
                    }
                ],
                "review_id": review_id,
            }
            for thread_id, thread in zip(ids, threads, strict=True)
        ]

    def post_implementation_thread_replies(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
        batch_nonce: str,
    ) -> ImplementationThreadReplyResult:
        """Record head-gated implementation replies for stage tests."""
        del expected_head_sha, batch_nonce
        by_id = {
            str(thread.get("thread_id") or thread.get("id") or ""): thread for thread in threads
        }
        receipts: list[dict[str, Any]] = []
        for thread_id, reply in replies.items():
            thread = by_id.get(thread_id)
            if not isinstance(thread, dict):
                continue
            reply_record = {
                "id": f"implementation-reply-{thread_id}",
                "author": "hephaestus[bot]",
                "body": reply,
            }
            self._thread_replies.setdefault(thread_id, []).append(reply_record)
            receipts.append(
                {
                    **thread,
                    "id": thread_id,
                    "comments": [*thread.get("comments", []), reply_record],
                    "implementation_reply_id": reply_record["id"],
                    "implementation_reply_body": reply,
                    "implementation_head_sha": "a" * 40,
                }
            )
        self._log("post_implementation_thread_replies", pr_number, tuple(sorted(replies)))
        replied = tuple(sorted(str(thread_id) for thread_id in replies if thread_id in by_id))
        blocked = tuple(sorted(set(replies) - set(replied)))
        return ImplementationThreadReplyResult(replied, blocked, tuple(receipts))

    def reviewer_validation_receipts(
        self,
        pr_number: int,
        *,
        reviewed_head_sha: str,
        threads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Derive test receipts from the current live thread conversation.

        The production adapter validates the signed marker.  The stage fake
        instead preserves the same durable property needed here: only a final
        host-posted implementation reply for the current reviewed head may
        reach reconciliation, never a process-local payload receipt.
        """
        del pr_number
        receipts: list[dict[str, Any]] = []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            comments = thread.get("comments")
            if not isinstance(comments, list) or not comments:
                continue
            final_comment = comments[-1]
            if not isinstance(final_comment, dict):
                continue
            reply_id = final_comment.get("id")
            reply_body = final_comment.get("body")
            if not (
                isinstance(reply_id, str)
                and reply_id.startswith("implementation-reply-")
                and isinstance(reply_body, str)
            ):
                continue
            receipts.append(
                {
                    **thread,
                    "implementation_reply_id": reply_id,
                    "implementation_reply_body": reply_body,
                    "implementation_head_sha": reviewed_head_sha,
                }
            )
        return receipts

    def reconcile_reviewer_validated_threads(
        self,
        pr_number: int,
        *,
        reviewed_head_sha: str,
        receipts: list[dict[str, Any]],
        resolved_thread_ids: set[str],
        feedback: dict[str, str],
    ) -> ReviewerThreadReconciliationResult:
        """Record the reviewer's fresh resolution/rejection decision for tests."""
        del reviewed_head_sha, receipts
        self._log(
            "reconcile_reviewer_validated_threads",
            pr_number,
            tuple(sorted(resolved_thread_ids)),
            tuple(sorted(feedback)),
        )
        return ReviewerThreadReconciliationResult(
            resolved_thread_ids=tuple(sorted(resolved_thread_ids)),
            feedback_thread_ids=tuple(sorted(feedback)),
        )

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Mirror pr_manager.mark_pr_implementation_go (records mutation)."""
        self._pr_impl_state = (True, False)
        self._log("mark_pr_implementation_go", pr_number)

    def publish_implementation_go_audit(
        self, pr_number: int, head_sha: str, audit: ReviewAudit
    ) -> None:
        """Mirror public audit publication and exact-head handoff cleanup."""
        marker, body = render_implementation_go_audit(audit, pr_number=pr_number, head_sha=head_sha)
        self.upsert_issue_comment(pr_number, marker, body)
        handoff_prefix = (
            f"<!-- hephaestus-implementation-reply-handoff:pr={pr_number}:head={head_sha}:"
        )
        self.comments[pr_number] = [
            comment
            for comment in self.comments.get(pr_number, [])
            if not (
                isinstance(comment, str) and comment.split("\n", 1)[0].startswith(handoff_prefix)
            )
        ]
        self._log("publish_implementation_go_audit", pr_number, head_sha)

    def persist_pending_implementation_go_audit(
        self, pr_number: int, head_sha: str, audit: ReviewAudit
    ) -> None:
        """Persist the exact-head recovery record before the label transition."""
        self.pending_go_audits[pr_number] = PendingImplementationGoAudit(
            pr_number=pr_number,
            head_sha=head_sha,
            audit=audit,
        )
        self._log("persist_pending_implementation_go_audit", pr_number, head_sha)

    def pending_implementation_go_audit(
        self, pr_number: int
    ) -> PendingImplementationGoAudit | None:
        """Return the fake durable recovery record."""
        return self.pending_go_audits.get(pr_number)

    def clear_pending_implementation_go_audit(self, pr_number: int, head_sha: str) -> None:
        """Clear only a matching exact-head fake recovery record."""
        receipt = self.pending_go_audits.get(pr_number)
        if receipt is not None and receipt.head_sha == head_sha:
            del self.pending_go_audits[pr_number]
        self._log("clear_pending_implementation_go_audit", pr_number, head_sha)

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Mirror pr_manager.mark_pr_implementation_no_go (records mutation)."""
        self._pr_impl_state = (False, True)
        self._log("mark_pr_implementation_no_go", pr_number)

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Mirror ci_driver.CIDriver._gh_pr_state (canned answer)."""
        del pr_number  # single canned answer; not per-PR keyed
        return self._pr_state

    def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, Any] | None:
        """Mirror the post-405 operational readiness lookup."""
        del pr_number
        return dict(self._pr_state) if isinstance(self._pr_state, dict) else self._pr_state

    @property
    def _repo_slug(self) -> str:
        """Return the stable repository identity used by merge authorization."""
        return "org/repo-a"

    def _viewer_login(self) -> str:
        """Return the fake authenticated automation actor."""
        return "hephaestus[bot]"

    def merge_authorization_reviews(self, pr_number: int) -> tuple[dict[str, object], ...]:
        """Return a fresh copy of the scripted native-review snapshot."""
        del pr_number
        return tuple(dict(review) for review in self._authorization_reviews)

    def repository_permission_for_actor(self, login: str) -> str:
        """Return the scripted current collaborator permission."""
        return self._actor_permissions.get(login, "NONE")

    def base_branch_requires_conversation_resolution(
        self, pr_number: int, base_branch: str
    ) -> bool:
        """Return the canned server-enforced conversation-resolution protection."""
        self.conversation_resolution_checks.append((pr_number, base_branch))
        return self._conversation_resolution

    def merge_pr_if_head(
        self,
        pr_number: int,
        reviewed_sha: str,
        authorization: MergeAuthorization,
    ) -> ConditionalMergeResult:
        """Mirror one successful SHA-conditional normal merge request."""
        self.merge_attempts.append((pr_number, reviewed_sha, authorization.review_id))
        self._log("merge_pr_if_head", pr_number, reviewed_sha, authorization.review_id)
        self._pr_state = {"state": "MERGED"}
        return ConditionalMergeResult(status=200, body={"merged": True})

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Mirror ci_driver._learn_record_terminal over the arming record.

        Terminal when seeded so (``learn_terminal=True``) or once
        :meth:`mark_drive_green_learn_result` recorded an outcome — the
        exactly-once /learn read-back (#848).
        """
        return self._learn_terminal or issue_number in self.learn_results

    def drive_green_learn_inflight(self, issue_number: int) -> bool:
        """Mirror a durable pre-dispatch /learn claim."""
        return issue_number in self.learn_claims

    def claim_drive_green_learn(self, issue_number: int, pr_number: int) -> bool:
        """Record the pre-dispatch claim unless another run already owns it."""
        if self.drive_green_learn_terminal(issue_number) or issue_number in self.learn_claims:
            return False
        self.learn_claims.add(issue_number)
        self._log("claim_drive_green_learn", issue_number, pr_number)
        return True

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Mirror post_merge_processor.mark_drive_green_learn_result [durable]."""
        self.learn_results[issue_number] = succeeded
        self.learn_claims.discard(issue_number)
        self._log("mark_drive_green_learn_result", issue_number, succeeded)

    def ensure_state_labels(self) -> None:
        """Mirror the repo-stage label-vocabulary ensure (records mutation).

        The canonical ``skip_epics`` recorder is inherited from
        :class:`FakeGitHub`; this is the only repo-stage (#1817) protocol
        method without a canonical recorder there.
        """
        self._log("ensure_state_labels")


if TYPE_CHECKING:
    # mypy-enforced declaration that FakeStageGitHub satisfies the
    # StageGitHub protocol (m5): a drifted signature fails type checking.
    _stage_github_protocol_check: StageGitHub = FakeStageGitHub()


def _budget_fn(name: str) -> int:
    """Look up a budget across all ROUTES rows (conservative default 1)."""
    for route in ROUTES.values():
        if name in route.budgets:
            return route.budgets[name]
    return 1


class _Paths:
    """Path accessor stub for stage tests."""

    repo_root = "/tmp/repo"
    worktree = "/tmp/repo/worktree"


@pytest.fixture
def make_ctx() -> Callable[..., StageContext]:
    """Build StageContext instances with a fake clock and ROUTES budgets."""

    def _make_ctx(
        *,
        config: PipelineConfig | None = None,
        config_overrides: dict[str, Any] | None = None,
        org: str = "test-org",
        dry_run: bool = False,
        github: FakeStageGitHub | None = None,
        paths: Any = None,
        now_fn: Callable[[], float] | None = None,
        budget_fn: Callable[[str], int] | None = None,
        event_fn: Callable[[StageEvent], None] | None = None,
        learning_journal: Any = None,
        plan_review_sessions: Any = None,
        branch_worktree_owner_status: (
            Callable[[WorkItem, str, str], BranchWorktreeOwnerStatus] | None
        ) = None,
    ) -> StageContext:
        if config is not None and config_overrides:
            raise ValueError("pass config or config_overrides, not both")

        ticks = [0]

        def default_now_fn() -> float:
            ticks[0] += 1
            return 1000.0 + ticks[0]

        resolved_config = config or PipelineConfig(
            org=org,
            repos=["test-repo"],
            dry_run=dry_run,
        )
        if config_overrides:
            resolved_config = replace(resolved_config, **config_overrides)

        return StageContext(
            config=resolved_config,
            org=org,
            dry_run=dry_run,
            github=github if github is not None else FakeStageGitHub(),
            paths=paths if paths is not None else _Paths(),
            now_fn=now_fn if now_fn is not None else default_now_fn,
            budget_fn=budget_fn if budget_fn is not None else _budget_fn,
            event_fn=event_fn,
            learning_journal=learning_journal,
            plan_review_sessions=plan_review_sessions,
            branch_worktree_owner_status=branch_worktree_owner_status,
        )

    return _make_ctx


@pytest.fixture
def make_work_item() -> Callable[..., WorkItem]:
    """Build WorkItem instances parked in a plan-side stage."""

    def _make_item(
        *,
        repo: str = "test-repo",
        kind: ItemKind = ItemKind.ISSUE,
        issue: int | None = 1,
        pr: int | None = None,
        stage: StageName = StageName.PLANNING,
        state: str = "ENTER",
        labels: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkItem:
        item = WorkItem(repo=repo, kind=kind, issue=issue, pr=pr, stage=stage, state=state)
        if labels:
            item.labels_cache = dict.fromkeys(labels, True)
        if payload:
            item.payload = payload
        # Direct EVAL unit tests model a review job that has already crossed
        # the checkout barrier. Integration walks still install this proof only
        # after the barrier completes.
        if pr is not None and state == "EVAL":
            item.payload.setdefault("reviewed_pr_head_sha", "a" * 40)
        return item

    return _make_item
