"""Digest-guarded issue-body reads and replacement."""

from __future__ import annotations

import json
import re
import subprocess

import hephaestus.automation.github_api as github_api

from .pipeline.stages.base import IssueBodyReplacementResult
from .pipeline_github_contract import _PipelineGitHubHost

_PUBLICATION_REJECTION_MARKERS = (
    "body is too long",
    "http 422",
    "unprocessable entity",
    "validation failed",
)


def _is_publication_rejection(error: BaseException) -> bool:
    """Return whether GitHub explicitly rejected the submitted body."""
    detail = str(error).casefold()
    return any(marker in detail for marker in _PUBLICATION_REJECTION_MARKERS)


class PipelineGitHubIssueBodies(_PipelineGitHubHost):
    """Own optimistic-concurrency issue-body replacement."""

    def replace_issue_body_if_unchanged(
        self,
        issue_number: int,
        expected_body_digest: str,
        new_body: str,
    ) -> IssueBodyReplacementResult:
        """Replace an issue body after a fresh digest check and exact readback."""
        if issue_number <= 0:
            raise ValueError("issue_number must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_body_digest):
            raise ValueError("expected_body_digest must be a lowercase SHA-256 digest")
        if self._skip(f"replace body of issue #{issue_number} if unchanged"):
            return IssueBodyReplacementResult(dry_run=True)

        try:
            current = self.gh_issue_json(issue_number)
        except (
            subprocess.SubprocessError,
            RuntimeError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            return IssueBodyReplacementResult(retryable=True)
        current_digest = current.get("bodyDigest")
        current_body = current.get("body")
        if not isinstance(current_digest, str) or not isinstance(current_body, str):
            return IssueBodyReplacementResult(retryable=True)
        if current_digest != expected_body_digest:
            return IssueBodyReplacementResult(conflict=True, body_digest=current_digest)

        try:
            with github_api._body_file(new_body) as path:
                self._gh(["issue", "edit", str(issue_number), "--body-file", path])
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            if _is_publication_rejection(exc):
                return IssueBodyReplacementResult(rejected=True)
            return IssueBodyReplacementResult(retryable=True)

        try:
            readback = self.gh_issue_json(issue_number)
        except (
            subprocess.SubprocessError,
            RuntimeError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            return IssueBodyReplacementResult(retryable=True)
        readback_body = readback.get("body")
        readback_digest = readback.get("bodyDigest")
        expected_new_digest = github_api.issue_body_digest(new_body)
        if readback_body != new_body or readback_digest != expected_new_digest:
            return IssueBodyReplacementResult(
                retryable=True,
                body_digest=readback_digest if isinstance(readback_digest, str) else None,
            )
        return IssueBodyReplacementResult(replaced=True, body_digest=expected_new_digest)
