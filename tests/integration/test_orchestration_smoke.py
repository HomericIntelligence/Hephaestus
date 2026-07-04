"""Smoke tests for omitted orchestration modules — integration backstop.

These tests validate that the 16 automation modules omitted from coverage
(per pyproject.toml[tool.coverage.run].omit) remain importable and their
console entry points work correctly.

Module enumeration and entry-point discovery verified at plan time:
- All 16 modules are importable (guards against import regressions)
- 5 modules have console scripts: implementer, planner, loop_runner, pr_reviewer, audit_reviewer
- 2 modules are script-less but have main(): address_review, ci_driver
- 9 modules lack main() entirely: implementer_cli (only argument parsing /
    logging setup since #714 relocated main() to implementer),
    implementer_phase_runner, implementer_summary, curses_ui,
    the 4 CIDriver collaborators extracted in #1357 (pr_discovery,
    ci_check_inspector, ci_fix_orchestrator, post_merge_processor), and
    loop_repo_manager (repo-management cluster extracted from loop_runner #1360)
"""

import subprocess
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _omitted_modules() -> list[str]:
    """Derive omitted-module import paths from pyproject.toml (no drift)."""
    root = Path(__file__).resolve().parents[2]
    with open(root / "pyproject.toml", "rb") as f:
        omit = tomllib.load(f)["tool"]["coverage"]["run"]["omit"]
    prefix, suffix = "hephaestus/automation/", ".py"
    return sorted(
        entry[: -len(suffix)].replace("/", ".")
        for entry in omit
        if entry.startswith(prefix) and entry.endswith(suffix)
    )


# All omitted orchestration modules (derived from pyproject.toml omit list).
OMITTED_MODULES = _omitted_modules()

# Modules with console scripts (run --help to verify entry point works)
CONSOLE_SCRIPTS = [
    ("hephaestus-implement-issues", "hephaestus.automation.implementer"),
    ("hephaestus-plan-issues", "hephaestus.automation.planner"),
    ("hephaestus-automation-loop", "hephaestus.automation.loop_runner"),
    ("hephaestus-review-prs", "hephaestus.automation.pr_reviewer"),
    ("hephaestus-audit-prs", "hephaestus.automation.audit_reviewer"),
]

# Modules with main() but no console script of their own.
# ``implementer.main()`` backs the ``hephaestus-implement-issues`` script and is
# covered by CONSOLE_SCRIPTS; since #714, ``implementer_cli`` holds only the
# argument-parsing / logging helpers and no longer defines main().
MAIN_ONLY_MODULES = [
    "hephaestus.automation.address_review",
    "hephaestus.automation.ci_driver",
]


@pytest.mark.integration
class TestOrchestrationsImportable:
    """All omitted modules must remain importable."""

    @pytest.mark.parametrize("module_name", OMITTED_MODULES)
    def test_module_importable(self, module_name: str) -> None:
        """Verify module can be imported without errors."""
        try:
            __import__(module_name)
        except ImportError as e:
            pytest.fail(f"Module {module_name} failed to import: {e}")


@pytest.mark.integration
class TestConsoleScriptsWork:
    """Console scripts must respond to --help without live session."""

    @pytest.mark.parametrize("script_name,module_name", CONSOLE_SCRIPTS)
    def test_console_script_help(self, script_name: str, module_name: str) -> None:
        """Verify console script exits 0 on --help.

        Invokes via ``python -c`` with ``sys.argv`` manipulation so the test
        works without a dev-install (``pip install -e .``) that registers
        console entry-points on PATH.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    f"import sys; sys.argv = ['{script_name}', '--help']; "
                    f"from {module_name} import main; raise SystemExit(main())"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        output = result.stdout + result.stderr
        assert result.returncode == 0, (
            f"Script {script_name} ({module_name}) exited with {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Should print usage text (argparse default)
        assert "usage:" in output.lower(), (
            f"Script {script_name} did not print usage text\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.mark.integration
class TestMainCallable:
    """Modules with main() must have a callable main function."""

    @pytest.mark.parametrize("module_name", MAIN_ONLY_MODULES)
    def test_main_is_callable(self, module_name: str) -> None:
        """Verify module has a callable main() function."""
        module = __import__(module_name, fromlist=["main"])
        assert hasattr(module, "main"), f"Module {module_name} does not have main()"
        assert callable(module.main), f"Module {module_name}.main is not callable"
