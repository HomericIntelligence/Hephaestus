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

from hephaestus.automation.models import DEFAULT_STATE_DIR
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


def _pretest_payload(tmp_path: Path) -> dict[str, Any]:
    """Return one complete successful candidate payload."""
    receipt = {
        "schema_version": 1,
        "repository": "example/project",
        "repository_identity": "example/project:0123456789abcdef",
        "ownership_key": "example/project:0123456789abcdef:9:impl",
        "item_number": 9,
        "lane": "impl",
        "path": str(tmp_path / "build/writer"),
        "revision": "a" * 40,
        "generation": 6,
        "detached": False,
        "branch": "writer-branch",
        "obligations": [],
    }
    diff = "diff --git a/a.py b/a.py\n+value = 2\n"
    return {
        "phase": "ready",
        "repository": "example/project",
        "issue_number": 9,
        "pr_number": 10,
        "repo_root": str(tmp_path),
        "worktree_path": receipt["path"],
        "branch": "writer-branch",
        "expected_remote_sha": "a" * 40,
        "source_receipt": receipt,
        "source_receipt_sha256": hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "source_repository_identity": receipt["repository_identity"],
        "source_ownership_key": receipt["ownership_key"],
        "source_generation": 6,
        "candidate_tree_sha": "b" * 40,
        "add_paths": [],
        "update_paths": ["a.py"],
        "diff": diff,
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "content_snapshot": [
            [key, "c" * 64] for key in ("index_sha256", "worktree_sha256", "untracked_sha256")
        ],
        "thread_snapshot_json": RemediationReviewInput.canonical_thread_snapshot(_threads()),
        "batch_nonce": "d" * 32,
        "candidate_sequence": 1,
        "successful_job_id": "job-1",
        "successful_result_sha256": "e" * 64,
        "addressed_replies": {"thread-1": "Corrected the test."},
        "consumed_head": None,
    }


def test_pretest_store_preserves_candidate_through_compare_and_swap(tmp_path: Path) -> None:
    """Invalidation keeps evidence and permits only the next bound candidate."""
    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    digest = store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    assert store.load_pretest_candidate(repo_root=tmp_path, pr_number=10) == ready
    assert store.save_pretest_candidate(repo_root=tmp_path, candidate=ready) == digest
    invalidated = replace(ready, phase="invalidated")
    invalid_digest = store.save_pretest_candidate(
        repo_root=tmp_path, candidate=invalidated, expected_digest=digest
    )
    with pytest.raises(ValueError):
        store.save_pretest_candidate(repo_root=tmp_path, candidate=ready, expected_digest=digest)
    refreshed = replace(
        ready, candidate_sequence=2, successful_job_id="job-2", successful_result_sha256="f" * 64
    )
    store.save_pretest_candidate(
        repo_root=tmp_path,
        candidate=refreshed,
        expected_digest=invalid_digest,
        previous_successful_job_id="job-1",
    )
    assert store.load_pretest_candidate(repo_root=tmp_path, pr_number=10) == refreshed
    history = tmp_path / DEFAULT_STATE_DIR / "remediation-prepublication"
    assert (history / f"pr-10-pretest-{digest}.json").read_bytes() == ready.canonical_bytes
    assert not (history / "pr-10.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_sequence", True),
        ("source_generation", 0),
        ("phase", "failed"),
        ("successful_result_sha256", "invalid"),
        ("source_receipt_sha256", "0" * 64),
        ("update_paths", ["../outside"]),
        ("addressed_replies", {"other": "Done."}),
        ("consumed_head", "a" * 40),
        ("source_ownership_key", "different"),
    ],
)
def test_pretest_candidate_rejects_invalid_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    """Candidate fields cannot replace source, path or reply evidence."""
    from hephaestus.automation import remediation_prepublication as store

    payload = _pretest_payload(tmp_path)
    payload[field] = value
    with pytest.raises(ValueError):
        store.RemediationPretestCandidate.from_dict(payload)


def test_pretest_candidate_rejects_unknown_fields(tmp_path: Path) -> None:
    """A failed-result field cannot enter the successful record."""
    from hephaestus.automation import remediation_prepublication as store

    payload = _pretest_payload(tmp_path)
    payload["failure_diagnostic"] = "not successful evidence"
    with pytest.raises(ValueError):
        store.RemediationPretestCandidate.from_dict(payload)


