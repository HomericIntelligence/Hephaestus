"""Contracts for immutable remediation recovery records."""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline.github_jobs import ImplementationReplyProgress
from hephaestus.automation.pipeline.reply_handoff import (
    implementation_remediation_reply_handoff,
    implementation_remediation_reply_handoff_journal_entry,
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
    journaled_implementation_remediation_reply_handoff,
    retry_pending_implementation_reply_handoff,
)
from hephaestus.automation.remediation_prepublication import (
    RemediationPreparationIntent,
    load_prepublication_receipt,
    remove_prepublication_receipt,
    save_prepublication_intent,
    save_prepublication_receipt,
)
from hephaestus.automation.remediation_recovery import (
    REMEDIATION_REVIEW_INPUT_FORMAT,
    RemediationRecoveryReceipt,
    RemediationReplyResult,
    RemediationReviewInput,
    encode_remediation_review_input,
)
from hephaestus.automation.review_journal import IssueComment


def _threads() -> list[dict[str, Any]]:
    return [
        {
            "id": "thread-1",
            "isResolved": False,
            "path": "a.py",
            "line": 4,
            "side": "RIGHT",
            "comments": [
                {
                    "id": "comment-1",
                    "author": "reviewer",
                    "body": "Fix this.",
                }
            ],
        }
    ]


def _review_input(**overrides: object) -> RemediationReviewInput:
    diff = "diff --git a/a.py b/a.py\n+value = 2\n"
    fields: dict[str, object] = {
        "format_version": REMEDIATION_REVIEW_INPUT_FORMAT,
        "repository": "homericintelligence/hephaestus",
        "issue_number": 3009,
        "pr_number": 3010,
        "repo_root": "/repo",
        "worktree_path": "/repo/build/writer",
        "branch": "codex/fix-2975-review-findings",
        "reviewed_parent_sha": "a" * 40,
        "candidate_tree_sha": "b" * 40,
        "recovery_commit_sha": "c" * 40,
        "changed_paths": ("a.py",),
        "committed_diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "committed_diff": diff,
        "failure_diagnostic": "provider failed after file write",
        "thread_snapshot_sha256": RemediationReviewInput.thread_snapshot_digest(_threads()),
        "thread_snapshot_json": RemediationReviewInput.canonical_thread_snapshot(_threads()),
    }
    fields.update(overrides)
    return RemediationReviewInput(**fields)  # type: ignore[arg-type]


def test_review_input_uses_exact_canonical_16_field_bytes() -> None:
    """The review digest covers one exact ordered UTF-8 array."""
    review_input = _review_input()
    decoded = json.loads(review_input.canonical_bytes)

    assert len(decoded) == 16
    assert decoded[0] == 3
    assert decoded[1:4] == ["homericintelligence/hephaestus", 3009, 3010]
    assert decoded[10] == ["a.py"]
    assert decoded[15] == RemediationReviewInput.canonical_thread_snapshot(_threads())
    assert (
        review_input.review_input_sha256 == hashlib.sha256(review_input.canonical_bytes).hexdigest()
    )
    assert RemediationReviewInput.from_canonical_bytes(review_input.canonical_bytes) == review_input


def test_review_input_is_deeply_immutable() -> None:
    """Caller mutation cannot change the sealed paths or thread snapshot."""
    paths = ["a.py"]
    threads = _threads()
    review_input = _review_input(
        changed_paths=tuple(paths),
        thread_snapshot_sha256=RemediationReviewInput.thread_snapshot_digest(threads),
        thread_snapshot_json=RemediationReviewInput.canonical_thread_snapshot(threads),
    )
    before = review_input.canonical_bytes

    paths.append("b.py")
    threads[0]["isResolved"] = True

    assert review_input.changed_paths == ("a.py",)
    assert review_input.canonical_bytes == before
    with pytest.raises(FrozenInstanceError):
        review_input.branch = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("format_version", 2, "format_version"),
        ("repository", "Other/Repo", "repository"),
        ("issue_number", 0, "issue_number"),
        ("repo_root", "relative", "repo_root"),
        ("changed_paths", ("../escape",), "changed_paths"),
        ("committed_diff_sha256", "d" * 64, "committed_diff_sha256"),
        ("thread_snapshot_sha256", "e" * 64, "thread_snapshot_sha256"),
    ],
)
def test_review_input_rejects_identity_or_nested_digest_drift(
    field: str, value: object, message: str
) -> None:
    """A changed identity or nested payload cannot recreate the receipt."""
    with pytest.raises(ValueError, match=message):
        _review_input(**{field: value})


