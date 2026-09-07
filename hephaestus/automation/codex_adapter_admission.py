"""Admit one detached and locked Codex isolation adapter deployment."""

from __future__ import annotations

import ast
import base64
import contextlib
import csv
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from configparser import ConfigParser
from dataclasses import dataclass, fields, is_dataclass
from io import StringIO
from pathlib import Path
from types import MappingProxyType
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


@dataclass(frozen=True, slots=True)
class _VerifiedInstalledTree:
    """Keep the verified installed bytes that the importer can use."""

    root: Path
    files: Mapping[str, bytes]


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


_MAX_ADMISSION_ARTIFACT_BYTES = 256 * 1024 * 1024


def _read_descriptor(descriptor: int, *, label: str) -> tuple[bytes, str]:
    data = bytearray()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        data.extend(chunk)
        offset += len(chunk)
        if offset > _MAX_ADMISSION_ARTIFACT_BYTES:
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


def _validate_installed_tree(lock: CodexAdapterDeploymentLockV1) -> _VerifiedInstalledTree:
    root = _validate_absolute_path(Path(lock.installed_tree_root), directory=True)
    expected = {entry.path: entry for entry in lock.installed_tree_manifest}
    if len(expected) != len(lock.installed_tree_manifest):
        raise CodexAdapterAdmissionError("installed tree manifest has duplicate paths")
    actual: dict[str, LockedTreeFileV1] = {}
    verified_files: dict[str, bytes] = {}
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
        verified_files[relative] = data
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
        record_text = verified_files[record_path.relative_to(root).as_posix()].decode("utf-8")
        entry_points_text = verified_files[entry_points_path.relative_to(root).as_posix()].decode(
            "utf-8"
        )
    except (KeyError, UnicodeDecodeError) as exc:
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
    return _VerifiedInstalledTree(
        root=root,
        files=MappingProxyType(verified_files),
    )


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


def _verify_retained_sigstore(
    *,
    artifact: object,
    bundle_bytes: bytes,
    trusted_root_bytes: bytes,
    identity: str,
    issuer: str,
    checkpoint_bytes: bytes | None = None,
    inclusion_proof_bytes: bytes | None = None,
) -> tuple[object, object]:
    """Verify retained Sigstore objects without a trust-root refresh."""
    try:
        import base64

        from cryptography import x509
        from sigstore.hashes import Hashed
        from sigstore.models import Bundle, TransparencyLogEntry, TrustedRoot
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity
        from sigstore_models.trustroot import v1 as trustroot_v1

        trusted_root = TrustedRoot(trustroot_v1.TrustedRoot.from_json(trusted_root_bytes))
        try:
            bundle = Bundle.from_json(bundle_bytes)
        except Exception:
            legacy = json.loads(bundle_bytes)
            if (
                set(legacy) != {"base64Signature", "cert", "rekorBundle"}
                or checkpoint_bytes is None
                or inclusion_proof_bytes is None
            ):
                raise
            rekor = legacy["rekorBundle"]
            payload = rekor["Payload"]
            proof = json.loads(inclusion_proof_bytes)
            checkpoint = proof.get("checkpoint")
            if not isinstance(checkpoint, dict) or checkpoint.get("envelope") is None:
                raise ValueError("retained checkpoint is absent") from None
            if checkpoint["envelope"].encode("utf-8") != checkpoint_bytes:
                raise ValueError("retained checkpoint does not match proof") from None
            api_proof = {
                "checkpoint": checkpoint["envelope"],
                "hashes": [
                    base64.b64decode(value, validate=True).hex() for value in proof["hashes"]
                ],
                "logIndex": int(proof["logIndex"]),
                "rootHash": base64.b64decode(proof["rootHash"], validate=True).hex(),
                "treeSize": int(proof["treeSize"]),
            }
            response = {
                "retained": {
                    "body": payload["body"],
                    "integratedTime": payload["integratedTime"],
                    "logID": payload["logID"],
                    "logIndex": payload["logIndex"],
                    "verification": {
                        "signedEntryTimestamp": rekor["SignedEntryTimestamp"],
                        "inclusionProof": api_proof,
                    },
                }
            }
            certificate = x509.load_pem_x509_certificate(
                base64.b64decode(legacy["cert"], validate=True)
            )
            entry = TransparencyLogEntry._from_v1_response(response)
            bundle = Bundle.from_parts(
                certificate,
                base64.b64decode(legacy["base64Signature"], validate=True),
                entry,
            )
        verifier = Verifier(trusted_root=trusted_root)
        verifier.verify_artifact(
            cast(bytes | Hashed, artifact),
            bundle,
            Identity(identity=identity, issuer=issuer),
        )
        return bundle, trusted_root
    except Exception as exc:
        raise CodexAdapterAdmissionError("offline Sigstore verification failed") from exc


