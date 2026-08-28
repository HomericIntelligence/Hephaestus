"""Tests for the managed Pi rollout documentation bundle."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
COMPATIBILITY = REPO_ROOT / "COMPATIBILITY.md"
SECURITY = REPO_ROOT / "SECURITY.md"
RUNBOOK_INDEX = REPO_ROOT / "docs" / "runbooks" / "index.md"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "pi-rollout.md"
RELEASE_NOTE = REPO_ROOT / "docs" / "release-notes" / "pi-automation-rollout.md"
PRIVATE_PROVIDER = REPO_ROOT / "docs" / "pi-private-provider.md"
INSTALLER_ARCHITECTURE = REPO_ROOT / "docs" / "INSTALLER_ARCHITECTURE.md"


def test_rollout_docs_are_discoverable() -> None:
    """The rollout docs must be easy to find from the main doc indexes."""
    assert RUNBOOK.is_file()
    assert RELEASE_NOTE.is_file()
    assert "pi-rollout.md" in RUNBOOK_INDEX.read_text(encoding="utf-8")
    assert "docs/runbooks/pi-rollout.md" in README.read_text(encoding="utf-8")
    release_note_text = RELEASE_NOTE.read_text(encoding="utf-8")
    assert "Managed Pi automation rollout" in release_note_text
    assert "#2513, #2520" in release_note_text


def test_rollout_runbook_covers_enable_omit_and_recovery_paths() -> None:
    """The runbook must cover enable, omit, and recovery flows."""
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "managed Pi provider path" in text
    assert "hephaestus-install-pi-plugins --global --yes --no-approve" in text
    assert "hephaestus-install-pi-plugins --dry-run --json" in text
    assert "--disable-pi-automation" in text
    assert "Host-owned Athena `advise` and `learn` remain available" in text
    assert "If `--agent pi` fails" in text


def test_rollout_docs_keep_the_provider_boundary_explicit() -> None:
    """The docs must keep the Pi boundary separate from host-owned Athena."""
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    security = SECURITY.read_text(encoding="utf-8")
    private_provider = PRIVATE_PROVIDER.read_text(encoding="utf-8")

    assert "## Agent Provider Compatibility" in compatibility
    assert "ubuntu-24.04-arm" in compatibility
    assert "--disable-pi-automation" in compatibility
    assert "### Managed Pi supply chain" in security
    assert "roll back" in security.lower()
    assert "Then install and preflight the managed package set" in private_provider
    assert "--disable-pi-automation" in private_provider


def test_rollout_docs_reference_the_installer_architecture() -> None:
    """The installer architecture note must name the managed package helper."""
    text = INSTALLER_ARCHITECTURE.read_text(encoding="utf-8")

    assert "run_pi_package_manager()" in text
    assert "helper directly" in text
