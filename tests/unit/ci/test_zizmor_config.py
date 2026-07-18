"""Tests for the UV-managed zizmor GitHub Actions SAST configuration.

zizmor is the workflow-surface complement to bandit (Python) and ShellCheck
(shell); see issue #2151 and SECURITY.md. These guards freeze the two
enforcement surfaces (pre-commit + required CI job) and the offline/online flag
split so the scanner cannot silently stop gating or drift out of alignment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef, unused-ignore]


REPO_ROOT = Path(__file__).resolve().parents[3]

# Offline PR-gate flags. The required CI job and the pre-commit hook MUST both
# carry every one of these so a workflow security regression fails fast and
# deterministically, with no network dependency.
OFFLINE_FLAGS = ("--no-online-audits", "--min-severity", "medium")


def _pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _zizmor_precommit_entry() -> str:
    """Return the ``entry`` command of the local zizmor pre-commit hook."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "zizmor"
    )
    return str(hook["entry"])


def test_zizmor_is_a_versioned_dev_dependency() -> None:
    """The project-managed development environment supplies zizmor."""
    config = _pyproject()
    dev_group = config["dependency-groups"]["dev"]  # type: ignore[index]
    assert any(dependency.startswith("zizmor>=") for dependency in dev_group)


def test_required_workflow_runs_zizmor_offline_at_medium() -> None:
    """The required PR gate runs the project-managed zizmor, offline, medium+."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/_required.yml").read_text())
    job = workflow["jobs"]["security-workflow-scan"]
    assert job["name"] == "security/workflow-scan"
    run_step = next(step for step in job["steps"] if step.get("id") == "zizmor")
    command = run_step["run"]
    assert "uv run zizmor" in command
    for flag in OFFLINE_FLAGS:
        assert flag in command, f"required workflow-scan job missing {flag!r}"
    assert ".github/workflows/" in command


def test_workflow_scan_job_is_wired_into_the_required_gate() -> None:
    """security-workflow-scan must be aggregated by required-checks-gate.

    Without this the offline zizmor gate would run but never block a merge.
    The generic gate-membership guard (test_required_checks_gate.py) also
    covers this dynamically; asserting it here documents the contract for
    this specific scan.
    """
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/_required.yml").read_text())
    gate_needs = workflow["jobs"]["required-checks-gate"]["needs"]
    assert "security-workflow-scan" in gate_needs


def test_precommit_and_ci_zizmor_flags_match() -> None:
    """The pre-commit hook and the CI gate use the identical offline flags.

    A drift between the two would let a commit pass locally but fail in CI (or
    vice versa); freeze them together.
    """
    entry = _zizmor_precommit_entry()
    assert entry.startswith("uv run zizmor")
    for flag in OFFLINE_FLAGS:
        assert flag in entry, f"zizmor pre-commit hook missing {flag!r}"
    assert ".github/workflows/" in entry


def test_scheduled_scan_uses_online_audits() -> None:
    """The weekly security.yml scan enables the online, API-backed audits.

    It must NOT pass --no-online-audits and must supply a GH_TOKEN so audits
    such as known-vulnerable-actions can query the GitHub API.
    """
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/security.yml").read_text())
    job = workflow["jobs"]["workflow-scan"]
    run_step = next(step for step in job["steps"] if step.get("id") == "zizmor")
    command = run_step["run"]
    assert "uv run zizmor" in command
    assert "--no-online-audits" not in command
    assert "--min-severity" in command and "medium" in command
    # Not a secret: this is the GitHub Actions expression the workflow uses to
    # pass the ephemeral job token to zizmor's online audits.
    assert run_step["env"]["GH_TOKEN"] == "${{ github.token }}"  # noqa: S105


def test_security_md_documents_static_analysis_coverage() -> None:
    """SECURITY.md documents the per-surface static-analysis coverage.

    Issue #2151 requires a documented equivalent for the workflow and shell
    surfaces; the coverage table names zizmor and ShellCheck alongside bandit.
    """
    security_md = (REPO_ROOT / "SECURITY.md").read_text()
    assert "Static Analysis Coverage" in security_md
    for tool in ("zizmor", "Bandit", "ShellCheck"):
        assert tool in security_md, f"SECURITY.md coverage table missing {tool}"
