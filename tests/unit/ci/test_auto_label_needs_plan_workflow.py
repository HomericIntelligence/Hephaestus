"""Regression contracts for the dual-trigger auto-label workflow."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-label-needs-plan.yml"
DOCUMENTATION = REPO_ROOT / "docs" / "auto-label-needs-plan.md"
ISSUE_NUMBER_EXPRESSION = "${{ github.event.issue.number || inputs.issue_number }}"


def _load_yaml_text(text: str) -> dict[str, Any]:
    """Load a YAML mapping without YAML 1.1 scalar coercion."""
    document = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a repository YAML file using the GitHub-compatible ``on`` key."""
    return _load_yaml_text(path.read_text(encoding="utf-8"))


def _load_documented_caller() -> dict[str, Any]:
    """Extract and load the documented reusable-workflow caller stub."""
    document = DOCUMENTATION.read_text(encoding="utf-8")
    match = re.search(r"```yaml\n(?P<caller>.*?)\n```", document, re.DOTALL)
    assert match is not None, "documentation must contain a YAML caller example"
    return _load_yaml_text(match.group("caller"))


def _label_step() -> dict[str, Any]:
    """Return the workflow's label-mutating step."""
    workflow = _load_yaml(WORKFLOW)
    steps = workflow["jobs"]["needs-plan"]["steps"]
    step = next(item for item in steps if item.get("name") == "Add state:needs-plan label")
    assert isinstance(step, dict)
    return cast(dict[str, Any], step)


def _write_fake_gh(directory: Path) -> Path:
    """Create a fake ``gh`` executable that records every invocation argument."""
    executable = directory / "gh"
    executable.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nprintf \'<%s>\\n\' "$@" > "$GH_CALL_LOG"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _run_label_step(
    tmp_path: Path, shell: str, issue_number: str
) -> subprocess.CompletedProcess[str]:
    """Run the workflow shell with a controlled ``gh`` boundary."""
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_fake_gh(tools)
    call_log = tmp_path / "gh-call.log"
    environment = os.environ | {
        "GH_TOKEN": "test-token",
        "ISSUE_NUMBER": issue_number,
        "REPO": "HomericIntelligence/example",
        "GH_CALL_LOG": str(call_log),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", "-c", shell],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_workflow_call_input_is_required_number() -> None:
    """Reusable callers must provide a required numeric issue number."""
    workflow = _load_yaml(WORKFLOW)
    issue_input = workflow["on"]["workflow_call"]["inputs"]["issue_number"]

    assert issue_input["required"] == "true"
    assert issue_input["type"] == "number"


def test_issues_and_workflow_call_share_one_validated_label_job() -> None:
    """Both triggers must reach one job with identical issue resolution."""
    workflow = _load_yaml(WORKFLOW)
    job = workflow["jobs"]["needs-plan"]
    step = _label_step()

    assert workflow["on"]["issues"]["types"] == ["opened", "reopened"]
    assert set(workflow["jobs"]) == {"needs-plan"}
    assert "if" not in job
    assert workflow["concurrency"]["group"] == (f"auto-label-needs-plan-{ISSUE_NUMBER_EXPRESSION}")
    assert step["env"]["ISSUE_NUMBER"] == ISSUE_NUMBER_EXPRESSION


def test_documented_caller_passes_issue_number() -> None:
    """The documented caller binds the required input to its server event."""
    caller = _load_documented_caller()

    assert caller["jobs"]["call"]["with"]["issue_number"] == ("${{ github.event.issue.number }}")


def test_rollout_and_rollback_are_documented() -> None:
    """The breaking reusable-input rollout has an explicit containment path."""
    documentation = DOCUMENTATION.read_text(encoding="utf-8")

    assert "## Rollout and rollback" in documentation
    assert "coordinated rollout" in documentation
    assert "revert the reusable workflow" in documentation
    assert "already-applied labels" in documentation


@pytest.mark.parametrize(
    ("path", "issue_number"),
    [
        ("native issues event", "42"),
        ("reusable workflow input", "43"),
    ],
)
def test_label_script_accepts_both_resolved_input_paths(
    tmp_path: Path, path: str, issue_number: str
) -> None:
    """A valid issue number from either trigger reaches the labels endpoint."""
    del path
    result = _run_label_step(tmp_path, str(_label_step()["run"]), issue_number)

    assert result.returncode == 0, result.stderr
    call = (tmp_path / "gh-call.log").read_text(encoding="utf-8").splitlines()
    assert call == [
        "<api>",
        "<--method>",
        "<POST>",
        f"<repos/HomericIntelligence/example/issues/{issue_number}/labels>",
        "<-f>",
        "<labels[]=state:needs-plan>",
    ]


@pytest.mark.parametrize("issue_number", ["", "0", "00", "01", "-1", "1.5", "abc"])
def test_label_script_rejects_invalid_issue_numbers(tmp_path: Path, issue_number: str) -> None:
    """Invalid or missing inputs fail before the GitHub API boundary."""
    result = _run_label_step(tmp_path, str(_label_step()["run"]), issue_number)

    assert result.returncode != 0
    assert not (tmp_path / "gh-call.log").exists()