def test_pretest_store_rejects_missing_lineage_and_retains_consumed(tmp_path: Path) -> None:
    """Phase changes need exact evidence and consumed records stay terminal."""
    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    digest = store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    invalid = replace(ready, phase="invalidated")
    invalid_digest = store.save_pretest_candidate(
        repo_root=tmp_path, candidate=invalid, expected_digest=digest
    )
    next_ready = replace(
        ready, candidate_sequence=2, successful_job_id="new-job", successful_result_sha256="f" * 64
    )
    with pytest.raises(ValueError):
        store.save_pretest_candidate(
            repo_root=tmp_path, candidate=next_ready, expected_digest=invalid_digest
        )
    assert store.load_pretest_candidate(repo_root=tmp_path, pr_number=10) == invalid
    next_digest = store.save_pretest_candidate(
        repo_root=tmp_path,
        candidate=next_ready,
        expected_digest=invalid_digest,
        previous_successful_job_id=ready.successful_job_id,
    )
    consumed = replace(next_ready, phase="consumed", consumed_head="f" * 40)
    consumed_digest = store.save_pretest_candidate(
        repo_root=tmp_path, candidate=consumed, expected_digest=next_digest
    )
    with pytest.raises(ValueError):
        store.save_pretest_candidate(
            repo_root=tmp_path, candidate=next_ready, expected_digest=consumed_digest
        )
    assert store.load_pretest_candidate(repo_root=tmp_path, pr_number=10) == consumed


@pytest.mark.parametrize(
    "kind", ["symlink", "fifo", "malformed", "version", "unknown", "whitespace"]
)
def test_pretest_store_refuses_invalid_existing_records(tmp_path: Path, kind: str) -> None:
    """Invalid evidence remains intact and cannot become recovery authority."""
    import os

    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    path = tmp_path / DEFAULT_STATE_DIR / "remediation-prepublication" / "pr-10-pretest.json"
    path.unlink()
    if kind == "symlink":
        target = tmp_path / "outside.json"
        target.write_bytes(ready.canonical_bytes)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "malformed":
        path.write_text("{")
    elif kind == "whitespace":
        path.write_bytes(ready.canonical_bytes + b"\n")
    else:
        record = json.loads(ready.canonical_bytes)
        if kind == "version":
            record[0] = True
        else:
            record[2]["unexpected"] = True
        path.write_text(json.dumps(record))
    before = path.lstat()
    with pytest.raises((ValueError, OSError)):
        store.load_pretest_candidate(repo_root=tmp_path, pr_number=10)
    with pytest.raises((ValueError, OSError)):
        store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    assert path.lstat().st_ino == before.st_ino


def test_pretest_store_refuses_symlink_directory(tmp_path: Path) -> None:
    """The private store cannot follow a redirected path component."""
    from hephaestus.automation import remediation_prepublication as store

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "build").symlink_to(outside, target_is_directory=True)
    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    with pytest.raises(OSError):
        store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    assert not list(outside.iterdir())


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_nonce", "1" * 32),
        ("thread_snapshot_json", "changed"),
        ("candidate_sequence", 3),
        ("source_generation", 7),
    ],
)
def test_pretest_store_refuses_changed_refresh_lineage(
    tmp_path: Path, field: str, value: object
) -> None:
    """A new batch or source cannot replace an invalidated candidate."""
    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    digest = store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    invalid = replace(ready, phase="invalidated")
    invalid_digest = store.save_pretest_candidate(
        repo_root=tmp_path, candidate=invalid, expected_digest=digest
    )
    payload = ready.as_dict()
    payload.update(
        candidate_sequence=2, successful_job_id="next", successful_result_sha256="f" * 64
    )
    payload[field] = value
    with pytest.raises(ValueError):
        refreshed = store.RemediationPretestCandidate.from_dict(payload)
        store.save_pretest_candidate(
            repo_root=tmp_path,
            candidate=refreshed,
            expected_digest=invalid_digest,
            previous_successful_job_id=ready.successful_job_id,
        )
    assert store.load_pretest_candidate(repo_root=tmp_path, pr_number=10) == invalid


