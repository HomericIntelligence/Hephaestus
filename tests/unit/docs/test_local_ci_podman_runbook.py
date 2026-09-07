"""Regression tests for the local CI Podman recovery runbook."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "local-ci-podman.md"
RUNBOOK_INDEX = REPO_ROOT / "docs" / "runbooks" / "index.md"


def _normalized_runbook() -> str:
    """Return the runbook with lowercase, collapsed whitespace."""
    return re.sub(r"\s+", " ", RUNBOOK.read_text(encoding="utf-8").lower())


def test_runbook_exists_and_is_in_the_index() -> None:
    """The runbook exists and the runbook index links to it."""
    assert RUNBOOK.is_file()
    assert "(local-ci-podman.md)" in RUNBOOK_INDEX.read_text(encoding="utf-8")


def test_runbook_limits_destructive_recovery() -> None:
    """The approval and no-rollback rules precede the scoped removal command."""
    text = _normalized_runbook()
    approval = text.index("explicit approval")
    no_rollback = text.index("no rollback")
    removal = text.index("podman machine rm hephaestus-ci")

    assert approval < removal
    assert no_rollback < removal
    assert "podman machine rm podman-machine-default" not in text
    assert "podman machine reset" not in text


def test_runbook_has_bootstrap_and_health_contract() -> None:
    """The recovery procedure selects and verifies the replacement connection."""
    text = _normalized_runbook()

    assert (
        "podman machine init --cpus 4 --disk-size 30 --memory 8192 "
        "--provider applehv --now --update-connection hephaestus-ci"
    ) in text
    assert "podman machine inspect hephaestus-ci" in text
    assert "podman system connection list" in text
    assert "podman info" in text
    assert "active" in text and "hephaestus-ci" in text


def test_runbook_has_complete_local_ci_verification() -> None:
    """The procedure verifies the image and the complete local CI runner."""
    text = _normalized_runbook()

    assert "podman build -f ci/containerfile -t hephaestus-ci:local ." in text
    assert "container_engine=podman bash scripts/run_ci_local.sh all --rebuild" in text
    assert "hephaestus_ci_runner_failure" in text
    assert "native fallback" in text
    assert "until" in text and "health checks pass" in text