def _default_offline_verifier(lock: CodexAdapterDeploymentLockV1) -> None:
    """Verify the Codex artifact with locked Sigstore material and no refresh."""
    try:
        from cryptography.x509.oid import NameOID
        from sigstore.hashes import Hashed
        from sigstore_models.common.v1 import HashAlgorithm

        if (
            lock.codex_archive_sha256 != _ARCHIVE_SHA256
            or lock.sigstore_bundle_sha256 != _SIGSTORE_SHA256
        ):
            raise CodexAdapterAdmissionError("Codex release artifact identity is invalid")

        trusted_root_bytes = _require_artifact(
            lock.trusted_root_path,
            lock.trusted_root_sha256,
            label="offline trust artifact",
        )
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
        bundle_bytes = _require_artifact(
            lock.sigstore_bundle_path,
            lock.sigstore_bundle_sha256,
            label="Codex release artifact",
        )
        artifact = Hashed(
            algorithm=HashAlgorithm.SHA2_256,
            digest=bytes.fromhex(lock.extracted_elf_sha256),
        )
        bundle, trusted_root = _verify_retained_sigstore(
            artifact=artifact,
            bundle_bytes=bundle_bytes,
            trusted_root_bytes=trusted_root_bytes,
            identity=lock.workflow_certificate_identity,
            issuer=lock.oidc_issuer,
            checkpoint_bytes=checkpoint_bytes,
            inclusion_proof_bytes=inclusion_proof_bytes,
        )
        issuer = cast(Any, bundle).signing_certificate.issuer
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
    except CodexAdapterAdmissionError:
        raise
    except Exception as exc:
        raise CodexAdapterAdmissionError("offline Sigstore verification failed") from exc


def _validate_import_closure(tree: _VerifiedInstalledTree, module_name: str) -> None:
    """Reject imports that can select code outside the locked or host-owned closure."""
    adapter_root = module_name.split(".", maxsplit=1)[0]
    allowed_roots = set(sys.stdlib_module_names) | {adapter_root, "hephaestus"}
    forbidden_names = {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "object",
        "open",
        "setattr",
        "type",
        "vars",
    }
    for relative, source in tree.files.items():
        if not relative.endswith(".py"):
            continue
        try:
            syntax = ast.parse(source, filename=f"locked-adapter:{relative}")
        except (SyntaxError, ValueError) as exc:
            raise CodexAdapterAdmissionError("installed adapter module is invalid") from exc
        for node in ast.walk(syntax):
            if (isinstance(node, ast.Attribute) and node.attr.startswith("__")) or (
                isinstance(node, ast.Name) and node.id in forbidden_names
            ):
                raise CodexAdapterAdmissionError(
                    "installed adapter exceeds the closed capability surface"
                )
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = {node.module.split(".", maxsplit=1)[0]}
            else:
                continue
            if not roots <= allowed_roots:
                raise CodexAdapterAdmissionError(
                    "installed adapter dependency is outside the verified closure"
                )
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("hephaestus")
                and node.module != "hephaestus.agents.codex_isolation"
            ) or (
                isinstance(node, ast.Import)
                and any(
                    alias.name.startswith("hephaestus")
                    and alias.name != "hephaestus.agents.codex_isolation"
                    for alias in node.names
                )
            ):
                raise CodexAdapterAdmissionError(
                    "installed adapter dependency is outside the verified closure"
                )


