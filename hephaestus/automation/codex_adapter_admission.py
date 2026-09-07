"""Admit one detached and locked Codex isolation adapter deployment."""

from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import stat
import sys
from collections.abc import Callable, Mapping
from configparser import ConfigParser
from dataclasses import dataclass, fields
from io import StringIO
from pathlib import Path
from typing import Any, cast

_ENTRY_POINT_GROUP = "hephaestus.codex_isolation_adapters"
_ARCHIVE_SHA256 = "7a148fb7e7ed8a4bfa3ac4ffe014336070398eff780893582afab9195c6652f7"
_SIGSTORE_SHA256 = "847b47e73068f86635c23ab5501647a93fab9ad0c450d6661a88481dfcd6d759"
_RELEASE_TAG = "rust-v0.153.4"
_ARCHIVE_ASSET = "codex-aarch64-unknown-linux-musl.zst"
_SIGSTORE_ASSET = "codex-aarch64-unknown-linux-musl.sigstore"
_TARGET = "aarch64-unknown-linux-musl"
_FULCIO_ISSUER = "O=sigstore.dev, CN=sigstore-intermediate"
_WORKFLOW_IDENTITY = (
    "https://github.com/openai/codex/.github/workflows/rust-release.yml@refs/tags/rust-v0.153.4"
)
_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_VIRTUALIZATION_FRAMEWORK = Path("/System/Library/Frameworks/Virtualization.framework")
_ENTRY_POINT_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?\Z")
_PYTHON_NAME_RE = re.compile(r"[A-Za-z_]\w*\Z")
_WHEEL_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.]*\Z")


class CodexAdapterAdmissionError(RuntimeError):
    """Report one stable, credential-free adapter admission failure."""


