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

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from hephaestus.automation import github_api, pr_manager
from hephaestus.automation._review_utils import (
    close_issue_as_covered,
    ensure_state_dir,
    find_merged_closing_pr,
    find_merged_pr_for_issue,
    get_pr_head_branch,
)
from hephaestus.automation.arming_state import (
    ArmingStateStore,
)
from hephaestus.automation.git_utils import issue_auto_impl_branch_name
from hephaestus.automation.pipeline.scope_retraction import (
    SCOPE_RETRACTION_MARKER_PREFIX,
    normalize_scope_retraction_paths,
    scope_retraction_marker,
)
from hephaestus.automation.pipeline.stages.base import (
    ConditionalMergeResult,
    ImplementationThreadReplyResult,
    ReviewerThreadReconciliationResult,
)
from hephaestus.automation.prompts.pr_review import (
    SEVERITY_MARKER_PREFIX,
    VALID_SEVERITIES,
)
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_PREFIX,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    blocked_audit_recovery_body,
    is_plan_comment,
    is_plan_review_comment,
)
from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ALL_STATE_LABELS,
    SKIP_REASON_MARKER,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_LABEL_SPECS,
    STATE_SKIP,
    format_skip_reason_comment,
    has_label,
    is_implementation_go,
)
from hephaestus.constants import read_timeout_env
from hephaestus.github.client import gh_call
from hephaestus.utils.file_lock import LockUnavailableError, file_lock

logger = logging.getLogger(__name__)

_CLOSES_ISSUE_LINE_RE = re.compile(r"^Closes #(\d+)\s*$", re.MULTILINE)
_STANDALONE_VERDICT_LINE_RE = re.compile(r"(?i)^\s*verdict\s*:")
_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+(\d{3})\b", re.MULTILINE)
_IMPLEMENTATION_REPLY_REVIEW_BODY_RE = re.compile(
    r"\AImplementation responses for [1-9]\d* review thread\(s\)\.\n\n"
    r"<!-- hephaestus-implementation-review:[0-9a-f]{24} -->\Z"
)


class DuplicateImplementationReplyDraftsError(RuntimeError):
    """Two current pending drafts make cross-checkout ownership unprovable."""

    def __init__(self, review_ids: tuple[str, ...]) -> None:
        """Record the opaque draft review IDs that made ownership ambiguous."""
        super().__init__("multiple current implementation reply drafts")
        self.review_ids = review_ids


def _parse_included_http_response(
    stdout: str,
) -> tuple[int | None, dict[str, Any] | None, bool]:
    """Parse the final status and JSON object from ``gh api --include`` output.

    A missing HTTP status means the CLI did not provide enough evidence to
    classify the request. A received non-object or invalid body is explicitly
    malformed so callers fail closed rather than treating it as transport
    ambiguity.
    """
    matches = list(_HTTP_STATUS_RE.finditer(stdout))
    if not matches:
        return None, None, False
    status = int(matches[-1].group(1))
    headers_end = re.search(r"\r?\n\r?\n", stdout[matches[-1].start() :])
    if headers_end is None:
        return status, None, True
    body_start = matches[-1].start() + headers_end.end()
    body = stdout[body_start:].strip()
    if not body:
        return status, None, True
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return status, None, True
    return (status, parsed, False) if isinstance(parsed, dict) else (status, None, True)


def rate_limit_remaining() -> tuple[int, int] | None:
    """Return ``(remaining, reset_epoch)`` for the GraphQL budget, or ``None``.

    Feeds the coordinator's non-blocking rate gate. A blocking *sleeping* guard
    would be fatal for a single coordinator thread, so the pipeline timer-parks
    instead (see ``coordinator._rate_budget_ok``).
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
    """Non-blocking GraphQL rate-budget gate for the coordinator.

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


def _with_severity_marker(comment: dict[str, Any]) -> str:
    """Prepend the ``<!-- hephaestus-severity: X -->`` marker line (#1856).

    An absent/unknown severity is written as ``major`` (blocking) so an
    unclassifiable thread never silently unblocks a GO, and so the pre-#1856
    all-blocking behavior is reproduced until the reviewer's severity is seeded.
    """
    sev = str(comment.get("severity") or "").strip().lower()
    if sev not in VALID_SEVERITIES:
        sev = "major"
    body = str(comment.get("body") or "")
    body = "\n".join(
        line
        for line in body.splitlines()
        if not line.strip().startswith(SEVERITY_MARKER_PREFIX)
        and not line.strip().startswith(SCOPE_RETRACTION_MARKER_PREFIX)
        and not _STANDALONE_VERDICT_LINE_RE.match(line)
    )
    paths = normalize_scope_retraction_paths(comment.get("scope_retraction_paths"))
    markers = [f"{SEVERITY_MARKER_PREFIX} {sev} -->"]
    if paths:
        markers.append(scope_retraction_marker(paths))
    markers.append(body)
    return "\n".join(markers)


def _has_no_explicit_pull_request_bypasses(protection: dict[str, Any]) -> bool:
    """Return whether the classic protection response grants no PR bypasses.

    Classic branch protection exposes actor allowances that may bypass pull
    request requirements under ``required_pull_request_reviews``.  A missing
    review-requirement object means this response exposes no such allowance.
    GitHub also omits the allowance field itself when no PR bypass is
    configured. Once the field is present, every allowance collection must be
    present, list-typed, and empty. This is deliberately fail-closed because a
    merge actor must not infer that a malformed allowance is safe.
    """
    reviews = protection.get("required_pull_request_reviews")
    if reviews is None:
        return True
    if not isinstance(reviews, dict):
        return False
    if "bypass_pull_request_allowances" not in reviews:
        return True
    allowances = reviews["bypass_pull_request_allowances"]
    if not isinstance(allowances, dict):
        return False
    for actor_type in ("users", "teams", "apps"):
        actors = allowances.get(actor_type)
        if not isinstance(actors, list) or actors:
            return False
    return True


