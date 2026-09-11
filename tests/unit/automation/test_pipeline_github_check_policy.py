"""Effective branch-policy and exact-head Check Run tests."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.github_api as github_api_mod
import hephaestus.automation.pipeline_github as pg
import hephaestus.automation.pipeline_github_required_checks as required_checks_mod
from hephaestus.automation.pipeline_github_check_policy import (
    EffectiveMergePolicy,
    RequiredCheck,
)
from hephaestus.automation.pipeline_github_ref_patterns import pathname_pattern_matches
from hephaestus.automation.pipeline_github_ruleset_conditions import (
    required_app_id,
    ruleset_applies,
)

_STATUS_EVIDENCE_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def command_runner() -> MagicMock:
    """Keep policy tests on the explicit command boundary."""

    def unexpected_command(argv: list[str], **_kwargs: Any) -> None:
        pytest.fail(f"Unexpected GitHub command: {argv!r}")

    return MagicMock(side_effect=unexpected_command)


@pytest.fixture(autouse=True)
def stable_check_suite_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep policy tests focused on required Check Run evaluation."""

    def suite_ids(
        _adapter: object,
        _head_sha: str,
        *,
        deadline_s: float,
        cancellation: threading.Event,
    ) -> tuple[int, ...]:
        del deadline_s, cancellation
        return (1,)

    monkeypatch.setattr(pg.PipelineGitHub, "_check_suite_ids_for_head", suite_ids)


def _response(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], stderr="", returncode=0, stdout=json.dumps(payload))


def _error_response(status: int, payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        stderr="",
        returncode=1,
        stdout=f"HTTP/2 {status}\ncontent-type: application/json\n\n{json.dumps(payload)}",
    )


def _classic_policy(
    *,
    conversation_resolution: bool = True,
    enforce_admins: bool = True,
    strict: bool = False,
) -> dict[str, object]:
    return {
        "required_status_checks": {
            "strict": strict,
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
    strict: bool = False,
    merge_queue: bool = False,
) -> dict[str, object]:
    rules: list[dict[str, object]] = [
        {
            "type": "pull_request",
            "parameters": {
                "required_review_thread_resolution": conversation_resolution,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": strict,
                "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": context, "integration_id": app_id}],
            },
        },
    ]
    if merge_queue:
        rules.append(
            {
                "type": "merge_queue",
                "parameters": {
                    "merge_method": "SQUASH",
                    "max_entries_to_build": 2,
                    "min_entries_to_merge": 1,
                    "max_entries_to_merge": 5,
                    "min_entries_to_merge_wait_minutes": 5,
                    "grouping_strategy": "HEADGREEN",
                    "check_response_timeout_minutes": 180,
                },
            }
        )
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
        "rules": rules,
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

    def call(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
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
    command_runner: MagicMock,
) -> None:
    """The stable policy preserves all context and application identities."""
    ruleset = _ruleset(can_bypass="pull_requests_only")
    call_mock = _policy_transport(_classic_policy(), [ruleset])
    command_runner.side_effect = call_mock
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
    assert policy.strict_update_enforced is False
    assert policy.merge_queue_required is False
    # Classic protection plus one ruleset list and detail are stable-read twice.
    assert call_mock.call_count == 8


@pytest.mark.parametrize(
    ("actor_type", "actor_id"),
    [
        ("DeployKey", None),
        ("OrganizationAdmin", None),
        ("OrganizationAdmin", 0),
        ("EnterpriseOwner", None),
        ("EnterpriseOwner", -1),
    ],
    ids=(
        "deploy-key-null",
        "organization-admin-null",
        "organization-admin-ignored-integer",
        "enterprise-owner-null",
        "enterprise-owner-ignored-integer",
    ),
)
def test_documented_bypass_actor_ids_produce_an_effective_policy(
    command_runner: MagicMock,
    actor_type: str,
    actor_id: object,
) -> None:
    """Documented null and ignored actor IDs are valid policy input."""
    ruleset = _ruleset(can_bypass="always")
    ruleset["bypass_actors"] = [
        {
            "actor_id": actor_id,
            "actor_type": actor_type,
            "bypass_mode": "always",
        }
    ]
    command_runner.side_effect = _policy_transport(_classic_policy(), [ruleset])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.bypassable_ruleset_ids == (155,)