_ISOLATED_ADAPTER_HELPER = r"""
import base64
import builtins
import dataclasses
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import signal
import sys
import sysconfig
import threading
import types


output_lock = threading.Lock()


def send(value, request_id=None):
    reply = dict(value)
    if request_id is not None:
        reply["id"] = request_id
    with output_lock:
        sys.stdout.write(json.dumps(reply, separators=(",", ":"), sort_keys=True) + "\n")
        sys.stdout.flush()


def fail(exc, request_id=None):
    code = getattr(exc, "code", None)
    send(
        {"ok": False, "code": code if isinstance(code, str) else None},
        request_id,
    )


def encode(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        if value_type.__module__ != "hephaestus.agents.codex_isolation":
            raise TypeError("adapter returned an unsupported value")
        return {
            "type": "dataclass",
            "name": value_type.__name__,
            "fields": {
                field.name: encode(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [encode(item) for item in value]}
    if isinstance(value, list):
        return {"type": "list", "items": [encode(item) for item in value]}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {"type": "dict", "items": {key: encode(item) for key, item in value.items()}}
    if value is None or type(value) in {bool, int, float, str}:
        return {"type": "scalar", "value": value}
    raise TypeError("adapter returned an unsupported value")


class MemoryLoader(importlib.abc.Loader):
    def __init__(self, source, is_package):
        self.source = source
        self.is_package = is_package

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        origin = module.__spec__.origin
        module.__dict__["__builtins__"] = (
            protocol_builtins
            if module.__name__ == "hephaestus.agents.codex_isolation"
            else safe_builtins
        )
        module.__dict__["__loader__"] = None
        module.__spec__.loader = None
        code = compile(self.source, origin, "exec")
        exec(code, module.__dict__)


class ClosedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, sources, adapter_root, permitted_paths):
        self.sources = sources
        self.adapter_root = adapter_root
        self.permitted_paths = tuple(permitted_paths)

    def _safe_stdlib_spec(self, fullname, path):
        for loader in (
            importlib.machinery.BuiltinImporter,
            importlib.machinery.FrozenImporter,
        ):
            spec = loader.find_spec(fullname)
            if spec is not None:
                return spec
        search_path = tuple(path) if path is not None else self.permitted_paths
        spec = importlib.machinery.PathFinder.find_spec(fullname, search_path)
        if spec is None:
            raise ModuleNotFoundError(fullname)
        origin = spec.origin
        if not isinstance(origin, str):
            raise ModuleNotFoundError(fullname)
        canonical = os.path.realpath(origin)
        if not any(
            canonical == root or canonical.startswith(root + os.sep)
            for root in self.permitted_paths
        ):
            raise ModuleNotFoundError(fullname)
        return spec

    def find_spec(self, fullname, path=None, target=None):
        del target
        source = self.sources.get(fullname)
        if source is not None:
            value, is_package = source
            return importlib.util.spec_from_loader(
                fullname,
                MemoryLoader(value, is_package),
                origin=f"locked-adapter:{fullname}",
                is_package=is_package,
            )
        root = fullname.split(".", 1)[0]
        if root == self.adapter_root or fullname.startswith("hephaestus."):
            raise ModuleNotFoundError(fullname)
        if root not in sys.stdlib_module_names:
            raise ModuleNotFoundError(fullname)
        return self._safe_stdlib_spec(fullname, path)


CAPABILITY_EXPORTS = {
    "base64": {
        "b64decode",
        "b64encode",
    },
    "contextlib": {
        "ExitStack",
        "contextmanager",
        "nullcontext",
        "suppress",
    },
    "dataclasses": {
        "FrozenInstanceError",
        "MISSING",
        "asdict",
        "astuple",
        "dataclass",
        "field",
        "fields",
        "is_dataclass",
        "replace",
    },
    "enum": {
        "Enum",
        "Flag",
        "IntEnum",
        "IntFlag",
        "StrEnum",
        "auto",
        "unique",
    },
    "hashlib": {
        "blake2b",
        "blake2s",
        "file_digest",
        "sha256",
        "sha512",
    },
    "itertools": {
        "chain",
        "count",
        "groupby",
        "islice",
        "pairwise",
        "product",
        "repeat",
        "zip_longest",
    },
    "json": {"JSONDecodeError", "JSONDecoder", "JSONEncoder", "dump", "dumps", "load", "loads"},
    "math": {
        "ceil",
        "floor",
        "fsum",
        "isclose",
        "isfinite",
        "isinf",
        "isnan",
        "prod",
        "sqrt",
    },
    "os": {
        "O_CLOEXEC",
        "O_CREAT",
        "O_DIRECTORY",
        "O_EXCL",
        "O_NOFOLLOW",
        "O_RDONLY",
        "O_RDWR",
        "O_TRUNC",
        "O_WRONLY",
        "chmod",
        "close",
        "dup",
        "dup2",
        "fchmod",
        "fdopen",
        "fork",
        "fspath",
        "fstat",
        "fsync",
        "getegid",
        "geteuid",
        "getgid",
        "getpgid",
        "getpid",
        "getppid",
        "getsid",
        "getuid",
        "kill",
        "killpg",
        "lseek",
        "lstat",
        "makedirs",
        "mkdir",
        "open",
        "pipe",
        "pread",
        "read",
        "readlink",
        "remove",
        "replace",
        "rmdir",
        "setpgid",
        "setsid",
        "stat",
        "unlink",
        "waitpid",
        "write",
    },
    "pathlib": {"Path", "PosixPath", "PurePath", "PurePosixPath"},
    "secrets": {"compare_digest", "token_bytes", "token_hex", "token_urlsafe"},
    "shutil": {
        "copy",
        "copy2",
        "copyfile",
        "copyfileobj",
        "move",
        "rmtree",
        "which",
    },
    "signal": {
        "SIGCHLD",
        "SIGINT",
        "SIGKILL",
        "SIGPIPE",
        "SIGTERM",
        "Signals",
        "pthread_kill",
        "pthread_sigmask",
        "signal",
        "strsignal",
    },
    "stat": {"S_IFDIR", "S_IFLNK", "S_IFMT", "S_IFREG", "S_IMODE", "S_ISDIR", "S_ISLNK", "S_ISREG"},
    "subprocess": {
        "CalledProcessError",
        "CompletedProcess",
        "DEVNULL",
        "PIPE",
        "Popen",
        "STDOUT",
        "SubprocessError",
        "TimeoutExpired",
        "run",
    },
    "sys": {"byteorder", "maxsize", "platform", "version_info"},
    "tempfile": {
        "NamedTemporaryFile",
        "TemporaryDirectory",
        "TemporaryFile",
        "mkdtemp",
        "mkstemp",
    },
    "threading": {
        "Condition",
        "Event",
        "Lock",
        "RLock",
        "Thread",
        "Timer",
        "current_thread",
        "get_ident",
    },
    "time": {
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "sleep",
        "time",
        "time_ns",
    },
    "typing": {
        "Any",
        "Callable",
        "ClassVar",
        "Final",
        "Iterable",
        "Iterator",
        "Literal",
        "Mapping",
        "Never",
        "Optional",
        "Protocol",
        "Sequence",
        "TypeAlias",
        "TypeVar",
        "cast",
        "final",
        "overload",
    },
}


class CapabilityModule(types.ModuleType):
    def __getattribute__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return types.ModuleType.__getattribute__(self, name)


def close_callback(callback):
    if callback is None or not callable(callback):
        return callback

    def callback_bridge(*args, **kwargs):
        closed_args = tuple(close_value(value) for value in args)
        closed_kwargs = {key: close_value(value) for key, value in kwargs.items()}
        return close_value(callback(*closed_args, **closed_kwargs))

    return callback_bridge


def close_callback_payload(value):
    if isinstance(value, tuple):
        return tuple(close_callback_payload(item) for item in value)
    if isinstance(value, list):
        return [close_callback_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: close_callback_payload(item) for key, item in value.items()}
    return close_value(value)


def call_callback_api(target, args, kwargs):
    positional = list(args)
    named = dict(kwargs)
    if target is threading.Thread:
        callback_index = 1
        callback_name = "target"
        payload_indexes = {3, 4}
        payload_names = {"args", "kwargs"}
    elif target is threading.Timer:
        callback_index = 1
        callback_name = "function"
        payload_indexes = {2, 3}
        payload_names = {"args", "kwargs"}
    else:
        callback_index = 1
        callback_name = "handler"
        payload_indexes = set()
        payload_names = set()

    if len(positional) > callback_index:
        positional[callback_index] = close_callback(positional[callback_index])
    elif callback_name in named:
        named[callback_name] = close_callback(named[callback_name])

    positional = [
        close_callback_payload(value) if index in payload_indexes else unwrap_value(value)
        for index, value in enumerate(positional)
    ]
    named = {
        key: close_callback_payload(value) if key in payload_names else unwrap_value(value)
        for key, value in named.items()
    }
    return target(*positional, **named)


class CapabilityObject:
    __slots__ = ("_capability_id",)

    def __init__(self, target):
        capability_id = id(self)
        capability_targets[capability_id] = target
        object.__setattr__(self, "_capability_id", capability_id)

    def _target(self):
        capability_id = object.__getattribute__(self, "_capability_id")
        return capability_targets[capability_id]

    def __getattribute__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        target = object.__getattribute__(self, "_target")()
        return close_value(getattr(target, name))

    def __call__(self, *args, **kwargs):
        target = object.__getattribute__(self, "_target")()
        if target in {threading.Thread, threading.Timer, signal.signal}:
            return close_value(call_callback_api(target, args, kwargs))
        return close_value(
            target(*close_callback_payload(args), **close_callback_payload(kwargs))
        )

    def __iter__(self):
        target = object.__getattribute__(self, "_target")()
        return close_value(iter(target))

    def __next__(self):
        target = object.__getattribute__(self, "_target")()
        return close_value(next(target))

    def __getitem__(self, key):
        target = object.__getattribute__(self, "_target")()
        return close_value(target[unwrap_value(key)])

    def __setitem__(self, key, value):
        target = object.__getattribute__(self, "_target")()
        target[unwrap_value(key)] = unwrap_value(value)

    def __enter__(self):
        target = object.__getattribute__(self, "_target")()
        return close_value(target.__enter__())

    def __exit__(self, *args):
        target = object.__getattribute__(self, "_target")()
        return target.__exit__(*unwrap_value(args))

    def __bool__(self):
        return bool(object.__getattribute__(self, "_target")())

    def __len__(self):
        return len(object.__getattribute__(self, "_target")())

    def __fspath__(self):
        return os.fspath(object.__getattribute__(self, "_target")())

    def __str__(self):
        return str(object.__getattribute__(self, "_target")())

    def __repr__(self):
        return repr(object.__getattribute__(self, "_target")())

    def __truediv__(self, other):
        target = object.__getattribute__(self, "_target")()
        return close_value(target / unwrap_value(other))

    def __rtruediv__(self, other):
        target = object.__getattribute__(self, "_target")()
        return close_value(unwrap_value(other) / target)

closed_values = {}
capability_targets = {}


def unwrap_value(value):
    if type(value) is CapabilityObject:
        return object.__getattribute__(value, "_target")()
    if isinstance(value, tuple):
        return tuple(unwrap_value(item) for item in value)
    if isinstance(value, dict):
        return {key: unwrap_value(item) for key, item in value.items()}
    return value


def close_value(value):
    if value is None or type(value) in {bool, bytes, float, int, str}:
        return value
    if type(value) is CapabilityObject:
        return value
    value_module = getattr(value, "__module__", getattr(type(value), "__module__", ""))
    if value_module == "hephaestus.agents.codex_isolation" or value_module == adapter_root or (
        isinstance(value_module, str) and value_module.startswith(adapter_root + ".")
    ):
        return value
    cached = closed_values.get(id(value))
    if cached is not None:
        return cached
    if isinstance(value, types.ModuleType):
        module_name = value.__name__
        root = module_name.split(".", 1)[0]
        exports = CAPABILITY_EXPORTS.get(root)
        if exports is None:
            raise AttributeError(module_name)
        closed = CapabilityModule(module_name)
        closed_values[id(value)] = closed
        for name in exports:
            if hasattr(value, name):
                setattr(closed, name, close_value(getattr(value, name)))
        return closed
    if isinstance(value, dict):
        closed = types.MappingProxyType(
            {close_value(key): close_value(item) for key, item in value.items()}
        )
        closed_values[id(value)] = closed
        return closed
    if isinstance(value, (list, tuple)):
        closed = tuple(close_value(item) for item in value)
        closed_values[id(value)] = closed
        return closed
    if isinstance(value, (set, frozenset)):
        closed = frozenset(close_value(item) for item in value)
        closed_values[id(value)] = closed
        return closed
    closed = CapabilityObject(value)
    closed_values[id(value)] = closed
    return closed


def decode(value, protocol):
    value_type = value.get("type")
    if value_type == "scalar":
        return value["value"]
    if value_type == "tuple":
        return tuple(decode(item, protocol) for item in value["items"])
    if value_type == "list":
        return [decode(item, protocol) for item in value["items"]]
    if value_type == "dict":
        return {key: decode(item, protocol) for key, item in value["items"].items()}
    if value_type == "dataclass":
        name = value["name"]
        if not name.startswith("Codex") or not name.endswith("V1"):
            raise TypeError("adapter request type is invalid")
        cls = getattr(protocol, name)
        return cls(**{key: decode(item, protocol) for key, item in value["fields"].items()})
    raise TypeError("adapter request value is invalid")


try:
    payload = json.loads(sys.stdin.readline())
    adapter_root = payload["module"].split(".", 1)[0]
    sources = {}
    for relative, encoded in payload["files"].items():
        if relative.endswith("/__init__.py"):
            name = relative[:-12].replace("/", ".")
            is_package = True
        elif relative.endswith(".py"):
            name = relative[:-3].replace("/", ".")
            is_package = False
        else:
            continue
        sources[name] = (base64.b64decode(encoded), is_package)
    sources["hephaestus.agents.codex_isolation"] = (
        base64.b64decode(payload["protocol_source"]),
        False,
    )
    hephaestus = types.ModuleType("hephaestus")
    hephaestus.__path__ = []
    agents = types.ModuleType("hephaestus.agents")
    agents.__path__ = []
    hephaestus.agents = agents
    sys.modules["hephaestus"] = hephaestus
    sys.modules["hephaestus.agents"] = agents
    standard_library = sysconfig.get_path("stdlib")
    platform_library = sysconfig.get_path("platstdlib")
    permitted_paths = {
        os.path.realpath(standard_library),
        os.path.realpath(platform_library),
        os.path.realpath(os.path.join(standard_library, "lib-dynload")),
        os.path.realpath(os.path.join(platform_library, "lib-dynload")),
    }
    sys.path[:] = [value for value in sys.path if value in permitted_paths]
    finder = ClosedFinder(sources, adapter_root, permitted_paths)
    sys.meta_path[:] = [finder]

    protocol_name = "hephaestus.agents.codex_isolation"
    safe_importlib = types.ModuleType("importlib")
    safe_builtins_module = types.ModuleType("builtins")

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".", 1)[0]
        if (
            name == protocol_name
            and isinstance(globals, dict)
            and globals.get("__name__") != protocol_name
        ):
            return protocol_capability
        if root == "builtins":
            if name != "builtins":
                raise ModuleNotFoundError(name)
            return safe_builtins_module
        if root == "importlib":
            if name != "importlib":
                raise ModuleNotFoundError(name)
            return safe_importlib
        if root == "sys":
            if name != "sys":
                raise ModuleNotFoundError(name)
        imported = builtins.__import__(name, globals, locals, fromlist, level)
        if root == adapter_root or (
            isinstance(globals, dict) and globals.get("__name__") == protocol_name
        ):
            return imported
        return close_value(imported)

    def safe_import_module(name, package=None):
        if package is not None or name.startswith("."):
            raise ModuleNotFoundError(name)
        return guarded_import(name, fromlist=("*",))

    safe_importlib.import_module = safe_import_module
    safe_builtin_names = {
        "ArithmeticError", "AssertionError", "AttributeError", "BaseException",
        "BlockingIOError", "BrokenPipeError", "BufferError", "BytesWarning",
        "ChildProcessError", "ConnectionAbortedError", "ConnectionError",
        "ConnectionRefusedError", "ConnectionResetError", "DeprecationWarning",
        "EOFError", "EncodingWarning", "EnvironmentError", "Exception",
        "FileExistsError", "FileNotFoundError", "FloatingPointError", "FutureWarning",
        "GeneratorExit", "IOError", "ImportError", "ImportWarning", "IndentationError",
        "IndexError", "InterruptedError", "IsADirectoryError", "KeyError",
        "KeyboardInterrupt", "LookupError", "MemoryError", "ModuleNotFoundError",
        "NameError", "NotADirectoryError", "NotImplemented", "NotImplementedError",
        "OSError", "OverflowError", "PendingDeprecationWarning", "PermissionError",
        "ProcessLookupError", "RecursionError", "ReferenceError", "ResourceWarning",
        "RuntimeError", "RuntimeWarning", "StopAsyncIteration", "StopIteration",
        "SyntaxError", "SyntaxWarning", "SystemError", "SystemExit", "TabError",
        "TimeoutError", "TypeError", "UnboundLocalError", "UnicodeDecodeError",
        "UnicodeEncodeError", "UnicodeError", "UnicodeTranslateError", "UnicodeWarning",
        "UserWarning", "ValueError", "Warning", "ZeroDivisionError", "__build_class__",
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
        "callable", "chr", "classmethod", "complex", "dict", "divmod", "enumerate",
        "filter", "float", "format", "frozenset", "hash", "hex", "int",
        "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "memoryview", "min", "next", "oct", "ord", "pow", "print", "property",
        "range", "repr", "reversed", "round", "set", "slice", "sorted",
        "staticmethod", "str", "sum", "super", "tuple", "zip",
    }
    for name in safe_builtin_names:
        setattr(safe_builtins_module, name, getattr(builtins, name))
    safe_builtins_module.__import__ = guarded_import
    safe_builtins = {name: getattr(builtins, name) for name in safe_builtin_names}
    safe_builtins["__import__"] = guarded_import
    protocol_builtins = dict(vars(builtins))
    protocol_builtins["__import__"] = guarded_import

    protocol = importlib.import_module(protocol_name)
    protocol_capability = CapabilityModule(protocol_name)
    for name in protocol.__all__:
        setattr(protocol_capability, name, getattr(protocol, name))
    selected_module = importlib.import_module(payload["module"])
    factory = getattr(selected_module, payload["factory"])
    if not callable(factory):
        raise TypeError("adapter factory is invalid")
    send({"ok": True, "kind": "ready"})
except BaseException as exc:
    fail(exc)
    raise SystemExit(1)

adapter = None
prepared_handles = {}
prepared_handles_lock = threading.Lock()


def store_prepared(value):
    while True:
        cleanup_handle = os.urandom(32).hex()
        with prepared_handles_lock:
            if cleanup_handle not in prepared_handles:
                prepared_handles[cleanup_handle] = value
                return cleanup_handle


def read_prepared(cleanup_handle):
    if type(cleanup_handle) is not str:
        raise TypeError("prepared cleanup handle is invalid")
    with prepared_handles_lock:
        if cleanup_handle not in prepared_handles:
            raise TypeError("prepared cleanup handle is invalid")
        return prepared_handles[cleanup_handle]


def take_prepared(cleanup_handle):
    if type(cleanup_handle) is not str:
        raise TypeError("prepared cleanup handle is invalid")
    with prepared_handles_lock:
        try:
            return prepared_handles.pop(cleanup_handle)
        except KeyError:
            raise TypeError("prepared cleanup handle is invalid") from None


def handle(request):
    global adapter
    request_id = request.get("id")
    try:
        if type(request_id) is not int:
            raise TypeError("adapter request id is invalid")
        operation = request["operation"]
        if operation == "factory":
            adapter = factory()
            if all(
                callable(getattr(adapter, name, None))
                for name in ("prepare", "invoke", "destroy")
            ):
                send(
                    {
                        "ok": True,
                        "kind": "adapter",
                        "identity": {
                            name: getattr(adapter, name, None)
                            for name in (
                                "adapter_distribution",
                                "adapter_version",
                                "installed_tree_sha256",
                            )
                        },
                    },
                    request_id,
                )
            else:
                send(
                    {"ok": True, "kind": "value", "value": encode(adapter)},
                    request_id,
                )
        elif operation == "prepare" and adapter is not None:
            arguments = [decode(item, protocol) for item in request["arguments"]]
            result = adapter.prepare(*arguments)
            cleanup_handle = store_prepared(result)
            try:
                encoded = encode(result)
            except BaseException as exc:
                code = getattr(exc, "code", None)
                send(
                    {
                        "ok": False,
                        "code": code if isinstance(code, str) else None,
                        "cleanup_handle": cleanup_handle,
                    },
                    request_id,
                )
            else:
                send(
                    {
                        "ok": True,
                        "kind": "prepared",
                        "cleanup_handle": cleanup_handle,
                        "value": encoded,
                    },
                    request_id,
                )
        elif operation == "invoke_prepared" and adapter is not None:
            prepared = read_prepared(request["cleanup_handle"])
            arguments = [decode(item, protocol) for item in request["arguments"]]
            result = adapter.invoke(prepared, *arguments)
            send({"ok": True, "kind": "value", "value": encode(result)}, request_id)
        elif operation == "destroy_prepared" and adapter is not None:
            prepared = take_prepared(request["cleanup_handle"])
            result = adapter.destroy(prepared)
            send({"ok": True, "kind": "value", "value": encode(result)}, request_id)
        else:
            raise TypeError("adapter operation is invalid")
    except BaseException as exc:
        fail(exc, request_id)


for line in sys.stdin:
    try:
        request = json.loads(line)
    except BaseException as exc:
        fail(exc)
        continue
    threading.Thread(target=handle, args=(request,), daemon=True).start()
"""


