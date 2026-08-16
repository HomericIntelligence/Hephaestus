"""Tests for the UV-managed zizmor GitHub Actions SAST configuration.

zizmor is the workflow-surface complement to bandit (Python) and ShellCheck
(shell); see issue #2151 and SECURITY.md. These guards freeze the two
enforcement surfaces (pre-commit + required CI job) and the offline/online flag
split so the scanner cannot silently stop gating or drift out of alignment.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tomllib  # type: ignore[no-redef, unused-ignore]
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests/unit/ci/fixtures/zizmor"

SCAN_ROOTS = (".github/workflows/", ".github/actions/")
EXPECTED_OFFLINE_ARGV = (
    "uv",
    "run",
    "zizmor",
    "--no-online-audits",
    "--min-severity",
    "medium",
    *SCAN_ROOTS,
)
EXPECTED_ONLINE_ARGV = (
    "uv",
    "run",
    "zizmor",
    "--min-severity",
    "medium",
    *SCAN_ROOTS,
)
NON_PRODUCTION_ACTION_FIXTURES = {
    "tests/unit/ci/fixtures/zizmor/unpinned_action/action.yml",
}

# Offline PR-gate flags. The required CI job and the pre-commit hook MUST both
# carry every one of these so a workflow security regression fails fast and
# deterministically, with no network dependency.
OFFLINE_FLAGS = ("--no-online-audits", "--min-severity", "medium")


def _pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _zizmor_precommit_hook() -> dict[str, object]:
    """Return the local zizmor pre-commit hook configuration."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    return next(
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "zizmor"
    )


def _workflow_zizmor_run(path: Path, job: str) -> str:
    """Return the zizmor run command from one workflow job."""
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    step = next(step for step in config["jobs"][job]["steps"] if step.get("id") == "zizmor")
    return str(step["run"])


def _scanner_argv(command: str) -> tuple[str, ...]:
    """Return the zizmor argv from a direct or containerized shell command."""
    tokens = shlex.split(command)
    marker = ("uv", "run", "zizmor")
    for index in range(len(tokens) - len(marker) + 1):
        if tuple(tokens[index : index + len(marker)]) == marker:
            return tuple(tokens[index:])
    raise AssertionError(f"zizmor invocation not found in command: {command}")


def _tracked_action_manifests() -> set[str]:
    """Return every tracked canonical GitHub Action manifest."""
    output = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        path for path in output.splitlines() if Path(path).name in {"action.yml", "action.yaml"}
    }


def _tracked_composite_actions() -> set[str]:
    """Return tracked Action manifests whose runs.using value is composite."""
    composites: set[str] = set()
    for path in _tracked_action_manifests():
        document = yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))
        runs = document.get("runs") if isinstance(document, dict) else None
        if isinstance(runs, dict) and runs.get("using") == "composite":
            composites.add(path)
    return composites


