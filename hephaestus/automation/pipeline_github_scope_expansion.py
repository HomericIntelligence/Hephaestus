"""Scope-expansion helpers for the GitHub adapter façade."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

from hephaestus.automation.github_api import (
    _body_file as github_body_file,
    gh_call as direct_gh_call,
    scope_expansion_issue_owner_query,
    strip_null_bytes,
)

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *  # noqa: F403
from .review_journal import has_exact_leading_marker
from .session_naming import issue_auto_impl_branch_name


class PipelineGitHubScopeExpansion(_PipelineGitHubHost):
    """Scope-expansion issue and review helpers for ``PipelineGitHub``."""

    def _repo_issues(self, state: str) -> list[dict[str, Any]]:
        """Return every issue page for this repository with strict pagination."""
        if self._repo_slug is None:
            raise RuntimeError("issue discovery requires a repo-scoped PipelineGitHub")
        if state not in {"open", "closed", "all"}:
            raise ValueError("issue state must be open, closed, or all")
        owner, name = self._owner_name()
        issues: list[dict[str, Any]] = []
        for page_number in range(1, 101):
            result = direct_gh_call(
                [
                    "api",
                    "--method",
                    "GET",
                    f"repos/{owner}/{name}/issues?state={state}&per_page=100&page={page_number}",
                ],
                timeout=self._gh_timeout,
            )
            page = json.loads(result.stdout or "[]")
            if not isinstance(page, list):
                raise RuntimeError("issue list is malformed")
            for issue in page:
                if not isinstance(issue, dict):
                    raise RuntimeError("issue list is malformed")
                if "pull_request" in issue:
                    continue
                issues.append(dict(issue))
            if len(page) < 100:
                return issues
        raise RuntimeError("issue list traversal exceeded its bound")

    def all_repo_issues(self) -> list[dict[str, Any]]:
        """Return the complete repository issue journal (open and closed)."""
        return self._repo_issues("all")

    def issues_with_marker(self, marker: str) -> list[dict[str, Any]]:
        """Return actor-owned issues whose body begins with the exact marker."""
        owner, name = self._owner_name()
        matches: list[dict[str, Any]] = []
        for issue in self.all_repo_issues():
            body = issue.get("body")
            if not isinstance(body, str) or not has_exact_leading_marker(body, marker):
                continue
            issue_number = issue.get("number")
            if (
                isinstance(issue_number, bool)
                or not isinstance(issue_number, int)
                or issue_number <= 0
            ):
                raise RuntimeError("marker-matched issue identity is malformed")
            proof = self._graphql(
                scope_expansion_issue_owner_query(owner, name, issue_number),
                number=issue_number,
            )
            if proof.get("body") != body:
                raise RuntimeError("marker-matched issue changed during ownership verification")
            if proof.get("viewer_did_author") is True:
                matches.append(issue)
        return matches

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
            with github_body_file(body) as body_path:
                cmd = [
                    "issue",
                    "create",
                    "--title",
                    strip_null_bytes(title),
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

    def _scope_expansion_timeline_prs(  # noqa: C901
        self, issue_number: int
    ) -> set[int]:
        """Return same-repository PRs that reference a child issue timeline."""
        if self._repo_slug is None:
            raise RuntimeError("association discovery requires a repo-scoped PipelineGitHub")
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("association discovery requires a positive issue number")
        owner, name = self._owner_name()
        expected_repository_path = f"/repos/{owner}/{name}".casefold()
        associations: set[int] = set()
        for page_number in range(1, 101):
            result = direct_gh_call(
                [
                    "api",
                    "--method",
                    "GET",
                    (
                        f"repos/{owner}/{name}/issues/{issue_number}/timeline"
                        f"?per_page=100&page={page_number}"
                    ),
                ],
                timeout=self._gh_timeout,
            )
            page = json.loads(result.stdout or "[]")
            if not isinstance(page, list):
                raise RuntimeError("child issue timeline is malformed")
            for event in page:
                if not isinstance(event, dict):
                    raise RuntimeError("child issue timeline is malformed")
                if event.get("event") != "cross-referenced":
                    continue
                source = event.get("source")
                if not isinstance(source, dict) or source.get("type") != "issue":
                    raise RuntimeError("child pull-request association is malformed")
                issue = source.get("issue")
                if not isinstance(issue, dict):
                    raise RuntimeError("child pull-request association is malformed")
                source_number = issue.get("number")
                repository_url = issue.get("repository_url")
                if (
                    isinstance(source_number, bool)
                    or not isinstance(source_number, int)
                    or source_number <= 0
                    or not isinstance(repository_url, str)
                ):
                    raise RuntimeError("child pull-request association is malformed")
                repository = urlsplit(repository_url)
                repository_path = repository.path.rstrip("/")
                if (
                    not repository.scheme
                    or not repository.netloc
                    or "/repos/" not in repository_path
                ):
                    raise RuntimeError("child pull-request association is malformed")
                pull_request = issue.get("pull_request")
                if pull_request is None:
                    continue
                if not isinstance(pull_request, dict):
                    raise RuntimeError("child pull-request association is malformed")
                pull_url = pull_request.get("url")
                if not isinstance(pull_url, str):
                    raise RuntimeError("child pull-request association is malformed")
                pull = urlsplit(pull_url)
                if (
                    pull.scheme != repository.scheme
                    or pull.netloc != repository.netloc
                    or pull.path.rstrip("/") != f"{repository_path}/pulls/{source_number}"
                ):
                    raise RuntimeError("child pull-request association is malformed")
                if repository_path.casefold().endswith(expected_repository_path):
                    associations.add(source_number)
            if len(page) < 100:
                return associations
        raise RuntimeError("child issue timeline traversal exceeded its bound")

    def _scope_expansion_canonical_branch_prs(self, issue_number: int) -> set[int]:
        """Return PRs that use the canonical child implementation branch."""
        if self._repo_slug is None:
            raise RuntimeError("association discovery requires a repo-scoped PipelineGitHub")
        owner, name = self._owner_name()
        branch = issue_auto_impl_branch_name(issue_number)
        associations: set[int] = set()
        for page_number in range(1, 101):
            result = direct_gh_call(
                [
                    "api",
                    "--method",
                    "GET",
                    (
                        f"repos/{owner}/{name}/pulls?state=all&head={owner}:{branch}"
                        f"&per_page=100&page={page_number}"
                    ),
                ],
                timeout=self._gh_timeout,
            )
            page = json.loads(result.stdout or "[]")
            if not isinstance(page, list):
                raise RuntimeError("canonical child pull-request list is malformed")
            for candidate in page:
                if not isinstance(candidate, dict):
                    raise RuntimeError("canonical child pull-request list is malformed")
                number = candidate.get("number")
                head = candidate.get("head")
                head_repo = head.get("repo") if isinstance(head, dict) else None
                if (
                    isinstance(number, bool)
                    or not isinstance(number, int)
                    or number <= 0
                    or not isinstance(head, dict)
                    or head.get("ref") != branch
                    or not isinstance(head_repo, dict)
                    or str(head_repo.get("full_name") or "").casefold()
                    != self._repo_slug.casefold()
                ):
                    raise RuntimeError("canonical child pull-request list is malformed")
                associations.add(number)
            if len(page) < 100:
                return associations
        raise RuntimeError("canonical child pull-request traversal exceeded its bound")

    def _scope_expansion_merge_evidence(self, pr_number: int) -> dict[str, str] | None:
        """Return strict main-merge evidence for one associated PR."""
        result = self._gh(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,state,mergedAt,mergeCommit,baseRefName",
            ]
        )
        value = json.loads(result.stdout or "{}")
        if not isinstance(value, dict):
            raise RuntimeError("child pull-request evidence is malformed")
        if value.get("number") != pr_number:
            raise RuntimeError("child pull-request identity is malformed")
        if str(value.get("state") or "").upper() != "MERGED":
            return None
        if not isinstance(value.get("mergedAt"), str) or not value["mergedAt"]:
            raise RuntimeError("child pull-request merge timestamp is unavailable")
        if value.get("baseRefName") != "main":
            raise RuntimeError("child pull request did not merge into main")
        merge_commit = value.get("mergeCommit")
        merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        if not isinstance(merge_sha, str) or re.fullmatch(r"[0-9a-f]{40}", merge_sha) is None:
            raise RuntimeError("child pull-request merge SHA is unavailable")
        return {"merge_sha": merge_sha, "base_branch": "main"}

    def merged_scope_expansion_pr(
        self,
        issue_number: int,
        *,
        source_pr_number: int | None = None,
    ) -> dict[str, str] | None:
        """Return unique main-merge evidence associated with a child issue."""
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("association discovery requires a positive issue number")
        if source_pr_number is not None and (
            isinstance(source_pr_number, bool)
            or not isinstance(source_pr_number, int)
            or source_pr_number <= 0
        ):
            raise ValueError("source pull-request identity must be a positive number")
        associations = self._scope_expansion_timeline_prs(issue_number)
        associations.update(self._scope_expansion_canonical_branch_prs(issue_number))
        if source_pr_number is not None:
            associations.discard(source_pr_number)
        if len(associations) > 1:
            raise RuntimeError("child issue has multiple implementation pull requests")
        if not associations:
            return None
        return self._scope_expansion_merge_evidence(next(iter(associations)))

    def commit_is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        """Return whether one full commit is an ancestor of another commit."""
        if self._repo_slug is None:
            raise RuntimeError("commit comparison requires a repo-scoped PipelineGitHub")
        if re.fullmatch(r"[0-9a-f]{40}", ancestor_sha) is None:
            raise ValueError("commit comparison requires a full lowercase ancestor SHA")
        if descendant_sha != "main" and re.fullmatch(r"[0-9a-f]{40}", descendant_sha) is None:
            raise ValueError("commit comparison requires main or a full lowercase descendant SHA")
        owner, name = self._owner_name()
        result = direct_gh_call(
            [
                "api",
                "--method",
                "GET",
                f"repos/{owner}/{name}/compare/{ancestor_sha}...{descendant_sha}",
            ],
            timeout=self._gh_timeout,
        )
        value = json.loads(result.stdout or "{}")
        if not isinstance(value, dict):
            raise RuntimeError("commit comparison is malformed")
        status = value.get("status")
        if status not in {"ahead", "behind", "diverged", "identical"}:
            raise RuntimeError("commit comparison status is unavailable")
        return status in {"ahead", "identical"}

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
        with github_body_file(request_body) as input_path:
            result = direct_gh_call(
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
