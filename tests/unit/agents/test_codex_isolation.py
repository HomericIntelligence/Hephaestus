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
        ("GIT_NO_REPLACE_OBJECTS", "1"),
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


@pytest.mark.parametrize("replacement", [None, "0"])
def test_git_receipt_requires_exact_no_replace_contract(
    tmp_path: Path,
    replacement: str | None,
) -> None:
    """The public receipt rejects a missing or disabled replacement guard."""
    receipt = _git_receipt(tmp_path)
    fixed = dict(receipt.fixed_environment)
    if replacement is None:
        fixed.pop("GIT_NO_REPLACE_OBJECTS")
    else:
        fixed["GIT_NO_REPLACE_OBJECTS"] = replacement

    with pytest.raises(ValueError, match="exact Git isolation map"):
        dataclasses.replace(receipt, fixed_environment=tuple(sorted(fixed.items())))


def _policy(tmp_path: Path) -> Any:
    iso = _module()
    worktree = str((tmp_path / "worktree").resolve())
    return iso.CodexExecutionPolicyV1(
        schema_version=1,
        read_only_mounts=tuple(
            str((tmp_path / relative).resolve())
            for relative in (
                "readonly",
                "git-dir",
                "common-dir",
                "index",
                "config",
                "config.worktree",
            )
        ),
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
    environment = tuple(
        sorted(
            (
                ("CODEX_HOME", str((tmp_path / "profile").resolve())),
                *receipt.fixed_environment,
            )
        )
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


@pytest.mark.parametrize("model", ["", "default", "gpt-6-astra:max", "MyModel", "sol"])
def test_request_preserves_literal_model_identity(tmp_path: Path, model: str) -> None:
    """An omitted model and each literal model have distinct request identities."""
    original = _request(tmp_path)
    identity = (
        original.repository,
        original.issue,
        original.role,
        original.worktree_identity,
        model,
        original.session,
    )
    request = dataclasses.replace(original, model=model, session_identity_digest=_digest(identity))
    assert request.model == model
    assert request.session_identity_digest == _digest(identity)
    if model == "":
        literal_identity = (*identity[:4], "default", identity[5])
        assert request.session_identity_digest != _digest(literal_identity)


def test_request_binds_git_receipt_paths_and_environment(tmp_path: Path) -> None:
    """A re-digested request cannot diverge from its exact Git receipt."""
    request = _request(tmp_path)
    changed_environment = tuple(
        (name, "0" if name == "GIT_NO_REPLACE_OBJECTS" else value)
        for name, value in request.environment
    )
    with pytest.raises(ValueError, match="environment does not match"):
        dataclasses.replace(
            request,
            environment=changed_environment,
            environment_digest=_digest(changed_environment),
        )

    other_worktree = str((tmp_path / "other-worktree").resolve())
    changed_receipt = dataclasses.replace(
        request.git_receipt,
        canonical_worktree=other_worktree,
        fixed_environment=tuple(
            sorted(
                (
                    name,
                    other_worktree if name == "GIT_WORK_TREE" else value,
                )
                for name, value in request.git_receipt.fixed_environment
            )
        ),
    )
    changed_environment_map = dict(request.environment)
    changed_environment_map["GIT_WORK_TREE"] = other_worktree
    changed_environment = tuple(sorted(changed_environment_map.items()))
    with pytest.raises(ValueError, match="worktree paths"):
        dataclasses.replace(
            request,
            environment=changed_environment,
            environment_digest=_digest(changed_environment),
            git_receipt=changed_receipt,
            git_receipt_digest=_digest(changed_receipt),
        )


def test_request_allows_read_only_git_capability_downgrade(tmp_path: Path) -> None:
    """The request can reduce the worktree grant to read-only access."""
    request = _request(tmp_path)
    worktree = request.git_receipt.canonical_worktree
    profile = request.private_profile_path
    policy = dataclasses.replace(
        request.policy,
        read_only_mounts=(*request.policy.read_only_mounts, worktree),
        read_write_mounts=(profile,),
    )

    downgraded = dataclasses.replace(
        request,
        policy=policy,
        policy_digest=_digest(policy),
    )

    assert worktree in downgraded.policy.read_only_mounts
    assert worktree not in downgraded.policy.read_write_mounts


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


def test_executable_staging_streams_held_descriptor_bytes(
    tmp_path: Path,
) -> None:
    """Staging streams bytes through held source and destination descriptors."""
    iso = _module()
    source = tmp_path / "source-codex"
    original = _linux_elf(b"original")
    source.write_bytes(original)
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)
    read = patch.object(iso.os, "read", wraps=iso.os.read)

    with read as read_call:
        staged = iso._stage_linux_executable(
            source,
            job_root,
        )

    assert staged.path.read_bytes() == original
    assert staged.digest == _digest(original)
    assert stat.S_IMODE(staged.path.stat().st_mode) == 0o500
    assert read_call.call_count >= 2
    iso.close_staged_linux_executable(staged)


def test_executable_staging_reports_the_actual_staged_digest(tmp_path: Path) -> None:
    """Staging reports the digest of the admitted bytes, not a caller's constant."""
    iso = _module()
    source = tmp_path / "source-codex"
    payload = _linux_elf(b"trusted")
    source.write_bytes(payload)
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)

    staged = iso._stage_linux_executable(
        source,
        job_root,
    )

    assert staged.digest == _digest(payload)
    iso.close_staged_linux_executable(staged)


