"""Tests for the closed PR 3006 source-review bootstrap grant."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from hephaestus.automation.host_verification_bootstrap import (
    BOOTSTRAP_MANIFEST,
    BOOTSTRAP_MARKER,
    BootstrapGrantError,
    authenticate_bootstrap_grant,
    parse_status_manifest,
    revalidate_bootstrap_proof,
)
from hephaestus.automation.review_journal import IssueComment


def _comment(**changes: object) -> IssueComment:
    grant: dict[str, object] = {
        "repository": "HomericIntelligence/Hephaestus",
        "issue": 2701,
        "pr": 3006,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "boundary": "linux-pyxis-enroot",
        "state": "approved",
        "manifest": [{"status": status, "path": path} for status, path in BOOTSTRAP_MANIFEST],
    }
    grant.update(changes)
    return IssueComment(
        body=BOOTSTRAP_MARKER + "\n" + json.dumps(grant),
        author_login="operator",
        author_association="MEMBER",
        viewer_did_author=True,
        database_id=123,
    )


def _authenticate(comments: list[IssueComment]) -> object:
    return authenticate_bootstrap_grant(
        comments,
        comment_id=123,
        repository="HomericIntelligence/Hephaestus",
        issue=2701,
        pr=3006,
        head_sha="a" * 40,
        base_sha="b" * 40,
        manifest=BOOTSTRAP_MANIFEST,
    )


def test_exact_grant_creates_process_local_proof() -> None:
    """The authenticated comment permits only its exact reviewed candidate."""
    proof = _authenticate([_comment()])
    assert revalidate_bootstrap_proof(proof, [_comment()]) is True
    assert revalidate_bootstrap_proof(proof, [_comment(state="revoked")]) is False
    assert revalidate_bootstrap_proof(proof, []) is False
    assert len(BOOTSTRAP_MANIFEST) == 30
    assert all("host_coverage.py" not in path for _, path in BOOTSTRAP_MANIFEST)


@pytest.mark.parametrize(
    "change",
    [
        {"state": "revoked"},
        {"head_sha": "c" * 40},
        {"base_sha": "c" * 40},
        {"repository": "other/repo"},
        {"issue": 3007},
        {"pr": 3007},
        {"boundary": "other"},
        {"extra": True},
        {"manifest": []},
        {"issue": True},
    ],
)
def test_invalid_grants_fail_closed(change: dict[str, object]) -> None:
    """A grant cannot change the reviewed target or mandatory operation map."""
    with pytest.raises(BootstrapGrantError):
        _authenticate([_comment(**change)])


@pytest.mark.parametrize(
    "change",
    [
        {"viewer_did_author": False},
        {"author_association": "CONTRIBUTOR"},
        {"author_login": ""},
        {"database_id": 124},
    ],
)
def test_foreign_or_unverified_comment_is_rejected(change: dict[str, Any]) -> None:
    """The selected comment must belong to the authenticated operator."""
    with pytest.raises(BootstrapGrantError):
        _authenticate([replace(_comment(), **change)])


def test_duplicate_comment_identity_is_rejected() -> None:
    """Duplicate selected records do not establish one grant."""
    with pytest.raises(BootstrapGrantError):
        _authenticate([_comment(), _comment()])


@pytest.mark.parametrize("raw", ["M\0a.py\0M\0a.py\0", "R100\0old\0new\0", "M\0a.py", "X\0a.py\0"])
def test_invalid_status_records_are_rejected(raw: str) -> None:
    """Status parsing retains exact paths and rejects ambiguous records."""
    with pytest.raises(BootstrapGrantError):
        parse_status_manifest(raw)


def test_no_rename_status_parser_retains_deletions() -> None:
    """The worker reports deletions so the grant policy can reject them."""
    assert parse_status_manifest("D\0old.py\0A\0new.py\0") == (("A", "new.py"), ("D", "old.py"))


def test_reconstructed_proof_is_not_restart_authority() -> None:
    """Copied fields cannot recreate the live process proof."""
    from hephaestus.automation.host_verification_bootstrap import BootstrapProof

    proof = _authenticate([_comment()])
    assert isinstance(proof, BootstrapProof)
    assert not revalidate_bootstrap_proof(replace(proof), [_comment()])


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "operation"])
def test_exact_manifest_rejects_scope_drift(mutation: str) -> None:
    """All 30 approved operations are mandatory and exclusive."""
    rows = [{"status": s, "path": p} for s, p in BOOTSTRAP_MANIFEST]
    if mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        rows.append({"status": "M", "path": "hephaestus/automation/host_coverage.py"})
    elif mutation == "duplicate":
        rows[-1] = rows[0]
    else:
        rows[0]["status"] = "D"
    with pytest.raises(BootstrapGrantError):
        _authenticate([_comment(manifest=rows)])


@pytest.mark.parametrize(
    "body",
    [
        "prefix" + BOOTSTRAP_MARKER + "\n{}",
        BOOTSTRAP_MARKER + "\n{",
        BOOTSTRAP_MARKER + '\n{"state":"approved","state":"revoked"}',
        BOOTSTRAP_MARKER + "\n[]",
        BOOTSTRAP_MARKER + "\n" + " " * 33_000,
    ],
)
def test_noncanonical_or_unbounded_comment_is_rejected(body: str) -> None:
    """The exact top-level marker and one bounded closed object are required."""
    with pytest.raises(BootstrapGrantError):
        _authenticate([replace(_comment(), body=body)])


@pytest.mark.parametrize(
    "mutation", ["valid", "head", "closed", "armed", "readback", "labels", "label_read"]
)
def test_revocation_preserves_exact_open_unarmed_guard(mutation: str) -> None:
    """Only an exact open PR can receive the replacement NOGO label."""
    from unittest.mock import MagicMock

    from hephaestus.automation.host_verification_bootstrap import revoke_bootstrap_go

    github = MagicMock()
    state: dict[str, object] = {
        "state": "OPEN",
        "id": "PR_3006",
        "headRefOid": "a" * 40,
        "autoMergeRequest": None,
    }
    if mutation == "head":
        state["headRefOid"] = "c" * 40
    elif mutation == "closed":
        state["state"] = "CLOSED"
    elif mutation == "armed":
        state["autoMergeRequest"] = {}
    readback = dict(state)
    if mutation == "readback":
        readback["id"] = "OTHER_PR"
    github.gh_pr_state.side_effect = [state, readback]
    github.pr_has_implementation_state_label.return_value = (mutation == "labels", True)
    if mutation == "label_read":
        github.pr_has_implementation_state_label.side_effect = RuntimeError("read failed")
    assert revoke_bootstrap_go(github, pr=3006, head_sha="a" * 40) is (mutation == "valid")
    assert github.mark_pr_implementation_no_go.call_count == (
        0 if mutation in {"head", "closed", "armed"} else 1
    )
    github.merge_pr_if_head.assert_not_called()


@pytest.mark.parametrize("revoked", [False, True])
def test_go_write_requires_fresh_bootstrap_grant(revoked: bool) -> None:
    """The source-review exception cannot survive revocation before GO."""
    from unittest.mock import MagicMock

    from hephaestus.automation.pipeline.routing import Disposition, StageName
    from hephaestus.automation.pipeline.stages.base import StageOutcome
    from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

    item = WorkItem(
        repo="Hephaestus", issue=2701, pr=3006, kind=ItemKind.ISSUE, stage=StageName.PR_REVIEW
    )
    item.payload.update(
        host_verification_bootstrap_proof=_authenticate([_comment()]),
        reviewed_pr_head_sha="a" * 40,
        reviewed_pr_base_sha="b" * 40,
    )
    github = MagicMock()
    github._repo_slug = "HomericIntelligence/Hephaestus"
    github.issue_comments.return_value = [_comment(state="revoked" if revoked else "approved")]
    github.list_unresolved_review_threads.return_value = []
    github.gh_pr_state.return_value = {
        "state": "OPEN",
        "id": "PR_3006",
        "headRefOid": "a" * 40,
        "autoMergeRequest": None,
    }
    github.pr_has_implementation_state_label.return_value = (not revoked, revoked)
    result = PrReviewStage().write_go(item, github)
    assert isinstance(result, StageOutcome)
    assert result.disposition == (Disposition.FINISH_FAIL if revoked else Disposition.ADVANCE)
    assert github.mark_pr_implementation_go.call_count == (0 if revoked else 1)
    assert github.mark_pr_implementation_no_go.call_count == (1 if revoked else 0)


def test_merge_request_rejects_copied_bootstrap_proof() -> None:
    """A typed request cannot transport a reconstructed proof."""
    import threading
    import time

    from hephaestus.automation.host_verification_bootstrap import BootstrapProof
    from hephaestus.automation.pipeline.github_jobs import RunMergeWaitCycleRequest

    proof = _authenticate([_comment()])
    assert isinstance(proof, BootstrapProof)
    with pytest.raises(ValueError, match="bootstrap proof"):
        RunMergeWaitCycleRequest(
            pr_number=3006,
            issue_number=2701,
            reviewed_head_sha="a" * 40,
            proof_generation=1,
            declined_readiness_fingerprint=None,
            deadline_s=time.monotonic() + 30,
            cancellation=threading.Event(),
            bootstrap_proof=replace(proof),
        )


def test_review_round_clears_bootstrap_authority_and_context() -> None:
    """A later review round cannot inherit a former round's bootstrap proof."""
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.stages.pr_review_threads import _clear_round_review_state
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

    item = WorkItem(
        repo="Hephaestus", issue=2701, pr=3006, kind=ItemKind.ISSUE, stage=StageName.PR_REVIEW
    )
    keys = {
        "host_verification_bootstrap_proof": _authenticate([_comment()]),
        "host_verification_bootstrap_json": "{}",
        "review_status_manifest": BOOTSTRAP_MANIFEST,
    }
    item.payload.update(keys)
    _clear_round_review_state(item)
    assert not set(keys).intersection(item.payload)