@dataclass(frozen=True, slots=True)
class LockedTreeFileV1:
    """Bind one installed regular file."""

    path: str
    sha256: str
    size: int
    mode: int

    @classmethod
    def from_mapping(cls, value: object) -> LockedTreeFileV1:
        """Build one locked-file record from an exact mapping."""
        if not isinstance(value, Mapping) or set(value) != {field.name for field in fields(cls)}:
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        path = value["path"]
        digest = value["sha256"]
        size = value["size"]
        mode = value["mode"]
        if (
            not isinstance(path, str)
            or not _is_digest(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or not 0 <= mode <= 0o777
        ):
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        relative = Path(path)
        if relative.is_absolute() or path in {"", "."} or ".." in relative.parts:
            raise CodexAdapterAdmissionError("installed tree path is invalid")
        return cls(path=path, sha256=cast(str, digest), size=size, mode=mode)


@dataclass(frozen=True, slots=True)
class CodexAdapterDeploymentLockV1:
    """Bind one reviewed external adapter and its offline deployment objects."""

    schema_version: int
    adapter_distribution: str
    adapter_version: str
    wheel_path: str
    wheel_sha256: str
    wheel_tags: tuple[str, ...]
    installed_tree_root: str
    installed_tree_manifest: tuple[LockedTreeFileV1, ...]
    installed_tree_sha256: str
    entry_point_group: str
    entry_point_name: str
    entry_point_module: str
    entry_point_factory: str
    adapter_api_version: int
    request_schema_version: int
    prepared_schema_version: int
    result_schema_version: int
    guest_image_path: str
    guest_image_format: str
    guest_image_platform: str
    guest_image_sha256: str
    codex_archive_path: str
    codex_archive_sha256: str
    sigstore_bundle_path: str
    sigstore_bundle_sha256: str
    extracted_elf_path: str
    extracted_elf_sha256: str
    codex_release_tag: str
    codex_archive_asset: str
    codex_sigstore_asset: str
    codex_target: str
    fulcio_certificate_issuer: str
    workflow_certificate_identity: str
    oidc_issuer: str
    trusted_root_path: str
    trusted_root_sha256: str
    rekor_public_key_path: str
    rekor_public_key_sha256: str
    rekor_checkpoint_path: str
    rekor_checkpoint_sha256: str
    rekor_inclusion_proof_path: str
    rekor_inclusion_proof_sha256: str
    rekor_log_id: str
    rekor_log_index: int
    rekor_integration_time: int

    @classmethod
    def from_bytes(cls, value: bytes) -> CodexAdapterDeploymentLockV1:
        """Parse only one canonical lock with an exact version-1 field set."""
        try:
            decoded = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexAdapterAdmissionError("deployment lock schema is invalid") from exc
        if not isinstance(decoded, dict) or set(decoded) != {field.name for field in fields(cls)}:
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        if value != _canonical_json(decoded):
            raise CodexAdapterAdmissionError("deployment lock is not canonical")
        converted = dict(decoded)
        tags = converted["wheel_tags"]
        manifest = converted["installed_tree_manifest"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        if not isinstance(manifest, list):
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        converted["wheel_tags"] = tuple(tags)
        converted["installed_tree_manifest"] = tuple(
            LockedTreeFileV1.from_mapping(item) for item in manifest
        )
        lock = cls(**converted)
        lock._validate_types()
        paths = tuple(item.path for item in lock.installed_tree_manifest)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise CodexAdapterAdmissionError("installed tree manifest must be sorted and unique")
        return lock

    def _validate_types(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        integer_names = {
            "adapter_api_version",
            "request_schema_version",
            "prepared_schema_version",
            "result_schema_version",
        }
        if any(
            not isinstance(getattr(self, name), int)
            or isinstance(getattr(self, name), bool)
            or getattr(self, name) != 1
            for name in integer_names
        ):
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        if (
            not isinstance(self.rekor_log_index, int)
            or isinstance(self.rekor_log_index, bool)
            or self.rekor_log_index < 0
            or not isinstance(self.rekor_integration_time, int)
            or isinstance(self.rekor_integration_time, bool)
            or self.rekor_integration_time < 0
        ):
            raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        tuple_names = {"wheel_tags", "installed_tree_manifest"}
        for field in fields(self):
            if field.name in integer_names | tuple_names | {
                "schema_version",
                "rekor_log_index",
                "rekor_integration_time",
            }:
                continue
            if not isinstance(getattr(self, field.name), str) or not getattr(self, field.name):
                raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        for name in (field.name for field in fields(self) if field.name.endswith("sha256")):
            if not _is_digest(getattr(self, name)):
                raise CodexAdapterAdmissionError("deployment lock schema is invalid")
        if (
            len(self.wheel_tags) != 3
            or any(_WHEEL_TAG_RE.fullmatch(tag) is None for tag in self.wheel_tags)
            or not Path(self.wheel_path).name.endswith(f"-{'-'.join(self.wheel_tags)}.whl")
        ):
            raise CodexAdapterAdmissionError("wheel tags do not match retained wheel")
        if (
            _ENTRY_POINT_NAME_RE.fullmatch(self.entry_point_name) is None
            or any(
                _PYTHON_NAME_RE.fullmatch(part) is None
                for part in self.entry_point_module.split(".")
            )
            or _PYTHON_NAME_RE.fullmatch(self.entry_point_factory) is None
        ):
            raise CodexAdapterAdmissionError("installed adapter entry point is invalid")


@dataclass(frozen=True, slots=True)
class CodexAdapterAdmission:
    """Return the host-verified adapter identity and exact factory."""

    lock: CodexAdapterDeploymentLockV1
    deployment_lock_sha256: str
    factory: object

    def validate_adapter_identity(
        self,
        *,
        distribution: str,
        version: str,
        installed_tree_sha256: str,
    ) -> None:
        """Reject adapter self-attestation that differs from the host lock."""
        expected = (
            self.lock.adapter_distribution,
            self.lock.adapter_version,
            self.lock.installed_tree_sha256,
        )
        if (distribution, version, installed_tree_sha256) != expected:
            raise CodexAdapterAdmissionError("adapter identity does not match deployment lock")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_component_permissions(value: os.stat_result, *, directory: bool) -> bool:
    """Return whether one path component has owner-controlled permissions."""
    if value.st_uid not in {0, os.getuid()}:
        return False
    if not value.st_mode & 0o022:
        return True
    # A root-owned sticky directory, such as /tmp, does not let another user
    # replace this user's entries. No writable non-sticky component is safe.
    return directory and value.st_uid == 0 and bool(value.st_mode & stat.S_ISVTX)


def _open_absolute_path(path: Path, *, directory: bool) -> int:
    """Open an absolute path one no-follow component at a time."""
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)) or not path.parts:
        raise CodexAdapterAdmissionError("deployment lock path is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path.anchor, directory_flags)
    except OSError as exc:
        raise CodexAdapterAdmissionError("deployment object is unavailable") from exc
    try:
        root_status = os.fstat(descriptor)
        if not _safe_component_permissions(root_status, directory=True):
            raise CodexAdapterAdmissionError("deployment path component is unsafe")
        for offset, part in enumerate(path.parts[1:], start=1):
            final = offset == len(path.parts) - 1
            flags = (
                directory_flags
                if not final or directory
                else (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
            )
            try:
                opened = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise CodexAdapterAdmissionError("deployment object is unavailable") from exc
            os.close(descriptor)
            descriptor = opened
            value = os.fstat(descriptor)
            expected_type = (
                stat.S_ISDIR(value.st_mode)
                if not final or directory
                else stat.S_ISREG(value.st_mode)
            )
            if not expected_type:
                raise CodexAdapterAdmissionError("deployment object has an invalid type")
            if not _safe_component_permissions(value, directory=not final or directory):
                raise CodexAdapterAdmissionError("deployment path component is unsafe")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_absolute_path(path: Path, *, directory: bool = False) -> Path:
    descriptor = _open_absolute_path(path, directory=directory)
    os.close(descriptor)
    return path


def _read_descriptor(descriptor: int, *, label: str) -> tuple[bytes, str]:
    data = bytearray()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        data.extend(chunk)
        offset += len(chunk)
        if offset > 128 * 1024 * 1024:
            raise CodexAdapterAdmissionError(f"{label} is too large")
    return bytes(data), hashlib.sha256(data).hexdigest()


def _read_locked_file(path: Path, *, label: str) -> tuple[bytes, str]:
    try:
        descriptor = _open_absolute_path(path, directory=False)
    except OSError as exc:
        raise CodexAdapterAdmissionError(f"{label} is unavailable") from exc
    try:
        identity = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise CodexAdapterAdmissionError(f"{label} identity changed")
        return _read_descriptor(descriptor, label=label)
    finally:
        os.close(descriptor)


def _require_artifact(path: str, expected: str, *, label: str) -> bytes:
    data, actual = _read_locked_file(Path(path), label=label)
    if actual != expected:
        raise CodexAdapterAdmissionError(f"{label} digest does not match deployment lock")
    return data


def _validate_fixed_identity(lock: CodexAdapterDeploymentLockV1) -> None:
    if (
        lock.entry_point_group != _ENTRY_POINT_GROUP
        or lock.codex_release_tag != _RELEASE_TAG
        or lock.codex_archive_asset != _ARCHIVE_ASSET
        or lock.codex_sigstore_asset != _SIGSTORE_ASSET
        or lock.codex_target != _TARGET
        or lock.fulcio_certificate_issuer != _FULCIO_ISSUER
        or lock.workflow_certificate_identity != _WORKFLOW_IDENTITY
        or lock.oidc_issuer != _OIDC_ISSUER
    ):
        raise CodexAdapterAdmissionError("certificate identity or release identity is invalid")
    if lock.guest_image_platform != "aarch64-linux":
        raise CodexAdapterAdmissionError("guest image platform is invalid")


def _validate_installed_tree(lock: CodexAdapterDeploymentLockV1) -> None:
    root = _validate_absolute_path(Path(lock.installed_tree_root), directory=True)
    expected = {entry.path: entry for entry in lock.installed_tree_manifest}
    if len(expected) != len(lock.installed_tree_manifest):
        raise CodexAdapterAdmissionError("installed tree manifest has duplicate paths")
    actual: dict[str, LockedTreeFileV1] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            if path.is_symlink():
                raise CodexAdapterAdmissionError("installed tree contains a link")
            continue
        if path.is_symlink() or not path.is_file():
            raise CodexAdapterAdmissionError("installed tree contains an invalid object")
        relative = path.relative_to(root).as_posix()
        data, digest = _read_locked_file(path, label="installed tree file")
        value = path.stat(follow_symlinks=False)
        actual[relative] = LockedTreeFileV1(
            path=relative,
            sha256=digest,
            size=len(data),
            mode=value.st_mode & 0o777,
        )
    if actual != expected:
        raise CodexAdapterAdmissionError("installed tree does not match deployment lock")
    manifest_value = [
        {"mode": item.mode, "path": item.path, "sha256": item.sha256, "size": item.size}
        for item in lock.installed_tree_manifest
    ]
    if hashlib.sha256(_canonical_json(manifest_value)).hexdigest() != lock.installed_tree_sha256:
        raise CodexAdapterAdmissionError("installed tree digest does not match deployment lock")

    distribution_stem = lock.adapter_distribution.replace("-", "_")
    dist_info = root / f"{distribution_stem}-{lock.adapter_version}.dist-info"
    record_path = dist_info / "RECORD"
    entry_points_path = dist_info / "entry_points.txt"
    try:
        record_text = _read_locked_file(record_path, label="installed tree RECORD")[0].decode(
            "utf-8"
        )
        entry_points_text = _read_locked_file(
            entry_points_path,
            label="installed tree entry points",
        )[0].decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CodexAdapterAdmissionError("installed tree metadata is invalid") from exc
    inventory = {
        row[0]
        for row in csv.reader(StringIO(record_text))
        if row and row[0] and not Path(row[0]).is_absolute() and ".." not in Path(row[0]).parts
    }
    if inventory != set(actual):
        raise CodexAdapterAdmissionError("installed tree RECORD does not match installed files")
    parser = ConfigParser(interpolation=None)
    try:
        parser.read_string(entry_points_text)
        entry_point_value = parser[_ENTRY_POINT_GROUP][lock.entry_point_name]
    except Exception as exc:
        raise CodexAdapterAdmissionError("installed adapter entry point is invalid") from exc
    if entry_point_value.strip() != f"{lock.entry_point_module}:{lock.entry_point_factory}":
        raise CodexAdapterAdmissionError("installed adapter entry point is invalid")


def _validate_elf(value: bytes) -> None:
    if (
        len(value) < 64
        or value[:4] != b"\x7fELF"
        or value[4] != 2
        or value[5] != 1
        or int.from_bytes(value[18:20], "little") != 183
    ):
        raise CodexAdapterAdmissionError("locked Codex executable is not AArch64 ELF")


def _validate_locked_rekor_evidence(
    lock: CodexAdapterDeploymentLockV1,
    *,
    bundle: object,
    trusted_root: object,
    public_key_bytes: bytes,
    checkpoint_bytes: bytes,
    inclusion_proof_bytes: bytes,
) -> None:
    """Bind retained Rekor objects to the verified bundle and trusted root."""
    try:
        log_entry = cast(Any, bundle).log_entry._inner
        proof = log_entry.inclusion_proof
        checkpoint = proof.checkpoint
        if checkpoint is None:
            raise ValueError
        expected_proof = _canonical_json(json.loads(proof.to_json()))
        matching_logs = [
            log
            for log in cast(Any, trusted_root)._inner.tlogs
            if log.log_id.key_id.hex() == lock.rekor_log_id
        ]
        evidence_matches = (
            log_entry.log_id.key_id.hex() == lock.rekor_log_id
            and log_entry.log_index == lock.rekor_log_index
            and log_entry.integrated_time == lock.rekor_integration_time
            and checkpoint.envelope.encode("utf-8") == checkpoint_bytes
            and expected_proof == inclusion_proof_bytes
            and len(matching_logs) == 1
            and matching_logs[0].public_key.raw_bytes == public_key_bytes
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodexAdapterAdmissionError("offline Rekor evidence is invalid") from exc
    if not evidence_matches:
        raise CodexAdapterAdmissionError("offline Rekor evidence does not match deployment lock")


def _default_offline_verifier(lock: CodexAdapterDeploymentLockV1) -> None:
    """Verify the Codex artifact with locked Sigstore material and no refresh."""
    try:
        from cryptography.x509.oid import NameOID
        from sigstore.models import Bundle, TrustedRoot
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity
        from sigstore_models.trustroot import v1 as trustroot_v1

        trusted_root_bytes = _require_artifact(
            lock.trusted_root_path,
            lock.trusted_root_sha256,
            label="offline trust artifact",
        )
        trusted_root = TrustedRoot(trustroot_v1.TrustedRoot.from_json(trusted_root_bytes))
        public_key_bytes = _require_artifact(
            lock.rekor_public_key_path,
            lock.rekor_public_key_sha256,
            label="offline trust artifact",
        )
        checkpoint_bytes = _require_artifact(
            lock.rekor_checkpoint_path,
            lock.rekor_checkpoint_sha256,
            label="offline trust artifact",
        )
        inclusion_proof_bytes = _require_artifact(
            lock.rekor_inclusion_proof_path,
            lock.rekor_inclusion_proof_sha256,
            label="offline trust artifact",
        )
        verifier = Verifier(trusted_root=trusted_root)
        bundle_bytes = _require_artifact(
            lock.sigstore_bundle_path,
            lock.sigstore_bundle_sha256,
            label="Codex release artifact",
        )
        bundle = Bundle.from_json(bundle_bytes)
        issuer = bundle.signing_certificate.issuer
        if [value.value for value in issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)] != [
            "sigstore.dev"
        ] or [value.value for value in issuer.get_attributes_for_oid(NameOID.COMMON_NAME)] != [
            "sigstore-intermediate"
        ]:
            raise CodexAdapterAdmissionError("certificate identity is invalid")
        _validate_locked_rekor_evidence(
            lock,
            bundle=bundle,
            trusted_root=trusted_root,
            public_key_bytes=public_key_bytes,
            checkpoint_bytes=checkpoint_bytes,
            inclusion_proof_bytes=inclusion_proof_bytes,
        )
        policy = Identity(
            identity=lock.workflow_certificate_identity,
            issuer=lock.oidc_issuer,
        )
        artifact = _require_artifact(
            lock.extracted_elf_path,
            lock.extracted_elf_sha256,
            label="locked Codex executable",
        )
        verifier.verify_artifact(artifact, bundle, policy)
    except CodexAdapterAdmissionError:
        raise
    except Exception as exc:
        raise CodexAdapterAdmissionError("offline Sigstore verification failed") from exc


def _default_importer(installed_tree_root: str, module_name: str, factory_name: str) -> object:
    """Load the selected module only from the verified installed tree."""
    root = Path(installed_tree_root).resolve(strict=True)
    search_path = [str(root)]
    module = None
    try:
        for index in range(1, len(module_name.split(".")) + 1):
            selected_name = ".".join(module_name.split(".")[:index])
            spec = importlib.machinery.PathFinder.find_spec(selected_name, search_path)
            if spec is None or spec.loader is None or spec.origin in {None, "built-in", "frozen"}:
                raise CodexAdapterAdmissionError("installed adapter module is invalid")
            origin = Path(spec.origin).resolve(strict=True)
            if not origin.is_relative_to(root):
                raise CodexAdapterAdmissionError("installed adapter module escaped locked tree")
            if index < len(module_name.split(".")):
                locations = spec.submodule_search_locations
                if locations is None:
                    raise CodexAdapterAdmissionError("installed adapter module is invalid")
                search_path = []
                for location in locations:
                    resolved_location = Path(location).resolve(strict=True)
                    if not resolved_location.is_relative_to(root):
                        raise CodexAdapterAdmissionError(
                            "installed adapter module escaped locked tree"
                        )
                    search_path.append(str(resolved_location))
            module = importlib.util.module_from_spec(spec)
            sys.modules[selected_name] = module
            spec.loader.exec_module(module)
    except CodexAdapterAdmissionError:
        raise
    except Exception as exc:
        raise CodexAdapterAdmissionError("installed adapter import failed") from exc
    if module is None:
        raise CodexAdapterAdmissionError("installed adapter module is invalid")
    factory = getattr(module, factory_name)
    if not callable(factory):
        raise CodexAdapterAdmissionError("installed adapter factory is invalid")
    return factory


def _virtualization_framework_available() -> bool:
    """Return whether the required macOS virtualization framework is present."""
    return _VIRTUALIZATION_FRAMEWORK.is_dir()


def _admit_verified_lock(
    lock: CodexAdapterDeploymentLockV1,
    *,
    actual_lock_sha256: str,
    selected_entry_point: str,
    offline_verifier: Callable[[CodexAdapterDeploymentLockV1], None],
    importer: Callable[[str, str, str], object],
) -> CodexAdapterAdmission:
    """Complete admission while the caller holds the detached lock open."""
    if selected_entry_point != lock.entry_point_name:
        raise CodexAdapterAdmissionError("adapter selection does not match deployment lock")
    _validate_fixed_identity(lock)

    _require_artifact(lock.wheel_path, lock.wheel_sha256, label="adapter wheel")
    _validate_installed_tree(lock)
    _require_artifact(lock.guest_image_path, lock.guest_image_sha256, label="guest image")
    for path_name, digest_name in (
        ("trusted_root_path", "trusted_root_sha256"),
        ("rekor_public_key_path", "rekor_public_key_sha256"),
        ("rekor_checkpoint_path", "rekor_checkpoint_sha256"),
        ("rekor_inclusion_proof_path", "rekor_inclusion_proof_sha256"),
    ):
        _require_artifact(
            cast(str, getattr(lock, path_name)),
            cast(str, getattr(lock, digest_name)),
            label="offline trust artifact",
        )
    if (
        lock.codex_archive_sha256 != _ARCHIVE_SHA256
        or lock.sigstore_bundle_sha256 != _SIGSTORE_SHA256
    ):
        raise CodexAdapterAdmissionError("Codex release artifact identity is invalid")
    _require_artifact(
        lock.codex_archive_path,
        lock.codex_archive_sha256,
        label="Codex release artifact",
    )
    _require_artifact(
        lock.sigstore_bundle_path,
        lock.sigstore_bundle_sha256,
        label="Codex release artifact",
    )
    elf = _require_artifact(
        lock.extracted_elf_path,
        lock.extracted_elf_sha256,
        label="locked Codex executable",
    )
    _validate_elf(elf)
    offline_verifier(lock)
    try:
        factory = importer(
            lock.installed_tree_root,
            lock.entry_point_module,
            lock.entry_point_factory,
        )
    except CodexAdapterAdmissionError:
        raise
    except Exception as exc:
        raise CodexAdapterAdmissionError("installed adapter import failed") from exc
    return CodexAdapterAdmission(
        lock=lock,
        deployment_lock_sha256=actual_lock_sha256,
        factory=factory,
    )


def admit_codex_adapter(
    *,
    lock_path: Path,
    expected_sha256: str,
    selected_entry_point: str,
    offline_verifier: Callable[[CodexAdapterDeploymentLockV1], None] = _default_offline_verifier,
    importer: Callable[[str, str, str], object] = _default_importer,
    host_platform: str | None = None,
    host_machine: str | None = None,
    virtualization_available: bool | None = None,
) -> CodexAdapterAdmission:
    """Admit one exact adapter only after all host-owned checks pass."""
    if not _is_digest(expected_sha256):
        raise CodexAdapterAdmissionError("deployment lock digest is invalid")
    if _ENTRY_POINT_NAME_RE.fullmatch(selected_entry_point) is None:
        raise CodexAdapterAdmissionError("adapter selection is invalid")
    if host_platform is None:
        host_platform = sys.platform
    if host_machine is None:
        host_machine = platform.machine()
    if virtualization_available is None:
        virtualization_available = _virtualization_framework_available()
    if host_platform != "darwin" or host_machine != "arm64" or not virtualization_available:
        raise CodexAdapterAdmissionError("Codex adapter host is not admitted")

    try:
        if stat.S_ISLNK(os.lstat(lock_path).st_mode):
            raise CodexAdapterAdmissionError("deployment lock path is invalid")
    except FileNotFoundError:
        pass
    try:
        descriptor = _open_absolute_path(lock_path, directory=False)
    except CodexAdapterAdmissionError:
        raise
    try:
        lock_bytes, actual_lock_sha256 = _read_descriptor(descriptor, label="deployment lock")
        current = os.stat(lock_path, follow_symlinks=False)
        held = os.fstat(descriptor)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise CodexAdapterAdmissionError("deployment lock identity changed")
        if actual_lock_sha256 != expected_sha256:
            raise CodexAdapterAdmissionError("deployment lock digest does not match expected value")
        return _admit_verified_lock(
            CodexAdapterDeploymentLockV1.from_bytes(lock_bytes),
            actual_lock_sha256=actual_lock_sha256,
            selected_entry_point=selected_entry_point,
            offline_verifier=offline_verifier,
            importer=importer,
        )
    finally:
        os.close(descriptor)


__all__ = [
    "CodexAdapterAdmission",
    "CodexAdapterAdmissionError",
    "CodexAdapterDeploymentLockV1",
    "LockedTreeFileV1",
    "admit_codex_adapter",
]
