"""Regression tests for the dedicated required ShellCheck job."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"


def _shellcheck_run_script() -> str:
    """Return the script run by the required ShellCheck workflow step."""
    workflow = yaml.safe_load(REQUIRED_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["shellcheck"]["steps"]
    matches = [step for step in steps if step.get("name") == "Run shellcheck"]

    assert len(matches) == 1
    return str(matches[0]["run"])


def test_shellcheck_job_recurses_over_shell_helpers() -> None:
    """The job uses globstar to discover nested shell helpers."""
    script = _shellcheck_run_script()

    assert "shopt -s nullglob globstar" in script
    assert "scripts/**/*.sh" in script
    assert "scripts/shell/*.sh" not in script


def test_shellcheck_job_includes_slurm_batch_scripts() -> None:
    """The job explicitly includes Slurm batch scripts."""
    script = _shellcheck_run_script()

    assert "scripts/**/*.sbatch" in script
