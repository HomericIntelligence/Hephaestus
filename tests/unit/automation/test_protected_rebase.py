"""Behavioral tests for protected rebase contracts and plan builders."""

from __future__ import annotations

import pytest

from hephaestus.automation.protected_rebase import (
    BRIDGE_SLOT,
    MERGE_BASE_SLOT,
    NEW_TIP_SLOT,
    PROTECTED_REBASE_FORK_HEAD,
    PROTECTED_REBASE_TARGET_UNVERIFIED,
    BridgeMetadata,
    ProtectedRebaseBridgeError,
    ProtectedRebaseCapabilityError,
    ProtectedRebaseTarget,
    ProtectedRebaseValidationError,
    WriterAllocationOrigin,
    logical_head_for_cycle,
    parse_bridge_metadata,
    pop_writer_capability,
    protected_rebase_git_plan,
    register_writer_capability,
    render_bridge_message,
    substitute_plan_slots,
    validate_protected_rebase_target,
    validate_protected_writer_capability,
)

BASE = "a" * 40
HEAD = "b" * 40
TIP = "c" * 40
BRIDGE = "d" * 40
PREVIOUS = "e" * 40
SIGNOFF = "Signed-off-by: Worker <worker@example.com>"


def _readiness(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mergeStateStatus": "BEHIND",
        "mergeable": "MERGEABLE",
        "headRefOid": HEAD,
        "baseRefName": "main",
        "baseRefOid": BASE,
        "headRefName": "issue-17",
        "headRepository": {"name": "hephaestus"},
        "headRepositoryOwner": {"login": "acme"},
    }
    payload.update(overrides)
    return payload


def _validate(payload: object) -> ProtectedRebaseTarget | str:
    return validate_protected_rebase_target(
        payload,
        repository="acme/hephaestus",
        pr_number=17,
        expected_head_sha=HEAD,
        default_branch="main",
    )


def test_valid_behind_target_is_accepted() -> None:
    """A complete same-repository BEHIND payload proves the rebase target."""
    target = _validate(_readiness())
    assert target == ProtectedRebaseTarget(
        repository="acme/hephaestus",
        pr_number=17,
        branch="issue-17",
        head_sha=HEAD,
        base_sha=BASE,
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "BEHIND",
        {"mergeStateStatus": "CLEAN"},
        _readiness(mergeable="CONFLICTING"),
        _readiness(headRefOid=TIP),
        _readiness(headRefOid="b" * 39),
        _readiness(baseRefName="release"),
        _readiness(baseRefOid=None),
        _readiness(baseRefOid="a" * 39),
        _readiness(headRefName=""),
        _readiness(headRefName="main"),
        _readiness(headRefName=None),
        _readiness(headRepository=None),
        _readiness(headRepositoryOwner="acme"),
    ],
)
def test_missing_or_malformed_fields_make_the_target_unverified(payload: object) -> None:
    """Each missing or malformed readiness field fails closed as unverified."""
    assert _validate(payload) == PROTECTED_REBASE_TARGET_UNVERIFIED


def test_not_behind_is_unverified() -> None:
    """A ready status is not a protected rebase target."""
    assert _validate(_readiness(mergeStateStatus="CLEAN")) == PROTECTED_REBASE_TARGET_UNVERIFIED


def test_foreign_head_repository_is_a_fork_head() -> None:
    """A head repository outside the verified identity is a fork head."""
    assert _validate(_readiness(headRepository={"name": "fork"})) == PROTECTED_REBASE_FORK_HEAD
    assert (
        _validate(_readiness(headRepositoryOwner={"login": "forker"})) == PROTECTED_REBASE_FORK_HEAD
    )


def test_target_rejects_malformed_identity() -> None:
    """The target dataclass validates its own immutable identity."""
    with pytest.raises(ProtectedRebaseValidationError):
        ProtectedRebaseTarget(
            repository="acme", pr_number=17, branch="issue-17", head_sha=HEAD, base_sha=BASE
        )
    with pytest.raises(ProtectedRebaseValidationError):
        ProtectedRebaseTarget(
            repository="acme/hephaestus",
            pr_number=0,
            branch="issue-17",
            head_sha=HEAD,
            base_sha=BASE,
        )


def _metadata(cycle: int = 1) -> BridgeMetadata:
    return BridgeMetadata(
        issue_number=2455,
        pr_number=17,
        cycle=cycle,
        base_sha=BASE,
        previous_head_sha=PREVIOUS,
        logical_tip_sha=TIP,
        signoff=SIGNOFF,
    )


