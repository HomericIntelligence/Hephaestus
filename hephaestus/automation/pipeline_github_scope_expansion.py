"""Scope-expansion helpers for the GitHub adapter façade."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import hephaestus.automation.github_api as github_api
from hephaestus.automation.github_api import gh_call

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *  # noqa: F403
from .review_journal import has_exact_leading_marker


class PipelineGitHubScopeExpansion(_PipelineGitHubHost):
    """Scope-expansion issue and review helpers for ``PipelineGitHub``."""

    def _repo_issues(self, state: str) -> list[dict[str, Any]]:
        """Return every issue page for this repository with strict pagination."""
        if self._repo_slug is None:
            raise RuntimeError("issue discovery requires a repo-scoped PipelineGitHub")
        issues: list[dict[str, Any]] = []
        for page_number in range(1, 101):
            result = self._gh(
                [
                    "issue",
                    "list",
                    "--state",
                    state,
                    "--limit",
                    "100",
                    "--page",
                    str(page_number),
                    "--json",
                    "number,title,body,state,url",
                ]
            )
            page = json.loads(result.stdout or "[]")
            if not isinstance(page, list):
                raise RuntimeError("issue list is malformed")
            for issue in page:
                if not isinstance(issue, dict):
                    raise RuntimeError("issue list is malformed")
                issues.append(dict(issue))
            if len(page) < 100:
                return issues
        raise RuntimeError("issue list traversal exceeded its bound")

    def all_repo_issues(self) -> list[dict[str, Any]]:
        """Return the complete repository issue journal (open and closed)."""
        return self._repo_issues("all")

    def issues_with_marker(self, marker: str) -> list[dict[str, Any]]:
        """Return every issue whose body begins with the exact marker."""
        return [
            issue
            for issue in self.all_repo_issues()
            if has_exact_leading_marker(str(issue.get("body") or ""), marker)
        ]

    def issue_with_marker(self, marker: str) -> dict[str, Any] | None:
        """Return the unique issue whose body begins with the exact marker."""
        matches = self.issues_with_marker(marker)
        if not matches:
            return None
        if len(matches) > 1:
            raise RuntimeError("issue marker matched multiple repository issues")
        return matches[0]

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
        """Durably ensure the issue exists and return its number (idempotent)."""
        if self._skip(f"create issue {title!r}"):
            return 0
        if self._repo_slug is not None and labels:
            existing = self._label_names()
            for label in labels:
                if label not in existing:
                    self._create_label(label)
                    existing.add(label)
        try:
            with github_api._body_file(body) as body_path:
                cmd = [
                    "issue",
                    "create",
                    "--title",
                    github_api.strip_null_bytes(title),
                    "--body-file",
                    body_path,
                ]
                if labels:
                    for label in labels:
                        cmd.extend(["--label", label])
                result = self._gh(cmd)
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            raise RuntimeError(f"failed to create issue {title!r}: {exc}") from exc
        output = (result.stdout or "").strip()
        match = re.search(r"/issues/(\d+)", output)
        if match is not None:
            return int(match.group(1))
        try:
            return int(output.rsplit("/", 1)[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"failed to parse issue number from output: {output!r}") from exc

    def post_scope_expansion_blocking_review(
        self,
        pr_number: int,
        *,
        body: str,
        marker: str,
    ) -> str:
        """Post one idempotent COMMENTED review that records a child-issue block."""
        if not has_exact_leading_marker(body, marker):
            raise ValueError(f"blocking review body must start with marker {marker!r}")
        if self._skip(f"post scope-expansion blocking review on PR #{pr_number}"):
            return ""
        owner, name = self._owner_name()
        matching = [
            review
            for review in self.merge_authorization_reviews(pr_number)
            if review.get("state") == "COMMENTED"
            and review.get("viewerDidAuthor") is True
            and str(review.get("body") or "") == body
        ]
        if matching:
            review_id = matching[-1].get("id")
            if not isinstance(review_id, str) or not review_id:
                raise RuntimeError("blocking review id is unavailable")
            return review_id
        request_body = json.dumps({"event": "COMMENT", "body": body})
        with github_api._body_file(request_body) as input_path:
            result = gh_call(
                [
                    "api",
                    "-X",
                    "POST",
                    f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
                    "--input",
                    input_path,
                ],
                timeout=self._gh_timeout,
            )
        review = json.loads(result.stdout or "{}")
        review_id = review.get("id")
        if not isinstance(review_id, str) or not review_id:
            review_id = review.get("node_id")
        if not isinstance(review_id, str) or not review_id:
            raise RuntimeError("blocking review publication was not confirmed")
        matching = [
            review
            for review in self.merge_authorization_reviews(pr_number)
            if review.get("state") == "COMMENTED"
            and review.get("viewerDidAuthor") is True
            and str(review.get("body") or "") == body
        ]
        if not matching:
            raise RuntimeError("blocking review publication was not confirmed")
        if review_id not in {str(review.get("id") or "") for review in matching}:
            raise RuntimeError("blocking review publication returned an unexpected review id")
        return review_id
