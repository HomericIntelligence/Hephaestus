"""GitHub-backed implementation-go audit recovery receipts."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from hephaestus.automation.implementation_go_audit_receipt import (
    IMPLEMENTATION_GO_AUDIT_PENDING_PREFIX,
    LegacyPendingImplementationGoAuditError,
    PendingImplementationGoAudit,
    parse_pending_implementation_go_audit,
    parse_published_implementation_go_audit,
    render_pending_implementation_go_audit,
)
from hephaestus.automation.issue_timeline import _IMPLEMENTATION_REPLY_HANDOFF_MARKER_RE
from hephaestus.automation.rebase_review_receipt import (
    REBASE_REVIEW_PREFIX,
    RebaseReviewRecord,
    parse_review_rebase_record,
    render_review_rebase_record,
)
from hephaestus.automation.review_audit import ReviewAudit, render_implementation_go_audit

from .pipeline_github_contract import _PipelineGitHubHost
from .review_journal import has_exact_leading_marker


class PipelineGitHubAuditReceipts(_PipelineGitHubHost):
    """Own durable pending-publication receipts independently of label state."""

    def publish_review_rebase_record(self, record: RebaseReviewRecord) -> None:
        """Store rebase facts and confirm their authenticated readback."""
        if record.repository != f"{self.org}/{self.repo}" or record.state != "active":
            raise ValueError("rebase record does not match the repository")
        self.read_review_rebase_record(record.pr_number)
        owned = [
            str(comment.get("body", ""))
            for comment in self._repo_issue_comments(record.pr_number)
            if self._comment_owned_by_viewer(comment)
        ]
        self._require_original_rebase_audit(record, owned)
        marker, body = render_review_rebase_record(record)
        self.upsert_issue_comment(record.pr_number, marker, body)
        if self.read_review_rebase_record(record.pr_number) != record:
            raise RuntimeError("rebase record readback failed")

    def read_review_rebase_record(self, pr_number: int) -> RebaseReviewRecord | None:
        """Read one owned record and its original audit without restoring proof."""
        owned = [
            str(comment.get("body", ""))
            for comment in self._repo_issue_comments(pr_number)
            if self._comment_owned_by_viewer(comment)
        ]
        records = [
            parse_review_rebase_record(body)
            for body in owned
            if body.startswith(REBASE_REVIEW_PREFIX)
        ]
        if not records:
            return None
        if len(records) != 1 or records[0] is None:
            raise RuntimeError("rebase records are ambiguous")
        record = records[0]
        if (
            record.repository != f"{self.org}/{self.repo}"
            or record.pr_number != pr_number
            or record.state == "revoked"
        ):
            raise RuntimeError("rebase record is revoked or does not match")
        self._require_original_rebase_audit(record, owned)
        if record.state == "superseded":
            self._require_superseding_audit(record, owned)
            return None
        return record

    @staticmethod
    def _require_original_rebase_audit(record: RebaseReviewRecord, owned: list[str]) -> None:
        """Require one unchanged owned audit for the original reviewed head."""
        _, audit_body = render_implementation_go_audit(
            record.audit, pr_number=record.pr_number, head_sha=record.reviewed_head_sha
        )
        matching_audits = [
            body for body in owned if body.partition("\n")[0] == record.original_audit_id
        ]
        if matching_audits != [audit_body]:
            raise RuntimeError("original rebase review audit is absent or changed")

    @staticmethod
    def _require_superseding_audit(record: RebaseReviewRecord, owned: list[str]) -> None:
        """Require one owned clean GO audit for the superseding source commit."""
        matches = [
            audit
            for body in owned
            if (audit := parse_published_implementation_go_audit(body)) is not None
            and audit.pr_number == record.pr_number
            and audit.head_sha == record.superseded_by_head_sha
        ]
        if len(matches) != 1:
            raise RuntimeError("superseding review audit is absent or ambiguous")

    def _supersede_review_rebase_record(self, pr_number: int, head_sha: str) -> None:
        """Retire old recovery facts only after a new clean GO publication."""
        record = self.read_review_rebase_record(pr_number)
        if record is None or record.reviewed_head_sha == head_sha:
            return
        state = self.gh_pr_state(pr_number)
        if not isinstance(state, dict) or (
            state.get("state") != "OPEN"
            or state.get("headRefOid") != head_sha
            or state.get("baseRefName") != "main"
            or "autoMergeRequest" not in state
            or state["autoMergeRequest"] is not None
            or self.pr_has_implementation_state_label(pr_number) != (True, False)
        ):
            raise RuntimeError("superseding review head is not exclusive GO")
        retired = replace(record, state="superseded", superseded_by_head_sha=head_sha)
        owned = [
            str(comment.get("body", ""))
            for comment in self._repo_issue_comments(pr_number)
            if self._comment_owned_by_viewer(comment)
        ]
        self._require_superseding_audit(retired, owned)
        marker, body = render_review_rebase_record(retired)
        self.upsert_issue_comment(pr_number, marker, body)
        visible = [
            comment
            for comment in self._repo_issue_comments(pr_number)
            if self._comment_owned_by_viewer(comment) and str(comment.get("body", "")) == body
        ]
        if len(visible) != 1 or self.read_review_rebase_record(pr_number) is not None:
            raise RuntimeError("rebase supersession readback failed")

    def persist_pending_implementation_go_audit(
        self, pr_number: int, head_sha: str, audit: ReviewAudit
    ) -> None:
        """Upsert and read back the exact receipt before the GO transition."""
        marker, body = render_pending_implementation_go_audit(pr_number, head_sha, audit)
        self.upsert_issue_comment(pr_number, marker, body)
        comments = self._repo_issue_comments(pr_number)
        if not any(
            self._comment_owned_by_viewer(comment) and str(comment.get("body", "")) == body
            for comment in comments
        ):
            raise RuntimeError("pending implementation-go audit receipt was not visible")

    def pending_implementation_go_audit(
        self, pr_number: int
    ) -> PendingImplementationGoAudit | None:
        """Recover the newest actor-owned pending receipt for this PR."""
        recovered: PendingImplementationGoAudit | None = None
        owned_bodies: list[str] = []
        for comment in self._repo_issue_comments(pr_number):
            if not self._comment_owned_by_viewer(comment):
                continue
            body = str(comment.get("body", ""))
            owned_bodies.append(body)
            if not body.startswith(IMPLEMENTATION_GO_AUDIT_PENDING_PREFIX):
                continue
            try:
                receipt = parse_pending_implementation_go_audit(body)
            except LegacyPendingImplementationGoAuditError as error:
                # Version 1 predates the typed verdict, so it cannot prove GO.
                # Preserve it as an invalid sentinel so a stale GO label cannot
                # route a restarted process to merge_wait.
                if error.pr_number != pr_number:
                    raise RuntimeError(
                        "pending implementation-go audit receipt is invalid"
                    ) from error
                recovered = PendingImplementationGoAudit(
                    pr_number=error.pr_number,
                    head_sha=error.head_sha,
                    audit=ReviewAudit(
                        grade=None,
                        summary="Legacy implementation-go audit requires a fresh review.",
                        findings=(),
                        raw_feedback="",
                        valid=False,
                        verdict=None,
                    ),
                )
                continue
            if receipt is None or receipt.pr_number != pr_number:
                raise RuntimeError("pending implementation-go audit receipt is invalid")
            recovered = receipt
        if recovered is not None:
            return recovered
        public_receipts = [
            receipt
            for body in owned_bodies
            if (receipt := parse_published_implementation_go_audit(body)) is not None
            and receipt.pr_number == pr_number
        ]
        for receipt in reversed(public_receipts):
            if any(
                _IMPLEMENTATION_REPLY_HANDOFF_MARKER_RE.fullmatch(body.partition("\n")[0])
                and f"pr={pr_number}:head={receipt.head_sha}:" in body.partition("\n")[0]
                for body in owned_bodies
            ):
                # The public comment survived a crash before its exact-head
                # handoff cleanup. Re-enter publication reconciliation so the
                # public readback can authorize that deletion.
                return receipt
        return None

    def clear_pending_implementation_go_audit(self, pr_number: int, head_sha: str) -> None:
        """Delete only actor-owned pending receipts for the published exact head."""
        for comment in self._repo_issue_comments(pr_number):
            if not self._comment_owned_by_viewer(comment):
                continue
            body = str(comment.get("body", ""))
            if not body.startswith(IMPLEMENTATION_GO_AUDIT_PENDING_PREFIX):
                continue
            try:
                receipt = parse_pending_implementation_go_audit(body)
            except LegacyPendingImplementationGoAuditError as error:
                if error.pr_number != pr_number:
                    raise RuntimeError(
                        "pending implementation-go audit receipt is invalid"
                    ) from error
                if error.head_sha == head_sha:
                    comment_id = comment.get("databaseId")
                    if comment_id is None:
                        raise RuntimeError(
                            "pending implementation-go audit receipt has no database id"
                        ) from error
                    self._delete_issue_comment(int(comment_id))
                continue
            if receipt is None or receipt.pr_number != pr_number:
                raise RuntimeError("pending implementation-go audit receipt is invalid")
            if receipt.head_sha != head_sha:
                continue
            comment_id = comment.get("databaseId")
            if comment_id is None:
                raise RuntimeError("pending implementation-go audit receipt has no database id")
            self._delete_issue_comment(int(comment_id))

    def publish_implementation_go_audit(
        self, pr_number: int, head_sha: str, audit: ReviewAudit
    ) -> None:
        """Publish exactly one public audit, then remove matching recovery journals."""
        marker, body = render_implementation_go_audit(audit, pr_number=pr_number, head_sha=head_sha)
        pending_marker, pending_body = render_pending_implementation_go_audit(
            pr_number, head_sha, audit
        )
        comments = self._promote_pending_implementation_go_audit(
            pr_number,
            marker=marker,
            body=body,
            pending_marker=pending_marker,
            pending_body=pending_body,
        )
        comments = self._converge_implementation_go_audits(
            pr_number, marker=marker, body=body, comments=comments
        )
        if any(
            self._comment_owned_by_viewer(comment)
            and str(comment.get("body", "")).startswith(REBASE_REVIEW_PREFIX)
            for comment in comments
        ):
            self._supersede_review_rebase_record(pr_number, head_sha)
        for comment in comments:
            if not self._comment_owned_by_viewer(comment):
                continue
            first_line = str(comment.get("body", "")).split("\n", 1)[0]
            if (
                _IMPLEMENTATION_REPLY_HANDOFF_MARKER_RE.fullmatch(first_line) is None
                or f"pr={pr_number}:head={head_sha}:" not in first_line
            ):
                continue
            comment_id = comment.get("databaseId")
            if comment_id is None:
                raise RuntimeError("owned implementation reply handoff has no database id")
            self._delete_issue_comment(int(comment_id))

    @staticmethod
    def _marker_comments(comments: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
        """Return comments whose bodies begin with one exact opaque marker."""
        return [
            comment
            for comment in comments
            if has_exact_leading_marker(str(comment.get("body", "")), marker)
        ]

    def _promote_pending_implementation_go_audit(
        self,
        pr_number: int,
        *,
        marker: str,
        body: str,
        pending_marker: str,
        pending_body: str,
    ) -> list[dict[str, Any]]:
        """Promote the durable receipt in place and return a fresh comment read."""
        comments = self._repo_issue_comments(pr_number)
        visible_public = [
            comment
            for comment in self._marker_comments(comments, marker)
            if self._comment_owned_by_viewer(comment)
        ]
        if any(str(comment.get("body", "")) == body for comment in visible_public):
            return comments
        if visible_public:
            if len(visible_public) != 1:
                raise RuntimeError("implementation-go audit comment identity is ambiguous")
            public_id = visible_public[0].get("databaseId")
            if public_id is None:
                raise RuntimeError("owned implementation-go audit has no database id")
            # A version-1 public audit has this exact marker but lacks the
            # typed verdict. Replace only the one actor-owned marker match.
            self._patch_issue_comment(int(public_id), body)
            return self._repo_issue_comments(pr_number)
        pending = [
            comment
            for comment in self._marker_comments(comments, pending_marker)
            if self._comment_owned_by_viewer(comment)
            and str(comment.get("body", "")) == pending_body
        ]
        has_rebase_history = any(
            self._comment_owned_by_viewer(comment)
            and str(comment.get("body", "")).startswith(REBASE_REVIEW_PREFIX)
            for comment in comments
        )
        if pending and not has_rebase_history:
            pending_id = pending[-1].get("databaseId")
            if pending_id is None:
                raise RuntimeError("pending implementation-go audit has no database id")
            self._patch_issue_comment(int(pending_id), body)
        else:
            self.upsert_issue_comment(pr_number, marker, body)
        return self._repo_issue_comments(pr_number)

    def _converge_implementation_go_audits(
        self,
        pr_number: int,
        *,
        marker: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Converge visible actor-owned audit comments to one exact body."""
        owned_audits = [
            comment
            for comment in self._marker_comments(comments, marker)
            if self._comment_owned_by_viewer(comment)
        ]
        exact_audits = [comment for comment in owned_audits if str(comment.get("body", "")) == body]
        if not exact_audits:
            raise RuntimeError("implementation-go audit comment was not visible after write")
        canonical = exact_audits[-1]
        if canonical.get("databaseId") is None:
            raise RuntimeError("owned implementation-go audit has no database id")
        for duplicate in owned_audits:
            if duplicate is canonical:
                continue
            duplicate_id = duplicate.get("databaseId")
            if duplicate_id is None:
                raise RuntimeError("owned implementation-go audit has no database id")
            self._delete_issue_comment(int(duplicate_id))
        comments = self._repo_issue_comments(pr_number)
        visible_audits = [
            comment
            for comment in comments
            if self._comment_owned_by_viewer(comment)
            and has_exact_leading_marker(str(comment.get("body", "")), marker)
        ]
        if len(visible_audits) != 1 or str(visible_audits[0].get("body", "")) != body:
            raise RuntimeError("implementation-go audit comments did not converge after write")
        return comments
