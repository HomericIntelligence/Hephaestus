"""Tests for the frozen Codex isolation-adapter protocol."""

from __future__ import annotations

import dataclasses
import importlib
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def _module() -> Any:
    return importlib.import_module("hephaestus.agents.codex_isolation")


def _digest(value: Any) -> str:
    return _module().canonical_sha256(value)


def _git_receipt(tmp_path: Path) -> Any:
    iso = _module()
    worktree = str((tmp_path / "worktree").resolve())
    git_dir = str((tmp_path / "git-dir").resolve())
    common_dir = str((tmp_path / "common-dir").resolve())
    index = str((tmp_path / "index").resolve())
    repository_config = str((tmp_path / "config").resolve())
    worktree_config = str((tmp_path / "config.worktree").resolve())
    fixed_environment = (
        ("GIT_ATTR_NOSYSTEM", "1"),
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("GIT_DIR", git_dir),
        ("GIT_INDEX_FILE", index),
        ("GIT_OPTIONAL_LOCKS", "0"),
        ("GIT_WORK_TREE", worktree),
    )
    identities = (("git_dir", (1, 2, 0o40700, 1000, 0, 10)),)
    digests = (("index", "2" * 64), ("pointer", "1" * 64))
    return iso.CodexGitReceiptV1(
        schema_version=1,
        canonical_worktree=worktree,
        git_dir=git_dir,
        common_dir=common_dir,
        index=index,
        repository_config=repository_config,
        worktree_config=worktree_config,
        fixed_environment=fixed_environment,
        protected_paths=(str((tmp_path / "worktree" / ".git").resolve()),),
        read_only_paths=(git_dir, common_dir, index, repository_config, worktree_config),
        read_write_paths=(worktree,),
        identities=identities,
        digests=digests,
    )


def _policy(tmp_path: Path) -> Any:
    iso = _module()
    worktree = str((tmp_path / "worktree").resolve())
    return iso.CodexExecutionPolicyV1(
        schema_version=1,
        read_only_mounts=(str((tmp_path / "readonly").resolve()),),
        read_write_mounts=(worktree,),
        protected_overlay_mounts=(str((tmp_path / "worktree" / ".git").resolve()),),
        provider_relay="vsock://2:443",
        command_network="deny",
        max_output_bytes=4096,
        term_grace_seconds=1.0,
        kill_grace_seconds=1.0,
        pipe_close_grace_seconds=1.0,
        inventory_quiescence_seconds=0.25,
        total_deadline=1000.0,
    )


def _request(tmp_path: Path, **changes: Any) -> Any:
    iso = _module()
    policy = _policy(tmp_path)
    receipt = _git_receipt(tmp_path)
    command = ("codex", "exec", "--json", "bound prompt")
    environment = (
        ("CODEX_HOME", str((tmp_path / "profile").resolve())),
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
    )
    identity = (
        "HomericIntelligence/Hephaestus",
        3019,
        "implementer",
        str((tmp_path / "worktree").resolve()),
        "gpt-5.6-sol",
        "session-3019",
    )
    values: dict[str, Any] = {
        "schema_version": 1,
        "run_nonce": "a" * 64,
        "entry_point_name": "production-v1",
        "adapter_api_version": 1,
        "package_version": "1.0.0",
        "deployment_lock_digest": "b" * 64,
        "wheel_digest": "c" * 64,
        "installed_tree_digest": "d" * 64,
        "command": command,
        "command_digest": _digest(command),
        "executable_platform": "linux",
        "executable_target": "aarch64-unknown-linux-musl",
        "executable_release": "rust-v0.153.4",
        "executable_asset_name": "codex-aarch64-unknown-linux-musl.zst",
        "executable_path": str((tmp_path / "codex").resolve()),
        "executable_digest": "e" * 64,
        "executable_file_identity": (1, 2, 0o100500, 1000, 64, 10),
        "guest_image_digest": "f" * 64,
        "environment": environment,
        "environment_digest": _digest(environment),
        "prompt": "Bound issue prompt",
        "prompt_digest": _digest("Bound issue prompt"),
        "worktree_path": str((tmp_path / "worktree").resolve()),
        "private_profile_path": str((tmp_path / "profile").resolve()),
        "policy": policy,
        "policy_digest": _digest(policy),
        "git_receipt": receipt,
        "git_receipt_digest": _digest(receipt),
        "repository": identity[0],
        "issue": identity[1],
        "role": identity[2],
        "worktree_identity": identity[3],
        "model": identity[4],
        "session": identity[5],
        "session_identity_digest": _digest(identity),
        "monotonic_deadline": 900.0,
    }
    values.update(changes)
    return iso.CodexIsolationRequestV1(**values)