@pytest.mark.parametrize(
    ("actor_type", "actor_id", "bypass_mode"),
    [
        ("Integration", None, "always"),
        ("RepositoryRole", None, "always"),
        ("Team", None, "always"),
        ("User", None, "always"),
        ("EnterpriseRole", None, "always"),
        ("Integration", 0, "always"),
        ("RepositoryRole", -1, "always"),
        ("Team", True, "always"),
        ("User", "5", "always"),
        ("DeployKey", 5, "always"),
        ("DeployKey", None, "pull_request"),
        ("OrganizationAdmin", True, "always"),
        ("EnterpriseOwner", "ignored", "always"),
        ([], 5, "always"),
        ("RepositoryRole", 5, []),
    ],
    ids=(
        "integration-null",
        "repository-role-null",
        "team-null",
        "user-null",
        "enterprise-role-null",
        "integration-zero",
        "repository-role-negative",
        "team-boolean",
        "user-string",
        "deploy-key-non-null",
        "deploy-key-pull-request-mode",
        "organization-admin-boolean",
        "enterprise-owner-string",
        "unhashable-actor-type",
        "unhashable-bypass-mode",
    ),
)
def test_malformed_bypass_actor_ids_fail_closed(
    command_runner: MagicMock,
    actor_type: object,
    actor_id: object,
    bypass_mode: object,
) -> None:
    """Invalid actor-specific ID and mode combinations produce no policy."""
    ruleset = _ruleset()
    ruleset["bypass_actors"] = [
        {
            "actor_id": actor_id,
            "actor_type": actor_type,
            "bypass_mode": bypass_mode,
        }
    ]
    command_runner.side_effect = _policy_transport(_classic_policy(), [ruleset])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
    ("classic_strict", "ruleset_strict", "expected"),
    [(False, False, False), (True, False, True), (False, True, True)],
)
def test_effective_policy_combines_classic_and_ruleset_strict_update(
    command_runner: MagicMock,
    classic_strict: bool,
    ruleset_strict: bool,
    expected: bool,
) -> None:
    """Either applicable server policy can require an up-to-date branch."""
    command_runner.side_effect = _policy_transport(
        _classic_policy(strict=classic_strict),
        [_ruleset(strict=ruleset_strict)],
    )
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.strict_update_enforced is expected


def test_bypassable_classic_strictness_cannot_authorize_a_direct_merge(
    command_runner: MagicMock,
) -> None:
    """Direct merge rejects strictness that does not apply to the actor."""
    command_runner.side_effect = _policy_transport(
        _classic_policy(strict=True, enforce_admins=False),
        [_ruleset(strict=False, can_bypass="never")],
    )
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    rest_call = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], stderr="", returncode=0, stdout='HTTP/2 409\n\n{"merged":false}'
        )
    )
    command_runner.side_effect = rest_call

    assert policy is not None
    assert policy.conversation_resolution_enforced is True
    assert policy.strict_update_enforced is False
    result = adapter.merge_pr_if_head(7, "a" * 40, policy=policy)

    assert result.malformed is True
    rest_call.assert_not_called()


def test_non_bypassable_ruleset_strictness_authorizes_a_direct_merge(
    command_runner: MagicMock,
) -> None:
    """Direct merge accepts strictness that applies to the actor."""
    command_runner.side_effect = _policy_transport(
        _classic_policy(strict=False, enforce_admins=False),
        [_ruleset(strict=True, can_bypass="never")],
    )
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    rest_call = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], stderr="", returncode=0, stdout='HTTP/2 409\n\n{"merged":false}'
        )
    )
    command_runner.side_effect = rest_call

    assert policy is not None
    assert policy.conversation_resolution_enforced is True
    assert policy.strict_update_enforced is True
    result = adapter.merge_pr_if_head(7, "a" * 40, policy=policy)

    assert result.status == 409
    assert result.malformed is False
    rest_call.assert_called_once()


def test_bypassable_ruleset_strictness_is_not_direct_merge_protection(
    command_runner: MagicMock,
) -> None:
    """A ruleset that this actor can bypass cannot supply direct-route safety."""
    command_runner.side_effect = _policy_transport(
        _classic_policy(strict=False),
        [_ruleset(strict=True, can_bypass="pull_requests_only")],
    )
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.strict_update_enforced is False


def test_effective_policy_records_an_applicable_required_merge_queue(
    command_runner: MagicMock,
) -> None:
    """An active applicable merge-queue rule selects queue admission."""
    command_runner.side_effect = _policy_transport(_classic_policy(), [_ruleset(merge_queue=True)])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.merge_queue_required is True
    assert policy.merge_queue_method == "SQUASH"


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
    command_runner: MagicMock,
    include: list[str],
    exclude: list[str],
    default_branch: str,
    base_branch: str,
    applies: bool,
) -> None:
    """The default-branch token matches only the repository's exact default ref."""
    ruleset = _ruleset()
    ruleset["conditions"] = {"ref_name": {"include": include, "exclude": exclude}}
    command_runner.side_effect = _policy_transport(
        _classic_policy(), [ruleset], default_branch=default_branch
    )
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (-1, None), (1, 1), (15368, 15368)],
)
def test_required_app_id_normalizes_documented_bindings(
    value: object, expected: int | None
) -> None:
    """Documented GitHub App bindings normalize without changing identity."""
    assert required_app_id(value) == expected