def test_pretest_store_serializes_competing_first_writes(tmp_path: Path) -> None:
    """Only one distinct candidate can win the initial compare-and-swap."""
    from concurrent.futures import ThreadPoolExecutor

    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    other = replace(ready, successful_job_id="other")

    def save(candidate: store.RemediationPretestCandidate) -> str | None:
        try:
            return store.save_pretest_candidate(repo_root=tmp_path, candidate=candidate)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, (ready, other)))
    assert sum(value is not None for value in results) == 1
    saved = store.load_pretest_candidate(repo_root=tmp_path, pr_number=10)
    assert saved in (ready, other)
    assert saved is not None and saved.digest in results


def test_pretest_store_archives_before_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement failure keeps both original authority and archived bytes."""
    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    digest = store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr("hephaestus.automation.remediation_prepublication.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.save_pretest_candidate(
            repo_root=tmp_path,
            candidate=replace(ready, phase="invalidated"),
            expected_digest=digest,
        )
    assert store.load_pretest_candidate(repo_root=tmp_path, pr_number=10) == ready
    directory = tmp_path / DEFAULT_STATE_DIR / "remediation-prepublication"
    assert (directory / f"pr-10-pretest-{digest}.json").read_bytes() == ready.canonical_bytes
    assert list(directory.glob("*.next"))


def test_pretest_source_digest_uses_complete_canonical_receipt(tmp_path: Path) -> None:
    """Whitespace in the source file does not define its canonical digest."""
    from hephaestus.automation import remediation_prepublication as store

    payload = _pretest_payload(tmp_path)
    payload["source_receipt"]["repository"] = "project"
    receipt = payload["source_receipt"]
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    payload["source_receipt_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    candidate = store.RemediationPretestCandidate.from_dict(payload)
    assert store.canonical_source_receipt_json(candidate.source_receipt) == canonical
    assert store.source_receipt_digest(candidate.source_receipt) == payload["source_receipt_sha256"]
    payload["source_receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, indent=2).encode()
    ).hexdigest()
    with pytest.raises(ValueError):
        store.RemediationPretestCandidate.from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("detached", 0),
        ("item_number", "9"),
        ("branch", "other"),
        ("revision", "b" * 40),
        ("lane", "review"),
    ],
)
def test_pretest_candidate_rejects_coerced_or_changed_source(
    tmp_path: Path, field: str, value: object
) -> None:
    """A matching recomputed hash cannot replace the source identity."""
    from hephaestus.automation import remediation_prepublication as store

    payload = _pretest_payload(tmp_path)
    payload["source_receipt"][field] = value
    payload["source_receipt_sha256"] = hashlib.sha256(
        json.dumps(payload["source_receipt"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ValueError):
        store.RemediationPretestCandidate.from_dict(payload)


def test_pretest_store_rejects_oversized_or_missing_compare_record(tmp_path: Path) -> None:
    """A size overflow or missing predecessor cannot create a new authority."""
    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    with pytest.raises(ValueError):
        store.save_pretest_candidate(repo_root=tmp_path, candidate=ready, expected_digest="f" * 64)
    store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    path = tmp_path / DEFAULT_STATE_DIR / "remediation-prepublication" / "pr-10-pretest.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        store.load_pretest_candidate(repo_root=tmp_path, pr_number=10)
    assert path.stat().st_size == 1024 * 1024 + 1


def test_pretest_refresh_accepts_equal_result_digest_from_new_job(tmp_path: Path) -> None:
    """Distinct successful jobs can return the same result while lineage stays exact."""
    from hephaestus.automation import remediation_prepublication as store

    ready = store.RemediationPretestCandidate.from_dict(_pretest_payload(tmp_path))
    first = store.save_pretest_candidate(repo_root=tmp_path, candidate=ready)
    invalid = replace(ready, phase="invalidated")
    second = store.save_pretest_candidate(
        repo_root=tmp_path, candidate=invalid, expected_digest=first
    )
    refreshed = replace(ready, candidate_sequence=2, successful_job_id="next-job")
    store.save_pretest_candidate(
        repo_root=tmp_path,
        candidate=refreshed,
        expected_digest=second,
        previous_successful_job_id=ready.successful_job_id,
    )
    assert store.load_pretest_candidate(repo_root=tmp_path, pr_number=10) == refreshed


def _disjoint_legacy_body() -> str:
    """Render an ordinary journal for a different thread at the current head."""
    old = _threads()
    old[0]["id"] = "old-thread"
    handoff = implementation_reply_handoff("c" * 40, old, {"old-thread": "Fixed."}, "d" * 32)
    assert handoff is not None
    entry = implementation_reply_handoff_journal_entry(3010, handoff)
    assert entry is not None
    return entry[1]


def _read_legacy_batch(body: str, threads: list[dict[str, Any]] | None = None) -> object:
    """Read an ordinary journal through the public remediation selector."""
    inputs = _review_input()
    return journaled_implementation_remediation_reply_handoff(
        [IssueComment(body=body, viewer_did_author=True)],
        repository=inputs.repository,
        issue_number=inputs.issue_number,
        pr_number=inputs.pr_number,
        branch=inputs.branch,
        current_remote_head=inputs.recovery_commit_sha,
        threads=_threads() if threads is None else threads,
    )


def test_disjoint_current_head_legacy_batch_is_not_recovery_authority() -> None:
    """An ordinary old batch cannot block a different live thread batch."""
    assert _read_legacy_batch(_disjoint_legacy_body()) is None


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("position", ["only", "before", "after"])
def test_valid_disjoint_legacy_keeps_exact_current_format_three(
    version: int, position: str
) -> None:
    """Legacy selection does not alter a current format-three recovery result."""
    marker, encoded = _disjoint_legacy_body().split("\n", 1)
    payload = json.loads(encoded.removeprefix("<!-- ").removesuffix(" -->"))
    payload["format"] = version
    if version == 1:
        del payload["armed"]
    legacy = IssueComment(
        body=marker + "\n<!-- " + json.dumps(payload) + " -->", viewer_did_author=True
    )
    inputs = _review_input()
    handoff = implementation_remediation_reply_handoff(inputs, _reply_result(inputs), "e" * 32)
    assert handoff is not None
    rendered = implementation_remediation_reply_handoff_journal_entry(3010, handoff)
    assert rendered is not None
    current = IssueComment(body=rendered[1], viewer_did_author=True)
    kwargs: dict[str, Any] = {
        "repository": inputs.repository,
        "issue_number": inputs.issue_number,
        "pr_number": inputs.pr_number,
        "branch": inputs.branch,
        "current_remote_head": inputs.recovery_commit_sha,
        "threads": _threads(),
    }
    expected = journaled_implementation_remediation_reply_handoff([current], **kwargs)
    comments = (
        [legacy]
        if position == "only"
        else [legacy, current]
        if position == "before"
        else [current, legacy]
    )
    actual = journaled_implementation_remediation_reply_handoff(comments, **kwargs)
    assert actual == (None if position == "only" else expected)


@pytest.mark.parametrize(
    "case",
    [
        "bool_format",
        "unknown_format",
        "bool_pr",
        "wrong_pr",
        "wrong_head",
        "wrong_batch",
        "unarmed",
        "extra",
        "fingerprint",
        "empty_replies",
        "blank_id",
        "spaced_id",
        "internal_space_id",
        "normalized_duplicate",
        "blank_reply",
        "nonstring_reply",
        "large_reply",
        "duplicate_json",
        "progress_invalid",
        "progress_foreign",
        "progress_receipt_foreign",
        "progress_extra",
    ],
)
def test_malformed_disjoint_legacy_cannot_be_ignored(case: str) -> None:
    """Invalid legacy evidence must stop selection even for different IDs."""
    marker, encoded = _disjoint_legacy_body().split("\n", 1)
    payload = json.loads(encoded.removeprefix("<!-- ").removesuffix(" -->"))
    edits: dict[str, tuple[str, object]] = {
        "bool_format": ("format", True),
        "unknown_format": ("format", 4),
        "bool_pr": ("pr_number", True),
        "wrong_pr": ("pr_number", 3011),
        "wrong_head": ("head_sha", "b" * 40),
        "wrong_batch": ("batch_nonce", "e" * 32),
        "unarmed": ("armed", False),
        "extra": ("unexpected", 1),
        "fingerprint": ("thread_snapshot_sha256", "bad"),
        "empty_replies": ("replies", {}),
        "blank_id": ("replies", {"": "Fixed."}),
        "spaced_id": ("replies", {" old-thread ": "Fixed."}),
        "internal_space_id": ("replies", {"old thread": "Fixed."}),
        "normalized_duplicate": ("replies", {"old-thread": "Fixed.", " old-thread": "Fixed."}),
        "blank_reply": ("replies", {"old-thread": " "}),
        "nonstring_reply": ("replies", {"old-thread": 1}),
        "large_reply": ("replies", {"old-thread": "x" * 4001}),
        "progress_invalid": ("progress", {}),
        "progress_foreign": (
            "progress",
            ImplementationReplyProgress(
                phase="verify_reply", pull_request_id="pr", active_thread_id="foreign"
            ).as_dict(),
        ),
        "progress_receipt_foreign": (
            "progress",
            ImplementationReplyProgress(
                phase="post_replies",
                pull_request_id="pr",
                replied_thread_ids=("old-thread",),
                receipts=({"id": "old-thread", "thread_id": "foreign"},),
            ).as_dict(),
        ),
        "progress_extra": (
            "progress",
            {
                **ImplementationReplyProgress(
                    phase="create_review", pull_request_id="pr"
                ).as_dict(),
                "unexpected": True,
            },
        ),
    }
    if case in edits:
        key, value = edits[case]
        payload[key] = value
    body = json.dumps(payload)
    if case == "duplicate_json":
        body = body.replace('"format": 2', '"format": 2, "format": 2')
    with pytest.raises(ValueError):
        _read_legacy_batch(marker + "\n<!-- " + body + " -->")


@pytest.mark.parametrize(
    "case",
    [
        "overlap",
        "changed_body",
        "empty",
        "duplicate",
        "missing_comments",
        "missing_body",
        "blank_id",
        "spaced_id",
    ],
)
def test_legacy_disjointness_requires_complete_nonoverlapping_threads(case: str) -> None:
    """Changed or incomplete live snapshots cannot hide legacy authority."""
    threads = _threads()
    if case in {"overlap", "changed_body"}:
        old = deepcopy(threads[0])
        old["id"] = "old-thread"
        if case == "changed_body":
            old["comments"][0]["body"] = "Changed source comment."
            threads = [old]
        else:
            threads.append(old)
    elif case == "empty":
        threads = []
    elif case == "duplicate":
        threads.append(deepcopy(threads[0]))
    elif case == "missing_comments":
        del threads[0]["comments"]
    elif case == "missing_body":
        del threads[0]["comments"][0]["body"]
    else:
        threads[0]["id"] = " " if case == "blank_id" else " thread-1 "
    with pytest.raises(ValueError):
        _read_legacy_batch(_disjoint_legacy_body(), threads)


def test_disjoint_legacy_progress_is_retained_without_mutation() -> None:
    """Valid progress stays in its original journal while selection ignores it."""
    marker, encoded = _disjoint_legacy_body().split("\n", 1)
    payload = json.loads(encoded.removeprefix("<!-- ").removesuffix(" -->"))
    payload["progress"] = ImplementationReplyProgress(
        phase="post_replies",
        pull_request_id="pr",
        replied_thread_ids=("old-thread",),
        receipts=({"id": "old-thread"},),
    ).as_dict()
    body = marker + "\n<!-- " + json.dumps(payload) + " -->"
    assert _read_legacy_batch(body) is None
    assert body == marker + "\n<!-- " + json.dumps(payload) + " -->"


@pytest.mark.parametrize("alternate", [[], {}, None, 1, "", " old-thread", "old thread"])
def test_legacy_progress_rejects_invalid_alternate_thread_id(alternate: object) -> None:
    """An invalid alternate receipt ID raises a controlled validation error."""
    marker, encoded = _disjoint_legacy_body().split("\n", 1)
    payload = json.loads(encoded.removeprefix("<!-- ").removesuffix(" -->"))
    payload["progress"] = ImplementationReplyProgress(
        phase="post_replies",
        pull_request_id="pr",
        replied_thread_ids=("old-thread",),
        receipts=({"id": "old-thread", "thread_id": alternate},),
    ).as_dict()
    body = marker + "\n<!-- " + json.dumps(payload) + " -->"
    with pytest.raises(ValueError, match="legacy reply journal progress is invalid"):
        _read_legacy_batch(body)
