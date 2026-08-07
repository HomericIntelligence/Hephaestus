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
    assert "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" in source
    assert "snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/" in source
    assert source.count("[check-valid-until=no]") == 2
    assert "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*" in source

    # A single digest-pinned Python root configures APT before every stage that
    # installs OS packages; no stage may fall back to the base image's mutable
    # deb.debian.org sources or upgrade against whatever repository is current.
    assert source.count("FROM python:3.13-slim@sha256:") == 1
    assert source.count("FROM python-snapshot") == 3
    assert "apt-get upgrade" not in source
    assert "deb.debian.org" not in source


def test_node_runtime_source_is_digest_pinned() -> None:
    """Rebuilding the CI image must not silently select a different Node runtime."""
    lines = CONTAINERFILE.read_text(encoding="utf-8").splitlines()
    node_stage = next(line for line in lines if line.startswith("FROM node:"))

    assert (
        node_stage == "FROM node:22-bookworm-slim@sha256:"
        "d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS node"
    )


def test_baked_environment_is_writable_by_runtime_uid() -> None:
    """Docker's host UID must be able to let uv refresh the baked environment."""
    source = CONTAINERFILE.read_text(encoding="utf-8")

    assert "chmod -R a+rwX /opt/hephaestus-venv" in source


def test_runtime_tools_follow_the_requested_build_architecture() -> None:
    """The image must select its executable artifacts for the build platform."""
    source = CONTAINERFILE.read_text(encoding="utf-8")

    assert source.count('case "$TARGETARCH" in') == 2

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

    assert source.count('*) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1') == 2