def _encode_adapter_value(value: object) -> dict[str, object]:
    """Encode one version-1 protocol value for the isolated helper."""
    if is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        if (
            value_type.__module__ != "hephaestus.agents.codex_isolation"
            or not value_type.__name__.endswith("V1")
        ):
            raise CodexAdapterAdmissionError("isolated adapter value is invalid")
        return {
            "type": "dataclass",
            "name": value_type.__name__,
            "fields": {
                field.name: _encode_adapter_value(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_encode_adapter_value(item) for item in value]}
    if isinstance(value, list):
        return {"type": "list", "items": [_encode_adapter_value(item) for item in value]}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            "type": "dict",
            "items": {key: _encode_adapter_value(item) for key, item in value.items()},
        }
    if value is None or type(value) in {bool, int, float, str}:
        return {"type": "scalar", "value": value}
    raise CodexAdapterAdmissionError("isolated adapter value is invalid")


def _decode_adapter_value(value: object) -> object:
    """Decode one version-1 protocol value from the isolated helper."""
    if not isinstance(value, dict) or type(value.get("type")) is not str:
        raise CodexAdapterAdmissionError("isolated adapter result is invalid")
    value_type = value["type"]
    if value_type == "scalar":
        scalar = value.get("value")
        if scalar is None or type(scalar) in {bool, int, float, str}:
            return scalar
    elif value_type in {"tuple", "list"}:
        items = value.get("items")
        if isinstance(items, list):
            decoded = [_decode_adapter_value(item) for item in items]
            return tuple(decoded) if value_type == "tuple" else decoded
    elif value_type == "dict":
        items = value.get("items")
        if isinstance(items, dict) and all(isinstance(key, str) for key in items):
            return {key: _decode_adapter_value(item) for key, item in items.items()}
    elif value_type == "dataclass":
        from hephaestus.agents import codex_isolation

        name = value.get("name")
        values = value.get("fields")
        if (
            isinstance(name, str)
            and name.startswith("Codex")
            and name.endswith("V1")
            and isinstance(values, dict)
            and all(isinstance(key, str) for key in values)
        ):
            record_type = getattr(codex_isolation, name, None)
            if isinstance(record_type, type) and is_dataclass(record_type):
                return record_type(
                    **{key: _decode_adapter_value(item) for key, item in values.items()}
                )
    raise CodexAdapterAdmissionError("isolated adapter result is invalid")


