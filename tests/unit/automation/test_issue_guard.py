"""Tests for ref-backed issue ownership guards."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from hephaestus.automation import recover_issue_guard
from hephaestus.automation.issue_guard import (
    _RECOVERY_GRACE,
    GitHubIssueGuardStore,
    GuardConflictError,
    GuardCredential,
    GuardLostError,
    GuardPhase,
    GuardRecord,
    GuardUnavailableError,
    InMemoryGuardStore,
    IssueGuard,
    assert_recovery_secret_absent,
)
from hephaestus.automation.pipeline.guarded_github import (
    GuardedStageGitHub,
    GuardTargetError,
)
from hephaestus.automation.state_labels import (
    STATE_IN_PROGRESS,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
)


def test_only_one_concurrent_worker_acquires_the_issue() -> None:
    """Two workers racing on one issue produce one durable owner."""
    store = InMemoryGuardStore()

    def acquire() -> object:
        return IssueGuard(store).acquire("Owner/Repo", 2404, "planning")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: acquire(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert STATE_IN_PROGRESS in store.labels[("Owner/Repo", 2404)]


def test_released_claim_can_be_reacquired_with_a_new_identity() -> None:
    """A terminal predecessor does not become a permanent local lock."""
    store = InMemoryGuardStore()
    first_service = IssueGuard(store)
    first = first_service.acquire("Owner/Repo", 2404, "planning")
    assert first is not None
    first_service.release(first, "completed")

    second = IssueGuard(store).acquire("Owner/Repo", 2404, "implementation")

    assert second is not None
    assert second.credential.claim_id != first.credential.claim_id
    assert second.record.phase is GuardPhase.ACTIVE


def test_non_owner_cannot_confirm_or_recover_a_live_claim() -> None:
    """Claim and ref identity are required for both owner operations."""
    store = InMemoryGuardStore()
    owner = IssueGuard(store)
    handle = owner.acquire("Owner/Repo", 2404, "planning")
    assert handle is not None

    with pytest.raises(GuardLostError):
        owner.confirm(GuardCredential("Owner/Repo", 2404, uuid4(), uuid4()), timedelta(0))
    with pytest.raises(GuardConflictError):
        owner.recover(
            "Owner/Repo",
            2404,
            expected_claim=handle.credential.claim_id,
            expected_oid="0" * 40,
            reason="wrong ref",
            actor="operator",
        )


def test_recovery_requires_expired_lease_and_preserves_plan_blocked() -> None:
    """Explicit recovery clears only the guard, never the operator latch."""
    store = InMemoryGuardStore()
    store.labels[("Owner/Repo", 2404)] = {STATE_PLAN_BLOCKED}
    owner = IssueGuard(store)
    handle = owner.acquire("Owner/Repo", 2404, "plan-review")
    assert handle is not None

    store.now = handle.record.lease_expires_at + _RECOVERY_GRACE + timedelta(seconds=1)
    recovered = IssueGuard(store).recover(
        "Owner/Repo",
        2404,
        expected_claim=handle.credential.claim_id,
        expected_oid=handle.oid,
        reason="runner abandoned after timeout",
        actor="operator",
    )

    assert recovered.record.phase is GuardPhase.RECOVERED
    assert STATE_IN_PROGRESS not in store.labels[("Owner/Repo", 2404)]
    assert STATE_PLAN_BLOCKED in store.labels[("Owner/Repo", 2404)]


def test_guard_record_rejects_noncanonical_timestamp() -> None:
    """The ref payload must have one exact JSON and timestamp encoding."""
    store = InMemoryGuardStore()
    handle = IssueGuard(store).acquire("Owner/Repo", 2404, "planning")
    assert handle is not None
    payload = handle.record.to_json().replace('Z"', '+00:00"')

    with pytest.raises(ValueError):
        GuardRecord.from_json(payload)


def test_owner_can_renew_and_release_a_guard() -> None:
    """Renewal extends the lease and release records a terminal predecessor."""
    store = InMemoryGuardStore()
    service = IssueGuard(store)
    handle = service.acquire("Owner/Repo", 2404, "implementation")
    assert handle is not None

    fresh = service.renew(handle, timedelta(minutes=1))
    assert fresh.oid == handle.oid
    store.now = fresh.record.lease_expires_at - timedelta(minutes=1)
    renewed = service.renew(fresh, timedelta(hours=2))
    assert renewed.oid != fresh.oid
    service.release(renewed, "completed successfully")

    assert store.refs[("Owner/Repo", 2404)].record.phase is GuardPhase.RELEASED
    assert STATE_IN_PROGRESS not in store.labels[("Owner/Repo", 2404)]


def test_guard_rejects_expired_release_and_short_confirmation() -> None:
    """An expired owner cannot clear a claim or dispatch new work."""
    store = InMemoryGuardStore()
    service = IssueGuard(store)
    handle = service.acquire("Owner/Repo", 2404, "planning")
    assert handle is not None
    store.now = handle.record.lease_expires_at

    with pytest.raises(GuardLostError):
        service.confirm(handle.credential, timedelta(0))
    with pytest.raises(GuardLostError):
        service.release(handle, "expired")


def test_guard_record_round_trips_and_recovery_secret_is_rejected() -> None:
    """Canonical records remain stable and recovery credentials stay isolated."""
    store = InMemoryGuardStore()
    handle = IssueGuard(store).acquire("Owner/Repo", 2404, "planning")
    assert handle is not None
    assert GuardRecord.from_json(handle.record.to_json()) == handle.record

    assert_recovery_secret_absent({})
    with pytest.raises(GuardUnavailableError):
        assert_recovery_secret_absent({"HEPHAESTUS_GUARD_RECOVERY_TOKEN": "operator"})


def test_http_guard_store_uses_server_time_and_non_force_refs() -> None:
    """REST storage parses Date headers and sends explicit non-forced ref writes."""
    record = GuardRecord(
        version=1,
        repository="Owner/Repo",
        issue=2404,
        claim_id=uuid4(),
        run_id=uuid4(),
        actor="automation",
        phase=GuardPhase.ACTIVE,
        work_stage="planning",
        lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        predecessor_oid="1" * 40,
        reason="test",
    )
    commit_oid = "2" * 40
    tree_oid = "3" * 40
    calls: list[tuple[list[str], dict[str, Any]]] = []
    date = "Tue, 01 Jan 2030 00:00:00 GMT"

    def response(status: int, body: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["gh"],
            0,
            stdout=f"HTTP/1.1 {status} OK\r\nDate: {date}\r\n\r\n{json.dumps(body)}",
            stderr="",
        )

    def call(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        paths = {arg for arg in args if arg.startswith("repos/Owner/Repo/")}
        if args[-1] == "user":
            return response(200, {"login": "operator"})
        if any(path.endswith("issues/2404") for path in paths):
            return response(200, {"labels": [{"name": STATE_PLAN_GO}, {"name": 3}]})
        if any(path.endswith("git/ref/heads/main") for path in paths):
            return response(200, {"object": {"sha": "4" * 40}})
        if any(path.endswith("git/ref/heads/hephaestus/issue-guards/issue-2404") for path in paths):
            return response(200, {"object": {"sha": commit_oid}})
        if any(path.endswith(f"git/commits/{'4' * 40}") for path in paths):
            return response(200, {"tree": {"sha": tree_oid}, "message": "tip"})
        if any("/git/commits/" in path for path in paths):
            return response(200, {"tree": {"sha": tree_oid}, "message": record.to_json()})
        if any(path.endswith("git/commits") for path in paths):
            return response(201, {"sha": commit_oid})
        if any(path.endswith("git/refs") for path in paths) or any(
            "issue-2404" in path for path in paths
        ):
            return response(201, {})
        if any(path.endswith("Owner/Repo/") for path in paths):
            return response(200, {"default_branch": "main"})
        return response(200, {})

    github = GitHubIssueGuardStore("Owner/Repo", call=call, env={"GH_TOKEN": "test"})
    assert github.read_labels("Owner/Repo", 2404) == (STATE_PLAN_GO,)
    github.add_label("Owner/Repo", 2404, "state:extra")
    github.remove_label("Owner/Repo", 2404, "state:extra")
    assert github.actor() == "operator"
    assert github.default_tip("Owner/Repo") == ("4" * 40, tree_oid)
    assert github.create_commit("Owner/Repo", tree_oid, ["4" * 40], record.to_json())[0]
    github.create_ref("Owner/Repo", 2404, commit_oid)
    github.update_ref("Owner/Repo", 2404, commit_oid, "1" * 40)
    snapshot = github.read_ref("Owner/Repo", 2404)
    assert snapshot is not None and snapshot.record == record
    assert all(kwargs["env"] == {"GH_TOKEN": "test"} for _args, kwargs in calls)
    update = next(args for args, _kwargs in calls if "force=false" in args)
    assert "force=false" in update


class _FakeGitHub:
    """Small mutation surface used to verify the target-bound proxy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def find_issue_for_pr(self, pr_number: int) -> int:
        return 2404

    def __getattr__(self, name: str) -> Any:
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, (args, kwargs)))
            return 99 if name == "create_pr" else True

        return method