def test_bridge_message_round_trip() -> None:
    """A rendered bridge message parses back into the same record."""
    metadata = _metadata(cycle=3)
    parsed = parse_bridge_metadata(render_bridge_message(metadata))
    assert parsed == metadata


@pytest.mark.parametrize(
    "message",
    [
        "",
        "no trailers",
        f"Protected-Rebase-Format: 2\n{SIGNOFF}",
        f"Protected-Rebase-Format: 1\n{SIGNOFF}\n{SIGNOFF}",
        "Protected-Rebase-Format: 1",
    ],
)
def test_malformed_bridge_messages_are_rejected(message: str) -> None:
    """A bridge message without a valid format-1 record and one sign-off fails."""
    with pytest.raises(ProtectedRebaseValidationError):
        parse_bridge_metadata(message)


def test_bridge_metadata_validates_cycle_range_and_shas() -> None:
    """A cycle outside 1..8 or a malformed SHA is rejected."""
    with pytest.raises(ProtectedRebaseValidationError):
        _metadata(cycle=9)
    with pytest.raises(ProtectedRebaseValidationError):
        BridgeMetadata(
            issue_number=2455,
            pr_number=17,
            cycle=1,
            base_sha="not-a-sha",
            previous_head_sha=PREVIOUS,
            logical_tip_sha=TIP,
            signoff=SIGNOFF,
        )


def test_first_cycle_logical_head_is_the_previous_head() -> None:
    """Cycle 1 rebases the range from the previous published head."""
    assert (
        logical_head_for_cycle(1, previous_head_sha=PREVIOUS, prior_bridge_message=None) == PREVIOUS
    )


def test_later_cycles_unwrap_the_prior_bridge_logical_tip() -> None:
    """Cycles 2 to 8 unwrap and validate the prior bridge's signed metadata."""
    message = render_bridge_message(_metadata(cycle=2))
    assert logical_head_for_cycle(3, previous_head_sha=BRIDGE, prior_bridge_message=message) == TIP


def test_later_cycles_reject_a_mismatched_prior_cycle() -> None:
    """A prior bridge that does not record cycle-1 fails closed."""
    message = render_bridge_message(_metadata(cycle=1))
    with pytest.raises(ProtectedRebaseBridgeError):
        logical_head_for_cycle(3, previous_head_sha=BRIDGE, prior_bridge_message=message)
    with pytest.raises(ProtectedRebaseValidationError):
        logical_head_for_cycle(2, previous_head_sha=BRIDGE, prior_bridge_message="garbage")


def _plan() -> tuple[tuple[str, ...], ...]:
    return protected_rebase_git_plan(
        logical_head_sha=HEAD,
        new_base_sha=BASE,
        previous_head_sha=PREVIOUS,
        branch="issue-17",
        default_branch="main",
    )


def test_git_plan_uses_signed_ordinary_operations_only() -> None:
    """The Git plan contains only ordinary operations with signing requests."""
    plan = _plan()
    flat = [token for argv in plan for token in argv]
    assert "--gpg-sign" in flat
    assert "-S" in flat
    assert flat.count("rebase") == 1
    banned = ("--force", "--force-with-lease", "arm_auto_merge", "merge_pr_if_head")
    for token in banned:
        assert token not in flat
    push = plan[-1]
    assert push[:3] == ("git", "push", "origin")
    assert push[3] == f"{BRIDGE_SLOT}:refs/heads/issue-17"
    assert all(not token.startswith("--force") for token in push)


def test_git_plan_verifies_no_merge_commits_and_both_ancestries() -> None:
    """The plan scans the replayed range and proves both bridge parents."""
    plan = _plan()
    assert ("git", "rev-list", "--merges", f"{MERGE_BASE_SLOT}..HEAD") in plan
    assert (
        "git",
        "merge-base",
        "--is-ancestor",
        PREVIOUS,
        BRIDGE_SLOT,
    ) in plan
    assert ("git", "merge-base", "--is-ancestor", BASE, BRIDGE_SLOT) in plan
    assert (
        "git",
        "ls-remote",
        "origin",
        "refs/heads/main",
        "refs/heads/issue-17",
    ) in plan


