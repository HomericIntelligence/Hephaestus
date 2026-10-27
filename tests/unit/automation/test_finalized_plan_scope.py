"""Read authenticated finalized plans through both scope consumers."""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Literal

import pytest

from hephaestus.automation.pipeline.admission import _fetch_planned_files
from hephaestus.automation.pipeline.github_jobs import ReadCurrentPlanScopeRequest
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from hephaestus.automation.requirements_recovery import (
    ATHENA_FINALIZED_PLAN_PREFIX,
    HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
)
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    IssueComment,
    render_current_plan,
)
from hephaestus.automation.state_labels import STATE_PLAN_GO


def _finalized_body(
    content: str = "## Files to Modify\n\n- `src/service.py`\n- `tests/test_service.py`",
    *,
    prefix: str = ATHENA_FINALIZED_PLAN_PREFIX,
) -> str:
    """Seal one synthetic plan with two source files."""
    template = (
        f"{content}\n\n{prefix}R={'a' * 64} P=123456789:{'b' * 64} V=987654321:{'c' * 64} F=<F> -->"
    )
    return template.replace("F=<F>", f"F={hashlib.sha256(template.encode()).hexdigest()}")


@pytest.mark.parametrize("consumer", ["overlap", "publication"])
def test_finalized_body_without_comments_supplies_scope(
    consumer: Literal["overlap", "publication"], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both consumers accept the same authenticated sealed body."""
    body = _finalized_body()
    github = PipelineGitHub("HomericIntelligence", repo="Athena")
    snapshot = {
        "number": 265,
        "state": "OPEN",
        "title": "Use the current plan",
        "body": body,
        "bodyDigest": hashlib.sha256(body.encode()).hexdigest(),
        "labels": [{"name": STATE_PLAN_GO}],
    }
    monkeypatch.setattr(github, "gh_issue_json", lambda _issue: dict(snapshot))
    monkeypatch.setattr(github, "issue_comments", lambda _issue: [])
    deadline = time.monotonic() + 10

    if consumer == "overlap":
        paths = _fetch_planned_files(
            265, github=github, deadline_s=deadline, shutdown=threading.Event()
        )
    else:
        receipt = PipelineGitHubJobRunner._read_current_plan_scope(
            ReadCurrentPlanScopeRequest("HomericIntelligence/Athena", 265, deadline), github
        )
        assert receipt.plan_sha256 == hashlib.sha256(body.encode()).hexdigest()
        paths = set(receipt.paths)

    assert paths == {"src/service.py", "tests/test_service.py"}


def _scope(consumer: str, github: PipelineGitHub) -> set[str] | None:
    """Read scope through the selected real consumer."""
    deadline = time.monotonic() + 10
    if consumer == "overlap":
        return _fetch_planned_files(
            265, github=github, deadline_s=deadline, shutdown=threading.Event()
        )
    result = PipelineGitHubJobRunner._read_current_plan_scope(
        ReadCurrentPlanScopeRequest("HomericIntelligence/Athena", 265, deadline), github
    )
    return set(result.paths)


def _github(monkeypatch: pytest.MonkeyPatch, body: str) -> tuple[PipelineGitHub, dict[str, Any]]:
    """Serve a sealed issue and an older comment plan at the GitHub boundary."""
    github = PipelineGitHub("HomericIntelligence", repo="Athena")
    snapshot: dict[str, Any] = {
        "number": 265,
        "state": "OPEN",
        "title": "Use the current plan",
        "body": body,
        "bodyDigest": hashlib.sha256(body.encode()).hexdigest(),
        "labels": [{"name": STATE_PLAN_GO}],
    }
    monkeypatch.setattr(github, "gh_issue_json", lambda _issue: dict(snapshot))
    monkeypatch.setattr(
        github,
        "issue_comments",
        lambda _issue: [
            IssueComment(
                render_current_plan("## Files to Modify\n- `old/scope.py`"),
                author_login="owner",
                viewer_did_author=True,
            )
        ],
    )
    return github, snapshot


@pytest.mark.parametrize("consumer", ["overlap", "publication"])
@pytest.mark.parametrize("heading", ["Files to Modify", "File Changes"])
def test_finalized_scope_contains_complete_declared_paths(
    consumer: str, heading: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root files and file-change sections retain overlap protection."""
    body = _finalized_body(
        f"## {heading}\n- `AGENTS.md`\n- `.pre-commit-config.yaml`\n- `src/service.py`",
        prefix=HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
    )
    github, _snapshot = _github(monkeypatch, body)
    assert _scope(consumer, github) == {"AGENTS.md", ".pre-commit-config.yaml", "src/service.py"}


@pytest.mark.parametrize("consumer", ["overlap", "publication"])
def test_empty_finalized_scope_declares_nothing(
    consumer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sealed plan without a file section declares no path, not an older plan."""
    github, _snapshot = _github(monkeypatch, _finalized_body("## Objective\nNo file scope."))
    assert _scope(consumer, github) == set()


@pytest.mark.parametrize("consumer", ["overlap", "publication"])
def test_partly_unusable_finalized_scope_keeps_its_usable_paths(
    consumer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable declaration is dropped, and the older comment plan stays unused."""
    body = _finalized_body("## Files to Modify\n- `src/service.py`\n- `../outside.py`")
    github, _snapshot = _github(monkeypatch, body)
    assert _scope(consumer, github) == {"src/service.py"}


@pytest.mark.parametrize("consumer", ["overlap", "publication"])
@pytest.mark.parametrize("fault", ["read-failure", "bad-shape"])
def test_unavailable_finalized_read_cannot_grant_scope(
    consumer: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incomplete or changed reads cannot use an older comment plan."""
    github, _snapshot = _github(monkeypatch, _finalized_body())
    if fault == "read-failure":

        def failed_read(_issue: int) -> dict[str, Any]:
            raise RuntimeError("GitHub read unavailable")

        monkeypatch.setattr(github, "gh_issue_json", failed_read)
    else:
        monkeypatch.setattr(github, "gh_issue_json", lambda _issue: [])
    with pytest.raises(CommentJournalReadError):
        _scope(consumer, github)


@pytest.mark.parametrize("consumer", ["overlap", "publication"])
def test_ordinary_comment_plan_keeps_its_scope(
    consumer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary issue continues to use its actor-owned plan comment."""
    github, snapshot = _github(monkeypatch, "Ordinary requirements.")
    snapshot["labels"] = [{"name": STATE_PLAN_GO}]
    assert _scope(consumer, github) == {"old/scope.py"}


@pytest.mark.parametrize("consumer", ["overlap", "publication"])
def test_seal_inside_comment_does_not_change_comment_path_rules(
    consumer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comment seal cannot select the finalized issue-body path."""
    github, snapshot = _github(monkeypatch, "Ordinary requirements.")
    snapshot["labels"] = [{"name": STATE_PLAN_GO}]
    body = _finalized_body("## Files to Modify\n- `AGENTS.md`\n- `src/service.py`")
    monkeypatch.setattr(
        github,
        "issue_comments",
        lambda _issue: [IssueComment(render_current_plan(body), viewer_did_author=True)],
    )
    expected = {"src/service.py"}
    if consumer == "publication":
        expected.add("AGENTS.md")
    assert _scope(consumer, github) == expected
