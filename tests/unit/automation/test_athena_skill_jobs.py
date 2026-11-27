"""Behavior tests for the session-id-first learn request (issue #3518)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceKind
from hephaestus.automation.athena_skill_jobs import (
    LearningEvidenceError,
    LearnSessionRequest,
    require_source_evidence,
    session_learn_delivery,
)

_SOURCE_EVIDENCE_REASON = "learning_deferred:source_evidence_required"


def _request(tmp_path: Path, **kwargs: object) -> LearnSessionRequest:
    """Build one session-id-first learn request with controlled overrides."""
    values: dict[str, object] = {
        "session_id": "opaque-session",
        "agent": "claude",
        "cwd": tmp_path,
        "prompt": "$athena:learn",
    }
    values.update(kwargs)
    return LearnSessionRequest(**values)  # type: ignore[arg-type]


def _source_binding(tmp_path: Path) -> WorkspaceBinding:
    """Build one valid source workspace binding."""
    return WorkspaceBinding(
        kind=WorkspaceKind.SOURCE,
        cwd=tmp_path,
        reusable_root=tmp_path.parent,
        repository="owner/repo",
        ownership_key="owner/repo#9",
        item_number=9,
        lane=SourceLane.REVIEW,
        revision="deadbeef",
        generation=1,
        detached=True,
    )


def test_bare_session_id_has_no_source_evidence(tmp_path: Path) -> None:
    """A session id alone cannot satisfy the source-evidence boundary."""
    request = _request(tmp_path)
    assert request.source_evidence is None

    with pytest.raises(LearningEvidenceError) as caught:
        require_source_evidence(request)
    assert caught.value.failure_kind == _SOURCE_EVIDENCE_REASON
    assert caught.value.recovery


@pytest.mark.parametrize(
    "binding",
    [None, "session-only"],
    ids=["no-workspace", "session-only-workspace"],
)
def test_partial_evidence_still_fails_closed(tmp_path: Path, binding: str | None) -> None:
    """Repo and issue without a source binding do not satisfy the boundary."""
    workspace = None if binding is None else WorkspaceBinding.session_only(tmp_path)
    request = _request(tmp_path, repo="owner/repo", issue=9, workspace=workspace)
    with pytest.raises(LearningEvidenceError) as caught:
        require_source_evidence(request)
    assert caught.value.failure_kind == _SOURCE_EVIDENCE_REASON


def test_bound_request_passes_evidence_check(tmp_path: Path) -> None:
    """A complete source binding satisfies the boundary without delivery here."""
    request = _request(
        tmp_path,
        repo="owner/repo",
        issue=9,
        workspace=_source_binding(tmp_path),
    )
    require_source_evidence(request)
    assert request.source_evidence is not None


def test_delivery_state_exposes_explicit_deferral(tmp_path: Path) -> None:
    """The entry returns a named state and reason instead of silently skipping."""
    state = session_learn_delivery(_request(tmp_path))
    assert state.state == "deferred"
    assert state.reason == _SOURCE_EVIDENCE_REASON
    assert state.recovery

    bound = session_learn_delivery(
        _request(
            tmp_path,
            repo="owner/repo",
            issue=9,
            workspace=_source_binding(tmp_path),
        )
    )
    assert bound.state == "bound"
    assert bound.reason is None