@pytest.mark.parametrize("value", [True, False, 0, -2, "1", 1.0, [], {}])
def test_required_app_id_rejects_malformed_bindings(value: object) -> None:
    """Malformed GitHub App bindings fail before policy can use them."""
    with pytest.raises(ValueError, match="App ID is malformed"):
        required_app_id(value)


def _scoped_ruleset(source_type: str, selectors: dict[str, object]) -> dict[str, object]:
    """Return one ruleset with caller-selected parent scope conditions."""
    ruleset = _ruleset()
    ruleset["source_type"] = source_type
    ruleset["conditions"] = {
        "ref_name": {"include": ["refs/heads/main"], "exclude": []},
        **selectors,
    }
    return ruleset


@pytest.mark.parametrize(
    ("source_type", "selectors"),
    [
        (
            "Organization",
            {"repository_name": {"include": ["repo"], "exclude": [], "protected": False}},
        ),
        ("Organization", {"repository_id": {"repository_ids": [1, 2]}}),
        (
            "Organization",
            {
                "repository_property": {
                    "include": [
                        {
                            "name": "service",
                            "property_values": ["api"],
                            "source": "custom",
                        }
                    ],
                    "exclude": [],
                }
            },
        ),
        (
            "Enterprise",
            {
                "repository_name": {"include": ["repo"], "exclude": []},
                "organization_name": {"include": ["org"], "exclude": []},
            },
        ),
        (
            "Enterprise",
            {
                "repository_id": {"repository_ids": [1]},
                "organization_id": {"organization_ids": [2]},
            },
        ),
        (
            "Enterprise",
            {
                "repository_property": {
                    "include": [
                        {
                            "name": "visibility",
                            "property_values": ["private"],
                            "source": "system",
                        }
                    ]
                },
                "organization_property": {
                    "include": [{"name": "region", "property_values": ["us"]}]
                },
            },
        ),
    ],
    ids=(
        "organization-name",
        "organization-repository-id",
        "organization-property",
        "enterprise-names",
        "enterprise-ids",
        "enterprise-properties",
    ),
)
def test_parent_ruleset_selectors_accept_documented_shapes(
    source_type: str, selectors: dict[str, object]
) -> None:
    """Repository-selected parent selectors accept each documented selector family."""
    assert ruleset_applies(_scoped_ruleset(source_type, selectors), "main", "main") is True


@pytest.mark.parametrize(
    ("source_type", "selectors", "message"),
    [
        ("Repository", {"repository_name": {}}, "parent-only"),
        ("Unknown", {}, "source type"),
        ("Organization", {}, "repository selector"),
        (
            "Organization",
            {
                "repository_name": {"include": ["repo"]},
                "repository_id": {"repository_ids": [1]},
            },
            "repository selector",
        ),
        (
            "Organization",
            {"organization_name": {"include": ["org"]}},
            "repository selector",
        ),
        (
            "Enterprise",
            {"repository_name": {"include": ["repo"]}},
            "enterprise ruleset selectors",
        ),
        (
            "Enterprise",
            {
                "repository_name": {"include": ["repo"]},
                "organization_name": {"include": ["org"]},
                "organization_id": {"organization_ids": [1]},
            },
            "enterprise ruleset selectors",
        ),
    ],
)
def test_parent_ruleset_scope_rejects_ambiguous_selector_sets(
    source_type: str,
    selectors: dict[str, object],
    message: str,
) -> None:
    """A parent ruleset needs exactly one selector for each required scope."""
    with pytest.raises(ValueError, match=message):
        ruleset_applies(_scoped_ruleset(source_type, selectors), "main", "main")


@pytest.mark.parametrize(
    "selector",
    [
        None,
        {},
        {"include": ["repo"], "extra": []},
        {"include": "repo"},
        {"include": [""]},
        {"include": ["repo"], "exclude": "other"},
        {"include": ["repo"], "protected": "false"},
    ],
)
def test_ruleset_name_selector_rejects_malformed_shapes(selector: object) -> None:
    """A repository-name selector needs valid lists and an optional Boolean flag."""
    ruleset = _scoped_ruleset("Organization", {"repository_name": selector})
    with pytest.raises(ValueError, match="selector is malformed"):
        ruleset_applies(ruleset, "main", "main")


@pytest.mark.parametrize(
    "selector",
    [
        None,
        {},
        {"repository_ids": []},
        {"repository_ids": "1"},
        {"repository_ids": [True]},
        {"repository_ids": [0]},
        {"repository_ids": [1], "extra": []},
    ],
)
def test_ruleset_id_selector_rejects_malformed_shapes(selector: object) -> None:
    """A repository-ID selector needs a nonempty list of positive integer IDs."""
    ruleset = _scoped_ruleset("Organization", {"repository_id": selector})
    with pytest.raises(ValueError, match="ID selector is malformed"):
        ruleset_applies(ruleset, "main", "main")