def test_reply_result_is_exhaustive_and_digest_bound() -> None:
    """The reply map has the same threads and exact review-input digest."""
    review_input = _review_input()
    result = RemediationReplyResult.create(
        review_input_sha256=review_input.review_input_sha256,
        replies={"thread-1": "Fixed the validation."},
        thread_snapshot_json=review_input.thread_snapshot_json,
    )

    assert result.replies == (("thread-1", "Fixed the validation."),)
    assert RemediationReplyResult.from_dict(result.as_dict()) == result
    assert result.digest == hashlib.sha256(result.canonical_bytes).hexdigest()
    with pytest.raises(ValueError, match="reply IDs"):
        RemediationReplyResult.create(
            review_input_sha256=review_input.review_input_sha256,
            replies={"thread-2": "Wrong thread."},
            thread_snapshot_json=review_input.thread_snapshot_json,
        )


def test_canonical_decode_rejects_noncanonical_or_changed_nested_data() -> None:
    """Recovery recalculates canonical bytes and nested digests."""
    review_input = _review_input()
    spaced = json.dumps(json.loads(review_input.canonical_bytes), ensure_ascii=False).encode()
    with pytest.raises(ValueError, match="canonical"):
        RemediationReviewInput.from_canonical_bytes(spaced)

    changed = json.loads(review_input.canonical_bytes)
    changed[13] = "different diagnostic"
    changed_bytes = json.dumps(changed, ensure_ascii=False, separators=(",", ":")).encode()
    recovered = RemediationReviewInput.from_canonical_bytes(changed_bytes)
    assert recovered.review_input_sha256 != review_input.review_input_sha256

    with pytest.raises(ValueError, match="committed_diff_sha256"):
        replace(review_input, committed_diff="different diff")


def _reply_result(review_input: RemediationReviewInput) -> RemediationReplyResult:
    return RemediationReplyResult.create(
        review_input_sha256=review_input.review_input_sha256,
        replies={"thread-1": "Fixed the validation."},
        thread_snapshot_json=review_input.thread_snapshot_json,
    )


def _recovery_receipt(review_input: RemediationReviewInput) -> RemediationRecoveryReceipt:
    journal_input_encoding, journal_input_data = encode_remediation_review_input(
        review_input.canonical_bytes
    )
    return RemediationRecoveryReceipt(
        review_input_bytes=review_input.canonical_bytes.decode("utf-8"),
        review_input_sha256=review_input.review_input_sha256,
        journal_input_encoding=journal_input_encoding,
        journal_input_data=journal_input_data,
        expected_remote_sha=review_input.reviewed_parent_sha,
        content_snapshot=(
            ("index_sha256", "1" * 64),
            ("untracked_sha256", "2" * 64),
            ("worktree_sha256", "3" * 64),
        ),
        add_paths=review_input.changed_paths,
        update_paths=(),
    )


def _implementation_reply_body(
    review_input: RemediationReviewInput,
    reply: str,
    batch_nonce: str,
    *,
    thread_id: str = "thread-1",
) -> str:
    """Build the deterministic host reply used in a progress receipt."""
    response = reply.removeprefix("[Response] ")
    seed = ":".join(
        (
            review_input.repository,
            str(review_input.pr_number),
            thread_id,
            review_input.recovery_commit_sha,
            response,
            batch_nonce,
        )
    )
    marker = hashlib.sha256(seed.encode()).hexdigest()[:24]
    return (
        f"[Response] {response}\n\n"
        f"<!-- hephaestus-implementation-reply:{marker} -->\n"
        f"<!-- hephaestus-implementation-batch:{batch_nonce} -->"
    )


def _production_live_thread(thread: dict[str, Any]) -> dict[str, Any]:
    """Add the fields that the GitHub thread hydration returns in production."""
    live = deepcopy(thread)
    live["pr_node_id"] = "PR_node"
    live["comments"] = [
        {
            **comment,
            "author_type": "User",
            "viewer_did_author": False,
            "review_id": "review-original",
            "review_state": "COMMENTED",
            "review_body": "",
            "review_commit_sha": "a" * 40,
        }
        for comment in live["comments"]
    ]
    return live


