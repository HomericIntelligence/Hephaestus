"""Effective branch-policy and exact-head Check Run tests."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.github_api as github_api_mod
import hephaestus.automation.pipeline_github as pg
import hephaestus.automation.pipeline_github_mutations as mutations_mod
import hephaestus.automation.pipeline_github_required_checks as required_checks_mod
from hephaestus.automation.pipeline_github_check_policy import (
    EffectiveMergePolicy,
    RequiredCheck,
)
from hephaestus.automation.pipeline_github_ref_patterns import pathname_pattern_matches
from hephaestus.automation.pipeline_github_ruleset_conditions import ruleset_applies

_STATUS_EVIDENCE_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _response(payload: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload))


def _error_response(status: int, payload: object) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=1,
        stdout=f"HTTP/2 {status}\ncontent-type: application/json\n\n{json.dumps(payload)}",
    )


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
    *,
    default_branch: str = "main",
) -> MagicMock:
    details = {ruleset["id"]: ruleset for ruleset in rulesets}

    def call(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        endpoint = next(part for part in args if isinstance(part, str) and "/repos/" in part)
        if endpoint == "/repos/org/repo":
            return _response({"default_branch": default_branch})
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
    assert call_mock.call_count == 8


@pytest.mark.parametrize(
    ("include", "exclude", "default_branch", "base_branch", "applies"),
    [
        (["~DEFAULT_BRANCH"], [], "main", "main", True),
        (["~DEFAULT_BRANCH"], [], "trunk", "main", False),
        (["~ALL"], ["~DEFAULT_BRANCH"], "trunk", "main", True),
        (["~ALL"], ["~DEFAULT_BRANCH"], "main", "main", False),
    ],
)
def test_default_branch_token_matches_only_the_repository_default_branch(
    monkeypatch: pytest.MonkeyPatch,
    include: list[str],
    exclude: list[str],
    default_branch: str,
    base_branch: str,
    applies: bool,
) -> None:
    """The default-branch token matches only the repository's exact default ref."""
    ruleset = _ruleset()
    ruleset["conditions"] = {"ref_name": {"include": include, "exclude": exclude}}
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(_classic_policy(), [ruleset], default_branch=default_branch),
    )
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        base_branch,
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert any(check.context == "ruleset-ci" for check in policy.required_checks) is applies


@pytest.mark.parametrize(
    ("include", "exclude", "applies"),
    [
        (["refs/*"], [], False),
        (["refs/heads/*"], [], True),
        (["~ALL"], ["refs/*"], True),
        (["~ALL"], ["refs/heads/*"], False),
        (["refs/**/main"], [], True),
    ],
)
def test_ruleset_ref_patterns_use_github_pathname_matching(
    include: list[str],
    exclude: list[str],
    applies: bool,
) -> None:
    """Ruleset patterns use GitHub's path-separator-aware match rules."""
    ruleset = _ruleset()
    ruleset["conditions"] = {"ref_name": {"include": include, "exclude": exclude}}

    assert ruleset_applies(ruleset, "main", "main") is applies


@pytest.mark.parametrize(
    ("value", "pattern", "matches"),
    [
        ("refs/heads/main", "refs/heads/[^d]*", True),
        ("refs/heads/main", "refs/heads/[!d]*", True),
        ("refs/heads/]abc", r"refs/heads/[\]]*", True),
        ("refs/heads/-", r"refs/heads/[a\-c]", True),
        ("refs/heads/b", "refs/heads/[a-c]", True),
        ("refs/heads/d", "refs/heads/[a-c]", False),
    ],
    ids=(
        "caret-negation",
        "bang-negation",
        "escaped-closing-bracket",
        "escaped-hyphen",
        "range-match",
        "range-miss",
    ),
)
def test_ref_character_classes_match_ruby_pathname_semantics(
    value: str,
    pattern: str,
    matches: bool,
) -> None:
    """Character classes match Ruby File::FNM_PATHNAME reference results."""
    assert pathname_pattern_matches(value, pattern) is matches


@pytest.mark.parametrize(
    "pattern",
    [
        "refs/heads/[]]abc",
        "refs/heads/[abc",
        "refs/heads/[z-a]",
        "refs/heads/[[:alpha:]]",
    ],
)
def test_invalid_ref_character_classes_fail_closed(pattern: str) -> None:
    """An invalid character class cannot become a permissive selector."""
    with pytest.raises(ValueError, match="character class"):
        pathname_pattern_matches("refs/heads/main", pattern)


