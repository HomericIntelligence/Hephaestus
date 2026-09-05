"""Host validation for review threads that may authorize writer remediation."""

from __future__ import annotations

from typing import Any


def _normalize_remediation_threads(
    threads: list[dict[str, Any]], *, reviewed_head_sha: str
) -> list[dict[str, Any]]:
    """Normalize only authoritative live review threads for remediation.

    An unresolved review thread can expand temporary writer scope only when
    its originating comment is from the loop or a trusted repository member,
    belongs to the exact reviewed head, and remains attached to the current
    open pull request. Thread prose is untrusted task data, not authority.
    """
    trusted_associations = frozenset({"COLLABORATOR", "MEMBER", "OWNER"})
    normalized: list[dict[str, Any]] = []
    for thread in threads:
        thread_id = str(thread.get("id") or thread.get("thread_id") or "").strip()
        comments = thread.get("comments")
        pr_state = thread.get("pr_state")
        if (
            not thread_id
            or thread.get("isResolved") is not False
            or not isinstance(comments, list)
            or not comments
            or not isinstance(pr_state, dict)
            or pr_state.get("state") != "OPEN"
            or pr_state.get("headRefOid") != reviewed_head_sha
        ):
            continue
        origin = comments[0]
        if not isinstance(origin, dict):
            continue
        origin_is_loop = origin.get("viewer_did_author") is True
        origin_is_trusted_member = (
            origin.get("author_type") == "User"
            and origin.get("author_association") in trusted_associations
        )
        if (
            not (origin_is_loop or origin_is_trusted_member)
            or not isinstance(origin.get("id"), str)
            or not origin.get("id")
            or not isinstance(origin.get("review_id"), str)
            or not origin.get("review_id")
            or origin.get("review_state") not in {"COMMENTED", "PENDING"}
            or origin.get("review_commit_sha") != reviewed_head_sha
        ):
            continue
        line = thread.get("line")
        body = str(thread.get("body") or "")
        if len(comments) > 1:
            rendered_comments: list[str] = []
            for comment in comments[1:]:
                if not isinstance(comment, dict):
                    continue
                author = str(comment.get("author") or "unknown reviewer").strip()
                comment_body = str(comment.get("body") or "").strip()
                if comment_body:
                    rendered_comments.append(f"{author}: {comment_body}")
            if rendered_comments:
                body = f"{body}\n\nThread conversation:\n" + "\n\n".join(rendered_comments)
        normalized.append(
            {
                "thread_id": thread_id,
                "path": str(thread.get("path") or ""),
                "line": (
                    line
                    if isinstance(line, int) and not isinstance(line, bool) and line > 0
                    else None
                ),
                "body": body,
            }
        )
    return normalized


__all__ = ["_normalize_remediation_threads"]