def test_guarded_proxy_confirms_target_and_blocks_guard_label_mutation() -> None:
    """Workers cannot retarget a credential or mutate its ownership label."""
    store = InMemoryGuardStore()
    handle = IssueGuard(store).acquire("Owner/Repo", 2404, "planning")
    assert handle is not None
    proxy = GuardedStageGitHub(_FakeGitHub(), store, handle.credential)

    proxy.add_labels(2404, [STATE_PLAN_GO])
    assert proxy.raw.calls == [("add_labels", ((2404, [STATE_PLAN_GO]), {}))]
    with pytest.raises(GuardTargetError):
        proxy.add_labels(2405, [STATE_PLAN_GO])
    with pytest.raises(GuardTargetError):
        proxy.add_labels(2404, [STATE_IN_PROGRESS])


def test_guarded_proxy_covers_issue_and_pr_mutation_surface() -> None:
    """Every explicit proxy mutation confirms the issue or linked PR first."""
    store = InMemoryGuardStore()
    handle = IssueGuard(store).acquire("Owner/Repo", 2404, "implementation")
    assert handle is not None
    raw = _FakeGitHub()
    proxy = GuardedStageGitHub(raw, store, handle.credential)

    proxy.ensure_blocked_audit(2404)
    proxy.remove_labels(2404, [STATE_PLAN_GO])
    proxy.edit_labels(2404, add=[STATE_PLAN_GO], remove=[])
    proxy.close_issue_as_covered(2404, 7)
    proxy.upsert_issue_comment(2404, "marker", "body")
    proxy.append_issue_comment(2404, "marker", "body")
    proxy.upsert_plan_comment(2404, "plan")
    proxy.post_implementation_thread_replies(
        7, expected_head_sha="a" * 40, threads=[], replies={}, batch_nonce="nonce"
    )
    proxy.reconcile_reviewer_validated_threads(
        7, reviewed_head_sha="a" * 40, receipts=[], resolved_thread_ids=set(), feedback={}
    )
    assert proxy.create_pr(2404, "branch", "title", "body") == 99
    proxy.mark_pr_implementation_no_go(7)
    proxy.post_review_threads(7, [], expected_head_sha="a" * 40)
    proxy.mark_pr_implementation_go(7)
    proxy.merge_pr_if_head(7, "a" * 40)
    assert proxy.drive_green_learn_terminal(2404)
    assert proxy.drive_green_learn_inflight(2404)
    assert proxy.claim_drive_green_learn(2404, 7)
    proxy.mark_drive_green_learn_result(2404, succeeded=True)
    proxy.skip_epics({2404: []})
    with pytest.raises(GuardTargetError):
        proxy.ensure_state_labels()