def test_public_executable_staging_does_not_accept_caller_identity(tmp_path: Path) -> None:
    """The public staging boundary takes no caller-selected identity values."""
    iso = _module()
    source = tmp_path / "source-codex"
    job_root = tmp_path / "job"
    expected = object()

    with patch.object(iso, "_stage_linux_executable", return_value=expected) as stage:
        actual = iso.stage_linux_executable(source, job_root)

    assert actual is expected
    stage.assert_called_once_with(
        source,
        job_root,
    )
    with pytest.raises(TypeError):
        iso.stage_linux_executable(source, job_root, expected_digest="0" * 64)


def test_executable_staging_rejects_a_source_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging rejects a source path that changes after descriptor open."""
    iso = _module()
    source = tmp_path / "source-codex"
    original = _linux_elf(b"trusted")
    replacement = _linux_elf(b"replacement")
    source.write_bytes(original)
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)
    original_open = iso.os.open

    def replacing_open(path: os.PathLike[str] | str, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == source:
            source.rename(tmp_path / "opened-source")
            source.write_bytes(replacement)
            source.chmod(0o500)
        return descriptor

    monkeypatch.setattr(iso.os, "open", replacing_open)

    with pytest.raises(iso.CodexIsolationError) as error:
        iso._stage_linux_executable(
            source,
            job_root,
        )

    assert error.value.code == "codex_adapter_protocol_mismatch"
    assert not tuple(job_root.iterdir())


def test_staging_streams_arbitrary_size_sources_without_a_size_pin(tmp_path: Path) -> None:
    """Staging accepts a source of any size; the admission lock owns authenticity."""
    iso = _module()
    expected = _linux_elf(b"locked")
    source = tmp_path / "source-codex"
    source.write_bytes(expected + b"x")
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)

    staged = iso._stage_linux_executable(
        source,
        job_root,
    )

    assert staged.path.read_bytes() == expected + b"x"
    iso.close_staged_linux_executable(staged)


def test_staged_executable_descriptor_survives_path_swap(tmp_path: Path) -> None:
    """The held descriptor stays bound when the staged path is replaced."""
    iso = _module()
    source = tmp_path / "source-codex"
    trusted = _linux_elf(b"trusted")
    source.write_bytes(trusted)
    source.chmod(0o500)
    job_root = tmp_path / "job"
    job_root.mkdir(mode=0o700)

    staged = iso._stage_linux_executable(
        source,
        job_root,
    )
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
        iso._stage_linux_executable(
            source,
            job_root,
        )

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


def test_result_evidence_accepts_provider_failure_but_keeps_isolation_checks(
    tmp_path: Path,
) -> None:
    """Provider failure classification requires complete isolation evidence."""
    iso = _module()
    request = _request(tmp_path)
    prepared = _prepared(request)
    result = _result(request, prepared, exit_status=1)
    iso.validate_result_evidence(request, prepared, result)
    with pytest.raises(iso.CodexIsolationError, match="codex_adapter_result_invalid"):
        iso.validate_result(request, prepared, result)
    for changes in (
        {"pipes_closed": False},
        {"request_digest": "0" * 64},
        {"inventories": ()},
        {"error_code": "codex_adapter_inventory_uncertain"},
    ):
        with pytest.raises(iso.CodexIsolationError):
            iso.validate_result_evidence(request, prepared, dataclasses.replace(result, **changes))


@pytest.mark.parametrize(
    "tag",
    [
        "rust-v0.153.0",
        "rust-v0.153.1",
        "rust-v0.153.4",
        "rust-v0.153.99",
        "rust-v0.153.999999999",
    ],
)
def test_parse_release_tag_accepts_supported_line(tag) -> None:
    """Supported-line release tags are accepted and parsed into tuples."""
    iso = _module()
    assert iso.parse_release_tag(tag) == tuple(int(p) for p in tag[len("rust-v") :].split("."))


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "rust-v0.152.9",
        "rust-v0.154.0",
        "rust-v1.0.0",
        "rust-v0.153",
        "rust-v0.153.4.5",
        "rust-v00.153.4",
        "Rust-v0.153.4",
        "rust-v0.15٣.4",
        "rust-v0.153.4\n",
        "rust-v100000000000.0.0",
        "rust-v9223372036854775807.153.4",
    ],
)
def test_parse_release_tag_rejects_outside_supported_line(tag) -> None:
    """Outside-line, malformed, or Unicode release tags are rejected."""
    iso = _module()
    assert iso.parse_release_tag(tag) is None


@pytest.mark.parametrize(
    "output",
    [
        "codex-cli 0.153.0",
        "codex-cli 0.153.4",
        "codex-cli 0.153.99",
    ],
)
def test_parse_version_output_accepts_supported_line(output) -> None:
    """Supported-line --version output is accepted and parsed into tuples."""
    iso = _module()
    assert iso.parse_version_output(output) == tuple(
        int(p) for p in output[len("codex-cli ") :].split(".")
    )


@pytest.mark.parametrize(
    "output",
    [
        "",
        "codex-cli 0.152.9",
        "codex-cli 0.154.0",
        "codex-cli 0.153",
        "codex-cli 0.153.4x",
        "codex-cli 0.153.4\n",
        "codex-cli 0.15٣.4",
        "codex-cli 9999999999.153.4",
        "codex-cli 0.153.9999999999",
    ],
)
def test_parse_version_output_rejects_outside_supported_line(output) -> None:
    """Outside-line or malformed --version output is rejected."""
    iso = _module()
    assert iso.parse_version_output(output) is None


def test_fleet_version_output_accepts_patch_variants() -> None:
    """Fleet provider accepts any patch inside the supported line."""
    from hephaestus.agents import codex_isolation

    for version in ("codex-cli 0.153.0", "codex-cli 0.153.1", "codex-cli 0.153.99"):
        assert codex_isolation.is_supported_codex_version_output(version)


def test_fleet_version_output_rejects_non_supported() -> None:
    """Fleet provider fails closed outside the supported line."""
    from hephaestus.agents import codex_isolation

    for version in ("codex-cli 0.154.0", "codex-cli 0.152.0", "codex-cli", "", "codex-cli 0.153"):
        assert not codex_isolation.is_supported_codex_version_output(version)