@pytest.mark.parametrize(
    "selector",
    [
        None,
        {},
        {"include": [], "extra": []},
        {"include": "record"},
        {"include": [None]},
        {"include": [{"name": "kind"}]},
        {"include": [{"name": "kind", "property_values": [], "extra": "x"}]},
        {"include": [{"name": "", "property_values": ["api"]}]},
        {"include": [{"name": "kind", "property_values": "api"}]},
        {"include": [{"name": "kind", "property_values": [""]}]},
        {"include": [{"name": "kind", "property_values": ["api"], "source": "other"}]},
        {"include": [], "exclude": "record"},
    ],
)
def test_ruleset_property_selector_rejects_malformed_shapes(selector: object) -> None:
    """A property selector rejects unknown fields and malformed records."""
    ruleset = _scoped_ruleset("Organization", {"repository_property": selector})
    with pytest.raises(ValueError, match="property selector is malformed"):
        ruleset_applies(ruleset, "main", "main")


@pytest.mark.parametrize(
    "conditions",
    [
        None,
        {},
        {"ref_name": None},
        {"ref_name": {}},
        {"ref_name": {"include": [], "exclude": [], "extra": []}},
        {"ref_name": {"include": "main", "exclude": []}},
        {"ref_name": {"include": [], "exclude": "main"}},
        {"ref_name": {"include": [""], "exclude": []}},
        {"ref_name": {"include": [], "exclude": [1]}},
    ],
)
def test_ruleset_ref_conditions_reject_malformed_shapes(conditions: object) -> None:
    """Branch conditions require complete nonempty string pattern lists."""
    ruleset = _ruleset()
    ruleset["conditions"] = conditions
    with pytest.raises(
        ValueError, match=r"conditions are malformed|patterns? (?:are|is) malformed"
    ):
        ruleset_applies(ruleset, "main", "main")


def test_ruleset_only_policy_accepts_unambiguous_absent_classic_protection(
    command_runner: MagicMock,
) -> None:
    """An authenticated absent classic policy does not hide active rulesets."""
    ruleset = _ruleset()
    base_transport = _policy_transport({}, [ruleset])

    def call(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if any(str(part).endswith("/branches/main/protection") for part in args):
            return _error_response(404, {"message": "Branch not protected"})
        return base_transport(args, **kwargs)

    command_runner.side_effect = MagicMock(side_effect=call)
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
    command_runner: MagicMock,
) -> None:
    """No enforcement source can make an absent thread policy safe."""
    ruleset = _ruleset(conversation_resolution=False)
    base_transport = _policy_transport({}, [ruleset])

    def call(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if any(str(part).endswith("/branches/main/protection") for part in args):
            return _error_response(404, {"message": "Branch not protected"})
        return base_transport(args, **kwargs)

    command_runner.side_effect = MagicMock(side_effect=call)
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
        subprocess.CompletedProcess(
            args=[], stderr="", returncode=1, stdout="not an HTTP response"
        ),
        _error_response(403, {"message": "Forbidden"}),
        _error_response(404, {"message": "Not Found"}),
        _error_response(404, ["Branch not protected"]),
    ],
    ids=("no-status", "other-error", "ambiguous-404", "malformed-404"),
)
def test_classic_protection_errors_other_than_unambiguous_absence_fail_closed(
    command_runner: MagicMock,
    response: subprocess.CompletedProcess[str],
) -> None:
    """Only GitHub's exact absent-protection response can become empty policy."""
    ruleset = _ruleset()
    base_transport = _policy_transport({}, [ruleset])

    def call(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if any(str(part).endswith("/branches/main/protection") for part in args):
            return response
        return base_transport(args, **kwargs)

    command_runner.side_effect = MagicMock(side_effect=call)
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
    command_runner: MagicMock,
) -> None:
    """GitHub's documented context-only classic response remains usable."""
    classic = _classic_policy()
    status_checks = classic["required_status_checks"]
    assert isinstance(status_checks, dict)
    status_checks.pop("checks")
    command_runner.side_effect = _policy_transport(classic, [])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.required_checks == (RequiredCheck("classic-ci", None),)


def test_classic_any_app_binding_is_normalized_to_an_unbound_requirement(
    command_runner: MagicMock,
) -> None:
    """GitHub's documented app ID -1 accepts a matching run from any app."""
    classic = _classic_policy()
    status_checks = classic["required_status_checks"]
    assert isinstance(status_checks, dict)
    status_checks["checks"] = [{"context": "classic-ci", "app_id": -1}]
    command_runner.side_effect = _policy_transport(classic, [])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.required_checks == (RequiredCheck("classic-ci", None),)


