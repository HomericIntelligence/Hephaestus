"""Provider-neutral receipts for the pinned Athena skill contract.

The Athena contract is host-owned. Loading it does not start or validate an
agent harness. Provider checks belong only to jobs that execute through that
provider.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


class AthenaContractError(RuntimeError):
    """Raised when the pinned Athena contract cannot be proven."""


_CONTRACT_MANIFEST = Path(__file__).with_name("athena_contract_manifest.json")
_CONTRACT_FILES = {
    "advise_sha256": Path("skills/advise/SKILL.md"),
    "learn_sha256": Path("skills/learn/SKILL.md"),
    "dependency_resolution_sha256": Path("docs/dependency-resolution.md"),
}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


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


def _sha256_file(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise AthenaContractError(f"missing Athena contract file: {path}") from exc
    return hashlib.sha256(content).hexdigest()


def _load_manifest() -> AthenaContractReceipt:
    """Load and validate the packaged provider-neutral contract manifest."""
    try:
        raw = json.loads(_CONTRACT_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AthenaContractError("Athena contract manifest is unavailable or invalid") from exc
    if not isinstance(raw, dict):
        raise AthenaContractError("Athena contract manifest must be an object")
    try:
        receipt = AthenaContractReceipt(
            athena_repository=str(raw["athena_repository"]),
            athena_commit=str(raw["athena_commit"]),
            advise_sha256=str(raw["advise_sha256"]),
            learn_sha256=str(raw["learn_sha256"]),
            dependency_resolution_sha256=str(raw["dependency_resolution_sha256"]),
            trust_source=str(raw["trust_source"]),
        )
    except KeyError as exc:
        raise AthenaContractError(f"Athena contract manifest lacks {exc.args[0]!r}") from exc
    if not receipt.athena_repository or not receipt.trust_source:
        raise AthenaContractError("Athena contract manifest identity is empty")
    if _COMMIT_PATTERN.fullmatch(receipt.athena_commit) is None:
        raise AthenaContractError("Athena contract manifest commit is invalid")
    for field in _CONTRACT_FILES:
        if _SHA256_PATTERN.fullmatch(getattr(receipt, field)) is None:
            raise AthenaContractError(f"Athena contract manifest {field} is invalid")
    return receipt


def load_athena_contract_receipt(
    *,
    contract_root: Path | None = None,
    trust_source: str | None = None,
) -> AthenaContractReceipt:
    """Load the packaged Athena contract without an agent harness.

    Args:
        contract_root: Optional Athena checkout to verify against the packaged
            hashes. Normal host execution does not need an Athena checkout.
        trust_source: Optional receipt label for an explicitly verified root.

    Returns:
        The provider-neutral packaged contract receipt.

    Raises:
        AthenaContractError: If the manifest is invalid or a supplied checkout
            does not contain the pinned contract content.

    """
    receipt = _load_manifest()
    if contract_root is not None:
        try:
            root = Path(contract_root).resolve(strict=True)
        except OSError as exc:
            raise AthenaContractError("Athena contract root is unavailable") from exc
        for field, relative in _CONTRACT_FILES.items():
            if _sha256_file(root / relative) != getattr(receipt, field):
                raise AthenaContractError(f"pinned Athena content mismatch: {relative}")
    if trust_source is not None:
        if not trust_source.strip():
            raise AthenaContractError("Athena contract trust source is empty")
        receipt = replace(receipt, trust_source=trust_source)
    return receipt


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