@pytest.mark.parametrize(
    ("include", "exclude", "applies"),
    [
        (["refs/heads/[^d]*"], [], True),
        (["~ALL"], ["refs/heads/[^d]*"], False),
    ],
    ids=("inclusive-character-class", "exclusive-character-class"),
)
def test_ref_character_classes_propagate_through_ruleset_conditions(
    include: list[str],
    exclude: list[str],
    applies: bool,
) -> None:
    """Include and exclude selectors use the same character-class contract."""
    ruleset = _ruleset()
    ruleset["conditions"] = {"ref_name": {"include": include, "exclude": exclude}}

    assert ruleset_applies(ruleset, "main", "main") is applies


def test_ruleset_only_policy_accepts_unambiguous_absent_classic_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authenticated absent classic policy does not hide active rulesets."""
    ruleset = _ruleset()
    base_transport = _policy_transport({}, [ruleset])

    def call(args: list[str], **kwargs: Any) -> SimpleNamespace:
        if any(str(part).endswith("/branches/main/protection") for part in args):
            return _error_response(404, {"message": "Branch not protected"})
        return base_transport(args, **kwargs)

    monkeypatch.setattr(github_api_mod, "gh_call", MagicMock(side_effect=call))
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.required_checks == (RequiredCheck("ruleset-ci", 15368),)
    assert policy.conversation_resolution_enforced is True


def test_ruleset_only_policy_without_thread_resolution_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No enforcement source can make an absent thread policy safe."""
    ruleset = _ruleset(conversation_resolution=False)
    base_transport = _policy_transport({}, [ruleset])

    def call(args: list[str], **kwargs: Any) -> SimpleNamespace:
        if any(str(part).endswith("/branches/main/protection") for part in args):
            return _error_response(404, {"message": "Branch not protected"})
        return base_transport(args, **kwargs)

    monkeypatch.setattr(github_api_mod, "gh_call", MagicMock(side_effect=call))
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.conversation_resolution_enforced is False


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(returncode=1, stdout="not an HTTP response"),
        _error_response(403, {"message": "Forbidden"}),
        _error_response(404, {"message": "Not Found"}),
        _error_response(404, ["Branch not protected"]),
    ],
    ids=("no-status", "other-error", "ambiguous-404", "malformed-404"),
)
def test_classic_protection_errors_other_than_unambiguous_absence_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    response: SimpleNamespace,
) -> None:
    """Only GitHub's exact absent-protection response can become empty policy."""
    ruleset = _ruleset()
    base_transport = _policy_transport({}, [ruleset])

    def call(args: list[str], **kwargs: Any) -> SimpleNamespace:
        if any(str(part).endswith("/branches/main/protection") for part in args):
            return response
        return base_transport(args, **kwargs)

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


def test_classic_contexts_only_response_is_a_valid_required_check_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub's documented context-only classic response remains usable."""
    classic = _classic_policy()
    status_checks = classic["required_status_checks"]
    assert isinstance(status_checks, dict)
    status_checks.pop("checks")
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(classic, []),
    )
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.required_checks == (RequiredCheck("classic-ci", None),)


def test_classic_any_app_binding_is_normalized_to_an_unbound_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub's documented app ID -1 accepts a matching run from any app."""
    classic = _classic_policy()
    status_checks = classic["required_status_checks"]
    assert isinstance(status_checks, dict)
    status_checks["checks"] = [{"context": "classic-ci", "app_id": -1}]
    monkeypatch.setattr(github_api_mod, "gh_call", _policy_transport(classic, []))
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.required_checks == (RequiredCheck("classic-ci", None),)


def test_mixed_unbound_and_app_bound_same_context_has_stable_total_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different valid bindings for one context form one deterministic policy."""
    classic = _classic_policy()
    status_checks = classic["required_status_checks"]
    assert isinstance(status_checks, dict)
    status_checks["contexts"] = ["shared-ci"]
    status_checks["checks"] = []
    ruleset = _ruleset(context="shared-ci", app_id=15368)
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(classic, [ruleset]),
    )
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.required_checks == (
        RequiredCheck("shared-ci", None),
        RequiredCheck("shared-ci", 15368),
    )


def test_evaluate_ruleset_is_valid_non_enforcing_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An evaluate ruleset does not enforce and does not make policy unavailable."""
    ruleset = _ruleset(context="evaluate-ci")
    ruleset["enforcement"] = "evaluate"
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(_classic_policy(), [ruleset]),
    )
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert all(check.context != "evaluate-ci" for check in policy.required_checks)


