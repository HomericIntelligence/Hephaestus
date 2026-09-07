"""Define the fail-closed Codex isolation-adapter protocol."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from itertools import pairwise
from pathlib import Path
from typing import Never, Protocol, cast

CODEX_ISOLATION_ADAPTER_ENTRY_POINT_GROUP = "hephaestus.codex_isolation_adapters"
CODEX_ISOLATION_API_VERSION = 1
CODEX_RELEASE = "rust-v0.153.4"
CODEX_VERSION_OUTPUT = "codex-cli 0.153.4"
CODEX_LINUX_TARGET = "aarch64-unknown-linux-musl"
CODEX_LINUX_ASSET = "codex-aarch64-unknown-linux-musl.zst"
_CODEX_LINUX_EXECUTABLE_SIZE = 222_567_456
_CODEX_LINUX_EXECUTABLE_SHA256 = "4d76e542c222ea8c75861d8c4ade60a1a332a63255ce1c60bdaebf7c2a2869e6"
_EXECUTABLE_COPY_CHUNK_SIZE = 1024 * 1024

STABLE_ERROR_CODES = frozenset(
    {
        "codex_adapter_not_selected",
        "codex_adapter_not_installed",
        "codex_adapter_ambiguous",
        "codex_adapter_initialization_failed",
        "codex_adapter_protocol_mismatch",
        "codex_adapter_request_mismatch",
        "codex_adapter_launch_failed",
        "codex_adapter_timeout",
        "codex_adapter_pipe_cleanup_failed",
        "codex_adapter_inventory_uncertain",
        "codex_adapter_descendants_remain",
        "codex_adapter_result_invalid",
    }
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_POINT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?\Z")
_FILE_IDENTITY_LENGTH = 6
FileIdentity = tuple[int, int, int, int, int, int]
StringPairs = tuple[tuple[str, str], ...]
NamedFileIdentities = tuple[tuple[str, FileIdentity], ...]


class CodexIsolationError(RuntimeError):
    """Report one stable, non-transient isolation failure."""

    def __init__(self, code: str) -> None:
        """Initialize the error with one stable code."""
        if code not in STABLE_ERROR_CODES:
            raise ValueError("The Codex isolation error code is not valid")
        self.code = code
        self.transient = False
        super().__init__(code)


class _CodexPrepareCleanupError(RuntimeError):
    """Carry one host-only cleanup action for an unpublishable prepare result."""

    def __init__(self, cleanup: Callable[[], None]) -> None:
        """Store one cleanup action that a host can claim one time."""
        self._cleanup = cleanup
        self._claimed = False
        self._lock = threading.Lock()
        super().__init__("The prepared guest needs opaque cleanup")

    def claim_cleanup(self) -> Callable[[], None] | None:
        """Return the cleanup action one time."""
        with self._lock:
            if self._claimed:
                return None
            self._claimed = True
            return self._cleanup


def _fail(code: str) -> Never:
    raise CodexIsolationError(code)


def _is_exact_int(value: object) -> bool:
    return type(value) is int


def _is_number(value: object) -> bool:
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    return False


def _require_schema_version(value: object) -> None:
    if not _is_exact_int(value) or value != 1:
        raise TypeError("schema_version must be integer 1")


def _require_string(value: object, field_name: str, *, allow_empty: bool = False) -> None:
    if type(value) is not str or (not allow_empty and not value):
        raise TypeError(f"{field_name} must be a string")


def _require_digest(value: object, field_name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise TypeError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_nonce(value: object, field_name: str) -> None:
    _require_digest(value, field_name)


def _require_absolute_path(value: object, field_name: str) -> None:
    _require_string(value, field_name)
    path = cast(str, value)
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise ValueError(f"{field_name} must be a canonical absolute path")


def _require_string_tuple(value: object, field_name: str) -> None:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value):
        raise TypeError(f"{field_name} must be a tuple of strings")


def _require_path_tuple(value: object, field_name: str) -> None:
    _require_string_tuple(value, field_name)
    for item in cast(tuple[str, ...], value):
        _require_absolute_path(item, field_name)


def _require_file_identity(value: object, field_name: str) -> None:
    if (
        type(value) is not tuple
        or len(value) != _FILE_IDENTITY_LENGTH
        or any(not _is_exact_int(item) or item < 0 for item in value)
    ):
        raise TypeError(f"{field_name} must be an immutable file identity")


def _require_string_pairs(value: object, field_name: str, *, digests: bool = False) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an immutable tuple")
    pairs = cast(tuple[object, ...], value)
    keys: list[str] = []
    for pair in pairs:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError(f"{field_name} must contain pairs")
        key, item = pair
        _require_string(key, field_name)
        if digests:
            _require_digest(item, field_name)
        else:
            _require_string(item, field_name, allow_empty=True)
        keys.append(cast(str, key))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError(f"{field_name} must have sorted unique keys")


def _require_named_identities(value: object, field_name: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an immutable tuple")
    keys: list[str] = []
    for pair in cast(tuple[object, ...], value):
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError(f"{field_name} must contain pairs")
        key, identity = pair
        _require_string(key, field_name)
        _require_file_identity(identity, field_name)
        keys.append(cast(str, key))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError(f"{field_name} must have sorted unique keys")


def _canonical_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        parameters = getattr(type(value), "__dataclass_params__", None)
        if parameters is None or not parameters.frozen:
            raise TypeError("Canonical records must be frozen")
        return {field.name: _canonical_value(getattr(value, field.name)) for field in fields(value)}
    if type(value) is tuple:
        return [_canonical_value(item) for item in cast(tuple[object, ...], value)]
    if type(value) in {str, int, bool} or value is None:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError("Canonical floats must be finite")
        return value
    raise TypeError("The value is not canonical")


def canonical_bytes(value: object) -> bytes:
    """Return the unique UTF-8 bytes for one frozen protocol value."""
    if type(value) is bytes:
        return value
    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 digest of one canonical value."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def new_run_nonce() -> str:
    """Return a fresh 32-byte host nonce in lowercase hexadecimal."""
    return os.urandom(32).hex()


@dataclass(frozen=True, slots=True)
class CodexExecutionPolicyV1:
    """Bind the version-1 operating-system execution policy."""

    schema_version: int
    read_only_mounts: tuple[str, ...]
    read_write_mounts: tuple[str, ...]
    protected_overlay_mounts: tuple[str, ...]
    provider_relay: str
    command_network: str
    max_output_bytes: int
    term_grace_seconds: float
    kill_grace_seconds: float
    pipe_close_grace_seconds: float
    inventory_quiescence_seconds: float
    total_deadline: float

    def __post_init__(self) -> None:
        """Validate the exact policy fields."""
        _require_schema_version(self.schema_version)
        _require_path_tuple(self.read_only_mounts, "read_only_mounts")
        _require_path_tuple(self.read_write_mounts, "read_write_mounts")
        _require_path_tuple(self.protected_overlay_mounts, "protected_overlay_mounts")
        _require_string(self.provider_relay, "provider_relay")
        if self.command_network != "deny":
            raise ValueError("command_network must deny access")
        if not _is_exact_int(self.max_output_bytes) or self.max_output_bytes <= 0:
            raise TypeError("max_output_bytes must be a positive integer")
        for name in (
            "term_grace_seconds",
            "kill_grace_seconds",
            "pipe_close_grace_seconds",
            "inventory_quiescence_seconds",
            "total_deadline",
        ):
            value = getattr(self, name)
            if not _is_number(value) or float(value) <= 0:
                raise TypeError(f"{name} must be a positive finite number")


@dataclass(frozen=True, slots=True)
class CodexGitReceiptV1:
    """Bind the version-1 Git paths, identities, digests, and policy grants."""

    schema_version: int
    canonical_worktree: str
    git_dir: str
    common_dir: str
    index: str
    repository_config: str
    worktree_config: str
    fixed_environment: StringPairs
    protected_paths: tuple[str, ...]
    read_only_paths: tuple[str, ...]
    read_write_paths: tuple[str, ...]
    identities: NamedFileIdentities
    digests: StringPairs

    def __post_init__(self) -> None:
        """Validate the exact Git receipt fields."""
        _require_schema_version(self.schema_version)
        for name in (
            "canonical_worktree",
            "git_dir",
            "common_dir",
            "index",
            "repository_config",
            "worktree_config",
        ):
            _require_absolute_path(getattr(self, name), name)
        _require_string_pairs(self.fixed_environment, "fixed_environment")
        fixed = dict(self.fixed_environment)
        expected = {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_DIR": self.git_dir,
            "GIT_INDEX_FILE": self.index,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_WORK_TREE": self.canonical_worktree,
        }
        if fixed != expected:
            raise ValueError("fixed_environment must contain the exact Git isolation map")
        _require_path_tuple(self.protected_paths, "protected_paths")
        _require_path_tuple(self.read_only_paths, "read_only_paths")
        _require_path_tuple(self.read_write_paths, "read_write_paths")
        _require_named_identities(self.identities, "identities")
        _require_string_pairs(self.digests, "digests", digests=True)


@dataclass(frozen=True, slots=True)
class CodexIsolationRequestV1:
    """Bind the accepted version-1 request field set."""

    schema_version: int
    run_nonce: str
    entry_point_name: str
    adapter_api_version: int
    package_version: str
    deployment_lock_digest: str
    wheel_digest: str
    installed_tree_digest: str
    command: tuple[str, ...]
    command_digest: str
    executable_platform: str
    executable_target: str
    executable_release: str
    executable_asset_name: str
    executable_path: str
    executable_digest: str
    executable_file_identity: FileIdentity
    guest_image_digest: str
    environment: StringPairs
    environment_digest: str
    prompt: str
    prompt_digest: str
    worktree_path: str
    private_profile_path: str
    policy: CodexExecutionPolicyV1
    policy_digest: str
    git_receipt: CodexGitReceiptV1
    git_receipt_digest: str
    repository: str
    issue: int
    role: str
    worktree_identity: str
    model: str
    session: str
    session_identity_digest: str
    monotonic_deadline: float

    def __post_init__(self) -> None:  # noqa: C901
        """Validate the accepted version-1 request."""
        _require_schema_version(self.schema_version)
        _require_nonce(self.run_nonce, "run_nonce")
        _require_entry_point_name(self.entry_point_name)
        if not _is_exact_int(self.adapter_api_version) or self.adapter_api_version != 1:
            raise TypeError("adapter_api_version must be integer 1")
        _require_string(self.package_version, "package_version")
        for name in (
            "deployment_lock_digest",
            "wheel_digest",
            "installed_tree_digest",
            "command_digest",
            "executable_digest",
            "guest_image_digest",
            "environment_digest",
            "prompt_digest",
            "policy_digest",
            "git_receipt_digest",
            "session_identity_digest",
        ):
            _require_digest(getattr(self, name), name)
        _require_string_tuple(self.command, "command")
        if not self.command:
            raise ValueError("command must not be empty")
        if canonical_sha256(self.command) != self.command_digest:
            raise ValueError("command_digest does not match command")
        if self.executable_platform != "linux":
            raise ValueError("executable_platform must be linux")
        if self.executable_target != CODEX_LINUX_TARGET:
            raise ValueError("executable_target is not supported")
        if self.executable_release != CODEX_RELEASE:
            raise ValueError("executable_release is not supported")
        if self.executable_asset_name != CODEX_LINUX_ASSET:
            raise ValueError("executable_asset_name is not supported")
        _require_absolute_path(self.executable_path, "executable_path")
        _require_file_identity(self.executable_file_identity, "executable_file_identity")
        _require_string_pairs(self.environment, "environment")
        if canonical_sha256(self.environment) != self.environment_digest:
            raise ValueError("environment_digest does not match environment")
        _require_string(self.prompt, "prompt", allow_empty=True)
        if canonical_sha256(self.prompt) != self.prompt_digest:
            raise ValueError("prompt_digest does not match prompt")
        _require_absolute_path(self.worktree_path, "worktree_path")
        _require_absolute_path(self.private_profile_path, "private_profile_path")
        if type(self.policy) is not CodexExecutionPolicyV1:
            raise TypeError("policy must be CodexExecutionPolicyV1")
        if canonical_sha256(self.policy) != self.policy_digest:
            raise ValueError("policy_digest does not match policy")
        if type(self.git_receipt) is not CodexGitReceiptV1:
            raise TypeError("git_receipt must be CodexGitReceiptV1")
        if canonical_sha256(self.git_receipt) != self.git_receipt_digest:
            raise ValueError("git_receipt_digest does not match git_receipt")
        environment = dict(self.environment)
        if any(
            environment.get(name) != value for name, value in self.git_receipt.fixed_environment
        ):
            raise ValueError("environment does not match git_receipt fixed_environment")
        if not (
            self.worktree_path == self.worktree_identity == self.git_receipt.canonical_worktree
        ):
            raise ValueError("worktree paths do not match the Git receipt")
        if set(self.policy.protected_overlay_mounts) != set(self.git_receipt.protected_paths):
            raise ValueError("protected mounts do not match the Git receipt")
        if not set(self.git_receipt.read_only_paths).issubset(self.policy.read_only_mounts):
            raise ValueError("read-only mounts do not contain the Git receipt paths")
        policy_paths = set(self.policy.read_only_mounts) | set(self.policy.read_write_mounts)
        if not set(self.git_receipt.read_write_paths).issubset(policy_paths):
            raise ValueError("policy mounts do not contain the Git receipt writable paths")
        if any(
            path not in self.git_receipt.read_write_paths and path != self.private_profile_path
            for path in self.policy.read_write_mounts
        ):
            raise ValueError("writable mounts exceed the Git receipt and private profile")
        _require_string(self.repository, "repository")
        if not _is_exact_int(self.issue) or self.issue <= 0:
            raise TypeError("issue must be a positive integer")
        if self.role != "implementer":
            raise ValueError("role must be implementer")
        _require_absolute_path(self.worktree_identity, "worktree_identity")
        _require_string(self.model, "model")
        _require_string(self.session, "session")
        identity = (
            self.repository,
            self.issue,
            self.role,
            self.worktree_identity,
            self.model,
            self.session,
        )
        if canonical_sha256(identity) != self.session_identity_digest:
            raise ValueError("session_identity_digest does not match the bound identity")
        if not _is_number(self.monotonic_deadline) or float(self.monotonic_deadline) <= 0:
            raise TypeError("monotonic_deadline must be a positive finite number")


@dataclass(frozen=True, slots=True)
class CodexIsolationPreparedV1:
    """Describe the accepted version-1 prepared field set."""

    schema_version: int
    request_nonce: str
    request_digest: str
    guest_boot_nonce: str
    guest_image_digest: str
    adapter_package_digest: str
    executable_digest: str
    elf_platform: str
    elf_target: str
    version_output: str
    guest_file_identity: FileIdentity
    invocation_token: str
    preparation_deadline: float

    def __post_init__(self) -> None:
        """Validate the accepted version-1 prepared result."""
        _require_schema_version(self.schema_version)
        _require_nonce(self.request_nonce, "request_nonce")
        _require_digest(self.request_digest, "request_digest")
        _require_nonce(self.guest_boot_nonce, "guest_boot_nonce")
        for name in ("guest_image_digest", "adapter_package_digest", "executable_digest"):
            _require_digest(getattr(self, name), name)
        if self.elf_platform != "linux" or self.elf_target != CODEX_LINUX_TARGET:
            raise ValueError("The prepared ELF identity is not supported")
        if self.version_output != CODEX_VERSION_OUTPUT:
            raise ValueError("The prepared Codex version is not supported")
        _require_file_identity(self.guest_file_identity, "guest_file_identity")
        _require_nonce(self.invocation_token, "invocation_token")
        if not _is_number(self.preparation_deadline) or float(self.preparation_deadline) <= 0:
            raise TypeError("preparation_deadline must be a positive finite number")


@dataclass(frozen=True, slots=True)
class CodexIsolationResultV1:
    """Describe the accepted version-1 result field set."""

    schema_version: int
    adapter_identity: str
    adapter_version: str
    request_nonce: str
    request_digest: str
    guest_boot_nonce: str
    prepared_record_digest: str
    exit_status: int
    output: str
    error_code: str | None
    term_sent: bool
    term_timestamp: float
    kill_sent: bool
    kill_timestamp: float
    pipes_closed: bool
    pipe_close_timestamp: float
    inventories: tuple[CodexDescendantInventoryV1, ...]
    policy_digest: str
    executable_digest: str
    git_receipt_digest: str
    session_identity_digest: str

    def __post_init__(self) -> None:
        """Validate the accepted version-1 final result."""
        _require_schema_version(self.schema_version)
        _require_string(self.adapter_identity, "adapter_identity")
        _require_string(self.adapter_version, "adapter_version")
        _require_nonce(self.request_nonce, "request_nonce")
        _require_nonce(self.guest_boot_nonce, "guest_boot_nonce")
        for name in (
            "request_digest",
            "prepared_record_digest",
            "policy_digest",
            "executable_digest",
            "git_receipt_digest",
            "session_identity_digest",
        ):
            _require_digest(getattr(self, name), name)
        if not _is_exact_int(self.exit_status) or not -255 <= self.exit_status <= 255:
            raise TypeError("exit_status must be a bounded integer")
        _require_string(self.output, "output", allow_empty=True)
        if self.error_code is not None and self.error_code not in STABLE_ERROR_CODES:
            raise ValueError("error_code is not a stable code")
        for name in ("term_sent", "kill_sent", "pipes_closed"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be Boolean")
        for name in ("term_timestamp", "kill_timestamp", "pipe_close_timestamp"):
            value = getattr(self, name)
            if not _is_number(value) or float(value) < 0:
                raise TypeError(f"{name} must be a nonnegative finite number")
        if not self.term_timestamp <= self.kill_timestamp <= self.pipe_close_timestamp:
            raise ValueError("Cleanup timestamps must be ordered")
        if type(self.inventories) is not tuple or any(
            type(item) is not CodexDescendantInventoryV1 for item in self.inventories
        ):
            raise TypeError("inventories must contain frozen version-1 records")


class CodexIsolationAdapterV1(Protocol):
    """Supply the accepted version-1 adapter lifecycle."""

    def prepare(self, request: CodexIsolationRequestV1) -> CodexIsolationPreparedV1:
        """Prepare one credential-free guest."""

    def invoke(self, prepared: CodexIsolationPreparedV1, auth_path: str) -> CodexIsolationResultV1:
        """Invoke Codex in the prepared guest."""

    def destroy(self, prepared: CodexIsolationPreparedV1) -> None:
        """Destroy one prepared guest and confirm terminal cleanup."""


def validate_adapter(adapter: object) -> None:
    """Require the accepted version-1 lifecycle operations."""
    if any(not callable(getattr(adapter, name, None)) for name in ("prepare", "invoke", "destroy")):
        _fail("codex_adapter_protocol_mismatch")


@dataclass(frozen=True, slots=True)
class CodexDescendantInventoryV1:
    """Describe one ordered and complete descendant inventory."""

    schema_version: int
    sequence: int
    monotonic_timestamp: float
    complete: bool
    descendants: tuple[int, ...]
    cgroup_populated: bool

    def __post_init__(self) -> None:
        """Validate the exact descendant-inventory fields."""
        _require_schema_version(self.schema_version)
        if not _is_exact_int(self.sequence) or self.sequence < 0:
            raise TypeError("sequence must be a nonnegative integer")
        if not _is_number(self.monotonic_timestamp) or float(self.monotonic_timestamp) < 0:
            raise TypeError("monotonic_timestamp must be a nonnegative finite number")
        if type(self.complete) is not bool or type(self.cgroup_populated) is not bool:
            raise TypeError("Inventory state values must be Boolean")
        if type(self.descendants) is not tuple or any(
            not _is_exact_int(item) or item <= 0 for item in self.descendants
        ):
            raise TypeError("descendants must be an immutable process identifier tuple")
        if tuple(sorted(set(self.descendants))) != self.descendants:
            raise ValueError("descendants must be sorted and unique")
        if self.cgroup_populated != bool(self.descendants):
            raise ValueError("cgroup_populated does not match descendants")


def _require_entry_point_name(name: object) -> None:
    if type(name) is not str or _ENTRY_POINT_RE.fullmatch(name) is None:
        _fail("codex_adapter_not_selected")


def validate_prepared(
    request: CodexIsolationRequestV1,
    prepared: CodexIsolationPreparedV1,
) -> None:
    """Validate an accepted version-1 prepared record."""
    if (
        type(request) is not CodexIsolationRequestV1
        or type(prepared) is not CodexIsolationPreparedV1
    ):
        _fail("codex_adapter_protocol_mismatch")
    expected = (
        (prepared.request_nonce, request.run_nonce),
        (prepared.request_digest, canonical_sha256(request)),
        (prepared.guest_image_digest, request.guest_image_digest),
        (prepared.adapter_package_digest, request.installed_tree_digest),
        (prepared.executable_digest, request.executable_digest),
        (prepared.elf_platform, request.executable_platform),
        (prepared.elf_target, request.executable_target),
        (prepared.guest_file_identity, request.executable_file_identity),
    )
    if any(actual != required for actual, required in expected):
        _fail("codex_adapter_request_mismatch")
    if prepared.preparation_deadline > request.monotonic_deadline:
        _fail("codex_adapter_request_mismatch")


def _validate_inventories_v1(
    inventories: tuple[CodexDescendantInventoryV1, ...],
    quiescence: float,
) -> None:
    if len(inventories) < 2 or any(not item.complete for item in inventories):
        _fail("codex_adapter_inventory_uncertain")
    for previous, current in pairwise(inventories):
        if (
            current.sequence != previous.sequence + 1
            or current.monotonic_timestamp < previous.monotonic_timestamp
        ):
            _fail("codex_adapter_inventory_uncertain")
    final_two = inventories[-2:]
    if any(item.descendants or item.cgroup_populated for item in final_two):
        _fail("codex_adapter_descendants_remain")
    if final_two[1].monotonic_timestamp - final_two[0].monotonic_timestamp < quiescence:
        _fail("codex_adapter_inventory_uncertain")


def validate_result(  # noqa: C901 - preserve the accepted version-1 validator
    request: CodexIsolationRequestV1,
    prepared: CodexIsolationPreparedV1,
    result: CodexIsolationResultV1,
) -> None:
    """Validate an accepted version-1 final result."""
    if (
        type(request) is not CodexIsolationRequestV1
        or type(prepared) is not CodexIsolationPreparedV1
        or type(result) is not CodexIsolationResultV1
    ):
        _fail("codex_adapter_protocol_mismatch")
    expected = (
        (result.adapter_identity, request.entry_point_name),
        (result.adapter_version, request.package_version),
        (result.request_nonce, request.run_nonce),
        (result.request_digest, canonical_sha256(request)),
        (result.guest_boot_nonce, prepared.guest_boot_nonce),
        (result.prepared_record_digest, canonical_sha256(prepared)),
        (result.policy_digest, request.policy_digest),
        (result.executable_digest, request.executable_digest),
        (result.git_receipt_digest, request.git_receipt_digest),
        (result.session_identity_digest, request.session_identity_digest),
    )
    if any(actual != required for actual, required in expected):
        _fail("codex_adapter_request_mismatch")
    if len(result.output.encode("utf-8")) > request.policy.max_output_bytes:
        _fail("codex_adapter_result_invalid")
    if request.prompt and request.prompt in result.output:
        _fail("codex_adapter_result_invalid")
    if request.private_profile_path in result.output:
        _fail("codex_adapter_result_invalid")
    if not result.pipes_closed:
        _fail("codex_adapter_pipe_cleanup_failed")
    if result.kill_sent and not result.term_sent:
        _fail("codex_adapter_result_invalid")
    if result.kill_sent and (
        result.kill_timestamp - result.term_timestamp > request.policy.term_grace_seconds
    ):
        _fail("codex_adapter_timeout")
    if result.term_sent:
        cleanup_start = result.kill_timestamp if result.kill_sent else result.term_timestamp
        signal_grace = (
            request.policy.kill_grace_seconds
            if result.kill_sent
            else request.policy.term_grace_seconds
        )
        if (
            result.pipe_close_timestamp - cleanup_start
            > signal_grace + request.policy.pipe_close_grace_seconds
        ):
            _fail("codex_adapter_pipe_cleanup_failed")
    if result.pipe_close_timestamp > request.monotonic_deadline or any(
        item.monotonic_timestamp > request.monotonic_deadline for item in result.inventories
    ):
        _fail("codex_adapter_timeout")
    _validate_inventories_v1(result.inventories, request.policy.inventory_quiescence_seconds)
    if result.error_code is not None:
        _fail(result.error_code)
    if result.exit_status != 0:
        _fail("codex_adapter_result_invalid")


@dataclass(frozen=True, slots=True)
class StagedLinuxExecutable:
    """Describe descriptor-copied Linux executable bytes."""

    path: Path
    descriptor: int
    digest: str
    file_identity: FileIdentity


def _stream_descriptor(
    source_descriptor: int,
    *,
    expected_size: int,
    destination_descriptor: int | None = None,
) -> str:
    os.lseek(source_descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        remaining = expected_size - size
        chunk = os.read(
            source_descriptor,
            min(_EXECUTABLE_COPY_CHUNK_SIZE, remaining + 1),
        )
        if not chunk:
            break
        size += len(chunk)
        if size > expected_size:
            _fail("codex_adapter_protocol_mismatch")
        digest.update(chunk)
        if destination_descriptor is not None:
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    _fail("codex_adapter_protocol_mismatch")
                view = view[written:]
    if size != expected_size:
        _fail("codex_adapter_protocol_mismatch")
    return digest.hexdigest()


def _validate_linux_elf(data: bytes) -> None:
    if len(data) < 64 or data[:4] != b"\x7fELF":
        _fail("codex_adapter_protocol_mismatch")
    if data[4] != 2 or data[5] != 1 or int.from_bytes(data[18:20], "little") != 183:
        _fail("codex_adapter_protocol_mismatch")


def _identity(status: os.stat_result) -> FileIdentity:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_uid,
        status.st_size,
        status.st_mtime_ns,
    )


def _open_staging_directory(root: Path, root_status: os.stat_result) -> int:
    descriptor = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        if _identity(os.fstat(descriptor)) != _identity(root_status):
            _fail("codex_adapter_protocol_mismatch")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_locked_source(source: Path, expected_size: int) -> tuple[int, os.stat_result]:
    path_status = source.lstat()
    if not stat.S_ISREG(path_status.st_mode):
        _fail("codex_adapter_protocol_mismatch")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_status.st_mode)
            or _identity(descriptor_status) != _identity(path_status)
            or descriptor_status.st_size != expected_size
        ):
            _fail("codex_adapter_protocol_mismatch")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, descriptor_status


def _remove_staged_destination(created: bool, directory_descriptor: int, name: str) -> None:
    if created and directory_descriptor >= 0:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=directory_descriptor)


def _staged_copy_matches(
    *,
    expected_digest: str,
    actual_digest: str,
    written_status: os.stat_result,
    verified_status: os.stat_result,
    path_status: os.stat_result,
    root_status: os.stat_result,
    held_root_status: os.stat_result,
    current_root_status: os.stat_result,
) -> bool:
    return (
        actual_digest == expected_digest
        and _identity(verified_status) == _identity(written_status)
        and _identity(path_status) == _identity(verified_status)
        and _identity(current_root_status) == _identity(held_root_status)
        and (
            current_root_status.st_dev,
            current_root_status.st_ino,
            current_root_status.st_mode,
            current_root_status.st_uid,
        )
        == (
            root_status.st_dev,
            root_status.st_ino,
            root_status.st_mode,
            root_status.st_uid,
        )
    )


def _stage_linux_executable(
    source_path: Path,
    job_root: Path,
    *,
    expected_size: int = _CODEX_LINUX_EXECUTABLE_SIZE,
    expected_digest: str = _CODEX_LINUX_EXECUTABLE_SHA256,
) -> StagedLinuxExecutable:
    """Copy the exact locked AArch64 Linux ELF through held descriptors."""
    if (
        not _is_exact_int(expected_size)
        or expected_size < 64
        or type(expected_digest) is not str
        or _DIGEST_RE.fullmatch(expected_digest) is None
    ):
        _fail("codex_adapter_protocol_mismatch")
    source = Path(source_path)
    root = Path(job_root)
    root_status = root.lstat()
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) & 0o077
    ):
        _fail("codex_adapter_protocol_mismatch")
    source_descriptor = -1
    destination_descriptor = -1
    directory_descriptor = -1
    verify_descriptor = -1
    destination_created = False
    destination = root / "codex-aarch64-unknown-linux-musl"
    try:
        directory_descriptor = _open_staging_directory(root, root_status)
        source_descriptor, source_status = _open_locked_source(source, expected_size)
        _validate_linux_elf(os.pread(source_descriptor, 64, 0))
        destination_descriptor = os.open(
            destination.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o500,
            dir_fd=directory_descriptor,
        )
        destination_created = True
        source_digest = _stream_descriptor(
            source_descriptor,
            expected_size=expected_size,
            destination_descriptor=destination_descriptor,
        )
        if source_digest != expected_digest:
            _fail("codex_adapter_protocol_mismatch")
        current_source_status = source.lstat()
        if _identity(current_source_status) != _identity(source_status):
            _fail("codex_adapter_protocol_mismatch")
        os.fchmod(destination_descriptor, 0o500)
        os.fsync(destination_descriptor)
        os.fsync(directory_descriptor)
        written_status = os.fstat(destination_descriptor)
        verify_descriptor = os.open(
            destination.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_descriptor,
        )
        verified_status = os.fstat(verify_descriptor)
        destination_digest = _stream_descriptor(
            verify_descriptor,
            expected_size=expected_size,
        )
        current_destination_status = os.stat(
            destination.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        held_root_status = os.fstat(directory_descriptor)
        current_root_status = root.lstat()
        if not _staged_copy_matches(
            expected_digest=expected_digest,
            actual_digest=destination_digest,
            written_status=written_status,
            verified_status=verified_status,
            path_status=current_destination_status,
            root_status=root_status,
            held_root_status=held_root_status,
            current_root_status=current_root_status,
        ):
            _fail("codex_adapter_protocol_mismatch")
        staged = StagedLinuxExecutable(
            destination,
            verify_descriptor,
            expected_digest,
            _identity(verified_status),
        )
        verify_descriptor = -1
        return staged
    except CodexIsolationError:
        _remove_staged_destination(
            destination_created,
            directory_descriptor,
            destination.name,
        )
        raise
    except OSError:
        _remove_staged_destination(
            destination_created,
            directory_descriptor,
            destination.name,
        )
        _fail("codex_adapter_protocol_mismatch")
    finally:
        for descriptor in (
            verify_descriptor,
            directory_descriptor,
            destination_descriptor,
            source_descriptor,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def stage_linux_executable(source_path: Path, job_root: Path) -> StagedLinuxExecutable:
    """Stage only the reviewed Codex release executable identity."""
    return _stage_linux_executable(
        source_path,
        job_root,
        expected_size=_CODEX_LINUX_EXECUTABLE_SIZE,
        expected_digest=_CODEX_LINUX_EXECUTABLE_SHA256,
    )


def close_staged_linux_executable(staged: StagedLinuxExecutable) -> None:
    """Close the held staged-executable descriptor."""
    try:
        os.close(staged.descriptor)
    except OSError:
        _fail("codex_adapter_protocol_mismatch")


__all__ = [
    "CODEX_ISOLATION_ADAPTER_ENTRY_POINT_GROUP",
    "CODEX_ISOLATION_API_VERSION",
    "STABLE_ERROR_CODES",
    "CodexDescendantInventoryV1",
    "CodexExecutionPolicyV1",
    "CodexGitReceiptV1",
    "CodexIsolationAdapterV1",
    "CodexIsolationError",
    "CodexIsolationPreparedV1",
    "CodexIsolationRequestV1",
    "CodexIsolationResultV1",
    "StagedLinuxExecutable",
    "canonical_bytes",
    "canonical_sha256",
    "close_staged_linux_executable",
    "new_run_nonce",
    "stage_linux_executable",
    "validate_adapter",
    "validate_prepared",
    "validate_result",
]