class PipelineGitHub:
    """Coordinator-owned GitHub accessor implementing ``StageGitHub``.

    Read surface delegates to the existing helpers verbatim; the mutator
    surface maps the coordinator-neutral names onto ``github_api`` /
    ``pr_manager`` / ``_review_utils`` mutators, honoring dry-run inside each
    mutator (log-and-skip) per the ``StageGitHub`` protocol docstring.
    """

    def __init__(
        self,
        org: str,
        *,
        repo: str | None = None,
        dry_run: bool = False,
        repo_root: Path | None = None,
    ) -> None:
        """Initialize the accessor.

        Args:
            org: GitHub organization.
            repo: Repository name for repository-scoped pipeline work. The
                org-only form is retained solely for discovery setup; review
                thread reads and all mutations require a concrete repository.
            dry_run: When True, every mutator logs-and-skips.
            repo_root: Repo checkout root anchoring the drive-green arming
                state dir (defaults to the current working directory).

        """
        self.org = org
        self.repo = repo
        self.dry_run = dry_run
        self._repo_root = repo_root or Path.cwd()
        self._arming = ArmingStateStore(lambda: ensure_state_dir(self._repo_root))
        self._viewer_login_cache: str | None = None

    @property
    def _repo_slug(self) -> str | None:
        if not self.repo:
            return None
        return f"{self.org}/{self.repo}"

    def _owner_name(self) -> tuple[str, str]:
        """Return explicit owner/name for repo-scoped GitHub API calls."""
        if self.repo is None:
            raise RuntimeError("repo-scoped GitHub operation requires a repo")
        return self.org, self.repo

    def _viewer_login(self) -> str:
        """Return the authenticated actor used to own mutable journal comments."""
        if self._viewer_login_cache is None:
            self._viewer_login_cache = github_api.gh_current_login() or ""
        if not self._viewer_login_cache:
            raise RuntimeError("cannot verify GitHub comment ownership: viewer login unavailable")
        return self._viewer_login_cache

    def _comment_owned_by_viewer(self, comment: dict[str, Any]) -> bool:
        """Fail closed unless GitHub proves the current actor authored a comment."""
        if "viewerDidAuthor" in comment:
            return bool(comment.get("viewerDidAuthor"))
        user = comment.get("user") or comment.get("author")
        login = user.get("login") if isinstance(user, dict) else ""
        return bool(login) and str(login).lower() == self._viewer_login().lower()

    def _graphql(self, query: str, **fields: int | str) -> dict[str, Any]:
        """Run a repo-scoped GraphQL query with explicit owner/repo fields."""
        owner, name = self._owner_name()
        argv = ["api", "graphql", "-f", f"query={query}"]
        for key, value in {"owner": owner, "name": name, **fields}.items():
            argv.extend(["-F", f"{key}={value}"])
        result = gh_call(argv)
        data = json.loads(result.stdout or "{}")
        if not isinstance(data, dict):
            raise RuntimeError("GraphQL response was not an object")
        github_api._check_graphql_errors(data, "repo-scoped pipeline GraphQL")
        return data

    def _with_repo(self, argv: list[str]) -> list[str]:
        """Append an explicit repo selector when this accessor is repo-scoped."""
        if self._repo_slug is None:
            return argv
        return [*argv, "--repo", self._repo_slug]

    def _gh(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return gh_call(self._with_repo(argv), **kwargs)

    def _label_names(self) -> set[str]:
        if self._repo_slug is None:
            # Org-scoped fallback: always re-fetch so a multithreaded
            # coordinator never trusts another repo's slug-keyed entry (#1858).
            return github_api.gh_list_labels(refresh=True)
        result = self._gh(["label", "list", "--json", "name", "--limit", "200"])
        data = json.loads(result.stdout or "[]")
        return {str(item["name"]) for item in data if isinstance(item, dict) and item.get("name")}

    def _create_label(self, name: str) -> None:
        spec = STATE_LABEL_SPECS.get(name, {})
        cmd = ["label", "create", name, "--color", spec.get("color", "ededed"), "--force"]
        if desc := spec.get("description", ""):
            cmd.extend(["--description", desc])
        self._gh(cmd)

    def _add_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        existing = self._label_names()
        for label in labels:
            if label not in existing:
                self._create_label(label)
                existing.add(label)
        cmd = ["issue", "edit", str(issue_number)]
        for label in labels:
            cmd.extend(["--add-label", label])
        self._gh(cmd)

    def _remove_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        existing = self._label_names()
        labels_to_remove = [label for label in labels if label in existing]
        if not labels_to_remove:
            return
        cmd = ["issue", "edit", str(issue_number)]
        for label in labels_to_remove:
            cmd.extend(["--remove-label", label])
        self._gh(cmd)

    @staticmethod
    def _label_names_from_payload(payload: dict[str, Any]) -> list[str]:
        labels = payload.get("labels")
        if not isinstance(labels, list):
            return []
        names: list[str] = []
        for label in labels:
            if isinstance(label, str):
                names.append(label)
            elif isinstance(label, dict) and isinstance(label.get("name"), str):
                names.append(str(label["name"]))
        return names

    def _comments_have_plan(self, comments: Any) -> bool:
        """Return whether an actor-owned canonical plan artifact is present.

        Comment markers locate data; they never establish state.  Requiring
        GitHub's ownership proof prevents a foreign comment from impersonating
        the pipeline's canonical plan and influencing stage orchestration.
        """
        if not isinstance(comments, list):
            return False
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            if not self._comment_owned_by_viewer(comment):
                continue
            body = comment.get("body")
            if not isinstance(body, str):
                continue
            stripped = body.lstrip()
            if is_plan_review_comment(stripped):
                continue
            if is_plan_comment(stripped):
                return True
        return False

    def _open_prs_for_branch(self, branch_name: str) -> list[tuple[int, str]]:
        """Return open PRs on ``branch_name`` without altering auto-merge."""
        discovery_error: github_api.OpenPrDiscoveryIncompleteError | None = None
        try:
            open_prs = github_api._find_open_prs_for_head(branch_name, self._gh)
        except github_api.OpenPrDiscoveryIncompleteError as exc:
            open_prs = exc.open_prs
            discovery_error = exc
        if discovery_error is not None:
            raise RuntimeError(
                f"could not verify existing PR state for head {branch_name!r}"
            ) from discovery_error
        return open_prs

    def _find_open_pr_for_branch(self, branch_name: str) -> int | None:
        """Select the unique ``main`` target among open head PRs."""
        open_prs = self._open_prs_for_branch(branch_name)
        return github_api._select_open_pr_for_base(open_prs, "main")

    def _verified_open_pr_head_branch(self, pr_number: int, issue_number: int) -> str:
        """Return the nonblank head branch of an open fallback PR or fail closed."""
        try:
            result = self._gh(["pr", "view", str(pr_number), "--json", "headRefName"])
            stdout = result.stdout
            if not isinstance(stdout, str) or not stdout.strip():
                raise ValueError("empty PR-head response")
            data = json.loads(stdout)
            if not isinstance(data, dict):
                raise ValueError("PR-head response was not an object")
            head_ref_name = data.get("headRefName")
            if not isinstance(head_ref_name, str) or not head_ref_name.strip():
                raise ValueError("PR-head response omitted a usable head")
        except (AttributeError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"could not verify existing PR state for issue #{issue_number}"
            ) from exc
        return head_ref_name.strip()

    def _find_pr_on_branch(self, branch_name: str, state: str, issue_number: int) -> int | None:
        """Return one validated non-open PR on the canonical issue branch."""
        result = self._gh(
            [
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                state,
                "--json",
                "number",
                "--limit",
                "1",
            ]
        )
        stdout = result.stdout
        if not isinstance(stdout, str) or not stdout.strip():
            raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
        pr_data = json.loads(stdout)
        if not isinstance(pr_data, list):
            raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
        if not pr_data:
            return None
        first_pr = pr_data[0]
        if not isinstance(first_pr, dict):
            raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
        number = first_pr.get("number")
        if not isinstance(number, int) or number <= 0:
            raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
        return number

    def _find_closing_pr(self, issue_number: int, state: str) -> int | None:
        """Return a validated PR with an exact ``Closes #issue`` line."""
        result = self._gh(
            [
                "pr",
                "list",
                "--state",
                state,
                "--search",
                f"Closes #{issue_number} in:body",
                "--json",
                "number,body",
                "--limit",
                "1000",
            ]
        )
        stdout = result.stdout
        if not isinstance(stdout, str) or not stdout.strip():
            raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
        candidates = json.loads(stdout)
        if not isinstance(candidates, list) or len(candidates) >= 1000:
            raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
        closes_pattern = re.compile(rf"^Closes #{issue_number}\b", re.MULTILINE)
        matching_pr: int | None = None
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
            body = candidate.get("body")
            number = candidate.get("number")
            if not isinstance(body, str) or not isinstance(number, int) or number <= 0:
                raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
            if closes_pattern.search(body):
                if state.lower() == "open":
                    head_branch = self._verified_open_pr_head_branch(number, issue_number)
                    open_prs = self._open_prs_for_branch(head_branch)
                    if number not in {open_pr_number for open_pr_number, _base in open_prs}:
                        raise RuntimeError(
                            f"could not verify existing PR state for issue #{issue_number}"
                        )
                if matching_pr is None:
                    matching_pr = number
        return matching_pr

    def _find_pr_for_issue(self, issue_number: int, *, state: str) -> int | None:
        if state.lower() == "open":
            selected_pr = self._find_open_pr_for_branch(issue_auto_impl_branch_name(issue_number))
            if selected_pr is not None:
                return selected_pr
        else:
            selected_pr = self._find_pr_on_branch(
                issue_auto_impl_branch_name(issue_number), state, issue_number
            )
            if selected_pr is not None:
                return selected_pr
        return self._find_closing_pr(issue_number, state)

    def _repo_unresolved_threads(  # noqa: C901 - complete thread hydration is fail-closed
        self, pr_number: int
    ) -> list[dict[str, Any]]:
        """List unresolved PR review threads for this accessor's explicit repo."""
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$after:String){"
            "  repository(owner:$owner,name:$name){"
            "    pullRequest(number:$number){"
            "      reviewThreads(first:100,after:$after){"
            "        pageInfo{ hasNextPage endCursor }"
            "        nodes{ id isResolved }"
            "      }"
            "    }"
            "  }"
            "}"
        )

        def read_thread_ids() -> tuple[str, ...]:
            """Read one complete unresolved-thread traversal without hydrating it."""
            thread_ids: list[str] = []
            seen: set[str] = set()
            seen_cursors: set[str] = set()
            after: str | None = None
            while True:
                fields: dict[str, int | str] = {"number": int(pr_number)}
                if after is not None:
                    fields["after"] = after
                data = self._graphql(query, **fields)
                data_node = data.get("data") if isinstance(data, dict) else None
                repository = data_node.get("repository") if isinstance(data_node, dict) else None
                pull_request = (
                    repository.get("pullRequest") if isinstance(repository, dict) else None
                )
                review_threads = (
                    pull_request.get("reviewThreads") if isinstance(pull_request, dict) else None
                )
                if not isinstance(review_threads, dict):
                    raise RuntimeError("could not fetch all PR review threads")
                nodes = review_threads.get("nodes")
                if not isinstance(nodes, list):
                    raise RuntimeError("could not fetch all PR review threads")
                for node in nodes:
                    if not isinstance(node, dict):
                        raise RuntimeError("could not fetch all PR review threads")
                    is_resolved = node.get("isResolved")
                    thread_id = node.get("id")
                    if (
                        not isinstance(is_resolved, bool)
                        or not isinstance(thread_id, str)
                        or not thread_id
                        or thread_id in seen
                    ):
                        raise RuntimeError("could not fetch all PR review threads")
                    seen.add(thread_id)
                    if not is_resolved:
                        thread_ids.append(thread_id)
                page_info = review_threads.get("pageInfo")
                if not isinstance(page_info, dict) or not isinstance(
                    page_info.get("hasNextPage"), bool
                ):
                    raise RuntimeError("could not fetch all PR review threads")
                if not page_info["hasNextPage"]:
                    return tuple(thread_ids)
                next_cursor = page_info.get("endCursor")
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor in seen_cursors
                ):
                    raise RuntimeError("could not fetch all PR review threads")
                seen_cursors.add(next_cursor)
                after = next_cursor

        first_ids = read_thread_ids()
        if first_ids != read_thread_ids():
            raise RuntimeError("could not stabilize all PR review threads")
        threads: list[dict[str, Any]] = []
        for thread_id in first_ids:
            # A review-thread list deliberately does not request a bounded
            # nested comments connection.  Fetch the complete, paginated
            # node snapshot instead: all turns must reach the implementer
            # and reviewer, including long-lived conversations.
            snapshot = self._review_thread_snapshot(pr_number, thread_id)
            if snapshot is None:
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            if snapshot.get("isResolved") is True:
                # The thread was closed between the list and node reads.
                continue
            comments = snapshot.get("comments")
            if not isinstance(comments, list) or self._thread_comment_snapshot(snapshot) is None:
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            first_comment = comments[0]
            authors: list[str] = []
            review_body = ""
            review_commit_sha = ""
            for comment in comments:
                if not isinstance(comment, dict):
                    raise RuntimeError(
                        f"could not fetch all comments for PR review thread {thread_id}"
                    )
                author = comment.get("author")
                if not isinstance(author, str):
                    raise RuntimeError(
                        f"could not fetch all comments for PR review thread {thread_id}"
                    )
                if author:
                    authors.append(author)
                if not review_body:
                    review_body = str(comment.get("review_body") or "")
                    review_commit_sha = str(comment.get("review_commit_sha") or "")
            thread = {
                "id": thread_id,
                "path": snapshot.get("path", ""),
                "line": snapshot.get("line"),
                "side": snapshot.get("side") or "RIGHT",
                "body": first_comment.get("body", ""),
                "author": authors[0] if authors else "",
                "author_type": comments[0].get("author_type", "") if comments else "",
                "authors": authors,
                "comments": [dict(comment) for comment in comments],
                "review_id": comments[0].get("review_id", "") if comments else "",
                "review_body": review_body,
                "review_commit_sha": review_commit_sha,
                "pr_state": snapshot.get("pr_state"),
            }
            threads.append(thread)
        return threads

    def _repo_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        """Strictly fetch the bounded chronological issue-comment journal."""
        owner, name = (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        return github_api._fetch_issue_comments_paginated(
            issue_number,
            owner=owner,
            name=name,
            call=gh_call,
        )

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return structured issue comments in GitHub creation order."""
        comments = self._repo_issue_comments(issue_number)
        return [
            IssueComment(
                body=str(comment.get("body", "")),
                author_login=str(
                    (comment.get("user") or comment.get("author") or {}).get("login", "")
                ),
                author_association=str(
                    comment.get("author_association") or comment.get("authorAssociation") or ""
                ),
                created_at=str(comment.get("created_at") or comment.get("createdAt") or ""),
                updated_at=str(comment.get("updated_at") or comment.get("updatedAt") or ""),
                viewer_did_author=self._comment_owned_by_viewer(comment),
                database_id=(
                    int(comment["databaseId"]) if comment.get("databaseId") is not None else None
                ),
                url=str(comment.get("html_url") or comment.get("url") or ""),
            )
            for comment in comments
        ]

    def ensure_blocked_audit(self, issue_number: int) -> None:
        """Repair an interrupted BLOCKED explanation without touching its label."""
        body = blocked_audit_recovery_body(self.issue_comments(issue_number))
        if body is None:
            return
        self.upsert_issue_comment(
            issue_number,
            PLAN_REVIEW_CANONICAL_MARKER,
            body,
            legacy_marker=PLAN_REVIEW_PREFIX,
        )

    def _repo_review_thread_receipts_for_review(
        self,
        pr_number: int,
        review_id: str,
        expected_comments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return immutable sole-comment receipts from one just-created review.

        ``review_id`` is the REST review POST response's ``node_id`` field —
        the GraphQL global node id of the same ``PullRequestReview`` object
        returned in the sole first comment's ``pullRequestReview.id``. A
        receipt is accepted only when that thread still has exactly one
        complete comment whose body/path/line/side matches one requested
        comment. This intentionally fails closed if any reply arrives between
        POST and this first receipt readback: author login alone is never
        evidence that the process authored a reply.
        """
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$after:String){"
            "  repository(owner:$owner,name:$name){"
            "    pullRequest(number:$number){"
            "      reviewThreads(first:100,after:$after){"
            "        pageInfo{ hasNextPage endCursor }"
            "        nodes{ id isResolved path line side:diffSide "
            "comments(first:2){ pageInfo{ hasNextPage } "
            "nodes{ id body author{ login } pullRequestReview{ id } } } }"
            "      }"
            "    }"
            "  }"
            "}"
        )
        expected = [
            (
                str(comment.get("path") or ""),
                comment.get("line"),
                str(comment.get("side") or "RIGHT"),
                str(comment.get("body") or ""),
            )
            for comment in expected_comments
        ]
        unmatched = list(expected)
        receipts: list[dict[str, Any]] = []
        after: str | None = None
        try:
            while True:
                fields: dict[str, int | str] = {"number": int(pr_number)}
                if after is not None:
                    fields["after"] = after
                data = self._graphql(query, **fields)
                review_threads = (
                    data.get("data", {})
                    .get("repository", {})
                    .get("pullRequest", {})
                    .get("reviewThreads", {})
                )
                for node in review_threads.get("nodes", []):
                    if node.get("isResolved"):
                        continue
                    comment_connection = node.get("comments", {})
                    comments = comment_connection.get("nodes", [])
                    if (
                        comment_connection.get("pageInfo", {}).get("hasNextPage")
                        or len(comments) != 1
                    ):
                        continue
                    first_comment = comments[0]
                    review = first_comment.get("pullRequestReview") or {}
                    if review.get("id") != review_id:
                        continue
                    thread_id = node.get("id")
                    author_node = first_comment.get("author")
                    author = author_node.get("login") if isinstance(author_node, dict) else ""
                    body = first_comment.get("body")
                    key = (
                        str(node.get("path") or ""),
                        node.get("line"),
                        str(node.get("side") or "RIGHT"),
                        str(body or ""),
                    )
                    comment_id = first_comment.get("id")
                    if (
                        not isinstance(thread_id, str)
                        or not thread_id
                        or not isinstance(comment_id, str)
                        or not comment_id
                        or not isinstance(author, str)
                        or not author
                        or not isinstance(body, str)
                        or key not in unmatched
                    ):
                        continue
                    unmatched.remove(key)
                    receipts.append(
                        {
                            "id": thread_id,
                            "path": key[0],
                            "line": key[1],
                            "side": key[2],
                            "body": body,
                            "author": author,
                            "authors": [author],
                            "comments": [
                                {
                                    "id": comment_id,
                                    "author": author,
                                    "body": body,
                                    "review_id": review_id,
                                }
                            ],
                            "review_id": review_id,
                        }
                    )
                page_info = review_threads.get("pageInfo", {})
                if not page_info.get("hasNextPage"):
                    break
                next_cursor = page_info.get("endCursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == after:
                    raise RuntimeError("could not fetch all PR review threads")
                after = next_cursor
        except (subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
            logger.warning("Could not fetch review receipts for PR #%s: %s", pr_number, exc)
            return []
        if unmatched or len(receipts) != len(expected):
            return []
        return receipts

    def _skip(self, what: str) -> bool:
        """Return True (and log) when dry-run should skip a mutation."""
        if self.dry_run:
            logger.info("[dry-run] would %s", what)
            return True
        return False

    @staticmethod
    def _pr_is_current_open_head(state: dict[str, Any] | None, expected_head_sha: str) -> bool:
        """Return whether a fresh PR state is open, unarmed, and on the reviewed head."""
        return bool(
            isinstance(expected_head_sha, str)
            and re.fullmatch(r"[0-9a-f]{40}", expected_head_sha)
            and isinstance(state, dict)
            and str(state.get("state") or "").upper() == "OPEN"
            and state.get("autoMergeRequest") is None
            and str(state.get("headRefOid") or "") == expected_head_sha
        )

    @staticmethod
    def _thread_comment_snapshot(thread: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
        """Return the complete immutable comment snapshot for one live thread.

        Thread line positions can move when the implementation pushes a fix, so
        they are deliberately not part of this concurrency guard.  Every
        existing comment's opaque id and body are instead preserved;
        an external reply or edit makes the snapshot differ and prevents a
        coordinator mutation.
        """
        comments = thread.get("comments")
        if not isinstance(comments, list) or not comments:
            return None
        snapshot: list[tuple[str, str]] = []
        seen_comment_ids: set[str] = set()
        for comment in comments:
            if not isinstance(comment, dict):
                return None
            comment_id = comment.get("id")
            author = comment.get("author")
            body = comment.get("body")
            if not (
                isinstance(comment_id, str)
                and comment_id.strip()
                and isinstance(author, str)
                and isinstance(body, str)
            ):
                return None
            if comment_id in seen_comment_ids:
                return None
            seen_comment_ids.add(comment_id)
            snapshot.append((comment_id, body))
        return tuple(snapshot)

    @classmethod
    def _same_thread_snapshot(cls, receipt: dict[str, Any], live: dict[str, Any]) -> bool:
        """Return whether a live thread is unchanged since a host snapshot."""
        return bool(
            isinstance(receipt.get("id"), str)
            and receipt.get("id") == live.get("id")
            and cls._thread_comment_snapshot(receipt) == cls._thread_comment_snapshot(live)
        )

    @classmethod
    def _same_snapshot_with_reply(
        cls,
        receipt: dict[str, Any],
        live: dict[str, Any],
        reply_body: str,
        reply_comment_id: str,
    ) -> bool:
        """Return whether the only live change is this exact coordinator reply."""
        before = cls._thread_comment_snapshot(receipt)
        after = cls._thread_comment_snapshot(live)
        return bool(
            before is not None
            and after is not None
            and len(after) == len(before) + 1
            and after[:-1] == before
            and after[-1][0] == reply_comment_id
            and after[-1][1] == reply_body
            and receipt.get("id") == live.get("id")
        )

    @classmethod
    def _host_reply_receipt(
        cls,
        receipt: dict[str, Any],
        live: dict[str, Any],
        reply_body: str,
        expected_comment_id: str | None = None,
        expected_review_id: str | None = None,
    ) -> str | None:
        """Return a proven coordinator reply appended to one exact snapshot.

        ``expected_comment_id`` is unavailable when GitHub accepted a mutation
        but the response body was malformed.  In that ambiguous case the
        complete post-mutation read still proves the exact host-owned body and
        one-comment extension before recovering a receipt.
        """
        after = cls._thread_comment_snapshot(live)
        comments = live.get("comments")
        if (
            after is None
            or not after
            or not isinstance(comments, list)
            or not comments
            or not isinstance(comments[-1], dict)
            or comments[-1].get("viewer_did_author") is not True
        ):
            return None
        comment_id = after[-1][0]
        if expected_comment_id is not None and comment_id != expected_comment_id:
            return None
        if (
            expected_review_id is not None
            and cls._final_comment_review_id(live) != expected_review_id
        ):
            return None
        return (
            comment_id
            if cls._same_snapshot_with_reply(receipt, live, reply_body, comment_id)
            else None
        )

    @staticmethod
    def _safe_thread_reply(value: object) -> str | None:
        """Return a bounded non-empty agent/reviewer reply or ``None``."""
        if not isinstance(value, str):
            return None
        reply = value.strip()
        return reply if 0 < len(reply) <= 4_000 else None

    def _implementation_thread_reply_body(
        self, pr_number: int, head_sha: str, thread_id: str, reply: str
    ) -> str:
        """Bind an implementation reply to one exact thread and pushed head."""
        seed = ":".join([self._repo_slug or self.org, str(pr_number), thread_id, head_sha, reply])
        marker = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return f"{reply}\n\n<!-- hephaestus-implementation-reply:{marker} -->"

    def _implementation_reply_review_body(
        self, pr_number: int, head_sha: str, replies: dict[str, str]
    ) -> str:
        """Return the durable summary marker for one implementation reply batch."""
        seed = ":".join(
            [
                self._repo_slug or self.org,
                str(pr_number),
                head_sha,
                *(f"{thread_id}:{reply}" for thread_id, reply in sorted(replies.items())),
            ]
        )
        marker = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return (
            f"Implementation responses for {len(replies)} review thread(s).\n\n"
            f"<!-- hephaestus-implementation-review:{marker} -->"
        )

    @staticmethod
    def _is_implementation_reply_review_body(body: object) -> bool:
        """Return whether ``body`` is a coordinator-owned batch marker."""
        return (
            isinstance(body, str)
            and _IMPLEMENTATION_REPLY_REVIEW_BODY_RE.fullmatch(body) is not None
        )

    def _implementation_reply_review_inventory(  # noqa: C901 - paginated GraphQL proof is fail-closed
        self, pr_number: int, expected_head_sha: str, review_body: str
    ) -> tuple[str, tuple[str, str] | None, tuple[str, ...], bool] | None:
        """Return the exact batch review plus stale and conflicting pending drafts.

        Only a viewer-owned pending review carrying the opaque implementation
        marker and an *older* commit is eligible for automatic discard.  A
        manual or malformed pending review is a conflict: leave it untouched
        and fail closed rather than taking ownership of another actor's work.
        """
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$after:String){"
            "repository(owner:$owner,name:$name){pullRequest(number:$number){"
            "id state headRefOid autoMergeRequest{enabledAt} reviews(first:100,after:$after){"
            "pageInfo{hasNextPage endCursor} nodes{id state body viewerDidAuthor commit{oid}}}}}}"
        )
        expected_pr_id: str | None = None
        pending_matches: list[tuple[str, str]] = []
        commented_matches: list[tuple[str, str]] = []
        stale_pending_ids: list[str] = []
        has_pending_conflict = False
        seen_cursors: set[str] = set()
        after: str | None = None
        while True:
            fields: dict[str, int | str] = {"number": pr_number}
            if after is not None:
                fields["after"] = after
            data = self._graphql(query, **fields)
            data_node = data.get("data") if isinstance(data, dict) else None
            repository = data_node.get("repository") if isinstance(data_node, dict) else None
            pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
            if not isinstance(pull_request, dict):
                return None
            pr_id = pull_request.get("id")
            if (
                not isinstance(pr_id, str)
                or not pr_id
                or not self._pr_is_current_open_head(pull_request, expected_head_sha)
            ):
                return None
            if expected_pr_id is None:
                expected_pr_id = pr_id
            elif expected_pr_id != pr_id:
                return None
            reviews = pull_request.get("reviews")
            if not isinstance(reviews, dict) or not isinstance(reviews.get("nodes"), list):
                return None
            for review in reviews["nodes"]:
                if not isinstance(review, dict):
                    return None
                if review.get("viewerDidAuthor") is not True:
                    continue
                review_id = review.get("id")
                state = review.get("state")
                body = review.get("body")
                commit = review.get("commit")
                commit_oid = commit.get("oid") if isinstance(commit, dict) else None
                if not isinstance(review_id, str) or not review_id or not isinstance(state, str):
                    return None
                if state == "PENDING":
                    if (
                        body == review_body
                        and isinstance(commit_oid, str)
                        and commit_oid == expected_head_sha
                    ):
                        pending_matches.append((review_id, state))
                    elif (
                        body == review_body
                        and isinstance(commit_oid, str)
                        and re.fullmatch(r"[0-9a-f]{40}", commit_oid) is not None
                        and commit_oid != expected_head_sha
                    ):
                        stale_pending_ids.append(review_id)
                    else:
                        has_pending_conflict = True
                    continue
                if body != review_body:
                    continue
                if (
                    state != "COMMENTED"
                    or not isinstance(commit_oid, str)
                    or commit_oid != expected_head_sha
                ):
                    return None
                commented_matches.append((review_id, state))
            page_info = reviews.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(
                page_info.get("hasNextPage"), bool
            ):
                return None
            if not page_info["hasNextPage"]:
                break
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                return None
            seen_cursors.add(next_cursor)
            after = next_cursor
        if expected_pr_id is None or len(commented_matches) > 1:
            return None
        if commented_matches:
            # A completed batch remains durable proof even when a losing
            # cross-checkout process left a current-head pending draft. The
            # adapter never deletes that draft, but it must not force the
            # already-submitted review back through a retry loop.
            return (
                expected_pr_id,
                commented_matches[0],
                tuple(sorted(set(stale_pending_ids))),
                has_pending_conflict,
            )
        if len(pending_matches) > 1:
            raise DuplicateImplementationReplyDraftsError(
                tuple(sorted(review_id for review_id, _ in pending_matches))
            )
        return (
            expected_pr_id,
            pending_matches[0] if pending_matches else None,
            tuple(sorted(set(stale_pending_ids))),
            has_pending_conflict,
        )

    def _find_implementation_reply_review(
        self, pr_number: int, expected_head_sha: str, review_body: str
    ) -> tuple[str, tuple[str, str] | None] | None:
        """Find the current actor's durable batch review for an exact PR head.

        The pending review is the restart-safe receipt for an interrupted
        implementation-reply pass.  A matching submitted review proves that a
        prior call completed after its transport response was lost.
        """
        inventory = self._implementation_reply_review_inventory(
            pr_number, expected_head_sha, review_body
        )
        if inventory is None:
            return None
        pr_id, review, stale_pending_ids, has_pending_conflict = inventory
        if stale_pending_ids or (
            has_pending_conflict and (review is None or review[1] != "COMMENTED")
        ):
            return None
        return pr_id, review

    def _create_implementation_reply_review(
        self, pr_id: str, expected_head_sha: str, review_body: str
    ) -> str | None:
        """Create one pending review that will own every implementation reply."""
        query = (
            "mutation($prId:ID!,$commitOid:GitObjectID!,$body:String!,$clientMutationId:String!){"
            "addPullRequestReview(input:{pullRequestId:$prId,commitOID:$commitOid,body:$body,"
            "clientMutationId:$clientMutationId}){pullRequestReview{id state body commit{oid}}}}"
        )
        data = self._graphql(
            query,
            prId=pr_id,
            commitOid=expected_head_sha,
            body=review_body,
            clientMutationId=hashlib.sha256(review_body.encode("utf-8")).hexdigest(),
        )
        response_data = data.get("data") if isinstance(data, dict) else None
        mutation = (
            response_data.get("addPullRequestReview") if isinstance(response_data, dict) else None
        )
        review = mutation.get("pullRequestReview") if isinstance(mutation, dict) else None
        commit = review.get("commit") if isinstance(review, dict) else None
        if (
            not isinstance(review, dict)
            or not isinstance(review.get("id"), str)
            or not review["id"]
            or review.get("state") != "PENDING"
            or review.get("body") != review_body
            or not isinstance(commit, dict)
            or commit.get("oid") != expected_head_sha
        ):
            return None
        return str(review["id"])

    def _submit_implementation_reply_review(
        self, pr_id: str, review_id: str, review_body: str
    ) -> bool:
        """Submit the fully proven reply batch as one COMMENTED review."""
        query = (
            "mutation($prId:ID!,$reviewId:ID!,$body:String!,$clientMutationId:String!){"
            "submitPullRequestReview(input:{pullRequestId:$prId,pullRequestReviewId:$reviewId,"
            "event:COMMENT,body:$body,clientMutationId:$clientMutationId})"
            "{pullRequestReview{id state body}}}"
        )
        data = self._graphql(
            query,
            prId=pr_id,
            reviewId=review_id,
            body=review_body,
            clientMutationId=hashlib.sha256(
                f"{review_id}:{review_body}:submit".encode()
            ).hexdigest(),
        )
        response_data = data.get("data") if isinstance(data, dict) else None
        mutation = (
            response_data.get("submitPullRequestReview")
            if isinstance(response_data, dict)
            else None
        )
        review = mutation.get("pullRequestReview") if isinstance(mutation, dict) else None
        return bool(
            isinstance(review, dict)
            and review.get("id") == review_id
            and review.get("state") == "COMMENTED"
            and review.get("body") == review_body
        )

    def _delete_implementation_reply_review(self, review_id: str) -> bool:
        """Delete one proven coordinator-owned pending review draft."""
        query = (
            "mutation($reviewId:ID!,$clientMutationId:String!){"
            "deletePullRequestReview(input:{pullRequestReviewId:$reviewId,"
            "clientMutationId:$clientMutationId}){pullRequestReview{id}}}"
        )
        data = self._graphql(
            query,
            reviewId=review_id,
            clientMutationId=hashlib.sha256(
                f"{review_id}:discard-implementation-replies".encode()
            ).hexdigest(),
        )
        response_data = data.get("data") if isinstance(data, dict) else None
        mutation = (
            response_data.get("deletePullRequestReview")
            if isinstance(response_data, dict)
            else None
        )
        review = mutation.get("pullRequestReview") if isinstance(mutation, dict) else None
        return bool(isinstance(review, dict) and review.get("id") == review_id)

    def _reconcile_implementation_reply_reviews(
        self, pr_number: int, expected_head_sha: str, review_body: str
    ) -> tuple[str, tuple[str, str] | None, bool] | None:
        """Discard only proven stale drafts and return the current batch state.

        The review inventory is reread before every deletion, so a head move
        or a new manual pending review turns into a retry instead of deleting
        a draft whose identity is no longer proved.  A PENDING review for the
        current head but another body is intentionally left alone.
        """
        while True:
            inventory = self._implementation_reply_review_inventory(
                pr_number, expected_head_sha, review_body
            )
            if inventory is None:
                return None
            pr_id, review, stale_pending_ids, has_pending_conflict = inventory
            if not stale_pending_ids:
                return pr_id, review, has_pending_conflict
            stale_review_id = stale_pending_ids[0]
            if not self._delete_implementation_reply_review(stale_review_id):
                # GitHub may have applied the delete before losing its response.
                # The next inventory read is the only safe recovery proof.
                recovered = self._implementation_reply_review_inventory(
                    pr_number, expected_head_sha, review_body
                )
                if recovered is None or stale_review_id in recovered[2]:
                    return None

    def _discard_current_implementation_reply_review(
        self,
        pr_number: int,
        expected_head_sha: str,
        review_body: str,
        pr_id: str,
        review_id: str,
    ) -> bool:
        """Abort a current batch which cannot atomically cover every thread."""
        current = self._reconcile_implementation_reply_reviews(
            pr_number, expected_head_sha, review_body
        )
        if current is None:
            return False
        current_pr_id, review, _has_pending_conflict = current
        if current_pr_id != pr_id or review != (review_id, "PENDING"):
            return False
        if self._delete_implementation_reply_review(review_id):
            return True
        recovered = self._reconcile_implementation_reply_reviews(
            pr_number, expected_head_sha, review_body
        )
        return bool(recovered is not None and recovered[0] == pr_id and recovered[1] is None)

    @staticmethod
    def _final_comment_review_id(thread: dict[str, Any]) -> str | None:
        """Return the review owning a complete thread's final comment."""
        comments = thread.get("comments")
        if not isinstance(comments, list) or not comments or not isinstance(comments[-1], dict):
            return None
        review_id = comments[-1].get("review_id")
        return review_id if isinstance(review_id, str) and review_id else None

    def _validated_implementation_reply(
        self, pr_number: int, reviewed_head_sha: str, thread: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Return a final host-owned, exact-head implementation reply receipt."""
        thread_id = thread.get("id")
        snapshot = self._thread_comment_snapshot(thread)
        comments = thread.get("comments")
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or snapshot is None
            or not isinstance(comments, list)
            or not comments
        ):
            return None
        reply_id, reply_body = snapshot[-1]
        final_comment = comments[-1]
        if (
            not isinstance(final_comment, dict)
            or not final_comment.get("viewer_did_author")
            or not isinstance(final_comment.get("review_id"), str)
            or not final_comment["review_id"]
            or final_comment.get("review_state") != "COMMENTED"
            or not self._is_implementation_reply_review_body(final_comment.get("review_body"))
            or final_comment.get("review_commit_sha") != reviewed_head_sha
        ):
            return None
        marker_match = re.fullmatch(
            r"(?s)(.*)\n\n<!-- hephaestus-implementation-reply:[0-9a-f]{24} -->",
            reply_body,
        )
        if marker_match is None:
            return None
        reply = self._safe_thread_reply(marker_match.group(1))
        if reply is None:
            return None
        expected_body = self._implementation_thread_reply_body(
            pr_number, reviewed_head_sha, thread_id, reply
        )
        if reply_body != expected_body:
            return None
        return reply_id, reply_body

    def _validated_implementation_reply_batch(
        self,
        pr_number: int,
        reviewed_head_sha: str,
        threads: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str, str]]:
        """Return one complete, submitted implementation reply batch.

        A per-thread reply marker only establishes that one response is bound
        to a thread and head.  It cannot establish that an implementation pass
        used a single review.  Before the reviewer may act, every eligible
        final reply must therefore belong to one submitted review whose
        deterministic summary covers the complete live group.
        """
        candidates: list[tuple[dict[str, Any], str, str, str, str]] = []
        seen_thread_ids: set[str] = set()
        for thread in threads:
            if not isinstance(thread, dict):
                return []
            thread_id = thread.get("id")
            comments = thread.get("comments")
            implementation_reply = self._validated_implementation_reply(
                pr_number, reviewed_head_sha, thread
            )
            if implementation_reply is None:
                continue
            if (
                not isinstance(thread_id, str)
                or not thread_id
                or thread_id in seen_thread_ids
                or not isinstance(comments, list)
                or not comments
                or not isinstance(comments[-1], dict)
            ):
                return []
            review_id = comments[-1].get("review_id")
            review_body = comments[-1].get("review_body")
            if not isinstance(review_id, str) or not review_id or not isinstance(review_body, str):
                return []
            seen_thread_ids.add(thread_id)
            reply_id, reply_body = implementation_reply
            candidates.append((thread, reply_id, reply_body, review_id, review_body))
        if not candidates:
            return []
        review_ids = {candidate[3] for candidate in candidates}
        review_bodies = {candidate[4] for candidate in candidates}
        if len(review_ids) != 1 or len(review_bodies) != 1:
            return []
        expected_body = self._implementation_reply_review_body(
            pr_number,
            reviewed_head_sha,
            {str(candidate[0]["id"]): candidate[2] for candidate in candidates},
        )
        if review_bodies.pop() != expected_body:
            return []
        return [(thread, reply_id, reply_body) for thread, reply_id, reply_body, _, _ in candidates]

    def reviewer_validation_receipts(
        self,
        pr_number: int,
        *,
        reviewed_head_sha: str,
        threads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Derive current implementation receipts from complete live threads.

        The marker is recomputed from the exact reply body, thread id, PR, and
        reviewed SHA.  This makes the GitHub snapshot, rather than ephemeral
        work-item memory, the authority for the reviewer handoff after a
        coordinator restart.
        """
        if not self._pr_is_current_open_head(self.gh_pr_state(pr_number), reviewed_head_sha):
            raise RuntimeError("reviewed PR head is no longer current")
        receipts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for thread in threads:
            if not isinstance(thread, dict):
                raise RuntimeError("malformed live review thread")
            thread_id = thread.get("id")
            if (
                not isinstance(thread_id, str)
                or not thread_id
                or thread_id in seen
                or self._thread_comment_snapshot(thread) is None
            ):
                raise RuntimeError("malformed live review-thread snapshot")
            seen.add(thread_id)
        for thread, reply_id, reply_body in self._validated_implementation_reply_batch(
            pr_number, reviewed_head_sha, threads
        ):
            receipts.append(
                {
                    **thread,
                    "implementation_reply_id": reply_id,
                    "implementation_reply_body": reply_body,
                    "implementation_head_sha": reviewed_head_sha,
                }
            )
        return receipts

    def _reviewer_thread_feedback_body(
        self, pr_number: int, head_sha: str, thread_id: str, feedback: str
    ) -> str:
        """Bind a reviewer rejection explanation to one exact thread and head."""
        seed = ":".join(
            [self._repo_slug or self.org, str(pr_number), thread_id, head_sha, feedback]
        )
        marker = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return (
            f"Reviewer validation found this still unresolved: {feedback}\n\n"
            f"<!-- hephaestus-reviewer-validation:{marker} -->"
        )

    def _add_thread_reply(
        self, thread_id: str, body: str, *, review_id: str | None = None
    ) -> str | None:
        """Post one coordinator-owned reply and return its opaque comment ID.

        Implementation replies supply a pending review id so GitHub associates
        every reply from one implementation pass with the same review.  Fresh
        reviewer feedback remains a standalone thread reply.
        """
        if review_id is None:
            query = (
                "mutation($threadId:ID!,$body:String!,$clientMutationId:String!){"
                "addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,body:$body,"
                "clientMutationId:$clientMutationId}){comment{id}}}"
            )
        else:
            query = (
                "mutation($threadId:ID!,$reviewId:ID!,$body:String!,$clientMutationId:String!){"
                "addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,"
                "pullRequestReviewId:$reviewId,body:$body,clientMutationId:$clientMutationId})"
                "{comment{id}}}"
            )
        mutation_fields: dict[str, str] = {
            "threadId": thread_id,
            "body": body,
            "clientMutationId": hashlib.sha256(
                f"{review_id or ''}:{thread_id}:{body}".encode()
            ).hexdigest(),
        }
        if review_id is not None:
            mutation_fields["reviewId"] = review_id
        data = self._graphql(
            query,
            **mutation_fields,
        )
        response_data = data.get("data") if isinstance(data, dict) else None
        mutation = (
            response_data.get("addPullRequestReviewThreadReply")
            if isinstance(response_data, dict)
            else None
        )
        comment = mutation.get("comment") if isinstance(mutation, dict) else None
        if not isinstance(comment, dict) or not isinstance(comment.get("id"), str):
            return None
        return str(comment["id"])

    def _review_thread_snapshot(  # noqa: C901 - GraphQL response validation is fail-closed
        self, pr_number: int, thread_id: str
    ) -> dict[str, Any] | None:
        """Return one complete thread and PR-state snapshot, including a resolved thread.

        An unresolved-thread list cannot prove the contents of a thread after
        ``resolveReviewThread`` hides it.  This node-scoped read is therefore
        the post-mutation proof that no comment raced the reviewed receipt and
        that the resolved node still belongs to this exact pull request.  The
        PR's open/unarmed/head fields are selected with every comment page, so
        reconciliation never combines a complete conversation read with a
        later, racy PR-state read.  Conversations spanning multiple pages are
        reread to a matching fixed point before they become a mutation proof.
        """
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$threadId:ID!,$after:String){"
            "repository(owner:$owner,name:$name){pullRequest(number:$number){"
            "id state headRefOid autoMergeRequest{enabledAt}}}"
            "node(id:$threadId){... on PullRequestReviewThread{"
            "id isResolved path line side:diffSide pullRequest{"
            "id number repository{name owner{login}}}"
            "comments(first:100,after:$after){pageInfo{hasNextPage endCursor}"
            "nodes{id body viewerDidAuthor author{login __typename} "
            "pullRequestReview{id state body commit{oid}}}}}}}"
        )

        def read_once() -> tuple[dict[str, Any], bool] | None:  # noqa: C901
            comments: list[dict[str, Any]] = []
            seen_comment_ids: set[str] = set()
            seen_cursors: set[str] = set()
            after: str | None = None
            page_count = 0
            expected_pr_id: str | None = None
            expected_pr_state: dict[str, Any] | None = None
            expected_thread_fields: tuple[bool, str, int | None, str | None] | None = None
            while True:
                page_count += 1
                fields: dict[str, int | str] = {
                    "number": int(pr_number),
                    "threadId": thread_id,
                }
                if after is not None:
                    fields["after"] = after
                data = self._graphql(query, **fields)
                data_node = data.get("data") if isinstance(data, dict) else None
                repository = data_node.get("repository") if isinstance(data_node, dict) else None
                requested_pr = (
                    repository.get("pullRequest") if isinstance(repository, dict) else None
                )
                if not isinstance(requested_pr, dict):
                    return None
                pr_id = requested_pr.get("id")
                auto_merge_request = requested_pr.get("autoMergeRequest")
                if (
                    not isinstance(pr_id, str)
                    or not pr_id
                    or not isinstance(requested_pr.get("state"), str)
                    or not isinstance(requested_pr.get("headRefOid"), str)
                    or "autoMergeRequest" not in requested_pr
                    or (auto_merge_request is not None and not isinstance(auto_merge_request, dict))
                ):
                    return None
                pr_state = {
                    "state": requested_pr["state"],
                    "headRefOid": requested_pr["headRefOid"],
                    "autoMergeRequest": auto_merge_request,
                }
                if expected_pr_id is None:
                    expected_pr_id = pr_id
                    expected_pr_state = pr_state
                elif expected_pr_id != pr_id or expected_pr_state != pr_state:
                    return None
                node = data_node.get("node") if isinstance(data_node, dict) else None
                if not isinstance(node, dict) or node.get("id") != thread_id:
                    return None
                pull_request = node.get("pullRequest")
                thread_pr_number = (
                    pull_request.get("number") if isinstance(pull_request, dict) else None
                )
                thread_repository = (
                    pull_request.get("repository") if isinstance(pull_request, dict) else None
                )
                owner = (
                    thread_repository.get("owner") if isinstance(thread_repository, dict) else None
                )
                if (
                    not isinstance(pull_request, dict)
                    or pull_request.get("id") != expected_pr_id
                    or isinstance(thread_pr_number, bool)
                    or not isinstance(thread_pr_number, int)
                    or thread_pr_number != pr_number
                    or not isinstance(thread_repository, dict)
                    or thread_repository.get("name") != self.repo
                    or not isinstance(owner, dict)
                    or owner.get("login") != self.org
                    or not isinstance(node.get("isResolved"), bool)
                ):
                    return None
                path = node.get("path")
                line = node.get("line")
                side = node.get("side")
                if (
                    not isinstance(path, str)
                    or (line is not None and (isinstance(line, bool) or not isinstance(line, int)))
                    or (side is not None and not isinstance(side, str))
                ):
                    return None
                thread_fields = (node["isResolved"], path, line, side)
                if expected_thread_fields is None:
                    expected_thread_fields = thread_fields
                elif expected_thread_fields != thread_fields:
                    return None
                comment_connection = node.get("comments")
                if not isinstance(comment_connection, dict):
                    return None
                comment_nodes = comment_connection.get("nodes")
                if not isinstance(comment_nodes, list):
                    return None
                for comment in comment_nodes:
                    if not isinstance(comment, dict):
                        return None
                    if "author" not in comment:
                        return None
                    author_node = comment.get("author")
                    if author_node is None:
                        author = ""
                        author_type = ""
                    elif isinstance(author_node, dict):
                        author_login = author_node.get("login")
                        actor_type = author_node.get("__typename")
                        if not isinstance(author_login, str) or not isinstance(actor_type, str):
                            return None
                        author = author_login
                        author_type = actor_type
                    else:
                        return None
                    review = comment.get("pullRequestReview")
                    if review is not None and not isinstance(review, dict):
                        return None
                    commit = review.get("commit") if isinstance(review, dict) else None
                    if commit is not None and not isinstance(commit, dict):
                        return None
                    comment_id = comment.get("id")
                    body = comment.get("body")
                    review_id = review.get("id") if isinstance(review, dict) else ""
                    review_state = review.get("state") if isinstance(review, dict) else ""
                    review_body = review.get("body") if isinstance(review, dict) else ""
                    review_commit_sha = commit.get("oid") if isinstance(commit, dict) else ""
                    if (
                        not isinstance(comment_id, str)
                        or not comment_id
                        or not isinstance(body, str)
                        or not isinstance(author, str)
                        or not isinstance(author_type, str)
                        or not isinstance(comment.get("viewerDidAuthor"), bool)
                        or not isinstance(review_id, str)
                        or not isinstance(review_state, str)
                        or not isinstance(review_body, str)
                        or not isinstance(review_commit_sha, str)
                    ):
                        return None
                    if comment_id in seen_comment_ids:
                        return None
                    seen_comment_ids.add(comment_id)
                    comments.append(
                        {
                            "id": comment_id,
                            "body": body,
                            "author": author,
                            "author_type": author_type,
                            "viewer_did_author": comment["viewerDidAuthor"],
                            "review_id": review_id,
                            "review_state": review_state,
                            "review_body": review_body,
                            "review_commit_sha": review_commit_sha,
                        }
                    )
                page_info = comment_connection.get("pageInfo")
                if not isinstance(page_info, dict) or not isinstance(
                    page_info.get("hasNextPage"), bool
                ):
                    return None
                if not page_info["hasNextPage"]:
                    if expected_thread_fields is None or expected_pr_state is None:
                        return None
                    return (
                        {
                            "id": thread_id,
                            "isResolved": expected_thread_fields[0],
                            "path": expected_thread_fields[1],
                            "line": expected_thread_fields[2],
                            "side": expected_thread_fields[3],
                            "comments": comments,
                            "pr_state": expected_pr_state,
                        },
                        page_count > 1,
                    )
                next_cursor = page_info.get("endCursor")
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor in seen_cursors
                ):
                    raise RuntimeError(
                        f"could not fetch all comments for PR review thread {thread_id}"
                    )
                seen_cursors.add(next_cursor)
                after = next_cursor

        first = read_once()
        if first is None:
            return None
        snapshot, was_paginated = first
        if not was_paginated:
            return snapshot
        # A multi-page connection cannot be one atomic read.  Stabilize it
        # with a complete second traversal; any comment or PR-state change
        # makes the proof unusable instead of resolving a raced discussion.
        second = read_once()
        if second is None or snapshot != second[0]:
            return None
        return second[0]

    def _implementation_reply_lock_path(self, pr_number: int) -> Path:
        """Return the cross-process lock for one repository PR reply batch."""
        repo_key = hashlib.sha256((self._repo_slug or self.org).encode("utf-8")).hexdigest()[:16]
        return (
            ensure_state_dir(self._repo_root)
            / "locks"
            / (f"implementation-replies-{repo_key}-{pr_number}.lock")
        )

    def post_implementation_thread_replies(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
    ) -> ImplementationThreadReplyResult:
        """Serialize one repository-local implementation reply batch per PR.

        GitHub's client mutation id is a tracing field rather than a
        compare-and-swap primitive.  Cooperating loop processes therefore
        hold one PR-scoped lock across discovery, draft creation, replies, and
        submission.  The adapter fails closed on platforms without an
        exclusive lock instead of risking duplicate draft reviews.
        """
        candidate_ids = tuple(sorted(str(thread_id) for thread_id in replies))
        if self._skip(
            f"post {len(candidate_ids)} implementation review-thread replies on PR #{pr_number}"
        ):
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
        try:
            with file_lock(self._implementation_reply_lock_path(pr_number), require_exclusive=True):
                return self._post_implementation_thread_replies_locked(
                    pr_number,
                    expected_head_sha=expected_head_sha,
                    threads=threads,
                    replies=replies,
                )
        except (LockUnavailableError, OSError) as error:
            logger.warning("Implementation reply batch lock failed on PR #%s: %s", pr_number, error)
            return ImplementationThreadReplyResult(
                retryable_thread_ids=candidate_ids,
                retryable=True,
            )

    def discard_stale_implementation_thread_reply_batch(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        current_head_sha: str,
        replies: dict[str, str],
    ) -> bool:
        """Discard verified old-head drafts after a handoff becomes stale.

        The review stage calls this only after it has proved the PR remains
        open and unarmed at ``current_head_sha``.  It never deletes a current
        manual pending review; those are returned as a conflict by the
        inventory and left for an operator.
        """
        if self._skip(f"discard stale implementation reply draft on PR #{pr_number}"):
            return True
        if (
            not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha)
            or not re.fullmatch(r"[0-9a-f]{40}", current_head_sha)
            or expected_head_sha == current_head_sha
            or not replies
        ):
            return False
        bodies: dict[str, str] = {}
        for thread_id, reply in replies.items():
            if not isinstance(thread_id, str) or not thread_id:
                return False
            safe_reply = self._safe_thread_reply(reply)
            if safe_reply is None:
                return False
            bodies[thread_id] = self._implementation_thread_reply_body(
                pr_number, expected_head_sha, thread_id, safe_reply
            )
        review_body = self._implementation_reply_review_body(pr_number, expected_head_sha, bodies)
        try:
            with file_lock(self._implementation_reply_lock_path(pr_number), require_exclusive=True):
                reconciled = self._reconcile_implementation_reply_reviews(
                    pr_number, current_head_sha, review_body
                )
        except (LockUnavailableError, OSError) as error:
            logger.warning(
                "Stale implementation reply draft lock failed on PR #%s: %s", pr_number, error
            )
            return False
        return reconciled is not None

    def _post_implementation_thread_replies_locked(  # noqa: C901
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
    ) -> ImplementationThreadReplyResult:
        """Post implementation-agent replies after a real fix commit reached GitHub.

        Every target must be an ID from the complete host-provided thread
        snapshot. The method never resolves threads: the next reviewer pass
        performs a fresh review and owns that decision.
        """
        candidate_ids = tuple(sorted(str(thread_id) for thread_id in replies))
        if not candidate_ids or not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
        snapshots: dict[str, dict[str, Any]] = {}
        for thread in threads:
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
            thread_id = str(thread["id"])
            if thread_id in snapshots or self._thread_comment_snapshot(thread) is None:
                return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
            snapshots[thread_id] = dict(thread)
        if not set(candidate_ids).issubset(snapshots):
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
        blocked: list[str] = []

        def receipt(live: dict[str, Any], comment_id: str, body: str) -> dict[str, Any]:
            return {
                **live,
                "implementation_reply_id": comment_id,
                "implementation_reply_body": body,
                "implementation_head_sha": expected_head_sha,
            }

        prepared: list[tuple[str, dict[str, Any], str]] = []
        recovered: dict[str, dict[str, Any]] = {}
        reply_bodies: dict[str, str] = {}
        try:
            for thread_id in candidate_ids:
                reply = self._safe_thread_reply(replies.get(thread_id))
                snapshot = snapshots[thread_id]
                if reply is None:
                    blocked.append(thread_id)
                    continue
                body = self._implementation_thread_reply_body(
                    pr_number, expected_head_sha, thread_id, reply
                )
                reply_bodies[thread_id] = body
                live = self._review_thread_snapshot(pr_number, thread_id)
                if (
                    not isinstance(live, dict)
                    or live.get("isResolved") is not False
                    or not self._pr_is_current_open_head(live.get("pr_state"), expected_head_sha)
                ):
                    blocked.append(thread_id)
                    continue
                # A prior transport failure may have applied this exact reply.
                # Recover its host proof before treating the saved snapshot as
                # stale or attempting a duplicate mutation.
                recovered_id = self._host_reply_receipt(snapshot, live, body)
                if recovered_id is not None:
                    recovered[thread_id] = receipt(live, recovered_id, body)
                    continue
                if not self._same_thread_snapshot(snapshot, live):
                    blocked.append(thread_id)
                    continue
                prepared.append((thread_id, snapshot, body))

            review_body = (
                self._implementation_reply_review_body(pr_number, expected_head_sha, reply_bodies)
                if len(reply_bodies) == len(candidate_ids)
                else None
            )
            current_batch = (
                self._reconcile_implementation_reply_reviews(
                    pr_number, expected_head_sha, review_body
                )
                if review_body is not None
                else None
            )
            if review_body is None or current_batch is None:
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )
            pr_id, existing_review, has_pending_conflict = current_batch
            if blocked:
                # A PENDING review is not a receipt.  Abort the exact
                # coordinator draft if any target changed, rather than
                # reporting a hidden partial batch as delivered.
                if (
                    existing_review is not None
                    and existing_review[1] == "PENDING"
                    and not self._discard_current_implementation_reply_review(
                        pr_number,
                        expected_head_sha,
                        review_body,
                        pr_id,
                        existing_review[0],
                    )
                ):
                    return ImplementationThreadReplyResult(
                        retryable_thread_ids=candidate_ids,
                        retryable=True,
                    )
                return ImplementationThreadReplyResult(
                    blocked_thread_ids=tuple(sorted(blocked)),
                )
            if has_pending_conflict and (
                existing_review is None or existing_review[1] != "COMMENTED"
            ):
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )
            if existing_review is None:
                # A reply from an old per-comment review is not a successful
                # batch receipt.  A fresh review pass must establish a
                # coherent current handoff without retrying this stale one.
                if recovered:
                    return ImplementationThreadReplyResult(
                        blocked_thread_ids=candidate_ids,
                    )
                review_id = self._create_implementation_reply_review(
                    pr_id, expected_head_sha, review_body
                )
                if review_id is None:
                    return ImplementationThreadReplyResult(
                        retryable_thread_ids=candidate_ids,
                        retryable=True,
                    )
                review_state = "PENDING"
            else:
                review_id, review_state = existing_review

            recovered_review_ids = {
                self._final_comment_review_id(recovered[thread_id]) for thread_id in recovered
            }
            if recovered_review_ids and recovered_review_ids != {review_id}:
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )
            if review_state == "COMMENTED":
                if not prepared and len(recovered) == len(candidate_ids):
                    return ImplementationThreadReplyResult(
                        replied_thread_ids=candidate_ids,
                        receipts=tuple(recovered[thread_id] for thread_id in candidate_ids),
                    )
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )
            if review_state != "PENDING":
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )

            for thread_id, snapshot, body in prepared:
                comment_id = self._add_thread_reply(thread_id, body, review_id=review_id)
                replied_live = self._review_thread_snapshot(pr_number, thread_id)
                if (
                    not isinstance(replied_live, dict)
                    or replied_live.get("isResolved") is not False
                    or not self._pr_is_current_open_head(
                        replied_live.get("pr_state"), expected_head_sha
                    )
                ):
                    return ImplementationThreadReplyResult(
                        retryable_thread_ids=candidate_ids,
                        retryable=True,
                    )
                proved_comment_id = self._host_reply_receipt(
                    snapshot,
                    replied_live,
                    body,
                    comment_id,
                    expected_review_id=review_id,
                )
                if proved_comment_id is None:
                    return ImplementationThreadReplyResult(
                        retryable_thread_ids=candidate_ids,
                        retryable=True,
                    )
                recovered[thread_id] = receipt(replied_live, proved_comment_id, body)

            confirmed_review = self._find_implementation_reply_review(
                pr_number, expected_head_sha, review_body
            )
            if (
                len(recovered) != len(candidate_ids)
                or confirmed_review != (pr_id, (review_id, "PENDING"))
                or not self._submit_implementation_reply_review(pr_id, review_id, review_body)
            ):
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )
        except DuplicateImplementationReplyDraftsError as error:
            logger.error(
                "Implementation reply batch on PR #%s stopped: duplicate current drafts %s",
                pr_number,
                ", ".join(error.review_ids),
            )
            return ImplementationThreadReplyResult(
                blocked_thread_ids=candidate_ids,
                duplicate_current_draft_ids=error.review_ids,
            )
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
        ) as error:
            logger.warning("Implementation thread replies failed on PR #%s: %s", pr_number, error)
            return ImplementationThreadReplyResult(
                retryable_thread_ids=candidate_ids,
                retryable=True,
            )
        return ImplementationThreadReplyResult(
            replied_thread_ids=candidate_ids,
            receipts=tuple(recovered[thread_id] for thread_id in candidate_ids),
        )

    def reconcile_reviewer_validated_threads(  # noqa: C901
        self,
        pr_number: int,
        *,
        reviewed_head_sha: str,
        receipts: list[dict[str, Any]],
        resolved_thread_ids: set[str],
        feedback: dict[str, str],
    ) -> ReviewerThreadReconciliationResult:
        """Apply the reviewer's fresh per-thread decision, preserving races safely."""
        expected_ids = {str(receipt.get("id") or "") for receipt in receipts}
        candidate_ids = tuple(sorted(expected_ids | set(resolved_thread_ids) | set(feedback)))
        if (
            not expected_ids
            or "" in expected_ids
            or set(resolved_thread_ids) & set(feedback)
            or expected_ids != set(resolved_thread_ids) | set(feedback)
            or not re.fullmatch(r"[0-9a-f]{40}", reviewed_head_sha)
            or self._skip(
                f"reconcile {len(candidate_ids)} reviewer-validated threads on PR #{pr_number}"
            )
        ):
            return ReviewerThreadReconciliationResult(blocked_thread_ids=candidate_ids)
        batch = self._validated_implementation_reply_batch(pr_number, reviewed_head_sha, receipts)
        batch_by_id = {
            str(thread.get("id") or ""): (reply_id, reply_body)
            for thread, reply_id, reply_body in batch
        }
        if set(batch_by_id) != expected_ids or any(
            not isinstance(receipt, dict)
            or batch_by_id.get(str(receipt.get("id") or ""))
            != (
                receipt.get("implementation_reply_id"),
                receipt.get("implementation_reply_body"),
            )
            for receipt in receipts
        ):
            return ReviewerThreadReconciliationResult(blocked_thread_ids=candidate_ids)
        by_id: dict[str, dict[str, Any]] = {}
        for receipt in receipts:
            if not isinstance(receipt, dict):
                return ReviewerThreadReconciliationResult(blocked_thread_ids=candidate_ids)
            thread_id = str(receipt.get("id") or "")
            reply_id = receipt.get("implementation_reply_id")
            reply_body = receipt.get("implementation_reply_body")
            implementation_head_sha = receipt.get("implementation_head_sha")
            snapshot = self._thread_comment_snapshot(receipt)
            validated_reply = self._validated_implementation_reply(
                pr_number, reviewed_head_sha, receipt
            )
            if (
                not thread_id
                or thread_id in by_id
                or not isinstance(reply_id, str)
                or not isinstance(reply_body, str)
                or not isinstance(implementation_head_sha, str)
                or implementation_head_sha != reviewed_head_sha
                or snapshot is None
                or snapshot[-1][0] != reply_id
                or snapshot[-1][1] != reply_body
                or validated_reply != (reply_id, reply_body)
            ):
                return ReviewerThreadReconciliationResult(blocked_thread_ids=candidate_ids)
            by_id[thread_id] = receipt
        resolved: list[str] = []
        replied: list[str] = []
        blocked: list[str] = []
        try:
            for thread_id in candidate_ids:
                receipt = by_id[thread_id]
                reply_id = receipt["implementation_reply_id"]
                reply_body = receipt["implementation_reply_body"]
                live = self._review_thread_snapshot(pr_number, thread_id)
                if (
                    not isinstance(live, dict)
                    or live.get("isResolved") is not False
                    or not self._pr_is_current_open_head(live.get("pr_state"), reviewed_head_sha)
                    or not self._same_thread_snapshot(receipt, live)
                ):
                    blocked.append(thread_id)
                    continue
                # The caller's receipt is untrusted input to this adapter.
                # Reprove viewer ownership and the exact marker from the fresh
                # live comment, not merely the id/author/body snapshot used to
                # detect concurrent conversation changes.
                if self._validated_implementation_reply(pr_number, reviewed_head_sha, live) != (
                    reply_id,
                    reply_body,
                ):
                    blocked.append(thread_id)
                    continue
                if thread_id in feedback:
                    detail = self._safe_thread_reply(feedback[thread_id])
                    if detail is None:
                        blocked.append(thread_id)
                        continue
                    body = self._reviewer_thread_feedback_body(
                        pr_number, reviewed_head_sha, thread_id, detail
                    )
                    comment_id = self._add_thread_reply(thread_id, body)
                    after = self._review_thread_snapshot(pr_number, thread_id)
                    if (
                        not isinstance(after, dict)
                        or after.get("isResolved") is not False
                        or not self._pr_is_current_open_head(
                            after.get("pr_state"), reviewed_head_sha
                        )
                        or self._host_reply_receipt(receipt, after, body, comment_id) is None
                    ):
                        blocked.append(thread_id)
                        continue
                    replied.append(thread_id)
                    continue
                try:
                    resolve_query = (
                        "mutation($threadId:ID!,$clientMutationId:String!){"
                        "resolveReviewThread(input:{threadId:$threadId,clientMutationId:$clientMutationId})"
                        "{thread{id isResolved}}}"
                    )
                    # A transport error can arrive after GitHub has applied
                    # the mutation.  Treat every issued resolve as unsafe
                    # until both the complete resolved snapshot and final
                    # exact-head read prove this receipt remained current.
                    resolve_data = self._graphql(
                        resolve_query,
                        threadId=thread_id,
                        clientMutationId=hashlib.sha256(
                            f"{pr_number}:{reviewed_head_sha}:{thread_id}:resolve".encode()
                        ).hexdigest(),
                    )
                    response_data = (
                        resolve_data.get("data") if isinstance(resolve_data, dict) else None
                    )
                    mutation = (
                        response_data.get("resolveReviewThread")
                        if isinstance(response_data, dict)
                        else None
                    )
                    resolved_thread = mutation.get("thread") if isinstance(mutation, dict) else None
                    post_resolution = self._review_thread_snapshot(pr_number, thread_id)
                    resolution_proven = bool(
                        isinstance(resolved_thread, dict)
                        and resolved_thread.get("id") == thread_id
                        and resolved_thread.get("isResolved") is True
                        and isinstance(post_resolution, dict)
                        and post_resolution.get("isResolved") is True
                        and self._same_thread_snapshot(receipt, post_resolution)
                        and self._pr_is_current_open_head(
                            post_resolution.get("pr_state"), reviewed_head_sha
                        )
                    )
                except (
                    AttributeError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    subprocess.SubprocessError,
                    json.JSONDecodeError,
                ) as error:
                    logger.warning(
                        "Reviewer thread resolution proof failed on PR #%s thread %s: %s",
                        pr_number,
                        thread_id,
                        error,
                    )
                    resolution_proven = False
                if not resolution_proven:
                    # GitHub has no SHA-conditional unresolve mutation.
                    # We cannot prove that this process resolved the current
                    # discussion, so compensation could reopen a thread a
                    # human or another reviewer legitimately resolved.  Leave
                    # the outcome untouched and make the stage obtain a fresh
                    # review proof instead.
                    blocked.append(thread_id)
                    continue
                resolved.append(thread_id)
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
        ) as error:
            logger.warning("Reviewer thread reconciliation failed on PR #%s: %s", pr_number, error)
            return ReviewerThreadReconciliationResult(
                resolved_thread_ids=tuple(resolved),
                feedback_thread_ids=tuple(replied),
                blocked_thread_ids=tuple(sorted(set(candidate_ids) - set(resolved) - set(replied))),
            )
        return ReviewerThreadReconciliationResult(
            resolved_thread_ids=tuple(resolved),
            feedback_thread_ids=tuple(replied),
            blocked_thread_ids=tuple(blocked),
        )

    # -- read surface --------------------------------------------------------

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue JSON (``github_api.issues.gh_issue_json``)."""
        if self._repo_slug is not None:
            result = self._gh(
                ["issue", "view", str(issue_number), "--json", "number,title,state,labels,body"]
            )
            data = json.loads(result.stdout or "{}")
            if not isinstance(data, dict):
                raise RuntimeError(f"Failed to fetch issue #{issue_number}: non-object response")
            for field in ("title", "body"):
                value = data.get(field)
                if isinstance(value, str):
                    data[field] = github_api.strip_null_bytes(value)
            return data
        return github_api.gh_issue_json(issue_number)

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Return the merged PR closing this issue (``_review_utils``)."""
        if self._repo_slug is not None:
            return self._find_pr_for_issue(issue_number, state="merged")
        return find_merged_closing_pr(issue_number)

    def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the merged PR for this issue (tri-state seeding lookup)."""
        if self._repo_slug is not None:
            return self._find_pr_for_issue(issue_number, state="merged")
        return find_merged_pr_for_issue(issue_number)

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the selected open PR for the issue's branch or exact closing line."""
        return self._find_pr_for_issue(issue_number, state="open")

    def find_issue_for_pr(self, pr_number: int) -> int | None:
        """Return the PR's linked issue from its exact ``Closes #N`` body line."""
        try:
            result = self._gh(["pr", "view", str(pr_number), "--json", "body"], check=False)
            data = json.loads(result.stdout or "{}")
        except Exception as exc:
            logger.warning("PR #%s: linked issue read failed: %s", pr_number, exc)
            return None
        body = str(data.get("body") or "")
        match = _CLOSES_ISSUE_LINE_RE.search(body)
        if match is None:
            logger.warning("PR #%s: no exact Closes #N line found for PR-scope seeding", pr_number)
            return None
        return int(match.group(1))

    def pr_review_context(self, pr_number: int) -> dict[str, str] | None:
        """Read the PR metadata that precedes a checkout-bound review.

        A direct ``--prs`` seed must not grant the only review GO/NOGO based
        solely on a PR number.  The diff intentionally does *not* come from
        ``gh pr diff``: an ABA head race could otherwise pair another commit's
        mutable remote diff with this head.  The checkout barrier derives the
        diff locally after it proves this exact head.
        """
        try:
            body_result = self._gh(
                ["pr", "view", str(pr_number), "--json", "body,headRefOid,baseRefName"]
            )
            body_data = json.loads(body_result.stdout or "{}")
            if not isinstance(body_data, dict):
                return None
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            logger.warning("PR #%s: review context read failed: %s", pr_number, exc)
            return None
        body = body_data.get("body")
        head = body_data.get("headRefOid")
        base_branch = body_data.get("baseRefName")
        if (
            not isinstance(body, str)
            or not isinstance(head, str)
            or not head
            or not isinstance(base_branch, str)
            or not base_branch
        ):
            return None
        return {
            "pr_description": github_api.strip_null_bytes(body),
            "pr_head_sha": head,
            "pr_base_branch": base_branch,
        }

    def has_existing_plan(self, issue_number: int) -> bool:
        """Return whether the canonical plan artifact exists.

        This is an artifact-presence query, not an approval gate. Plan approval
        is read exclusively from GitHub labels by stage entry checks.
        """
        try:
            result = (
                self._gh(
                    ["issue", "view", str(issue_number), "--json", "comments"],
                    check=False,
                )
                if self._repo_slug is not None
                else gh_call(["issue", "view", str(issue_number), "--json", "comments"])
            )
            data = json.loads(result.stdout or "{}")
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and self._comments_have_plan(data.get("comments"))

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Return the PR's head branch (``_review_utils.get_pr_head_branch``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(
                    ["pr", "view", str(pr_number), "--json", "headRefName"],
                    check=False,
                )
                data = json.loads(result.stdout or "{}")
                value = data.get("headRefName") if isinstance(data, dict) else None
                return str(value) if value else None
            except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
                return None
        return get_pr_head_branch(pr_number)

    def pr_head_is_writable(self, pr_number: int) -> bool:
        """Return whether the PR head belongs to this repository's origin.

        The automation loop may review a fork PR from ``refs/pull/N/head``,
        but it cannot safely push an address commit there through the base
        repository's ``origin``. Any missing or malformed repository identity
        fails closed.
        """
        if self._repo_slug is None or self.repo is None:
            return False
        try:
            result = self._gh(
                ["pr", "view", str(pr_number), "--json", "headRepository,headRepositoryOwner"],
                check=False,
            )
            data = json.loads(result.stdout or "{}")
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        repository = data.get("headRepository")
        owner = data.get("headRepositoryOwner")
        repo_name = repository.get("name") if isinstance(repository, dict) else repository
        owner_login = owner.get("login") if isinstance(owner, dict) else owner
        return (
            isinstance(repo_name, str)
            and isinstance(owner_login, str)
            and repo_name.casefold() == self.repo.casefold()
            and owner_login.casefold() == self.org.casefold()
        )

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return ``(has_go, has_no_go)`` (``pr_manager``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(["pr", "view", str(pr_number), "--json", "labels"], check=False)
                data = json.loads(result.stdout or "{}")
                labels = self._label_names_from_payload(data if isinstance(data, dict) else {})
            except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
                return (False, False)
            return is_implementation_go(labels), has_label(labels, STATE_IMPLEMENTATION_NO_GO)
        return pr_manager.pr_has_implementation_state_label(pr_number)

    def _unresolved_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """Fetch unresolved threads through the complete repo-scoped GraphQL view.

        Fail-closed: a fetch error (subprocess, JSON, or GraphQL error)
        propagates to the caller on BOTH paths (#1868). The pipeline
        coordinator already isolates a raised exception to the single
        work item mid-step (routes it to finished(fail)) rather than
        crashing the run, so failing closed here costs one item, not a
        silent GO on unreviewed open threads.
        """
        if self._repo_slug is None:
            raise RuntimeError("review-thread operations require a repo-scoped PipelineGitHub")
        return self._repo_unresolved_threads(pr_number)

    def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """Return complete current snapshots for every unresolved review thread."""
        return self._unresolved_threads(pr_number)

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Read shared PR state for seed, implementation, and merge_wait.

        One ``gh pr view`` returns ``{state, headRefOid, mergedAt,
        baseRefName, autoMergeRequest}``; ``None`` signals a read failure.
        Seed and implementation paths use the result for terminal-state
        checks before branch adoption or label routing, while pr_review and
        merge_wait use it to bind and verify a reviewed head on a confirmed,
        unarmed PR. It deliberately excludes
        GitHub merge-readiness and check-status fields: this accessor does not
        use CI/CD as automation-loop authorization, and no queue stage uses it
        to mutate or poll auto-merge.
        """
        try:
            result = self._gh(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "state,headRefOid,mergedAt,baseRefName,autoMergeRequest",
                ]
            )
            data = json.loads(result.stdout or "{}")
            return data if isinstance(data, dict) else None
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            logger.warning("PR #%s: gh_pr_state read failed: %s", pr_number, exc)
            return None

    def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, Any] | None:
        """Read operational readiness before a conditional normal merge.

        This read classifies whether GitHub declined an otherwise valid merge
        because the PR is not ready, rather than treating CI/CD state as a
        separate source of merge authority.
        """
        try:
            result = self._gh(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "state,headRefOid,mergedAt,baseRefName,autoMergeRequest,mergeable,mergeStateStatus",
                ]
            )
            data = json.loads(result.stdout or "{}")
            return data if isinstance(data, dict) else None
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            logger.warning("PR #%s: merge readiness read failed: %s", pr_number, exc)
            return None

    def base_branch_requires_conversation_resolution(
        self, pr_number: int, base_branch: str
    ) -> bool:
        """Read the exact base branch's classic REST protection contract.

        This capability has no organization-wide fallback: merge_wait can only
        admit a normal merge when this accessor is bound to the PR's repository
        and GitHub confirms all of the following for the exact admitted base
        branch: ``required_conversation_resolution.enabled``,
        ``enforce_admins.enabled``, and no explicit pull-request bypass
        allowances. GitHub then enforces that policy on the server at merge
        time, including for administrators, covering a thread that appears
        after the local read.
        """
        if (
            pr_number <= 0
            or self._repo_slug is None
            or not isinstance(base_branch, str)
            or not base_branch
        ):
            return False
        try:
            owner, name = self._owner_name()
            branch = quote(base_branch, safe="")
            result = gh_call(
                [
                    "api",
                    "--method",
                    "GET",
                    f"/repos/{owner}/{name}/branches/{branch}/protection",
                ],
                check=False,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout or "{}")
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "PR #%s: failed to read protection for base branch %r: %s",
                pr_number,
                base_branch,
                exc,
            )
            return False
        if not isinstance(data, dict):
            return False
        conversation_resolution = data.get("required_conversation_resolution")
        enforce_admins = data.get("enforce_admins")
        return (
            isinstance(conversation_resolution, dict)
            and conversation_resolution.get("enabled") is True
            and isinstance(enforce_admins, dict)
            and enforce_admins.get("enabled") is True
            and _has_no_explicit_pull_request_bypasses(data)
        )

    def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
        """Attempt one immediate squash merge conditional on the reviewed SHA.

        The request deliberately avoids the GitHub CLI PR-merge subcommand,
        native auto-merge, merge queues, administrator flags, and retries. A
        stage-owned lifecycle read decides whether an ambiguous request may be
        retried later.
        """
        if pr_number <= 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", reviewed_sha):
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        owner, name = self._owner_name()
        if self._skip(f"conditionally squash merge PR #{pr_number} at {reviewed_sha}"):
            return ConditionalMergeResult(status=None, body=None, dry_run=True)
        try:
            result = gh_call(
                [
                    "api",
                    "--method",
                    "PUT",
                    "--include",
                    f"/repos/{owner}/{name}/pulls/{pr_number}/merge",
                    "-f",
                    f"sha={reviewed_sha}",
                    "-f",
                    "merge_method=squash",
                ],
                check=False,
                retry_on_rate_limit=False,
                max_retries=1,
            )
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            logger.warning("PR #%s: conditional merge transport failure: %s", pr_number, exc)
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        status, body, malformed = _parse_included_http_response(result.stdout or "")
        if status is None:
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        return ConditionalMergeResult(status=status, body=body, malformed=malformed)

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

    def drive_green_learn_inflight(self, issue_number: int) -> bool:
        """Return whether a persisted /learn dispatch may already have run.

        A process can fail after the agent receives its prompt but before it
        writes its outcome. This durable claim is intentionally not treated as
        a successful result: recovery retains the record for inspection, but
        must never repeat the external learning side effect.
        """
        record = self._arming.load(issue_number) or {}
        return str(record.get("learn_status") or "").lower() == "in_progress"

    # -- mutator surface (dry-run honored here) -------------------------------

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably add labels (``gh_issue_add_labels``)."""
        if self._skip(f"add labels {labels} to #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._add_labels(issue_number, labels)
            return
        github_api.gh_issue_add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably remove labels (``gh_issue_remove_labels``)."""
        if self._skip(f"remove labels {labels} from #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._remove_labels(issue_number, labels)
            return
        github_api.gh_issue_remove_labels(issue_number, labels)

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Atomically add+remove labels in a single ``gh issue edit``."""
        if self._skip(f"edit labels on #{issue_number} (+{add} -{remove})"):
            return
        if self._repo_slug is not None:
            if add:
                existing = self._label_names()
                for label in add:
                    if label not in existing:
                        self._create_label(label)
                        existing.add(label)
        elif add:
            github_api._ensure_labels_exist(add)
        cmd = ["issue", "edit", str(issue_number)]
        for label in add:
            cmd.extend(["--add-label", label])
        for label in remove:
            cmd.extend(["--remove-label", label])
        if add or remove:
            (self._gh if self._repo_slug is not None else gh_call)(cmd)

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Close the issue as covered by a merged PR (``_review_utils``)."""
        if self._skip(f"close #{issue_number} as covered by PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            self._gh(
                [
                    "issue",
                    "close",
                    str(issue_number),
                    "--comment",
                    f"Closed by merged PR #{pr_number} (Closes #{issue_number}).",
                ],
                check=False,
            )
            return
        close_issue_as_covered(issue_number, pr_number)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Upsert the actor-owned current plan, migrating the legacy heading key."""
        self.upsert_issue_comment(
            issue_number,
            PLAN_CANONICAL_MARKER,
            body,
            legacy_marker=PLAN_COMMENT_MARKER,
        )

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Upsert one actor-owned canonical comment keyed on an opaque marker.

        Human-authored marker collisions are inert: they are neither trusted,
        patched, deleted, nor allowed to deny service. A legacy human-readable
        marker may be supplied only as an actor-owned migration candidate.
        """
        if self._skip(f"upsert {marker!r} comment on #{issue_number}"):
            return
        if not body.lstrip().startswith(marker):
            raise ValueError(f"canonical comment body must start with marker {marker!r}")
        comments = self._repo_issue_comments(issue_number)
        exact = [c for c in comments if str(c.get("body", "")).lstrip().startswith(marker)]
        owned = [comment for comment in exact if self._comment_owned_by_viewer(comment)]
        if not owned and legacy_marker is not None:
            owned = [
                comment
                for comment in comments
                if str(comment.get("body", "")).lstrip().startswith(legacy_marker)
                and self._comment_owned_by_viewer(comment)
            ]
        if not owned:
            self._post_issue_comment(issue_number, body)
            comments = self._repo_issue_comments(issue_number)
            owned = [
                comment
                for comment in comments
                if str(comment.get("body", "")).lstrip().startswith(marker)
                and self._comment_owned_by_viewer(comment)
            ]
            if not owned:
                # GitHub may be briefly read-after-write stale. The next
                # idempotent pass will discover and converge the new pointer.
                return

        target_id = owned[-1].get("databaseId")
        if target_id is None:
            raise RuntimeError(f"owned comment for {marker!r} has no database id")
        owner, name = (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        if str(owned[-1].get("body", "")) != body:
            with github_api._body_file(body) as path:
                gh_call(
                    [
                        "api",
                        "--method",
                        "PATCH",
                        f"/repos/{owner}/{name}/issues/comments/{int(target_id)}",
                        "-F",
                        f"body=@{path}",
                    ]
                )
        for duplicate in owned[:-1]:
            duplicate_id = duplicate.get("databaseId")
            if duplicate_id is not None:
                self._delete_issue_comment(int(duplicate_id))

    def _post_issue_comment(self, issue_number: int, body: str) -> None:
        """Post one issue comment in the adapter's configured repository."""
        if self._repo_slug is not None:
            with github_api._body_file(body) as path:
                self._gh(["issue", "comment", str(issue_number), "--body-file", path])
            return
        github_api.gh_issue_comment(issue_number, body)

    def _delete_issue_comment(self, comment_id: int) -> None:
        """Delete one duplicate actor-owned comment in the configured repository."""
        owner, name = (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        github_api.gh_issue_delete_comment(
            comment_id,
            repo=(owner, name),
            missing_ok=True,
        )

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Append an immutable actor-owned artifact once, failing on mismatched replay."""
        if self._skip(f"append immutable {marker!r} comment on #{issue_number}"):
            return
        if not body.lstrip().startswith(marker):
            raise ValueError(f"immutable comment body must start with marker {marker!r}")
        comments = self._repo_issue_comments(issue_number)
        matching = [
            comment
            for comment in comments
            if str(comment.get("body", "")).lstrip().startswith(marker)
            and self._comment_owned_by_viewer(comment)
        ]
        if matching:
            if any(str(comment.get("body", "")) != body for comment in matching):
                raise RuntimeError(f"immutable journal conflict for marker {marker!r}")
            # Immutable history is append-only. Identical actor-owned copies
            # can arise from a create race; tolerate them without rewriting or
            # deleting the durable audit trail.
            return
        self._post_issue_comment(issue_number, body)
        comments = self._repo_issue_comments(issue_number)
        matching = [
            comment
            for comment in comments
            if str(comment.get("body", "")).lstrip().startswith(marker)
            and self._comment_owned_by_viewer(comment)
        ]
        if any(str(comment.get("body", "")) != body for comment in matching):
            raise RuntimeError(f"immutable journal conflict for marker {marker!r}")

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        PR creation requires a repo-scoped accessor.  The legacy helper can
        alter auto-merge state, so an unscoped caller must fail closed rather
        than delegate to it.

        First select and reuse an open PR on the supplied branch, then use
        ``find_pr_for_issue`` as the issue-level fallback before creating a
        PR with the *given* title/body — NOT ``pr_manager.ensure_pr_created``,
        which would discard the stage's composed body (protocol docstring).
        Dry-run returns 0 (no PR).
        """
        if self._repo_slug is None:
            raise RuntimeError("create PR requires a repo-scoped PipelineGitHub accessor")
        if self._repo_slug is not None:
            open_prs = self._open_prs_for_branch(branch)
            existing_on_branch = github_api._select_open_pr_for_base(open_prs, "main")
            if existing_on_branch is not None:
                return existing_on_branch
        existing = self.find_pr_for_issue(issue_number)
        if existing:
            return existing
        if self._skip(f"create PR for #{issue_number} from {branch!r}"):
            return 0
        if self._repo_slug is not None:
            github_api._assert_body_has_closes(body)
            github_api._assert_branch_commits_signed(branch, base="main")
            with github_api._body_file(body) as body_path:
                result = self._gh(
                    [
                        "pr",
                        "create",
                        "--head",
                        branch,
                        "--base",
                        "main",
                        "--title",
                        github_api.strip_null_bytes(title),
                        "--body-file",
                        body_path,
                    ]
                )
            raw_output = result.stdout
            output = raw_output.strip()
            match = re.search(r"/pull/(\d+)", output)
            if match:
                return int(match.group(1))
            logger.error("Failed to parse PR number from gh pr create output: %r", raw_output)
            raise RuntimeError(
                f"Failed to parse PR number from gh pr create output: {raw_output!r}"
            )
        return github_api.gh_pr_create(branch, title, body)

    def post_pr_comment(self, pr_number: int, body: str) -> None:
        """Post an explanatory PR comment (``gh_issue_comment`` channel)."""
        if self._skip(f"post comment on PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            with github_api._body_file(body) as path:
                self._gh(["issue", "comment", str(pr_number), "--body-file", path])
            return
        github_api.gh_issue_comment(pr_number, body)

    def upsert_pr_comment(self, pr_number: int, marker_prefix: str, body: str) -> bool:
        """Create-or-update a marker-keyed PR comment (issue comment channel)."""
        if self._skip(f"upsert comment on PR #{pr_number}"):
            return False
        if self._repo_slug is None:
            github_api.gh_issue_upsert_comment(pr_number, marker_prefix, body)
            return True
        self._upsert_repo_issue_comment(pr_number, marker_prefix, body)
        return True

    def _upsert_repo_issue_comment(
        self, issue_number: int, marker_prefix: str, body: str
    ) -> int | None:
        """Repo-scoped version of ``gh_issue_upsert_comment``."""
        comments = self._repo_issue_comments(issue_number)
        matching = [
            comment
            for comment in comments
            if str(comment.get("body", "")).startswith(marker_prefix)
            and comment.get("databaseId") is not None
        ]
        if not matching:
            self.post_pr_comment(issue_number, body)
            return None

        owner, name = self._owner_name()
        target_id = int(matching[-1]["databaseId"])
        for duplicate in matching[:-1]:
            duplicate_id = duplicate.get("databaseId")
            if duplicate_id is not None:
                gh_call(
                    [
                        "api",
                        "--method",
                        "DELETE",
                        f"/repos/{owner}/{name}/issues/comments/{int(duplicate_id)}",
                    ]
                )
        with github_api._body_file(body) as path:
            gh_call(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"/repos/{owner}/{name}/issues/comments/{target_id}",
                    "-F",
                    f"body=@{path}",
                ]
            )
        return target_id

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-no-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-no-go"):
            return
        if self._repo_slug is not None:
            self._add_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
            self._remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        else:
            pr_manager.mark_pr_implementation_no_go(pr_number)
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if has_go or not has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-no-go label read-back failed")

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-go"):
            return
        if self._repo_slug is not None:
            self._add_labels(pr_number, [STATE_IMPLEMENTATION_GO])
            self._remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
        else:
            pr_manager.mark_pr_implementation_go(pr_number)
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if not has_go or has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-go label read-back failed")

    def post_review_threads(
        self,
        pr_number: int,
        threads: list[dict[str, Any]],
        summary: str,
        *,
        expected_head_sha: str,
    ) -> list[dict[str, Any]]:
        """Post review threads only on a fresh, exact reviewed PR head."""
        if self._skip(f"post {len(threads)} review thread(s) on PR #{pr_number}"):
            return []
        if not self._pr_is_current_open_head(self.gh_pr_state(pr_number), expected_head_sha):
            raise RuntimeError("review publication head is stale, closed, or auto-merge armed")
        if self._repo_slug is not None:
            if threads:
                diff_result = self._gh(["pr", "diff", str(pr_number)], check=False)
                postable_threads = github_api._filter_comments_to_diff(
                    threads, diff_result.stdout or ""
                )
                if len(postable_threads) != len(threads):
                    raise RuntimeError(
                        "review-thread batch contains an anchor outside the current PR diff"
                    )
                threads = postable_threads
            review_comments = [
                {
                    "path": c["path"],
                    "line": c["line"],
                    "side": c.get("side", "RIGHT"),
                    "body": _with_severity_marker(c),
                }
                for c in threads
            ]
            owner, name = self._owner_name()
            request_body = json.dumps(
                {
                    "body": summary,
                    "commit_id": expected_head_sha,
                    "event": "COMMENT",
                    "comments": review_comments,
                }
            )
            # Diff filtering can take long enough for a push, close, or
            # auto-merge arm to invalidate the original state proof. Check
            # immediately before the irreversible review publication, then
            # retain the post-write readback below for receipt proof.
            if not self._pr_is_current_open_head(self.gh_pr_state(pr_number), expected_head_sha):
                raise RuntimeError("review publication head is stale, closed, or auto-merge armed")
            with github_api._body_file(request_body) as input_path:
                result = gh_call(
                    [
                        "api",
                        "-X",
                        "POST",
                        f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
                        "--input",
                        input_path,
                    ]
                )
            review = json.loads(result.stdout or "{}")
            review_node_id = review.get("node_id")
            if not review_node_id:
                logger.warning("Posted PR review on #%s but no review node id returned", pr_number)
                return []
            receipts = self._repo_review_thread_receipts_for_review(
                pr_number,
                str(review_node_id),
                review_comments,
            )
            if review_comments and not receipts:
                logger.warning(
                    "Posted PR review %s (node id %r) on #%s with %d comment(s) but could not "
                    "prove immutable sole-comment receipts; leaving them unresolved",
                    review.get("id"),
                    review_node_id,
                    pr_number,
                    len(review_comments),
                )
            if not self._pr_is_current_open_head(self.gh_pr_state(pr_number), expected_head_sha):
                raise RuntimeError("review publication head changed during receipt proof")
            return receipts
        return []

    def claim_drive_green_learn(self, issue_number: int, pr_number: int) -> bool:
        """Persist and read back the pre-dispatch /learn claim.

        The claim is the exactly-once boundary for the agent's external
        learning work. A nonterminal arm record becomes ``in_progress``
        before the job is handed to the worker; a restart encountering that
        state must surface an unknown outcome instead of invoking /learn a
        second time.
        """
        if self._skip(f"claim drive-green learn for #{issue_number} (PR #{pr_number})"):
            return True
        # Hold a stable sibling lock across read/check/write/readback. The
        # JSON record is atomically replaced by save(), so it cannot itself be
        # the lock inode. Every coordinator process takes this same lock before
        # claiming, making only one external /learn dispatch possible.
        with file_lock(
            self._arming.learn_claim_lock_path(issue_number),
            require_exclusive=True,
        ):
            record = self._arming.load(issue_number) or {"pr_number": pr_number}
            status = str(record.get("learn_status") or "").lower()
            if status in {"succeeded", "failed", "in_progress"}:
                return False
            record["pr_number"] = pr_number
            record["learn_status"] = "in_progress"
            record["learn_attempted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if not self._arming.save(issue_number, record):
                raise RuntimeError(
                    f"could not persist drive-green learn claim for issue #{issue_number}"
                )
            persisted = self._arming.load(issue_number)
            if (
                persisted is None
                or persisted.get("pr_number") != pr_number
                or persisted.get("learn_status") != "in_progress"
            ):
                raise RuntimeError(
                    f"could not verify drive-green learn claim for issue #{issue_number}"
                )
            return True

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
        if not self._arming.save(issue_number, record):
            raise RuntimeError(
                f"could not persist drive-green learn result for issue #{issue_number}"
            )
        persisted = self._arming.load(issue_number)
        if persisted is None or persisted.get("learn_status") != record["learn_status"]:
            raise RuntimeError(
                f"could not verify drive-green learn result for issue #{issue_number}"
            )

    # -- repo-stage surface (#1817) -------------------------------------------

    def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
        """Tag epics ``state:skip`` via the sanctioned chokepoint.

        The ONE seeding write (doc row "Epic tagging is the one seeding
        write; done BEFORE excluding"), executed by the coordinator through
        ``github_api.skip_epics``.
        """
        if self._skip(f"tag epics {sorted(epics_labels)} {STATE_SKIP}"):
            return
        if self._repo_slug is not None:
            for number, labels in epics_labels.items():
                if STATE_SKIP not in labels:
                    self._add_labels(number, [STATE_SKIP])
                    try:
                        self.upsert_issue_comment(
                            number,
                            SKIP_REASON_MARKER,
                            format_skip_reason_comment(
                                "excluded from the planning loop as an epic/roadmap "
                                "tracking issue (checklist of child work, not a code task)"
                            ),
                        )
                    except Exception as exc:  # pragma: no cover - best-effort
                        logger.warning("could not post skip-reason comment on #%s: %s", number, exc)
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
        if self._repo_slug is not None:
            existing = self._label_names()
            for label in wanted:
                if label not in existing:
                    self._create_label(label)
            return
        github_api._ensure_labels_exist(wanted)
