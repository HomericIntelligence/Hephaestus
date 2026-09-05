"""Effective branch-policy and exact-head Check Run tests."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.github_api as github_api_mod
import hephaestus.automation.pipeline_github as pg
import hephaestus.automation.pipeline_github_mutations as mutations_mod


def _response(payload: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload))


def _classic_policy(
    *,
    conversation_resolution: bool = True,
    enforce_admins: bool = True,
) -> dict[str, object]:
    return {
        "required_status_checks": {
            "contexts": ["classic-ci"],
            "checks": [{"context": "classic-ci", "app_id": 15368}],
        },
        "required_conversation_resolution": {"enabled": conversation_resolution},
        "enforce_admins": {"enabled": enforce_admins},
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
        },
    }


def _ruleset(
    *,
    ruleset_id: int = 155,
    can_bypass: str = "never",
    conversation_resolution: bool = True,
    context: str = "ruleset-ci",
    app_id: int | None = 15368,
) -> dict[str, object]:
    return {
        "id": ruleset_id,
        "name": "main policy",
        "target": "branch",
        "source_type": "Repository",
        "source": "org/repo",
        "enforcement": "active",
        "conditions": {
            "ref_name": {
                "include": ["refs/heads/main"],
                "exclude": [],
            }
        },
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "required_review_thread_resolution": conversation_resolution,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [{"context": context, "integration_id": app_id}],
                },
            },
        ],
        "bypass_actors": [
            {
                "actor_id": 5,
                "actor_type": "RepositoryRole",
                "bypass_mode": "pull_request",
            }
        ],
        "current_user_can_bypass": can_bypass,
    }


def _summary(ruleset: dict[str, object]) -> dict[str, object]:
    return {
        key: ruleset[key]
        for key in ("id", "name", "target", "source_type", "source", "enforcement")
    }


def _policy_transport(
    classic: object,
    rulesets: list[dict[str, object]],
) -> MagicMock:
    details = {ruleset["id"]: ruleset for ruleset in rulesets}

    def call(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        endpoint = next(part for part in args if isinstance(part, str) and "/repos/" in part)
        if endpoint.endswith("/branches/main/protection"):
            return _response(classic)
        if "/rulesets?" in endpoint:
            return _response([_summary(ruleset) for ruleset in rulesets])
        if "/rulesets/" in endpoint:
            return _response(details[int(endpoint.rsplit("/", 1)[1])])
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    return MagicMock(side_effect=call)


def test_effective_policy_combines_classic_and_applicable_ruleset_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stable policy preserves all context and application identities."""
    ruleset = _ruleset(can_bypass="pull_requests_only")
    call_mock = _policy_transport(_classic_policy(), [ruleset])
    monkeypatch.setattr(github_api_mod, "gh_call", call_mock)
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert [(check.context, check.app_id) for check in policy.required_checks] == [
        ("classic-ci", 15368),
        ("ruleset-ci", 15368),
    ]
    assert policy.conversation_resolution_enforced is True
    assert policy.bypassable_ruleset_ids == (155,)
    # Classic protection plus one ruleset list and detail are stable-read twice.
    assert call_mock.call_count == 6


@pytest.mark.parametrize(
    ("classic_conversation", "ruleset_bypass", "expected"),
    [
        (False, "never", True),
        (False, "pull_requests_only", False),
        (True, "pull_requests_only", True),
    ],
)
def test_conversation_resolution_requires_one_non_bypassable_enforcement_source(
    monkeypatch: pytest.MonkeyPatch,
    classic_conversation: bool,
    ruleset_bypass: str,
    expected: bool,
) -> None:
    """A bypassable ruleset alone cannot protect a late review thread."""
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(
            _classic_policy(conversation_resolution=classic_conversation),
            [_ruleset(can_bypass=ruleset_bypass)],
        ),
    )
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.conversation_resolution_enforced is expected


