"""Pinned Athena contract receipts for Mnemosyne-backed skills.

The Pi package catalog is the source of truth for which Athena repository and
commit Hephaestus admits.  This module binds that catalog entry to the exact
skill contracts that define Mnemosyne dependency resolution, advice retrieval,
and PR-backed learning.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hephaestus.agents.pi_plugins import (
    PiPackageCatalog,
    PiPreflightResult,
    load_pi_package_catalog,
    preflight_pi_environment,
    prove_athena_skill_command,
)


class AthenaContractError(RuntimeError):
    """Raised when the pinned Athena contract cannot be proven."""


_ATHENA_PACKAGE_KEY = "athena"
_CONTRACT_FILES = {
    "advise_sha256": Path("skills/advise/SKILL.md"),
    "learn_sha256": Path("skills/learn/SKILL.md"),
    "dependency_resolution_sha256": Path("docs/dependency-resolution.md"),
}
type PinnedContentReader = Callable[[Path, str, Path], bytes]


@dataclass(frozen=True)
class AthenaContractReceipt:
    """Checksum-bound receipt for the Athena skill contract used by Hephaestus."""

    athena_repository: str
    athena_commit: str
    advise_sha256: str
    learn_sha256: str
    dependency_resolution_sha256: str
    trust_source: str

    @property
    def requires_flat_skill_corpus(self) -> bool:
        """Return whether selected Mnemosyne entries must come from flat skills."""
        return bool(self.advise_sha256 and self.dependency_resolution_sha256)

    @property
    def requires_pr_backed_learning(self) -> bool:
        """Return whether learning success must be backed by a readback PR."""
        return bool(self.learn_sha256 and self.dependency_resolution_sha256)

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serializable receipt dictionary."""
        return asdict(self)


def _athena_package(catalog: PiPackageCatalog) -> tuple[str, str]:
    for package in catalog.packages:
        if package.key == _ATHENA_PACKAGE_KEY:
            return package.identity, package.pin
    raise AthenaContractError("Pi package catalog does not contain Athena")


def _sha256_file(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise AthenaContractError(f"missing Athena contract file: {path}") from exc
    return hashlib.sha256(content).hexdigest()


def _pinned_git_content(root: Path, commit: str, relative: Path) -> bytes:
    """Read one contract blob from Athena's immutable catalog-pinned tree."""
    try:
        result = subprocess.run(
            ("git", "-C", str(root), "show", f"{commit}:{relative.as_posix()}"),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AthenaContractError(
            f"cannot read pinned Athena contract content: {relative}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise AthenaContractError(
            f"cannot read pinned Athena contract content {relative}: {detail or result.returncode}"
        )
    return result.stdout


def _preflight_athena_root(
    preflight: PiPreflightResult,
    catalog: PiPackageCatalog,
) -> Path:
    """Return the exact Athena checkout proven by a ready Pi preflight."""
    if not preflight.ready or preflight.inventory is None or not preflight.inventory.ready:
        raise AthenaContractError("Athena contract requires a ready Pi package preflight")
    repository, commit = _athena_package(catalog)
    root = preflight.inventory.roots.get(_ATHENA_PACKAGE_KEY)
    if root is None:
        raise AthenaContractError("Pi package preflight lacks the Athena package root")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise AthenaContractError("Pi preflight Athena package root is unavailable") from exc

    for command in ("skill:advise", "skill:learn"):
        try:
            command_receipt = prove_athena_skill_command(command, preflight)
        except ValueError as exc:
            raise AthenaContractError(
                f"Pi preflight does not prove Athena command {command!r}"
            ) from exc
        try:
            receipt_root = Path(command_receipt.package_root).resolve(strict=True)
        except OSError as exc:
            raise AthenaContractError(
                "Pi Athena command receipt package root is unavailable"
            ) from exc
        if (
            command_receipt.package_key != _ATHENA_PACKAGE_KEY
            or command_receipt.repository != repository
            or command_receipt.commit != commit
            or receipt_root != resolved_root
        ):
            raise AthenaContractError(
                f"Pi preflight receipt is not bound to catalog-pinned {command}"
            )
    return resolved_root


def load_athena_contract_receipt(
    *,
    contract_root: Path | None = None,
    catalog: PiPackageCatalog | None = None,
    preflight: PiPreflightResult | None = None,
    pinned_content_reader: PinnedContentReader = _pinned_git_content,
    trust_source: str = "pi-package-catalog",
) -> AthenaContractReceipt:
    """Load a checksum-bound receipt for the pinned Athena contract.

    Args:
        contract_root: Optional assertion of the contract root. It must resolve
            to the Athena package root proven by ``preflight``.
        catalog: Optional already-loaded Pi package catalog.
        preflight: Optional preflight receipt. When omitted, a fresh preflight
            runs in the current working directory.
        pinned_content_reader: Injectable reader for blobs at the immutable
            catalog-pinned Athena revision.
        trust_source: Human-readable trust basis stored on the receipt.

    Returns:
        A receipt combining catalog repository/commit identity with file hashes.

    Raises:
        AthenaContractError: If preflight does not prove the catalog-pinned
            Athena checkout or its live content differs from that pinned tree.

    """
    loaded_catalog = load_pi_package_catalog() if catalog is None else catalog
    repository, commit = _athena_package(loaded_catalog)
    ready_preflight = preflight if preflight is not None else preflight_pi_environment(Path.cwd())
    root = _preflight_athena_root(ready_preflight, loaded_catalog)
    if contract_root is not None:
        try:
            supplied_root = Path(contract_root).resolve(strict=True)
        except OSError as exc:
            raise AthenaContractError("Athena contract root is unavailable") from exc
        if supplied_root != root:
            raise AthenaContractError(
                "Athena contract root does not match Pi preflight package root"
            )

    hashes: dict[str, str] = {}
    for field, relative in _CONTRACT_FILES.items():
        live_content = root / relative
        actual_hash = _sha256_file(live_content)
        expected_hash = hashlib.sha256(pinned_content_reader(root, commit, relative)).hexdigest()
        if actual_hash != expected_hash:
            raise AthenaContractError(f"pinned Athena content mismatch: {relative}")
        hashes[field] = expected_hash
    return AthenaContractReceipt(
        athena_repository=repository,
        athena_commit=commit,
        trust_source=trust_source,
        **hashes,
    )


def assert_athena_contract_matches(
    receipt: AthenaContractReceipt,
    expected: Mapping[str, Any],
) -> None:
    """Fail closed when a receipt differs from expected contract metadata."""
    actual = receipt.to_dict()
    mismatches = [
        key for key, expected_value in expected.items() if actual.get(key) != expected_value
    ]
    if mismatches:
        raise AthenaContractError(
            "Athena contract receipt mismatch: " + ", ".join(sorted(mismatches))
        )
