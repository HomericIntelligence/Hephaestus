# This mixin consumes the adapter transport namespace by design.
# ruff: noqa: F403, F405
from .pipeline.stages.base import ImplementationReplyProgress
from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *


class PipelineGitHubReviews(_PipelineGitHubHost):
    """Own review-thread snapshots, replies, reconciliation, and evidence."""

    @staticmethod
    def _mutation_payload(result: object, operation: str) -> dict[str, Any]:
        """Normalize legacy test doubles while production returns typed receipts."""
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            payload = result["data"].get(operation)
            if not isinstance(payload, dict):
                return {}
            if operation == "addPullRequestReviewThreadReply":
                comment = payload.get("comment")
                return comment if isinstance(comment, dict) else {}
            if operation in {"addPullRequestReview", "submitPullRequestReview"}:
                review = payload.get("pullRequestReview")
                return review if isinstance(review, dict) else {}
            if operation == "resolveReviewThread":
                thread = payload.get("thread")
                if not isinstance(thread, dict):
                    return {}
                return {
                    **thread,
                    "clientMutationId": payload.get("clientMutationId") or "legacy-receipt",
                }
            return payload
        return result if isinstance(result, dict) else {}

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
        owner, name = self._owner_name()
        spec = github_api.review_receipts_page_query(owner, name, pr_number, review_id)
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
        while True:
            fields: dict[str, int | str] = {"number": int(pr_number)}
            if after is not None:
                fields["after"] = after
            review_threads = self._graphql(spec, **fields)
            for node in review_threads.get("nodes", []):
                if node.get("isResolved"):
                    continue
                comment_connection = node.get("comments", {})
                comments = comment_connection.get("nodes", [])
                if comment_connection.get("pageInfo", {}).get("hasNextPage") or len(comments) != 1:
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
        if unmatched or len(receipts) != len(expected):
            return []
        return receipts

    @staticmethod
    def _pr_is_current_open_head(state: dict[str, Any] | None, expected_head_sha: str) -> bool:
        """Return whether a fresh PR state is open, unarmed, and on the reviewed head."""
        return bool(
            isinstance(expected_head_sha, str)
            and _FULL_COMMIT_SHA_RE.fullmatch(expected_head_sha)
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
        self, pr_number: int, head_sha: str, thread_id: str, reply: str, batch_nonce: str
    ) -> str:
        """Bind an implementation reply to one exact thread, head, and batch."""
        if re.fullmatch(r"[0-9a-f]{32}", batch_nonce) is None:
            raise ValueError(
                "implementation reply batch nonce must be 32 lowercase hexadecimal chars"
            )
        response = reply.removeprefix("[Response] ")
        seed = ":".join(
            [
                self._repo_slug or self.org,
                str(pr_number),
                thread_id,
                head_sha,
                response,
                batch_nonce,
            ]
        )
        marker = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return (
            f"[Response] {response}\n\n"
            f"<!-- hephaestus-implementation-reply:{marker} -->\n"
            f"<!-- hephaestus-implementation-batch:{batch_nonce} -->"
        )

    @staticmethod
    def _implementation_reply_batch_nonce(review_body: object) -> str | None:
        """Return the nonce carried by one source-attached implementation reply."""
        if not isinstance(review_body, str):
            return None
        match = _IMPLEMENTATION_REPLY_BODY_RE.fullmatch(review_body)
        return match.group(2) if match is not None else None

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
            or final_comment.get("review_commit_sha") != reviewed_head_sha
        ):
            return None
        marker_match = _IMPLEMENTATION_REPLY_BODY_RE.fullmatch(reply_body)
        if marker_match is None:
            return None
        visible_reply = marker_match.group(1)
        if not visible_reply.startswith("[Response] "):
            return None
        reply = self._safe_thread_reply(visible_reply.removeprefix("[Response] "))
        batch_nonce = marker_match.group(2)
        if reply is None or batch_nonce is None:
            return None
        expected_body = self._implementation_thread_reply_body(
            pr_number, reviewed_head_sha, thread_id, reply, batch_nonce
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

        A per-thread reply marker binds one response to a thread and head. The
        shared CSPRNG batch nonce binds the complete implementation pass without
        publishing an unanchored review-level summary.
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
            reply_id, reply_body = implementation_reply
            batch_nonce = self._implementation_reply_batch_nonce(reply_body)
            review_id = comments[-1].get("review_id")
            if batch_nonce is None or not isinstance(review_id, str) or not review_id:
                return []
            seen_thread_ids.add(thread_id)
            candidates.append((thread, reply_id, reply_body, batch_nonce, review_id))
        if not candidates:
            return []
        batch_nonces = {candidate[3] for candidate in candidates}
        if len(batch_nonces) != 1:
            return []
        # Multiple implementation responses from one pass must share one
        # submitted GitHub review, not merely a common local nonce. Otherwise
        # legacy one-review-per-comment replies could be resolved as a batch.
        if len(candidates) > 1 and len({candidate[4] for candidate in candidates}) != 1:
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
            f"[Review] Reviewer validation found this still unresolved: {feedback}\n\n"
            f"<!-- hephaestus-reviewer-validation:{marker} -->"
        )

    def _add_implementation_thread_reply(
        self,
        thread_id: str,
        body: str,
        *,
        pending_review_id: str,
        expected_head_sha: str,
    ) -> dict[str, Any]:
        """Post one implementation reply bound to the pending review receipt."""
        spec = github_api.add_implementation_thread_reply_mutation(
            thread_id,
            body,
            pending_review_id=pending_review_id,
            expected_head_sha=expected_head_sha,
        )
        return self._mutation_payload(self._graphql(spec), spec.operation)

    def _add_reviewer_feedback_reply(
        self,
        thread_id: str,
        body: str,
        *,
        expected_head_sha: str,
    ) -> dict[str, Any]:
        """Post reviewer feedback and require its new COMMENTED review receipt."""
        spec = github_api.add_reviewer_feedback_reply_mutation(
            thread_id,
            body,
            expected_head_sha=expected_head_sha,
        )
        return self._mutation_payload(self._graphql(spec), spec.operation)

    def _create_pending_implementation_review(
        self, pull_request_id: str, head_sha: str, batch_nonce: str
    ) -> str | None:
        """Create one pending review envelope for an implementation reply batch."""
        del batch_nonce
        spec = github_api.create_pending_review_mutation(pull_request_id, head_sha)
        receipt = self._mutation_payload(self._graphql(spec), spec.operation)
        review_id = receipt.get("id") if isinstance(receipt, dict) else None
        return review_id if isinstance(review_id, str) and review_id else None

    def _submit_implementation_review(
        self, pull_request_id: str, review_id: str, expected_head_sha: str
    ) -> bool:
        """Submit a complete pending implementation reply review as a comment."""
        spec = github_api.submit_review_mutation(review_id, pull_request_id, expected_head_sha)
        receipt = self._mutation_payload(self._graphql(spec), spec.operation)
        return bool(isinstance(receipt, dict) and receipt.get("id") == review_id)

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
        snapshot read and attach duplicate responses.  A standalone checkout
        retains its repository-local state fallback.
        """
        repo_key = hashlib.sha256((self._repo_slug or self.org).encode("utf-8")).hexdigest()[:16]
        git_metadata = self._repo_root / ".git"
        lock_root = ensure_state_dir(self._repo_root) / "locks"
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

    def post_implementation_thread_replies(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
        batch_nonce: str | None = None,
        progress: ImplementationReplyProgress | None = None,
    ) -> ImplementationThreadReplyResult:
        """Serialize one repository-local implementation reply batch per PR.

        GitHub's client mutation id is a tracing field rather than a
        compare-and-swap primitive.  Cooperating loop processes therefore
        hold one worktree-shared PR-scoped lock across discovery and direct
        thread replies.  The adapter fails closed on platforms without an
        exclusive lock instead of risking duplicate thread replies.
        """
        candidate_ids = tuple(sorted(str(thread_id) for thread_id in replies))
        if not isinstance(batch_nonce, str) or re.fullmatch(r"[0-9a-f]{32}", batch_nonce) is None:
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
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
                    batch_nonce=batch_nonce,
                    progress=progress,
                )
        except (LockUnavailableError, OSError) as error:
            logger.warning("Implementation reply batch lock failed on PR #%s: %s", pr_number, error)
            return ImplementationThreadReplyResult(
                retryable_thread_ids=candidate_ids,
                retryable=True,
            )

    def _post_implementation_thread_replies_locked(  # noqa: C901
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
        batch_nonce: str,
        progress: ImplementationReplyProgress | None = None,
    ) -> ImplementationThreadReplyResult:
        """Post implementation-agent replies against a verified current PR head.

        Every target must be an ID from the complete host-provided thread
        snapshot. The method never resolves threads: the next reviewer pass
        performs a fresh review and owns that decision.
        """
        candidate_ids = tuple(sorted(str(thread_id) for thread_id in replies))
        if (
            not candidate_ids
            or _FULL_COMMIT_SHA_RE.fullmatch(expected_head_sha) is None
            or re.fullmatch(r"[0-9a-f]{32}", batch_nonce) is None
        ):
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
        if progress is not None and (
            not set(progress.replied_thread_ids).issubset(candidate_ids)
            or (
                progress.pending_review_id is not None
                and not isinstance(progress.pending_review_id, str)
            )
        ):
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
        pull_request_ids: set[str] = set()
        pending_review_ids: set[str] = set()
        commented_review_ids: set[str] = set()
        has_commented_recovery = False
        pending_review_id: str | None = progress.pending_review_id if progress is not None else None
        active_thread_id: str | None = progress.active_thread_id if progress is not None else None

        def safe_progress(phase: str) -> ImplementationReplyProgress:
            """Capture only proven replies before a safe pre-dispatch retry."""
            valid_phases = {
                "create_review",
                "post_replies",
                "verify_reply",
                "submit_review",
                "verify_submission",
            }
            if phase not in valid_phases:
                phase = "post_replies"
            return ImplementationReplyProgress(
                phase=phase,  # type: ignore[arg-type]
                pull_request_id=next(
                    iter(pull_request_ids),
                    progress.pull_request_id if progress is not None else "",
                ),
                pending_review_id=pending_review_id,
                replied_thread_ids=tuple(sorted(recovered)),
                receipts=tuple(recovered[thread_id] for thread_id in sorted(recovered)),
                active_thread_id=active_thread_id,
                active_comment_id=(
                    recovered.get(active_thread_id, {}).get("implementation_reply_id")
                    if active_thread_id is not None
                    else None
                ),
            )

        try:
            for thread_id in candidate_ids:
                reply = self._safe_thread_reply(replies.get(thread_id))
                if reply is not None:
                    reply = self._safe_thread_reply(reply.removeprefix("[Response] "))
                snapshot = snapshots[thread_id]
                if reply is None:
                    blocked.append(thread_id)
                    continue
                body = self._implementation_thread_reply_body(
                    pr_number, expected_head_sha, thread_id, reply, batch_nonce
                )
                live = self._review_thread_snapshot(pr_number, thread_id)
                if not isinstance(live, dict) or live.get("isResolved") is not False:
                    blocked.append(thread_id)
                    continue
                if not self._pr_is_current_open_head(live.get("pr_state"), expected_head_sha):
                    # The shared handoff has just observed the exact pushed
                    # head. A later per-thread GraphQL read can still lag that
                    # fact briefly. Treat it as a retryable host-visibility
                    # race; the outer retry rereads PR state and classifies a
                    # real head move as stale before posting anything.
                    return ImplementationThreadReplyResult(
                        retryable_thread_ids=candidate_ids,
                        retryable=True,
                        visibility_lag=True,
                    )
                pull_request_id = live.get("pr_node_id")
                if not isinstance(pull_request_id, str) or not pull_request_id:
                    blocked.append(thread_id)
                    continue
                pull_request_ids.add(pull_request_id)
                reply_bodies[thread_id] = body
                # A prior transport failure may have applied this exact reply.
                # Recover its host proof before treating the saved snapshot as
                # stale or attempting a duplicate mutation.
                recovered_id = self._host_reply_receipt(snapshot, live, body)
                if recovered_id is not None:
                    recovered[thread_id] = receipt(live, recovered_id, body)
                    final_comment = live.get("comments", [])[-1]
                    review_id = (
                        final_comment.get("review_id") if isinstance(final_comment, dict) else None
                    )
                    review_state = (
                        final_comment.get("review_state")
                        if isinstance(final_comment, dict)
                        else None
                    )
                    if not isinstance(review_id, str) or not review_id:
                        blocked.append(thread_id)
                    elif review_state == "PENDING":
                        pending_review_ids.add(review_id)
                    elif review_state == "COMMENTED":
                        has_commented_recovery = True
                        commented_review_ids.add(review_id)
                    else:
                        blocked.append(thread_id)
                    continue
                if not self._same_thread_snapshot(snapshot, live):
                    blocked.append(thread_id)
                    continue
                prepared.append((thread_id, snapshot, body))

            if blocked:
                return ImplementationThreadReplyResult(
                    blocked_thread_ids=tuple(sorted(blocked)),
                )
            if len(pull_request_ids) != 1 or len(pending_review_ids) > 1:
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )
            pull_request_id = next(iter(pull_request_ids))
            observed_pending_review_id = next(iter(pending_review_ids), None)
            if (
                observed_pending_review_id is not None
                and pending_review_id is not None
                and observed_pending_review_id != pending_review_id
            ):
                return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
            pending_review_id = observed_pending_review_id or pending_review_id
            if has_commented_recovery and (prepared or pending_review_id is not None):
                # A submitted review cannot safely be extended. A mixed
                # recovery means another actor changed the batch boundary.
                return ImplementationThreadReplyResult(
                    retryable_thread_ids=candidate_ids,
                    retryable=True,
                )
            if has_commented_recovery and len(candidate_ids) > 1 and len(commented_review_ids) != 1:
                # Never let legacy one-review-per-comment recovery masquerade
                # as a complete implementation pass.
                return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
            if has_commented_recovery:
                # The submit mutation may have succeeded even when its
                # response was lost. Every reply is already bound to the
                # submitted review, so a retry must not create an empty one.
                return ImplementationThreadReplyResult(
                    replied_thread_ids=candidate_ids,
                    receipts=tuple(recovered[thread_id] for thread_id in candidate_ids),
                )
            # Every implementation pass, including a singleton reply, must be
            # submitted as one GitHub review. The later comment-review handoff
            # accepts only replies bound to that submitted review.
            if pending_review_id is None:
                pending_review_id = self._create_pending_implementation_review(
                    pull_request_id, expected_head_sha, batch_nonce
                )
                if pending_review_id is None:
                    # GitHub may have accepted the create mutation while its
                    # response was lost. There is no source-attached receipt
                    # yet from which to identify that pending review safely;
                    # fail closed rather than creating a duplicate envelope.
                    return ImplementationThreadReplyResult(
                        blocked_thread_ids=candidate_ids,
                    )
            for thread_id, snapshot, body in prepared:
                active_thread_id = thread_id
                reply_receipt = self._add_implementation_thread_reply(
                    thread_id,
                    body,
                    pending_review_id=pending_review_id,
                    expected_head_sha=expected_head_sha,
                )
                comment_id = reply_receipt.get("id")
                replied_live = self._review_thread_snapshot(pr_number, thread_id)
                if (
                    not isinstance(replied_live, dict)
                    or replied_live.get("isResolved") is not False
                    or not self._pr_is_current_open_head(
                        replied_live.get("pr_state"), expected_head_sha
                    )
                ):
                    # The reply mutation returned a receipt, but the
                    # required exact-head readback did not prove it. The
                    # mutation already ran; do not replay it from a handoff.
                    return ImplementationThreadReplyResult(
                        blocked_thread_ids=candidate_ids,
                    )
                proved_comment_id = self._host_reply_receipt(
                    snapshot,
                    replied_live,
                    body,
                    comment_id,
                )
                if proved_comment_id is None:
                    # A receipt that cannot be reconciled to the immutable
                    # post-mutation snapshot is not safe retry work.
                    return ImplementationThreadReplyResult(
                        blocked_thread_ids=candidate_ids,
                    )
                recovered[thread_id] = receipt(replied_live, proved_comment_id, body)
                active_thread_id = None
            if pending_review_id is not None and not self._submit_implementation_review(
                pull_request_id, pending_review_id, expected_head_sha
            ):
                # Submission was attempted; an unproven receipt is terminal
                # for this intent and cannot be replayed.
                return ImplementationThreadReplyResult(
                    blocked_thread_ids=candidate_ids,
                )
            if pending_review_id is not None:
                for thread_id in candidate_ids:
                    body = reply_bodies[thread_id]
                    submitted_live = self._review_thread_snapshot(pr_number, thread_id)
                    if (
                        not isinstance(submitted_live, dict)
                        or submitted_live.get("isResolved") is not False
                        or not self._pr_is_current_open_head(
                            submitted_live.get("pr_state"), expected_head_sha
                        )
                    ):
                        # The submit mutation has already run; the missing
                        # proof must trigger a fresh review, never replay.
                        return ImplementationThreadReplyResult(
                            blocked_thread_ids=candidate_ids,
                        )
                    submitted_id = self._validated_implementation_reply(
                        pr_number, expected_head_sha, submitted_live
                    )
                    if submitted_id is None or submitted_id[1] != body:
                        return ImplementationThreadReplyResult(
                            blocked_thread_ids=candidate_ids,
                        )
                    recovered[thread_id] = receipt(submitted_live, submitted_id[0], body)
        except github_api.GraphQLMutationOutcomeUnknownError as error:
            logger.warning(
                "Implementation reply mutation outcome is unknown on PR #%s: %s",
                pr_number,
                error.intent.safe_summary(),
            )
            return ImplementationThreadReplyResult(
                blocked_thread_ids=candidate_ids,
                outcome_unknown=True,
            )
        except github_api.GraphQLResponseError as error:
            if isinstance(error, github_api.GraphQLRetryableError) and error.pre_dispatch:
                remaining_ids = tuple(sorted(set(candidate_ids) - set(recovered)))
                progress_snapshot = (
                    safe_progress(
                        "create_review"
                        if pending_review_id is None
                        else "submit_review"
                        if not remaining_ids
                        else "verify_reply"
                        if active_thread_id is not None
                        else "post_replies"
                    )
                    if pull_request_ids or pending_review_id is not None or progress is not None
                    else None
                )
                return ImplementationThreadReplyResult(
                    replied_thread_ids=tuple(sorted(recovered)),
                    receipts=tuple(recovered[thread_id] for thread_id in sorted(recovered)),
                    retryable_thread_ids=remaining_ids,
                    progress=progress_snapshot,
                    retryable=True,
                )
            logger.warning(
                "Implementation reply GraphQL operation blocked on PR #%s: %s", pr_number, error
            )
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
        ) as error:
            logger.warning("Implementation thread replies failed on PR #%s: %s", pr_number, error)
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
        return ImplementationThreadReplyResult(
            replied_thread_ids=candidate_ids,
            receipts=tuple(recovered[thread_id] for thread_id in candidate_ids),
        )

    def reconcile_implementation_thread_replies(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
        batch_nonce: str,
    ) -> ImplementationThreadReplyResult:
        """Reconcile an armed handoff with read-only, marker-bound snapshots."""
        candidate_ids = tuple(sorted(str(thread_id) for thread_id in replies))
        if (
            not candidate_ids
            or _FULL_COMMIT_SHA_RE.fullmatch(expected_head_sha) is None
            or re.fullmatch(r"[0-9a-f]{32}", batch_nonce) is None
        ):
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
        by_id = {
            str(thread.get("id")): thread
            for thread in threads
            if isinstance(thread, dict) and isinstance(thread.get("id"), str)
        }
        if set(by_id) != set(candidate_ids):
            return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
        receipts: list[dict[str, Any]] = []
        for thread_id in candidate_ids:
            reply = self._safe_thread_reply(replies.get(thread_id))
            if reply is None:
                return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
            live = self._review_thread_snapshot(pr_number, thread_id)
            if (
                not isinstance(live, dict)
                or live.get("isResolved") is not False
                or not self._pr_is_current_open_head(live.get("pr_state"), expected_head_sha)
            ):
                return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
            expected_body = self._implementation_thread_reply_body(
                pr_number, expected_head_sha, thread_id, reply, batch_nonce
            )
            validated = self._validated_implementation_reply(pr_number, expected_head_sha, live)
            if validated is None or validated[1] != expected_body:
                return ImplementationThreadReplyResult(blocked_thread_ids=candidate_ids)
            receipts.append(
                {
                    **live,
                    "implementation_reply_id": validated[0],
                    "implementation_reply_body": validated[1],
                    "implementation_head_sha": expected_head_sha,
                }
            )
        return ImplementationThreadReplyResult(
            replied_thread_ids=candidate_ids,
            receipts=tuple(receipts),
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
            or _FULL_COMMIT_SHA_RE.fullmatch(reviewed_head_sha) is None
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
                    try:
                        feedback_receipt = self._add_reviewer_feedback_reply(
                            thread_id,
                            body,
                            expected_head_sha=reviewed_head_sha,
                        )
                    except github_api.GraphQLMutationOutcomeUnknownError as error:
                        logger.warning(
                            "Reviewer feedback mutation outcome is unknown on PR #%s thread %s: %s",
                            pr_number,
                            thread_id,
                            error.intent.safe_summary(),
                        )
                        blocked.append(thread_id)
                        continue
                    except github_api.GraphQLResponseError as error:
                        logger.warning(
                            "Reviewer feedback mutation blocked on PR #%s thread %s: %s",
                            pr_number,
                            thread_id,
                            error,
                        )
                        blocked.append(thread_id)
                        continue
                    comment_id = feedback_receipt.get("id")
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
                    # A transport error can arrive after GitHub has applied
                    # the mutation.  The typed receipt is therefore the only
                    # mutation result accepted before the readback proof.
                    resolve_spec = github_api.resolve_thread_mutation(thread_id)
                    resolve_receipt = self._graphql(resolve_spec)
                    resolved_thread = self._mutation_payload(
                        resolve_receipt, resolve_spec.operation
                    )
                    post_resolution = self._review_thread_snapshot(pr_number, thread_id)
                    resolution_proven = bool(
                        isinstance(resolved_thread, dict)
                        and resolved_thread.get("clientMutationId")
                        and resolved_thread.get("id") == thread_id
                        and resolved_thread.get("isResolved") is True
                        and isinstance(post_resolution, dict)
                        and post_resolution.get("isResolved") is True
                        and self._same_thread_snapshot(receipt, post_resolution)
                        and self._pr_is_current_open_head(
                            post_resolution.get("pr_state"), reviewed_head_sha
                        )
                    )
                except github_api.GraphQLMutationOutcomeUnknownError as error:
                    logger.warning(
                        "Reviewer thread resolution outcome is unknown on PR #%s thread %s: %s",
                        pr_number,
                        thread_id,
                        error.intent.safe_summary(),
                    )
                    # There is no compensating unresolve operation.  Leave
                    # the thread alone and force a fresh reviewer pass.
                    blocked.append(thread_id)
                    continue
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

    def post_review_threads(
        self,
        pr_number: int,
        threads: list[dict[str, Any]],
        *,
        expected_head_sha: str,
        review_diff: str | None = None,
    ) -> list[dict[str, Any]]:
        """Post one source-anchored review batch for an immutable snapshot."""
        # GitHub renders a review-level ``body`` as an unanchored general
        # comment. Publish only reviews that contain source-positioned threads.
        if not threads:
            logger.info(
                "PR #%s: skipped review publication without source-anchored threads",
                pr_number,
            )
            return []
        if self._skip(f"post {len(threads)} review thread(s) on PR #{pr_number}"):
            return []
        if self._repo_slug is not None:
            snapshot_diff = review_diff
            if snapshot_diff is None:
                # Direct callers that do not own a detached checkout retain
                # compatibility. The review stage always supplies its local
                # snapshot, so its anchors never move with the remote PR.
                snapshot_diff = self._gh(["pr", "diff", str(pr_number)], check=False).stdout or ""
            postable_threads = github_api._filter_comments_to_diff(threads, snapshot_diff)
            if len(postable_threads) != len(threads):
                raise RuntimeError(
                    "review-thread batch contains an anchor outside the reviewed diff"
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
                    "commit_id": expected_head_sha,
                    "event": "COMMENT",
                    "comments": review_comments,
                }
            )
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
            return receipts
        return []
