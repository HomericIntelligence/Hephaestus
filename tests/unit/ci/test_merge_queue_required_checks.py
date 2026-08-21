"""Regression contracts for required checks on merge-queue commits."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load workflow YAML without YAML 1.1 coercing the ``on`` key to true."""
    return cast(
        dict[str, Any],
        yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader),
    )


def test_every_required_context_workflow_runs_for_merge_groups() -> None:
    """Synthetic queue commits must emit the same required contexts as PR heads."""
    for path in (REQUIRED_WORKFLOW, TEST_WORKFLOW):
        workflow = _load_workflow(path)

        assert workflow["on"]["merge_group"]["types"] == ["checks_requested"], path.name