def _prepared(request: Any, **changes: Any) -> Any:
    iso = _module()
    values: dict[str, Any] = {
        "schema_version": 1,
        "request_nonce": request.run_nonce,
        "request_digest": _digest(request),
        "guest_boot_nonce": "1" * 64,
        "guest_image_digest": request.guest_image_digest,
        "adapter_package_digest": request.installed_tree_digest,
        "executable_digest": request.executable_digest,
        "elf_platform": "linux",
        "elf_target": "aarch64-unknown-linux-musl",
        "version_output": "codex-cli 0.153.4",
        "guest_file_identity": request.executable_file_identity,
        "invocation_token": "2" * 64,
        "preparation_deadline": 800.0,
    }
    values.update(changes)
    return iso.CodexIsolationPreparedV1(**values)


def _inventory(sequence: int, timestamp: float, *, descendants: tuple[int, ...] = ()) -> Any:
    iso = _module()
    return iso.CodexDescendantInventoryV1(
        schema_version=1,
        sequence=sequence,
        monotonic_timestamp=timestamp,
        complete=True,
        descendants=descendants,
        cgroup_populated=bool(descendants),
    )


def _result(request: Any, prepared: Any, **changes: Any) -> Any:
    iso = _module()
    values: dict[str, Any] = {
        "schema_version": 1,
        "adapter_identity": request.entry_point_name,
        "adapter_version": request.package_version,
        "request_nonce": request.run_nonce,
        "request_digest": _digest(request),
        "guest_boot_nonce": prepared.guest_boot_nonce,
        "prepared_record_digest": _digest(prepared),
        "exit_status": 0,
        "output": "done",
        "error_code": None,
        "term_sent": True,
        "term_timestamp": 10.0,
        "kill_sent": False,
        "kill_timestamp": 11.0,
        "pipes_closed": True,
        "pipe_close_timestamp": 12.0,
        "inventories": (_inventory(1, 12.25), _inventory(2, 12.5)),
        "policy_digest": request.policy_digest,
        "executable_digest": request.executable_digest,
        "git_receipt_digest": request.git_receipt_digest,
        "session_identity_digest": request.session_identity_digest,
    }
    values.update(changes)
    return iso.CodexIsolationResultV1(**values)


def _linux_elf(payload: bytes = b"locked") -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[6] = 1
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = (183).to_bytes(2, "little")
    return bytes(header) + payload


def test_missing_adapter_fails_before_profile_probe_or_process(tmp_path: Path) -> None:
    """A missing selection stops implementation before a host side effect."""
    from hephaestus.agents import runtime
    from hephaestus.agents.codex_isolation import CodexIsolationError
    from hephaestus.agents.execution_policy import (
        AgentOperation,
        AgentRole,
        ExecutionRequest,
        SessionLifecycle,
    )

    request = ExecutionRequest(
        AgentRole.IMPLEMENTER,
        AgentOperation.IMPLEMENT,
        SessionLifecycle.START_NEW,
    )
    with (
        patch("hephaestus.agents.runtime._codex_child_env") as profile,
        patch("hephaestus.agents.runtime.run_codex_session") as process,
        pytest.raises(CodexIsolationError, match="codex_adapter_not_selected"),
    ):
        runtime.run_agent_session(
            "codex",
            "implement",
            cwd=tmp_path,
            timeout=30,
            execution_request=request,
        )

    profile.assert_not_called()
    process.assert_not_called()


