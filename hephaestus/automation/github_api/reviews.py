"""Pull-request review posting and inline-comment dedupe helpers."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import hephaestus.automation.github_api as _api

ReviewCommentIndexKey = tuple[str, Any] | tuple[str, Any, str]


def _fetch_pr_inline_review_thread_nodes(pr_number: int) -> list[dict[str, Any]]:
    """Return PR review-thread nodes used by inline-comment dedupe helpers.

    Fails open: returns ``[]`` on any API/parse error so callers preserve their
    existing post-as-usual behaviour.
    """
    owner, repo = _api.get_repo_info()
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        _api.logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return []
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    pullRequest(number:$number){"
        "      reviewThreads(first:100){"
        "        nodes{ isResolved path line side:diffSide "
        "comments(first:20){ nodes{ id body viewerCanUpdate } } }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        result = _api._gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={repo}",
                "-F",
                f"number={int(pr_number)}",
            ],
            check=False,
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        _api.logger.warning(
            "PR #%s: could not fetch inline review-thread nodes (%s)", pr_number, exc
        )
        return []

    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def gh_pr_inline_comment_index(
    pr_number: int,
) -> dict[ReviewCommentIndexKey, tuple[str, str, bool]]:
    """Map ``(path, line)`` → ``(comment_node_id, body, editable)`` for unresolved threads.

    Returns the GraphQL node id, current body, AND ``viewerCanUpdate`` flag of
    the FIRST comment of each unresolved review thread, keyed by its
    ``(path, line)``. Used by :func:`gh_pr_review_post` to detect a line that
    already has a comment so the new content can be **appended** to the existing
    body in place rather than posted as a duplicate (#1083). The body is required
    because the ``updatePullRequestReviewComment`` mutation REPLACES the body —
    the caller must concatenate ``existing + new`` or the original comment is
    lost (#1085). The ``editable`` flag (``viewerCanUpdate``) is required because
    the first comment of a thread may belong to another app/account (Copilot,
    CodeQL); GitHub rejects an in-place edit of such a comment with
    ``Body is not editable``, so the caller must post its OWN editable shadow
    comment instead (#1327). Fails open: returns ``{}`` on any API/parse error so
    the caller posts everything as before.
    """
    index: dict[ReviewCommentIndexKey, tuple[str, str, bool]] = {}
    for node in _api._fetch_pr_inline_review_thread_nodes(pr_number):
        if node.get("isResolved"):
            continue
        comment_nodes = node.get("comments", {}).get("nodes", [])
        if not comment_nodes:
            continue
        comment_id = comment_nodes[0].get("id")
        if not comment_id:
            continue
        body = comment_nodes[0].get("body") or ""
        # Default to editable when the field is absent so behaviour is unchanged
        # for callers/tests that predate the ``viewerCanUpdate`` selection.
        editable = bool(comment_nodes[0].get("viewerCanUpdate", True))
        path = node.get("path") or ""
        line = node.get("line")
        side = node.get("side") or "RIGHT"
        index[(path, line, side)] = (comment_id, body, editable)
        # Preserve the older key shape for callers/tests that predate diff-side
        # support, and as a fallback if GitHub ever omits ``diffSide``.
        index.setdefault((path, line), (comment_id, body, editable))
    return index


def gh_pr_wont_fix_line_index(pr_number: int) -> set[ReviewCommentIndexKey]:
    """Return ``(path, line[, side])`` keys of WON'T-FIX-dismissed review threads.

    A thread is "won't-fix" when it is RESOLVED and any of its comments starts
    with :data:`~hephaestus.automation.protocol.WONT_FIX_MARKER` — the validator
    (or a human) dismissed the finding as intentional-by-design (#1163). The
    reviewer dedup (:func:`_edit_or_keep_comments`) uses this to SUPPRESS a
    re-raised finding on such a line, so an intentional-design comment cannot
    stack duplicate threads across runs. Fails open: ``set()`` on any error.
    """
    from ..protocol import WONT_FIX_MARKER

    keys: set[ReviewCommentIndexKey] = set()
    for node in _api._fetch_pr_inline_review_thread_nodes(pr_number):
        if not node.get("isResolved"):
            continue
        comment_nodes = node.get("comments", {}).get("nodes", [])
        if not any(
            str(c.get("body") or "").lstrip().startswith(WONT_FIX_MARKER)
            for c in comment_nodes
            if isinstance(c, dict)
        ):
            continue
        path = node.get("path") or ""
        line = node.get("line")
        side = node.get("side") or "RIGHT"
        keys.add((path, line, side))
        keys.add((path, line))
    return keys


def gh_pr_update_review_comment(comment_node_id: str, body: str) -> None:
    """Replace a PR review comment's body via ``updatePullRequestReviewComment``.

    Used to edit an existing inline comment in place (#1083) instead of posting
    a duplicate on the same line.

    GitHub rejects this mutation when the token does not own the comment
    ("Body is not editable").  That failure is EXPECTED and fully recovered by
    :func:`_edit_or_keep_comments` — it catches the exception and posts an
    editable shadow comment instead (#1327).  We therefore suppress ERROR-level
    logs for this call (``log_on_error=False``) so routine automation-loop runs
    are not polluted with misleading error noise for a handled condition.
    Genuine, unexpected failures still propagate as exceptions to the caller.
    """
    mutation = (
        "mutation($id:ID!,$body:String!){"
        "  updatePullRequestReviewComment(input:{pullRequestReviewCommentId:$id,body:$body}){"
        "    pullRequestReviewComment{ id }"
        "  }"
        "}"
    )
    _api._gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"id={comment_node_id}",
            "-f",
            f"body={body}",
        ],
        log_on_error=False,
    )


_BODY_NOT_EDITABLE_MARKER = "not editable"


_ADDITIONAL_REVIEW_NOTE_DELIMITER = "\n\n---\n_Additional review note (same line):_\n\n"


_REVIEW_COMMENT_DEDUPE_STOPWORDS = {
    "about",
    "actual",
    "after",
    "again",
    "also",
    "another",
    "because",
    "before",
    "being",
    "cannot",
    "changed",
    "comment",
    "coverage",
    "current",
    "future",
    "only",
    "please",
    "production",
    "proves",
    "regression",
    "sibling",
    "still",
    "suite",
    "that",
    "this",
    "where",
    "with",
    "without",
    "would",
}


_REVIEW_COMMENT_SIGNATURE_TOKENS = {
    "claude",
    "codex",
    "is_codex",
    "review_text",
    "run_codex_text",
    "stderr",
    "stdout",
    "summary",
    "verdict",
}


def _normalize_review_comment_body(body: str) -> str:
    """Normalize a review comment body for duplicate-content comparison."""
    normalized = body.lower()
    normalized = re.sub(r"`([^`]*)`", r"\1", normalized)
    normalized = re.sub(r"[^a-z0-9_#./-]+", " ", normalized)
    return " ".join(normalized.split())


def _review_comment_keyword_tokens(body: str) -> set[str]:
    """Return content-bearing tokens for same-line review duplicate checks."""
    normalized = _normalize_review_comment_body(body)
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9_#./-]+", normalized):
        candidates = [token]
        if any(sep in token for sep in "./-"):
            candidates.extend(part for part in re.split(r"[./-]+", token) if part)
        for candidate in candidates:
            if len(candidate) < 4 and not candidate.startswith("#"):
                continue
            if candidate in _REVIEW_COMMENT_DEDUPE_STOPWORDS:
                continue
            tokens.add(candidate)
    return tokens


def _review_comment_already_covers(existing_body: str, new_body: str) -> bool:
    """Return True when an existing same-line comment already covers ``new_body``."""
    new_norm = _normalize_review_comment_body(new_body)
    if not new_norm:
        return True
    new_tokens = _review_comment_keyword_tokens(new_body)
    parts = existing_body.split(_ADDITIONAL_REVIEW_NOTE_DELIMITER)
    for part in parts:
        existing_norm = _normalize_review_comment_body(part)
        if not existing_norm:
            continue
        if new_norm in existing_norm or existing_norm in new_norm:
            return True
        if SequenceMatcher(None, existing_norm, new_norm).ratio() >= 0.82:
            return True
        existing_tokens = _review_comment_keyword_tokens(part)
        if new_tokens and existing_tokens:
            overlap = existing_tokens & new_tokens
            # Re-review wording varies, but true duplicates keep the same code
            # identifiers and defect nouns. Compare against the smaller set so
            # a shorter restatement of the same finding is still suppressed.
            overlap_ratio = len(overlap) / min(len(existing_tokens), len(new_tokens))
            if len(overlap) >= 6 and overlap_ratio >= 0.6:
                return True
            signature_overlap = overlap & _REVIEW_COMMENT_SIGNATURE_TOKENS
            if len(overlap) >= 6 and len(signature_overlap) >= 3:
                return True
    return False


def _post_shadow_review_comment(pr_number: int, comment: dict[str, Any]) -> tuple[str, str] | None:
    """Post OUR OWN editable inline comment shadowing a foreign (uneditable) one.

    The first comment of a thread can belong to another app/account (Copilot,
    CodeQL); GitHub forbids editing it ("Body is not editable"). Rather than
    silently drop the finding, we post a brand-new inline comment at the same
    ``(path, line, side)`` that WE own and can edit on every later run (#1327).

    Posting reuses :func:`gh_pr_review_post` with ``dedupe_existing=False`` so it
    does NOT re-enter the dedupe path (which would re-detect the same foreign
    comment and loop). ``gh_pr_review_post`` returns thread ids, not the inline
    comment node id, so we re-fetch the inline-comment index to recover the new
    comment's editable node id for subsequent same-line updates.

    Returns ``(new_comment_node_id, posted_body)`` on success, or ``None`` if the
    post or the follow-up lookup failed (the caller then falls back to posting the
    finding fresh).
    """
    path = comment.get("path") or ""
    line = comment.get("line")
    side = comment.get("side") or "RIGHT"
    body = comment.get("body") or ""
    try:
        _api.gh_pr_review_post(
            pr_number,
            [{"path": path, "line": line, "side": side, "body": body}],
            summary="",
            dedupe_existing=False,
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        _api.logger.warning(
            "PR #%s: could not post editable shadow comment on %s:%s (%s): %s",
            pr_number,
            path,
            line,
            side,
            exc,
        )
        return None
    # Re-fetch the index to discover OUR new comment's editable node id. Prefer an
    # editable entry for this exact line; that is the comment we just posted.
    refreshed = _api.gh_pr_inline_comment_index(pr_number)
    entry = refreshed.get((path, line, side)) or refreshed.get((path, line))
    if entry is None or not entry[2]:
        _api.logger.warning(
            "PR #%s: posted editable shadow comment on %s:%s (%s) but could not relocate "
            "its editable node id",
            pr_number,
            path,
            line,
            side,
        )
        return None
    return entry[0], entry[1]


def _edit_or_keep_comments(pr_number: int, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Edit comments whose line already has an UNRESOLVED bot comment; keep the rest.

    For each comment whose ``(path, line)`` matches an UNRESOLVED existing bot
    thread, skip it if the existing body already covers the same finding, else
    rewrite that thread's comment to ``existing_body + new_body`` (a single edit
    preserving the original text, #1085) and drop it from the returned list.

    When the existing thread's first comment is NOT editable — it belongs to
    another app/account such as Copilot or CodeQL (``viewerCanUpdate`` is false,
    or the update mutation reports "Body is not editable") — we do NOT silently
    keep the foreign comment. Instead we post OUR OWN editable shadow comment on
    the same line and from then on edit THAT comment for every re-raise of the
    finding (#1327). The foreign comment is left untouched.

    Findings on fresh lines — including lines whose only prior comment is a
    RESOLVED thread — are returned unchanged for posting. Dedup is deliberately
    NOT done against resolved history (#1152 reversed #1116): a resolved thread
    is supposed to mean the finding was *fixed and verified*, so if the reviewer
    re-raises it the resolution was wrong and the finding MUST re-surface as a
    fresh thread — otherwise a force-resolved-but-unaddressed finding is
    silently suppressed, leaving the GO gate with zero unresolved threads and
    letting the PR converge with the issue still unfixed.

    Fails open: an empty index returns *comments* unchanged, and an edit that
    raises falls back to posting that comment fresh.
    """
    editable_index = _api.gh_pr_inline_comment_index(pr_number)
    wont_fix_lines = _api.gh_pr_wont_fix_line_index(pr_number)
    if not editable_index and not wont_fix_lines:
        return comments
    fresh: list[dict[str, Any]] = []
    for c in comments:
        path = c.get("path") or ""
        line = c.get("line")
        side = c.get("side") or "RIGHT"
        body = c.get("body") or ""

        # WON'T-FIX suppression (#1163): the finding's line was dismissed as
        # intentional-by-design, so do NOT re-post it (that is the whole point of
        # the dismissal — otherwise a re-raised finding stacks a new thread every
        # run since the dismissed thread is resolved, not unresolved).
        if (path, line, side) in wont_fix_lines or (path, line) in wont_fix_lines:
            _api.logger.info(
                "PR #%s: skipped re-raise of won't-fix (intentional-design) finding on %s:%s (%s)",
                pr_number,
                path,
                line,
                side,
            )
            continue

        editable = editable_index.get((path, line, side)) or editable_index.get((path, line))
        if editable is None:
            fresh.append(c)
            continue
        existing_id, existing_body, can_update = editable
        if _api._review_comment_already_covers(existing_body, body):
            _api.logger.info(
                "PR #%s: skipped duplicate same-line review comment on %s:%s (%s)",
                pr_number,
                path,
                line,
                side,
            )
            continue

        # #1327: the existing comment belongs to another app/account and is not
        # editable. Post our OWN editable shadow comment on the same line, index
        # it, and edit THAT comment on every later re-raise. Leave the foreign
        # comment untouched. ``viewerCanUpdate`` is the primary signal.
        if not can_update:
            shadow = _api._post_shadow_review_comment(pr_number, c)
            if shadow is None:
                fresh.append(c)
                continue
            new_id, new_body = shadow
            editable_index[(path, line, side)] = (new_id, new_body, True)
            editable_index[(path, line)] = (new_id, new_body, True)
            _api.logger.info(
                "PR #%s: posted editable shadow comment on %s:%s (%s); foreign comment left intact",
                pr_number,
                path,
                line,
                side,
            )
            continue

        # #1085: updatePullRequestReviewComment REPLACES the body, so concatenate
        # the existing body + the new note. Passing only the suffix would destroy
        # the original comment.
        combined = f"{existing_body}{_ADDITIONAL_REVIEW_NOTE_DELIMITER}{body}"
        try:
            _api.gh_pr_update_review_comment(existing_id, combined)
            editable_index[(path, line, side)] = (existing_id, combined, True)
            editable_index[(path, line)] = (existing_id, combined, True)
            _api.logger.info(
                "PR #%s: edited existing comment on %s:%s (%s) instead of duplicating",
                pr_number,
                path,
                line,
                side,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # A deterministic "Body is not editable" means viewerCanUpdate was
            # stale/unavailable; recover by posting our own editable shadow
            # comment (#1327). Any other edit error falls back to posting fresh.
            if _BODY_NOT_EDITABLE_MARKER in str(exc).lower():
                shadow = _api._post_shadow_review_comment(pr_number, c)
                if shadow is not None:
                    new_id, new_body = shadow
                    editable_index[(path, line, side)] = (new_id, new_body, True)
                    editable_index[(path, line)] = (new_id, new_body, True)
                    _api.logger.info(
                        "PR #%s: edit reported not-editable on %s:%s (%s); posted editable "
                        "shadow comment instead",
                        pr_number,
                        path,
                        line,
                        side,
                    )
                    continue
            _api.logger.warning(
                "PR #%s: edit-in-place failed for %s:%s (%s): %s; posting fresh",
                pr_number,
                path,
                line,
                side,
                exc,
            )
            fresh.append(c)
    return fresh


def gh_pr_review_post(
    pr_number: int,
    comments: list[dict[str, Any]],
    summary: str,
    event: str = "COMMENT",
    dry_run: bool = False,
    dedupe_existing: bool = False,
) -> list[str]:
    """Post a PR review with inline comments via GitHub GraphQL API.

    Args:
        pr_number: PR number
        comments: List of dicts with keys: path (str), line (int), side (str), body (str)
        summary: Overall review summary body
        event: Review event type: COMMENT, APPROVE, or REQUEST_CHANGES
        dry_run: If True, log intent and return empty list without posting
        dedupe_existing: When True (#1083), a comment whose ``(path, line)``
            already has an unresolved bot review comment is EDITED in place
            (the new body is appended) rather than posted as a duplicate thread.
            Only the genuinely new comments are posted as fresh threads. If
            dedupe/editing consumes every inline comment, no summary-only review
            is posted; a duplicate-only re-review should be a no-op, not another
            review submission. Fails open: if the existing-comment index cannot
            be fetched, every comment is posted as before.

    Returns:
        List of created review thread IDs (empty on dry_run or if no comments)

    """
    if dry_run:
        _api.logger.info(
            "[dry_run] Would post PR review on #%s with %s inline comments",
            pr_number,
            len(comments),
        )
        return []

    owner, repo = _api.get_repo_info()

    # Build the inline-comment list. The REST reviews endpoint natively accepts
    # ``{path, line, side, body}`` — unlike the GraphQL
    # ``DraftPullRequestReviewComment`` input type, which only exposes
    # ``path``/``position``/``body`` and has NO ``line``/``side`` fields. The
    # previous GraphQL mutation therefore failed twice over:
    #   1. ``-f comments=<json-string>`` sent the array as a STRING, so GitHub
    #      rejected every post ("Variable $comments ... was provided invalid
    #      value"; even ``[]`` failed with 'Expected "[]" to be a key-value
    #      object'), and the loop treated every PR as a spurious NOGO.
    #   2. Even passed as a typed array, ``line``/``side`` are undefined on
    #      ``DraftPullRequestReviewComment``.
    # POST /pulls/{n}/reviews is the correct surface for ``line``/``side``
    # comments and for summary-only (empty ``comments``) reviews alike.
    #
    # #1039: GitHub returns HTTP 422 and rejects the WHOLE review if any inline
    # comment targets a line outside the PR diff hunks. The reviewer model can
    # cite such lines (especially since its diff context is truncated upstream),
    # so validate each comment against the live diff and drop the strays — a
    # logged degradation instead of a hard failure that the loop reads as a NOGO.
    if comments:
        diff_result = _api._gh_call(["pr", "diff", str(pr_number)], check=False)
        comments = _api._filter_comments_to_diff(comments, diff_result.stdout or "")

    # #1083: edit-in-place instead of duplicating. If a comment targets a line
    # that already has an unresolved bot comment, append the new body to that
    # comment and drop it from the to-post set. Fails open (posts everything) if
    # the existing-comment index can't be fetched.
    had_inline_comments = bool(comments)
    if comments and dedupe_existing:
        comments = _api._edit_or_keep_comments(pr_number, comments)
        if had_inline_comments and not comments:
            _api.logger.info(
                "PR #%s: skipped duplicate-only PR review after existing-line dedupe",
                pr_number,
            )
            return []

    review_comments = [
        {
            "path": c["path"],
            "line": c["line"],
            "side": c.get("side", "RIGHT"),
            "body": c["body"],
        }
        for c in comments
    ]

    request_body = json.dumps({"body": summary, "event": event, "comments": review_comments})
    fd, input_path = tempfile.mkstemp(prefix="gh-review-", suffix=".json")
    try:
        os.close(fd)
        _api.write_secure(Path(input_path), request_body)
        result = _api._gh_call(
            [
                "api",
                "-X",
                "POST",
                f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                "--input",
                input_path,
            ]
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(input_path)

    review = json.loads(result.stdout)
    # The REST payload returns both the numeric ``id`` and the GraphQL global
    # ``node_id``. The thread-resolution follow-up matches on the GraphQL review
    # node id, so pass that through (preserving the #375 guarantee that only
    # threads created by *this* review are returned).
    review_node_id = review.get("node_id")
    if not review_node_id:
        _api.logger.warning("Posted PR review on #%s but no review node id returned", pr_number)
        return []

    thread_ids = _api._review_threads_for_review(pr_number, review_node_id)
    _api.logger.info("Posted PR review on #%s; created %s thread(s)", pr_number, len(thread_ids))
    return thread_ids


def _review_threads_for_review(pr_number: int, review_id: str) -> list[str]:
    """Return unresolved review-thread IDs belonging to review ``review_id``.

    A ``PullRequestReviewComment`` does not expose its parent thread, so we
    cannot derive thread IDs from the ``addPullRequestReview`` payload. Instead
    we list ``pullRequest.reviewThreads`` and keep the threads whose *first*
    comment was authored by this review (``comments.nodes[0].pullRequestReview.id
    == review_id``). This preserves the #375 guarantee — only threads created by
    *this* review are returned, not pre-existing human-reviewer threads — while
    using fields that actually exist in the GitHub GraphQL schema.

    Returns an empty list on any failure (the caller treats no-threads as
    "nothing to resolve later", which is safe).
    """
    owner, repo = _api.get_repo_info()
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        _api.logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return []

    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    pullRequest(number:$number){"
        "      reviewThreads(first:100){"
        "        nodes{ id isResolved comments(first:1){ nodes{ pullRequestReview{ id } } } }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        result = _api._gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={repo}",
                "-F",
                f"number={int(pr_number)}",
            ]
        )
        data = json.loads(result.stdout)
        _api._check_graphql_errors(data, f"_review_threads_for_review(pr={pr_number})")
    except (subprocess.CalledProcessError, json.JSONDecodeError, RuntimeError) as exc:
        _api.logger.warning("Could not fetch review threads for PR #%s: %s", pr_number, exc)
        return []

    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )

    # Preserve insertion order; a thread can hold multiple comments but its id
    # is unique, so a dict keyed on id dedupes naturally.
    seen: dict[str, None] = {}
    for node in nodes:
        if node.get("isResolved"):
            continue
        first_comments = node.get("comments", {}).get("nodes", [])
        if not first_comments:
            continue
        review = first_comments[0].get("pullRequestReview") or {}
        if review.get("id") != review_id:
            continue
        tid = node.get("id")
        if tid:
            seen[tid] = None

    return list(seen)