class _IsolatedAdapterProcess:
    """Run one verified adapter instance in an isolated Python process."""

    def __init__(
        self,
        installed_tree: _VerifiedInstalledTree,
        module_name: str,
        factory_name: str,
    ) -> None:
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._condition = threading.Condition()
        self._responses: dict[int, dict[str, object]] = {}
        self._reader_error: BaseException | None = None
        self._next_request_id = 0
        self._closed = False
        protocol_path = Path(__file__).parents[1] / "agents" / "codex_isolation.py"
        try:
            protocol_source = protocol_path.read_bytes()
        except OSError as exc:
            raise CodexAdapterAdmissionError("host adapter protocol is unavailable") from exc
        payload = {
            "module": module_name,
            "factory": factory_name,
            "files": {
                path: base64.b64encode(value).decode("ascii")
                for path, value in installed_tree.files.items()
            },
            "protocol_source": base64.b64encode(protocol_source).decode("ascii"),
        }
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _ISOLATED_ADAPTER_HELPER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                cwd="/",
                close_fds=True,
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
            self._write(payload)
            ready = self._read()
            if ready.get("kind") != "ready":
                raise CodexAdapterAdmissionError("installed adapter import failed")
            self._reader = threading.Thread(
                target=self._read_replies,
                daemon=True,
                name="codex-adapter-replies",
            )
            self._reader.start()
        except BaseException:
            self.close()
            raise

    def _write(self, value: object) -> None:
        stream = self._process.stdin
        if stream is None or self._closed:
            raise CodexAdapterAdmissionError("isolated adapter process is unavailable")
        try:
            with self._write_lock:
                stream.write(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n")
                stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise CodexAdapterAdmissionError("isolated adapter process failed") from exc

    def _read(self) -> dict[str, object]:
        stream = self._process.stdout
        if stream is None:
            raise CodexAdapterAdmissionError("isolated adapter process is unavailable")
        try:
            line = stream.readline()
            reply = json.loads(line)
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexAdapterAdmissionError("isolated adapter process failed") from exc
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            code = reply.get("code") if isinstance(reply, dict) else None
            if isinstance(code, str):
                from hephaestus.agents.codex_isolation import CodexIsolationError

                try:
                    raise CodexIsolationError(code)
                except ValueError:
                    pass
            raise CodexAdapterAdmissionError("isolated adapter operation failed")
        return cast(dict[str, object], reply)

    def _read_replies(self) -> None:
        """Route concurrent helper replies to their host request."""
        stream = self._process.stdout
        try:
            if stream is None:
                raise CodexAdapterAdmissionError("isolated adapter process is unavailable")
            for line in stream:
                reply = json.loads(line)
                if not isinstance(reply, dict) or type(reply.get("id")) is not int:
                    raise CodexAdapterAdmissionError("isolated adapter process failed")
                with self._condition:
                    self._responses[cast(int, reply["id"])] = cast(dict[str, object], reply)
                    self._condition.notify_all()
            raise CodexAdapterAdmissionError("isolated adapter process stopped")
        except BaseException as exc:
            with self._condition:
                self._reader_error = exc
                self._condition.notify_all()

    def request(
        self,
        operation: str,
        *arguments: object,
        cleanup_handle: str | None = None,
    ) -> dict[str, object]:
        """Run one factory or adapter operation."""
        with self._condition:
            request_id = self._next_request_id
            self._next_request_id += 1
        payload: dict[str, object] = {
            "id": request_id,
            "operation": operation,
            "arguments": [_encode_adapter_value(argument) for argument in arguments],
        }
        if cleanup_handle is not None:
            payload["cleanup_handle"] = cleanup_handle
        self._write(payload)
        with self._condition:
            while request_id not in self._responses and self._reader_error is None:
                self._condition.wait()
            if request_id not in self._responses:
                raise CodexAdapterAdmissionError("isolated adapter process failed") from None
            reply = self._responses.pop(request_id)
        if (
            operation == "prepare"
            and isinstance(reply, dict)
            and type(reply.get("cleanup_handle")) is str
        ):
            return reply
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            code = reply.get("code") if isinstance(reply, dict) else None
            if isinstance(code, str):
                from hephaestus.agents.codex_isolation import CodexIsolationError

                try:
                    raise CodexIsolationError(code)
                except ValueError:
                    pass
            raise CodexAdapterAdmissionError("isolated adapter operation failed")
        return reply

    def close(self) -> None:
        """Stop the helper process and close its control pipes."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            process = getattr(self, "_process", None)
            if process is None:
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
            with self._condition:
                if self._reader_error is None:
                    self._reader_error = CodexAdapterAdmissionError(
                        "isolated adapter process stopped"
                    )
                self._condition.notify_all()

    def __del__(self) -> None:
        self.close()


class _IsolatedAdapter:
    """Proxy one adapter instance in the isolated helper process."""

    def __init__(
        self,
        process: _IsolatedAdapterProcess,
        *,
        adapter_distribution: str,
        adapter_version: str,
        installed_tree_sha256: str,
    ) -> None:
        self._process = process
        self.adapter_distribution = adapter_distribution
        self.adapter_version = adapter_version
        self.installed_tree_sha256 = installed_tree_sha256
        self._prepared_lock = threading.Lock()
        self._prepared_handles: dict[int, tuple[object, str]] = {}

    def _claim_prepared_handle(self, prepared: object, *, remove: bool) -> str:
        """Get one helper-owned prepared handle from its host value."""
        with self._prepared_lock:
            retained = self._prepared_handles.get(id(prepared))
            if retained is None or retained[0] is not prepared:
                raise CodexAdapterAdmissionError("isolated adapter result is invalid")
            if remove:
                self._prepared_handles.pop(id(prepared))
            return retained[1]

    def _destroy_handle(self, cleanup_handle: str) -> None:
        """Destroy the raw prepared value behind one helper handle."""
        value = _decode_adapter_value(
            self._process.request(
                "destroy_prepared",
                cleanup_handle=cleanup_handle,
            ).get("value")
        )
        if value is not None:
            raise CodexAdapterAdmissionError("isolated adapter result is invalid")

    def prepare(self, request: object) -> object:
        """Prepare one isolated adapter request."""
        reply = self._process.request("prepare", request)
        cleanup_handle = reply.get("cleanup_handle")
        if type(cleanup_handle) is not str:
            raise CodexAdapterAdmissionError("isolated adapter result is invalid")
        from hephaestus.agents.codex_isolation import _CodexPrepareCleanupError

        if reply.get("ok") is not True:
            raise _CodexPrepareCleanupError(lambda: self._destroy_handle(cleanup_handle))
        try:
            prepared = _decode_adapter_value(reply.get("value"))
        except BaseException as exc:
            raise _CodexPrepareCleanupError(lambda: self._destroy_handle(cleanup_handle)) from exc
        with self._prepared_lock:
            self._prepared_handles[id(prepared)] = (prepared, cleanup_handle)
        return prepared

    def invoke(self, prepared: object, auth_path: str) -> object:
        """Invoke one prepared isolated adapter request."""
        cleanup_handle = self._claim_prepared_handle(prepared, remove=False)
        return _decode_adapter_value(
            self._process.request(
                "invoke_prepared",
                auth_path,
                cleanup_handle=cleanup_handle,
            ).get("value")
        )

    def destroy(self, prepared: object) -> None:
        """Destroy one prepared isolated adapter request."""
        cleanup_handle = self._claim_prepared_handle(prepared, remove=True)
        self._destroy_handle(cleanup_handle)

    def _close(self) -> None:
        """Close the private host control process."""
        self._process.close()

    def __del__(self) -> None:
        self._process.close()


class _IsolatedFactory:
    """Proxy one verified factory in the isolated helper process."""

    codex_isolation_api_version = 1

    def __init__(self, process: _IsolatedAdapterProcess) -> None:
        self._process = process
        self._owns_process = True

    def __call__(self) -> object:
        reply = self._process.request("factory")
        if reply.get("kind") == "adapter":
            identity = reply.get("identity")
            if not isinstance(identity, dict) or any(
                type(identity.get(name)) is not str
                for name in (
                    "adapter_distribution",
                    "adapter_version",
                    "installed_tree_sha256",
                )
            ):
                self._process.close()
                raise CodexAdapterAdmissionError("isolated adapter identity is invalid")
            adapter = _IsolatedAdapter(
                self._process,
                adapter_distribution=cast(str, identity["adapter_distribution"]),
                adapter_version=cast(str, identity["adapter_version"]),
                installed_tree_sha256=cast(str, identity["installed_tree_sha256"]),
            )
            self._owns_process = False
            return adapter
        try:
            return _decode_adapter_value(reply.get("value"))
        finally:
            self._process.close()

    def _close(self) -> None:
        """Close the private helper before adapter ownership transfers."""
        self._process.close()

    def __del__(self) -> None:
        if self._owns_process:
            self._process.close()


def _default_importer(
    installed_tree: _VerifiedInstalledTree,
    module_name: str,
    factory_name: str,
) -> object:
    """Load the verified adapter in an isolated helper process."""
    _validate_import_closure(installed_tree, module_name)
    try:
        return _IsolatedFactory(_IsolatedAdapterProcess(installed_tree, module_name, factory_name))
    except Exception as exc:
        raise CodexAdapterAdmissionError("installed adapter import failed") from exc


def _virtualization_framework_available() -> bool:
    """Return whether the required macOS virtualization framework is present."""
    return _VIRTUALIZATION_FRAMEWORK.is_dir()


def _admit_verified_lock(
    lock: CodexAdapterDeploymentLockV1,
    *,
    actual_lock_sha256: str,
    selected_entry_point: str,
    offline_verifier: Callable[[CodexAdapterDeploymentLockV1], None],
    importer: Callable[..., object],
) -> CodexAdapterAdmission:
    """Complete admission while the caller holds the detached lock open."""
    if selected_entry_point != lock.entry_point_name:
        raise CodexAdapterAdmissionError("adapter selection does not match deployment lock")
    _validate_fixed_identity(lock)

    _require_artifact(lock.wheel_path, lock.wheel_sha256, label="adapter wheel")
    installed_tree = _validate_installed_tree(lock)
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
        importer_root: str | _VerifiedInstalledTree = lock.installed_tree_root
        if importer is _default_importer:
            importer_root = installed_tree
        factory = importer(importer_root, lock.entry_point_module, lock.entry_point_factory)
    except CodexAdapterAdmissionError:
        raise
    except Exception as exc:
        raise CodexAdapterAdmissionError("installed adapter import failed") from exc
    return CodexAdapterAdmission(
        lock=lock,
        deployment_lock_sha256=actual_lock_sha256,
        factory=factory,
    )


def _admit_codex_adapter(  # noqa: C901 - one fail-closed admission transaction
    *,
    lock_path: Path,
    expected_sha256: str,
    selected_entry_point: str,
    offline_verifier: Callable[[CodexAdapterDeploymentLockV1], None] | None = None,
    importer: Callable[..., object] | None = None,
    host_platform: str | None = None,
    host_machine: str | None = None,
    virtualization_available: bool | None = None,
) -> CodexAdapterAdmission:
    """Admit one exact adapter with explicit test seams."""
    if offline_verifier is None:
        offline_verifier = _default_offline_verifier
    if importer is None:
        importer = _default_importer
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


def admit_codex_adapter(
    *,
    lock_path: Path,
    expected_sha256: str,
    selected_entry_point: str,
) -> CodexAdapterAdmission:
    """Admit one exact adapter through the single production authority."""
    return _admit_codex_adapter(
        lock_path=lock_path,
        expected_sha256=expected_sha256,
        selected_entry_point=selected_entry_point,
    )


__all__ = [
    "CodexAdapterAdmission",
    "CodexAdapterAdmissionError",
    "CodexAdapterDeploymentLockV1",
    "LockedTreeFileV1",
    "admit_codex_adapter",
]
