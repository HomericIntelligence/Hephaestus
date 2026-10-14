"""CLI behavior for safe GitHub issue-timeline compaction."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from hephaestus.automation.issue_timeline import IssueTimelineCompaction
from hephaestus.automation.protocol import PLAN_CANONICAL_MARKER
from hephaestus.automation.review_journal import (
    HISTORY_MARKER,
    render_current_plan,
    render_current_review,
)


@pytest.fixture
def script_module() -> ModuleType:
    """Load the standalone script as a testable module."""
    path = Path(__file__).parents[3] / "scripts" / "compact_issue_timelines.py"
    spec = importlib.util.spec_from_file_location("compact_issue_timelines", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_is_read_only_and_excludes_pull_requests(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default mode inventories all issues but performs no mutations."""
    comments = {
        1: [
            {"id": 11, "body": render_current_plan("Plan", revision=1), "user": {"login": "bot"}},
            {"id": 12, "body": render_current_review("GO", revision=1), "user": {"login": "bot"}},
            {
                "id": 13,
                "body": "<!-- hephaestus-state-skip-reason -->\nold",
                "user": {"login": "bot"},
            },
        ]
    }
    monkeypatch.setattr(
        script_module.github_api,
        "_gh_call",
        lambda _args: SimpleNamespace(stdout='[[{"number":1},{"number":2,"pull_request":{}}]]'),
    )
    monkeypatch.setattr(script_module.github_api, "gh_current_login", lambda: "bot")
    monkeypatch.setattr(
        script_module.github_api,
        "fetch_issue_comments_metadata",
        lambda issue, _repo: comments[issue],
    )
    mutations: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        script_module.github_api,
        "gh_issue_delete_comment",
        lambda *args, **kwargs: mutations.append((*args, kwargs)),
    )

    assert script_module.main(["--repo", "owner/repo"]) == 0

    assert mutations == []
    assert "DRY-RUN #1" in capsys.readouterr().out


def test_apply_rechecks_the_delete_plan_before_removing_comments(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed timeline stops before an old delete list is applied."""
    deleted: list[int] = []
    monkeypatch.setattr(
        script_module,
        "_plan_issue",
        lambda *_args, **_kwargs: IssueTimelineCompaction(delete_comment_ids=(12,)),
    )
    monkeypatch.setattr(
        script_module.github_api,
        "gh_issue_delete_comment",
        lambda comment_id, **_kwargs: deleted.append(comment_id),
    )

    with pytest.raises(RuntimeError, match="timeline changed before deletion"):
        script_module._apply_issue(
            1,
            IssueTimelineCompaction(delete_comment_ids=(11,)),
            repo=("owner", "repo"),
            viewer_login="bot",
        )

    assert deleted == []


def test_apply_rejects_plan_alias_in_owned_history_before_patch_or_delete(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A history payload marker stops apply before it can mutate the issue."""
    history = (
        f"{HISTORY_MARKER.format(revision=1, kind='plan')}\n"
        "## Previous plan\n\n"
        f"{PLAN_CANONICAL_MARKER}\n"
        "# Historical payload"
    )
    comments = {
        1: [
            {"id": 11, "body": history, "user": {"login": "bot"}},
            {
                "id": 12,
                "body": render_current_review("GO", revision=2),
                "user": {"login": "bot"},
            },
            {
                "id": 13,
                "body": "<!-- hephaestus-state-skip-reason -->\nobsolete",
                "user": {"login": "bot"},
            },
        ]
    }
    monkeypatch.setattr(
        script_module.github_api,
        "_gh_call",
        lambda _args: SimpleNamespace(stdout='[[{"number":1}]]'),
    )
    monkeypatch.setattr(script_module.github_api, "gh_current_login", lambda: "bot")
    monkeypatch.setattr(
        script_module.github_api,
        "fetch_issue_comments_metadata",
        lambda issue, _repo: comments[issue],
    )
    upserts: list[tuple[Any, ...]] = []
    deletes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        script_module.github_api,
        "gh_issue_upsert_owned_comment",
        lambda *args, **kwargs: upserts.append((*args, kwargs)),
    )
    monkeypatch.setattr(
        script_module.github_api,
        "gh_issue_delete_comment",
        lambda *args, **kwargs: deletes.append((*args, kwargs)),
    )

    assert script_module.main(["--repo", "owner/repo", "--apply"]) == 1

    assert upserts == []
    assert deletes == []
    assert "immutable history artifact" in capsys.readouterr().err