def test_mixed_unbound_and_app_bound_same_context_has_stable_total_order(
    command_runner: MagicMock,
) -> None:
    """Different valid bindings for one context form one deterministic policy."""
    classic = _classic_policy()
    status_checks = classic["required_status_checks"]
    assert isinstance(status_checks, dict)
    status_checks["contexts"] = ["shared-ci"]
    status_checks["checks"] = []
    ruleset = _ruleset(context="shared-ci", app_id=15368)
    command_runner.side_effect = _policy_transport(classic, [ruleset])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
    command_runner: MagicMock,
) -> None:
    """An evaluate ruleset does not enforce and does not make policy unavailable."""
    ruleset = _ruleset(context="evaluate-ci")
    ruleset["enforcement"] = "evaluate"
    command_runner.side_effect = _policy_transport(_classic_policy(), [ruleset])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert all(check.context != "evaluate-ci" for check in policy.required_checks)


def test_inherited_organization_ruleset_uses_repository_scoped_selection(
    command_runner: MagicMock,
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
    command_runner.side_effect = _policy_transport(_classic_policy(), [ruleset])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
    command_runner: MagicMock,
    classic_conversation: bool,
    ruleset_bypass: str,
    expected: bool,
) -> None:
    """A bypassable ruleset alone cannot protect a late review thread."""
    command_runner.side_effect = _policy_transport(
        _classic_policy(conversation_resolution=classic_conversation),
        [_ruleset(can_bypass=ruleset_bypass)],
    )
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )

    assert policy is not None
    assert policy.conversation_resolution_enforced is expected


def test_policy_snapshot_change_fails_closed(command_runner: MagicMock) -> None:
    """A ruleset change between complete traversals cannot authorize a merge."""
    stable = _ruleset(context="first")
    changed = _ruleset(context="changed")
    details = iter([stable, changed])

    def call(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
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

    command_runner.side_effect = MagicMock(side_effect=call)
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
        {"current_user_can_bypass": []},
    ],
)
def test_malformed_active_ruleset_fails_closed(
    command_runner: MagicMock,
    malformation: dict[str, object],
) -> None:
    """Incomplete active ruleset facts never produce an effective policy."""
    ruleset = {**_ruleset(), **malformation}
    command_runner.side_effect = _policy_transport(_classic_policy(), [ruleset])
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)

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
        ("2026-09-05Q12:00:00Z", False),
        ("2026-09-05T24:00:00Z", False),
        ("2026-09-05T12:60:00Z", False),
        ("2026-09-05T12:00:60Z", False),
        ("2026-09-05T12:00:00+24:00", False),
        ("2026-09-05T12:00:00+00:60", False),
        ("0001-01-01T00:00:00+23:59", False),
        (None, False),
    ],
    ids=(
        "inside",
        "boundary",
        "expired",
        "future",
        "malformed",
        "separator",
        "hour-range",
        "minute-range",
        "second-range",
        "offset-hour-range",
        "offset-minute-range",
        "overflow",
        "missing",
    ),
)
def test_required_check_run_evidence_enforces_seven_day_freshness(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    completed_at: object,
    expected: bool,
) -> None:
    """A required Check Run is current only in the inclusive seven-day window."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
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
    command_runner.side_effect = MagicMock(
        side_effect=[
            _response(runs),
            _response(runs),
            _response(empty_statuses),
            _response(empty_statuses),
        ]
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
    command_runner: MagicMock,
    conclusions: tuple[str, str],
) -> None:
    """Each GitHub-allowed terminal conclusion satisfies a required check."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
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
    command_runner.side_effect = MagicMock(
        side_effect=[
            _response(payload),
            _response(payload),
            _response({"sha": head, "total_count": 0, "statuses": []}),
            _response({"sha": head, "total_count": 0, "statuses": []}),
        ]
    )

    assert adapter.required_checks_pass_for_head(
        head,
        policy,
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )


def test_required_check_gate_rejects_a_non_frozen_policy(
    command_runner: MagicMock,
) -> None:
    """The check gate has no second policy-discovery authority path."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    call_mock = MagicMock()
    command_runner.side_effect = call_mock

    assert (
        adapter.required_checks_pass_for_head(
            "a" * 40,
            None,  # type: ignore[arg-type]
            deadline_s=time.monotonic() + 30.0,
            cancellation=threading.Event(),
        )
        is False
    )

    call_mock.assert_not_called()


def test_check_runs_require_exact_application_identity(
    command_runner: MagicMock,
) -> None:
    """A same-name Check Run from the wrong application is not evidence."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    policy_call = _policy_transport(_classic_policy(), [])
    command_runner.side_effect = policy_call
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
    command_runner.side_effect = check_call

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
    command_runner: MagicMock,
    app: object,
) -> None:
    """Every Check Run must contain a positive application identity."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    command_runner.side_effect = _policy_transport(_classic_policy(), [])
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
    command_runner.side_effect = MagicMock(
        return_value=_response({"total_count": 1, "check_runs": [run]})
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


def test_optional_check_run_with_null_app_does_not_revoke_required_evidence(
    command_runner: MagicMock,
) -> None:
    """A schema-valid optional run with no app cannot change merge authority."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    head = "a" * 40
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(),
    )
    payload = {
        "total_count": 2,
        "check_runs": [
            _check_run(head, run_id=1, context="required-ci", app_id=15368),
            _check_run(head, run_id=2, context="optional-ci", app_id=None),
        ],
    }
    empty_statuses = {"sha": head, "total_count": 0, "statuses": []}
    command_runner.side_effect = MagicMock(
        side_effect=[
            _response(payload),
            _response(payload),
            _response(empty_statuses),
            _response(empty_statuses),
        ]
    )

    assert adapter.required_checks_pass_for_head(
        head,
        policy,
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )


