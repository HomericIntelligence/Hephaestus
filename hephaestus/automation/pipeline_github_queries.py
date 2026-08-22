# This mixin consumes the adapter transport namespace by design.
# ruff: noqa: F403, F405
import json
import subprocess
from urllib.parse import quote

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *
from .review_journal import (
    CommentJournalReadError,
    IssueComment,
    PlanDiscoveryResult,
    discover_plan_from_comments,
    normalize_issue_comments,
)


class PipelineGitHubQueries(_PipelineGitHubHost):
    """Own read-only issue, PR, plan, and review-state queries."""

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
        matching_pr: int | None = None
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
            body = candidate.get("body")
            number = candidate.get("number")
            if not isinstance(body, str) or not isinstance(number, int) or number <= 0:
                raise RuntimeError(f"could not verify existing PR state for issue #{issue_number}")
            if has_exact_closing_line(body, issue_number):
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
        """Return a complete, ownership-verified issue-comment journal."""
        try:
            return normalize_issue_comments(
                self._repo_issue_comments(issue_number),
                viewer_login=self._viewer_login(),
            )
        except CommentJournalReadError:
            raise
        except Exception as exc:
            raise CommentJournalReadError(
                f"failed to read issue #{issue_number} comments: {exc}"
            ) from exc

    def ensure_blocked_audit(self, issue_number: int) -> None:
        """Repair an interrupted BLOCKED explanation without touching its label."""
        body = blocked_audit_recovery_body(self.issue_comments(issue_number))
        if body is None:
            return
        self.upsert_issue_comment(
            issue_number,
            PLAN_REVIEW_CANONICAL_MARKER,
            body,
        )

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue JSON (``github_api.issues.gh_issue_json``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(
                    [
                        "issue",
                        "view",
                        str(issue_number),
                        "--json",
                        "number,title,state,labels,body",
                    ]
                )
            except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                raise RuntimeError(f"Failed to fetch issue #{issue_number}: {exc}") from exc
            try:
                data = json.loads(result.stdout or "{}")
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Failed to fetch issue #{issue_number}: {exc}") from exc
            if not isinstance(data, dict):
                raise RuntimeError(f"Failed to fetch issue #{issue_number}: non-object response")
            raw_body = data.get("body")
            authority_sanitized = False
            for field in ("title", "body"):
                value = data.get(field)
                if isinstance(value, str):
                    cleaned = github_api.strip_null_bytes(value)
                    authority_sanitized = authority_sanitized or cleaned != value
                    data[field] = cleaned
            if authority_sanitized:
                data["authoritySanitized"] = True
            if isinstance(raw_body, str):
                data["bodyDigest"] = github_api.issue_body_digest(raw_body)
            return data
        return github_api.gh_issue_json(issue_number)

    def issue_body_edited_by_viewer(self, issue_number: int) -> bool:
        """Authenticate the body editor before trusting a finalized-plan seal."""
        if self._repo_slug is None:
            return github_api.gh_issue_body_edited_by_viewer(issue_number)
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            " viewer{ login }"
            " repository(owner:$owner,name:$name){"
            "  issue(number:$number){ editor{ login } }"
            " }"
            "}"
        )
        data = self._graphql(query, number=issue_number)
        root = data.get("data") if isinstance(data, dict) else None
        viewer = root.get("viewer") if isinstance(root, dict) else None
        repository = root.get("repository") if isinstance(root, dict) else None
        issue = repository.get("issue") if isinstance(repository, dict) else None
        editor = issue.get("editor") if isinstance(issue, dict) else None
        viewer_login = viewer.get("login") if isinstance(viewer, dict) else None
        editor_login = editor.get("login") if isinstance(editor, dict) else None
        return (
            isinstance(viewer_login, str)
            and bool(viewer_login)
            and isinstance(editor_login, str)
            and editor_login.lower() == viewer_login.lower()
        )

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
        """Read immutable PR metadata that precedes a checkout-bound review.

        A direct ``--prs`` seed must not grant the only review GO/NOGO based
        solely on a PR number.  The diff intentionally does *not* come from
        ``gh pr diff``: an ABA race could otherwise pair another commit's
        mutable remote diff with this base/head pair.  The checkout barrier
        derives the diff locally after it proves both exact commits.
        """
        try:
            body_result = self._gh(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "title,body,headRefOid,baseRefOid,baseRefName",
                ]
            )
            body_data = json.loads(body_result.stdout or "{}")
            if not isinstance(body_data, dict):
                return None
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            logger.warning("PR #%s: review context read failed: %s", pr_number, exc)
            return None
        title = body_data.get("title")
        body = body_data.get("body")
        head = body_data.get("headRefOid")
        base = body_data.get("baseRefOid")
        base_branch = body_data.get("baseRefName")
        if (
            not isinstance(title, str)
            or not isinstance(body, str)
            or not isinstance(head, str)
            or not head
            or not isinstance(base, str)
            or not base
            or not isinstance(base_branch, str)
            or not base_branch
        ):
            return None
        return {
            "pr_title": github_api.strip_null_bytes(title),
            "pr_description": github_api.strip_null_bytes(body),
            "pr_head_sha": head,
            "pr_base_sha": base,
            "pr_base_branch": base_branch,
        }

    def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
        """Discover the actor-owned canonical plan without inventing absence."""
        try:
            return discover_plan_from_comments(self.issue_comments(issue_number))
        except CommentJournalReadError as exc:
            logger.warning("Issue #%s: plan discovery failed: %s", issue_number, exc)
            return PlanDiscoveryResult.read_error(exc)

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
        """Return the exclusive implementation-state label flags."""
        try:
            result = self._gh(
                ["pr", "view", str(pr_number), "--json", "labels"],
                check=False,
            )
            data = json.loads(result.stdout or "{}")
            labels = self._label_names_from_payload(data if isinstance(data, dict) else {})
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
            return (False, False)
        return is_implementation_go(labels), has_label(labels, STATE_IMPLEMENTATION_NO_GO)

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