def test_version_1_wire_and_adapter_contract_stays_frozen() -> None:
    """Production keeps the accepted version-1 contract exact."""
    iso = _module()

    assert tuple(field.name for field in dataclasses.fields(iso.CodexIsolationRequestV1)) == (
        "schema_version",
        "run_nonce",
        "entry_point_name",
        "adapter_api_version",
        "package_version",
        "deployment_lock_digest",
        "wheel_digest",
        "installed_tree_digest",
        "command",
        "command_digest",
        "executable_platform",
        "executable_target",
        "executable_release",
        "executable_asset_name",
        "executable_path",
        "executable_digest",
        "executable_file_identity",
        "guest_image_digest",
        "environment",
        "environment_digest",
        "prompt",
        "prompt_digest",
        "worktree_path",
        "private_profile_path",
        "policy",
        "policy_digest",
        "git_receipt",
        "git_receipt_digest",
        "repository",
        "issue",
        "role",
        "worktree_identity",
        "model",
        "session",
        "session_identity_digest",
        "monotonic_deadline",
    )
    assert tuple(field.name for field in dataclasses.fields(iso.CodexIsolationPreparedV1)) == (
        "schema_version",
        "request_nonce",
        "request_digest",
        "guest_boot_nonce",
        "guest_image_digest",
        "adapter_package_digest",
        "executable_digest",
        "elf_platform",
        "elf_target",
        "version_output",
        "guest_file_identity",
        "invocation_token",
        "preparation_deadline",
    )
    assert tuple(field.name for field in dataclasses.fields(iso.CodexIsolationResultV1)) == (
        "schema_version",
        "adapter_identity",
        "adapter_version",
        "request_nonce",
        "request_digest",
        "guest_boot_nonce",
        "prepared_record_digest",
        "exit_status",
        "output",
        "error_code",
        "term_sent",
        "term_timestamp",
        "kill_sent",
        "kill_timestamp",
        "pipes_closed",
        "pipe_close_timestamp",
        "inventories",
        "policy_digest",
        "executable_digest",
        "git_receipt_digest",
        "session_identity_digest",
    )
    assert {
        name
        for name, value in vars(iso.CodexIsolationAdapterV1).items()
        if callable(value) and not name.startswith("_")
    } == {"prepare", "invoke", "destroy"}


def test_protocol_exports_no_unadmitted_loader_or_invocation_helper() -> None:
    """The public protocol cannot bypass automation-owned admission."""
    iso = _module()

    assert not hasattr(iso, "load_adapter_factory")
    assert not hasattr(iso, "prepare_and_invoke")
    assert not hasattr(iso, "_load_adapter_factory")
    assert not hasattr(iso, "_prepare_and_invoke")


def test_request_v1_rejects_missing_unknown_and_mutable_fields(tmp_path: Path) -> None:
    """The request accepts only its frozen version-1 field set."""
    iso = _module()
    request = _request(tmp_path)
    values = {field.name: getattr(request, field.name) for field in dataclasses.fields(request)}

    missing = dict(values)
    missing.pop("prompt")
    with pytest.raises(TypeError):
        iso.CodexIsolationRequestV1(**missing)
    with pytest.raises(TypeError):
        iso.CodexIsolationRequestV1(**values, unknown=True)
    with pytest.raises((TypeError, ValueError)):
        iso.CodexIsolationRequestV1(**{**values, "command": ["codex"]})
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.prompt = "changed"


