"""Test that the omit-allowlist in pyproject.toml is frozen and documented."""

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def get_pyproject_toml_path() -> Path:
    """Find the project root and return path to pyproject.toml."""
    current = Path(__file__).resolve()
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current / "pyproject.toml"
        current = current.parent
    raise RuntimeError("Could not find pyproject.toml")


class TestOmitAllowlist:
    """Tests for the frozen omit-allowlist in pyproject.toml."""

    def test_omit_allowlist_frozen(self) -> None:
        """Verify that [tool.coverage.run].omit contains only the documented modules."""
        pyproject_path = get_pyproject_toml_path()
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        omit_list = pyproject.get("tool", {}).get("coverage", {}).get("run", {}).get("omit", [])

        # Expected omit list: test globs + 16 automation modules
        expected_globs = {
            "*/tests/*",
            "*/__init__.py",
        }
        expected_modules = {
            "hephaestus/automation/implementer.py",
            "hephaestus/automation/implementer_cli.py",
            "hephaestus/automation/implementer_phase_runner.py",
            "hephaestus/automation/implementer_summary.py",
            "hephaestus/automation/planner.py",
            "hephaestus/automation/address_review.py",
            "hephaestus/automation/ci_driver.py",
            # CIDriver collaborators extracted in #1357 (refs #1179, #1289).
            "hephaestus/automation/pr_discovery.py",
            "hephaestus/automation/ci_check_inspector.py",
            "hephaestus/automation/ci_fix_orchestrator.py",
            "hephaestus/automation/post_merge_processor.py",
            "hephaestus/automation/loop_runner.py",
            # loop_repo_manager.py: repo-management cluster extracted from loop_runner.py
            "hephaestus/automation/loop_repo_manager.py",
            "hephaestus/automation/curses_ui.py",
            "hephaestus/automation/pr_reviewer.py",
            "hephaestus/automation/audit_reviewer.py",
        }

        actual_set = set(omit_list)
        expected_set = expected_globs | expected_modules

        # Fail loudly if the omit list has grown or changed
        if actual_set != expected_set:
            removed = expected_set - actual_set
            added = actual_set - expected_set
            msg = "Omit-allowlist mismatch (guards against silent growth):\n"
            if removed:
                msg += f"  Removed (unexpected): {removed}\n"
            if added:
                msg += f"  Added (guard this in code): {added}\n"
            msg += (
                "See tests/unit/validation/test_omit_allowlist.py"
                " and tests/integration/test_orchestration_smoke.py\n"
            )
            msg += "These tests document and enforce the orchestration module omit-list."
            pytest.fail(msg)

        assert actual_set == expected_set
