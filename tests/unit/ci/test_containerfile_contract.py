"""Contracts for the portable, reproducible local CI image."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTAINERFILE = REPO_ROOT / "ci" / "Containerfile"


def test_node_runtime_source_is_digest_pinned() -> None:
    """Rebuilding the CI image must not silently select a different Node runtime."""
    lines = CONTAINERFILE.read_text(encoding="utf-8").splitlines()
    node_stage = next(line for line in lines if line.startswith("FROM node:"))

    assert "@sha256:" in node_stage


def test_runtime_tools_follow_the_requested_build_architecture() -> None:
    """The image must select its executable artifacts for the build platform."""
    source = CONTAINERFILE.read_text(encoding="utf-8")

    assert "ARG TARGETARCH" in source
    assert 'case "$TARGETARCH"' in source