def test_all_v1_records_are_frozen_slotted_and_exact(tmp_path: Path) -> None:
    """Each version-1 record has no mutable instance namespace or extra fields."""
    request = _request(tmp_path)
    prepared = _prepared(request)
    records = (
        request.policy,
        request.git_receipt,
        request,
        prepared,
        _inventory(1, 12.25),
        _result(request, prepared),
    )
    for record in records:
        assert not hasattr(record, "__dict__")
        values = {field.name: getattr(record, field.name) for field in dataclasses.fields(record)}
        first_name = dataclasses.fields(record)[0].name
        missing = dict(values)
        missing.pop(first_name)
        with pytest.raises(TypeError):
            type(record)(**missing)
        with pytest.raises(TypeError):
            type(record)(**values, unknown=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(record, first_name, values[first_name])


def test_result_v1_rejects_nonce_and_digest_mismatch(tmp_path: Path) -> None:
    """The final result must match each host-bound identity."""
    iso = _module()
    request = _request(tmp_path)
    prepared = _prepared(request)
    iso.validate_prepared(request, prepared)

    for changes in (
        {"request_nonce": "3" * 64},
        {"request_digest": "4" * 64},
        {"prepared_record_digest": "5" * 64},
        {"policy_digest": "6" * 64},
        {"executable_digest": "7" * 64},
        {"git_receipt_digest": "8" * 64},
        {"session_identity_digest": "9" * 64},
    ):
        with pytest.raises(iso.CodexIsolationError) as error:
            iso.validate_result(request, prepared, _result(request, prepared, **changes))
        assert error.value.code == "codex_adapter_request_mismatch"


def test_result_v1_requires_two_quiescent_empty_inventories(tmp_path: Path) -> None:
    """The final result requires two ordered and quiescent empty inventories."""
    iso = _module()
    request = _request(tmp_path)
    prepared = _prepared(request)
    invalid_sets = (
        (_inventory(1, 12.5),),
        (_inventory(2, 12.0), _inventory(1, 12.5)),
        (_inventory(1, 12.25, descendants=(99,)), _inventory(2, 12.5)),
        (_inventory(1, 12.25), _inventory(2, 12.4)),
    )

    for inventories in invalid_sets:
        with pytest.raises(iso.CodexIsolationError) as error:
            iso.validate_result(
                request,
                prepared,
                _result(request, prepared, inventories=inventories),
            )
        assert error.value.code in {
            "codex_adapter_inventory_uncertain",
            "codex_adapter_descendants_remain",
        }


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"term_sent": False, "kill_sent": True}, "codex_adapter_result_invalid"),
        ({"kill_sent": True, "kill_timestamp": 11.1}, "codex_adapter_timeout"),
        ({"pipe_close_timestamp": 12.1}, "codex_adapter_pipe_cleanup_failed"),
        (
            {"inventories": (_inventory(1, 899.9), _inventory(2, 900.1))},
            "codex_adapter_timeout",
        ),
    ],
)
def test_result_v1_rejects_cleanup_deadline_overrun(
    tmp_path: Path,
    changes: dict[str, Any],
    code: str,
) -> None:
    """Signal, pipe, and inventory evidence must stay in the frozen bounds."""
    iso = _module()
    request = _request(tmp_path)
    prepared = _prepared(request)

    with pytest.raises(iso.CodexIsolationError) as error:
        iso.validate_result(request, prepared, _result(request, prepared, **changes))

    assert error.value.code == code