def _audit_ids(fixture: Path, tmp_path: Path) -> set[str]:
    """Run isolated fixed-argv zizmor and return its reported audit IDs."""
    executable = Path(sys.executable).with_name("zizmor")
    assert executable.is_file(), f"zizmor executable not found: {executable}"

    completed = subprocess.run(
        [
            str(executable),
            "--offline",
            "--no-config",
            "--no-ignores",
            "--min-severity",
            "medium",
            "--format=json-v1",
            "-",
        ],
        cwd=tmp_path,
        env={},
        input=fixture.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode in {11, 12, 13, 14}, completed.stderr
    findings = json.loads(completed.stdout)
    return {str(finding["ident"]) for finding in findings}


def test_zizmor_is_a_versioned_dev_dependency() -> None:
    """The project-managed development environment supplies zizmor."""
    config = _pyproject()
    dev_group = config["dependency-groups"]["dev"]  # type: ignore[index]
    assert any(dependency.startswith("zizmor>=") for dependency in dev_group)


def test_precommit_zizmor_flags() -> None:
    """The pre-commit hook runs zizmor with deterministic offline flags."""
    entry = str(_zizmor_precommit_hook()["entry"])
    assert entry.startswith("uv run zizmor")
    for flag in OFFLINE_FLAGS:
        assert flag in entry, f"zizmor pre-commit hook missing {flag!r}"
    assert ".github/" + "workflows/" in entry


def test_required_and_precommit_zizmor_configuration_is_aligned() -> None:
    """Required CI and pre-commit use the complete identical offline command."""
    required = _workflow_zizmor_run(
        REPO_ROOT / ".github/workflows/_required.yml",
        "security-workflow-scan",
    )
    hook = _zizmor_precommit_hook()

    assert _scanner_argv(required) == EXPECTED_OFFLINE_ARGV
    assert tuple(shlex.split(str(hook["entry"]))) == EXPECTED_OFFLINE_ARGV
    assert hook["pass_filenames"] is False


def test_scheduled_zizmor_preserves_online_mode_and_target_parity() -> None:
    """The weekly scan keeps online audits and covers both definition roots."""
    scheduled = _workflow_zizmor_run(
        REPO_ROOT / ".github/workflows/security.yml",
        "workflow-scan",
    )
    assert _scanner_argv(scheduled) == EXPECTED_ONLINE_ARGV


def test_non_production_action_allowlist_is_tracked_and_composite() -> None:
    """Fixture exemptions cannot become stale or cease being composite Actions."""
    tracked = _tracked_action_manifests()
    composites = _tracked_composite_actions()

    assert tracked >= NON_PRODUCTION_ACTION_FIXTURES
    assert composites >= NON_PRODUCTION_ACTION_FIXTURES

    fixture_composites = {
        path for path in composites if path.startswith("tests/unit/ci/fixtures/zizmor/")
    }
    assert fixture_composites == NON_PRODUCTION_ACTION_FIXTURES


def test_every_tracked_composite_action_is_scanned() -> None:
    """Every production composite Action lies within each configured scan."""
    production = _tracked_composite_actions() - NON_PRODUCTION_ACTION_FIXTURES
    assert production, "tracked composite-action inventory is unexpectedly empty"

    uncovered = {
        path for path in production if not any(path.startswith(root) for root in SCAN_ROOTS)
    }
    assert not uncovered, f"tracked composite Actions outside scan scope: {uncovered}"


def test_precommit_trigger_covers_tracked_composite_actions() -> None:
    """Changing any production composite Action triggers the complete hook."""
    hook = _zizmor_precommit_hook()
    trigger = re.compile(str(hook["files"]))
    production = _tracked_composite_actions() - NON_PRODUCTION_ACTION_FIXTURES

    uncovered = {path for path in production if trigger.search(path) is None}
    assert not uncovered, f"pre-commit trigger does not match: {uncovered}"


@pytest.mark.parametrize(
    ("fixture", "expected_audits"),
    [
        ("unpinned_action/action.yml", {"unpinned-uses"}),
        ("unsafe_workflow.yml", {"unpinned-uses", "excessive-permissions"}),
    ],
)
def test_fixed_fixture_scan_keeps_security_audits_active(
    fixture: str,
    expected_audits: set[str],
    tmp_path: Path,
) -> None:
    """Pinning and workflow least-privilege audits remain active."""
    observed = _audit_ids(FIXTURES / fixture, tmp_path)
    assert expected_audits <= observed, f"missing audits: {expected_audits - observed}"


def test_security_md_documents_static_analysis_coverage() -> None:
    """SECURITY.md documents the per-surface static-analysis coverage.

    Issue #2151 requires a documented equivalent for the workflow and shell
    surfaces; the coverage table names zizmor and ShellCheck alongside bandit.
    """
    security_md = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "Static Analysis Coverage" in security_md
    for tool in ("zizmor", "Bandit", "ShellCheck"):
        assert tool in security_md, f"SECURITY.md coverage table missing {tool}"