def _progress(
    review_input: RemediationReviewInput,
    reply: str,
    batch_nonce: str,
) -> tuple[ImplementationReplyProgress, list[dict[str, Any]]]:
    """Build one proved partial-delivery progress snapshot."""
    body = _implementation_reply_body(review_input, reply, batch_nonce)
    live = _production_live_thread(_threads()[0])
    live["comments"].append(
        {
            "id": "implementation-comment-1",
            "author": "hephaestus",
            "body": body,
            "viewer_did_author": True,
            "review_id": "PRR_pending",
            "review_state": "PENDING",
            "review_body": "",
            "review_commit_sha": review_input.recovery_commit_sha,
        }
    )
    receipt = {
        **live,
        "implementation_reply_id": "implementation-comment-1",
        "implementation_reply_body": body,
        "implementation_head_sha": review_input.recovery_commit_sha,
    }
    progress = ImplementationReplyProgress(
        phase="submit_review",
        pull_request_id="PR_node",
        pending_review_id="PRR_pending",
        replied_thread_ids=("thread-1",),
        receipts=(receipt,),
    )
    return progress, [live]


def test_prepublication_receipt_round_trips_only_for_exact_live_identity(
    tmp_path: Path,
) -> None:
    """A new coordinator can recover the same prepared child and batch."""
    repo_root = tmp_path.resolve()
    worktree = repo_root / "build" / "writer"
    worktree.mkdir(parents=True)
    review_input = _review_input(
        repo_root=str(repo_root),
        worktree_path=str(worktree),
    )
    receipt = _recovery_receipt(review_input)

    save_prepublication_receipt(
        repo_root=repo_root,
        receipt=receipt,
        batch_nonce="4" * 32,
    )

    recovered = load_prepublication_receipt(
        repo_root=repo_root,
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        expected_remote_sha=review_input.reviewed_parent_sha,
        thread_snapshot_json=review_input.thread_snapshot_json,
    )
    assert recovered == (receipt, "4" * 32, False)
    with pytest.raises(ValueError, match="live identity"):
        load_prepublication_receipt(
            repo_root=repo_root,
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            expected_remote_sha="d" * 40,
            thread_snapshot_json=review_input.thread_snapshot_json,
        )
    with pytest.raises(ValueError, match="live identity"):
        load_prepublication_receipt(
            repo_root=repo_root,
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            expected_remote_sha=review_input.reviewed_parent_sha,
            thread_snapshot_json="[]",
        )

    remove_prepublication_receipt(
        repo_root=repo_root,
        pr_number=review_input.pr_number,
        expected_review_input_sha256=review_input.review_input_sha256,
    )
    assert (
        load_prepublication_receipt(
            repo_root=repo_root,
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            expected_remote_sha=review_input.reviewed_parent_sha,
            thread_snapshot_json=review_input.thread_snapshot_json,
        )
        is None
    )


def test_prepublication_intent_blocks_restart_until_exact_receipt_replaces_it(
    tmp_path: Path,
) -> None:
    """A crash after commit cannot appear to be an ordinary absent receipt."""
    repo_root = tmp_path.resolve()
    worktree = repo_root / "build" / "writer"
    worktree.mkdir(parents=True)
    review_input = _review_input(repo_root=str(repo_root), worktree_path=str(worktree))
    receipt = _recovery_receipt(review_input)
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "repository": review_input.repository,
        "issue_number": review_input.issue_number,
        "pr_number": review_input.pr_number,
        "worktree_path": worktree,
        "branch": review_input.branch,
        "expected_remote_sha": review_input.reviewed_parent_sha,
        "candidate_tree_sha": review_input.candidate_tree_sha,
        "add_paths": receipt.add_paths,
        "update_paths": receipt.update_paths,
        "committed_diff_sha256": review_input.committed_diff_sha256,
        "committed_diff": review_input.committed_diff,
        "failure_diagnostic": review_input.failure_diagnostic,
        "thread_snapshot_json": review_input.thread_snapshot_json,
        "content_snapshot": receipt.content_snapshot,
        "batch_nonce": "4" * 32,
    }

    save_prepublication_intent(**kwargs)
    with pytest.raises(ValueError, match="prepare intent"):
        load_prepublication_receipt(
            repo_root=repo_root,
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            expected_remote_sha=review_input.reviewed_parent_sha,
            thread_snapshot_json=review_input.thread_snapshot_json,
        )

    save_prepublication_receipt(
        repo_root=repo_root,
        receipt=receipt,
        batch_nonce="4" * 32,
    )

    assert load_prepublication_receipt(
        repo_root=repo_root,
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        expected_remote_sha=review_input.reviewed_parent_sha,
        thread_snapshot_json=review_input.thread_snapshot_json,
    ) == (receipt, "4" * 32, False)