def test_policy_snapshot_change_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ruleset change between complete traversals cannot authorize a merge."""
    stable = _ruleset(context="first")
    changed = _ruleset(context="changed")
    details = iter([stable, changed])

    def call(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        endpoint = next(part for part in args if isinstance(part, str) and "/repos/" in part)
        if endpoint.endswith("/branches/main/protection"):
            return _response(_classic_policy())
        if "/rulesets?" in endpoint:
            return _response([_summary(stable)])
        if "/rulesets/" in endpoint:
            return _response(next(details))
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api_mod, "gh_call", MagicMock(side_effect=call))
    adapter = pg.PipelineGitHub("org", repo="repo")

    assert (
        adapter.effective_merge_policy(
            7,
            "main",
            deadline_s=time.monotonic() + 30.0,
            cancellation=threading.Event(),
        )
        is None
    )


@pytest.mark.parametrize(
    "malformation",
    [
        {"target": "tag"},
        {"enforcement": "mystery"},
        {"conditions": {"ref_name": {"include": "main", "exclude": []}}},
        {"rules": [{"type": "required_status_checks", "parameters": {}}]},
        {"bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole"}]},
    ],
)
def test_malformed_active_ruleset_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    malformation: dict[str, object],
) -> None:
    """Incomplete active ruleset facts never produce an effective policy."""
    ruleset = {**_ruleset(), **malformation}
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(_classic_policy(), [ruleset]),
    )
    adapter = pg.PipelineGitHub("org", repo="repo")

    assert (
        adapter.effective_merge_policy(
            7,
            "main",
            deadline_s=time.monotonic() + 30.0,
            cancellation=threading.Event(),
        )
        is None
    )


def _check_run(
    head: str,
    *,
    run_id: int,
    context: str,
    app_id: int | None,
    conclusion: str = "success",
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": context,
        "head_sha": head,
        "status": "completed",
        "conclusion": conclusion,
        "app": None if app_id is None else {"id": app_id},
    }


def test_check_runs_require_exact_application_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-name Check Run from the wrong application is not evidence."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    policy_call = _policy_transport(_classic_policy(), [])
    monkeypatch.setattr(github_api_mod, "gh_call", policy_call)
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    assert policy is not None
    head = "a" * 40
    check_call = MagicMock(
        return_value=_response(
            {
                "total_count": 1,
                "check_runs": [_check_run(head, run_id=1, context="classic-ci", app_id=999)],
            }
        )
    )
    monkeypatch.setattr(github_api_mod, "gh_call", check_call)

    assert (
        adapter.required_checks_pass_for_head(
            head,
            policy,
            deadline_s=time.monotonic() + 30.0,
            cancellation=threading.Event(),
        )
        is False
    )


@pytest.mark.parametrize("app", [None, {}, {"id": True}, {"id": 0}])
def test_check_runs_reject_missing_or_malformed_application_identity(
    monkeypatch: pytest.MonkeyPatch,
    app: object,
) -> None:
    """Every Check Run must contain a positive application identity."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(_classic_policy(), []),
    )
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    assert policy is not None
    head = "a" * 40
    run = _check_run(head, run_id=1, context="classic-ci", app_id=15368)
    run["app"] = app
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        MagicMock(return_value=_response({"total_count": 1, "check_runs": [run]})),
    )

    assert (
        adapter.required_checks_pass_for_head(
            head,
            policy,
            deadline_s=time.monotonic() + 30.0,
            cancellation=threading.Event(),
        )
        is False
    )


def test_check_traversal_honors_cancellation_between_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation stops pagination before the repository lock can be held longer."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(_classic_policy(), []),
    )
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    assert policy is not None
    head = "a" * 40
    cancellation = threading.Event()

    def call(_args: list[str], **_kwargs: Any) -> SimpleNamespace:
        cancellation.set()
        return _response(
            {
                "total_count": 101,
                "check_runs": [
                    _check_run(head, run_id=index, context="classic-ci", app_id=15368)
                    for index in range(1, 101)
                ],
            }
        )

    check_call = MagicMock(side_effect=call)
    monkeypatch.setattr(github_api_mod, "gh_call", check_call)

    assert (
        adapter.required_checks_pass_for_head(
            head,
            policy,
            deadline_s=time.monotonic() + 30.0,
            cancellation=cancellation,
        )
        is False
    )
    assert check_call.call_count == 1


def test_check_traversal_passes_aggregate_remaining_deadline_to_each_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each page uses the remaining aggregate operation budget."""
    adapter = pg.PipelineGitHub("org", repo="repo", gh_timeout=120)
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(_classic_policy(), []),
    )
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    assert policy is not None
    head = "a" * 40
    timeouts: list[float] = []

    def call(_args: list[str], **kwargs: Any) -> SimpleNamespace:
        timeouts.append(float(kwargs["timeout"]))
        return _response(
            {
                "total_count": 1,
                "check_runs": [_check_run(head, run_id=1, context="classic-ci", app_id=15368)],
            }
        )

    monkeypatch.setattr(github_api_mod, "gh_call", MagicMock(side_effect=call))
    deadline = time.monotonic() + 2.0

    assert adapter.required_checks_pass_for_head(
        head,
        policy,
        deadline_s=deadline,
        cancellation=threading.Event(),
    )
    assert len(timeouts) == 2
    assert all(0.0 < timeout <= 2.0 for timeout in timeouts)
    assert timeouts[1] <= timeouts[0]


def test_conditional_put_uses_remaining_aggregate_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final PUT cannot extend the repository-lock operation budget."""
    adapter = pg.PipelineGitHub("org", repo="repo", gh_timeout=120)
    call_mock = MagicMock(
        return_value=SimpleNamespace(returncode=0, stdout='HTTP/2 409\n\n{"merged":false}')
    )
    monkeypatch.setattr(mutations_mod, "gh_call", call_mock)

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        deadline_s=time.monotonic() + 2.0,
        cancellation=threading.Event(),
    )

    assert result.status == 409
    assert 0.0 < call_mock.call_args.kwargs["timeout"] <= 2.0


def test_conditional_put_honors_cancellation_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation stops the final PUT before GitHub receives a request."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    call_mock = MagicMock()
    monkeypatch.setattr(mutations_mod, "gh_call", call_mock)
    cancellation = threading.Event()
    cancellation.set()

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        deadline_s=time.monotonic() + 2.0,
        cancellation=cancellation,
    )

    assert result.transport_error is True
    call_mock.assert_not_called()