def test_inherited_organization_ruleset_uses_repository_scoped_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository-selected parent ruleset applies its validated ref condition."""
    ruleset = _ruleset(context="organization-ci")
    ruleset.update({"source_type": "Organization", "source": "org"})
    ruleset["conditions"] = {
        "ref_name": {"include": ["refs/heads/main"], "exclude": []},
        "repository_name": {
            "include": ["repo"],
            "exclude": [],
            "protected": False,
        },
    }
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        _policy_transport(_classic_policy(), [ruleset]),
    )
    adapter = pg.PipelineGitHub("org", repo="repo")

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert RequiredCheck("organization-ci", 15368) in policy.required_checks


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
        if endpoint == "/repos/org/repo":
            return _response({"default_branch": "main"})
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
    completed_at: object = "2026-09-05T12:00:00Z",
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": context,
        "head_sha": head,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": completed_at,
        "app": None if app_id is None else {"id": app_id},
    }


@pytest.mark.parametrize(
    ("completed_at", "expected"),
    [
        ("2026-09-05T12:00:00Z", True),
        ("2026-08-29T12:00:00Z", True),
        ("2026-08-29T11:59:59Z", False),
        ("2026-09-05T12:00:01Z", False),
        ("not-a-timestamp", False),
        (None, False),
    ],
    ids=("inside", "boundary", "expired", "future", "malformed", "missing"),
)
def test_required_check_run_evidence_enforces_seven_day_freshness(
    monkeypatch: pytest.MonkeyPatch,
    completed_at: object,
    expected: bool,
) -> None:
    """A required Check Run is current only in the inclusive seven-day window."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        required_checks_mod, "_status_evidence_now_utc", lambda: _STATUS_EVIDENCE_NOW
    )
    head = "a" * 40
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(),
    )
    run = _check_run(
        head,
        run_id=1,
        context="required-ci",
        app_id=15368,
        completed_at=completed_at,
    )
    runs = {"total_count": 1, "check_runs": [run]}
    empty_statuses = {"sha": head, "total_count": 0, "statuses": []}
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        MagicMock(
            side_effect=[
                _response(runs),
                _response(runs),
                _response(empty_statuses),
                _response(empty_statuses),
            ]
        ),
    )

    assert (
        adapter.required_checks_pass_for_head(
            head,
            policy,
            deadline_s=time.monotonic() + 30.0,
            cancellation=threading.Event(),
        )
        is expected
    )


@pytest.mark.parametrize(
    "conclusions",
    [
        ("neutral", "neutral"),
        ("skipped", "skipped"),
        ("neutral", "skipped"),
    ],
    ids=("all-neutral", "all-skipped", "mixed-allowed"),
)
def test_all_allowed_required_check_conclusions_satisfy_policy(
    monkeypatch: pytest.MonkeyPatch,
    conclusions: tuple[str, str],
) -> None:
    """Each GitHub-allowed terminal conclusion satisfies a required check."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    head = "a" * 40
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("first", None), RequiredCheck("second", None)),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(),
    )
    payload = {
        "total_count": 2,
        "check_runs": [
            _check_run(head, run_id=1, context="first", app_id=15368, conclusion=conclusions[0]),
            _check_run(head, run_id=2, context="second", app_id=15368, conclusion=conclusions[1]),
        ],
    }
    monkeypatch.setattr(
        github_api_mod,
        "gh_call",
        MagicMock(
            side_effect=[
                _response(payload),
                _response(payload),
                _response({"sha": head, "total_count": 0, "statuses": []}),
                _response({"sha": head, "total_count": 0, "statuses": []}),
            ]
        ),
    )

    assert adapter.required_checks_pass_for_head(
        head,
        policy,
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )


def test_required_check_gate_rejects_calls_without_a_frozen_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check gate has no second policy-discovery authority path."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    call_mock = MagicMock()
    monkeypatch.setattr(github_api_mod, "gh_call", call_mock)

    with pytest.raises(TypeError):
        adapter.required_checks_pass_for_head("a" * 40)  # type: ignore[call-arg]

    call_mock.assert_not_called()


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

    def call(args: list[str], **kwargs: Any) -> SimpleNamespace:
        timeouts.append(float(kwargs["timeout"]))
        if "/status?" in args[1]:
            return _response({"sha": head, "total_count": 0, "statuses": []})
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
    assert len(timeouts) == 4
    assert all(0.0 < timeout <= 2.0 for timeout in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)


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