def test_executable_staging_runs_only_descriptor_bound_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging copies bytes from the held source descriptor."""
    iso = _module()
    source = tmp_path / "source-codex"
    original = _linux_elf(b"original")
    replacement = _linux_elf(b"replacement")
    source.write_bytes(original)
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)
    original_open = iso.os.open

    def replacing_open(path: os.PathLike[str] | str, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == source:
            moved = tmp_path / "opened-source"
            source.rename(moved)
            source.write_bytes(replacement)
            source.chmod(0o500)
        return descriptor

    monkeypatch.setattr(iso.os, "open", replacing_open)
    staged = iso.stage_linux_executable(source, job_root)

    assert staged.path.read_bytes() == original
    assert staged.digest == _digest(original)
    assert stat.S_IMODE(staged.path.stat().st_mode) == 0o500
    assert source.read_bytes() == replacement
    iso.close_staged_linux_executable(staged)


def test_staged_executable_descriptor_survives_path_swap(tmp_path: Path) -> None:
    """The held descriptor stays bound when the staged path is replaced."""
    iso = _module()
    source = tmp_path / "source-codex"
    source.write_bytes(_linux_elf(b"trusted"))
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)

    staged = iso.stage_linux_executable(source, job_root)
    original = os.fstat(staged.descriptor)
    staged.path.unlink()
    staged.path.write_bytes(_linux_elf(b"replacement"))
    staged.path.chmod(0o500)

    assert os.fstat(staged.descriptor).st_ino == original.st_ino
    assert _digest(os.pread(staged.descriptor, original.st_size, 0)) == staged.digest
    iso.close_staged_linux_executable(staged)


def test_darwin_codex_artifact_is_rejected_for_linux_guest(tmp_path: Path) -> None:
    """Staging rejects a Darwin executable before it creates guest bytes."""
    iso = _module()
    source = tmp_path / "darwin-codex"
    source.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(64))
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)

    with pytest.raises(iso.CodexIsolationError) as error:
        iso.stage_linux_executable(source, job_root)

    assert error.value.code == "codex_adapter_protocol_mismatch"
    assert not tuple(job_root.iterdir())


def test_prepare_runs_version_without_auth_and_invoke_uses_same_guest_bytes(
    tmp_path: Path,
) -> None:
    """Both V1 phases bind one guest boot and one executable digest."""
    iso = _module()
    request = _request(tmp_path)
    prepared = _prepared(request)
    result = _result(request, prepared)
    calls: list[tuple[str, Any]] = []

    class Adapter:
        def prepare(self, supplied_request: Any) -> Any:
            calls.append(("prepare", supplied_request))
            return prepared

        def invoke(self, supplied_prepared: Any, auth_path: str) -> Any:
            calls.append(("invoke", (supplied_prepared, auth_path)))
            return result

        def destroy(self, supplied_prepared: Any) -> None:
            calls.append(("destroy", supplied_prepared))

    adapter = Adapter()
    actual_prepared = adapter.prepare(request)
    iso.validate_prepared(request, actual_prepared)
    auth_path = str((tmp_path / "auth.json").resolve())
    actual = adapter.invoke(actual_prepared, auth_path)
    iso.validate_result(request, actual_prepared, actual)
    adapter.destroy(actual_prepared)

    assert actual is result
    assert calls == [
        ("prepare", request),
        ("invoke", (prepared, auth_path)),
        ("destroy", prepared),
    ]
    assert prepared.guest_boot_nonce == result.guest_boot_nonce
    assert prepared.executable_digest == result.executable_digest


@pytest.mark.parametrize("name", ("$(touch owned)", "`touch owned`", "name;command", "name value"))
def test_adapter_command_substitution_is_rejected(name: str, tmp_path: Path) -> None:
    """The production admission path rejects command text before lock access."""
    from hephaestus.automation import codex_adapter_admission

    with pytest.raises(
        codex_adapter_admission.CodexAdapterAdmissionError,
        match="adapter selection is invalid",
    ):
        codex_adapter_admission.admit_codex_adapter(
            lock_path=tmp_path / "missing-lock.json",
            expected_sha256="a" * 64,
            selected_entry_point=name,
        )


def test_installed_wheel_entry_point_loads_in_fresh_process(tmp_path: Path) -> None:
    """The exact production admission path loads one installed wheel."""
    from tests.unit.automation import test_codex_adapter_admission

    test_codex_adapter_admission.test_public_admission_loads_verified_installed_bytes_in_a_fresh_process(
        tmp_path
    )


def test_protocol_rejects_noncanonical_values_and_wrong_versions(tmp_path: Path) -> None:
    """Canonical protocol values reject mutable and invalid scalar inputs."""
    iso = _module()
    assert len(iso.new_run_nonce()) == 64
    with pytest.raises(TypeError):
        iso.canonical_bytes({"mutable": "mapping"})
    with pytest.raises(TypeError):
        iso.canonical_bytes(float("nan"))
    with pytest.raises(ValueError):
        iso.CodexIsolationError("private_error_text")
    with pytest.raises(TypeError):
        dataclasses.replace(_policy(tmp_path), schema_version=True)
    with pytest.raises(TypeError):
        dataclasses.replace(_git_receipt(tmp_path), fixed_environment=[])
    with pytest.raises(ValueError):
        _request(tmp_path, executable_platform="darwin")


def test_result_rejects_output_pipe_and_incomplete_inventory(tmp_path: Path) -> None:
    """The final gate rejects unsafe output and uncertain cleanup evidence."""
    iso = _module()
    request = _request(tmp_path)
    prepared = _prepared(request)
    incomplete = dataclasses.replace(_inventory(1, 12.25), complete=False)
    cases = (
        ({"output": "x" * 4097}, "codex_adapter_result_invalid"),
        ({"output": request.prompt}, "codex_adapter_result_invalid"),
        ({"output": request.private_profile_path}, "codex_adapter_result_invalid"),
        ({"pipes_closed": False}, "codex_adapter_pipe_cleanup_failed"),
        ({"error_code": "codex_adapter_launch_failed"}, "codex_adapter_launch_failed"),
        ({"exit_status": 1}, "codex_adapter_result_invalid"),
        (
            {"inventories": (incomplete, _inventory(2, 12.5))},
            "codex_adapter_inventory_uncertain",
        ),
    )
    for changes, code in cases:
        with pytest.raises(iso.CodexIsolationError) as error:
            iso.validate_result(request, prepared, _result(request, prepared, **changes))
        assert error.value.code == code
