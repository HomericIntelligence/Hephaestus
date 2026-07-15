"""Keep Pixi platform documentation aligned with the workspace manifest."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[3]
PIXI = REPO_ROOT / "pixi.toml"
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

EXPECTED_PIXI_PLATFORMS = {"linux-64", "osx-arm64"}
STALE_PLATFORM_CLAIMS = (
    "linux-64 only",
    "`linux-64` only",
    'platforms = ["linux-64"]',
    "only supported on linux",
    "on macos or windows, install the published wheel",
    "supported install path for non-linux platforms",
    "pixi tooling is not available there",
)


def _pixi_platforms() -> set[str]:
    workspace = tomllib.loads(PIXI.read_text(encoding="utf-8"))["workspace"]
    raw_platforms = workspace["platforms"]
    assert isinstance(raw_platforms, list)
    assert all(isinstance(platform, str) for platform in raw_platforms)
    return set(cast(list[str], raw_platforms))


def _assert_declared_platforms_are_documented(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    missing = {platform for platform in _pixi_platforms() if f"`{platform}`" not in text}
    assert not missing, f"{path.name} omits Pixi platforms: {sorted(missing)}"


def test_pixi_manifest_declares_documented_platform_matrix() -> None:
    """The workspace platforms remain the approved Pixi target matrix."""
    assert _pixi_platforms() == EXPECTED_PIXI_PLATFORMS


def test_readme_platform_guidance_matches_manifest() -> None:
    """README names every platform declared by the Pixi workspace."""
    _assert_declared_platforms_are_documented(README)


def test_contributing_platform_guidance_matches_manifest() -> None:
    """CONTRIBUTING names every platform declared by the Pixi workspace."""
    _assert_declared_platforms_are_documented(CONTRIBUTING)


def test_docs_remove_linux_only_claims_and_route_workspace_gaps_to_pip() -> None:
    """Docs reserve the editable pip fallback for excluded Pixi targets."""
    for path in (README, CONTRIBUTING):
        text = path.read_text(encoding="utf-8").lower()
        stale = {claim for claim in STALE_PLATFORM_CLAIMS if claim in text}
        assert not stale, f"{path.name} retains stale claims: {sorted(stale)}"
        assert "windows or intel macos" in text

    contributing = CONTRIBUTING.read_text(encoding="utf-8")
    assert "pip install -e '.[dev]'" in contributing