def test_prepublication_receipt_rejects_a_competing_prepared_child(tmp_path: Path) -> None:
    """Concurrent coordinators cannot replace one durable child authority."""
    repo_root = tmp_path.resolve()
    worktree = repo_root / "build" / "writer"
    worktree.mkdir(parents=True)
    first_input = _review_input(repo_root=str(repo_root), worktree_path=str(worktree))
    second_input = replace(first_input, recovery_commit_sha="d" * 40)
    save_prepublication_receipt(
        repo_root=repo_root,
        receipt=_recovery_receipt(first_input),
        batch_nonce="4" * 32,
    )

    with pytest.raises(ValueError, match="different"):
        save_prepublication_receipt(
            repo_root=repo_root,
            receipt=_recovery_receipt(second_input),
            batch_nonce="5" * 32,
        )


def test_recovery_receipt_round_trip_keeps_exact_authority() -> None:
    """The durable receipt restores one canonical input and path manifest."""
    review_input = _review_input()
    receipt = _recovery_receipt(review_input)

    assert RemediationRecoveryReceipt.from_dict(receipt.as_dict()) == receipt
    assert receipt.review_input == review_input


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("review_input_sha256", "d" * 64),
        ("expected_remote_sha", "e" * 40),
        ("content_snapshot", {"index_sha256": "1" * 64}),
        ("add_paths", []),
        ("update_paths", ["a.py"]),
    ),
)
def test_recovery_receipt_rejects_swapped_or_incomplete_authority(
    field: str, value: object
) -> None:
    """A changed digest, parent, snapshot, or manifest invalidates the receipt."""
    raw = _recovery_receipt(_review_input()).as_dict()
    raw[field] = value

    with pytest.raises(ValueError, match="receipt identity"):
        RemediationRecoveryReceipt.from_dict(raw)


def test_recovery_receipt_rejects_canonical_input_from_another_candidate() -> None:
    """Input bytes from one candidate cannot use another candidate's digest."""
    first = _recovery_receipt(_review_input())
    second_input = _review_input(branch="other/recovery")
    raw = first.as_dict()
    raw["review_input_bytes"] = second_input.canonical_bytes.decode("utf-8")

    with pytest.raises(ValueError, match="receipt identity"):
        RemediationRecoveryReceipt.from_dict(raw)


def test_format_three_journal_round_trip_restores_exact_record() -> None:
    """Restart recovery recreates the exact input bytes and reply result."""
    review_input = _review_input()
    result = _reply_result(review_input)
    handoff = implementation_remediation_reply_handoff(
        review_input,
        result,
        "d" * 32,
    )
    assert handoff is not None
    rendered = implementation_remediation_reply_handoff_journal_entry(3010, handoff)
    assert rendered is not None
    _marker, body = rendered

    recovered = journaled_implementation_remediation_reply_handoff(
        [IssueComment(body=body, viewer_did_author=True)],
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        current_remote_head=review_input.recovery_commit_sha,
        threads=_threads(),
    )

    assert recovered is not None
    assert recovered["review_input_bytes"] == review_input.canonical_bytes.decode()
    assert recovered["review_input_sha256"] == review_input.review_input_sha256
    assert recovered["reply_result"] == result.as_dict()
    assert recovered["reply_result_sha256"] == result.digest
    assert recovered["head_sha"] == review_input.recovery_commit_sha
    assert recovered["reconciliation_only"] is False