def test_git_plan_rejects_malformed_inputs() -> None:
    """The plan builder validates SHAs and refuses the default branch."""
    with pytest.raises(ProtectedRebaseValidationError):
        protected_rebase_git_plan(
            logical_head_sha="sha",
            new_base_sha=BASE,
            previous_head_sha=PREVIOUS,
            branch="issue-17",
            default_branch="main",
        )
    with pytest.raises(ProtectedRebaseValidationError):
        protected_rebase_git_plan(
            logical_head_sha=HEAD,
            new_base_sha=BASE,
            previous_head_sha=PREVIOUS,
            branch="main",
            default_branch="main",
        )


def test_plan_slot_substitution_is_exact_token_replacement() -> None:
    """Runtime values replace whole marked tokens without touching flags."""
    argv = ("git", "commit-tree", f"{NEW_TIP_SLOT}^{{tree}}", "-p", NEW_TIP_SLOT)
    substituted = substitute_plan_slots(argv, {NEW_TIP_SLOT: TIP})
    assert substituted == ("git", "commit-tree", f"{TIP}^{{tree}}", "-p", TIP)


@pytest.fixture(autouse=True)
def _clean_capability_registry() -> None:
    """Keep the process-local capability registry empty between tests."""
    yield
    pop_writer_capability(_REGISTERED_TOKENS.pop()) if _REGISTERED_TOKENS else None
    while _REGISTERED_TOKENS:
        pop_writer_capability(_REGISTERED_TOKENS.pop())


_REGISTERED_TOKENS: list[str] = []


def _register(
    origin: WriterAllocationOrigin = WriterAllocationOrigin.NEWLY_CREATED,
) -> object:
    item = object()
    record = register_writer_capability(
        repository="acme/hephaestus",
        issue_number=17,
        branch="issue-17",
        worktree="/repo/.worktrees/impl-17",
        origin=origin,
        item=item,
    )
    _REGISTERED_TOKENS.append(record.capability.token)
    return record


def test_capability_mints_only_for_allow_listed_origins() -> None:
    """Adopted and reused allocations never receive a protected capability."""
    for origin in (WriterAllocationOrigin.ADOPTED, WriterAllocationOrigin.REUSED):
        with pytest.raises(ProtectedRebaseCapabilityError):
            _register(origin)
    for origin in (
        WriterAllocationOrigin.NEWLY_CREATED,
        WriterAllocationOrigin.DIRECT_ABSENT_RESERVED,
    ):
        record = _register(origin)
        assert record.origin is origin


def test_capability_validation_requires_an_exact_match() -> None:
    """Every compared field must match the registered record."""
    record = _register()
    token = record.capability.token
    validated = validate_protected_writer_capability(
        token,
        repository="acme/hephaestus",
        issue_number=17,
        branch="issue-17",
        worktree="/repo/.worktrees/impl-17",
    )
    assert validated is record
    base = {
        "repository": "acme/hephaestus",
        "issue_number": 17,
        "branch": "issue-17",
        "worktree": "/repo/.worktrees/impl-17",
    }
    for overrides in (
        {"repository": "acme/other"},
        {"issue_number": 18},
        {"branch": "issue-18"},
        {"worktree": "/repo/.worktrees/impl-18"},
    ):
        with pytest.raises(ProtectedRebaseCapabilityError):
            validate_protected_writer_capability(token, **{**base, **overrides})


def test_missing_or_malformed_capability_fails_closed() -> None:
    """An absent registry entry or a malformed token grants no authority."""
    with pytest.raises(ProtectedRebaseCapabilityError):
        validate_protected_writer_capability(
            "0" * 64,
            repository="acme/hephaestus",
            issue_number=17,
            branch="issue-17",
            worktree="/repo/.worktrees/impl-17",
        )
    with pytest.raises(ProtectedRebaseCapabilityError):
        validate_protected_writer_capability(
            "short",
            repository="acme/hephaestus",
            issue_number=17,
            branch="issue-17",
            worktree="/repo/.worktrees/impl-17",
        )


def test_pop_clears_a_registered_capability() -> None:
    """The terminal sink removes the registration permanently."""
    record = _register()
    token = record.capability.token
    assert pop_writer_capability(token) is record
    assert pop_writer_capability(token) is None
    with pytest.raises(ProtectedRebaseCapabilityError):
        validate_protected_writer_capability(
            token,
            repository="acme/hephaestus",
            issue_number=17,
            branch="issue-17",
            worktree="/repo/.worktrees/impl-17",
        )