def test_recovery_cli_inspects_without_recovery_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inspection is read-only and does not require the operator token."""

    class EmptyStore:
        def __init__(self, _repository: str) -> None:
            pass

        def read_ref(self, _repository: str, _issue: int) -> None:
            return None

        def read_labels(self, _repository: str, _issue: int) -> tuple[str, ...]:
            return ()

    monkeypatch.delenv("HEPHAESTUS_GUARD_RECOVERY_TOKEN", raising=False)
    monkeypatch.setattr(recover_issue_guard, "GitHubIssueGuardStore", EmptyStore)

    assert recover_issue_guard.main(["--repo", "Owner/Repo", "--issue", "2404", "--inspect"]) == 0
    assert '"guard": null' in capsys.readouterr().out


def test_recovery_cli_rejects_incomplete_recovery_request() -> None:
    """Recovery mode requires all expected-identity evidence before I/O."""
    assert recover_issue_guard.main(["--repo", "Owner/Repo", "--issue", "2404", "--recover"]) == 1


def test_recovery_cli_enforces_operator_actor_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid recovery token cannot authorize an unlisted GitHub actor."""
    monkeypatch.setenv("HEPHAESTUS_GUARD_RECOVERY_TOKEN", "operator-token")
    monkeypatch.setenv("HEPHAESTUS_GUARD_RECOVERY_ACTORS", "trusted-operator")
    monkeypatch.setenv("GH_TOKEN", "normal-token")

    class ActorStore:
        def __init__(self, _repository: str, *, env: dict[str, str]) -> None:
            assert env["GH_TOKEN"]

        def actor(self) -> str:
            return "unexpected-actor"

    monkeypatch.setattr(recover_issue_guard, "GitHubIssueGuardStore", ActorStore)
    assert (
        recover_issue_guard.main(
            [
                "--repo",
                "Owner/Repo",
                "--issue",
                "2404",
                "--recover",
                "--expected-claim",
                str(uuid4()),
                "--expected-oid",
                "a" * 40,
                "--reason",
                "operator recovery",
            ]
        )
        == 1
    )