def test_format_three_delivery_rejects_reply_mutation_after_journal() -> None:
    """Delivery posts only the reply that the immutable result digest seals."""
    review_input = _review_input()
    result = _reply_result(review_input)
    handoff = implementation_remediation_reply_handoff(review_input, result, "d" * 32)
    assert handoff is not None
    handoff["replies"]["thread-1"] = "Different valid prose."

    class NoGitHub:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"GitHub must not be called: {name}")

    status = retry_pending_implementation_reply_handoff(
        {"pending_implementation_reply_handoff": handoff},
        pr_number=review_input.pr_number,
        issue_number=review_input.issue_number,
        github=NoGitHub(),
        logger=logging.getLogger(__name__),
    )

    assert status == "invalid"


def test_format_three_successor_chain_restores_proved_partial_progress() -> None:
    """A linked successor is restart authority for its exact live reply."""
    review_input = _review_input()
    result = _reply_result(review_input)
    nonce = "d" * 32
    initial = implementation_remediation_reply_handoff(review_input, result, nonce)
    assert initial is not None
    initial_journal = implementation_remediation_reply_handoff_journal_entry(3010, initial)
    assert initial_journal is not None
    progress, live_threads = _progress(review_input, dict(result.replies)["thread-1"], nonce)
    successor = implementation_remediation_reply_handoff(
        review_input,
        result,
        nonce,
        progress=progress,
        journal_input_encoding=initial["review_input_encoding"],
        journal_input_data=initial["review_input_data"],
        journal_sequence=1,
        journal_predecessor_sha256=hashlib.sha256(initial_journal[1].encode()).hexdigest(),
    )
    assert successor is not None
    successor_journal = implementation_remediation_reply_handoff_journal_entry(3010, successor)
    assert successor_journal is not None

    recovered = journaled_implementation_remediation_reply_handoff(
        [
            IssueComment(body=initial_journal[1], viewer_did_author=True),
            IssueComment(body=successor_journal[1], viewer_did_author=True),
        ],
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        current_remote_head=review_input.recovery_commit_sha,
        threads=live_threads,
    )

    assert recovered is not None
    assert recovered["journal_sequence"] == 1
    assert recovered["progress"] == progress.as_dict()
    assert recovered["reconciliation_only"] is False


def test_format_three_refreshes_journaled_pending_progress_after_submit_crash() -> None:
    """Live COMMENTED state advances a PENDING successor after a hard crash."""
    review_input = _review_input()
    result = _reply_result(review_input)
    nonce = "d" * 32
    initial = implementation_remediation_reply_handoff(review_input, result, nonce)
    assert initial is not None
    initial_journal = implementation_remediation_reply_handoff_journal_entry(3010, initial)
    assert initial_journal is not None
    progress, live_threads = _progress(review_input, dict(result.replies)["thread-1"], nonce)
    successor = implementation_remediation_reply_handoff(
        review_input,
        result,
        nonce,
        progress=progress,
        journal_input_encoding=initial["review_input_encoding"],
        journal_input_data=initial["review_input_data"],
        journal_sequence=1,
        journal_predecessor_sha256=hashlib.sha256(initial_journal[1].encode()).hexdigest(),
    )
    assert successor is not None
    successor_journal = implementation_remediation_reply_handoff_journal_entry(3010, successor)
    assert successor_journal is not None
    live_threads[0]["comments"][-1]["review_state"] = "COMMENTED"

    recovered = journaled_implementation_remediation_reply_handoff(
        [
            IssueComment(body=initial_journal[1], viewer_did_author=True),
            IssueComment(body=successor_journal[1], viewer_did_author=True),
        ],
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        current_remote_head=review_input.recovery_commit_sha,
        threads=live_threads,
    )

    assert recovered is not None
    refreshed = ImplementationReplyProgress.from_dict(recovered["progress"])
    assert refreshed is not None
    assert refreshed.phase == "verify_submission"
    assert refreshed.pending_review_id is None
    assert refreshed.receipts[0]["comments"][-1]["review_state"] == "COMMENTED"


