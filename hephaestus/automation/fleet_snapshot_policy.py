"""Closed manifest and operator policy for the first Fleet snapshot profile."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

MANIFEST_SCHEMA = "hi/hephaestus/source-snapshot/v1"
POLICY_SCHEMA = "hi/hephaestus/source-snapshot-policy/v1"
MAX_MEMBERS = 10_000
MAX_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PATH_BYTES = 240
MAX_SCAN_ENTRIES = 40_000
EXCLUDED_NAMES = (
    ".aws",
    ".azure",
    ".claude.json",
    ".codex",
    ".config",
    ".credentials.json",
    ".fleet-runtime",
    ".git",
    ".git-credentials",
    ".gnupg",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".ssh",
    "auth.json",
    "credentials.json",
    "fleet-runtime",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
)


class SnapshotError(RuntimeError):
    """The source or artifact cannot satisfy the snapshot contract."""


def canonical(value: object) -> bytes:
    """Return the sole version-one JSON byte representation."""
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def digest(value: bytes) -> str:
    """Return the full content digest."""
    return hashlib.sha256(value).hexdigest()


def integer(value: object, minimum: int, maximum: int) -> bool:
    """Reject boolean and floating-point representations of counters."""
    return type(value) is int and minimum <= value <= maximum


def sha(value: object, length: int = 64) -> bool:
    """Return whether an identity is a lowercase hexadecimal digest."""
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def valid_path(value: object) -> bool:
    """Accept only portable, canonical regular-member paths."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PATH_BYTES
        and all(32 <= ord(char) <= 126 and char not in "\\:" for char in value)
        and all(
            part not in {"", ".", ".."} and not part.endswith((" ", "."))
            for part in value.split("/")
        )
    )


@dataclass(frozen=True)
class SnapshotPolicy:
    """Operator-owned bounds and private relative paths; not requester policy."""

    max_members: int
    max_bytes: int
    private_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject policy values outside the fixed implementation bounds."""
        if (
            not integer(self.max_members, 1, MAX_MEMBERS)
            or not integer(self.max_bytes, 1, MAX_BYTES)
            or type(self.private_paths) is not tuple
            or len(self.private_paths) > 100
            or any(not valid_path(path) for path in self.private_paths)
            or len(set(self.private_paths)) != len(self.private_paths)
        ):
            raise SnapshotError("invalid snapshot policy")

    def document(self) -> dict[str, Any]:
        """Return the complete immutable policy's canonical data."""
        return {
            "schema": POLICY_SCHEMA,
            "selection": "git-current-tracked-and-unignored-v1",
            "paths": "portable-ascii-v1",
            "fileModes": "posix-permissions-no-special-bits",
            "directories": "implicit-0700",
            "outputParent": "current-user-0700-exclusive-supervisor-lease",
            "symlinks": "reject",
            "submodules": "reject",
            "hardlinks": "reject",
            "excludedNames": sorted(EXCLUDED_NAMES),
            "excludedPatterns": [".env", ".env.*", "*.key", "*.pem"],
            "privatePaths": sorted(self.private_paths),
            "maxMembers": self.max_members,
            "maxBytes": self.max_bytes,
            "maxPathBytes": MAX_PATH_BYTES,
            "maxScanEntries": MAX_SCAN_ENTRIES,
            "maxManifestBytes": MAX_MANIFEST_BYTES,
            "maxGitOutputBytes": MAX_MANIFEST_BYTES,
        }

    @property
    def digest(self) -> str:
        """Bind every selection, exclusion and limit field."""
        return digest(canonical(self.document()))

    def excludes(self, path: str) -> bool:
        """Exclude private path components before reading their content."""
        parts = path.lower().split("/")
        return any(
            part in EXCLUDED_NAMES
            or part == ".env"
            or part.startswith(".env.")
            or part.endswith((".key", ".pem"))
            for part in parts
        ) or any(
            path == private or path.startswith(private + "/") for private in self.private_paths
        )


def validate_manifest(value: object, policy: SnapshotPolicy) -> dict[str, Any]:
    """Validate complete metadata without claiming actual content verification."""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "baseCommit", "policyDigest", "files"}
        or value["schema"] != MANIFEST_SCHEMA
        or not sha(value["baseCommit"], 40)
        or value["policyDigest"] != policy.digest
        or not isinstance(value["files"], list)
        or not 1 <= len(value["files"]) <= policy.max_members
    ):
        raise SnapshotError("invalid snapshot manifest")
    previous = ""
    names: set[str] = set()
    total = 0
    for entry in value["files"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "mode", "size", "sha256"}
            or not valid_path(entry["path"])
            or policy.excludes(entry["path"])
            or entry["path"] <= previous
            or entry["path"].lower() in names
            or any(part.lower() in names for part in _parents(entry["path"]))
            or not integer(entry["mode"], 0, 0o777)
            or not integer(entry["size"], 0, policy.max_bytes)
            or not sha(entry["sha256"])
        ):
            raise SnapshotError("invalid snapshot member")
        previous = entry["path"]
        names.add(previous.lower())
        total += entry["size"]
    if not 1 <= total <= policy.max_bytes:
        raise SnapshotError("snapshot content limit exceeded")
    return value


def _parents(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]
