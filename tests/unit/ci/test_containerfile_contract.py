"""Contracts for the portable, reproducible local CI image."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTAINERFILE = REPO_ROOT / "ci" / "Containerfile"


def test_debian_packages_use_one_immutable_snapshot() -> None:
    """Every APT-backed stage must resolve packages from fixed repository state."""
    source = CONTAINERFILE.read_text(encoding="utf-8")

    snapshot = re.search(r"^ARG DEBIAN_SNAPSHOT=(\d{8}T\d{6}Z)$", source, re.MULTILINE)
    assert snapshot is not None
    repository_lines = [
        line.strip().strip('" \\')
        for line in source.splitlines()
        if line.strip().startswith('"deb ')
    ]
    assert repository_lines == [
        "deb [check-valid-until=no] "
        + "http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ "
        + "${VERSION_CODENAME} main",
        "".join(
            (
                "deb [check-valid-until=no] ",
                "http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/ ",
                "${VERSION_CODENAME}-security main",
            )
        ),
    ]
    assert source.count("[check-valid-until=no]") == 2
    assert "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*" in source
    assert "deb.debian.org" not in source

    # A single digest-pinned Python root configures APT before every stage that
    # installs OS packages; no stage may fall back to the base image's mutable
    # repositories or upgrade against whatever repository is current.
    assert source.count("FROM python:3.13-slim@sha256:") == 1
    assert source.count("FROM python-snapshot") == 3
    apt_stages = [stage for stage in re.split(r"(?m)(?=^FROM )", source) if "apt-get" in stage]
    assert len(apt_stages) == 3
    assert all(stage.startswith("FROM python-snapshot") for stage in apt_stages)
    assert re.search(r"\bapt-get\b[^&;\n]*\b(?:dist-)?upgrade\b", source) is None


def test_node_runtime_source_is_digest_pinned() -> None:
    """Rebuilding the CI image must not silently select a different Node runtime."""
    lines = CONTAINERFILE.read_text(encoding="utf-8").splitlines()
    node_stage = next(line for line in lines if line.startswith("FROM node:"))

    assert (
        node_stage == "FROM node:22-bookworm-slim@sha256:"
        "d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS node"
    )


def test_baked_environment_is_not_made_world_writable() -> None:
    """Docker must not require weakening the baked environment's permissions."""
    source = CONTAINERFILE.read_text(encoding="utf-8")

    assert "chmod -R a+rwX /opt/hephaestus-venv" not in source


def test_runtime_tools_follow_the_requested_build_architecture() -> None:
    """The image must select its executable artifacts for the build platform."""
    source = CONTAINERFILE.read_text(encoding="utf-8")

    assert source.count('case "$TARGETARCH" in') == 3

    assert (
        'amd64) uv_asset="uv-x86_64-unknown-linux-gnu"; '
        'uv_sha256="90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb"' in source
    )
    assert (
        'arm64) uv_asset="uv-aarch64-unknown-linux-gnu"; '
        'uv_sha256="769d373e146692c639b5fbaae33b331c297a32e03d30448772051902df52bbf4"' in source
    )
    assert "${uv_asset}.tar.gz" in source
    assert 'echo "${uv_sha256}  /tmp/uv.tar.gz" | sha256sum --check' in source

    assert (
        'amd64) gh_arch="amd64"; '
        'gh_sha256="7c7fa3bb890db0934baf65910d97b8c0fa437b2e590f7f7daf6bdf82c5c486d7"' in source
    )
    assert (
        'arm64) gh_arch="arm64"; '
        'gh_sha256="0ba7a76739c865d82ebde24667d875d9b8caa55db47c7597c24accdd4defd2bb"' in source
    )
    assert "linux_${gh_arch}.deb" in source
    assert 'echo "${gh_sha256}  /tmp/gh.deb" | sha256sum --check' in source

    assert (
        'amd64) just_arch="x86_64"; '
        'just_sha256="bc7c9f377944f8de9cd0418b11d2955adebfa25a488c0b5e3dd2d2c0e9d732da"' in source
    )
    assert (
        'arm64) just_arch="aarch64"; '
        'just_sha256="bb3886b15e2cbcb9c0eb19956297d36de4eaef45b89d3f5fa5d1fc4ed3b5b51d"' in source
    )
    assert "just-1.36.0-${just_arch}-unknown-linux-musl.tar.gz" in source
    assert 'echo "${just_sha256}  /tmp/just.tar.gz" | sha256sum --check' in source

    assert source.count('*) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1') == 3


def test_github_artifact_downloads_retry_transient_network_failures() -> None:
    """Pinned GitHub downloads must survive transient runner network resets."""
    source = CONTAINERFILE.read_text(encoding="utf-8")
    download_lines = [
        line.strip()
        for line in source.splitlines()
        if "curl -fsSL" in line and '"https://github.com/' in line
    ]

    assert len(download_lines) == 3
    for line in download_lines:
        assert "--retry 5" in line
        assert "--retry-all-errors" in line
        assert "--connect-timeout 30" in line