@pytest.mark.parametrize("review_state", ("PENDING", "COMMENTED"))
def test_format_three_recovers_an_exact_live_reply_before_progress_append(
    review_state: str,
) -> None:
    """A fresh manager derives exact batch progress after a reply-side crash."""
    review_input = _review_input()
    result = _reply_result(review_input)
    nonce = "d" * 32
    initial = implementation_remediation_reply_handoff(review_input, result, nonce)
    assert initial is not None
    journal = implementation_remediation_reply_handoff_journal_entry(3010, initial)
    assert journal is not None
    body = _implementation_reply_body(review_input, dict(result.replies)["thread-1"], nonce)
    live = _production_live_thread(_threads()[0])
    live["comments"].append(
        {
            "id": "implementation-comment-1",
            "author": "hephaestus",
            "body": body,
            "viewer_did_author": True,
            "review_id": "PRR_pending",
            "review_state": review_state,
            "review_body": "",
            "review_commit_sha": review_input.recovery_commit_sha,
        }
    )

    recovered = journaled_implementation_remediation_reply_handoff(
        [IssueComment(body=journal[1], viewer_did_author=True)],
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        current_remote_head=review_input.recovery_commit_sha,
        threads=[live],
    )

    assert recovered is not None
    progress = ImplementationReplyProgress.from_dict(recovered["progress"])
    assert progress is not None
    assert progress.replied_thread_ids == ("thread-1",)
    assert progress.pending_review_id == ("PRR_pending" if review_state == "PENDING" else None)
    assert recovered["recover_pending_review"] is True


def test_format_three_recovers_one_reply_with_rich_untouched_thread_metadata() -> None:
    """A production-shaped untouched thread stays equal to its canonical prefix."""
    threads = _threads()
    threads.append(
        {
            "id": "thread-2",
            "isResolved": False,
            "path": "b.py",
            "line": 8,
            "side": "RIGHT",
            "comments": [{"id": "comment-2", "author": "reviewer", "body": "Fix this too."}],
        }
    )
    thread_json = RemediationReviewInput.canonical_thread_snapshot(threads)
    review_input = _review_input(
        thread_snapshot_sha256=hashlib.sha256(thread_json.encode()).hexdigest(),
        thread_snapshot_json=thread_json,
    )
    result = RemediationReplyResult.create(
        review_input_sha256=review_input.review_input_sha256,
        replies={"thread-1": "First fixed.", "thread-2": "Second fixed."},
        thread_snapshot_json=thread_json,
    )
    nonce = "d" * 32
    initial = implementation_remediation_reply_handoff(review_input, result, nonce)
    assert initial is not None
    journal = implementation_remediation_reply_handoff_journal_entry(3010, initial)
    assert journal is not None
    live = [_production_live_thread(thread) for thread in threads]
    body = _implementation_reply_body(
        review_input,
        "First fixed.",
        nonce,
        thread_id="thread-1",
    )
    live[0]["comments"].append(
        {
            "id": "implementation-comment-1",
            "author": "hephaestus",
            "author_type": "Bot",
            "body": body,
            "viewer_did_author": True,
            "review_id": "PRR_pending",
            "review_state": "PENDING",
            "review_body": "",
            "review_commit_sha": review_input.recovery_commit_sha,
        }
    )

    recovered = journaled_implementation_remediation_reply_handoff(
        [IssueComment(body=journal[1], viewer_did_author=True)],
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        current_remote_head=review_input.recovery_commit_sha,
        threads=live,
    )

    assert recovered is not None
    progress = ImplementationReplyProgress.from_dict(recovered["progress"])
    assert progress is not None
    assert progress.replied_thread_ids == ("thread-1",)
    assert progress.phase == "post_replies"


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("repository", []),
        ("issue_number", "3009"),
        ("repo_root", 7),
        ("committed_diff", b"diff"),
        ("thread_snapshot_json", "\ud800"),
        ("add_paths", ["a.py", 7]),
        ("content_snapshot", [["index_sha256", 7]]),
        ("content_snapshot", [["index_sha256"]]),
    ],
)
def test_prepublication_intent_rejects_malformed_primitive_types(
    field: str,
    invalid: object,
) -> None:
    """Malformed durable values become one bounded invalid-authority error."""
    diff = "diff --git a/a.py b/a.py\n+value = 2\n"
    payload = {
        "repository": "homericintelligence/hephaestus",
        "issue_number": 3009,
        "pr_number": 3010,
        "repo_root": "/repo",
        "worktree_path": "/repo/build/writer",
        "branch": "codex/fix",
        "expected_remote_sha": "a" * 40,
        "candidate_tree_sha": "b" * 40,
        "add_paths": ["a.py"],
        "update_paths": [],
        "committed_diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "committed_diff": diff,
        "failure_diagnostic": "failed",
        "thread_snapshot_json": RemediationReviewInput.canonical_thread_snapshot(_threads()),
        "content_snapshot": [
            ["index_sha256", "1" * 64],
            ["untracked_sha256", "2" * 64],
            ["worktree_sha256", "3" * 64],
        ],
        "batch_nonce": "d" * 32,
    }
    payload[field] = invalid

    with pytest.raises(ValueError, match="schema is invalid"):
        RemediationPreparationIntent.from_dict(payload)


