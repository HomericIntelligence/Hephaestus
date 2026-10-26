# This mixin consumes the adapter transport namespace by design.
# ruff: noqa: F403, F405

import json
from collections.abc import Mapping, Sequence

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *

THREAD_COMMENT_MAX = 2_000
THREAD_COMMENT_PAGE_MAX = 20
THREAD_SNAPSHOT_MAX_BYTES = 192 * 1024


def admitted_review_comment(
    comment: Mapping[str, object],
    *,
    author: str,
    author_type: str,
    review: tuple[str | None, str | None, str, str],
    comment_count: int,
    current_bytes: int,
) -> tuple[dict[str, object], int] | None:
    """Normalize one reviewed comment and return its new bounded byte total."""
    if comment_count >= THREAD_COMMENT_MAX:
        return None
    review_id, review_state, review_body, review_commit_sha = review
    normalized = {
        "id": comment["id"],
        "body": comment["body"],
        "author": author,
        "author_type": author_type,
        "viewer_did_author": comment["viewerDidAuthor"],
        "review_id": review_id,
        "review_state": review_state,
        "review_body": review_body,
        "review_commit_sha": review_commit_sha,
    }
    new_size = current_bytes + len(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    if new_size > THREAD_SNAPSHOT_MAX_BYTES:
        return None
    return normalized, new_size


#: GitHub rejects a review body above 65,536 characters.
MAX_REVIEW_BODY_CHARS = 65_000


def _severity_prefix(finding: dict[str, Any]) -> str:
    """Return the informational severity tag for a comment, when it has one."""
    severity = finding.get("severity")
    return f"**[{severity}]** " if isinstance(severity, str) and severity else ""


def _location_text(finding: dict[str, Any]) -> str:
    """Return a finding with its location, for the review body."""
    path = finding.get("path")
    line = finding.get("line")
    where = f"`{path}`" + (f" line {line}" if line else "") if path else ""
    return f"{where}\n{_severity_prefix(finding)}{finding['body']}".strip()


class PipelineGitHubReviews(_PipelineGitHubHost):
    """Own review-thread snapshots, replies, reconciliation, and evidence."""

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
        owner, name = self._owner_name()
        spec = github_api.pipeline_thread_snapshot_page_query(owner, name, pr_number, thread_id)

        def read_once() -> tuple[dict[str, Any], bool] | None:  # noqa: C901
            comments: list[dict[str, Any]] = []
            comment_bytes = 0
            seen_comment_ids: set[str] = set()
            seen_cursors: set[str] = set()
            after: str | None = None
            page_count = 0
            expected_pr_id: str | None = None
            expected_pr_state: dict[str, Any] | None = None
            expected_thread_fields: tuple[bool, str, int | None, str | None] | None = None
            while True:
                if page_count >= THREAD_COMMENT_PAGE_MAX:
                    raise RuntimeError(
                        f"could not fetch all comments for PR review thread {thread_id}"
                    )
                page_count += 1
                fields: dict[str, int | str] = {
                    "number": int(pr_number),
                    "threadId": thread_id,
                }
                if after is not None:
                    fields["after"] = after
                page = self._graphql(spec, **fields)
                pr_id = page["pr_node_id"]
                pr_state = page["pr_state"]
                if expected_pr_id is None:
                    expected_pr_id = pr_id
                    expected_pr_state = pr_state
                elif expected_pr_id != pr_id or expected_pr_state != pr_state:
                    return None
                node = page["thread"]
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
                comment_connection = page["comments"]
                comment_nodes = comment_connection["nodes"]
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
                    admitted = admitted_review_comment(
                        comment,
                        author=author,
                        author_type=author_type,
                        review=(review_id, review_state, review_body, review_commit_sha),
                        comment_count=len(comments),
                        current_bytes=comment_bytes,
                    )
                    if admitted is None:
                        return None
                    normalized_comment, comment_bytes = admitted
                    comments.append(normalized_comment)
                page_info = comment_connection["pageInfo"]
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
                            "pr_node_id": expected_pr_id,
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
        """Return a local worktree-shared lock for one repository PR reply batch.

        A linked worktree has a ``.git`` file pointing at the common
        repository's ``.git/worktrees/<name>`` directory.  Reply publication
        must use the common directory rather than the worktree-local state
        directory, otherwise separate loop processes can both pass the
        snapshot read and attach duplicate responses. A standalone checkout
        uses its explicit durable state root as the fallback.
        """
        repo_key = hashlib.sha256((self._repo_slug or self.org).encode("utf-8")).hexdigest()[:16]
        git_metadata = self._repo_root / ".git"
        lock_root = ensure_state_dir(self._state_root) / "locks"
        if git_metadata.is_dir():
            lock_root = git_metadata / "hephaestus-automation-locks"
        elif git_metadata.is_file():
            try:
                first_line = git_metadata.read_text(encoding="utf-8").splitlines()[0]
            except (IndexError, OSError, UnicodeDecodeError) as error:
                raise LockUnavailableError(
                    f"could not read Git metadata for implementation reply lock at {git_metadata}"
                ) from error
            prefix, separator, raw_git_dir = first_line.partition(":")
            if prefix != "gitdir" or not separator or not raw_git_dir.strip():
                raise LockUnavailableError(
                    f"invalid Git metadata for implementation reply lock at {git_metadata}"
                )
            git_dir = Path(raw_git_dir.strip())
            if not git_dir.is_absolute():
                git_dir = git_metadata.parent / git_dir
            common_git_dir = git_dir.parent.parent
            common_dir_file = git_dir / "commondir"
            if (
                git_dir.parent.name != "worktrees"
                or not git_dir.is_dir()
                or not (git_dir / "HEAD").is_file()
                or not common_git_dir.is_dir()
                or not (common_git_dir / "HEAD").is_file()
                or not common_dir_file.is_file()
            ):
                raise LockUnavailableError(
                    "invalid linked-worktree Git metadata for implementation reply lock at "
                    f"{git_metadata}"
                )
            try:
                common_dir_text = common_dir_file.read_text(encoding="utf-8").strip()
                common_dir_from_metadata = Path(common_dir_text)
                if not common_dir_from_metadata.is_absolute():
                    common_dir_from_metadata = git_dir / common_dir_from_metadata
                if (
                    not common_dir_text
                    or common_dir_from_metadata.resolve() != common_git_dir.resolve()
                ):
                    raise LockUnavailableError(
                        "invalid common Git directory for implementation reply lock at "
                        f"{git_metadata}"
                    )
            except (OSError, UnicodeDecodeError) as error:
                raise LockUnavailableError(
                    "could not read common Git directory for implementation reply lock at "
                    f"{git_metadata}"
                ) from error
            lock_root = common_git_dir / "hephaestus-automation-locks"
        return lock_root / f"implementation-replies-{repo_key}-{pr_number}.lock"

    def post_review(
        self,
        pr_number: int,
        inline: Sequence[dict[str, Any]],
        body_only: Sequence[dict[str, Any]],
        summary: str = "",
    ) -> None:
        """Post one review: inline comments on the diff, the rest in the body."""
        if not inline and not body_only and not summary:
            return
        if self._skip(f"post a review with {len(inline)} comment(s) on PR #{pr_number}"):
            return
        body = "\n\n".join(
            part for part in (summary, *(_location_text(finding) for finding in body_only)) if part
        )
        request_body = json.dumps(
            {
                "event": "COMMENT",
                "comments": [
                    {
                        "path": finding["path"],
                        "line": finding["line"],
                        "side": "RIGHT",
                        "body": f"{_severity_prefix(finding)}{finding['body']}",
                    }
                    for finding in inline
                ],
                **({"body": body[:MAX_REVIEW_BODY_CHARS]} if body else {}),
            }
        )
        owner, name = self._owner_name()
        with github_api._body_file(request_body) as input_path:
            self._deadline_gh_call(
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

    def apply_review_thread_decisions(
        self,
        pr_number: int,
        *,
        resolved_thread_ids: Sequence[str],
        feedback: dict[str, str],
    ) -> None:
        """Resolve the threads the reviewer accepted and answer the ones it did not."""
        if self._skip(f"apply reviewer decisions on PR #{pr_number}"):
            return
        for thread_id in resolved_thread_ids:
            self._graphql_call(
                "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}",
                id=thread_id,
            )
        for thread_id, detail in feedback.items():
            self._graphql_call(
                "mutation($id:ID!,$body:String!){addPullRequestReviewThreadReply("
                "input:{pullRequestReviewThreadId:$id,body:$body}){comment{id}}}",
                id=thread_id,
                body=f"[Review] Still unresolved: {detail}",
            )

    def _graphql_call(self, query: str, **variables: str) -> None:
        """Run one GraphQL mutation through ``gh``."""
        argv = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            argv += ["-f", f"{key}={value}"]
        self._deadline_gh_call(argv, timeout=self._gh_timeout)