def test_check_traversal_honors_cancellation_between_pages(
    command_runner: MagicMock,
) -> None:
    """Cancellation stops pagination before the repository lock can be held longer."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    command_runner.side_effect = _policy_transport(_classic_policy(), [])
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    assert policy is not None
    head = "a" * 40
    cancellation = threading.Event()

    def call(_args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
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
    command_runner.side_effect = check_call

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
    command_runner: MagicMock,
) -> None:
    """Each page uses the remaining aggregate operation budget."""
    adapter = pg.PipelineGitHub("org", repo="repo", gh_timeout=120, command_runner=command_runner)
    command_runner.side_effect = _policy_transport(_classic_policy(), [])
    policy = adapter.effective_merge_policy(
        7,
        "main",
        deadline_s=time.monotonic() + 30.0,
        cancellation=threading.Event(),
    )
    assert policy is not None
    head = "a" * 40
    timeouts: list[float] = []

    def call(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(float(kwargs["timeout"]))
        if "/status?" in args[1]:
            return _response({"sha": head, "total_count": 0, "statuses": []})
        return _response(
            {
                "total_count": 1,
                "check_runs": [_check_run(head, run_id=1, context="classic-ci", app_id=15368)],
            }
        )

    command_runner.side_effect = MagicMock(side_effect=call)
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
    command_runner: MagicMock,
) -> None:
    """The final PUT cannot extend the repository-lock operation budget."""
    adapter = pg.PipelineGitHub("org", repo="repo", gh_timeout=120, command_runner=command_runner)
    call_mock = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], stderr="", returncode=0, stdout='HTTP/2 409\n\n{"merged":false}'
        )
    )
    command_runner.side_effect = call_mock

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=EffectiveMergePolicy(
            base_branch="main",
            default_branch="main",
            required_checks=(RequiredCheck("required-ci", 15368),),
            conversation_resolution_enforced=True,
            bypassable_ruleset_ids=(),
            strict_update_enforced=True,
            merge_queue_method=None,
        ),
        deadline_s=time.monotonic() + 2.0,
        cancellation=threading.Event(),
    )

    assert result.status == 409
    assert 0.0 < call_mock.call_args.kwargs["timeout"] <= 2.0


def test_conditional_put_honors_cancellation_without_a_request(
    command_runner: MagicMock,
) -> None:
    """Cancellation stops the final PUT before GitHub receives a request."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    call_mock = MagicMock()
    command_runner.side_effect = call_mock
    cancellation = threading.Event()
    cancellation.set()

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=EffectiveMergePolicy(
            base_branch="main",
            default_branch="main",
            required_checks=(RequiredCheck("required-ci", 15368),),
            conversation_resolution_enforced=True,
            bypassable_ruleset_ids=(),
            strict_update_enforced=True,
            merge_queue_method=None,
        ),
        deadline_s=time.monotonic() + 2.0,
        cancellation=cancellation,
    )

    assert result.transport_error is True
    call_mock.assert_not_called()


def test_required_queue_uses_exact_head_graphql_admission(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue mode uses the node ID and reviewed head without native auto-merge."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    graphql_mock = MagicMock(
        return_value={"id": "MQE_node", "state": "QUEUED", "baseCommit": {"oid": "b" * 40}}
    )
    monkeypatch.setattr(adapter, "_graphql_with_timeout", graphql_mock)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=time.monotonic() + 2.0,
        cancellation=threading.Event(),
    )

    assert result.queued is True
    assert result.body == {"merged": False, "queue_entry_id": "MQE_node"}
    spec = graphql_mock.call_args.args[0]
    assert spec.operation == "enqueuePullRequest"
    assert spec.variables == {
        "pullRequestId": "PR_node",
        "expectedHeadOid": "a" * 40,
    }
    assert "enablePullRequestAutoMerge" not in spec.query