def test_format_three_rejects_unrelated_same_head_journals() -> None:
    """Two initial batches for one head are ambiguous and fail closed."""
    review_input = _review_input()
    result = _reply_result(review_input)
    entries: list[IssueComment] = []
    for nonce in ("d" * 32, "e" * 32):
        handoff = implementation_remediation_reply_handoff(review_input, result, nonce)
        assert handoff is not None
        journal = implementation_remediation_reply_handoff_journal_entry(3010, handoff)
        assert journal is not None
        entries.append(IssueComment(body=journal[1], viewer_did_author=True))

    with pytest.raises(ValueError, match="ambiguous"):
        journaled_implementation_remediation_reply_handoff(
            entries,
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            current_remote_head=review_input.recovery_commit_sha,
            threads=_threads(),
        )


@pytest.mark.parametrize("nested", (False, True))
def test_format_three_rejects_duplicate_json_members(nested: bool) -> None:
    """Duplicate outer and nested JSON members cannot replace authority."""
    review_input = _review_input()
    handoff = implementation_remediation_reply_handoff(
        review_input, _reply_result(review_input), "d" * 32
    )
    assert handoff is not None
    journal = implementation_remediation_reply_handoff_journal_entry(3010, handoff)
    assert journal is not None
    marker, payload_comment = journal[1].split("\n", 1)
    payload = payload_comment.removeprefix("<!-- ").removesuffix(" -->")
    if nested:
        payload = payload.replace(
            '"reply_result":{"replies":',
            '"reply_result":{"replies":{},"replies":',
            1,
        )
    else:
        payload = payload.replace('{"armed":true', '{"armed":false,"armed":true', 1)
    duplicated = f"{marker}\n<!-- {payload} -->"

    with pytest.raises(ValueError, match="duplicate"):
        journaled_implementation_remediation_reply_handoff(
            [IssueComment(body=duplicated, viewer_did_author=True)],
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            current_remote_head=review_input.recovery_commit_sha,
            threads=_threads(),
        )


def test_remediation_recovery_rejects_legacy_or_incomplete_records() -> None:
    """Formats 1 and 2 cannot act as remediation restart authority."""
    normal = implementation_reply_handoff(
        "c" * 40,
        _threads(),
        {"thread-1": "Fixed."},
        "d" * 32,
    )
    assert normal is not None
    legacy = implementation_reply_handoff_journal_entry(3010, normal)
    assert legacy is not None
    review_input = _review_input()
    with pytest.raises(ValueError, match="legacy"):
        journaled_implementation_remediation_reply_handoff(
            [IssueComment(body=legacy[1], viewer_did_author=True)],
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            current_remote_head=review_input.recovery_commit_sha,
            threads=_threads(),
        )

    marker = (
        "<!-- hephaestus-implementation-remediation-reply-handoff:"
        f"pr=3010:head={'c' * 40}:batch={'d' * 32}:seq=0 -->"
    )
    incomplete = f'{marker}\n<!-- {{"format":3,"kind":"remediation-recovery"}} -->'
    with pytest.raises(ValueError, match="schema"):
        journaled_implementation_remediation_reply_handoff(
            [IssueComment(body=incomplete, viewer_did_author=True)],
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            current_remote_head=review_input.recovery_commit_sha,
            threads=_threads(),
        )


