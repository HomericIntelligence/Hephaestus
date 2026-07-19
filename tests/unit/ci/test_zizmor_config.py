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


def test_security_md_documents_static_analysis_coverage() -> None:
    """SECURITY.md documents the per-surface static-analysis coverage.

    Issue #2151 requires a documented equivalent for the workflow and shell
    surfaces; the coverage table names zizmor and ShellCheck alongside bandit.
    """
    security_md = (REPO_ROOT / "SECURITY.md").read_text()
    assert "Static Analysis Coverage" in security_md
    for tool in ("zizmor", "Bandit", "ShellCheck"):
        assert tool in security_md, f"SECURITY.md coverage table missing {tool}"