def _already_enqueued_error(
    *, operation: str = "enqueuePullRequest"
) -> github_api_mod.MergeQueueAlreadyEnqueuedError:
    """Build the exact typed queue rejection for adapter tests."""
    return github_api_mod.MergeQueueAlreadyEnqueuedError(
        "Pull request is already in the queue",
        intent=github_api_mod.GraphQLMutationIntent(
            operation=operation,
            client_mutation_id="correlation",
            targets=(("pullRequestId", "PR_node"),),
            content_hashes=(),
        ),
    )


def test_required_queue_reconciles_an_existing_exact_head_entry(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated queue admission succeeds only after an exact-head readback."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    graphql_mock = MagicMock(
        side_effect=[
            _already_enqueued_error(),
            {
                "id": "PR_node",
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
            },
        ]
    )
    monkeypatch.setattr(
        adapter,
        "_graphql_with_timeout",
        graphql_mock,
    )
    unbounded_mock = MagicMock(side_effect=AssertionError("unbounded GraphQL readback"))
    monkeypatch.setattr(adapter, "_graphql", unbounded_mock)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=time.monotonic() + 2.0,
        cancellation=threading.Event(),
    )

    assert result.queued is True
    assert result.body == {"merged": False, "queue_entry_id": "MQE_node"}
    assert graphql_mock.call_count == 2
    readback_call = graphql_mock.call_args_list[1]
    assert readback_call.args[0].operation == "pullRequestQueueEntry"
    assert readback_call.kwargs == {"number": 7}
    unbounded_mock.assert_not_called()


def test_required_queue_reconciles_bare_unprocessable_envelope(
    command_runner: MagicMock,
) -> None:
    """A bare UNPROCESSABLE response requires exact-head queue readback."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    call_mock = MagicMock(
        side_effect=[
            _response(
                {
                    "data": {"enqueuePullRequest": None},
                    "errors": [
                        {
                            "type": "UNPROCESSABLE",
                            "message": "Pull request is already in the queue",
                        }
                    ],
                }
            ),
            _response(
                {
                    "data": {
                        "repository": {
                            "owner": {"login": "org"},
                            "name": "repo",
                            "pullRequest": {
                                "id": "PR_node",
                                "number": 7,
                                "state": "OPEN",
                                "headRefOid": "a" * 40,
                                "mergeQueueEntry": {
                                    "id": "MQE_node",
                                    "state": "AWAITING_CHECKS",
                                },
                            },
                        }
                    }
                }
            ),
        ]
    )
    command_runner.side_effect = call_mock
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=time.monotonic() + 2.0,
        cancellation=threading.Event(),
    )

    assert result.queued is True
    assert result.body == {"merged": False, "queue_entry_id": "MQE_node"}
    assert call_mock.call_count == 2


def test_required_queue_rejects_bare_message_for_another_error_type(
    command_runner: MagicMock,
) -> None:
    """Another error type cannot start queue readback."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    call_mock = MagicMock(
        return_value=_response(
            {
                "data": {"enqueuePullRequest": None},
                "errors": [
                    {
                        "type": "FORBIDDEN",
                        "message": "Pull request is already in the queue",
                    }
                ],
            }
        )
    )
    command_runner.side_effect = call_mock
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=time.monotonic() + 2.0,
        cancellation=threading.Event(),
    )

    assert result.malformed is True
    assert result.queued is False
    call_mock.assert_called_once()


def test_required_queue_readback_uses_only_the_remaining_deadline(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation elapsed time is removed from the bounded readback timeout."""
    adapter = pg.PipelineGitHub("org", repo="repo", gh_timeout=120, command_runner=command_runner)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    timeouts: list[float] = []

    def graphql(spec: object, timeout: float, **fields: int | str) -> dict[str, object]:
        timeouts.append(timeout)
        if getattr(spec, "operation", "") == "enqueuePullRequest":
            now[0] = 101.25
            raise _already_enqueued_error()
        assert fields == {"number": 7}
        return {
            "id": "PR_node",
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
        }

    monkeypatch.setattr(adapter, "_graphql_with_timeout", graphql)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=102.0,
        cancellation=threading.Event(),
    )

    assert result.queued is True
    assert timeouts == pytest.approx([2.0, 0.75])


def test_required_queue_exhausted_deadline_stops_already_queued_readback(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admission response after the deadline cannot start a readback."""
    adapter = pg.PipelineGitHub("org", repo="repo", gh_timeout=120, command_runner=command_runner)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    graphql_mock = MagicMock()

    def finish_after_deadline(*_args: object, **_kwargs: object) -> None:
        now[0] = 102.25
        raise _already_enqueued_error()

    graphql_mock.side_effect = finish_after_deadline
    monkeypatch.setattr(adapter, "_graphql_with_timeout", graphql_mock)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=102.0,
        cancellation=threading.Event(),
    )

    assert result.malformed is True
    assert result.queued is False
    assert graphql_mock.call_count == 1


