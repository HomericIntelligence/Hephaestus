#!/usr/bin/env python3
"""Integration tests for reproducible and complete wheel artifacts."""

from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import stat
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from .artifact_support import ControlledArtifacts, file_sha256, source_package_files

REPO_ROOT = Path(__file__).resolve().parents[2]


def _validate_member_name(name: str) -> None:
    """Reject absolute and traversal paths in a wheel."""
    path = PurePosixPath(name)
    assert name and not path.is_absolute(), f"unsafe absolute wheel member: {name!r}"
    assert ".." not in path.parts, f"unsafe traversal wheel member: {name!r}"
    assert "\\" not in name, f"unsafe wheel separator in member: {name!r}"


def _safe_wheel_members(wheel: Path) -> list[str]:
    """Validate wheel member names/types and return them in archive order."""
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        assert len(names) == len(set(names)), "wheel contains duplicate members"
        for info in infos:
            _validate_member_name(info.filename)
            assert not info.is_dir(), f"wheel directory member is forbidden: {info.filename!r}"
            mode = (info.external_attr >> 16) & 0o170000
            assert mode in (0, stat.S_IFREG), (
                f"wheel link/member type is forbidden: {info.filename!r}"
            )
        return names


def _dist_info_prefix(members: list[str]) -> str:
    """Return the only dist-info directory prefix in a wheel."""
    prefixes = {
        member.split("/", 1)[0]
        for member in members
        if member.split("/", 1)[0].endswith(".dist-info")
    }
    assert len(prefixes) == 1, f"expected one dist-info dir, got {prefixes}"
    return prefixes.pop()


@pytest.mark.integration
@pytest.mark.artifact
def test_wheel_build_is_reproducible(controlled_artifacts: ControlledArtifacts) -> None:
    """Two controlled current-version wheels must be byte-identical."""
    assert controlled_artifacts.first_wheel.name == controlled_artifacts.second_wheel.name
    assert file_sha256(controlled_artifacts.first_wheel) == file_sha256(
        controlled_artifacts.second_wheel
    )


@pytest.mark.integration
@pytest.mark.artifact
def test_wheel_complete_manifest_matches_source(controlled_artifacts: ControlledArtifacts) -> None:
    """Every wheel member must match the package and metadata inventory."""
    members = _safe_wheel_members(controlled_artifacts.first_wheel)
    dist_info = _dist_info_prefix(members)
    expected = source_package_files() | {
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/licenses/NOTICE",
        f"{dist_info}/RECORD",
    }
    assert set(members) == expected


@pytest.mark.integration
@pytest.mark.artifact
def test_wheel_record_covers_and_hashes_every_member(
    controlled_artifacts: ControlledArtifacts,
) -> None:
    """Every wheel member must have a valid size and SHA-256 RECORD row."""
    members = _safe_wheel_members(controlled_artifacts.first_wheel)
    with zipfile.ZipFile(controlled_artifacts.first_wheel) as wheel:
        dist_info = _dist_info_prefix(members)
        record_name = f"{dist_info}/RECORD"
        with wheel.open(record_name) as record_file:
            rows = list(csv.reader(io.TextIOWrapper(record_file, encoding="utf-8")))

        assert all(len(row) == 3 for row in rows)
        assert len({row[0] for row in rows}) == len(rows)
        assert {row[0] for row in rows} == set(members)

        for name, encoded_hash, encoded_size in rows:
            if name == record_name:
                assert encoded_hash == encoded_size == ""
                continue
            algorithm, digest = encoded_hash.split("=", 1)
            assert algorithm == "sha256"
            padding = "=" * (-len(digest) % 4)
            payload = wheel.read(name)
            assert base64.urlsafe_b64decode(digest + padding) == hashlib.sha256(payload).digest()
            assert int(encoded_size) == len(payload)


@pytest.mark.integration
@pytest.mark.artifact
def test_wheel_console_scripts_match_pyproject(controlled_artifacts: ControlledArtifacts) -> None:
    """Wheel console_scripts metadata must mirror [project.scripts] exactly."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = pyproject["project"]["scripts"]

    members = _safe_wheel_members(controlled_artifacts.first_wheel)
    dist_info = _dist_info_prefix(members)
    with zipfile.ZipFile(controlled_artifacts.first_wheel) as wheel:
        raw = wheel.read(f"{dist_info}/entry_points.txt").decode("utf-8")
    parser = configparser.ConfigParser()
    parser.read_string(raw)
    actual = dict(parser["console_scripts"])

    assert actual == expected
