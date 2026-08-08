#!/usr/bin/env python3
"""Integration tests for reproducible and complete source distributions."""

from __future__ import annotations

import tarfile
import tomllib
from pathlib import Path, PurePosixPath

import pytest

from .artifact_support import ControlledArtifacts, file_sha256, source_package_files

REPO_ROOT = Path(__file__).resolve().parents[2]

SDIST_TOP_LEVEL_FILES = {
    "README.md",
    "LICENSE",
    "NOTICE",
    "COMPATIBILITY.md",
    "pyproject.toml",
    # Hatchling force-includes the VCS ignore file in sdists so consumers can
    # reproduce the source-tree exclusion rules.
    ".gitignore",
}


def _dependency_name(requirement: str) -> str:
    """Return the normalized package name from a simple requirement string."""
    head = requirement.split(";", 1)[0].strip()
    for separator in ("<=", ">=", "==", "!=", "~=", "<", ">", "="):
        if separator in head:
            head = head.split(separator, 1)[0]
            break
    return head.strip().lower().replace("_", "-")


def _validate_member_name(name: str) -> None:
    """Reject absolute and traversal paths in an archive."""
    path = PurePosixPath(name)
    assert name and not path.is_absolute(), f"unsafe absolute archive member: {name!r}"
    assert ".." not in path.parts, f"unsafe traversal archive member: {name!r}"
    assert "\\" not in name, f"unsafe archive separator in member: {name!r}"


def _safe_regular_file_members(archive: Path) -> set[str]:
    """Validate sdist structure and return every regular file without its root."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        assert members, "sdist is empty"
        roots: set[str] = set()
        seen: set[str] = set()
        regular_files: set[str] = set()

        for member in members:
            _validate_member_name(member.name)
            parts = PurePosixPath(member.name).parts
            assert parts, f"empty archive member: {member.name!r}"
            roots.add(parts[0])
            assert member.isreg() or member.isdir(), (
                f"unsupported sdist member type: {member.name!r}"
            )
            assert not member.issym() and not member.islnk(), (
                f"linked sdist member is forbidden: {member.name!r}"
            )

            relative = "/".join(parts[1:])
            if not relative:
                assert member.isdir(), f"sdist root must be a directory: {member.name!r}"
                continue
            assert relative not in seen, f"duplicate sdist member: {relative!r}"
            seen.add(relative)
            if member.isreg():
                regular_files.add(relative)

        assert len(roots) == 1, f"sdist members have multiple roots: {roots}"
        return regular_files


@pytest.mark.integration
@pytest.mark.artifact
def test_dev_group_includes_build_backend_for_no_isolation_sdist() -> None:
    """The UV dev group supplies backend dependencies for controlled builds."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    build_requires = {
        _dependency_name(requirement) for requirement in pyproject["build-system"]["requires"]
    }
    dev_dependencies = {
        _dependency_name(requirement) for requirement in pyproject["dependency-groups"]["dev"]
    }

    assert build_requires <= dev_dependencies


@pytest.mark.integration
@pytest.mark.artifact
def test_sdist_build_is_reproducible(controlled_artifacts: ControlledArtifacts) -> None:
    """Two controlled current-version sdists must be byte-identical."""
    assert controlled_artifacts.first_sdist.name == controlled_artifacts.second_sdist.name
    assert file_sha256(controlled_artifacts.first_sdist) == file_sha256(
        controlled_artifacts.second_sdist
    )


@pytest.mark.integration
@pytest.mark.artifact
def test_sdist_complete_manifest_matches_source(controlled_artifacts: ControlledArtifacts) -> None:
    """Every regular sdist member must match the configured source inventory."""
    members = _safe_regular_file_members(controlled_artifacts.first_sdist)
    expected = source_package_files() | SDIST_TOP_LEVEL_FILES | {"PKG-INFO"}
    assert members == expected