def test_required_queue_cancellation_stops_already_queued_readback(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after admission prevents the read-only reconciliation call."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    cancellation = threading.Event()
    graphql_mock = MagicMock()

    def cancel_after_admission(*_args: object, **_kwargs: object) -> None:
        cancellation.set()
        raise _already_enqueued_error()

    graphql_mock.side_effect = cancel_after_admission
    monkeypatch.setattr(adapter, "_graphql_with_timeout", graphql_mock)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=time.monotonic() + 2.0,
        cancellation=cancellation,
    )

    assert result.malformed is True
    assert result.queued is False
    assert graphql_mock.call_count == 1


@pytest.mark.parametrize("stop", ["cancellation", "deadline"])
def test_required_queue_rejects_a_matching_readback_that_finishes_too_late(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    """A stop condition during readback cannot authorize queue success."""
    adapter = pg.PipelineGitHub("org", repo="repo", gh_timeout=120, command_runner=command_runner)
    cancellation = threading.Event()
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    graphql_mock = MagicMock()

    def graphql(spec: object, _timeout: float, **fields: int | str) -> dict[str, object]:
        if getattr(spec, "operation", "") == "enqueuePullRequest":
            raise _already_enqueued_error()
        assert fields == {"number": 7}
        if stop == "cancellation":
            cancellation.set()
        else:
            now[0] = 102.0
        return {
            "id": "PR_node",
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
        }

    graphql_mock.side_effect = graphql
    monkeypatch.setattr(adapter, "_graphql_with_timeout", graphql_mock)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=102.0,
        cancellation=cancellation,
    )

    assert result.malformed is True
    assert result.queued is False
    assert graphql_mock.call_count == 2


@pytest.mark.parametrize(
    "error",
    [
        github_api_mod.GraphQLResponseError("Pull request is already in the queue"),
        github_api_mod.GraphQLMutationOutcomeUnknownError(
            "Pull request is already in the queue",
            intent=github_api_mod.GraphQLMutationIntent(
                operation="updatePullRequestReviewComment",
                client_mutation_id="correlation",
                targets=(("id", "COMMENT"),),
                content_hashes=(),
            ),
        ),
    ],
    ids=("ordinary-response", "other-operation"),
)
def test_required_queue_does_not_reconcile_untyped_same_text_errors(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Only the dedicated enqueue error permits a queue-entry readback."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    graphql_mock = MagicMock(side_effect=error)
    monkeypatch.setattr(adapter, "_graphql_with_timeout", graphql_mock)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        cancellation=threading.Event(),
    )

    assert result.malformed is True
    assert result.queued is False
    assert graphql_mock.call_count == 1


@pytest.mark.parametrize(
    "readback",
    [
        {
            "id": "OTHER_PR",
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
        },
        {
            "id": "PR_node",
            "state": "CLOSED",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
        },
        {
            "id": "PR_node",
            "state": "OPEN",
            "headRefOid": "b" * 40,
            "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
        },
        {
            "id": "PR_node",
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": None,
        },
    ],
    ids=("different-pr", "closed-pr", "head-drift", "missing-entry"),
)
def test_required_queue_rejects_a_mismatched_existing_entry(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    readback: dict[str, object],
) -> None:
    """Readback must prove the same open pull request and exact reviewed head."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    graphql_mock = MagicMock(side_effect=[_already_enqueued_error(), readback])
    monkeypatch.setattr(
        adapter,
        "_graphql_with_timeout",
        graphql_mock,
    )
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        deadline_s=time.monotonic() + 2.0,
        cancellation=threading.Event(),
    )

    assert result.malformed is True
    assert result.queued is False
    assert graphql_mock.call_count == 2


def test_required_queue_rejects_an_unavailable_existing_entry(
    command_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed queue-entry query cannot become successful admission."""
    adapter = pg.PipelineGitHub("org", repo="repo", command_runner=command_runner)
    graphql_mock = MagicMock(
        side_effect=[
            _already_enqueued_error(),
            github_api_mod.GraphQLDeterministicError("readback failed"),
        ]
    )
    monkeypatch.setattr(adapter, "_graphql_with_timeout", graphql_mock)
    policy = EffectiveMergePolicy(
        base_branch="main",
        default_branch="main",
        required_checks=(RequiredCheck("required-ci", 15368),),
        conversation_resolution_enforced=True,
        bypassable_ruleset_ids=(15556494,),
        strict_update_enforced=False,
        merge_queue_method="SQUASH",
    )

    result = adapter.merge_pr_if_head(
        7,
        "a" * 40,
        policy=policy,
        pull_request_id="PR_node",
        cancellation=threading.Event(),
    )

    assert result.malformed is True
    assert result.queued is False
    assert graphql_mock.call_count == 2