def test_remediation_recovery_ignores_a_legacy_record_for_an_old_head() -> None:
    """An old normal journal does not hide the current format-3 record."""
    review_input = _review_input()
    normal = implementation_reply_handoff(
        "d" * 40,
        _threads(),
        {"thread-1": "Old response."},
        "e" * 32,
    )
    assert normal is not None
    legacy = implementation_reply_handoff_journal_entry(3010, normal)
    current = implementation_remediation_reply_handoff(
        review_input,
        _reply_result(review_input),
        "f" * 32,
    )
    assert legacy is not None
    assert current is not None
    rendered = implementation_remediation_reply_handoff_journal_entry(3010, current)
    assert rendered is not None

    recovered = journaled_implementation_remediation_reply_handoff(
        [
            IssueComment(body=legacy[1], viewer_did_author=True),
            IssueComment(body=rendered[1], viewer_did_author=True),
        ],
        repository=review_input.repository,
        issue_number=review_input.issue_number,
        pr_number=review_input.pr_number,
        branch=review_input.branch,
        current_remote_head=review_input.recovery_commit_sha,
        threads=_threads(),
    )

    assert recovered is not None
    assert recovered["review_input_sha256"] == review_input.review_input_sha256


def test_format_three_rejects_expansion_and_identity_swap() -> None:
    """Compressed data and external identities are validated again on recovery."""
    review_input = _review_input()
    handoff = implementation_remediation_reply_handoff(
        review_input,
        _reply_result(review_input),
        "d" * 32,
    )
    assert handoff is not None
    rendered = implementation_remediation_reply_handoff_journal_entry(3010, handoff)
    assert rendered is not None
    _marker, body = rendered

    with pytest.raises(ValueError, match="repository"):
        journaled_implementation_remediation_reply_handoff(
            [IssueComment(body=body, viewer_did_author=True)],
            repository="homericintelligence/other",
            issue_number=3009,
            pr_number=3010,
            branch=review_input.branch,
            current_remote_head=review_input.recovery_commit_sha,
            threads=_threads(),
        )

    marker, payload_comment = body.split("\n", 1)
    payload = json.loads(payload_comment.removeprefix("<!-- ").removesuffix(" -->"))
    payload["review_input_encoding"] = "unknown"
    changed = f"{marker}\n<!-- {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"
    with pytest.raises(ValueError, match="encoding"):
        journaled_implementation_remediation_reply_handoff(
            [IssueComment(body=changed, viewer_did_author=True)],
            repository=review_input.repository,
            issue_number=3009,
            pr_number=3010,
            branch=review_input.branch,
            current_remote_head=review_input.recovery_commit_sha,
            threads=_threads(),
        )


@pytest.mark.parametrize(
    "mutation",
    ("thread", "resolution", "path", "line", "comment", "author", "body"),
)
def test_format_three_rejects_any_thread_snapshot_mutation(mutation: str) -> None:
    """Each reply stays bound to the complete source-review conversation."""
    review_input = _review_input()
    handoff = implementation_remediation_reply_handoff(
        review_input,
        _reply_result(review_input),
        "d" * 32,
    )
    assert handoff is not None
    rendered = implementation_remediation_reply_handoff_journal_entry(3010, handoff)
    assert rendered is not None
    changed = deepcopy(_threads())
    if mutation == "thread":
        changed[0]["id"] = "thread-2"
    elif mutation == "resolution":
        changed[0]["isResolved"] = True
    elif mutation == "path":
        changed[0]["path"] = "b.py"
    elif mutation == "line":
        changed[0]["line"] = 5
    elif mutation == "comment":
        changed[0]["comments"].append(
            {"id": "comment-2", "author": "reviewer", "body": "More detail."}
        )
    elif mutation == "author":
        changed[0]["comments"][0]["author"] = "other-reviewer"
    else:
        changed[0]["comments"][0]["body"] = "Different request."

    with pytest.raises(ValueError, match="thread snapshot"):
        journaled_implementation_remediation_reply_handoff(
            [IssueComment(body=rendered[1], viewer_did_author=True)],
            repository=review_input.repository,
            issue_number=review_input.issue_number,
            pr_number=review_input.pr_number,
            branch=review_input.branch,
            current_remote_head=review_input.recovery_commit_sha,
            threads=changed,
        )


def test_normal_journal_format_two_is_unchanged() -> None:
    """The remediation API does not change the normal format-2 writer."""
    normal = implementation_reply_handoff(
        "c" * 40,
        _threads(),
        {"thread-1": "Fixed."},
        "d" * 32,
    )
    assert normal is not None
    rendered = implementation_reply_handoff_journal_entry(3010, normal)
    assert rendered is not None
    payload = json.loads(rendered[1].split("\n", 1)[1].removeprefix("<!-- ").removesuffix(" -->"))
    assert payload["format"] == 2
    assert "review_input" not in payload
