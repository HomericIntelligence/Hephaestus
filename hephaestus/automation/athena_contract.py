"""Pinned Athena contract receipts for Mnemosyne-backed skills.

The Pi package catalog is the source of truth for which Athena repository and
commit Hephaestus admits.  This module binds that catalog entry to the exact
skill contracts that define Mnemosyne dependency resolution, advice retrieval,
and PR-backed learning.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hephaestus.agents.pi_plugins import PiPackageCatalog, load_pi_package_catalog


class AthenaContractError(RuntimeError):
    """Raised when the pinned Athena contract cannot be proven."""


_ATHENA_PACKAGE_KEY = "athena"
_CONTRACT_FILES = {
    "advise_sha256": Path("skills/advise/SKILL.md"),
    "learn_sha256": Path("skills/learn/SKILL.md"),
    "dependency_resolution_sha256": Path("docs/dependency-resolution.md"),
}
_CONTRACT_ROOT_ENV = "HEPH_ATHENA_CONTRACT_ROOT"


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


def default_athena_contract_root() -> Path:
    """Locate the installed Athena package contract root."""
    if override := os.environ.get(_CONTRACT_ROOT_ENV):
        override_path = Path(override).expanduser()
        return override_path
    cache_root = Path.home() / ".codex" / "plugins" / "cache" / "athena" / "athena"
    candidates: list[Path] = [cache_root]
    if cache_root.exists():
        candidates.extend(sorted(cache_root.glob("*"), reverse=True))
    for candidate in candidates:
        if all((candidate / relative).is_file() for relative in _CONTRACT_FILES.values()):
            return candidate
    raise AthenaContractError(
        f"cannot locate Athena contract root; set {_CONTRACT_ROOT_ENV} to the pinned package"
    )


def load_athena_contract_receipt(
    *,
    contract_root: Path | None = None,
    catalog: PiPackageCatalog | None = None,
    trust_source: str = "pi-package-catalog",
) -> AthenaContractReceipt:
    """Load a checksum-bound receipt for the pinned Athena contract.

    Args:
        contract_root: Root containing the Athena package contract files. When
            omitted, the installed Athena plugin cache is resolved.
        catalog: Optional already-loaded Pi package catalog.
        trust_source: Human-readable trust basis stored on the receipt.

    Returns:
        A receipt combining catalog repository/commit identity with file hashes.

    Raises:
        AthenaContractError: If the catalog lacks Athena or any required file
            cannot be read.

    """
    loaded_catalog = load_pi_package_catalog() if catalog is None else catalog
    repository, commit = _athena_package(loaded_catalog)
    root = default_athena_contract_root() if contract_root is None else Path(contract_root)
    hashes = {field: _sha256_file(root / relative) for field, relative in _CONTRACT_FILES.items()}
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
